"""End-to-end orchestrator: read market -> decide -> execute on-chain -> reconcile.

This is the venue-specific counterpart of 拾贝's
``execute_shibei_v2_combo_live_prepared_orders`` — it consumes the *same*
venue-agnostic ``PlannedOrder`` contract the brain emits, but routes execution
to BNB Chain via the Trust Wallet Agent Kit instead of a Binance perp.

The full cycle:

    CMC Agent Hub signal  ─┐
                           ├─► on-chain reconcile (account_state)
    拾贝 scanner (ranking) ─┘            │
            │                            ▼
            ▼                   universe hard-filter (on-chain tradeable ∩ liquid)
       BTC env gate + risk stack ───► planned_orders (sizing)
            │                            │
            ▼                            ▼
       exits (TP / stop / max-hold) + opens ──► OnChainExecutionAdapter
                                                  (PancakeSwap swap via TWAK)
                                                       │
                                                       ▼
                                              account_state writeback + Feishu

By default ``DRY_RUN`` is on and the live gate is closed, so ``run_cycle`` plans
and simulates without signing anything.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from shibei_onchain.config import AgentConfig, load_config
from shibei_onchain.models import (
    AccountState,
    Candidate,
    ExecutionReceipt,
    MarketSignal,
    OrderAction,
    PlannedOrder,
    Position,
    RiskDecision,
    Side,
    SkippedOrder,
    StopLossEvent,
)
from shibei_onchain.signals.cmc_agent_hub import CmcAgentHub
from shibei_onchain.signals.regime_gate import evaluate_regime_gate
from shibei_onchain.brain.scanner import Scanner
from shibei_onchain.brain.risk import apply_risk_stack
from shibei_onchain.brain.planner import build_planned_orders
from shibei_onchain.onchain.universe_filter import UniverseFilter
from shibei_onchain.onchain.pancake_router import PancakeRouter
from shibei_onchain.onchain.twak_client import TwakClient
from shibei_onchain.onchain.reconcile import Reconciler
from shibei_onchain.onchain.execution_adapter import OnChainExecutionAdapter
from shibei_onchain.delivery.feishu import FeishuNotifier


STATE_FILE = "onchain_live.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str) -> Optional[datetime]:
    """Best-effort ISO-8601 parse (tolerates a trailing Z on Python 3.9)."""
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


def _same_clock_hour(ts: str, now: datetime) -> bool:
    """True iff ``ts`` falls in the same UTC clock hour as ``now``."""
    dt = _parse_iso(ts)
    if dt is None:
        return False
    return (dt.year, dt.month, dt.day, dt.hour) == (now.year, now.month, now.day, now.hour)


def _json_safe(value: Any) -> Any:
    """Recursively convert dataclasses / enums / paths into JSON-safe values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _json_safe(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


# --------------------------------------------------------------------------- #
@dataclass
class CycleResult:
    as_of: str
    live: bool
    signal: MarketSignal
    account_before: AccountState
    account_after: AccountState
    candidates: List[Candidate]
    universe_skipped: List[SkippedOrder]
    decision: RiskDecision
    planned_orders: List[PlannedOrder]
    exits: List[PlannedOrder]
    receipts: List[ExecutionReceipt]
    notes: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        filled = [r for r in self.receipts if r.status in ("filled", "dry_run")]
        return {
            "as_of": self.as_of,
            "live_orders": self.live,
            "regime": self.signal.regime.value,
            "btc_trend": self.signal.btc_trend.value,
            "signal_source": self.signal.source,
            "equity_usd": round(self.account_after.equity_usd, 2),
            "open_positions": len(self.account_after.positions),
            "candidates": len(self.candidates),
            "universe_skipped": len(self.universe_skipped),
            "risk_approved": len(self.decision.approved),
            "risk_rejected": len(self.decision.rejected),
            "planned_opens": len(self.planned_orders),
            "exits": len(self.exits),
            "receipts": len(self.receipts),
            "executed": len(filled),
            "tx_hashes": [r.tx_hash for r in self.receipts if r.tx_hash][:5],
            "notes": self.notes,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "as_of": self.as_of,
            "live": self.live,
            "summary": self.summary(),
            "signal": _json_safe(self.signal),
            "account_after": _json_safe(self.account_after),
            "decision": {
                "approved": [_json_safe(c) for c in self.decision.approved],
                "rejected": [_json_safe(s) for s in self.decision.rejected],
                "notes": self.decision.notes,
            },
            "universe_skipped": [_json_safe(s) for s in self.universe_skipped],
            "planned_orders": [_json_safe(o) for o in self.planned_orders],
            "exits": [_json_safe(o) for o in self.exits],
            "receipts": [_json_safe(r) for r in self.receipts],
        }


# --------------------------------------------------------------------------- #
# execution venues — both expose the same interface so the orchestrator is
# venue-agnostic: filter_candidates / read_account / detect_exits / execute_orders.
# --------------------------------------------------------------------------- #
class _PancakeVenue:
    """PancakeSwap spot venue (long = USDT→token swap; exits swap back)."""

    name = "pancake"

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.universe = UniverseFilter(config.onchain)
        self.twak = TwakClient(config.onchain)
        self.router = PancakeRouter(config.onchain)
        self.reconciler = Reconciler(config, self.twak, self.universe)
        self.adapter = OnChainExecutionAdapter(config, self.twak, self.router, self.universe)

    def filter_candidates(self, candidates, signal=None):
        return self.universe.filter(candidates, signal)

    def read_account(self, prior=None, ref_prices=None):
        return self.reconciler.read_account(prior=prior, ref_prices=ref_prices)

    def detect_exits(self, account, risk, *, now=None):
        return self.reconciler.detect_exits(account, risk, now=now)

    def execute_orders(self, orders, *, dry_run, ref_prices=None):
        return self.adapter.execute_orders(orders, dry_run=dry_run, ref_prices=ref_prices)

    def health(self):
        return self.twak.health()


def build_venue(config: AgentConfig):
    """Select the execution venue. ``aster`` routes BOTH legs to Aster perps;
    ``pancake`` (default) is long-spot on PancakeSwap."""
    if (config.venue or "pancake").lower() == "aster":
        from shibei_onchain.onchain.aster_perp import AsterPerpClient
        from shibei_onchain.onchain.aster_adapter import AsterExecutionAdapter

        return AsterExecutionAdapter(config, AsterPerpClient(config.aster))
    return _PancakeVenue(config)


# --------------------------------------------------------------------------- #
class OnChainAgent:
    """Wires the layers together and runs cycles."""

    def __init__(self, config: Optional[AgentConfig] = None) -> None:
        self.config = config or load_config()
        self.cmc = CmcAgentHub(self.config.cmc)
        self.scanner = Scanner(self.config)
        self.venue = build_venue(self.config)
        self.feishu = FeishuNotifier.from_config(self.config)

    # -- state persistence ------------------------------------------------- #
    def _state_path(self) -> Path:
        return Path(self.config.state_dir) / STATE_FILE

    def _load_prior_account(self) -> Optional[AccountState]:
        """Reload the rolling risk bookkeeping (stop-loss events + per-hour
        order count) so the circuit breaker and rate limit survive restarts.
        Positions themselves are re-read from chain by the reconciler."""
        path = self._state_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        acct = data.get("account_after") or {}
        # The ≤N-orders-per-hour budget is a *clock-hour* limit: reset it when the
        # persisted cycle was in a previous hour, otherwise it would brick the
        # agent forever after N lifetime orders.
        now = _utcnow()
        last_as_of = str(data.get("as_of") or acct.get("as_of") or "")
        open_orders = int(acct.get("open_orders_this_hour", 0) or 0)
        if not _same_clock_hour(last_as_of, now):
            open_orders = 0
        events: List[StopLossEvent] = []
        for raw in acct.get("stop_loss_events") or []:
            try:
                events.append(
                    StopLossEvent(
                        base_asset=str(raw.get("base_asset", "")),
                        side=Side(raw.get("side", "long")),
                        at=str(raw.get("at", "")),
                        price=float(raw.get("price", 0.0) or 0.0),
                        detail=raw.get("detail") or {},
                    )
                )
            except (ValueError, TypeError):
                continue
        prior = AccountState(
            stop_loss_events=events,
            open_orders_this_hour=open_orders,
            source="restored",
        )
        return prior

    def _persist(self, result: CycleResult, position_meta: Optional[Dict[str, Any]] = None,
                 safety_state: Optional[Dict[str, Any]] = None) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "config": self.config.redacted(),
                "position_meta": position_meta or {},
                "safety_state": safety_state or {},
                **result.to_dict(),
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # -- account-level safety state (high-water mark + day-start equity) ---- #
    def _load_safety_state(self) -> Dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        s = data.get("safety_state")
        return dict(s) if isinstance(s, dict) else {}

    def _sync_safety_state(self, account: AccountState, safety: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        """Maintain the peak (high-water mark) and the start-of-UTC-day equity that
        drive the drawdown / daily-loss kill-switches, and stamp them onto the
        live account so the risk stack can act. A non-positive equity read (e.g. a
        transient RPC failure) is ignored so a blip can't trip the switch."""
        eq = account.equity_usd if account.equity_usd and account.equity_usd > 0 else 0.0
        prev_peak = float(safety.get("peak_equity_usd") or 0.0)
        prev_day_start = float(safety.get("day_start_equity_usd") or 0.0)
        today = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        prev_day = str(safety.get("day_start_date") or "")
        if eq > 0:
            peak = max(prev_peak, eq)
            if prev_day != today or prev_day_start <= 0:
                day_start, day = eq, today
            else:
                day_start, day = prev_day_start, prev_day
        else:
            peak, day_start, day = prev_peak, prev_day_start, (prev_day or today)
        account.peak_equity_usd = peak
        account.day_start_equity_usd = day_start
        return {"peak_equity_usd": peak, "day_start_equity_usd": day_start, "day_start_date": day}

    # -- per-position management metadata (open time + current stop) ------- #
    def _load_position_meta(self) -> Dict[str, Any]:
        """Reload per-symbol management metadata (opened_at / entry / current
        stop) so the +1R breakeven move and max-hold backstop survive restarts.
        The exchange does not report when *we* opened a position, so we track it
        ourselves. Never raises."""
        path = self._state_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        meta = data.get("position_meta")
        return dict(meta) if isinstance(meta, dict) else {}

    def _sync_position_meta(self, account: AccountState, meta: Dict[str, Any], now_iso: str) -> Dict[str, Any]:
        """Reconcile metadata with the live book and ENRICH each Position with
        opened_at / current-stop / max-hold so the management layer can act.

        A position we've never seen (e.g. opened before this agent ran) is stamped
        opened_at=now (a conservative first-seen; documented) with a derived
        initial stop. Closed symbols are dropped."""
        risk = self.config.risk
        stop_pct = getattr(risk, "initial_stop_distance_pct", 0.06) or 0.06
        max_hold = float(getattr(risk, "long_max_hold_hours", 0.0) or 0.0)
        be_r = float(getattr(risk, "long_breakeven_r", 1.0) or 1.0)
        live: set = set()
        for pos in account.positions or []:
            sym = pos.symbol.upper()
            live.add(sym)
            m = meta.get(sym)
            if not isinstance(m, dict):
                init_stop = pos.entry_price * (1.0 - stop_pct) if pos.side is Side.LONG \
                    else pos.entry_price * (1.0 + stop_pct)
                m = {
                    "opened_at": now_iso,
                    "entry": pos.entry_price,
                    "stop": round(init_stop, 10),
                    "leg": pos.strategy_leg,
                    "first_seen": now_iso,
                }
                meta[sym] = m
            # enrich the live Position so detect_exits / risk see management context
            pos.opened_at = str(m.get("opened_at") or now_iso)
            pos.stop_loss_price = float(m.get("stop") or pos.stop_loss_price or 0.0)
            pos.max_hold_hours = max_hold
            pos.breakeven_r_multiple = be_r
        for sym in list(meta.keys()):
            if str(sym).upper() not in live:
                meta.pop(sym, None)
        return meta

    @staticmethod
    def _apply_receipts_to_meta(receipts: List[ExecutionReceipt], meta: Dict[str, Any]) -> Dict[str, Any]:
        """Fold executed management fills back into metadata: a filled breakeven
        move records the stop now resting at entry (so it isn't re-issued); a
        filled close drops the symbol."""
        for r in receipts:
            if r.status != "filled":
                continue
            sym = r.symbol.upper()
            if r.action is OrderAction.MOVE_STOP and sym in meta:
                meta[sym]["stop"] = meta[sym].get("entry") or meta[sym].get("stop")
            elif r.action is OrderAction.CLOSE:
                meta.pop(sym, None)
        return meta

    # -- helpers ----------------------------------------------------------- #
    def _ref_prices(self, candidates: List[Candidate], account: AccountState) -> Dict[str, float]:
        prices: Dict[str, float] = {}
        for pos in account.positions:
            if pos.current_price > 0:
                prices[pos.base_asset.upper()] = pos.current_price
            elif pos.entry_price > 0:
                prices[pos.base_asset.upper()] = pos.entry_price
        for cand in candidates:
            if cand.price > 0:
                prices[cand.base_asset.upper()] = cand.price
        return prices

    # -- the cycle --------------------------------------------------------- #
    def run_cycle(self, *, persist: bool = True) -> CycleResult:
        now = _utcnow()
        as_of = now.isoformat()
        notes: List[str] = []
        live = self.config.live_orders_allowed
        if not live:
            notes.append("dry_run/preview: " + ",".join(self.config.blocked_reasons()))

        # 1. signal layer — CMC Agent Hub
        signal = self.cmc.fetch_market_signal()

        # 2. account state — reconcile from the venue (carry rolling risk bookkeeping)
        prior = self._load_prior_account()
        account_before = self.venue.read_account(prior=prior)

        # 2b. position-management metadata — enrich open positions with the
        # opened_at / current-stop the exchange does not report, so the +1R
        # breakeven move and the max-hold backstop can act on them.
        position_meta = self._sync_position_meta(account_before, self._load_position_meta(), as_of)

        # 2c. account-safety state — maintain the high-water mark + day-start
        # equity that drive the drawdown / daily-loss kill-switches.
        safety_state = self._sync_safety_state(account_before, self._load_safety_state(), now)

        # 3. decision layer — 拾贝 ranking
        candidates = self.scanner.scan(signal=signal)
        ref_prices = self._ref_prices(candidates, account_before)

        # 4. tradeable-universe hard-filter (venue-specific: spot DEX depth vs Aster listings)
        kept, universe_skipped = self.venue.filter_candidates(candidates, signal)

        # 5. risk stack (BTC gate + sizing caps + conflict/circuit rules)
        decision = apply_risk_stack(kept, account_before, signal, self.config.risk, now=now)

        # 6. plan opens + detect exits
        planned_orders = build_planned_orders(decision.approved, account_before, self.config.risk)
        exits = self.venue.detect_exits(account_before, self.config.risk, now=now)

        # 7. execution — exits first, then opens
        receipts = self.venue.execute_orders(exits + planned_orders, dry_run=not live, ref_prices=ref_prices)

        # 8. account writeback. Only *real* fills consume the per-hour order
        # budget — dry-run simulations must not block a later live cycle.
        account_after = self.venue.read_account(prior=account_before, ref_prices=ref_prices)
        opened = sum(1 for r in receipts if r.action == OrderAction.OPEN and r.status == "filled")
        account_after.open_orders_this_hour = account_before.open_orders_this_hour + opened

        # fold management fills (breakeven move / max-hold close) back into meta
        position_meta = self._apply_receipts_to_meta(receipts, position_meta)

        result = CycleResult(
            as_of=as_of,
            live=live,
            signal=signal,
            account_before=account_before,
            account_after=account_after,
            candidates=candidates,
            universe_skipped=universe_skipped,
            decision=decision,
            planned_orders=planned_orders,
            exits=exits,
            receipts=receipts,
            notes=notes,
        )

        # 9. delivery — Feishu (best-effort, never raises)
        try:
            self.feishu.notify_cycle(
                signal=signal,
                account=account_after,
                receipts=receipts,
                skipped=universe_skipped + decision.rejected,
            )
        except Exception:  # pragma: no cover - delivery must never break a cycle
            pass

        if persist:
            self._persist(result, position_meta=position_meta, safety_state=safety_state)
        return result


# --------------------------------------------------------------------------- #
# Named entry point from the technical spec.
# --------------------------------------------------------------------------- #
def execute_shibei_v3_onchain_prepared_orders(
    orders: List[PlannedOrder],
    config: Optional[AgentConfig] = None,
    *,
    dry_run: Optional[bool] = None,
    ref_prices: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Consume venue-agnostic ``planned_orders`` and execute them on BNB Chain,
    then write back ``account_state`` — the on-chain counterpart of 拾贝's
    ``execute_shibei_v2_combo_live_prepared_orders``.

    ``dry_run`` defaults to ``not config.live_orders_allowed`` (the multi-gate
    live switch). Returns receipts + reconciled account state. Never signs unless
    the full live gate is open.
    """
    cfg = config or load_config()
    venue = build_venue(cfg)

    effective_dry_run = (not cfg.live_orders_allowed) if dry_run is None else bool(dry_run)
    receipts = venue.execute_orders(orders, dry_run=effective_dry_run, ref_prices=ref_prices)
    account_state = venue.read_account(ref_prices=ref_prices)
    return {
        "venue": cfg.venue,
        "dry_run": effective_dry_run,
        "live_orders_allowed": cfg.live_orders_allowed,
        "blocked_reasons": cfg.blocked_reasons(),
        "receipts": receipts,
        "account_state": account_state,
        "as_of": _utcnow().isoformat(),
    }


def run_once(config: Optional[AgentConfig] = None, *, persist: bool = True) -> CycleResult:
    return OnChainAgent(config).run_cycle(persist=persist)
