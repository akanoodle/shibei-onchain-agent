"""On-chain execution adapter — the final seam between plan and chain.

This is where a venue-agnostic :class:`PlannedOrder` from the 拾贝 brain becomes
an actual (or simulated) PancakeSwap swap on BNB Chain, executed through the
self-custody Trust Wallet Agent Kit client. The adapter does **not** make trading
decisions — the brain and the risk stack already did that. Its job is the
mechanical, *fail-visible* translation:

    plan -> resolve token -> short-leg gate -> gas floor -> token_in/out ->
    quote -> final guard -> (dry_run | live swap) -> ExecutionReceipt

Design constraints faithful to the project's invariants:

    * **Every call returns an** :class:`ExecutionReceipt` — never raises. A
      missing token, a disabled short leg, an empty gas tank, a tripped guard,
      or a failed swap each produce a receipt with a machine-readable
      ``status`` / ``error`` so the cycle records *why* nothing filled, never a
      silent drop or a crashed loop.
    * **No third-party import at module top level.** Everything web3-touching is
      behind the injected ``twak`` / ``router`` collaborators, which already
      lazy-import their own deps. This module is pure stdlib at import time.
    * **MVP is long-spot.** A SHORT order is a no-op here unless
      ``config.enable_short_leg`` is set — on-chain perps are a documented
      stretch goal (a perp venue / synthetic-short adapter would slot in behind
      the same ``execute_order`` seam). Until then a SHORT degrades to a visible
      ``skipped`` receipt rather than being mis-executed as a spot sell.
    * **Secrets never logged.** The adapter never touches the private key; the
      injected ``twak`` client holds and signs with it in-process.

``dry_run`` here is the *per-call* simulation switch (the orchestrator passes
``config.dry_run`` or a tighter ``not config.live_orders_allowed`` decision).
When true, the adapter computes the full quote + slippage-protected
``minAmountOut`` and the intended path, but performs **no signing and no
broadcast** — the receipt is stamped ``status="dry_run"`` so a demo / hackathon
run produces realistic, inspectable receipts with zero on-chain risk.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shibei_onchain.config import AgentConfig
from shibei_onchain.models import (
    ExecutionReceipt,
    OrderAction,
    PlannedOrder,
    Side,
    TokenInfo,
)
from shibei_onchain.onchain.pancake_router import PancakeRouter
from shibei_onchain.onchain.twak_client import TwakClient
from shibei_onchain.onchain.universe_filter import UniverseFilter


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OnChainExecutionAdapter:
    """Turn a :class:`PlannedOrder` into an :class:`ExecutionReceipt`.

    Collaborators are injected (constructor wiring done by the orchestrator):

        * ``twak``     — :class:`TwakClient` self-custody execution surface
                         (balances, approve, swap). Holds/derives the key.
        * ``router``   — :class:`PancakeRouter` quote + path + slippage floor.
        * ``universe`` — :class:`UniverseFilter` token registry + final guard.

    The adapter is stateless beyond these handles; ``execute_orders`` is a thin
    fan-out over ``execute_order``.
    """

    def __init__(
        self,
        config: AgentConfig,
        twak: TwakClient,
        router: PancakeRouter,
        universe: UniverseFilter,
    ) -> None:
        self.config = config
        self.onchain = config.onchain
        self.twak = twak
        self.router = router
        self.universe = universe

    # ------------------------------------------------------------------ #
    # readiness
    # ------------------------------------------------------------------ #
    @property
    def ready(self) -> bool:
        """The adapter is *wireable* when the universe resolved the quote asset
        and a router address is present.

        This is intentionally a *mechanical* readiness check (registry + router
        plumbing), distinct from :pyattr:`AgentConfig.live_orders_allowed` which
        is the *policy* gate that decides whether a real swap may be broadcast.
        Both must hold before live execution; ``ready`` alone never authorises a
        broadcast.
        """
        usdt = self.universe.usdt()
        has_quote_asset = bool(usdt and usdt.address)
        has_router = bool(self.universe.router_address() or self.router.router_addr())
        return has_quote_asset and has_router

    # ------------------------------------------------------------------ #
    # single-order execution (the 7-step flow)
    # ------------------------------------------------------------------ #
    def execute_order(
        self,
        order: PlannedOrder,
        *,
        dry_run: bool,
        ref_price: Optional[float] = None,
    ) -> ExecutionReceipt:
        """Execute (or simulate) one order. Always returns a receipt; never raises."""
        try:
            return self._execute_order_inner(order, dry_run=dry_run, ref_price=ref_price)
        except Exception as exc:  # noqa: BLE001 - failure must be visible, never crash
            # Defensive backstop: any unexpected error becomes a visible failed
            # receipt rather than propagating out of a public method.
            return self._receipt(
                order,
                status="failed",
                dry_run=bool(dry_run),
                error="execution_adapter_error:" + type(exc).__name__,
            )

    def _execute_order_inner(
        self,
        order: PlannedOrder,
        *,
        dry_run: bool,
        ref_price: Optional[float],
    ) -> ExecutionReceipt:
        # --- step 0: MOVE_STOP is a perp concept; spot has no resting stop --- #
        # The breakeven move only applies to the Aster perp venue. On spot we
        # simply acknowledge it as a no-op (visible, never a silent drop).
        if order.action is OrderAction.MOVE_STOP:
            return self._receipt(
                order,
                status="skipped",
                dry_run=bool(dry_run),
                error="move_stop_not_supported_on_spot",
            )

        # --- step 1: resolve the on-chain token ----------------------------- #
        token = self.universe.token_info(order.symbol) or self.universe.token_info(
            order.base_asset
        )
        if token is None or not token.address:
            return self._receipt(
                order,
                status="skipped",
                dry_run=bool(dry_run),
                error="not_onchain_tradeable",
                raw={
                    "base_asset": order.base_asset,
                    "chain_id": self.onchain.chain_id,
                },
            )

        # --- step 2: short-leg gate (MVP is long-spot only) ----------------- #
        if order.side is Side.SHORT and not self.config.enable_short_leg:
            return self._receipt(
                order,
                status="skipped",
                dry_run=bool(dry_run),
                error="short_leg_disabled",
                token=token,
                raw={
                    "note": "on-chain spot MVP: SHORT requires a perp/synthetic "
                    "stretch adapter; enable via config.enable_short_leg",
                },
            )

        # --- step 3: gas floor (failure visible, not silent) ---------------- #
        # Only a pre-broadcast safety: a dry-run simulates the quote regardless
        # of gas, so we skip this check when not actually signing. (Otherwise an
        # unfunded / unset wallet would block the simulation in web3 mode.)
        if not dry_run:
            gas_balance = float(self.twak.native_balance())
            if gas_balance < float(self.onchain.gas_min_bnb):
                return self._receipt(
                    order,
                    status="failed",
                    dry_run=False,
                    error="insufficient_gas",
                    token=token,
                    raw={
                        "native_balance": gas_balance,
                        "gas_min_bnb": self.onchain.gas_min_bnb,
                    },
                )

        # --- step 4: determine token_in / token_out ------------------------- #
        usdt = self.universe.usdt()
        token_in, token_out, decimals_in, decimals_out, amount_in = self._legs(
            order, token, usdt
        )
        if amount_in <= 0.0:
            return self._receipt(
                order,
                status="failed",
                dry_run=bool(dry_run),
                error="zero_amount_in",
                token=token,
                token_in=token_in,
                token_out=token_out,
                raw={"notional_usd": order.notional_usd, "quantity": order.quantity},
            )

        # --- step 5: quote + execution-time final guard --------------------- #
        quote = self.router.quote(
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            decimals_in=decimals_in,
            decimals_out=decimals_out,
            ref_price=self._ref_price_for(order, ref_price),
        )
        guard = self.universe.final_guard(order, quote)
        if guard is not None:
            return self._receipt(
                order,
                status="skipped",
                dry_run=bool(dry_run),
                error=guard.reason,
                token=token,
                token_in=token_in,
                token_out=token_out,
                quote=quote,
                raw={"final_guard": dict(guard.detail), "stage": guard.stage},
            )

        path = list(quote.get("path") or self.router.build_path(token_in, token_out))
        amount_in_wei = int(quote.get("amount_in_wei") or 0)
        min_amount_out_wei = int(quote.get("min_amount_out") or 0)
        amount_out = float(quote.get("amount_out") or 0.0)
        quote_price = float(quote.get("price") or 0.0)
        min_amount_out_human = self._from_wei(min_amount_out_wei, decimals_out)

        # --- step 6: dry-run — full quote, no signing, no broadcast --------- #
        if dry_run:
            return self._receipt(
                order,
                status="dry_run",
                dry_run=True,
                token=token,
                token_in=token_in,
                token_out=token_out,
                amount_in=amount_in,
                amount_out=amount_out,
                min_amount_out=min_amount_out_human,
                price=quote_price,
                quote=quote,
                raw={
                    "path": path,
                    "amount_in_wei": str(amount_in_wei),
                    "min_amount_out_wei": str(min_amount_out_wei),
                    "twak_mode": self.twak.mode,
                    "note": "simulated; no approve/swap broadcast",
                },
            )

        # --- step 7: live — approve (exact) then swap ----------------------- #
        router_addr = self.universe.router_address() or self.router.router_addr()
        approve_res = self.twak.approve(token_in, router_addr, amount_in_wei)
        if not approve_res.ok:
            return self._receipt(
                order,
                status="failed",
                dry_run=False,
                error="approve_failed:" + (approve_res.error or "unknown"),
                token=token,
                token_in=token_in,
                token_out=token_out,
                amount_in=amount_in,
                quote=quote,
                tx_hash=approve_res.tx_hash,
                raw={"approve": dict(approve_res.raw)},
            )

        swap_res = self.twak.swap_exact_tokens_for_tokens(
            token_in=token_in,
            token_out=token_out,
            amount_in_wei=amount_in_wei,
            min_amount_out_wei=min_amount_out_wei,
            path=path,
        )
        if not swap_res.ok:
            return self._receipt(
                order,
                status="failed",
                dry_run=False,
                error="swap_failed:" + (swap_res.error or "unknown"),
                token=token,
                token_in=token_in,
                token_out=token_out,
                amount_in=amount_in,
                quote=quote,
                tx_hash=swap_res.tx_hash,
                raw={"swap": dict(swap_res.raw)},
            )

        # filled — build the receipt from the swap result.
        filled_out = float(swap_res.amount_out or 0.0)
        filled_out_human = self._from_wei(filled_out, decimals_out) if filled_out else amount_out
        return self._receipt(
            order,
            status="filled",
            dry_run=False,
            token=token,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            amount_out=filled_out_human,
            min_amount_out=min_amount_out_human,
            price=quote_price,
            gas_used=int(swap_res.gas_used or 0),
            tx_hash=swap_res.tx_hash,
            quote=quote,
            raw={
                "path": path,
                "approve_tx": approve_res.tx_hash,
                "swap": dict(swap_res.raw),
                "twak_mode": self.twak.mode,
            },
        )

    # ------------------------------------------------------------------ #
    # batch
    # ------------------------------------------------------------------ #
    def execute_orders(
        self,
        orders: List[PlannedOrder],
        *,
        dry_run: bool,
        ref_prices: Optional[Dict[str, float]] = None,
    ) -> List[ExecutionReceipt]:
        """Execute a list of orders, returning one receipt per order (in order).

        ``ref_prices`` maps base asset -> USD reference price, used to feed the
        mock quote path. One bad order never aborts the batch — each gets its own
        visible receipt.
        """
        prices = {k.upper(): v for k, v in (ref_prices or {}).items()}
        receipts: List[ExecutionReceipt] = []
        for order in orders or []:
            rp = prices.get((order.base_asset or "").upper())
            receipts.append(self.execute_order(order, dry_run=dry_run, ref_price=rp))
        return receipts

    # ================================================================== #
    # internals
    # ================================================================== #
    def _legs(
        self,
        order: PlannedOrder,
        token: TokenInfo,
        usdt: TokenInfo,
    ):
        """Resolve (token_in, token_out, decimals_in, decimals_out, amount_in).

        OPEN long  = USDT -> token (spend ``notional_usd`` USDT).
        CLOSE long = token -> USDT (sell ``quantity`` token; fall back to
                     notional/price when quantity is unset).
        """
        usdt_addr = usdt.address
        usdt_dec = int(usdt.decimals)
        token_addr = token.address
        token_dec = int(token.decimals)

        if order.action is OrderAction.OPEN:
            # buy the token with USDT
            amount_in = float(order.notional_usd)
            if amount_in <= 0.0 and order.quantity > 0 and order.entry_price > 0:
                amount_in = float(order.quantity) * float(order.entry_price)
            return usdt_addr, token_addr, usdt_dec, token_dec, amount_in

        # CLOSE: sell the token back to USDT
        amount_in = float(order.quantity)
        if amount_in <= 0.0:
            price = float(order.entry_price) or float(order.metadata.get("current_price", 0.0))
            if order.notional_usd > 0 and price > 0:
                amount_in = float(order.notional_usd) / price
        return token_addr, usdt_addr, token_dec, usdt_dec, amount_in

    def _ref_price_for(self, order: PlannedOrder, ref_price: Optional[float]) -> Optional[float]:
        """Pick the USD-per-token reference for the (mock) quote.

        Prefer an explicit ``ref_price``, then the order's ``entry_price`` — both
        are USD-per-token, which is what :meth:`PancakeRouter.quote` expects for
        either swap direction.
        """
        if ref_price is not None and ref_price > 0:
            return float(ref_price)
        if order.entry_price and order.entry_price > 0:
            return float(order.entry_price)
        return None

    def _explorer_url(self, tx_hash: str) -> str:
        if not tx_hash:
            return ""
        base = (self.onchain.explorer_base or "").rstrip("/")
        if not base:
            return ""
        return base + "/tx/" + tx_hash

    @staticmethod
    def _from_wei(amount_wei: float, decimals: int) -> float:
        try:
            if amount_wei <= 0:
                return 0.0
            return float(amount_wei) / (10 ** int(decimals))
        except (TypeError, ValueError):
            return 0.0

    def _receipt(
        self,
        order: PlannedOrder,
        *,
        status: str,
        dry_run: bool,
        token: Optional[TokenInfo] = None,
        token_in: str = "",
        token_out: str = "",
        amount_in: float = 0.0,
        amount_out: float = 0.0,
        min_amount_out: float = 0.0,
        price: float = 0.0,
        gas_used: int = 0,
        tx_hash: str = "",
        error: str = "",
        quote: Optional[Dict[str, Any]] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> ExecutionReceipt:
        payload: Dict[str, Any] = {
            "symbol": order.symbol,
            "base_asset": order.base_asset,
            "action": order.action.value,
            "side": order.side.value,
            "twak_mode": self.twak.mode,
        }
        if token is not None:
            payload["token_address"] = token.address
            payload["decimals"] = token.decimals
        if quote is not None:
            # keep the quote snapshot for audit; it is already secret-free.
            payload["quote"] = dict(quote)
        if raw:
            payload.update(raw)

        return ExecutionReceipt(
            symbol=order.symbol,
            side=order.side,
            action=order.action,
            status=status,
            tx_hash=tx_hash or "",
            token_in=token_in or "",
            token_out=token_out or "",
            amount_in=float(amount_in),
            amount_out=float(amount_out),
            min_amount_out=float(min_amount_out),
            price=float(price),
            gas_used=int(gas_used),
            error=error or "",
            dry_run=bool(dry_run),
            as_of=_utc_now_iso(),
            explorer_url=self._explorer_url(tx_hash),
            raw=payload,
        )
