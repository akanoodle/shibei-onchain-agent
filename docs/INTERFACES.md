# Frozen module interfaces (the build contract)

Every module codes against `shibei_onchain.models` and `shibei_onchain.config`
(already written — read them; do not change them). Signatures below are
**frozen**: implement exactly these so the orchestrator and sibling modules
wire up without drift.

Global rules:
- **Python 3.9**. Use `from __future__ import annotations` at the top of every
  file. No `match`, no `X | Y` unions evaluated at runtime (annotations only).
- **No hard third-party deps at import time.** `web3`, `eth_account`, `requests`
  must be imported lazily *inside* the function that needs them, wrapped in
  try/except so the module imports fine without them.
- **Never raise on network/credential failure.** Degrade to mock/cache and set
  a `source`/`status` field. 拾贝 principle: *failures must be visible* — record
  a reason, never crash the cycle.
- **Never log secrets.** Private keys are read but never printed.
- Each module ships a unit test in `tests/` that runs in pure mock mode with
  **no network and no extra deps**.

---

## signals/cmc_agent_hub.py
```python
class CmcAgentHub:
    def __init__(self, config: CmcConfig) -> None: ...
    def fetch_market_signal(self, symbols: Optional[List[str]] = None) -> MarketSignal: ...
```
- mode `mock` (default): deterministic synthetic `MarketSignal` (regime NEUTRAL,
  btc_trend FLAT, btc_price ~ a fixed value, fear_greed ~50, token_signals with a
  liquidity_score per base asset). `source="mock"`.
- mode `mcp`: JSON-RPC `tools/call` over HTTP to `config.mcp_url` with header
  `Authorization: Bearer {api_key}` (lazy `requests`/`urllib`); map response →
  MarketSignal; on any error fall back to mock and append a note.
- mode `x402`: same against `config.x402_url` (no api key; document the $0.01
  USDC/call x402 model in a comment). Fall back to mock on error.
- Always returns a valid MarketSignal; never raises.

## signals/regime_gate.py
```python
def evaluate_regime_gate(signal: MarketSignal, risk: RiskParams) -> Dict[str, Any]: ...
```
Returns `{"long_blocked": bool, "short_blocked": bool, "reasons": List[str], "notes": List[str]}`.
- If `not risk.btc_gate_enabled`: both False, note "btc_gate_disabled".
- `long_blocked` if `signal.regime.value in risk.btc_gate_block_regimes` OR
  `signal.btc_trend.value in risk.btc_gate_block_trends` OR any `signal.risk_flags`
  in `risk.block_risk_flags`. reasons use codes like `regime_risk_off`,
  `btc_trend_down`, `risk_flag:market_halt`.
- `short_blocked` only if a hard flag in `risk.block_risk_flags` is present
  (shorts are favoured when BTC is weak). Note that explicitly.

## brain/scanner.py
```python
class Scanner:
    def __init__(self, config: AgentConfig) -> None: ...
    def scan(self, *, signal: Optional[MarketSignal] = None) -> List[Candidate]: ...
```
Produces ranked LONG `Candidate`s (and SHORT only if `config.enable_short_leg`).
Source precedence:
1. If `{config.state_dir}/latest.json` exists with a board ranking, load + map it.
2. Else if network available, build a momentum/relative-strength ranking from
   Binance **public** klines (read-only public data — `requests`/`urllib`, lazy);
   universe = the on-chain token registry symbols (read `config.onchain.tokens_path`).
3. Else deterministic mock ranking over the registry majors.
Each Candidate: realistic `price`, `stop_loss_price` (~ price*(1 - 0.06) for long),
`score`, `relative_strength_score`, `risk_per_trade_pct` from risk defaults,
`take_profit_r_multiple`/`breakeven_r_multiple` from risk, `strategy_id`/`leg`,
`signal_key`, `source_boards`. Never raises; falls back to mock. Respect
`config.max_candidates`.

## brain/planner.py
```python
def compute_sizing(candidate: Candidate, equity_usd: float, risk: RiskParams) -> Dict[str, Any]: ...
def build_planned_orders(candidates: List[Candidate], account: AccountState, risk: RiskParams) -> List[PlannedOrder]: ...
```
`compute_sizing` returns `{"risk_budget_usd","notional_usd","quantity","stop_distance_pct","capped"}`:
- `risk_budget = equity_usd * candidate.risk_per_trade_pct`
- `sd = candidate.stop_distance_pct`; clamp to `(0, risk.max_stop_distance_pct]`;
  if `sd<=0` → notional 0, capped True.
- `raw = risk_budget / sd`; `max_notional = equity_usd * risk.max_leverage`;
  `notional = min(raw, max_notional)` (capped if clamped); if
  `0 < notional < risk.min_notional_usdt` → `notional = risk.min_notional_usdt`.
- `quantity = notional / candidate.price` (0 if price<=0).
`build_planned_orders`: for each candidate emit a `PlannedOrder(action=OrderAction.OPEN, ...)`
filled from sizing + candidate fields, `quote_asset="USDT"`, `max_slippage_bps=100`.

