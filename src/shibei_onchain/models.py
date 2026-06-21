"""Frozen data contracts shared across every layer of the on-chain agent.

This module is the *spine* of the project. Every other module codes against
these dataclasses; nothing here imports from sibling modules, so it can be
imported with zero third-party dependencies (Python 3.9+ stdlib only).

Layer mapping (see README / ARCHITECTURE):

    Signal layer (CMC Agent Hub) ............ MarketSignal / MarketRegime
    Decision layer (拾贝 brain) ............... Candidate / Side / RiskDecision
    Execution layer (Trust Wallet @ BNB) .... PlannedOrder / ExecutionReceipt
    Account / reconcile ..................... AccountState / Position
    On-chain universe ....................... TokenInfo

The decision-layer ``Candidate`` deliberately mirrors the normalized candidate
produced by 拾贝 V3.0 (``normalize_v2_execution_candidate``): symbol, side,
entry/stop price, strategy_id/leg, risk_per_trade_pct, take_profit / breakeven
R multiples, max_hold_hours, metrics and evidence. That faithfulness is what
lets the *same* brain feed either a Binance perp adapter or this on-chain one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def base_asset_of(symbol: str, quote_assets: Optional[List[str]] = None) -> str:
    """Derive the base asset from a board symbol.

    ``"BNBUSDT" -> "BNB"``, ``"BTCUSDT" -> "BTC"``. Falls back to the symbol
    itself (upper-cased) when no known quote suffix matches.
    """
    sym = (symbol or "").upper()
    for quote in quote_assets or ("USDT", "BUSD", "USDC", "USD"):
        if sym.endswith(quote) and len(sym) > len(quote):
            return sym[: -len(quote)]
    return sym


# --------------------------------------------------------------------------- #
# Signal layer — CoinMarketCap Agent Hub
# --------------------------------------------------------------------------- #
class MarketRegime(str, Enum):
    RISK_ON = "risk_on"
    NEUTRAL = "neutral"
    RISK_OFF = "risk_off"
    UNKNOWN = "unknown"


class BtcTrend(str, Enum):
    UP = "up"
    FLAT = "flat"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass
class MarketSignal:
    """Normalized, decision-grade signal aggregated from CMC Agent Hub.

    Used two ways by the brain: (1) candidate enrichment and (2) as a *hard
    risk gate* (the BTC-environment / risk-flag gate that 拾贝 V3.0 only ever
    logged but never enforced — this project closes that gap)."""

    regime: MarketRegime = MarketRegime.UNKNOWN
    btc_trend: BtcTrend = BtcTrend.UNKNOWN
    btc_price: Optional[float] = None
    fear_greed: Optional[float] = None
    risk_flags: List[str] = field(default_factory=list)
    source: str = "mock"          # mcp | x402 | mock | cache
    as_of: str = ""               # ISO-8601 UTC
    notes: List[str] = field(default_factory=list)
    # per-token enrichment: base_asset -> {liquidity_score, risk_flag, ...}
    token_signals: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def token_signal(self, base_asset: str) -> Dict[str, Any]:
        return self.token_signals.get((base_asset or "").upper(), {})


# --------------------------------------------------------------------------- #
# Decision layer — ranked, venue-agnostic candidate
# --------------------------------------------------------------------------- #
class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def order_side(self) -> str:
        return "BUY" if self is Side.LONG else "SELL"

    @property
    def position_side(self) -> str:
        return "LONG" if self is Side.LONG else "SHORT"

    @property
    def display(self) -> str:
        return "多" if self is Side.LONG else "空"


@dataclass
class Candidate:
    """A ranked, venue-agnostic trade candidate.

    Mirrors 拾贝's normalized execution candidate so the on-chain adapter can
    consume the exact same brain output the Binance adapter does."""

    symbol: str                       # board symbol, e.g. "BNBUSDT"
    side: Side
    price: float                      # entry / current price (USD quote)
    stop_loss_price: float
    strategy_id: str = ""
    strategy_leg: str = ""
    score: float = 0.0                # track score
    relative_strength_score: float = 0.0
    source_boards: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)
    risk_per_trade_pct: float = 0.02
    take_profit_r_multiple: float = 2.5
    breakeven_r_multiple: float = 1.0
    max_hold_hours: float = 0.0
    metrics: Dict[str, Any] = field(default_factory=dict)
    decision_time: str = ""
    signal_key: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def base_asset(self) -> str:
        return base_asset_of(self.symbol)

    @property
    def stop_distance_pct(self) -> float:
        if self.price <= 0:
            return 0.0
        return abs(self.price - self.stop_loss_price) / self.price


@dataclass
class RiskDecision:
    """Result of running the risk stack over a list of candidates."""

    approved: List["Candidate"] = field(default_factory=list)
    rejected: List["SkippedOrder"] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    risk_state: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Execution layer — venue-agnostic order plan + receipts
# --------------------------------------------------------------------------- #
class OrderAction(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    MOVE_STOP = "move_stop"   # relocate the resting stop (e.g. +1R -> breakeven)


@dataclass
class PlannedOrder:
    """Venue-agnostic order plan emitted by the brain, consumed by the
    OnChainExecutionAdapter. This is the de-coupling seam: the brain never
    knows it is trading on PancakeSwap rather than a Binance perp."""

    symbol: str
    side: Side
    action: OrderAction
    base_asset: str
    quote_asset: str = "USDT"
    notional_usd: float = 0.0         # risk-budget-derived notional
    quantity: float = 0.0             # token amount (resolved by adapter if 0)
    entry_price: float = 0.0
    stop_loss_price: float = 0.0
    take_profit_r_multiple: float = 2.5
    breakeven_r_multiple: float = 1.0
    risk_budget_usd: float = 0.0
    risk_per_trade_pct: float = 0.02
    max_slippage_bps: int = 100
    strategy_id: str = ""
    strategy_leg: str = ""
    signal_key: str = ""
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SkippedOrder:
    """A candidate / order rejected at some stage, with a machine-readable
    reason. 拾贝's principle is 'failure must be visible' — nothing is dropped
    silently; everything that is rejected lands here."""

    symbol: str
    side: Side
    reason: str
    stage: str = ""                   # risk | universe_filter | final_guard | execution
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionReceipt:
    symbol: str
    side: Side
    action: OrderAction
    status: str                       # filled | dry_run | failed | skipped
    tx_hash: str = ""
    token_in: str = ""
    token_out: str = ""
    amount_in: float = 0.0
    amount_out: float = 0.0
    min_amount_out: float = 0.0
    price: float = 0.0
    gas_used: int = 0
    error: str = ""
    dry_run: bool = True
    as_of: str = ""
    explorer_url: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Account / reconcile
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    symbol: str
    side: Side
    base_asset: str
    quantity: float                   # token amount held
    entry_price: float
    notional_usd: float
    current_price: float = 0.0
    stop_loss_price: float = 0.0
    take_profit_r_multiple: float = 2.5
    breakeven_r_multiple: float = 1.0
    rr_multiple: float = 0.0
    max_hold_hours: float = 0.0
    opened_at: str = ""
    strategy_id: str = ""
    strategy_leg: str = ""
    tx_hash: str = ""

    def recompute_rr(self) -> float:
        """R-multiple = (move in favour) / (entry→stop risk distance)."""
        risk = abs(self.entry_price - self.stop_loss_price)
        if risk <= 0 or self.current_price <= 0 or self.entry_price <= 0:
            return self.rr_multiple
        if self.side is Side.LONG:
            move = self.current_price - self.entry_price
        else:
            move = self.entry_price - self.current_price
        self.rr_multiple = move / risk
        return self.rr_multiple


@dataclass
class StopLossEvent:
    """A realized stop-loss, used by the same-coin 24h circuit breaker."""

    base_asset: str
    side: Side
    at: str                           # ISO-8601 UTC
    price: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountState:
    equity_usd: float = 0.0           # total wallet value in USD
    quote_balance_usd: float = 0.0    # free USDT
    gas_balance: float = 0.0          # native BNB available for gas
    positions: List[Position] = field(default_factory=list)
    open_orders_this_hour: int = 0
    stop_loss_events: List[StopLossEvent] = field(default_factory=list)
    peak_equity_usd: float = 0.0      # high-water mark (for the drawdown kill-switch)
    day_start_equity_usd: float = 0.0 # equity at start of the UTC day (daily-loss kill-switch)
    as_of: str = ""
    source: str = "mock"              # chain | reconcile | mock
    notes: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def position_for(self, base_asset: str) -> Optional[Position]:
        base = (base_asset or "").upper()
        for pos in self.positions:
            if pos.base_asset.upper() == base:
                return pos
        return None

    @property
    def total_position_notional(self) -> float:
        return sum(max(0.0, p.notional_usd) for p in self.positions)


# --------------------------------------------------------------------------- #
# On-chain universe
# --------------------------------------------------------------------------- #
@dataclass
class TokenInfo:
    symbol: str                       # board symbol e.g. "BNBUSDT"
    base_asset: str                   # "BNB"
    address: str                      # ERC-20 address on BSC (WBNB for native)
    decimals: int = 18
    is_native: bool = False           # native BNB (wrapped to WBNB for swaps)
    min_liquidity_usd: float = 0.0    # reference DEX depth floor
    tradeable: bool = True
    notes: str = ""
