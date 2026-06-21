"""THE risk stack — 拾贝's differentiator, ported and *enforced* on-chain.

拾贝 V3.0 carried a deep, multi-layer risk discipline, but several of its
layers were either *log-only* (the BTC environment gate) or implicit in the
exchange (notional / leverage / order-rate caps). This module makes the whole
stack a single, ordered, side-effect-free decision function that runs *before*
any order is planned or signed. Nothing is dropped silently: every rejection is
a :class:`~shibei_onchain.models.SkippedOrder` with ``stage="risk"`` and a
stable machine-readable ``reason`` code, so an operator reading a cycle log can
see exactly *why* each candidate did or did not survive.

The checks run in a fixed order (see ``docs/INTERFACES.md`` — the build
contract). The first seven are *per-candidate* filters; the eighth applies the
*portfolio* caps while iterating the survivors in rank order, accumulating the
risk/notional that each approval would commit:

    1. regime gate per side ............... codes from ``regime_gate.reasons``
    2. invalid price ...................... ``invalid_price``
    3. stop distance too wide ............. ``stop_distance_too_wide``
    4. within-batch long/short conflict ... ``same_symbol_opposite_side_conflict``
                                            (both legs rejected; long-priority note)
    5. opposite existing position ......... ``opposite_position_exists``
    6. same-side existing position ........ ``position_already_open``
    7. same-coin 24h circuit breaker ...... ``circuit_breaker_24h``
    8. portfolio caps (rank order):
         - ≤3 new orders / hour ........... ``max_orders_per_hour``
         - ≤12% total initial risk ........ ``max_total_initial_risk``
         - ≤5x total notional ............. ``max_total_notional``

Sizing for the step-8 caps reuses :func:`brain.planner.compute_sizing`, so the
*same* risk-budget / notional numbers drive both the cap arithmetic here and the
orders emitted downstream. Pure & total: no I/O, no network, never raises;
imports only stdlib + frozen model/config types and two sibling pure functions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from shibei_onchain.config import RiskParams
from shibei_onchain.models import (
    AccountState,
    Candidate,
    MarketSignal,
    RiskDecision,
    Side,
    SkippedOrder,
    base_asset_of,
)
from shibei_onchain.signals.regime_gate import evaluate_regime_gate
from shibei_onchain.brain.planner import compute_sizing


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
def _parse_iso(ts: str) -> Optional[datetime]:
    """Best-effort parse of an ISO-8601 UTC timestamp.

    Tolerates a trailing ``Z`` (Python 3.9's ``fromisoformat`` does not). Returns
    a timezone-aware UTC ``datetime`` or ``None`` when unparseable — an
    unparseable timestamp must never crash the cycle.
    """
    if not ts:
        return None
    raw = str(ts).strip()
    if not raw:
        return None
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _hours_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600.0


def _beijing_hour(now: datetime) -> int:
    """Hour-of-day (0–23) in Beijing time (UTC+8) for a UTC-aware ``now``."""
    return (now.astimezone(timezone.utc) + timedelta(hours=8)).hour


# --------------------------------------------------------------------------- #
# portfolio-level baselines (existing exposure already on the book)
# --------------------------------------------------------------------------- #
def _position_initial_risk(account: AccountState) -> float:
    """Σ over open positions of ``notional * |entry-stop| / entry`` (USD).

    This is the dollars-at-risk already committed by the current book, the
    baseline the new orders' risk budgets are added on top of for the ≤12% total
    initial-risk cap. A position with no stop or a degenerate entry contributes
    zero (it cannot be sized into a risk number).
    """
    total = 0.0
    for pos in account.positions or ():
        entry = pos.entry_price
        notional = max(0.0, pos.notional_usd)
        if entry <= 0 or notional <= 0 or pos.stop_loss_price <= 0:
            continue
        total += notional * abs(entry - pos.stop_loss_price) / entry
    return total


# --------------------------------------------------------------------------- #
# the risk stack
# --------------------------------------------------------------------------- #
def apply_risk_stack(
    candidates: List[Candidate],
    account: AccountState,
    signal: MarketSignal,
    risk: RiskParams,
    *,
    now: Optional[datetime] = None,
) -> RiskDecision:
    """Run the full ordered risk stack over ``candidates``.

    Parameters
    ----------
    candidates:
        Ranked candidates (best first). Rank order matters: the step-8 portfolio
        caps approve survivors greedily in the given order, so the
        highest-ranked candidates win the limited order/risk/notional budget.
    account:
        Live :class:`AccountState` — positions, open-orders-this-hour, realized
        stop-loss events. Used by checks 5–8.
    signal:
        Normalized :class:`MarketSignal` driving the per-side regime gate
        (check 1) via :func:`evaluate_regime_gate`.
    risk:
        :class:`RiskParams` thresholds.
    now:
        Evaluation time (UTC). Defaults to ``datetime.now(timezone.utc)``. Used
        only by the circuit-breaker window (check 7).

    Returns
    -------
    RiskDecision
        ``approved`` (surviving candidates, original rank order), ``rejected``
        (one :class:`SkippedOrder` per rejected candidate, ``stage="risk"``),
        ``notes`` (human-readable), and ``risk_state`` (a machine-readable
        snapshot of the cap arithmetic for dashboards / debugging).

    Never raises.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    candidates = list(candidates or [])
    account = account if account is not None else AccountState()

    rejected: List[SkippedOrder] = []
    notes: List[str] = []

    def _reject(cand: Candidate, reason: str, **detail: Any) -> None:
        rejected.append(
            SkippedOrder(
                symbol=cand.symbol,
                side=cand.side,
                reason=reason,
                stage="risk",
                detail=detail,
            )
        )

    # ----------------------------------------------------------------------- #
    # check 1 — regime gate (per side). Pure decision over the BTC environment.
    # ----------------------------------------------------------------------- #
    gate = evaluate_regime_gate(signal, risk)
    long_blocked = bool(gate.get("long_blocked"))
    short_blocked = bool(gate.get("short_blocked"))
    gate_reasons = list(gate.get("reasons") or [])
    for note in gate.get("notes") or []:
        notes.append("regime_gate: {}".format(note))

    # ----------------------------------------------------------------------- #
    # check 1b — 汲水 V0.2 time-of-day filter (no NEW opens during the excluded
    # Beijing-time hours). Portfolio-wide: computed once, applied to every
    # candidate. V3.0 leaves ``excluded_entry_beijing_hours`` empty (no-op).
    # ----------------------------------------------------------------------- #
    excluded_hours = tuple(getattr(risk, "excluded_entry_beijing_hours", ()) or ())
    bj_hour = _beijing_hour(now)
    entry_hour_blocked = bj_hour in excluded_hours
    if entry_hour_blocked:
        notes.append(
            "entry_time_filter: Beijing hour {} in excluded set {} -> no new opens "
            "this cycle".format(bj_hour, list(excluded_hours))
        )

    # ----------------------------------------------------------------------- #
    # check 1c — account-level kill-switch (total drawdown / daily loss).
    #
    # The P0 safety gap 拾贝 V3.0 lacked: when the live account bleeds past a
    # threshold, FREEZE all new opens (existing positions keep being managed, so
    # they can still exit). Portfolio-wide; computed once. Off (thresholds 0) for
    # V3.0; defaulted ON for the negative-expectancy V0.2 leg.
    # ----------------------------------------------------------------------- #
    eq = account.equity_usd if account.equity_usd and account.equity_usd > 0 else 0.0
    peak = max(eq, getattr(account, "peak_equity_usd", 0.0) or 0.0)
    day_start = getattr(account, "day_start_equity_usd", 0.0) or 0.0
    dd_pct = float(getattr(risk, "max_account_drawdown_pct", 0.0) or 0.0)
    daily_pct = float(getattr(risk, "max_daily_loss_pct", 0.0) or 0.0)
    account_halt_reason = ""
    drawdown_frac = (1.0 - eq / peak) if (eq > 0 and peak > 0) else 0.0
    daily_loss_frac = (1.0 - eq / day_start) if (eq > 0 and day_start > 0) else 0.0
    if dd_pct > 0 and eq > 0 and peak > 0 and eq <= peak * (1.0 - dd_pct):
        account_halt_reason = "account_drawdown_halt"
    elif daily_pct > 0 and eq > 0 and day_start > 0 and eq <= day_start * (1.0 - daily_pct):
        account_halt_reason = "daily_loss_halt"
    if account_halt_reason:
        notes.append(
            "KILL-SWITCH {}: equity {:.2f} (peak {:.2f}, day_start {:.2f}) -> all new "
            "opens frozen; existing positions still managed".format(
                account_halt_reason, eq, peak, day_start
            )
        )

    # ----------------------------------------------------------------------- #
    # check 4 (pre-pass) — within-batch opposite-side conflict.
    #
    # Detect base assets that appear as BOTH a LONG and a SHORT candidate in the
    # *same* batch. 拾贝's rule is to reject BOTH legs (you cannot be net-long and
    # net-short the same coin in one cycle) — long-priority is only a *tie-break*
    # ordering convention, not a "keep the long" rule. We pre-compute the
    # conflicting base set so the main pass can reject either leg as it sees it.
    # ----------------------------------------------------------------------- #
    longs_by_base: Dict[str, int] = {}
    shorts_by_base: Dict[str, int] = {}
    for cand in candidates:
        base = base_asset_of(cand.symbol).upper()
        if cand.side is Side.LONG:
            longs_by_base[base] = longs_by_base.get(base, 0) + 1
        elif cand.side is Side.SHORT:
            shorts_by_base[base] = shorts_by_base.get(base, 0) + 1
    conflict_bases = {
        base for base in longs_by_base if base in shorts_by_base
    }
    if conflict_bases:
        notes.append(
            "within-batch conflict on {}: both legs rejected (long-priority is a "
            "tie-break only, not a keep-the-long rule)".format(sorted(conflict_bases))
        )

    # ----------------------------------------------------------------------- #
    # checks 2–7 — per-candidate filters. Survivors carry forward to step 8.
    # ----------------------------------------------------------------------- #
    survivors: List[Candidate] = []
    cb_count = max(1, risk.circuit_breaker_stoploss_count)
    cb_window = risk.circuit_breaker_window_hours

    for cand in candidates:
        base = base_asset_of(cand.symbol).upper()

        # check 1 (applied per candidate using the side-specific gate result)
        if cand.side is Side.LONG and long_blocked:
            _reject(cand, "regime_gate_blocked", side="long", gate_reasons=gate_reasons)
            continue
        if cand.side is Side.SHORT and short_blocked:
            _reject(cand, "regime_gate_blocked", side="short", gate_reasons=gate_reasons)
            continue

        # check 1b — V0.2 time-of-day filter (blocks all new opens this cycle)
        if entry_hour_blocked:
            _reject(
                cand,
                "excluded_entry_beijing_hour",
                beijing_hour=bj_hour,
                excluded_hours=list(excluded_hours),
            )
            continue

        # check 1c — account-level kill-switch (blocks all new opens)
        if account_halt_reason:
            _reject(
                cand,
                account_halt_reason,
                equity_usd=round(eq, 4),
                peak_equity_usd=round(peak, 4),
                day_start_equity_usd=round(day_start, 4),
                drawdown_frac=round(drawdown_frac, 6),
                daily_loss_frac=round(daily_loss_frac, 6),
            )
            continue

        # check 2 — invalid price (non-positive entry or stop)
        if cand.price <= 0 or cand.stop_loss_price <= 0:
            _reject(
                cand,
                "invalid_price",
                price=cand.price,
                stop_loss_price=cand.stop_loss_price,
            )
            continue

        # check 3 — stop distance too wide
        sd = cand.stop_distance_pct
        if sd > risk.max_stop_distance_pct:
            _reject(
                cand,
                "stop_distance_too_wide",
                stop_distance_pct=sd,
                max_stop_distance_pct=risk.max_stop_distance_pct,
            )
            continue

        # check 4 — within-batch opposite-side conflict (both legs rejected)
        if base in conflict_bases:
            _reject(
                cand,
                "same_symbol_opposite_side_conflict",
                base_asset=base,
            )
            continue

        # check 5 — opposite existing position
        pos = account.position_for(base)
        if pos is not None and pos.side is not cand.side:
            _reject(
                cand,
                "opposite_position_exists",
                base_asset=base,
                existing_side=pos.side.value,
            )
            continue

        # check 6 — same-side existing position (no stacking)
        if pos is not None and pos.side is cand.side:
            _reject(
                cand,
                "position_already_open",
                base_asset=base,
                existing_side=pos.side.value,
            )
            continue

        # check 7 — same-coin 24h circuit breaker
        recent_stops = 0
        for ev in account.stop_loss_events or ():
            if (ev.base_asset or "").upper() != base:
                continue
            ev_time = _parse_iso(ev.at)
            if ev_time is None:
                # An undated stop event is counted conservatively (visible risk).
                recent_stops += 1
                continue
            if cb_window <= 0 or _hours_between(now, ev_time) <= cb_window:
                recent_stops += 1
        if recent_stops >= cb_count:
            _reject(
                cand,
                "circuit_breaker_24h",
                base_asset=base,
                recent_stoploss_count=recent_stops,
                window_hours=cb_window,
                threshold=cb_count,
            )
            continue

        survivors.append(cand)

    # ----------------------------------------------------------------------- #
    # check 8 — portfolio caps, iterating survivors in rank order.
    #
    # Baselines come from the existing book; each approval *commits* its sizing
    # (risk budget + notional + one order slot) into the running totals, so a
    # later (lower-ranked) survivor is checked against everything already
    # approved this cycle. Sizing reuses brain.planner.compute_sizing.
    # ----------------------------------------------------------------------- #
    equity = account.equity_usd if account.equity_usd and account.equity_usd > 0 else 0.0
    existing_initial_risk = _position_initial_risk(account)
    existing_notional = account.total_position_notional
    open_orders_this_hour = max(0, account.open_orders_this_hour)

    approved: List[Candidate] = []
    approved_count = 0
    approved_risk_budget = 0.0
    approved_notional = 0.0

    max_orders = risk.max_new_orders_per_hour
    max_total_risk_pct = risk.max_total_initial_risk_pct
    max_notional_total = equity * risk.max_total_notional_multiple

    for cand in survivors:
        sizing = compute_sizing(cand, equity, risk)
        this_risk_budget = sizing["risk_budget_usd"]
        this_notional = sizing["notional_usd"]

        # cap a — orders per hour
        if open_orders_this_hour + approved_count >= max_orders:
            _reject(
                cand,
                "max_orders_per_hour",
                open_orders_this_hour=open_orders_this_hour,
                approved_so_far=approved_count,
                max_new_orders_per_hour=max_orders,
            )
            continue

        # cap b — total initial portfolio risk (fraction of equity)
        projected_risk = existing_initial_risk + approved_risk_budget + this_risk_budget
        risk_fraction = (projected_risk / equity) if equity > 0 else float("inf")
        if risk_fraction > max_total_risk_pct:
            _reject(
                cand,
                "max_total_initial_risk",
                projected_initial_risk_usd=round(projected_risk, 10),
                equity_usd=equity,
                projected_risk_fraction=(
                    round(risk_fraction, 10) if equity > 0 else None
                ),
                max_total_initial_risk_pct=max_total_risk_pct,
            )
            continue

        # cap c — total open notional (multiple of equity)
        projected_notional = existing_notional + approved_notional + this_notional
        if projected_notional > max_notional_total:
            _reject(
                cand,
                "max_total_notional",
                projected_notional_usd=round(projected_notional, 10),
                max_total_notional_usd=round(max_notional_total, 10),
                max_total_notional_multiple=risk.max_total_notional_multiple,
            )
            continue

        # commit this approval into the running totals
        approved.append(cand)
        approved_count += 1
        approved_risk_budget += this_risk_budget
        approved_notional += this_notional

    notes.append(
        "risk stack: {} candidate(s) -> {} approved, {} rejected".format(
            len(candidates), len(approved), len(rejected)
        )
    )

    risk_state: Dict[str, Any] = {
        "now": now.isoformat(),
        "equity_usd": equity,
        "long_blocked": long_blocked,
        "short_blocked": short_blocked,
        "beijing_hour": bj_hour,
        "entry_hour_blocked": entry_hour_blocked,
        "excluded_entry_beijing_hours": list(excluded_hours),
        "account_halt_reason": account_halt_reason,
        "peak_equity_usd": round(peak, 6),
        "day_start_equity_usd": round(day_start, 6),
        "drawdown_frac": round(drawdown_frac, 6),
        "daily_loss_frac": round(daily_loss_frac, 6),
        "max_account_drawdown_pct": dd_pct,
        "max_daily_loss_pct": daily_pct,
        "gate_reasons": gate_reasons,
        "conflict_bases": sorted(conflict_bases),
        "open_orders_this_hour": open_orders_this_hour,
        "max_new_orders_per_hour": max_orders,
        "approved_count": approved_count,
        "existing_initial_risk_usd": round(existing_initial_risk, 10),
        "approved_risk_budget_usd": round(approved_risk_budget, 10),
        "total_initial_risk_usd": round(
            existing_initial_risk + approved_risk_budget, 10
        ),
        "total_initial_risk_fraction": (
            round((existing_initial_risk + approved_risk_budget) / equity, 10)
            if equity > 0
            else None
        ),
        "max_total_initial_risk_pct": max_total_risk_pct,
        "existing_notional_usd": round(existing_notional, 10),
        "approved_notional_usd": round(approved_notional, 10),
        "total_notional_usd": round(existing_notional + approved_notional, 10),
        "max_total_notional_usd": round(max_notional_total, 10),
        "max_total_notional_multiple": risk.max_total_notional_multiple,
        "rejected_reasons": [s.reason for s in rejected],
    }

    return RiskDecision(
        approved=approved,
        rejected=rejected,
        notes=notes,
        risk_state=risk_state,
    )
