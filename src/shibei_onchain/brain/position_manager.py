"""brain/position_manager.py — active management of OPEN positions.

拾贝's exit discipline has three moving parts the *opening* logic does not cover:

    1. **+1R → breakeven** — once a position is up one risk-unit, pull the stop to
       entry so the trade can no longer lose. (拾贝 ``long_breakeven_r`` = 1.0R.)
    2. **gentle max-hold backstop** — 汲水 V0.2 itself has *no* time stop; we add a
       loose safety cap so a position cannot sit open forever (set wide enough to
       let winners run to 2.5R; it is a backstop, not the strategy's exit).
    3. resting 2.5R take-profit / initial stop — already placed on-exchange at open.

This module turns (1) and (2) into venue-agnostic :class:`PlannedOrder`s so the
*same* execution path that opens trades also manages them. It is pure: no I/O, no
network, never raises; the caller supplies positions already enriched with
``opened_at`` / ``stop_loss_price`` (from persisted metadata) and ``now``.

    * ``OrderAction.MOVE_STOP`` — relocate the resting stop to ``stop_loss_price``
      (the breakeven move sets it to entry). Adapters re-place the protective
      orders accordingly.
    * ``OrderAction.CLOSE``     — reduce-only market close (max-hold backstop).

Each position yields at most ONE management order per cycle, and CLOSE wins over
MOVE_STOP (no point moving a stop on a position we're about to flatten).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from shibei_onchain.models import (
    OrderAction,
    PlannedOrder,
    Position,
    Side,
    base_asset_of,
)


def _parse_iso(ts: str) -> Optional[datetime]:
    raw = str(ts or "").strip()
    if not raw:
        return None
    if raw[-1] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _age_hours(opened_at: str, now: datetime) -> Optional[float]:
    dt = _parse_iso(opened_at)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 3600.0


def _stop_below_breakeven(pos: Position) -> bool:
    """True iff the resting stop has NOT yet been pulled to breakeven (entry).

    For a long the breakeven move is *upward* (stop < entry still), for a short it
    is *downward* (stop > entry still). A degenerate/zero stop counts as 'not yet
    at breakeven' so the move still fires."""
    entry = pos.entry_price
    stop = pos.stop_loss_price
    if entry <= 0:
        return False
    eps = entry * 1e-6
    if pos.side is Side.LONG:
        return stop < entry - eps
    return stop <= 0 or stop > entry + eps


def plan_position_management(
    positions: List[Position],
    *,
    now: Optional[datetime] = None,
    breakeven_r: float = 1.0,
    long_max_hold_hours: float = 0.0,
) -> List[PlannedOrder]:
    """Return management orders (MOVE_STOP / CLOSE) for the open book.

    Pure & total. ``positions`` should carry ``opened_at`` and the current resting
    ``stop_loss_price`` (enriched from persisted metadata by the orchestrator).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    orders: List[PlannedOrder] = []
    for pos in positions or ():
        if pos.quantity <= 0 or pos.entry_price <= 0:
            continue
        base = pos.base_asset or base_asset_of(pos.symbol)

        # 1) max-hold backstop — CLOSE wins over everything else.
        if long_max_hold_hours and long_max_hold_hours > 0 and pos.opened_at:
            age = _age_hours(pos.opened_at, now)
            if age is not None and age >= long_max_hold_hours:
                orders.append(
                    PlannedOrder(
                        symbol=pos.symbol,
                        side=pos.side,
                        action=OrderAction.CLOSE,
                        base_asset=base,
                        quantity=pos.quantity,
                        entry_price=pos.entry_price,
                        stop_loss_price=pos.stop_loss_price,
                        strategy_leg=pos.strategy_leg,
                        reason="max_hold_backstop",
                        metadata={
                            "age_hours": round(age, 4),
                            "max_hold_hours": long_max_hold_hours,
                            "rr_multiple": round(pos.recompute_rr(), 4),
                        },
                    )
                )
                continue

        # 2) +1R → breakeven — relocate the stop to entry, once.
        rr = pos.recompute_rr()
        if rr >= breakeven_r and _stop_below_breakeven(pos):
            orders.append(
                PlannedOrder(
                    symbol=pos.symbol,
                    side=pos.side,
                    action=OrderAction.MOVE_STOP,
                    base_asset=base,
                    quantity=pos.quantity,
                    entry_price=pos.entry_price,
                    stop_loss_price=pos.entry_price,   # new stop = breakeven
                    strategy_leg=pos.strategy_leg,
                    reason="move_to_breakeven",
                    metadata={
                        "rr_multiple": round(rr, 4),
                        "breakeven_r": breakeven_r,
                        "old_stop": pos.stop_loss_price,
                        "new_stop": pos.entry_price,
                    },
                )
            )

    return orders
