"""Pure-mock, deterministic unit tests for ``brain/planner.py``.

No network, no third-party deps. Exercises the sizing formula from
``docs/INTERFACES.md`` plus the leverage cap, min-notional floor, stop-distance
clamp and the zero/invalid-price edge case, then the order-building path.
"""

from __future__ import annotations

import math

from shibei_onchain.config import RiskParams
from shibei_onchain.models import (
    AccountState,
    Candidate,
    OrderAction,
    Position,
    Side,
)
from shibei_onchain.brain.planner import build_planned_orders, compute_sizing


def _long(price=100.0, stop=94.0, risk_pct=0.02, **kw):
    """A LONG candidate; default 6% stop (price 100 -> stop 94)."""
    return Candidate(
        symbol=kw.pop("symbol", "BNBUSDT"),
        side=Side.LONG,
        price=price,
        stop_loss_price=stop,
        risk_per_trade_pct=risk_pct,
        take_profit_r_multiple=2.5,
        breakeven_r_multiple=1.0,
        **kw,
    )


# --------------------------------------------------------------------------- #
# core formula
# --------------------------------------------------------------------------- #
def test_canonical_2pct_6pct_stop():
    """2% of 1000 equity, 6% stop -> risk_budget 20, notional ~333.3."""
    risk = RiskParams()
    cand = _long(price=100.0, stop=94.0, risk_pct=0.02)
    s = compute_sizing(cand, 1000.0, risk)

    assert s["risk_budget_usd"] == 20.0
    assert math.isclose(s["stop_distance_pct"], 0.06, rel_tol=1e-9)
    # 20 / 0.06 = 333.333...
    assert math.isclose(s["notional_usd"], 20.0 / 0.06, rel_tol=1e-9)
    # quantity = notional / price
    assert math.isclose(s["quantity"], (20.0 / 0.06) / 100.0, rel_tol=1e-9)
    # 333 < 5000 leverage cap, > 10 floor -> not capped
    assert s["capped"] is False


def test_leverage_cap_applies():
    """A tiny stop distance drives raw notional above equity*max_leverage."""
    risk = RiskParams()  # max_leverage 5
    # 0.1% stop: raw = 20 / 0.001 = 20_000 >> 5_000 cap
    cand = _long(price=100.0, stop=99.9, risk_pct=0.02)
    s = compute_sizing(cand, 1000.0, risk)

    assert s["notional_usd"] == 1000.0 * risk.max_leverage  # 5000
    assert s["capped"] is True
    assert math.isclose(s["quantity"], 5000.0 / 100.0, rel_tol=1e-9)


def test_min_notional_floor_applies():
    """A sub-floor notional is lifted to risk.min_notional_usdt."""
    risk = RiskParams()  # min_notional_usdt 10.0, max_stop_distance_pct 0.20
    # equity 100, 2% -> budget 2; stop 20% -> raw = 2 / 0.20 = 10 ... use wider
    # to land below the floor: budget 0.2 (0.2% risk) / 0.20 = 1.0 < 10.
    cand = _long(price=50.0, stop=40.0, risk_pct=0.002)  # 20% stop
    s = compute_sizing(cand, 100.0, risk)

    assert s["notional_usd"] == risk.min_notional_usdt  # floored to 10
    assert s["capped"] is True
    assert math.isclose(s["quantity"], 10.0 / 50.0, rel_tol=1e-9)


def test_stop_distance_clamped_to_max():
    """A stop wider than max_stop_distance_pct is clamped for sizing."""
    risk = RiskParams()  # max_stop_distance_pct 0.20
    # 50% stop -> clamp to 20%. risk_budget 20; notional = 20/0.20 = 100.
    cand = _long(price=100.0, stop=50.0, risk_pct=0.02)
    s = compute_sizing(cand, 1000.0, risk)

    assert math.isclose(s["stop_distance_pct"], 0.20, rel_tol=1e-9)
    assert math.isclose(s["notional_usd"], 100.0, rel_tol=1e-9)
    assert s["capped"] is True


# --------------------------------------------------------------------------- #
# edge cases
# --------------------------------------------------------------------------- #
def test_zero_price_is_unsizable():
    """price<=0 -> stop_distance 0 -> notional 0, quantity 0, capped True."""
    risk = RiskParams()
    cand = _long(price=0.0, stop=0.0, risk_pct=0.02)
    s = compute_sizing(cand, 1000.0, risk)

    assert s["notional_usd"] == 0.0
    assert s["quantity"] == 0.0
    assert s["capped"] is True


