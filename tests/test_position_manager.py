"""Tests for the active position-management layer (Tier 2):

    * ``brain.position_manager.plan_position_management`` — the pure planner
      (+1R breakeven move, gentle max-hold backstop).
    * the Aster adapter's ``MOVE_STOP`` execution + ``detect_exits`` wiring.

All deterministic, no network: Aster runs in ``mock`` mode.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shibei_onchain.config import load_config
from shibei_onchain.models import AccountState, OrderAction, PlannedOrder, Position, Side
from shibei_onchain.onchain.aster_perp import AsterPerpClient
from shibei_onchain.onchain.aster_adapter import AsterExecutionAdapter
from shibei_onchain.brain.position_manager import plan_position_management


NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


def _pos(symbol="ETHUSDT", base="ETH", side=Side.LONG, entry=2000.0, stop=1880.0,
         current=2000.0, qty=0.01, opened_h_ago=1.0):
    p = Position(
        symbol=symbol, side=side, base_asset=base, quantity=qty,
        entry_price=entry, notional_usd=qty * current, current_price=current,
        stop_loss_price=stop, breakeven_r_multiple=1.0,
        opened_at=(NOW - timedelta(hours=opened_h_ago)).isoformat(),
    )
    return p


# --------------------------------------------------------------------------- #
# pure planner — +1R breakeven move
# --------------------------------------------------------------------------- #
def test_no_action_when_flat_and_young():
    # entry 2000, stop 1880 -> 1R = 120; current == entry -> 0R; 1h old.
    orders = plan_position_management([_pos()], now=NOW, breakeven_r=1.0, long_max_hold_hours=72.0)
    assert orders == []


def test_breakeven_move_fires_at_1r():
    # current 2120 = entry + 120 = +1R exactly -> move stop to entry.
    p = _pos(current=2120.0)
    orders = plan_position_management([p], now=NOW, breakeven_r=1.0, long_max_hold_hours=72.0)
    assert len(orders) == 1
    o = orders[0]
    assert o.action is OrderAction.MOVE_STOP
    assert o.stop_loss_price == 2000.0          # breakeven == entry
    assert o.reason == "move_to_breakeven"
    assert o.metadata["old_stop"] == 1880.0


def test_no_breakeven_move_below_1r():
    p = _pos(current=2100.0)                      # +100 / 120 = 0.83R < 1R
    orders = plan_position_management([p], now=NOW, breakeven_r=1.0, long_max_hold_hours=72.0)
    assert orders == []


def test_no_remove_when_stop_already_at_breakeven():
    # stop already at entry -> nothing to do even at +2R.
    p = _pos(stop=2000.0, current=2240.0)
    orders = plan_position_management([p], now=NOW, breakeven_r=1.0, long_max_hold_hours=72.0)
    assert orders == []


def test_short_breakeven_move():
    # short: entry 2000, stop 2120 (1R=120 up); current 1880 = +1R for a short.
    p = _pos(side=Side.SHORT, entry=2000.0, stop=2120.0, current=1880.0)
    orders = plan_position_management([p], now=NOW, breakeven_r=1.0, long_max_hold_hours=0.0)
    assert len(orders) == 1 and orders[0].action is OrderAction.MOVE_STOP
    assert orders[0].stop_loss_price == 2000.0


# --------------------------------------------------------------------------- #
# pure planner — max-hold backstop
# --------------------------------------------------------------------------- #
def test_max_hold_close_fires_when_aged_out():
    p = _pos(opened_h_ago=80.0, current=2120.0)   # 80h > 72h, and also +1R
    orders = plan_position_management([p], now=NOW, breakeven_r=1.0, long_max_hold_hours=72.0)
    # CLOSE wins over MOVE_STOP
    assert len(orders) == 1
    assert orders[0].action is OrderAction.CLOSE
    assert orders[0].reason == "max_hold_backstop"
    assert orders[0].metadata["age_hours"] >= 72.0


def test_max_hold_off_when_zero():
    p = _pos(opened_h_ago=500.0)
    orders = plan_position_management([p], now=NOW, breakeven_r=1.0, long_max_hold_hours=0.0)
    assert orders == []


def test_skips_degenerate_positions():
    p = _pos(qty=0.0)
    assert plan_position_management([p], now=NOW, long_max_hold_hours=72.0) == []


# --------------------------------------------------------------------------- #
# Aster adapter — MOVE_STOP execution + detect_exits wiring
# --------------------------------------------------------------------------- #
def _adapter():
    cfg = load_config({
        "SHIBEI_ONCHAIN_VENUE": "aster",
        "SHIBEI_ONCHAIN_ASTER_MODE": "mock",
        "SHIBEI_ONCHAIN_STRATEGY": "water_v02",
    })
    return AsterExecutionAdapter(cfg, AsterPerpClient(cfg.aster)), cfg


def _move_stop_order(symbol="ETHUSDT", base="ETH", entry=2000.0, qty=0.01):
    return PlannedOrder(
        symbol=symbol, side=Side.LONG, action=OrderAction.MOVE_STOP, base_asset=base,
        quantity=qty, entry_price=entry, stop_loss_price=entry, take_profit_r_multiple=2.5,
        reason="move_to_breakeven",
    )


def test_aster_move_stop_dry_run():
    adapter, _ = _adapter()
    r = adapter.execute_order(_move_stop_order(), dry_run=True, ref_price=2120.0)
    assert r.status == "dry_run"
    assert r.action is OrderAction.MOVE_STOP
    assert r.raw["action"] == "move_stop"
    assert r.raw["new_stop"] == 2000.0          # stop moved to entry (rounded)
    assert r.raw["take_profit_price"] > 2000.0  # 2.5R TP preserved above entry


def test_aster_move_stop_live_mock_replaces_protection():
    adapter, _ = _adapter()
    r = adapter.execute_order(_move_stop_order(), dry_run=False, ref_price=2120.0)
    assert r.status == "filled"
    assert r.raw["cancelled"] is True
    assert r.raw["stop_order"] and r.raw["take_profit_order"]


def test_aster_detect_exits_emits_breakeven_for_enriched_position():
    adapter, cfg = _adapter()
    # a long that's up +1R with its stop still below entry
    pos = Position(
        symbol="ETHUSDT", side=Side.LONG, base_asset="ETH", quantity=0.01,
        entry_price=2000.0, notional_usd=21.2, current_price=2120.0,
        stop_loss_price=1880.0, breakeven_r_multiple=1.0,
        opened_at=(NOW - timedelta(hours=1)).isoformat(),
    )
    acct = AccountState(equity_usd=1000.0, positions=[pos])
    exits = adapter.detect_exits(acct, cfg.risk, now=NOW)
    assert len(exits) == 1 and exits[0].action is OrderAction.MOVE_STOP


if __name__ == "__main__":  # pragma: no cover
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
