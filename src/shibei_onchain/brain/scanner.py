"""brain/scanner.py — the candidate scanner (拾贝 V3.0 ranking front-end).

The Scanner produces a ranked list of venue-agnostic :class:`Candidate`s for the
decision layer. It is deliberately *source-tolerant*: the same ranked output is
produced whether we read a cached board snapshot, pull live public market data,
or fall back to a deterministic offline mock. Downstream (planner / risk /
universe filter / execution) never knows or cares where the ranking came from.

Source precedence (first that succeeds wins):

    0. live 拾贝 board API (the real ranking) — authoritative when it fetches.
    1. ``{config.state_dir}/latest.json`` — a previously persisted board ranking
       (e.g. produced by a 拾贝 cycle). Loaded and mapped to Candidates.
    2. **CoinMarketCap** quotes — the CMC Agent Hub's ``get_crypto_quotes_latest``
       (price + 24h change for the majors). A momentum / relative-strength
       ranking is built over the on-chain token universe. This keeps the
       market-data feed on **CMC**, never a centralized-exchange API — aligned
       with the BNB-Hack's CMC-first data model.
    3. Deterministic mock ranking over the registry majors. Zero deps, no
       network — this is what the unit test exercises.

LONG candidates are always produced; SHORT candidates are only produced when
``config.enable_short_leg`` is set (the on-chain perp short is a stretch leg).

拾贝 principle — *failures must be visible*: the scanner never raises. On any
load / network error it degrades to the next source and records the effective
source on each candidate (``metrics["source"]`` and ``source_boards``).
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shibei_onchain.config import AgentConfig
from shibei_onchain.models import (
    Candidate,
    Side,
    base_asset_of,
)

# strategy identity (mirrors 拾贝 V3.0 normalized long candidate)
STRATEGY_ID = "shibei_v3_long_bc52f_candidate_a"
LONG_LEG = "shibei_onchain_long"
SHORT_LEG = "shibei_onchain_short"

# stop placement: long stop ~6% below entry; short stop ~6% above entry.
_STOP_DISTANCE_PCT = 0.06

# (no centralized-exchange API: the live fallback uses CMC quotes — see
# Scanner._load_cmc_rows. The BNB-Hack expects CMC as the data source.)

# deterministic reference prices for the offline mock path (USD quote).
_MOCK_PRICES: Dict[str, float] = {
    "BTC": 65000.0,
    "ETH": 3000.0,
    "BNB": 600.0,
    "XRP": 0.52,
    "ADA": 0.45,
    "DOGE": 0.16,
    "CAKE": 2.5,
    "USDC": 1.0,
    "USDT": 1.0,
}

class Scanner:
    """Builds a ranked list of LONG (and optionally SHORT) trade candidates."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.risk = config.risk
        self.onchain = config.onchain
        # populated by scan(): the source that actually produced the ranking.
        self.last_source: str = ""
        # strategy identity — 汲水 V0.2 long-only vs V3.0 long+short.
        if str(getattr(config, "strategy", "v3")).lower() == "water_v02":
            self.strategy_id = "shibei_water_v02_long"
            self.long_leg = "shibei_water_v02_long"
        else:
            self.strategy_id = STRATEGY_ID
            self.long_leg = LONG_LEG

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def scan(self, *, signal: Optional[Any] = None) -> List[Candidate]:
        """Return ranked LONG (and SHORT if enabled) candidates.

        Source precedence: 拾贝 board -> ``latest.json`` -> CMC quotes -> mock.
        Never raises; always returns a list (possibly empty if the registry is
        empty). ``signal`` (a :class:`MarketSignal`) is used only for
        enrichment hints and never changes whether the method succeeds.
        """
        universe = self._load_universe()

        rows: List[Dict[str, Any]] = []
        source = ""

        # 0) live 拾贝 board API (the real ranking) ----------------------
        # When the board FETCHES (non-None), it is authoritative: we do NOT fall
        # back to klines/mock, because that would fabricate a different ranking.
        # If 拾贝's hot names aren't on-chain tradeable, the agent trades nothing
        # (honest) rather than inventing a signal. Fallback only on fetch failure.
        board = self._load_board_rows(universe)
        if board is not None:
            rows, source = board, "shibei_board_api"
        else:
            # 1) cached board ranking (latest.json) ---------------------
            try:
                cached = self._load_latest_rows(universe)
            except Exception:  # pragma: no cover - defensive; loader already guards
                cached = None
            if cached:
                rows, source = cached, "latest_json"

            # 2) live CoinMarketCap quotes (CMC-sourced; never a CEX API) -
            if not rows:
                live = self._load_cmc_rows(universe)
                if live:
                    rows, source = live, "cmc_quotes"

            # 3) deterministic mock -------------------------------------
            if not rows:
                rows, source = self._mock_rows(universe), "mock"

        self.last_source = source
        candidates = self._rows_to_candidates(rows, source=source, signal=signal)

        # respect the configured candidate cap — but NOT for the board source,
        # whose full ranked list flows to the venue filter + risk stack (so a
        # tradeable name ranked below the cap is not cut before venue filtering).
        cap = self.config.max_candidates
        if source != "shibei_board_api" and cap is not None and cap >= 0:
            candidates = candidates[:cap]
        return candidates

    # ------------------------------------------------------------------ #
    # universe loading
    # ------------------------------------------------------------------ #
    def _load_universe(self) -> List[Dict[str, Any]]:
        """Read the on-chain token registry for this chain_id.

        Returns a list of ``{"symbol","base_asset","tradeable",...}`` dicts in
        registry order. On any error returns the built-in majors so the scanner
        always has *something* to rank. Never raises.
        """
        path = self.onchain.tokens_path
        chain_key = str(self.onchain.chain_id)
        try:
            resolved = self._resolve_tokens_path(path)
            if resolved and os.path.isfile(resolved):
                with open(resolved, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                networks = data.get("networks", {}) if isinstance(data, dict) else {}
                net = networks.get(chain_key) or {}
                tokens = net.get("tokens") or []
                out: List[Dict[str, Any]] = []
                for tok in tokens:
                    if not isinstance(tok, dict):
                        continue
                    sym = str(tok.get("symbol") or "").upper()
                    if not sym:
                        continue
                    out.append(
                        {
                            "symbol": sym,
                            "base_asset": str(
                                tok.get("base_asset") or base_asset_of(sym)
                            ).upper(),
                            "tradeable": bool(tok.get("tradeable", True)),
                            "min_liquidity_usd": float(
                                tok.get("min_liquidity_usd") or 0.0
                            ),
                        }
                    )
                if out:
                    return out
        except Exception:
            # fall through to the built-in majors on any parse / IO failure.
            pass
        return self._default_universe()

    def _resolve_tokens_path(self, path: str) -> str:
        """Resolve a (possibly relative) tokens_path against likely roots."""
        if not path:
            return ""
        if os.path.isabs(path):
            return path
        candidates = [path]
        # project root is two levels up from this file (src/shibei_onchain/brain).
        here = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
        candidates.append(os.path.join(project_root, path))
        candidates.append(os.path.join(os.getcwd(), path))
        for cand in candidates:
            if cand and os.path.isfile(cand):
                return cand
        return path

    @staticmethod
    def _default_universe() -> List[Dict[str, Any]]:
        majors = [
            ("BNBUSDT", "BNB"),
            ("ETHUSDT", "ETH"),
            ("BTCUSDT", "BTC"),
            ("CAKEUSDT", "CAKE"),
        ]
        return [
            {
                "symbol": sym,
                "base_asset": base,
                "tradeable": True,
                "min_liquidity_usd": 0.0,
            }
            for sym, base in majors
        ]

    # ------------------------------------------------------------------ #
    # source 1: cached board ranking (latest.json)
    # ------------------------------------------------------------------ #
    def _load_latest_rows(
        self, universe: List[Dict[str, Any]]
    ) -> Optional[List[Dict[str, Any]]]:
        """Load + map a persisted board ranking from latest.json.

        Accepts a flexible schema: either a top-level list, or a dict with a
        ``"candidates"`` / ``"boards"`` / ``"ranking"`` list. Each entry needs at
        least a ``symbol``; ``price``/``score`` are used when present. Only
        symbols present in the on-chain universe are kept. Returns ``None`` when
        no usable file exists. Never raises.
        """
        state_dir = self.config.state_dir or ""
        path = os.path.join(state_dir, "latest.json")
        if not os.path.isfile(path):
            # also try project-root-relative when state_dir is relative.
            here = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
            alt = os.path.join(project_root, state_dir, "latest.json")
            if os.path.isfile(alt):
                path = alt
            else:
                return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return None

        raw_rows = self._extract_ranking(data)
        if not raw_rows:
            return None
        return self._map_universe_rows(raw_rows, universe)

    def _map_universe_rows(
        self, raw_rows: List[Any], universe: List[Dict[str, Any]]
    ) -> Optional[List[Dict[str, Any]]]:
        """Map raw ranking rows (from latest.json OR the 拾贝 board API) onto the
        on-chain tradeable universe. Keeps only symbols in the registry; reads
        symbol/side/price/score/relative_strength_score (+ optional source_boards)
        from each row top level. Returns ``None`` when nothing maps."""
        allowed = {u["base_asset"]: u for u in universe}
        allowed_syms = {u["symbol"]: u for u in universe}

        rows: List[Dict[str, Any]] = []
        for idx, item in enumerate(raw_rows):
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or item.get("board") or "").upper()
            if not sym:
                continue
            base = str(item.get("base_asset") or base_asset_of(sym)).upper()
            uinfo = allowed_syms.get(sym) or allowed.get(base)
            if uinfo is None:
                continue  # not in on-chain universe → cannot trade it
            side = self._coerce_side(item.get("side"))
            if side is Side.SHORT and not self.config.enable_short_leg:
                continue
            price = self._coerce_float(item.get("price"))
            if price is None or price <= 0:
                price = _MOCK_PRICES.get(base, 0.0)
            score = self._coerce_float(item.get("score"))
            rs = self._coerce_float(item.get("relative_strength_score"))
            mapped = {
                "symbol": uinfo["symbol"],
                "base_asset": base,
                "side": side,
                "price": float(price),
                "score": float(score) if score is not None else float(len(raw_rows) - idx),
                "relative_strength_score": float(rs) if rs is not None else 0.0,
            }
            boards = item.get("source_boards")
            if isinstance(boards, list) and boards:
                mapped["source_boards"] = [str(b) for b in boards]
            rows.append(mapped)
        return rows or None

    def _load_board_rows(
        self, universe: List[Dict[str, Any]]
    ) -> Optional[List[Dict[str, Any]]]:
        """Pull the live 拾贝 board over HTTP and map it onto the universe.
        Active only when ``config.board.mode == 'api'``. Never raises."""
        if getattr(self.config, "board", None) is None or self.config.board.mode != "api":
            return None
        try:
            from shibei_onchain.signals.shibei_board import ShibeiBoardClient

            raw_rows = ShibeiBoardClient(self.config.board).fetch_rows()
        except Exception:  # noqa: BLE001 - defensive; client already guards
            return None
        if not raw_rows:
            return None  # fetch failed → let scan() fall back to latest.json/klines/mock
        # Map against a PERMISSIVE universe (the board's own symbols) — the
        # execution venue (Aster / PancakeSwap) does the real tradeable filtering
        # downstream, so symbols 拾贝 ranks but the venue can't trade surface as
        # visible SkippedOrders rather than being silently dropped here. 拾贝 ranks
        # the full Binance perp universe; the on-chain tradeable set is a subset.
        board_universe = [
            {
                "symbol": r["symbol"],
                "base_asset": base_asset_of(r["symbol"]),
                "tradeable": True,
                "min_liquidity_usd": 0.0,
            }
            for r in raw_rows
        ]
        return self._map_universe_rows(raw_rows, board_universe)

    @staticmethod
    def _extract_ranking(data: Any) -> List[Any]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("candidates", "ranking", "boards", "rows", "tracks"):
                val = data.get(key)
                if isinstance(val, list) and val:
                    return val
        return []

    # ------------------------------------------------------------------ #
    # source 2: CoinMarketCap quotes (CMC Agent Hub — never a CEX API)
    # ------------------------------------------------------------------ #
    def _load_cmc_rows(
        self, universe: List[Dict[str, Any]]
    ) -> Optional[List[Dict[str, Any]]]:
        """Build a momentum / relative-strength ranking from **CMC** quotes.

        The market-data feed is the CoinMarketCap Agent Hub
        (``get_crypto_quotes_latest``: price + 24h change), not a centralized
        exchange API — aligned with the BNB-Hack's CMC-first data model. Active
        only when ``config.cmc.mode`` is ``mcp``/``x402``; returns ``None`` in
        mock mode or on any failure so the caller degrades to the offline mock.
        Never raises.
        """
        cfg = getattr(self.config, "cmc", None)
        if cfg is None or (getattr(cfg, "mode", "mock") or "mock").lower() not in ("mcp", "x402"):
            return None
        try:
            from shibei_onchain.signals.cmc_agent_hub import CmcAgentHub

            quotes = CmcAgentHub(cfg).fetch_quotes()
        except Exception:  # noqa: BLE001 - defensive; hub already guards
            return None
        if not quotes:
            return None

        allowed = {u["base_asset"]: u for u in universe}
        rows: List[Dict[str, Any]] = []
        for base, q in quotes.items():
            uinfo = allowed.get(str(base).upper())
            if uinfo is None:
                continue
            price = float(q.get("price") or 0.0)
            if price <= 0:
                continue
            rows.append(
                {
                    "symbol": uinfo["symbol"],
                    "base_asset": str(base).upper(),
                    "side": Side.LONG,
                    "price": price,
                    "momentum": float(q.get("change_24h") or 0.0) / 100.0,
                }
            )
        if not rows:
            return None

        # relative strength = 24h-change rank vs the batch mean.
        momenta = [r["momentum"] for r in rows]
        mean_m = sum(momenta) / len(momenta) if momenta else 0.0
        for r in rows:
            rel = r["momentum"] - mean_m
            # one side per symbol: strong relative strength -> long, weak -> short
            # (short only when enabled). Score = signal *strength*, side-agnostic.
            if self.config.enable_short_leg and rel < 0:
                r["side"] = Side.SHORT
                strength = -rel
            else:
                r["side"] = Side.LONG
                strength = rel
            r["relative_strength_score"] = round(strength * 100.0, 4)
            r["score"] = round(50.0 + strength * 100.0, 4)
            r.pop("momentum", None)
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows

    # ------------------------------------------------------------------ #
    # source 3: deterministic offline mock
    # ------------------------------------------------------------------ #
    def _mock_rows(self, universe: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Deterministic ranking over the registry majors (no network/deps).

        Scores are derived from a fixed, reproducible per-base seed so the same
        registry always yields the same ranking — required for a deterministic
        test. Prices use :data:`_MOCK_PRICES` (sensible majors).
        """
        rows: List[Dict[str, Any]] = []
        for u in universe:
            base = u["base_asset"]
            price = _MOCK_PRICES.get(base, 100.0)
            # deterministic pseudo-momentum in roughly [-0.05, +0.05].
            seed = sum(ord(c) for c in base)
            rel = ((seed % 100) - 50) / 1000.0  # [-0.05, +0.049]
            # one side per symbol: strong -> long, weak -> short (if enabled).
            if self.config.enable_short_leg and rel < 0:
                side = Side.SHORT
                strength = -rel
            else:
                side = Side.LONG
                strength = rel
            rows.append(
                {
                    "symbol": u["symbol"],
                    "base_asset": base,
                    "side": side,
                    "price": float(price),
                    "relative_strength_score": round(strength * 100.0, 4),
                    "score": round(50.0 + strength * 100.0, 4),
                }
            )

        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows

    # ------------------------------------------------------------------ #
    # row -> Candidate mapping
    # ------------------------------------------------------------------ #
    def _rows_to_candidates(
        self,
        rows: List[Dict[str, Any]],
        *,
        source: str,
        signal: Optional[Any],
    ) -> List[Candidate]:
        now = datetime.now(timezone.utc).isoformat()
        candidates: List[Candidate] = []
        for row in rows:
            side = row.get("side", Side.LONG)
            if not isinstance(side, Side):
                side = self._coerce_side(side)
            if side is Side.SHORT and not self.config.enable_short_leg:
                continue

            price = float(row.get("price") or 0.0)
            base = str(row.get("base_asset") or base_asset_of(row.get("symbol", ""))).upper()
            symbol = str(row.get("symbol") or (base + "USDT")).upper()

            stop_pct = getattr(self.risk, "initial_stop_distance_pct", _STOP_DISTANCE_PCT) or _STOP_DISTANCE_PCT
            if side is Side.LONG:
                stop = price * (1.0 - stop_pct)
                leg = self.long_leg
                risk_pct = self.risk.long_risk_pct
                breakeven = self.risk.long_breakeven_r
            else:
                stop = price * (1.0 + stop_pct)
                leg = SHORT_LEG
                risk_pct = self.risk.short_risk_pct
                breakeven = self.risk.long_breakeven_r

            signal_key = "{strat}:{base}:{side}".format(
                strat=self.strategy_id, base=base, side=side.value
            )

            token_signal: Dict[str, Any] = {}
            if signal is not None:
                try:
                    token_signal = signal.token_signal(base) or {}
                except Exception:
                    token_signal = {}

            metrics = {
                "source": source,
                "mock": source == "mock",
            }
            if token_signal:
                metrics["liquidity_score"] = token_signal.get("liquidity_score")

            candidates.append(
                Candidate(
                    symbol=symbol,
                    side=side,
                    price=round(price, 8),
                    stop_loss_price=round(stop, 8),
                    strategy_id=self.strategy_id,
                    strategy_leg=leg,
                    score=float(row.get("score") or 0.0),
                    relative_strength_score=float(
                        row.get("relative_strength_score") or 0.0
                    ),
                    source_boards=(
                        [str(b) for b in row["source_boards"]]
                        if isinstance(row.get("source_boards"), list) and row.get("source_boards")
                        else [symbol]
                    ),
                    signals=["scanner:{0}".format(source)],
                    risk_per_trade_pct=risk_pct,
                    take_profit_r_multiple=self.risk.take_profit_r,
                    breakeven_r_multiple=breakeven,
                    max_hold_hours=self.risk.s16a_v0_max_hold_hours,
                    metrics=metrics,
                    decision_time=now,
                    signal_key=signal_key,
                    evidence={
                        "source": source,
                        "base_asset": base,
                        "stop_distance_pct": stop_pct,
                    },
                )
            )
        return candidates

    # ------------------------------------------------------------------ #
    # coercion helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _coerce_side(value: Any) -> Side:
        if isinstance(value, Side):
            return value
        text = str(value or "").strip().lower()
        if text in ("short", "sell", "s", "空"):
            return Side.SHORT
        return Side.LONG

    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(out) or math.isinf(out):
            return None
        return out
