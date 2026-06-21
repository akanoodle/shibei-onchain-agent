"""On-chain tradeable-universe filter — the HARD pre-trade gate.

拾贝 V3.0 ranks *board* symbols (``BNBUSDT``, ``RANDOMSHITUSDT``, …) without ever
asking *"can this actually be swapped on PancakeSwap, with enough depth, on the
chain we are wired to?"*. On a CEX that question is implicit; on-chain it is the
single most important pre-trade safety check. This module answers it.

Two enforcement points, both faithful to the 拾贝 principle *"failure must be
visible"* (rejections are recorded as :class:`SkippedOrder`, never dropped):

    * :meth:`UniverseFilter.filter` — runs *after* scoring/ranking, *before*
      sizing/execution. Keeps only candidates whose base asset is in the on-chain
      tradeable registry for the active ``chain_id`` and clears the per-token
      liquidity floor (registry ``min_liquidity_usd``, optionally tightened by a
      CMC ``liquidity_score``). Everything else becomes a
      ``SkippedOrder(stage="universe_filter", reason="not_onchain_tradeable" |
      "insufficient_liquidity")``.
    * :meth:`UniverseFilter.final_guard` — the execution-time re-check. Even a
      registry-approved token can blow up at swap time (empty/zero-output quote,
      or realized price drifted past the order's slippage budget). When the quote
      came from a real ``web3`` ``getAmountsOut`` call this guard catches it and
      returns a ``SkippedOrder(stage="final_guard")``.

The token registry is loaded from ``config/tokens.bsc.json`` (path from
``OnChainConfig.tokens_path``), keyed by ``str(onchain.chain_id)`` so both BSC
testnet (97) and mainnet (56) resolve. This module imports cleanly with **zero
third-party dependencies** — only the stdlib ``json``/``os`` are used.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from shibei_onchain.config import OnChainConfig
from shibei_onchain.models import (
    Candidate,
    MarketSignal,
    PlannedOrder,
    SkippedOrder,
    TokenInfo,
    base_asset_of,
)


# --------------------------------------------------------------------------- #
# repo-root resolution (stdlib only)
# --------------------------------------------------------------------------- #
def _project_root() -> str:
    """Best-effort path to the repository root.

    This file lives at ``<root>/src/shibei_onchain/onchain/universe_filter.py``;
    walk up four levels to recover ``<root>``.
    """
    here = os.path.abspath(__file__)
    # universe_filter.py -> onchain -> shibei_onchain -> src -> <root>
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))


def _resolve_tokens_path(tokens_path: str) -> Tuple[Optional[str], List[str]]:
    """Resolve ``tokens_path`` to an existing file, returning ``(path, tried)``.

    Absolute paths are used as-is. Relative paths are tried against the current
    working directory first, then the repository root (so the registry resolves
    whether the agent is launched from the repo root or from elsewhere).
    """
    tried: List[str] = []
    if not tokens_path:
        return None, tried

    if os.path.isabs(tokens_path):
        tried.append(tokens_path)
        return (tokens_path if os.path.isfile(tokens_path) else None), tried

    candidates = [
        os.path.abspath(os.path.join(os.getcwd(), tokens_path)),
        os.path.abspath(os.path.join(_project_root(), tokens_path)),
    ]
    for cand in candidates:
        if cand in tried:
            continue
        tried.append(cand)
        if os.path.isfile(cand):
            return cand, tried
    return None, tried


# --------------------------------------------------------------------------- #
# UniverseFilter
# --------------------------------------------------------------------------- #
class UniverseFilter:
    """Loads the on-chain token registry for the active chain and enforces the
    tradeability + liquidity-floor gate at both decision and execution time."""

    def __init__(self, onchain: OnChainConfig) -> None:
        self.onchain = onchain
        self.chain_id = int(onchain.chain_id)
        # network-level metadata (router / wbnb / usdt) loaded from the registry,
        # falling back to the config addresses when the file is missing.
        self._router: str = onchain.pancake_router or ""
        self._wbnb_address: str = onchain.wbnb_address or ""
        self._usdt_address: str = onchain.usdt_address or ""
        self._network_name: str = onchain.chain
        # registry indices keyed by upper-cased base asset and board symbol.
        self._by_base: Dict[str, TokenInfo] = {}
        self._by_symbol: Dict[str, TokenInfo] = {}
        # diagnostics — visible, never thrown.
        self.source: str = "mock"
        self.notes: List[str] = []
        self.tokens_path: str = ""

        self._load()

    # -- loading ----------------------------------------------------------- #
    def _load(self) -> None:
        path, tried = _resolve_tokens_path(self.onchain.tokens_path)
        if path is None:
            self.source = "config_fallback"
            self.notes.append(
                "tokens_file_not_found:" + ";".join(tried[-3:]) if tried else "tokens_path_empty"
            )
            self._load_config_fallback()
            return

        self.tokens_path = path
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:  # unreadable / malformed JSON
            self.source = "config_fallback"
            self.notes.append("tokens_file_unreadable:{}".format(type(exc).__name__))
            self._load_config_fallback()
            return

        networks = (data or {}).get("networks", {})
        net = networks.get(str(self.chain_id))
        if not isinstance(net, dict):
            self.source = "config_fallback"
            self.notes.append("chain_id_not_in_registry:{}".format(self.chain_id))
            self._load_config_fallback()
            return

        self._router = str(net.get("router") or self._router)
        self._wbnb_address = str(net.get("wbnb") or self._wbnb_address)
        self._usdt_address = str(net.get("usdt") or self._usdt_address)
        self._network_name = str(net.get("name") or self._network_name)

        for raw in net.get("tokens", []) or []:
            info = self._token_from_raw(raw)
            if info is not None:
                self._index(info)

        # ensure USDT is always resolvable as the quote asset, even if the
        # registry only lists it implicitly via the network ``usdt`` address.
        if "USDT" not in self._by_base and self._usdt_address:
            self._index(
                TokenInfo(
                    symbol="USDTUSDT",
                    base_asset="USDT",
                    address=self._usdt_address,
                    decimals=18,
                    min_liquidity_usd=0.0,
                    tradeable=True,
                    notes="quote stable (synthesized from network metadata)",
                )
            )

        self.source = "registry"
        if not self._by_base:
            self.notes.append("registry_chain_has_no_tokens:{}".format(self.chain_id))

    def _load_config_fallback(self) -> None:
        """Degrade gracefully: synthesize a minimal registry (WBNB + USDT) from
        the on-chain config so the agent can still resolve the quote asset and
        native token. Failures stay visible via ``self.notes``/``self.source``."""
        wbnb = TokenInfo(
            symbol="BNBUSDT",
            base_asset="BNB",
            address=self._wbnb_address,
            decimals=18,
            is_native=True,
            min_liquidity_usd=0.0,
            tradeable=bool(self._wbnb_address),
            notes="native BNB (config fallback)",
        )
        usdt = TokenInfo(
            symbol="USDTUSDT",
            base_asset="USDT",
            address=self._usdt_address,
            decimals=18,
            min_liquidity_usd=0.0,
            tradeable=bool(self._usdt_address),
            notes="quote stable (config fallback)",
        )
        if wbnb.address:
            self._index(wbnb)
        if usdt.address:
            self._index(usdt)

    @staticmethod
    def _token_from_raw(raw: Dict[str, Any]) -> Optional[TokenInfo]:
        if not isinstance(raw, dict):
            return None
        symbol = str(raw.get("symbol", "")).strip()
        base = str(raw.get("base_asset", "")).strip() or base_asset_of(symbol)
        address = str(raw.get("address", "")).strip()
        if not base or not address:
            return None
        try:
            decimals = int(raw.get("decimals", 18))
        except (TypeError, ValueError):
            decimals = 18
        try:
            min_liq = float(raw.get("min_liquidity_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            min_liq = 0.0
        return TokenInfo(
            symbol=symbol or (base.upper() + "USDT"),
            base_asset=base.upper(),
            address=address,
            decimals=decimals,
            is_native=bool(raw.get("is_native", False)),
            min_liquidity_usd=min_liq,
            tradeable=bool(raw.get("tradeable", True)),
            notes=str(raw.get("notes", "")),
        )

    def _index(self, info: TokenInfo) -> None:
        self._by_base[info.base_asset.upper()] = info
        if info.symbol:
            self._by_symbol[info.symbol.upper()] = info

    # -- lookups ----------------------------------------------------------- #
    def token_info(self, base_or_symbol: str) -> Optional[TokenInfo]:
        """Resolve a :class:`TokenInfo` by *either* base asset (``"BNB"``) or
        board symbol (``"BNBUSDT"``). Returns ``None`` if not in the registry."""
        key = (base_or_symbol or "").strip().upper()
        if not key:
            return None
        # try board symbol first (more specific), then base asset, then the base
        # asset derived from a board symbol.
        if key in self._by_symbol:
            return self._by_symbol[key]
        if key in self._by_base:
            return self._by_base[key]
        base = base_asset_of(key)
        return self._by_base.get(base)

    def is_tradeable(self, base_or_symbol: str) -> bool:
        info = self.token_info(base_or_symbol)
        return bool(info and info.tradeable and info.address)

    def router_address(self) -> str:
        return self._router

    def usdt(self) -> TokenInfo:
        """The quote-asset :class:`TokenInfo` (always resolvable)."""
        info = self._by_base.get("USDT")
        if info is not None:
            return info
        # last-resort synthetic so callers always get a TokenInfo, not None.
        return TokenInfo(
            symbol="USDTUSDT",
            base_asset="USDT",
            address=self._usdt_address,
            decimals=18,
            min_liquidity_usd=0.0,
            tradeable=bool(self._usdt_address),
            notes="quote stable (synthesized)",
        )

    def all_base_assets(self) -> List[str]:
        return sorted(self._by_base.keys())

    # -- the HARD pre-trade filter ---------------------------------------- #
    def filter(
        self,
        candidates: List[Candidate],
        signal: Optional[MarketSignal] = None,
    ) -> Tuple[List[Candidate], List[SkippedOrder]]:
        """Post-scoring HARD gate. Keep on-chain tradeable candidates that clear
        the liquidity floor; reject the rest as ``stage="universe_filter"``.

        Liquidity floor = registry ``min_liquidity_usd``. When the CMC signal
        carries a per-token ``liquidity_score`` (a 0..1 confidence) it *tightens*
        the floor: a score < 0.5 raises the effective floor proportionally, so a
        token the signal layer flags as thin is rejected even if the registry
        floor is nominally satisfied.
        """
        kept: List[Candidate] = []
        skipped: List[SkippedOrder] = []

        for cand in candidates or []:
            base = cand.base_asset
            info = self.token_info(cand.symbol) or self.token_info(base)

            if info is None or not info.tradeable or not info.address:
                skipped.append(
                    SkippedOrder(
                        symbol=cand.symbol,
                        side=cand.side,
                        reason="not_onchain_tradeable",
                        stage="universe_filter",
                        detail={
                            "base_asset": base,
                            "chain_id": self.chain_id,
                            "in_registry": info is not None,
                            "registry_source": self.source,
                        },
                    )
                )
                continue

            ok, detail = self._passes_liquidity(info, signal)
            if not ok:
                skipped.append(
                    SkippedOrder(
                        symbol=cand.symbol,
                        side=cand.side,
                        reason="insufficient_liquidity",
                        stage="universe_filter",
                        detail=detail,
                    )
                )
                continue

            kept.append(cand)

        return kept, skipped

    def _passes_liquidity(
        self, info: TokenInfo, signal: Optional[MarketSignal]
    ) -> Tuple[bool, Dict[str, Any]]:
        floor = max(0.0, float(info.min_liquidity_usd or 0.0))
        liquidity_score: Optional[float] = None
        effective_floor = floor

        if signal is not None:
            tok = signal.token_signal(info.base_asset)
            raw_score = tok.get("liquidity_score") if isinstance(tok, dict) else None
            if isinstance(raw_score, (int, float)):
                liquidity_score = float(raw_score)

        detail: Dict[str, Any] = {
            "base_asset": info.base_asset,
            "min_liquidity_usd": floor,
            "liquidity_score": liquidity_score,
            "chain_id": self.chain_id,
        }

        # No registry floor and no signal score => nothing to enforce (typical on
        # testnet where min_liquidity_usd is 0). Pass.
        if floor <= 0.0 and liquidity_score is None:
            return True, detail

        # A signal liquidity_score below 0.5 means the signal layer is not
        # confident in this pool's depth: tighten the floor proportionally and
        # treat an explicit zero/near-zero score as a hard reject.
        if liquidity_score is not None and liquidity_score < 0.5:
            if liquidity_score <= 0.0:
                detail["effective_floor"] = effective_floor
                detail["reason_detail"] = "signal_liquidity_score_zero"
                return False, detail
            # scale the floor up (thinner score -> higher required depth).
            effective_floor = floor * (0.5 / liquidity_score)
            detail["effective_floor"] = effective_floor
            detail["reason_detail"] = "signal_tightened_floor"
            # With no registry floor we can't compare against a depth number, so
            # a low-but-nonzero score on a zero-floor token is a soft pass.
            if floor <= 0.0:
                detail["effective_floor"] = effective_floor
                return True, detail
            # We have no live depth number here (that is the final_guard's job at
            # execution time); the registry floor is our proxy. The tightened
            # floor exceeds the registry floor by construction, so a thin-signal
            # token is rejected.
            return False, detail

        return True, detail

    # -- the execution-time re-check -------------------------------------- #
    def final_guard(
        self, order: PlannedOrder, quote: Dict[str, Any]
    ) -> Optional[SkippedOrder]:
        """Execution-time re-check against the *live* quote.

        Returns a ``SkippedOrder(stage="final_guard")`` when the swap should be
        aborted, else ``None``. Two abort conditions:

        * ``amount_out`` is zero/missing — an empty or broken pool route.
        * The quote came from a real ``web3`` ``getAmountsOut`` and the realized
          price drifted past the order's ``max_slippage_bps`` budget vs the
          intended ``entry_price``. (Mock quotes are skipped here because their
          price is synthetic — slippage is meaningless against a derived number.)
        """
        quote = quote or {}

        amount_out = self._as_float(quote.get("amount_out"))
        min_amount_out = self._as_float(quote.get("min_amount_out"))
        source = str(quote.get("source", ""))
        quote_price = self._as_float(quote.get("price"))

        # 1) zero / missing output — applies regardless of source.
        if amount_out <= 0.0:
            return SkippedOrder(
                symbol=order.symbol,
                side=order.side,
                reason="zero_output_quote",
                stage="final_guard",
                detail={
                    "amount_out": amount_out,
                    "min_amount_out": min_amount_out,
                    "source": source,
                },
            )

        # 2) realized slippage — only meaningful for a live web3 quote.
        if source == "web3" and order.entry_price > 0 and quote_price > 0:
            slippage = abs(quote_price - order.entry_price) / order.entry_price
            slippage_bps = slippage * 10000.0
            budget_bps = float(max(0, order.max_slippage_bps))
            if slippage_bps > budget_bps:
                return SkippedOrder(
                    symbol=order.symbol,
                    side=order.side,
                    reason="slippage_exceeds_budget",
                    stage="final_guard",
                    detail={
                        "entry_price": order.entry_price,
                        "quote_price": quote_price,
                        "slippage_bps": round(slippage_bps, 2),
                        "max_slippage_bps": order.max_slippage_bps,
                        "source": source,
                    },
                )

        return None

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            if value is None:
                return 0.0
            return float(value)
        except (TypeError, ValueError):
            return 0.0
