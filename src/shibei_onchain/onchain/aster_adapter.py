"""Aster execution venue — routes BOTH legs of the long/short strategy to Aster
perps, and reconciles the Aster futures account.

This is the perp counterpart of the PancakeSwap spot venue. It implements the
same venue interface the orchestrator uses, so ``SHIBEI_ONCHAIN_VENUE=aster``
swaps the whole execution layer over without the brain knowing:

    filter_candidates → read_account → detect_exits → execute_orders

Mapping a venue-agnostic ``PlannedOrder`` to Aster:

    OPEN  LONG  → BUY  MARKET (positionSide LONG)  + reduce-only STOP_MARKET + TAKE_PROFIT_MARKET
    OPEN  SHORT → SELL MARKET (positionSide SHORT) + reduce-only STOP_MARKET + TAKE_PROFIT_MARKET
    CLOSE       → reduce-only MARKET in the opposite direction

Stops and take-profits are placed as **resting reduce-only orders on Aster** at
open time, so exits are enforced on-exchange even if the agent is offline — more
robust than off-chain price polling. ``detect_exits`` therefore returns nothing
for this venue (the exchange owns the exits); the brain's 2.5R / stop / max-hold
intent is expressed as those resting orders.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from shibei_onchain.config import AgentConfig
from shibei_onchain.models import (
    AccountState,
    Candidate,
    ExecutionReceipt,
    OrderAction,
    PlannedOrder,
    Position,
    Side,
    SkippedOrder,
    base_asset_of,
)
from shibei_onchain.onchain.aster_perp import AsterPerpClient, _MOCK_MARKS
from shibei_onchain.brain.position_manager import plan_position_management


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


# A conservative default tradeable set for the perp venue (majors Aster lists).
# In live (api) mode this is intersected with the exchange's symbol list.
_DEFAULT_ASTER_SYMBOLS = set(_MOCK_MARKS.keys())


class AsterExecutionAdapter:
    """Long+short execution venue backed by Aster perpetual futures."""

    def __init__(self, config: AgentConfig, client: Optional[AsterPerpClient] = None) -> None:
        self.config = config
        self.client = client or AsterPerpClient(config.aster)
        # per-symbol LOT_SIZE precision (qty decimals / step / min), from exchangeInfo
        self._filters: Dict[str, Dict[str, Any]] = {}
        # In api mode use the EXCHANGE's real symbol list (so we never plan a
        # symbol Aster won't accept); fall back to the built-in majors only if the
        # exchangeInfo fetch fails. Mock mode uses the built-in majors.
        if self.client.mode == "api":
            listed = self._fetch_listed_symbols()
            self._symbols = listed if listed else set(_DEFAULT_ASTER_SYMBOLS)
        else:
            self._symbols = set(_DEFAULT_ASTER_SYMBOLS)

    @property
    def name(self) -> str:
        return "aster"

    @property
    def ready(self) -> bool:
        return bool(self.config.aster.base_url)

    def health(self) -> Dict[str, Any]:
        h = self.client.health()
        h["listed_symbols"] = sorted(self._symbols)
        return h

    def _fetch_listed_symbols(self) -> set:
        ok, data, _ = self.client._public_get("/fapi/v3/exchangeInfo", {})
        listed = set()
        if ok and isinstance(data, dict):
            for sym in data.get("symbols") or []:
                name = str(sym.get("symbol") or "").upper()
                if not name:
                    continue
                if str(sym.get("status") or "TRADING").upper() != "TRADING":
                    continue  # skip PRE_SETTLE / SETTLING / DELIVERING / etc.
                listed.add(name)
                step = 0.0
                min_qty = 0.0
                tick = 0.0
                min_notional = 0.0
                for f in sym.get("filters") or []:
                    ft = f.get("filterType")
                    if ft == "LOT_SIZE":
                        step = _to_float(f.get("stepSize"))
                        min_qty = _to_float(f.get("minQty"))
                    elif ft == "PRICE_FILTER":
                        tick = _to_float(f.get("tickSize"))
                    elif ft == "MIN_NOTIONAL":
                        min_notional = _to_float(f.get("notional") or f.get("minNotional"))
                qp = sym.get("quantityPrecision")
                pp = sym.get("pricePrecision")
                self._filters[name] = {
                    "qty_precision": int(qp) if qp is not None else 3,
                    "price_precision": int(pp) if pp is not None else 2,
                    "step": step,
                    "min_qty": min_qty,
                    "tick_size": tick,
                    "min_notional": min_notional,
                }
        return listed

    def _round_qty(self, symbol: str, qty: float) -> float:
        """Floor a quantity to the symbol's allowed precision (Aster LOT_SIZE).
        Returns 0.0 when the rounded size is below minQty (caught as a visible
        zero-quantity failure rather than an exchange rejection)."""
        filt = self._filters.get(symbol.upper())
        if not filt:
            return qty
        prec = int(filt.get("qty_precision", 3))
        factor = 10 ** prec
        rounded = math.floor(qty * factor) / factor
        min_qty = filt.get("min_qty") or 0.0
        if min_qty and rounded < min_qty:
            return 0.0
        return rounded

    def _ensure_min_notional(self, symbol: str, qty: float, mark: float) -> float:
        """Bump qty UP to the smallest LOT_SIZE step whose notional clears the
        exchange MIN_NOTIONAL. Floored sizing on a high-priced coin (e.g. ETH)
        can land just under the minimum; this lifts it back over."""
        filt = self._filters.get(symbol.upper())
        if not filt or mark <= 0:
            return qty
        min_notional = filt.get("min_notional") or 0.0
        if min_notional <= 0 or qty * mark >= min_notional:
            return qty
        step = filt.get("step") or 0.0
        if step <= 0:
            return qty
        steps = math.ceil((min_notional / mark) / step)
        return round(steps * step, 12)

    def _round_price(self, symbol: str, price: float) -> float:
        """Round a price to the symbol's tick size (Aster PRICE_FILTER)."""
        filt = self._filters.get(symbol.upper())
        if not filt or price <= 0:
            return price
        prec = int(filt.get("price_precision", 2))
        tick = filt.get("tick_size") or 0.0
        if tick > 0:
            price = round(price / tick) * tick
        return round(price, prec)

    # ------------------------------------------------------------------ #
    # 1. universe filter (perps cover far more symbols than spot DEX)
    # ------------------------------------------------------------------ #
    def filter_candidates(
        self, candidates: List[Candidate], signal: Optional[Any] = None
    ) -> Tuple[List[Candidate], List[SkippedOrder]]:
        kept: List[Candidate] = []
        skipped: List[SkippedOrder] = []
        for cand in candidates:
            if cand.symbol.upper() in self._symbols:
                kept.append(cand)
            else:
                skipped.append(
                    SkippedOrder(
                        symbol=cand.symbol,
                        side=cand.side,
                        reason="not_aster_listed",
                        stage="universe_filter",
                        detail={"venue": "aster"},
                    )
                )
        return kept, skipped

    # ------------------------------------------------------------------ #
    # 2. reconcile the Aster futures account
    # ------------------------------------------------------------------ #
    def read_account(
        self,
        prior: Optional[AccountState] = None,
        ref_prices: Optional[Dict[str, float]] = None,
    ) -> AccountState:
        balance = self.client.get_balance()
        raw_positions = self.client.get_positions()
        positions: List[Position] = []
        unrealized = 0.0
        for row in raw_positions:
            symbol = str(row.get("symbol") or "")
            side = Side.LONG if str(row.get("position_side")) == "LONG" else Side.SHORT
            qty = abs(float(row.get("qty") or 0.0))
            entry = float(row.get("entry") or 0.0)
            mark = float(row.get("mark") or entry)
            if qty <= 0:
                continue
            notional = qty * mark
            if entry > 0:
                unrealized += (mark - entry) * qty if side is Side.LONG else (entry - mark) * qty
            positions.append(
                Position(
                    symbol=symbol,
                    side=side,
                    base_asset=base_asset_of(symbol),
                    quantity=qty,
                    entry_price=entry,
                    notional_usd=notional,
                    current_price=mark,
                    strategy_leg="shibei_onchain_" + ("long" if side is Side.LONG else "short"),
                )
            )
        collateral = float(balance.get("balance") or 0.0)
        available = float(balance.get("available") or 0.0)
        equity = collateral + unrealized
        account = AccountState(
            equity_usd=equity if equity > 0 else collateral,
            quote_balance_usd=available,
            gas_balance=available,           # perps need no per-trade gas; collateral proxy
            positions=positions,
            as_of=_utc_now_iso(),
            source="aster" if self.client.mode == "api" else "aster_mock",
            raw={"balance": balance, "venue": "aster"},
        )
        if prior is not None:
            account.stop_loss_events = list(prior.stop_loss_events)
            account.open_orders_this_hour = prior.open_orders_this_hour
        return account

    # ------------------------------------------------------------------ #
    # 3. active management — +1R breakeven move + gentle max-hold backstop.
    #
    # The initial 2.5R take-profit and stop already rest on-exchange from open
    # time. What's left is the *dynamic* part: pull the stop to breakeven at +1R,
    # and flatten on the max-hold backstop. Positions are enriched by the
    # orchestrator with opened_at / stop_loss_price (from persisted metadata)
    # before this runs.
    # ------------------------------------------------------------------ #
    def detect_exits(self, account: AccountState, risk: Any, *, now: Any = None) -> List[PlannedOrder]:
        return plan_position_management(
            list(account.positions or []),
            now=now,
            breakeven_r=getattr(risk, "long_breakeven_r", 1.0),
            long_max_hold_hours=getattr(risk, "long_max_hold_hours", 0.0),
        )

    # ------------------------------------------------------------------ #
    # 4. execute
    # ------------------------------------------------------------------ #
    def execute_orders(
        self,
        orders: List[PlannedOrder],
        *,
        dry_run: bool,
        ref_prices: Optional[Dict[str, float]] = None,
    ) -> List[ExecutionReceipt]:
        receipts: List[ExecutionReceipt] = []
        for order in orders:
            rp = (ref_prices or {}).get(order.base_asset.upper()) if ref_prices else None
            receipts.append(self.execute_order(order, dry_run=dry_run, ref_price=rp))
        return receipts

    def execute_order(
        self,
        order: PlannedOrder,
        *,
        dry_run: bool,
        ref_price: Optional[float] = None,
    ) -> ExecutionReceipt:
        try:
            return self._execute_inner(order, dry_run=dry_run, ref_price=ref_price)
        except Exception as exc:  # noqa: BLE001 - failure visible, never crash a cycle
            return self._receipt(order, status="failed", dry_run=bool(dry_run),
                                 error="aster_adapter_error:" + type(exc).__name__)

    def _execute_inner(self, order: PlannedOrder, *, dry_run: bool, ref_price: Optional[float]) -> ExecutionReceipt:
        symbol = order.symbol.upper()
        if symbol not in self._symbols:
            return self._receipt(order, status="skipped", dry_run=bool(dry_run), error="not_aster_listed")

        mark = ref_price or order.entry_price or self.client.get_mark_price(symbol)
        leverage = max(1, min(self.config.risk.max_leverage, self.config.aster.default_leverage))
        quantity = order.quantity if order.quantity > 0 else (
            (order.notional_usd / mark) if mark > 0 else 0.0
        )
        quantity = self._round_qty(symbol, quantity)  # honour Aster LOT_SIZE precision
        if quantity <= 0:
            return self._receipt(order, status="failed", dry_run=bool(dry_run), error="zero_quantity",
                                 raw={"notional_usd": order.notional_usd, "mark": mark})

        # ---- MOVE_STOP: relocate the resting stop (e.g. +1R -> breakeven) -- #
        # Aster has no atomic "amend stop", so we cancel the symbol's resting
        # protective orders and re-place: a new stop at the requested price plus
        # the original 2.5R take-profit (recomputed from entry), so the TP is not
        # lost when we cancel-all.
        if order.action is OrderAction.MOVE_STOP:
            new_stop = self._round_price(symbol, order.stop_loss_price or order.entry_price)
            stop_pct = getattr(self.config.risk, "initial_stop_distance_pct", 0.06) or 0.06
            risk_dist = order.entry_price * stop_pct if order.entry_price > 0 else abs(mark - new_stop)
            r = order.take_profit_r_multiple or self.config.risk.take_profit_r
            raw_tp = (order.entry_price + r * risk_dist) if order.side is Side.LONG else (order.entry_price - r * risk_dist)
            tp_price = self._round_price(symbol, raw_tp)
            intended = {"action": "move_stop", "new_stop": new_stop,
                        "take_profit_price": tp_price, "reason": order.reason or "move_to_breakeven"}
            if dry_run:
                return self._receipt(order, status="dry_run", dry_run=True, price=new_stop,
                                     amount_out=quantity, raw=intended)
            cancel_res = self.client.cancel_open_orders(symbol=symbol)
            stop_res = self.client.place_stop(symbol=symbol, position_side=order.side.position_side,
                                              stop_price=new_stop, quantity=quantity) if new_stop > 0 else None
            tp_res = self.client.place_take_profit(symbol=symbol, position_side=order.side.position_side,
                                                   take_profit_price=tp_price, quantity=quantity) if tp_price > 0 else None
            ok = bool(stop_res and stop_res.ok)
            return self._receipt(
                order, status="filled" if ok else "failed", dry_run=False,
                tx_hash=(stop_res.order_id if stop_res else ""), price=new_stop, amount_out=quantity,
                error="" if ok else ("move_stop_failed:" + ((stop_res.error if stop_res else "") or "no_stop")),
                raw={**intended,
                     "cancelled": cancel_res.ok if cancel_res else None,
                     "stop_order": (stop_res.order_id if stop_res and stop_res.ok else None),
                     "take_profit_order": (tp_res.order_id if tp_res and tp_res.ok else None)},
            )

        # ---- CLOSE: reduce-only opposite-direction market ---------------- #
        if order.action is OrderAction.CLOSE:
            if dry_run:
                return self._receipt(order, status="dry_run", dry_run=True, price=mark,
                                     amount_out=quantity, raw={"action": "close", "reduce_only": True})
            res = self.client.close_market(symbol=symbol, position_side=order.side.position_side, quantity=quantity)
            return self._receipt(order, status="filled" if res.ok else "failed", dry_run=False,
                                 tx_hash=res.order_id, price=res.avg_price or mark, amount_out=quantity,
                                 error=res.error, raw={"action": "close", **res.raw})

        # ---- OPEN: leverage + market + resting stop + resting TP --------- #
        quantity = self._ensure_min_notional(symbol, quantity, mark)  # clear MIN_NOTIONAL
        if quantity <= 0:
            return self._receipt(order, status="failed", dry_run=bool(dry_run), error="zero_quantity",
                                 raw={"notional_usd": order.notional_usd, "mark": mark})
        # Recompute stop/TP from the ACTUAL entry (mark) so the % stop distance
        # and R-multiple stay correct even when the fill price differs from the
        # scanner's reference, then round to the symbol's tick size so Aster
        # accepts the price.
        if order.entry_price > 0 and order.stop_loss_price > 0:
            raw_stop = mark * (order.stop_loss_price / order.entry_price)
        else:
            raw_stop = order.stop_loss_price
        stop_price = self._round_price(symbol, raw_stop)
        risk_dist = abs(mark - stop_price)
        r = order.take_profit_r_multiple or self.config.risk.take_profit_r
        raw_tp = (mark + r * risk_dist) if order.side is Side.LONG else (mark - r * risk_dist)
        tp_price = self._round_price(symbol, raw_tp)
        intended = {
            "leverage": leverage,
            "position_side": order.side.position_side,
            "stop_price": stop_price,
            "take_profit_price": tp_price,
            "margin_used_usd": (quantity * mark) / leverage if leverage else None,
        }
        if dry_run:
            return self._receipt(order, status="dry_run", dry_run=True, price=mark, amount_out=quantity,
                                 raw={"action": "open", **intended, "aster_mode": self.client.mode})

        # live: margin affordability (perp analogue of the gas floor) — the
        # min-notional position must fit available collateral at this leverage.
        available = float(self.client.get_balance().get("available") or 0.0)
        required_margin = (quantity * mark) / max(1, leverage)
        if required_margin > available:
            return self._receipt(order, status="skipped", dry_run=False, error="insufficient_margin",
                                 raw={"required_margin_usd": round(required_margin, 4),
                                      "available_usd": round(available, 4),
                                      "notional_usd": round(quantity * mark, 2), "leverage": leverage})

        open_res = self.client.open_market(symbol=symbol, side=order.side, quantity=quantity, leverage=leverage)
        if not open_res.ok:
            return self._receipt(order, status="failed", dry_run=False,
                                 error="open_failed:" + (open_res.error or "unknown"),
                                 tx_hash=open_res.order_id, raw=open_res.raw)
        # resting protective orders (best-effort; recorded in raw)
        stop_res = self.client.place_stop(symbol=symbol, position_side=order.side.position_side,
                                          stop_price=stop_price, quantity=quantity) if stop_price > 0 else None
        tp_res = self.client.place_take_profit(
            symbol=symbol, position_side=order.side.position_side,
            take_profit_price=tp_price, quantity=quantity,
        ) if tp_price > 0 else None
        # surface protective-order failures so a naked position is never silent
        protection = {
            "stop_order": (stop_res.order_id if stop_res and stop_res.ok else None),
            "stop_error": (stop_res.error if stop_res and not stop_res.ok else None),
            "take_profit_order": (tp_res.order_id if tp_res and tp_res.ok else None),
            "take_profit_error": (tp_res.error if tp_res and not tp_res.ok else None),
        }
        unprotected = bool(stop_res and not stop_res.ok)
        return self._receipt(
            order, status="filled", dry_run=False, tx_hash=open_res.order_id,
            price=open_res.avg_price or mark, amount_out=quantity,
            error=("stop_not_placed:" + (stop_res.error or "") if unprotected else ""),
            raw={
                "action": "open", **intended,
                "order_id": open_res.order_id,
                **protection,
            },
        )

    # ------------------------------------------------------------------ #
    def _take_profit_price(self, order: PlannedOrder, mark: float) -> float:
        entry = order.entry_price or mark
        stop = order.stop_loss_price
        if entry <= 0 or stop <= 0:
            return 0.0
        risk_dist = abs(entry - stop)
        r = order.take_profit_r_multiple or self.config.risk.take_profit_r
        return entry + r * risk_dist if order.side is Side.LONG else entry - r * risk_dist

    def _receipt(self, order: PlannedOrder, *, status: str, dry_run: bool, tx_hash: str = "",
                 price: float = 0.0, amount_out: float = 0.0, error: str = "",
                 raw: Optional[Dict[str, Any]] = None) -> ExecutionReceipt:
        explorer = ""
        if tx_hash:
            explorer = self.config.aster.explorer_base.rstrip("/") + "/futures/" + order.symbol.upper()
        return ExecutionReceipt(
            symbol=order.symbol,
            side=order.side,
            action=order.action,
            status=status,
            tx_hash=tx_hash,
            token_in=self.config.aster.margin_asset,
            token_out=order.base_asset,
            amount_in=order.notional_usd,
            amount_out=amount_out,
            price=price,
            error=error,
            dry_run=dry_run,
            as_of=_utc_now_iso(),
            explorer_url=explorer,
            raw={"venue": "aster", **(raw or {})},
        )
