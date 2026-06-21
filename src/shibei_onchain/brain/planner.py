"""Position sizing + order planning (the brain's sizing seam).

This module turns ranked :class:`~shibei_onchain.models.Candidate` objects into
venue-agnostic :class:`~shibei_onchain.models.PlannedOrder` objects, using 拾贝
V3.0's risk-budget sizing formula:

    risk_budget = equity * risk_per_trade_pct           (the dollars at risk)
    sd          = stop_distance_pct, clamped to (0, max_stop_distance_pct]
    notional    = min(risk_budget / sd, equity * max_leverage)
    notional    = max(notional, min_notional)           (exchange floor)
    quantity    = notional / price

Sizing is intentionally pure and dependency-free: :mod:`brain.risk` imports
``compute_sizing`` to derive per-candidate risk budget / notional while applying
the portfolio caps, so the *same* numbers drive both the risk stack and the
emitted orders. Keeping this file stdlib-only (no third-party imports anywhere)
means it can be imported and unit-tested with zero dependencies.
"""

from __future__ import annotations

from typing import Any, Dict, List

from shibei_onchain.config import RiskParams
from shibei_onchain.models import (
    AccountState,
    Candidate,
    OrderAction,
    PlannedOrder,
)


# --------------------------------------------------------------------------- #
# sizing
# --------------------------------------------------------------------------- #
def compute_sizing(
    candidate: Candidate, equity_usd: float, risk: RiskParams
) -> Dict[str, Any]:
    """Compute the risk-budget-derived sizing for a single candidate.

    Returns a dict with keys::

        risk_budget_usd    dollars at risk on this trade (equity * risk_pct)
        notional_usd       position notional in USD (0 if unsizable)
        quantity           token amount (notional / price; 0 if price<=0)
        stop_distance_pct  the *clamped* stop distance actually used
        capped             True if any clamp/cap/floor altered the raw size

    The formula is faithful to 拾贝 V3.0 and matches ``docs/INTERFACES.md``:

    * ``risk_budget = equity_usd * candidate.risk_per_trade_pct``
    * ``sd`` is the candidate's stop distance, clamped to
      ``(0, risk.max_stop_distance_pct]``. A non-positive ``sd`` (e.g. price<=0
      or stop == entry) is unsizable → ``notional 0``, ``capped True``.
    * ``raw = risk_budget / sd``; ``max_notional = equity_usd * max_leverage``;
      ``notional = min(raw, max_notional)``.
    * an in-range but sub-floor notional is lifted to ``min_notional_usdt``.
    * ``quantity = notional / price`` (0 when price <= 0).
    """
    # Negative / non-positive risk fractions or equity make the trade unsizable.
    equity = equity_usd if equity_usd and equity_usd > 0 else 0.0
    risk_pct = candidate.risk_per_trade_pct if candidate.risk_per_trade_pct > 0 else 0.0
    risk_budget = equity * risk_pct

    raw_sd = candidate.stop_distance_pct
    max_sd = risk.max_stop_distance_pct
    capped = False

    # Clamp the stop distance into (0, max_stop_distance_pct].
    if raw_sd > max_sd > 0:
        sd = max_sd
        capped = True
    else:
        sd = raw_sd

    # Unsizable: no stop distance (price<=0, stop==entry) or no risk budget.
    if sd <= 0 or risk_budget <= 0:
        return {
            "risk_budget_usd": round(risk_budget, 10),
            "notional_usd": 0.0,
            "quantity": 0.0,
            "stop_distance_pct": sd if sd > 0 else 0.0,
            "capped": True,
        }

    raw_notional = risk_budget / sd
    max_notional = equity * max(1, risk.max_leverage)

    notional = raw_notional
    if notional > max_notional:
        notional = max_notional
        capped = True

    # Exchange minimum-notional floor (only lift a positive, sub-floor size).
    if 0 < notional < risk.min_notional_usdt:
        notional = risk.min_notional_usdt
        capped = True

    price = candidate.price
    quantity = notional / price if price > 0 else 0.0

    return {
        "risk_budget_usd": round(risk_budget, 10),
        "notional_usd": round(notional, 10),
        "quantity": round(quantity, 12),
        "stop_distance_pct": sd,
        "capped": capped,
    }


# --------------------------------------------------------------------------- #
# order planning
# --------------------------------------------------------------------------- #
def build_planned_orders(
    candidates: List[Candidate], account: AccountState, risk: RiskParams
) -> List[PlannedOrder]:
    """Turn approved candidates into venue-agnostic OPEN orders.

    Each candidate is sized against ``account.equity_usd`` via
    :func:`compute_sizing`, then mapped onto a :class:`PlannedOrder` carrying the
    notional, quantity, risk budget, stop/entry prices and strategy metadata the
    execution adapter needs. ``quote_asset`` defaults to ``"USDT"`` and
    ``max_slippage_bps`` to ``100`` (1.00%), faithful to the on-chain defaults.

    This function does *not* re-apply risk gates — callers (``brain.risk``)
    decide which candidates survive; this only translates survivors into orders.
    """
    equity = account.equity_usd if account is not None else 0.0
    orders: List[PlannedOrder] = []

    for cand in candidates:
        sizing = compute_sizing(cand, equity, risk)
        orders.append(
            PlannedOrder(
                symbol=cand.symbol,
                side=cand.side,
                action=OrderAction.OPEN,
                base_asset=cand.base_asset,
                quote_asset="USDT",
                notional_usd=sizing["notional_usd"],
                quantity=sizing["quantity"],
                entry_price=cand.price,
                stop_loss_price=cand.stop_loss_price,
                take_profit_r_multiple=cand.take_profit_r_multiple,
                breakeven_r_multiple=cand.breakeven_r_multiple,
                risk_budget_usd=sizing["risk_budget_usd"],
                risk_per_trade_pct=cand.risk_per_trade_pct,
                max_slippage_bps=100,
                strategy_id=cand.strategy_id,
                strategy_leg=cand.strategy_leg,
                signal_key=cand.signal_key,
                reason="planned_open",
                metadata={
                    "stop_distance_pct": sizing["stop_distance_pct"],
                    "sizing_capped": sizing["capped"],
                    "score": cand.score,
                    "relative_strength_score": cand.relative_strength_score,
                    "source_boards": list(cand.source_boards),
                },
            )
        )

    return orders