## brain/risk.py  (THE risk stack — depends on regime_gate + planner.compute_sizing)
```python
def apply_risk_stack(candidates, account, signal, risk, *, now=None) -> RiskDecision: ...
```
Apply in order, each failure → `SkippedOrder(stage="risk", reason=<code>)`:
1. regime gate per side (codes from regime_gate.reasons).
2. invalid: `price<=0` or `stop_loss_price<=0` → `invalid_price`.
3. `stop_distance_pct > risk.max_stop_distance_pct` → `stop_distance_too_wide`.
4. within-batch conflict: a base asset with both a LONG and SHORT candidate →
   reject **both** → `same_symbol_opposite_side_conflict` (long-priority note).
5. opposite existing position (`account.position_for(base)` opposite side) →
   `opposite_position_exists`.
6. same-side existing position (no stacking) → `position_already_open`.
7. circuit breaker: count `account.stop_loss_events` for base within
   `risk.circuit_breaker_window_hours` ≥ `risk.circuit_breaker_stoploss_count`
   → `circuit_breaker_24h`.
8. then portfolio caps, iterating survivors in rank order, accumulating:
   - per-hour: `account.open_orders_this_hour + approved_so_far ≥ risk.max_new_orders_per_hour`
     → `max_orders_per_hour`.
   - total initial risk: existing initial risk (Σ over positions of
     `notional * |entry-stop|/entry`) + Σ approved risk_budget + this risk_budget,
     all / equity > `risk.max_total_initial_risk_pct` → `max_total_initial_risk`.
   - total notional: existing notional + Σ approved notional + this notional >
     `equity * risk.max_total_notional_multiple` → `max_total_notional`.
   Use `compute_sizing` for per-candidate risk_budget/notional.
Return `RiskDecision(approved=[Candidate...], rejected=[SkippedOrder...], notes, risk_state)`.
`now` defaults to `datetime.now(timezone.utc)`.

## onchain/pancake_router.py
```python
class PancakeRouter:
    def __init__(self, onchain: OnChainConfig, web3=None) -> None: ...
    def build_path(self, token_in: str, token_out: str) -> List[str]: ...
    def get_amounts_out(self, amount_in_wei: int, path: List[str]) -> List[int]: ...
    def min_amount_out(self, expected_out_wei: int, slippage_bps: int) -> int: ...
    def quote(self, *, token_in: str, token_out: str, amount_in: float,
              decimals_in: int, decimals_out: int,
              ref_price: Optional[float] = None) -> Dict[str, Any]: ...
```
`quote` returns `{"amount_out","min_amount_out","amount_in_wei","path","price","source"}`
(human-unit amounts + the wei min_amount_out for the swap). `source` is `web3`
or `mock`. web3 path uses `getAmountsOut` via lazy web3; mock path derives
`amount_out` from `ref_price` (token_in USDT → out = amount_in/ref_price, etc.).
`build_path` routes direct, or via WBNB when neither side is WBNB/USDT.
`min_amount_out` = `expected * (10000 - slippage_bps)//10000`.

## onchain/twak_client.py  (Trust Wallet Agent Kit wrapper)
```python
@dataclass
class SwapResult:
    status: str; tx_hash: str = ""; amount_in: float = 0.0; amount_out: float = 0.0
    gas_used: int = 0; error: str = ""; raw: Dict[str, Any] = field(default_factory=dict)

class TwakClient:
    def __init__(self, onchain: OnChainConfig) -> None: ...
    def address(self) -> str: ...
    def native_balance(self) -> float: ...                          # BNB
    def token_balance(self, token_address: str, decimals: int) -> float: ...
    def approve(self, token_address: str, spender: str, amount_wei: int) -> SwapResult: ...
    def swap_exact_tokens_for_tokens(self, *, token_in: str, token_out: str,
        amount_in_wei: int, min_amount_out_wei: int, path: List[str],
        deadline_seconds: int = 600) -> SwapResult: ...
    def health(self) -> Dict[str, Any]: ...
```
Modes (`onchain.twak_mode`): `mock` (default) returns synthetic successes, a
deterministic pseudo tx hash, sane balances (e.g. native 0.5 BNB, USDT from
config), **no network, no key needed**; `web3` signs locally with
`onchain.private_key` via lazy `eth_account` + `web3` (self-custody — key never
leaves the process, never logged); `mcp`/`cli` call the Trust Wallet Agent Kit
endpoint/binary (`onchain.twak_endpoint`) and parse the receipt. On any failure
return `SwapResult(status="failed", error=...)` — never raise. Emphasise the
self-custody narrative in the module docstring.

