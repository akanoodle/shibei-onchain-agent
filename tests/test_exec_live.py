"""End-to-end integration test for the full pipeline in dry-run mock mode.

Exercises the real wiring: CMC signal -> reconcile -> scan -> universe filter ->
risk stack -> planner -> on-chain execution adapter -> reconcile. Runs with zero
extra deps and no network (everything in mock mode), and asserts high-level
invariants rather than implementation detail, so it stays robust as modules evolve.
"""

from __future__ import annotations

import json

import pytest

from shibei_onchain.config import load_config
from shibei_onchain.models import OrderAction, PlannedOrder, Side


def _mock_env(**overrides):
    env = {
        "SHIBEI_ONCHAIN_DRY_RUN": "true",
        "SHIBEI_ONCHAIN_TRADING_ENABLED": "false",
        "SHIBEI_ONCHAIN_CMC_MODE": "mock",
        "SHIBEI_ONCHAIN_TWAK_MODE": "mock",
        "SHIBEI_ONCHAIN_CHAIN_ID": "97",
        "SHIBEI_ONCHAIN_INITIAL_EQUITY_USD": "1000",
    }
    env.update(overrides)
    return env


def test_full_cycle_dry_run():
    from shibei_onchain.exec_live import OnChainAgent

    config = load_config(_mock_env())
    assert config.live_orders_allowed is False  # gate closed by default

    result = OnChainAgent(config).run_cycle(persist=False)

    # produced a ranked board and at least planned something
    assert isinstance(result.candidates, list)
    assert len(result.candidates) > 0
    # nothing was actually signed — every receipt is a dry-run
    for r in result.receipts:
        assert r.status in ("dry_run", "skipped", "failed")
        assert r.dry_run is True or r.status in ("skipped", "failed")
    # the whole result is JSON-serializable (state persistence works)
    blob = json.dumps(result.to_dict(), ensure_ascii=False, default=str)
    assert "summary" in blob

    summary = result.summary()
    assert summary["live_orders"] is False
    assert summary["candidates"] == len(result.candidates)


def test_execute_prepared_orders_dry_run():
    from shibei_onchain.exec_live import execute_shibei_v3_onchain_prepared_orders

    config = load_config(_mock_env())
    order = PlannedOrder(
        symbol="BNBUSDT",
        side=Side.LONG,
        action=OrderAction.OPEN,
        base_asset="BNB",
        quote_asset="USDT",
        notional_usd=50.0,
        entry_price=600.0,
        stop_loss_price=564.0,
        risk_budget_usd=20.0,
        max_slippage_bps=100,
    )
    out = execute_shibei_v3_onchain_prepared_orders([order], config, ref_prices={"BNB": 600.0})
    assert out["dry_run"] is True
    assert out["live_orders_allowed"] is False
    assert len(out["receipts"]) == 1
    receipt = out["receipts"][0]
    assert receipt.symbol == "BNBUSDT"
    assert receipt.status in ("dry_run", "skipped")
    assert out["account_state"] is not None


def test_short_leg_disabled_by_default():
    from shibei_onchain.exec_live import execute_shibei_v3_onchain_prepared_orders

    config = load_config(_mock_env())
    assert config.enable_short_leg is False
    order = PlannedOrder(
        symbol="ETHUSDT",
        side=Side.SHORT,
        action=OrderAction.OPEN,
        base_asset="ETH",
        notional_usd=50.0,
        entry_price=3000.0,
        stop_loss_price=3180.0,
    )
    out = execute_shibei_v3_onchain_prepared_orders([order], config, ref_prices={"ETH": 3000.0})
    receipt = out["receipts"][0]
    # MVP is long-spot; the short leg is a documented stretch and must not silently trade
    assert receipt.status == "skipped"


def test_unknown_token_skipped():
    from shibei_onchain.exec_live import execute_shibei_v3_onchain_prepared_orders

    config = load_config(_mock_env())
    order = PlannedOrder(
        symbol="TOTALLYFAKEUSDT",
        side=Side.LONG,
        action=OrderAction.OPEN,
        base_asset="TOTALLYFAKE",
        notional_usd=50.0,
        entry_price=1.0,
        stop_loss_price=0.94,
    )
    out = execute_shibei_v3_onchain_prepared_orders([order], config, ref_prices={"TOTALLYFAKE": 1.0})
    receipt = out["receipts"][0]
    assert receipt.status == "skipped"