def test_stop_equals_entry_is_unsizable():
    """Zero stop distance (stop == entry) cannot be sized."""
    risk = RiskParams()
    cand = _long(price=100.0, stop=100.0, risk_pct=0.02)
    s = compute_sizing(cand, 1000.0, risk)

    assert s["stop_distance_pct"] == 0.0
    assert s["notional_usd"] == 0.0
    assert s["quantity"] == 0.0
    assert s["capped"] is True


def test_zero_equity_is_unsizable():
    """No equity -> no risk budget -> unsizable."""
    risk = RiskParams()
    cand = _long(price=100.0, stop=94.0, risk_pct=0.02)
    s = compute_sizing(cand, 0.0, risk)

    assert s["risk_budget_usd"] == 0.0
    assert s["notional_usd"] == 0.0
    assert s["quantity"] == 0.0
    assert s["capped"] is True


# --------------------------------------------------------------------------- #
# order building
# --------------------------------------------------------------------------- #
def test_build_planned_orders_maps_fields():
    risk = RiskParams()
    account = AccountState(equity_usd=1000.0)
    cand = _long(
        price=100.0,
        stop=94.0,
        risk_pct=0.02,
        symbol="ETHUSDT",
        strategy_id="S16A",
        strategy_leg="v0",
        signal_key="sig-1",
        score=12.5,
    )

    orders = build_planned_orders([cand], account, risk)
    assert len(orders) == 1
    o = orders[0]

    assert o.action is OrderAction.OPEN
    assert o.side is Side.LONG
    assert o.symbol == "ETHUSDT"
    assert o.base_asset == "ETH"
    assert o.quote_asset == "USDT"
    assert o.max_slippage_bps == 100
    assert o.entry_price == 100.0
    assert o.stop_loss_price == 94.0
    assert o.risk_budget_usd == 20.0
    assert math.isclose(o.notional_usd, 20.0 / 0.06, rel_tol=1e-9)
    assert math.isclose(o.quantity, (20.0 / 0.06) / 100.0, rel_tol=1e-9)
    assert o.strategy_id == "S16A"
    assert o.strategy_leg == "v0"
    assert o.signal_key == "sig-1"
    assert o.take_profit_r_multiple == 2.5
    assert o.breakeven_r_multiple == 1.0
    # metadata carries sizing diagnostics
    assert math.isclose(o.metadata["stop_distance_pct"], 0.06, rel_tol=1e-9)
    assert o.metadata["sizing_capped"] is False
    assert o.metadata["score"] == 12.5


def test_build_planned_orders_uses_account_equity():
    """Notional must scale with the account's live equity."""
    risk = RiskParams()
    cand = _long(price=100.0, stop=94.0, risk_pct=0.02)

    small = build_planned_orders([cand], AccountState(equity_usd=1000.0), risk)[0]
    big = build_planned_orders([cand], AccountState(equity_usd=2000.0), risk)[0]
    assert math.isclose(big.notional_usd, small.notional_usd * 2.0, rel_tol=1e-9)


def test_build_planned_orders_empty():
    risk = RiskParams()
    account = AccountState(equity_usd=1000.0)
    assert build_planned_orders([], account, risk) == []


def test_short_candidate_sizes_symmetrically():
    """SHORT uses the same |entry-stop|/entry stop distance as LONG."""
    risk = RiskParams()
    short = Candidate(
        symbol="BTCUSDT",
        side=Side.SHORT,
        price=100.0,
        stop_loss_price=106.0,  # 6% above entry
        risk_per_trade_pct=0.02,
    )
    s = compute_sizing(short, 1000.0, risk)
    assert math.isclose(s["stop_distance_pct"], 0.06, rel_tol=1e-9)
    assert math.isclose(s["notional_usd"], 20.0 / 0.06, rel_tol=1e-9)


def test_account_with_positions_still_sizes_off_equity():
    """Existing positions don't change per-candidate sizing (only equity does)."""
    risk = RiskParams()
    pos = Position(
        symbol="BNBUSDT",
        side=Side.LONG,
        base_asset="BNB",
        quantity=1.0,
        entry_price=100.0,
        notional_usd=100.0,
    )
    account = AccountState(equity_usd=1000.0, positions=[pos])
    cand = _long(price=100.0, stop=94.0, risk_pct=0.02, symbol="ETHUSDT")
    o = build_planned_orders([cand], account, risk)[0]
    assert math.isclose(o.notional_usd, 20.0 / 0.06, rel_tol=1e-9)