## onchain/universe_filter.py
```python
class UniverseFilter:
    def __init__(self, onchain: OnChainConfig) -> None: ...        # loads tokens.json for chain_id
    def token_info(self, base_or_symbol: str) -> Optional[TokenInfo]: ...
    def is_tradeable(self, base_or_symbol: str) -> bool: ...
    def router_address(self) -> str: ...
    def usdt(self) -> TokenInfo: ...
    def filter(self, candidates: List[Candidate],
               signal: Optional[MarketSignal] = None) -> Tuple[List[Candidate], List[SkippedOrder]]: ...
    def final_guard(self, order: PlannedOrder, quote: Dict[str, Any]) -> Optional[SkippedOrder]: ...
```
Loads `config/tokens.bsc.json` keyed by `str(onchain.chain_id)`. `filter` is the
post-scoring HARD pre-trade filter: keep only candidates whose `base_asset` is in
the on-chain tradeable set and meets the liquidity floor (registry
`min_liquidity_usd`, optionally tightened by `signal.token_signal(base).liquidity_score`);
others → `SkippedOrder(stage="universe_filter", reason="not_onchain_tradeable" | "insufficient_liquidity")`.
`final_guard` is the execution-time re-check: if `quote["source"]=="web3"` and
realized slippage (`quote price` vs `order.entry_price`) exceeds
`order.max_slippage_bps`, or amount_out is 0 → return a SkippedOrder(stage="final_guard");
else None.

## onchain/reconcile.py  (depends on twak_client + universe_filter)
```python
class Reconciler:
    def __init__(self, config: AgentConfig, twak: TwakClient, universe: UniverseFilter) -> None: ...
    def read_account(self, prior: Optional[AccountState] = None,
                     ref_prices: Optional[Dict[str, float]] = None) -> AccountState: ...
    def detect_exits(self, account: AccountState, risk: RiskParams, *, now=None) -> List[PlannedOrder]: ...
```
`read_account`: read native BNB (gas), USDT (quote balance), and each registry
token balance via `twak`; value tokens at `ref_prices[base]` (or carry prior
position entry price); build `Position`s and `equity_usd`. Carries forward
`prior.stop_loss_events`/`open_orders_this_hour` when given. In mock twak mode
this yields a deterministic account (equity = config.initial_equity_usd in USDT,
no positions) unless `prior` carries positions. Never raises.
`detect_exits`: for each open Position recompute rr; emit `PlannedOrder(action=CLOSE)`
when rr ≥ `take_profit_r_multiple` (TP), price crossed stop (stop-loss), or a
SHORT position exceeded `max_hold_hours`. `reason` set accordingly.

## onchain/execution_adapter.py  (OnChainExecutionAdapter — depends on twak + router + universe)
```python
class OnChainExecutionAdapter:
    def __init__(self, config: AgentConfig, twak: TwakClient,
                 router: PancakeRouter, universe: UniverseFilter) -> None: ...
    @property
    def ready(self) -> bool: ...
    def execute_order(self, order: PlannedOrder, *, dry_run: bool,
                      ref_price: Optional[float] = None) -> ExecutionReceipt: ...
    def execute_orders(self, orders: List[PlannedOrder], *, dry_run: bool,
                       ref_prices: Optional[Dict[str, float]] = None) -> List[ExecutionReceipt]: ...
```
`ready`: universe has the token + router address present. `execute_order`:
1. resolve `TokenInfo` via universe; if missing → receipt status `skipped` reason `not_onchain_tradeable`.
2. SHORT order + `not config.enable_short_leg` → receipt status `skipped` reason `short_leg_disabled` (MVP is long-spot; document the perp stretch).
3. gas floor: `twak.native_balance() < onchain.gas_min_bnb` → receipt `failed` reason `insufficient_gas` (failure visible, not silent).
4. determine token_in/out: OPEN long = USDT→token; CLOSE long = token→USDT.
5. `router.quote(...)`; `universe.final_guard(order, quote)`; if guard trips → receipt `skipped`.
6. if `dry_run` → receipt status `dry_run` with the quote, minAmountOut, intended path (no signing).
7. else live: `twak.approve(token_in, router_addr, amount_in_wei)` (exact), then
   `twak.swap_exact_tokens_for_tokens(...)`; build receipt from SwapResult,
   `explorer_url = onchain.explorer_base + "/tx/" + tx_hash`.
Always returns an `ExecutionReceipt`; never raises.

## delivery/feishu.py
```python
class FeishuNotifier:
    def __init__(self, webhook_url: str = "", secret: str = "", enabled: bool = False) -> None: ...
    @classmethod
    def from_config(cls, config: AgentConfig) -> "FeishuNotifier": ...
    def notify(self, title: str, lines: List[str], *, level: str = "info") -> Dict[str, Any]: ...
    def notify_cycle(self, *, signal, account, receipts, skipped) -> Dict[str, Any]: ...
```
HMAC-SHA256 signed Feishu webhook (lazy `requests`/`urllib`). When disabled or
url empty → return `{"status":"skipped","reason":"disabled_or_no_webhook"}`.
Never raises. `notify_cycle` formats a compact human card: regime, equity, #fills,
#skips, sample tx hashes.
