"""Typed configuration for the on-chain agent.

All knobs are read from environment variables prefixed ``SHIBEI_ONCHAIN_*``
(see ``.env.example``). The defaults reproduce 拾贝 V3.0's risk stack exactly:

    * 2% risk per trade (long & short)
    * 2.5R take-profit, 1.0R move-to-breakeven
    * 20% max stop distance
    * 5x total notional cap, 5x max leverage
    * ≤12% total initial portfolio risk
    * ≤3 new orders per hour
    * same-coin 2-stop-loss-in-24h circuit breaker
    * same-symbol opposite-side double-reject, long-priority on conflict
    * BTC environment gate (the V3.0 'log-only' gap, now enforced)

The on-chain defaults target **BSC testnet** for safety; switch to mainnet
only via explicit env. Secrets (private key) are read but never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple


REQUIRED_CONFIRM_TEXT = "I_UNDERSTAND_SHIBEI_ONCHAIN_LIVE_RISK"


# --------------------------------------------------------------------------- #
# env coercion helpers
# --------------------------------------------------------------------------- #
def _env(source: Mapping[str, str], key: str, default: str = "") -> str:
    val = source.get(key)
    return default if val is None else str(val).strip()


def _f(source: Mapping[str, str], key: str, default: float) -> float:
    try:
        raw = source.get(key)
        if raw is None or str(raw).strip() == "":
            return default
        return float(raw)
    except (TypeError, ValueError):
        return default


def _i(source: Mapping[str, str], key: str, default: int) -> int:
    try:
        raw = source.get(key)
        if raw is None or str(raw).strip() == "":
            return default
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _b(source: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = source.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")


def _int_tuple(source: Mapping[str, str], key: str, default: Tuple[int, ...]) -> Tuple[int, ...]:
    """Parse a CSV of ints (e.g. ``"8,9,10,11,12,13,14,15"``). An *explicit empty*
    string disables the filter (returns ``()``); an absent key keeps ``default``.
    Out-of-range / non-int tokens are dropped silently. Never raises."""
    raw = source.get(key)
    if raw is None:
        return default
    text = str(raw).strip()
    if text == "":
        return ()
    out = []
    for tok in text.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            h = int(float(tok))
        except (TypeError, ValueError):
            continue
        if 0 <= h <= 23:
            out.append(h)
    return tuple(sorted(set(out)))


# --------------------------------------------------------------------------- #
# risk parameters (faithful to 拾贝 V3.0)
# --------------------------------------------------------------------------- #
@dataclass
class RiskParams:
    long_risk_pct: float = 0.02
    short_risk_pct: float = 0.02
    take_profit_r: float = 2.5
    long_breakeven_r: float = 1.0
    initial_stop_distance_pct: float = 0.06      # initial stop placement (entry ± 6%)
    # gentle max-hold backstop for the long leg (hours). 0 = off (V3.0). 汲水 V0.2
    # has no time stop, so this is a safety net (not the strategy stop): cap how
    # long a position can sit open, set loose enough to let winners run to 2.5R.
    long_max_hold_hours: float = 0.0
    max_stop_distance_pct: float = 0.20
    max_total_notional_multiple: float = 5.0     # ≤5x equity in open notional
    max_total_initial_risk_pct: float = 0.12     # ≤12% total initial risk budget
    # account-level kill-switches (the P0 gap 拾贝 V3.0 lacked): halt ALL new opens
    # when the live account bleeds past these. 0 = off. Existing positions keep
    # being managed (they can still exit) — only opening is frozen.
    max_account_drawdown_pct: float = 0.0        # halt if equity <= peak*(1-x)
    max_daily_loss_pct: float = 0.0              # halt if equity <= day_start*(1-x)
    max_new_orders_per_hour: int = 3             # ≤3 new orders / hour
    max_leverage: int = 5
    min_notional_usdt: float = 10.0
    fee_rate: float = 0.0005
    # same-coin circuit breaker: N realized stop-losses within window => block
    circuit_breaker_stoploss_count: int = 2
    circuit_breaker_window_hours: float = 24.0
    s16a_v0_max_hold_hours: float = 16.0
    s16a_v1_max_hold_hours: float = 24.0
    same_symbol_opposite_side_blocked: bool = True
    conflict_priority: str = "long"              # long-priority on same-ts conflict
    # BTC environment gate (decision-grade): block opens when regime is in set
    btc_gate_enabled: bool = True
    btc_gate_block_regimes: Tuple[str, ...] = ("risk_off",)
    btc_gate_block_trends: Tuple[str, ...] = ("down",)
    # block opens that carry these CMC risk flags
    block_risk_flags: Tuple[str, ...] = ("market_halt", "extreme_volatility")
    # 汲水 V0.2 time-of-day filter: no NEW opens during these Beijing-time (UTC+8)
    # hours. Empty tuple = filter off (V3.0 default); V0.2 uses 08..15 inclusive.
    excluded_entry_beijing_hours: Tuple[int, ...] = ()

    @classmethod
    def from_env(cls, source: Mapping[str, str], *, strategy: str = "v3") -> "RiskParams":
        p = "SHIBEI_ONCHAIN_"
        # 汲水 V0.2 bakes in the 北京时 08:00–15:59 no-entry window; V3.0 has none.
        default_excluded = tuple(range(8, 16)) if strategy == "water_v02" else ()
        # V0.2 long leg: a loose 72h safety backstop by default (off for V3.0).
        default_max_hold = 72.0 if strategy == "water_v02" else 0.0
        # Live-safety kill-switches default ON for V0.2 (it is a negative-expectancy
        # leg, so an unattended live run MUST be able to stop bleeding). Off for V3.0.
        default_dd = 0.25 if strategy == "water_v02" else 0.0
        default_daily = 0.10 if strategy == "water_v02" else 0.0
        return cls(
            long_risk_pct=_f(source, p + "LONG_RISK_PCT", _f(source, p + "RISK_PER_TRADE_PCT", 0.02)),
            short_risk_pct=_f(source, p + "SHORT_RISK_PCT", _f(source, p + "RISK_PER_TRADE_PCT", 0.02)),
            take_profit_r=_f(source, p + "TAKE_PROFIT_R", 2.5),
            long_breakeven_r=_f(source, p + "LONG_BREAKEVEN_R", 1.0),
            initial_stop_distance_pct=_f(source, p + "INITIAL_STOP_DISTANCE_PCT", 0.06),
            long_max_hold_hours=_f(source, p + "LONG_MAX_HOLD_HOURS", default_max_hold),
            max_stop_distance_pct=_f(source, p + "MAX_STOP_DISTANCE_PCT", 0.20),
            max_total_notional_multiple=_f(source, p + "MAX_TOTAL_NOTIONAL_MULTIPLE", 5.0),
            max_total_initial_risk_pct=_f(source, p + "MAX_TOTAL_INITIAL_RISK_PCT", 0.12),
            max_account_drawdown_pct=_f(source, p + "MAX_ACCOUNT_DRAWDOWN_PCT", default_dd),
            max_daily_loss_pct=_f(source, p + "MAX_DAILY_LOSS_PCT", default_daily),
            max_new_orders_per_hour=_i(source, p + "MAX_NEW_ORDERS_PER_HOUR", 3),
            max_leverage=max(1, min(5, _i(source, p + "MAX_LEVERAGE", 5))),
            min_notional_usdt=_f(source, p + "MIN_NOTIONAL_USDT", 10.0),
            fee_rate=_f(source, p + "FEE_RATE", 0.0005),
            circuit_breaker_stoploss_count=_i(source, p + "CIRCUIT_BREAKER_STOPLOSS_COUNT", 2),
            circuit_breaker_window_hours=_f(source, p + "CIRCUIT_BREAKER_WINDOW_HOURS", 24.0),
            s16a_v0_max_hold_hours=_f(source, p + "S16A_V0_MAX_HOLD_HOURS", 16.0),
            s16a_v1_max_hold_hours=_f(source, p + "S16A_V1_MAX_HOLD_HOURS", 24.0),
            same_symbol_opposite_side_blocked=_b(source, p + "SAME_SYMBOL_OPPOSITE_SIDE_BLOCKED", True),
            btc_gate_enabled=_b(source, p + "BTC_GATE_ENABLED", True),
            excluded_entry_beijing_hours=_int_tuple(
                source, p + "EXCLUDED_ENTRY_BEIJING_HOURS", default_excluded
            ),
        )


# --------------------------------------------------------------------------- #
# on-chain (Trust Wallet Agent Kit @ BNB Chain) config
# --------------------------------------------------------------------------- #
@dataclass
class OnChainConfig:
    chain: str = "bsc-testnet"
    chain_id: int = 97
    rpc_url: str = "https://data-seed-prebsc-1-s1.bnbchain.org:8545/"
    pancake_router: str = "0xD99D1c33F9fC3444f8101754aBC46c52416550D1"   # PancakeSwap testnet router
    wbnb_address: str = "0xae13d989daC2f0dEbFf460aC112a837C89BAa7cd"     # WBNB testnet
    usdt_address: str = "0x337610d27c682E347C9cD60BD4b3b107C9d34dDd"     # test stable (verify per faucet)
    wallet_address: str = ""
    private_key: str = ""             # loaded if present; NEVER logged
    max_slippage_bps: int = 100       # 1.00%
    gas_min_bnb: float = 0.01         # refuse to act below this gas floor
    approve_exact: bool = True        # exact-amount approvals, no infinite allowance
    twak_mode: str = "mock"           # mcp | cli | web3 | mock
    twak_endpoint: str = ""           # MCP url or CLI binary path
    twak_timeout_seconds: float = 60.0
    explorer_base: str = "https://testnet.bscscan.com"
    tokens_path: str = "config/tokens.bsc.json"
    quote_asset: str = "USDT"

    @classmethod
    def from_env(cls, source: Mapping[str, str]) -> "OnChainConfig":
        p = "SHIBEI_ONCHAIN_"
        d = cls()
        return cls(
            chain=_env(source, p + "CHAIN", d.chain),
            chain_id=_i(source, p + "CHAIN_ID", d.chain_id),
            rpc_url=_env(source, p + "RPC_URL", d.rpc_url),
            pancake_router=_env(source, p + "PANCAKE_ROUTER", d.pancake_router),
            wbnb_address=_env(source, p + "WBNB_ADDRESS", d.wbnb_address),
            usdt_address=_env(source, p + "USDT_ADDRESS", d.usdt_address),
            wallet_address=_env(source, p + "WALLET_ADDRESS", d.wallet_address),
            private_key=_env(source, p + "PRIVATE_KEY", d.private_key),
            max_slippage_bps=_i(source, p + "MAX_SLIPPAGE_BPS", d.max_slippage_bps),
            gas_min_bnb=_f(source, p + "GAS_MIN_BNB", d.gas_min_bnb),
            approve_exact=_b(source, p + "APPROVE_EXACT", d.approve_exact),
            twak_mode=_env(source, p + "TWAK_MODE", d.twak_mode).lower(),
            twak_endpoint=_env(source, p + "TWAK_ENDPOINT", d.twak_endpoint),
            twak_timeout_seconds=_f(source, p + "TWAK_TIMEOUT_SECONDS", d.twak_timeout_seconds),
            explorer_base=_env(source, p + "EXPLORER_BASE", d.explorer_base),
            tokens_path=_env(source, p + "TOKENS_PATH", d.tokens_path),
            quote_asset=_env(source, p + "QUOTE_ASSET", d.quote_asset),
        )


# --------------------------------------------------------------------------- #
# CoinMarketCap Agent Hub config
# --------------------------------------------------------------------------- #
@dataclass
class CmcConfig:
    mode: str = "mock"                # mcp | x402 | mock
    mcp_url: str = "https://mcp.coinmarketcap.com/mcp"
    x402_url: str = "https://mcp.coinmarketcap.com/x402/mcp"
    api_key: str = ""
    timeout_seconds: float = 15.0
    cache_ttl_seconds: float = 300.0

    @classmethod
    def from_env(cls, source: Mapping[str, str]) -> "CmcConfig":
        p = "SHIBEI_ONCHAIN_CMC_"
        d = cls()
        return cls(
            mode=_env(source, p + "MODE", d.mode).lower(),
            mcp_url=_env(source, p + "MCP_URL", d.mcp_url),
            x402_url=_env(source, p + "X402_URL", d.x402_url),
            api_key=_env(source, p + "API_KEY", d.api_key),
            timeout_seconds=_f(source, p + "TIMEOUT_SECONDS", d.timeout_seconds),
            cache_ttl_seconds=_f(source, p + "CACHE_TTL_SECONDS", d.cache_ttl_seconds),
        )


# --------------------------------------------------------------------------- #
# Aster perp DEX config (the long+short execution venue)
# --------------------------------------------------------------------------- #
@dataclass
class AsterConfig:
    """Aster Finance perpetual-futures venue (BNB-ecosystem, YZi Labs backed).

    Aster's Futures API is Binance-USDⓈ-M-shaped (``/fapi/v3/*``). The recommended
    auth is **v3 EIP-712**: a registered *agent / signer* wallet signs each
    request (``encode_structured_data`` over the urlencoded params as ``msg``,
    domain ``AsterSignTransaction`` / chainId 1666), so the user's main wallet
    only ever approves the agent + deposits collateral — pure self-custody, which
    is exactly Trust Wallet Agent Kit's model. The signer key defaults to the
    main wallet key when unset."""

    mode: str = "mock"                 # mock | api
    base_url: str = "https://fapi.asterdex-testnet.com"   # testnet default; prod: https://fapi.asterdex.com
    user_address: str = ""             # main account wallet (defaults to onchain.wallet_address)
    signer_address: str = ""           # API/agent wallet address (defaults to user)
    signer_private_key: str = ""       # API/agent wallet key (defaults to onchain.private_key); NEVER logged
    default_leverage: int = 3
    margin_asset: str = "USDT"
    recv_window_ms: int = 5000
    timeout_seconds: float = 20.0
    # EIP-712 domain chainId: 714 on Aster TESTNET, 1666 on MAINNET. Verified
    # against the live API — a mismatch yields "Signature check failed".
    eip712_chain_id: int = 714
    explorer_base: str = "https://testnet.asterdex.com"

    @classmethod
    def from_env(cls, source: Mapping[str, str]) -> "AsterConfig":
        p = "SHIBEI_ONCHAIN_ASTER_"
        d = cls()
        base_url = _env(source, p + "BASE_URL", d.base_url)
        # auto-pick the EIP-712 chainId from the network unless explicitly set.
        default_chain = 714 if "testnet" in base_url.lower() else 1666
        return cls(
            mode=_env(source, p + "MODE", d.mode).lower(),
            base_url=base_url,
            user_address=_env(source, p + "USER_ADDRESS", d.user_address),
            signer_address=_env(source, p + "SIGNER_ADDRESS", d.signer_address),
            signer_private_key=_env(source, p + "SIGNER_PRIVATE_KEY", d.signer_private_key),
            default_leverage=max(1, _i(source, p + "DEFAULT_LEVERAGE", d.default_leverage)),
            margin_asset=_env(source, p + "MARGIN_ASSET", d.margin_asset),
            recv_window_ms=_i(source, p + "RECV_WINDOW_MS", d.recv_window_ms),
            timeout_seconds=_f(source, p + "TIMEOUT_SECONDS", d.timeout_seconds),
            eip712_chain_id=_i(source, p + "EIP712_CHAIN_ID", default_chain),
            explorer_base=_env(source, p + "EXPLORER_BASE", d.explorer_base),
        )


# --------------------------------------------------------------------------- #
# 拾贝 leaderboard (榜单) source — the real ranking feed
# --------------------------------------------------------------------------- #
@dataclass
class BoardConfig:
    """The live 拾贝 ranking feed (main_track / fast_track / strong_supplement).

    ``mode=off`` (default) keeps the scanner on its self-contained fallback
    (latest.json -> Binance klines -> mock). ``mode=api`` pulls the live board
    over HTTP. Prod has cookie-session auth on, and ``/api/public/v1/*`` is
    auth-exempt only from localhost — so off-box a username/password login is
    needed (creds live in env, never in the repo)."""

    mode: str = "off"             # off | api
    base_url: str = "http://154.37.219.63:8766"
    leaderboard_path: str = "/api/public/v1/leaderboards/latest"
    login_path: str = "/api/auth/login"
    username: str = ""
    password: str = ""            # NEVER logged
    timeframe: str = "live"       # live | 15m
    limit: int = 50
    tracks: Tuple[str, ...] = ("main_track", "fast_track", "strong_supplement")
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls, source: Mapping[str, str]) -> "BoardConfig":
        p = "SHIBEI_ONCHAIN_BOARD_"
        d = cls()
        tracks_raw = _env(source, p + "TRACKS", "")
        tracks = tuple(t.strip() for t in tracks_raw.split(",") if t.strip()) or d.tracks
        return cls(
            mode=_env(source, p + "MODE", d.mode).lower(),
            base_url=_env(source, p + "BASE_URL", d.base_url).rstrip("/"),
            leaderboard_path=_env(source, p + "LEADERBOARD_PATH", d.leaderboard_path),
            login_path=_env(source, p + "LOGIN_PATH", d.login_path),
            username=_env(source, p + "USERNAME", d.username),
            password=_env(source, p + "PASSWORD", d.password),
            timeframe=_env(source, p + "TIMEFRAME", d.timeframe).lower(),
            limit=_i(source, p + "LIMIT", d.limit),
            tracks=tracks,
            timeout_seconds=_f(source, p + "TIMEOUT_SECONDS", d.timeout_seconds),
        )


# --------------------------------------------------------------------------- #
# top-level agent config
# --------------------------------------------------------------------------- #
@dataclass
class AgentConfig:
    dry_run: bool = True
    trading_enabled: bool = False
    confirm_text: str = ""
    required_confirm_text: str = REQUIRED_CONFIRM_TEXT
    execution_adapter_ready: bool = False
    state_dir: str = "data/state"
    initial_equity_usd: float = 1000.0
    max_candidates: int = 8
    strategy: str = "v3"               # v3 (long+short) | water_v02 (汲水 long-only)
    enable_short_leg: bool = False     # on-chain short (native on Aster perps)
    venue: str = "pancake"             # pancake (spot) | aster (perp, both legs)
    risk: RiskParams = field(default_factory=RiskParams)
    onchain: OnChainConfig = field(default_factory=OnChainConfig)
    cmc: CmcConfig = field(default_factory=CmcConfig)
    aster: AsterConfig = field(default_factory=AsterConfig)
    board: BoardConfig = field(default_factory=BoardConfig)
    feishu_enabled: bool = False
    feishu_webhook_url: str = ""
    feishu_webhook_secret: str = ""

    @property
    def credentials_present(self) -> bool:
        return bool(self.onchain.wallet_address) and bool(self.onchain.private_key)

    @property
    def live_orders_allowed(self) -> bool:
        """Faithful to 拾贝's multi-condition live gate: every condition must
        hold before a real on-chain swap can be submitted. Default config keeps
        this False (dry-run, no confirm, adapter not armed)."""
        return (
            self.trading_enabled
            and not self.dry_run
            and self.confirm_text == self.required_confirm_text
            and self.execution_adapter_ready
            and self.credentials_present
        )

    def blocked_reasons(self) -> list:
        reasons = []
        if not self.trading_enabled:
            reasons.append("trading_switch_not_enabled")
        if self.dry_run:
            reasons.append("dry_run_enabled")
        if self.confirm_text != self.required_confirm_text:
            reasons.append("confirm_text_missing")
        if not self.execution_adapter_ready:
            reasons.append("execution_adapter_not_ready")
        if not self.onchain.wallet_address:
            reasons.append("wallet_address_missing")
        if not self.onchain.private_key:
            reasons.append("private_key_missing")
        return reasons

    @classmethod
    def from_env(cls, source: Optional[Mapping[str, str]] = None) -> "AgentConfig":
        src = source if source is not None else os.environ
        p = "SHIBEI_ONCHAIN_"
        onchain = OnChainConfig.from_env(src)
        aster = AsterConfig.from_env(src)
        # Aster agent defaults to the main self-custody wallet when its own
        # signer/user fields are not separately configured.
        if not aster.user_address:
            aster.user_address = onchain.wallet_address
        if not aster.signer_address:
            aster.signer_address = aster.user_address
        if not aster.signer_private_key:
            aster.signer_private_key = onchain.private_key
        venue = _env(src, p + "VENUE", "pancake").lower()
        strategy = _env(src, p + "STRATEGY", "v3").lower()
        # 汲水 V0.2 is a LONG-ONLY strategy: default the short leg OFF regardless of
        # venue. V3.0 keeps the native Aster short on (unless explicitly overridden).
        short_default = False if strategy == "water_v02" else (venue == "aster")
        return cls(
            dry_run=_b(src, p + "DRY_RUN", True),
            trading_enabled=_b(src, p + "TRADING_ENABLED", False),
            confirm_text=_env(src, p + "CONFIRM_TEXT", ""),
            execution_adapter_ready=_b(src, p + "EXECUTION_ADAPTER_READY", False),
            state_dir=_env(src, p + "STATE_DIR", "data/state"),
            initial_equity_usd=_f(src, p + "INITIAL_EQUITY_USD", 1000.0),
            max_candidates=_i(src, p + "MAX_CANDIDATES", 8),
            strategy=strategy,
            enable_short_leg=_b(src, p + "ENABLE_SHORT_LEG", short_default),
            venue=venue,
            risk=RiskParams.from_env(src, strategy=strategy),
            onchain=onchain,
            cmc=CmcConfig.from_env(src),
            aster=aster,
            board=BoardConfig.from_env(src),
            feishu_enabled=_b(src, p + "FEISHU_ENABLED", False),
            feishu_webhook_url=_env(src, p + "FEISHU_WEBHOOK_URL", ""),
            feishu_webhook_secret=_env(src, p + "FEISHU_WEBHOOK_SECRET", ""),
        )

    def redacted(self) -> Dict[str, object]:
        """Config snapshot safe to log / show in a dashboard (no secrets)."""
        return {
            "dry_run": self.dry_run,
            "trading_enabled": self.trading_enabled,
            "execution_adapter_ready": self.execution_adapter_ready,
            "live_orders_allowed": self.live_orders_allowed,
            "blocked_reasons": self.blocked_reasons(),
            "strategy": self.strategy,
            "venue": self.venue,
            "chain": self.onchain.chain,
            "chain_id": self.onchain.chain_id,
            "twak_mode": self.onchain.twak_mode,
            "aster_mode": self.aster.mode,
            "aster_base_url": self.aster.base_url,
            "aster_signer": self.aster.signer_address or "(defaults to wallet)",
            "aster_signer_key": "***set***" if self.aster.signer_private_key else "(unset)",
            "cmc_mode": self.cmc.mode,
            "board_mode": self.board.mode,
            "board_url": self.board.base_url + self.board.leaderboard_path,
            "board_auth": ("user:" + self.board.username) if self.board.username else "(none)",
            "board_password": "***set***" if self.board.password else "(unset)",
            "wallet_address": self.onchain.wallet_address or "(unset)",
            "private_key": "***set***" if self.onchain.private_key else "(unset)",
            "enable_short_leg": self.enable_short_leg,
            "risk": {
                "long_risk_pct": self.risk.long_risk_pct,
                "short_risk_pct": self.risk.short_risk_pct,
                "take_profit_r": self.risk.take_profit_r,
                "max_total_notional_multiple": self.risk.max_total_notional_multiple,
                "max_total_initial_risk_pct": self.risk.max_total_initial_risk_pct,
                "max_new_orders_per_hour": self.risk.max_new_orders_per_hour,
                "btc_gate_enabled": self.risk.btc_gate_enabled,
                "excluded_entry_beijing_hours": list(self.risk.excluded_entry_beijing_hours),
                "long_breakeven_r": self.risk.long_breakeven_r,
                "long_max_hold_hours": self.risk.long_max_hold_hours,
                "initial_stop_distance_pct": self.risk.initial_stop_distance_pct,
                "max_account_drawdown_pct": self.risk.max_account_drawdown_pct,
                "max_daily_loss_pct": self.risk.max_daily_loss_pct,
            },
        }


def _read_dotenv(path: str) -> Dict[str, str]:
    """Parse a ``KEY=VALUE`` ``.env`` file (stdlib only, no dependency).

    Ignores blank lines and ``#`` comments, strips matching surrounding quotes.
    Returns ``{}`` if the file is absent or unreadable — never raises.
    """
    env: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if key:
                    env[key] = value
    except OSError:
        return {}
    return env


def load_config(source: Optional[Mapping[str, str]] = None) -> AgentConfig:
    """Build config from the environment.

    When ``source`` is given (e.g. in tests) it is used verbatim — hermetic, no
    file IO. Otherwise a ``.env`` file (path overridable via
    ``SHIBEI_ONCHAIN_ENV_FILE``) is overlaid *under* the real process
    environment, so an explicitly exported variable always wins over the file.
    """
    if source is not None:
        return AgentConfig.from_env(source)
    env_file = os.environ.get("SHIBEI_ONCHAIN_ENV_FILE", ".env")
    merged: Dict[str, str] = dict(_read_dotenv(env_file))
    merged.update(os.environ)  # real env overrides the .env file
    return AgentConfig.from_env(merged)
