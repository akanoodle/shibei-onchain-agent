"""Tests for the Aster execution venue (both legs) + venue routing."""

from __future__ import annotations

from shibei_onchain.config import load_config
from shibei_onchain.models import OrderAction, PlannedOrder, Side
from shibei_onchain.onchain.aster_perp import AsterPerpClient
from shibei_onchain.onchain.aster_adapter import AsterExecutionAdapter


def _config(**overrides):
    env = {
        "SHIBEI_ONCHAIN_VENUE": "aster",
        "SHIBEI_ONCHAIN_ASTER_MODE": "mock",
        "SHIBEI_ONCHAIN_DRY_RUN": "true",
    }
    env.update(overrides)
    return load_config(env)


def _adapter(config=None):
    config = config or _config()
    return AsterExecutionAdapter(config, AsterPerpClient(config.aster))


def _order(symbol="BNBUSDT", base="BNB", side=Side.LONG, action=OrderAction.OPEN,
           notional=120.0, qty=0.2, entry=600.0, stop=564.0):
    return PlannedOrder(
        symbol=symbol, side=side, action=action, base_asset=base,
        notional_usd=notional, quantity=qty, entry_price=entry, stop_loss_price=stop,
        take_profit_r_multiple=2.5,
    )


def test_config_defaults_to_aster_with_short_enabled():
    c = _config()
    assert c.venue == "aster"
    assert c.enable_short_leg is True


def test_filter_keeps_listed_skips_junk():
    adapter = _adapter()
    from shibei_onchain.models import Candidate

    cands = [
        Candidate(symbol="BNBUSDT", side=Side.LONG, price=600.0, stop_loss_price=564.0),
        Candidate(symbol="TOTALLYFAKEUSDT", side=Side.LONG, price=1.0, stop_loss_price=0.9),
    ]
    kept, skipped = adapter.filter_candidates(cands)
    assert [c.symbol for c in kept] == ["BNBUSDT"]
    assert skipped and skipped[0].reason == "not_aster_listed"


def test_read_account_mock_has_collateral_no_positions():
    acct = _adapter().read_account()
    assert acct.equity_usd > 0
    assert acct.positions == []
    assert acct.source == "aster_mock"


def test_open_long_dry_run_receipt():
    adapter = _adapter()
    r = adapter.execute_order(_order(side=Side.LONG), dry_run=True)
    assert r.status == "dry_run"
    assert r.raw["action"] == "open"
    assert r.raw["position_side"] == "LONG"
    # TP for a long at entry 600 / stop 564 (risk 36) and 2.5R = 600 + 90 = 690
    assert abs(r.raw["take_profit_price"] - 690.0) < 1e-6
    assert r.raw["stop_price"] == 564.0


def test_open_short_dry_run_receipt():
    adapter = _adapter()
    r = adapter.execute_order(
        _order(symbol="ETHUSDT", base="ETH", side=Side.SHORT, entry=3000.0, stop=3180.0),
        dry_run=True,
    )
    assert r.status == "dry_run"
    assert r.raw["position_side"] == "SHORT"


def test_open_long_live_mock_fills_and_reconciles():
    config = _config()
    client = AsterPerpClient(config.aster)
    adapter = AsterExecutionAdapter(config, client)
    r = adapter.execute_order(_order(side=Side.LONG, qty=0.5), dry_run=False)
    assert r.status == "filled"
    assert r.tx_hash  # order id
    acct = adapter.read_account()
    assert any(p.symbol == "BNBUSDT" and p.side is Side.LONG for p in acct.positions)


def test_close_reduce_only():
    config = _config()
    client = AsterPerpClient(config.aster)
    adapter = AsterExecutionAdapter(config, client)
    adapter.execute_order(_order(side=Side.LONG, qty=0.5), dry_run=False)
    r = adapter.execute_order(_order(action=OrderAction.CLOSE, side=Side.LONG, qty=0.5), dry_run=False)
    assert r.status == "filled"
    assert all(p.symbol != "BNBUSDT" for p in adapter.read_account().positions)


def test_unknown_symbol_skipped():
    adapter = _adapter()
    r = adapter.execute_order(_order(symbol="FAKEUSDT", base="FAKE"), dry_run=True)
    assert r.status == "skipped"
    assert r.error == "not_aster_listed"


def test_detect_exits_empty_on_aster():
    # exits rest on-exchange (stop + TP placed at open), so nothing to do here.
    adapter = _adapter()
    acct = adapter.read_account()
    assert adapter.detect_exits(acct, _config().risk) == []


def test_venue_routing_short_not_skipped():
    # via the named entry point, a SHORT under venue=aster must open (not skip).
    from shibei_onchain.exec_live import execute_shibei_v3_onchain_prepared_orders

    config = _config()
    out = execute_shibei_v3_onchain_prepared_orders(
        [_order(symbol="ETHUSDT", base="ETH", side=Side.SHORT, entry=3000.0, stop=3180.0)],
        config,
    )
    assert out["venue"] == "aster"
    assert out["receipts"][0].status in ("dry_run", "filled")  # NOT skipped
