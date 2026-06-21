"""Exhaustive, pure-mock, deterministic unit tests for ``brain/risk.py``.

This is the most important test file in the repo: ``brain.risk.apply_risk_stack``
is 拾贝's risk stack — the project's differentiator — so every rule and its exact
ordering is exercised here, plus edge cases. No network, no third-party deps.

The eight ordered checks (see ``docs/INTERFACES.md``):

    1. regime gate per side
    2. invalid price
    3. stop distance too wide
    4. within-batch long/short conflict (both legs rejected)
    5. opposite existing position
    6. same-side existing position (no stacking)
    7. same-coin 24h circuit breaker
    8. portfolio caps: orders/hour, total initial risk, total notional
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from shibei_onchain.config import RiskParams
from shibei_onchain.models import (
    AccountState,
    BtcTrend,
    Candidate,
    MarketRegime,
    MarketSignal,
    Position,
    Side,
    SkippedOrder,
    StopLossEvent,
)
from shibei_onchain.brain.risk import apply_risk_stack


NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _long(symbol="BNBUSDT", price=100.0, stop=94.0, risk_pct=0.02, **kw):
    return Candidate(
        symbol=symbol,
        side=Side.LONG,
        price=price,
        stop_loss_price=stop,
        risk_per_trade_pct=risk_pct,
        take_profit_r_multiple=2.5,
        breakeven_r_multiple=1.0,
        **kw,
    )


def _short(symbol="BNBUSDT", price=100.0, stop=106.0, risk_pct=0.02, **kw):
    return Candidate(
        symbol=symbol,
        side=Side.SHORT,
        price=price,
        stop_loss_price=stop,
        risk_per_trade_pct=risk_pct,
        take_profit_r_multiple=2.5,
        breakeven_r_multiple=1.0,
        **kw,
    )


def _position(symbol="BNBUSDT", side=Side.LONG, base="BNB", entry=100.0,
              stop=94.0, notional=100.0, qty=1.0):
    return Position(
        symbol=symbol,
        side=side,
        base_asset=base,
        quantity=qty,
        entry_price=entry,
        notional_usd=notional,
        stop_loss_price=stop,
    )


def _neutral_signal():
    """A benign signal: nothing is gated (NEUTRAL regime, FLAT trend)."""
    return MarketSignal(regime=MarketRegime.NEUTRAL, btc_trend=BtcTrend.FLAT)


def _account(equity=1000.0, **kw):
    return AccountState(equity_usd=equity, **kw)


def _reasons(decision):
    return [s.reason for s in decision.rejected]


def _approved_symbols(decision):
    return [c.symbol for c in decision.approved]


# --------------------------------------------------------------------------- #
# happy path / baseline
# --------------------------------------------------------------------------- #
def test_single_clean_long_is_approved():
    risk = RiskParams()
    d = apply_risk_stack([_long()], _account(), _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == ["BNBUSDT"]
    assert d.rejected == []
    # every rejection that *would* exist must carry stage="risk"; here none.
    assert all(s.stage == "risk" for s in d.rejected)


def test_empty_candidates_returns_empty_decision():
    risk = RiskParams()
    d = apply_risk_stack([], _account(), _neutral_signal(), risk, now=NOW)
    assert d.approved == []
    assert d.rejected == []


def test_rank_order_preserved_in_approved():
    risk = RiskParams()
    cands = [
        _long(symbol="BNBUSDT"),
        _long(symbol="ETHUSDT"),
        _long(symbol="SOLUSDT"),
    ]
    d = apply_risk_stack(cands, _account(), _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == ["BNBUSDT", "ETHUSDT", "SOLUSDT"]


# --------------------------------------------------------------------------- #
# check 1 — regime gate (BTC environment), per side
# --------------------------------------------------------------------------- #
def test_btc_gate_blocks_long_on_risk_off():
    risk = RiskParams()
    sig = MarketSignal(regime=MarketRegime.RISK_OFF, btc_trend=BtcTrend.FLAT)
    d = apply_risk_stack([_long()], _account(), sig, risk, now=NOW)
    assert _approved_symbols(d) == []
    assert _reasons(d) == ["regime_gate_blocked"]
    assert d.rejected[0].stage == "risk"
    assert d.rejected[0].detail["side"] == "long"
    # the underlying gate reason code is carried in detail for visibility
    assert "regime_risk_off" in d.rejected[0].detail["gate_reasons"]


def test_btc_gate_blocks_long_on_downtrend_but_allows_short():
    """A weak BTC tape (down trend) blocks longs but *favours* shorts."""
    risk = RiskParams()
    sig = MarketSignal(regime=MarketRegime.NEUTRAL, btc_trend=BtcTrend.DOWN)
    long_c = _long(symbol="BNBUSDT")
    short_c = _short(symbol="ETHUSDT")
    d = apply_risk_stack([long_c, short_c], _account(), sig, risk, now=NOW)
    assert _approved_symbols(d) == ["ETHUSDT"]  # short survives
    assert _reasons(d) == ["regime_gate_blocked"]
    assert d.rejected[0].side is Side.LONG


def test_hard_risk_flag_blocks_both_sides():
    """A hard flag (market_halt) blocks longs AND shorts."""
    risk = RiskParams()
    sig = MarketSignal(
        regime=MarketRegime.NEUTRAL,
        btc_trend=BtcTrend.FLAT,
        risk_flags=["market_halt"],
    )
    d = apply_risk_stack(
        [_long(symbol="BNBUSDT"), _short(symbol="ETHUSDT")],
        _account(),
        sig,
        risk,
        now=NOW,
    )
    assert _approved_symbols(d) == []
    assert _reasons(d) == ["regime_gate_blocked", "regime_gate_blocked"]
    sides = {s.detail["side"] for s in d.rejected}
    assert sides == {"long", "short"}


def test_disabled_gate_allows_long_in_risk_off():
    risk = RiskParams(btc_gate_enabled=False)
    sig = MarketSignal(regime=MarketRegime.RISK_OFF, btc_trend=BtcTrend.DOWN)
    d = apply_risk_stack([_long()], _account(), sig, risk, now=NOW)
    assert _approved_symbols(d) == ["BNBUSDT"]
    assert d.rejected == []


# --------------------------------------------------------------------------- #
# check 2 — invalid price
# --------------------------------------------------------------------------- #
def test_invalid_price_zero_entry():
    risk = RiskParams()
    d = apply_risk_stack(
        [_long(price=0.0, stop=0.0)], _account(), _neutral_signal(), risk, now=NOW
    )
    assert _approved_symbols(d) == []
    assert _reasons(d) == ["invalid_price"]


def test_invalid_price_zero_stop():
    risk = RiskParams()
    d = apply_risk_stack(
        [_long(price=100.0, stop=0.0)], _account(), _neutral_signal(), risk, now=NOW
    )
    assert _reasons(d) == ["invalid_price"]


def test_invalid_price_negative():
    risk = RiskParams()
    d = apply_risk_stack(
        [_long(price=-5.0, stop=-6.0)], _account(), _neutral_signal(), risk, now=NOW
    )
    assert _reasons(d) == ["invalid_price"]


# --------------------------------------------------------------------------- #
# check 3 — stop distance too wide
# --------------------------------------------------------------------------- #
def test_stop_distance_too_wide_rejected():
    risk = RiskParams()  # max_stop_distance_pct = 0.20
    # 100 -> 70 is a 30% stop, above the 20% ceiling.
    d = apply_risk_stack(
        [_long(price=100.0, stop=70.0)], _account(), _neutral_signal(), risk, now=NOW
    )
    assert _approved_symbols(d) == []
    assert _reasons(d) == ["stop_distance_too_wide"]
    assert math.isclose(d.rejected[0].detail["stop_distance_pct"], 0.30, rel_tol=1e-9)


def test_stop_distance_exactly_at_max_is_allowed():
    """Exactly at the ceiling is allowed (strictly greater is rejected)."""
    risk = RiskParams()  # 0.20
    d = apply_risk_stack(
        [_long(price=100.0, stop=80.0)], _account(), _neutral_signal(), risk, now=NOW
    )
    assert _approved_symbols(d) == ["BNBUSDT"]


# --------------------------------------------------------------------------- #
# check 4 — within-batch opposite-side conflict (BOTH legs rejected)
# --------------------------------------------------------------------------- #
def test_within_batch_conflict_rejects_both_legs():
    risk = RiskParams()
    long_c = _long(symbol="BNBUSDT")
    short_c = _short(symbol="BNBUSDT")
    d = apply_risk_stack(
        [long_c, short_c], _account(), _neutral_signal(), risk, now=NOW
    )
    assert _approved_symbols(d) == []
    assert _reasons(d) == [
        "same_symbol_opposite_side_conflict",
        "same_symbol_opposite_side_conflict",
    ]
    # both legs, distinct sides, same base
    sides = {s.side for s in d.rejected}
    assert sides == {Side.LONG, Side.SHORT}
    assert all(s.detail["base_asset"] == "BNB" for s in d.rejected)


def test_within_batch_conflict_only_affects_conflicted_base():
    """A clean candidate on a different base still survives the conflict."""
    risk = RiskParams()
    d = apply_risk_stack(
        [
            _long(symbol="BNBUSDT"),
            _short(symbol="BNBUSDT"),
            _long(symbol="ETHUSDT"),
        ],
        _account(),
        _neutral_signal(),
        risk,
        now=NOW,
    )
    assert _approved_symbols(d) == ["ETHUSDT"]
    assert _reasons(d) == [
        "same_symbol_opposite_side_conflict",
        "same_symbol_opposite_side_conflict",
    ]


def test_two_same_side_candidates_are_not_a_conflict():
    """Two LONGs on the same base is not an opposite-side conflict (check 4)."""
    risk = RiskParams()
    d = apply_risk_stack(
        [_long(symbol="BNBUSDT"), _long(symbol="BNBUSDT")],
        _account(),
        _neutral_signal(),
        risk,
        now=NOW,
    )
    # No within-batch *opposite* conflict; both pass check 4. (They are not
    # rejected as conflict — any later cap is a different reason.)
    assert all(s.reason != "same_symbol_opposite_side_conflict" for s in d.rejected)
    assert "BNBUSDT" in _approved_symbols(d)


# --------------------------------------------------------------------------- #
# check 5 — opposite existing position
# --------------------------------------------------------------------------- #
def test_opposite_existing_position_rejected():
    risk = RiskParams()
    acct = _account(positions=[_position(side=Side.SHORT, base="BNB")])
    d = apply_risk_stack([_long(symbol="BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == []
    assert _reasons(d) == ["opposite_position_exists"]
    assert d.rejected[0].detail["existing_side"] == "short"


# --------------------------------------------------------------------------- #
# check 6 — same-side existing position (no stacking)
# --------------------------------------------------------------------------- #
def test_same_side_existing_position_rejected():
    risk = RiskParams()
    acct = _account(positions=[_position(side=Side.LONG, base="BNB")])
    d = apply_risk_stack([_long(symbol="BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == []
    assert _reasons(d) == ["position_already_open"]
    assert d.rejected[0].detail["existing_side"] == "long"


def test_position_on_other_base_does_not_block():
    risk = RiskParams()
    acct = _account(positions=[_position(symbol="ETHUSDT", base="ETH", side=Side.LONG)])
    d = apply_risk_stack([_long(symbol="BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == ["BNBUSDT"]


# --------------------------------------------------------------------------- #
# check 7 — same-coin 24h circuit breaker (>=2 stop-loss events in window)
# --------------------------------------------------------------------------- #
def test_circuit_breaker_two_recent_stops_blocks():
    risk = RiskParams()  # count 2, window 24h
    events = [
        StopLossEvent(base_asset="BNB", side=Side.LONG,
                      at=(NOW - timedelta(hours=1)).isoformat()),
        StopLossEvent(base_asset="BNB", side=Side.LONG,
                      at=(NOW - timedelta(hours=5)).isoformat()),
    ]
    acct = _account(stop_loss_events=events)
    d = apply_risk_stack([_long(symbol="BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == []
    assert _reasons(d) == ["circuit_breaker_24h"]
    assert d.rejected[0].detail["recent_stoploss_count"] == 2


def test_circuit_breaker_one_recent_stop_allows():
    risk = RiskParams()
    events = [
        StopLossEvent(base_asset="BNB", side=Side.LONG,
                      at=(NOW - timedelta(hours=1)).isoformat()),
    ]
    acct = _account(stop_loss_events=events)
    d = apply_risk_stack([_long(symbol="BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == ["BNBUSDT"]


def test_circuit_breaker_stale_stops_outside_window_ignored():
    """Two stops, but both older than 24h -> not counted -> allowed."""
    risk = RiskParams()
    events = [
        StopLossEvent(base_asset="BNB", side=Side.LONG,
                      at=(NOW - timedelta(hours=30)).isoformat()),
        StopLossEvent(base_asset="BNB", side=Side.LONG,
                      at=(NOW - timedelta(hours=48)).isoformat()),
    ]
    acct = _account(stop_loss_events=events)
    d = apply_risk_stack([_long(symbol="BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == ["BNBUSDT"]


def test_circuit_breaker_counts_only_matching_base():
    """Stops on a different coin do not trip the breaker for this coin."""
    risk = RiskParams()
    events = [
        StopLossEvent(base_asset="ETH", side=Side.LONG,
                      at=(NOW - timedelta(hours=1)).isoformat()),
        StopLossEvent(base_asset="ETH", side=Side.LONG,
                      at=(NOW - timedelta(hours=2)).isoformat()),
    ]
    acct = _account(stop_loss_events=events)
    d = apply_risk_stack([_long(symbol="BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == ["BNBUSDT"]


def test_circuit_breaker_handles_z_suffix_timestamps():
    """ISO timestamps with a trailing Z are parsed (3.9 fromisoformat won't)."""
    risk = RiskParams()
    events = [
        StopLossEvent(base_asset="BNB", side=Side.LONG, at="2026-06-15T11:00:00Z"),
        StopLossEvent(base_asset="BNB", side=Side.LONG, at="2026-06-15T07:00:00Z"),
    ]
    acct = _account(stop_loss_events=events)
    d = apply_risk_stack([_long(symbol="BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _reasons(d) == ["circuit_breaker_24h"]


# --------------------------------------------------------------------------- #
# check 8a — >3 new orders per hour
# --------------------------------------------------------------------------- #
def test_max_orders_per_hour_caps_to_three():
    risk = RiskParams()  # max 3
    cands = [
        _long(symbol="BNBUSDT"),
        _long(symbol="ETHUSDT"),
        _long(symbol="SOLUSDT"),
        _long(symbol="ADAUSDT"),
        _long(symbol="XRPUSDT"),
    ]
    d = apply_risk_stack(cands, _account(), _neutral_signal(), risk, now=NOW)
    # first three (rank order) approved, the rest rejected for orders/hour
    assert _approved_symbols(d) == ["BNBUSDT", "ETHUSDT", "SOLUSDT"]
    assert _reasons(d) == ["max_orders_per_hour", "max_orders_per_hour"]


def test_max_orders_per_hour_respects_already_open_this_hour():
    """Two already opened this hour -> only one more allowed."""
    risk = RiskParams()  # max 3
    acct = _account(open_orders_this_hour=2)
    cands = [_long(symbol="BNBUSDT"), _long(symbol="ETHUSDT"), _long(symbol="SOLUSDT")]
    d = apply_risk_stack(cands, acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == ["BNBUSDT"]
    assert _reasons(d) == ["max_orders_per_hour", "max_orders_per_hour"]


def test_max_orders_per_hour_already_at_cap_rejects_all():
    risk = RiskParams()
    acct = _account(open_orders_this_hour=3)
    d = apply_risk_stack([_long()], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == []
    assert _reasons(d) == ["max_orders_per_hour"]


# --------------------------------------------------------------------------- #
# check 8b — >12% total initial risk
# --------------------------------------------------------------------------- #
def test_max_total_initial_risk_blocks_new_open():
    """Existing book already at 11% initial risk; a fresh 2% would breach 12%."""
    risk = RiskParams()  # max_total_initial_risk_pct = 0.12, equity 1000
    # A position whose initial risk = notional*|entry-stop|/entry.
    # entry 100, stop 89 -> 11% risk fraction; notional 1000 -> $110 = 11% of equity.
    pos = _position(symbol="ETHUSDT", base="ETH", side=Side.LONG,
                    entry=100.0, stop=89.0, notional=1000.0)
    acct = _account(positions=[pos])
    # New long risk_budget = 2% of 1000 = $20 -> 11% + 2% = 13% > 12% -> reject.
    d = apply_risk_stack([_long(symbol="BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == []
    assert _reasons(d) == ["max_total_initial_risk"]


def test_max_total_initial_risk_accumulates_within_batch():
    """No existing book, but six 2%-risk candidates = 12% (ok); the 7th breaches.

    Isolate this cap from the orders/hour cap by raising the order limit.
    """
    risk = RiskParams(max_new_orders_per_hour=100)
    cands = [_long(symbol="C{}USDT".format(i)) for i in range(7)]
    d = apply_risk_stack(cands, _account(), _neutral_signal(), risk, now=NOW)
    # 6 * 2% = 12% (not strictly > 12%, allowed); 7th -> 14% > 12% rejected.
    assert len(d.approved) == 6
    assert _reasons(d) == ["max_total_initial_risk"]


# --------------------------------------------------------------------------- #
# check 8c — >5x total notional
# --------------------------------------------------------------------------- #
def test_max_total_notional_blocks_new_open():
    """Existing notional already near 5x equity; a new open breaches the cap."""
    # equity 1000 -> notional cap = 5000. Existing notional 4900.
    # Keep existing initial risk tiny so cap (b) doesn't fire first: tight 0.2%
    # stop on the existing position => risk = 4900 * 0.002 = $9.8 (well under 12%).
    risk = RiskParams()
    pos = _position(symbol="ETHUSDT", base="ETH", side=Side.LONG,
                    entry=100.0, stop=99.8, notional=4900.0)
    acct = _account(positions=[pos])
    # New long sized at price 100 / stop 94 -> notional ~333 -> 4900+333 > 5000.
    d = apply_risk_stack([_long(symbol="BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == []
    assert _reasons(d) == ["max_total_notional"]


def test_max_total_notional_accumulates_within_batch():
    """Many low-risk, high-notional candidates pile up to breach 5x notional.

    Use a wide-ish 20% stop with low risk_pct so notional stays modest per
    trade and the initial-risk cap is not the binding one; raise the order cap
    so orders/hour is not binding either. The notional cap should bind.
    """
    # risk_pct 0.02, 20% stop -> per-trade notional = 20 / 0.20 = 100, risk $20.
    # That's only 0.1x equity/trade and 2% risk/trade. To breach 5x (=5000) we'd
    # need >50 of them, but 12% risk caps at 6. Instead use a tighter stop so the
    # notional cap binds before the risk cap: 1% stop -> notional = 20/0.01 = 2000
    # (2x equity), risk still $20 (2%). 3 of them: notional 6000 > 5000, risk 6%.
    risk = RiskParams(max_new_orders_per_hour=100)
    cands = [
        _long(symbol="C{}USDT".format(i), price=100.0, stop=99.0, risk_pct=0.02)
        for i in range(4)
    ]
    d = apply_risk_stack(cands, _account(), _neutral_signal(), risk, now=NOW)
    # each notional 2000; cap 5000 -> 2 approved (4000), 3rd would be 6000 > 5000.
    assert len(d.approved) == 2
    assert d.rejected[0].reason == "max_total_notional"
    assert all(s.reason == "max_total_notional" for s in d.rejected)


# --------------------------------------------------------------------------- #
# ordering: earlier checks fire before later ones
# --------------------------------------------------------------------------- #
def test_regime_gate_precedes_invalid_price():
    """A blocked long with an invalid price reports the gate (check 1), not 2."""
    risk = RiskParams()
    sig = MarketSignal(regime=MarketRegime.RISK_OFF, btc_trend=BtcTrend.FLAT)
    d = apply_risk_stack([_long(price=0.0, stop=0.0)], _account(), sig, risk, now=NOW)
    assert _reasons(d) == ["regime_gate_blocked"]


def test_invalid_price_precedes_stop_distance():
    risk = RiskParams()
    # zero price -> invalid (check 2) before any stop-distance evaluation
    d = apply_risk_stack(
        [_long(price=0.0, stop=-1.0)], _account(), _neutral_signal(), risk, now=NOW
    )
    assert _reasons(d) == ["invalid_price"]


def test_conflict_precedes_existing_position_checks():
    """Within-batch conflict (4) is reported before existing-position (5/6)."""
    risk = RiskParams()
    acct = _account(positions=[_position(side=Side.LONG, base="BNB")])
    d = apply_risk_stack(
        [_long(symbol="BNBUSDT"), _short(symbol="BNBUSDT")],
        acct,
        _neutral_signal(),
        risk,
        now=NOW,
    )
    assert _reasons(d) == [
        "same_symbol_opposite_side_conflict",
        "same_symbol_opposite_side_conflict",
    ]


def test_circuit_breaker_precedes_portfolio_caps():
    """Check 7 fires before the per-hour cap (8a)."""
    risk = RiskParams()
    acct = _account(
        open_orders_this_hour=3,  # would also trip 8a
        stop_loss_events=[
            StopLossEvent(base_asset="BNB", side=Side.LONG,
                          at=(NOW - timedelta(hours=1)).isoformat()),
            StopLossEvent(base_asset="BNB", side=Side.LONG,
                          at=(NOW - timedelta(hours=2)).isoformat()),
        ],
    )
    d = apply_risk_stack([_long(symbol="BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _reasons(d) == ["circuit_breaker_24h"]


# --------------------------------------------------------------------------- #
# risk_state / notes surface + robustness
# --------------------------------------------------------------------------- #
def test_risk_state_snapshot_populated():
    risk = RiskParams()
    d = apply_risk_stack([_long()], _account(), _neutral_signal(), risk, now=NOW)
    rs = d.risk_state
    assert rs["equity_usd"] == 1000.0
    assert rs["approved_count"] == 1
    assert rs["max_new_orders_per_hour"] == 3
    assert rs["max_total_initial_risk_pct"] == 0.12
    assert rs["max_total_notional_usd"] == 5000.0
    assert rs["now"] == NOW.isoformat()


def test_default_now_when_omitted_does_not_raise():
    """Omitting now uses datetime.now(utc); must not raise, returns a decision."""
    risk = RiskParams()
    d = apply_risk_stack([_long()], _account(), _neutral_signal(), risk)
    assert _approved_symbols(d) == ["BNBUSDT"]
    assert isinstance(d.risk_state["now"], str)


def test_naive_now_is_accepted():
    """A naive datetime is treated as UTC, not a crash."""
    risk = RiskParams()
    naive = datetime(2026, 6, 15, 12, 0, 0)
    d = apply_risk_stack([_long()], _account(), _neutral_signal(), risk, now=naive)
    assert _approved_symbols(d) == ["BNBUSDT"]


def test_none_account_does_not_raise():
    risk = RiskParams()
    # equity 0 -> sizing yields 0 notional/risk; still returns a clean decision.
    d = apply_risk_stack([_long()], None, _neutral_signal(), risk, now=NOW)
    assert isinstance(d.rejected, list)
    assert isinstance(d.approved, list)


def test_every_rejection_is_stage_risk():
    """No matter the rule, every SkippedOrder is stage='risk'."""
    risk = RiskParams()
    sig = MarketSignal(regime=MarketRegime.RISK_OFF, btc_trend=BtcTrend.DOWN,
                       risk_flags=["market_halt"])
    acct = _account(
        open_orders_this_hour=3,
        positions=[_position(symbol="ETHUSDT", base="ETH", side=Side.SHORT)],
        stop_loss_events=[
            StopLossEvent(base_asset="SOL", side=Side.LONG,
                          at=(NOW - timedelta(hours=1)).isoformat()),
            StopLossEvent(base_asset="SOL", side=Side.LONG,
                          at=(NOW - timedelta(hours=2)).isoformat()),
        ],
    )
    cands = [
        _long(symbol="BNBUSDT", price=0.0, stop=0.0),          # invalid / gated
        _long(symbol="ADAUSDT", price=100.0, stop=50.0),       # stop too wide / gated
        _long(symbol="ETHUSDT"),                                # opposite position
        _long(symbol="SOLUSDT"),                                # circuit breaker
        _short(symbol="XRPUSDT"),                               # short, halt-blocked
    ]
    d = apply_risk_stack(cands, acct, sig, risk, now=NOW)
    assert d.approved == []
    assert len(d.rejected) == 5
    assert all(isinstance(s, SkippedOrder) and s.stage == "risk" for s in d.rejected)


# --------------------------------------------------------------------------- #
# check 1b — 汲水 V0.2 time-of-day entry filter (Beijing 08:00–15:59 no opens)
# --------------------------------------------------------------------------- #
# UTC 04:00 == Beijing 12:00 (inside the V0.2 excluded window 08..15).
_BJ_INSIDE = datetime(2026, 6, 15, 4, 0, 0, tzinfo=timezone.utc)
# UTC 12:00 == Beijing 20:00 (outside the excluded window).
_BJ_OUTSIDE = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

_V02_HOURS = tuple(range(8, 16))


def test_v02_time_filter_blocks_all_opens_inside_window():
    risk = RiskParams(excluded_entry_beijing_hours=_V02_HOURS)
    cands = [_long(symbol="BNBUSDT"), _long(symbol="ETHUSDT")]
    d = apply_risk_stack(cands, _account(), _neutral_signal(), risk, now=_BJ_INSIDE)
    assert d.approved == []
    assert _reasons(d) == ["excluded_entry_beijing_hour", "excluded_entry_beijing_hour"]
    assert d.risk_state["entry_hour_blocked"] is True
    assert d.risk_state["beijing_hour"] == 12


def test_v02_time_filter_allows_opens_outside_window():
    risk = RiskParams(excluded_entry_beijing_hours=_V02_HOURS)
    d = apply_risk_stack([_long()], _account(), _neutral_signal(), risk, now=_BJ_OUTSIDE)
    assert _approved_symbols(d) == ["BNBUSDT"]
    assert d.risk_state["entry_hour_blocked"] is False
    assert d.risk_state["beijing_hour"] == 20


def test_v3_default_has_no_time_filter():
    # Default RiskParams() (V3.0) leaves the window empty -> never blocks on time,
    # even at a Beijing hour that V0.2 would exclude.
    risk = RiskParams()
    assert risk.excluded_entry_beijing_hours == ()
    d = apply_risk_stack([_long()], _account(), _neutral_signal(), risk, now=_BJ_INSIDE)
    assert _approved_symbols(d) == ["BNBUSDT"]
    assert d.risk_state["entry_hour_blocked"] is False


# --------------------------------------------------------------------------- #
# check 1c — account-level kill-switch (drawdown / daily loss)
# --------------------------------------------------------------------------- #
def test_drawdown_kill_switch_halts_all_opens():
    risk = RiskParams(max_account_drawdown_pct=0.25)
    # equity 700 <= peak 1000 * (1 - 0.25) = 750 -> halt
    acct = AccountState(equity_usd=700.0, peak_equity_usd=1000.0)
    d = apply_risk_stack([_long("BNBUSDT"), _long("ETHUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert d.approved == []
    assert set(_reasons(d)) == {"account_drawdown_halt"}
    assert d.risk_state["account_halt_reason"] == "account_drawdown_halt"


def test_drawdown_kill_switch_allows_above_threshold():
    risk = RiskParams(max_account_drawdown_pct=0.25)
    acct = AccountState(equity_usd=760.0, peak_equity_usd=1000.0)   # -24% > -25%
    d = apply_risk_stack([_long("BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == ["BNBUSDT"]
    assert d.risk_state["account_halt_reason"] == ""


def test_daily_loss_kill_switch_halts():
    risk = RiskParams(max_daily_loss_pct=0.10)
    # equity 880 <= day_start 1000 * 0.90 = 900 -> halt
    acct = AccountState(equity_usd=880.0, peak_equity_usd=1000.0, day_start_equity_usd=1000.0)
    d = apply_risk_stack([_long("BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == []
    assert _reasons(d) == ["daily_loss_halt"]


def test_v3_default_has_no_kill_switch():
    risk = RiskParams()   # both thresholds 0
    acct = AccountState(equity_usd=10.0, peak_equity_usd=1000.0, day_start_equity_usd=1000.0)
    d = apply_risk_stack([_long("BNBUSDT")], acct, _neutral_signal(), risk, now=NOW)
    assert _approved_symbols(d) == ["BNBUSDT"]            # no halt despite -99%
    assert d.risk_state["account_halt_reason"] == ""
