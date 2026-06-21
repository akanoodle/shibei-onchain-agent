"""Account reconciliation + exit detection — the on-chain "where am I?" layer.

拾贝 V3.0 trades a custodial perp account where positions, equity and free
margin are *reported back* by the venue. On-chain there is no such oracle: the
only ground truth is the wallet's token balances on BSC. This module turns that
raw, balance-level truth into the same :class:`AccountState` / :class:`Position`
contract the brain already speaks, and then detects which open positions should
be *closed* this cycle.

Two responsibilities, both faithful to the 拾贝 principle *"failure must be
visible, never silent"*:

    * :meth:`Reconciler.read_account` — read native BNB (the gas floor), free
      USDT (the quote balance) and every registry token balance via the
      :class:`TwakClient`, value each holding (at ``ref_prices`` or a carried
      prior entry price), and assemble a fresh :class:`AccountState`. The
      same-coin circuit-breaker history (``stop_loss_events``) and the per-hour
      order counter are *carried forward* from the prior state so the risk stack
      keeps its memory across cycles. In the default ``mock`` TwakClient mode
      this is fully deterministic: equity is exactly
      ``config.initial_equity_usd`` held in USDT, with no open positions unless a
      ``prior`` state carries them.

    * :meth:`Reconciler.detect_exits` — recompute each open position's
      R-multiple against its live price and emit a
      ``PlannedOrder(action=CLOSE)`` when (a) take-profit is reached
      (``rr >= take_profit_r_multiple``), (b) price has crossed the stop, or
      (c) a SHORT position has exceeded its ``max_hold_hours`` (the time-stop
      that 拾贝 applies to its short legs). The ``reason`` field records *why*.

Reads never raise: a balance read that fails upstream simply returns 0.0, and
the resulting state records a note rather than crashing the cycle. No
third-party import happens at module load — this file is pure stdlib.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from shibei_onchain.config import AgentConfig, RiskParams
from shibei_onchain.models import (
    AccountState,
    OrderAction,
    PlannedOrder,
    Position,
    Side,
    base_asset_of,
)
from shibei_onchain.onchain.twak_client import TwakClient
from shibei_onchain.onchain.universe_filter import UniverseFilter


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> Optional[datetime]:
    """Best-effort ISO-8601 parse; returns ``None`` on anything unparseable.

    Tolerates a trailing ``Z`` (treated as UTC) and naive timestamps (assumed
    UTC). Never raises — a bad timestamp simply means "age unknown".
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Reconciler:
    """Reconcile wallet balances into an :class:`AccountState` and detect exits.

    The reconciler is intentionally stateless between cycles: all carried memory
    (circuit-breaker stop-loss history, per-hour order count, the prior open
    positions whose entry prices anchor valuation) flows in via the ``prior``
    :class:`AccountState`. That makes a cycle reproducible from its inputs.
    """

    def __init__(
        self, config: AgentConfig, twak: TwakClient, universe: UniverseFilter
    ) -> None:
        self.config = config
        self.twak = twak
        self.universe = universe
        self.onchain = config.onchain

    # ------------------------------------------------------------------ #
    # read_account
    # ------------------------------------------------------------------ #
    def read_account(
        self,
        prior: Optional[AccountState] = None,
        ref_prices: Optional[Dict[str, float]] = None,
    ) -> AccountState:
        """Read wallet balances and assemble a fresh :class:`AccountState`.

        * native BNB -> ``gas_balance`` (the gas floor the executor checks),
        * free USDT -> ``quote_balance_usd`` (valued 1:1),
        * each registry token balance -> a :class:`Position` (when a prior
          position or a non-trivial balance exists), valued at
          ``ref_prices[base]`` or the carried prior entry price,
        * ``equity_usd`` = free USDT + Σ position notional + (gas not counted as
          tradeable equity).

        Carries forward ``prior.stop_loss_events`` and
        ``prior.open_orders_this_hour`` so the risk stack keeps its memory.
        Never raises — a failed read degrades to 0.0 and a visible note.
        """
        prices = {(k or "").upper(): float(v) for k, v in (ref_prices or {}).items()}
        prior_positions: Dict[str, Position] = {}
        if prior is not None:
            for pos in prior.positions:
                prior_positions[pos.base_asset.upper()] = pos

        notes: List[str] = []
        source = "mock" if self.twak.mode == "mock" else "reconcile"

        # -- gas (native BNB) --------------------------------------------- #
        gas_balance = self._safe_float(self.twak.native_balance, notes, "native_balance")

        # -- free USDT (quote balance, valued 1:1) ------------------------ #
        usdt_info = self.universe.usdt()
        quote_balance_usd = 0.0
        if usdt_info.address:
            quote_balance_usd = self._safe_token_balance(
                usdt_info.address, usdt_info.decimals, notes, "USDT"
            )

        # -- positions: every registry token except the quote stable ------ #
        positions: List[Position] = []
        for base in self.universe.all_base_assets():
            base_up = base.upper()
            if base_up == "USDT":
                continue
            info = self.universe.token_info(base_up)
            if info is None or not info.address:
                continue

            balance = self._safe_token_balance(
                info.address, info.decimals, notes, base_up
            )
            prior_pos = prior_positions.get(base_up)

            # Only materialize a Position when there is something to hold: a real
            # token balance, or a carried prior position. (Mock mode holds no
            # tokens, so this yields no positions unless `prior` carries them.)
            if balance <= 0.0 and prior_pos is None:
                continue

            position = self._build_position(
                base_up, info.symbol, balance, prior_pos, prices, notes
            )
            if position is not None:
                positions.append(position)

        position_notional = sum(max(0.0, p.notional_usd) for p in positions)
        equity_usd = quote_balance_usd + position_notional

        # In mock mode with no carried positions, anchor equity to the configured
        # initial equity so the deterministic offline path is self-consistent
        # (the synthetic USDT ledger already returns that figure, but we make the
        # contract explicit and robust to ledger drift).
        if source == "mock" and not positions and quote_balance_usd <= 0.0:
            quote_balance_usd = float(self.config.initial_equity_usd)
            equity_usd = quote_balance_usd
            notes.append("mock_equity_anchored_to_initial_equity_usd")

        # -- carry forward cross-cycle memory ----------------------------- #
        stop_loss_events = list(prior.stop_loss_events) if prior is not None else []
        open_orders_this_hour = prior.open_orders_this_hour if prior is not None else 0

        return AccountState(
            equity_usd=equity_usd,
            quote_balance_usd=quote_balance_usd,
            gas_balance=gas_balance,
            positions=positions,
            open_orders_this_hour=open_orders_this_hour,
            stop_loss_events=stop_loss_events,
            as_of=_now_iso(),
            source=source,
            notes=notes,
            raw={
                "twak_mode": self.twak.mode,
                "chain_id": self.onchain.chain_id,
                "position_notional_usd": position_notional,
            },
        )

    # ------------------------------------------------------------------ #
    # detect_exits
    # ------------------------------------------------------------------ #
    def detect_exits(
        self,
        account: AccountState,
        risk: RiskParams,
        *,
        now: Optional[datetime] = None,
    ) -> List[PlannedOrder]:
        """Emit a ``PlannedOrder(action=CLOSE)`` for each position that should
        exit this cycle.

        Three exit triggers, evaluated per open position:

        * **take-profit**: recomputed ``rr_multiple >= take_profit_r_multiple``,
        * **stop-loss**: live ``current_price`` has crossed ``stop_loss_price``
          (``<=`` for LONG, ``>=`` for SHORT),
        * **time-stop (SHORT only)**: holding age exceeds ``max_hold_hours``
          (the position's own value when set, else the strategy default).

        ``reason`` is ``take_profit`` / ``stop_loss`` / ``short_max_hold``.
        Stop-loss takes priority over take-profit when both would fire (a gap
        through the stop is an exit at the stop, not a win). Never raises.
        """
        now = now or datetime.now(timezone.utc)
        orders: List[PlannedOrder] = []

        for pos in account.positions:
            if pos.quantity <= 0:
                continue

            rr = pos.recompute_rr()
            tp_r = pos.take_profit_r_multiple or risk.take_profit_r

            stop_hit = self._stop_hit(pos)
            tp_hit = pos.current_price > 0 and rr >= tp_r
            max_hold = self._short_max_hold_exceeded(pos, risk, now)

            reason: Optional[str] = None
            detail: Dict[str, object] = {}
            # stop-loss is evaluated first: it dominates a simultaneous TP.
            if stop_hit:
                reason = "stop_loss"
                detail = {
                    "current_price": pos.current_price,
                    "stop_loss_price": pos.stop_loss_price,
                    "rr_multiple": rr,
                }
            elif tp_hit:
                reason = "take_profit"
                detail = {
                    "rr_multiple": rr,
                    "take_profit_r_multiple": tp_r,
                    "current_price": pos.current_price,
                }
            elif max_hold:
                reason = "short_max_hold"
                detail = {
                    "opened_at": pos.opened_at,
                    "max_hold_hours": self._effective_max_hold(pos, risk),
                    "now": now.isoformat(),
                }

            if reason is None:
                continue

            orders.append(self._close_order(pos, reason, detail, rr))

        return orders

    # ================================================================== #
    # internal helpers
    # ================================================================== #
    def _build_position(
        self,
        base: str,
        symbol: str,
        balance: float,
        prior_pos: Optional[Position],
        prices: Dict[str, float],
        notes: List[str],
    ) -> Optional[Position]:
        """Construct a :class:`Position` from a token balance + carried prior.

        Quantity is the live wallet balance (or the prior quantity if the live
        read is zero but a prior position is carried). Entry price comes from the
        prior position; current price from ``ref_prices`` (falling back to the
        entry price, i.e. a flat mark, when no live price is supplied).
        """
        quantity = balance if balance > 0.0 else (prior_pos.quantity if prior_pos else 0.0)
        if quantity <= 0.0:
            return None

        entry_price = prior_pos.entry_price if prior_pos else prices.get(base, 0.0)
        current_price = prices.get(base, entry_price)
        if current_price <= 0.0 and entry_price > 0.0:
            current_price = entry_price

        side = prior_pos.side if prior_pos else Side.LONG
        stop_loss_price = prior_pos.stop_loss_price if prior_pos else 0.0
        tp_r = prior_pos.take_profit_r_multiple if prior_pos else 2.5
        be_r = prior_pos.breakeven_r_multiple if prior_pos else 1.0
        max_hold = prior_pos.max_hold_hours if prior_pos else 0.0
        opened_at = prior_pos.opened_at if prior_pos else ""
        strategy_id = prior_pos.strategy_id if prior_pos else ""
        strategy_leg = prior_pos.strategy_leg if prior_pos else ""
        tx_hash = prior_pos.tx_hash if prior_pos else ""

        notional_usd = quantity * (current_price if current_price > 0.0 else 0.0)
        if current_price <= 0.0:
            notes.append("position_unpriced:" + base)

        position = Position(
            symbol=symbol or (base + "USDT"),
            side=side,
            base_asset=base,
            quantity=quantity,
            entry_price=entry_price,
            notional_usd=notional_usd,
            current_price=current_price,
            stop_loss_price=stop_loss_price,
            take_profit_r_multiple=tp_r,
            breakeven_r_multiple=be_r,
            max_hold_hours=max_hold,
            opened_at=opened_at,
            strategy_id=strategy_id,
            strategy_leg=strategy_leg,
            tx_hash=tx_hash,
        )
        position.recompute_rr()
        return position

    @staticmethod
    def _stop_hit(pos: Position) -> bool:
        if pos.stop_loss_price <= 0.0 or pos.current_price <= 0.0:
            return False
        if pos.side is Side.LONG:
            return pos.current_price <= pos.stop_loss_price
        return pos.current_price >= pos.stop_loss_price

    def _short_max_hold_exceeded(
        self, pos: Position, risk: RiskParams, now: datetime
    ) -> bool:
        if pos.side is not Side.SHORT:
            return False
        max_hold = self._effective_max_hold(pos, risk)
        if max_hold <= 0.0:
            return False
        opened = _parse_iso(pos.opened_at)
        if opened is None:
            return False
        age_hours = (now - opened).total_seconds() / 3600.0
        return age_hours > max_hold

    @staticmethod
    def _effective_max_hold(pos: Position, risk: RiskParams) -> float:
        if pos.max_hold_hours and pos.max_hold_hours > 0.0:
            return pos.max_hold_hours
        # default to the longer 拾贝 short time-stop when the position carries none.
        return risk.s16a_v1_max_hold_hours

    def _close_order(
        self,
        pos: Position,
        reason: str,
        detail: Dict[str, object],
        rr: float,
    ) -> PlannedOrder:
        return PlannedOrder(
            symbol=pos.symbol,
            side=pos.side,
            action=OrderAction.CLOSE,
            base_asset=pos.base_asset,
            quote_asset=self.onchain.quote_asset or "USDT",
            notional_usd=pos.notional_usd,
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            stop_loss_price=pos.stop_loss_price,
            take_profit_r_multiple=pos.take_profit_r_multiple,
            breakeven_r_multiple=pos.breakeven_r_multiple,
            max_slippage_bps=self.onchain.max_slippage_bps,
            strategy_id=pos.strategy_id,
            strategy_leg=pos.strategy_leg,
            reason=reason,
            metadata={
                "exit": reason,
                "rr_multiple": rr,
                "current_price": pos.current_price,
                **detail,
            },
        )

    # -- read guards (degrade, never raise) --------------------------------- #
    @staticmethod
    def _safe_float(fn, notes: List[str], label: str) -> float:
        try:
            return float(fn())
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            notes.append("read_failed:{}:{}".format(label, type(exc).__name__))
            return 0.0

    def _safe_token_balance(
        self, address: str, decimals: int, notes: List[str], label: str
    ) -> float:
        try:
            return float(self.twak.token_balance(address, int(decimals)))
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            notes.append("read_failed:{}:{}".format(label, type(exc).__name__))
            return 0.0