def test_per_hour_counter_resets_on_hour_rollover(tmp_path):
    """The ≤N-orders/hour budget is a clock-hour limit: a persisted counter from
    a previous hour must reset, otherwise the agent bricks after N lifetime opens."""
    import json
    from datetime import datetime, timedelta, timezone

    from shibei_onchain.exec_live import OnChainAgent, _same_clock_hour

    now = datetime.now(timezone.utc)
    prev_hour = (now - timedelta(hours=2)).isoformat()
    assert _same_clock_hour(now.isoformat(), now) is True
    assert _same_clock_hour(prev_hour, now) is False

    config = load_config(_mock_env(SHIBEI_ONCHAIN_STATE_DIR=str(tmp_path)))
    agent = OnChainAgent(config)

    # stale state (previous hour) with a maxed-out counter -> resets to 0
    agent._state_path().parent.mkdir(parents=True, exist_ok=True)
    agent._state_path().write_text(
        json.dumps({"as_of": prev_hour, "account_after": {"open_orders_this_hour": 3, "stop_loss_events": []}}),
        encoding="utf-8",
    )
    prior_stale = agent._load_prior_account()
    assert prior_stale is not None
    assert prior_stale.open_orders_this_hour == 0

    # fresh state (this hour) -> counter preserved
    agent._state_path().write_text(
        json.dumps({"as_of": now.isoformat(), "account_after": {"open_orders_this_hour": 3, "stop_loss_events": []}}),
        encoding="utf-8",
    )
    prior_fresh = agent._load_prior_account()
    assert prior_fresh is not None
    assert prior_fresh.open_orders_this_hour == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------- #
# account-safety state (high-water mark + day-start equity)
# --------------------------------------------------------------------------- #
def test_safety_state_tracks_peak_and_day_start():
    from datetime import datetime, timezone
    from shibei_onchain.exec_live import OnChainAgent
    from shibei_onchain.models import AccountState

    agent = OnChainAgent(load_config(_mock_env(SHIBEI_ONCHAIN_STRATEGY="water_v02")))
    now = datetime(2026, 6, 21, 10, 0, 0, tzinfo=timezone.utc)

    # day 1: equity 1000 -> peak 1000, day_start 1000
    a1 = AccountState(equity_usd=1000.0)
    s1 = agent._sync_safety_state(a1, {}, now)
    assert s1["peak_equity_usd"] == 1000.0 and s1["day_start_equity_usd"] == 1000.0
    assert a1.peak_equity_usd == 1000.0

    # later same day, equity rose to 1200 -> peak rises, day_start unchanged
    a2 = AccountState(equity_usd=1200.0)
    s2 = agent._sync_safety_state(a2, s1, now)
    assert s2["peak_equity_usd"] == 1200.0 and s2["day_start_equity_usd"] == 1000.0

    # next day, equity 1100 -> day_start resets to 1100, peak stays 1200
    nxt = datetime(2026, 6, 22, 1, 0, 0, tzinfo=timezone.utc)
    a3 = AccountState(equity_usd=1100.0)
    s3 = agent._sync_safety_state(a3, s2, nxt)
    assert s3["peak_equity_usd"] == 1200.0 and s3["day_start_equity_usd"] == 1100.0
    assert s3["day_start_date"] == "2026-06-22"


def test_safety_state_ignores_zero_equity_blip():
    from datetime import datetime, timezone
    from shibei_onchain.exec_live import OnChainAgent
    from shibei_onchain.models import AccountState

    agent = OnChainAgent(load_config(_mock_env()))
    now = datetime(2026, 6, 21, 10, 0, 0, tzinfo=timezone.utc)
    prior = {"peak_equity_usd": 1000.0, "day_start_equity_usd": 1000.0, "day_start_date": "2026-06-21"}
    a = AccountState(equity_usd=0.0)            # transient bad read
    s = agent._sync_safety_state(a, prior, now)
    assert s["peak_equity_usd"] == 1000.0       # unchanged, blip ignored
    assert s["day_start_equity_usd"] == 1000.0


# --------------------------------------------------------------------------- #
# doctor preflight (offline / mock — no network)
# --------------------------------------------------------------------------- #
def test_doctor_preflight_offline_mock():
    import argparse
    from shibei_onchain.cli import cmd_doctor

    cfg = load_config(_mock_env(
        SHIBEI_ONCHAIN_STRATEGY="water_v02",
        SHIBEI_ONCHAIN_VENUE="aster",
        SHIBEI_ONCHAIN_ASTER_MODE="mock",
        SHIBEI_ONCHAIN_BOARD_MODE="off",
        SHIBEI_ONCHAIN_WALLET_ADDRESS="0x" + "ab" * 20,
        SHIBEI_ONCHAIN_PRIVATE_KEY="0x" + "11" * 32,
    ))
    args = argparse.Namespace(json=True)
    rc = cmd_doctor(args, cfg)
    # mock venue has collateral + listings + marks; wallet/key present -> infra ready
    assert rc == 0


def test_doctor_flags_missing_key():
    import argparse
    from shibei_onchain.cli import cmd_doctor

    cfg = load_config(_mock_env(
        SHIBEI_ONCHAIN_VENUE="aster",
        SHIBEI_ONCHAIN_ASTER_MODE="mock",
        SHIBEI_ONCHAIN_BOARD_MODE="off",
        SHIBEI_ONCHAIN_WALLET_ADDRESS="",
        SHIBEI_ONCHAIN_PRIVATE_KEY="",
    ))
    args = argparse.Namespace(json=True)
    rc = cmd_doctor(args, cfg)
    assert rc == 1   # missing wallet + key are critical -> NOT READY
