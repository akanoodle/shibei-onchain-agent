"""CoinMarketCap Agent Hub — the *signal* layer of the on-chain agent.

This module aggregates a single, decision-grade :class:`MarketSignal` that the
拾贝 brain consumes two ways: (1) to enrich ranked candidates with per-token
liquidity/risk colour, and (2) as a *hard* BTC-environment risk gate (the gap
拾贝 V3.0 only ever logged but never enforced).

Three transports, one shape (``CmcConfig.mode``):

    mock  (default) · zero-dependency, fully deterministic synthetic signal.
                       No network, no key. This is what unit tests / offline
                       cycles run against.
    mcp             · JSON-RPC ``tools/call`` over HTTP to ``config.mcp_url``
                       (``https://mcp.coinmarketcap.com/mcp``) with the confirmed
                       auth header ``X-CMC-MCP-API-KEY: {api_key}`` (free key at
                       pro.coinmarketcap.com). Calls the real Agent-Hub tools
                       (``get_crypto_quotes_latest`` etc.) and DERIVES the signal.
                       Lazy ``requests`` with a ``urllib`` fallback.
    x402            · The *machine-payment* transport. Same JSON-RPC ``tools/call``
                       but against ``config.x402_url`` with no API key. CoinMarketCap
                       prices each Agent-Hub call at **$0.01 USDC per call**, settled
                       on-chain via the x402 / HTTP-402 ``Payment Required`` flow:
                       the server answers the first request with HTTP 402 and a
                       price quote, the agent's wallet pays $0.01 USDC, then replays
                       the request with an ``X-PAYMENT`` proof header. That makes
                       every data pull a self-funding micro-transaction — no API
                       key, no subscription, the agent simply pays per call. (We do
                       not sign a real payment here; if the upstream returns 402 or
                       any error we degrade to mock and record a visible note.)

HARD CONTRACT (see docs/INTERFACES.md):
    * ``fetch_market_signal`` ALWAYS returns a valid :class:`MarketSignal`;
      it NEVER raises. Any network / credential / dependency failure degrades to
      the deterministic mock signal and appends a human-readable note, with
      ``source`` set so the failure is *visible* (拾贝 principle).
    * No third-party import at module import time — ``requests`` is imported
      lazily inside the HTTP helper, behind try/except, with a stdlib fallback.
    * Python 3.9: ``from __future__ import annotations``; no ``match``; no runtime
      ``X | Y`` unions.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from shibei_onchain.config import CmcConfig
from shibei_onchain.models import BtcTrend, MarketRegime, MarketSignal, base_asset_of


# --------------------------------------------------------------------------- #
# deterministic mock reference data
# --------------------------------------------------------------------------- #
# The major base assets the on-chain universe trades. Each carries a fixed
# reference price (USD) and a liquidity score in [0, 1] so the mock signal is
# fully deterministic and useful for the universe filter's liquidity floor.
#
#   liquidity_score · 1.0 = deep / blue-chip, lower = thinner book.
#   ref_price       · stable synthetic mark, never network-derived in mock.
_MAJORS: Dict[str, Dict[str, Any]] = {
    "BTC": {"liquidity_score": 1.00, "ref_price": 60000.0},
    "ETH": {"liquidity_score": 0.97, "ref_price": 3000.0},
    "BNB": {"liquidity_score": 0.95, "ref_price": 600.0},
    "USDC": {"liquidity_score": 0.99, "ref_price": 1.0},
    "XRP": {"liquidity_score": 0.80, "ref_price": 0.50},
    "ADA": {"liquidity_score": 0.72, "ref_price": 0.40},
    "DOGE": {"liquidity_score": 0.70, "ref_price": 0.12},
    "CAKE": {"liquidity_score": 0.60, "ref_price": 2.50},
}

# Fixed mock environment marks (deterministic).
_MOCK_BTC_PRICE = 60000.0
_MOCK_FEAR_GREED = 50.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# agent hub
# --------------------------------------------------------------------------- #
class CmcAgentHub:
    """Fetches and normalizes a :class:`MarketSignal` from CoinMarketCap's
    Agent Hub, with a deterministic offline mock as the always-available floor.
    """

    def __init__(self, config: CmcConfig) -> None:
        self.config = config
        # tiny in-process cache so we can serve the last good signal on a
        # transient remote failure rather than only the synthetic mock.
        self._cache: Optional[MarketSignal] = None
        self._cache_at: float = 0.0

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def fetch_market_signal(self, symbols: Optional[List[str]] = None) -> MarketSignal:
        """Return a decision-grade :class:`MarketSignal`. Never raises.

        ``symbols`` is an optional list of board symbols (e.g. ``"BNBUSDT"``)
        whose base assets should be guaranteed present in ``token_signals``;
        when omitted, the full majors set is returned.
        """
        bases = self._wanted_bases(symbols)
        mode = (self.config.mode or "mock").lower()

        if mode == "mock":
            return self._mock_signal(bases, source="mock")

        if mode in ("mcp", "x402"):
            try:
                signal = self._remote_signal(mode, bases)
                if signal is not None:
                    self._cache = signal
                    self._cache_at = time.time()
                    return signal
            except Exception as exc:  # never raise out of the public method
                return self._degrade(bases, mode, "{}: {}".format(type(exc).__name__, exc))
            # _remote_signal returned None without raising -> already noted
            return self._degrade(bases, mode, "empty_or_unmapped_response")

        # unknown mode -> mock with a visible note
        sig = self._mock_signal(bases, source="mock")
        sig.notes.append("unknown_cmc_mode:{}".format(mode))
        return sig

    # ------------------------------------------------------------------ #
    # mock / degrade
    # ------------------------------------------------------------------ #
    def _wanted_bases(self, symbols: Optional[List[str]]) -> List[str]:
        bases: List[str] = list(_MAJORS.keys())
        if symbols:
            for sym in symbols:
                base = base_asset_of(sym)
                if base and base not in bases:
                    bases.append(base)
        return bases

    def _mock_signal(self, bases: List[str], source: str) -> MarketSignal:
        """Build a fully deterministic synthetic signal."""
        token_signals: Dict[str, Dict[str, Any]] = {}
        for base in bases:
            ref = _MAJORS.get(base)
            if ref is not None:
                token_signals[base] = {
                    "liquidity_score": float(ref["liquidity_score"]),
                    "ref_price": float(ref["ref_price"]),
                    "risk_flag": "",
                    "source": "mock",
                }
            else:
                # unknown / long-tail asset: conservative thin-liquidity default
                token_signals[base] = {
                    "liquidity_score": 0.40,
                    "ref_price": 0.0,
                    "risk_flag": "unlisted",
                    "source": "mock",
                }
        return MarketSignal(
            regime=MarketRegime.NEUTRAL,
            btc_trend=BtcTrend.FLAT,
            btc_price=_MOCK_BTC_PRICE,
            fear_greed=_MOCK_FEAR_GREED,
            risk_flags=[],
            source=source,
            as_of=_utc_now_iso(),
            notes=[],
            token_signals=token_signals,
            raw={"mode": "mock"},
        )

    def _degrade(self, bases: List[str], mode: str, reason: str) -> MarketSignal:
        """Remote transport failed: serve fresh cache if any, else mock.

        Always records a visible note describing *why* we degraded.
        """
        note = "{}_fallback_to_{}: {}".format(
            mode, "cache" if self._cache_fresh() else "mock", reason
        )
        if self._cache_fresh() and self._cache is not None:
            # return a shallow-copied cached signal flagged as cache
            cached = self._cache
            cached.source = "cache"
            cached.notes = list(cached.notes) + [note]
            cached.as_of = _utc_now_iso()
            return cached
        sig = self._mock_signal(bases, source="mock")
        sig.notes.append(note)
        return sig

    def _cache_fresh(self) -> bool:
        if self._cache is None:
            return False
        ttl = max(0.0, float(self.config.cache_ttl_seconds))
        return (time.time() - self._cache_at) <= ttl

    # ------------------------------------------------------------------ #
    # remote (mcp / x402) — JSON-RPC tools/call
    # ------------------------------------------------------------------ #
    # CoinMarketCap canonical ids for the majors (used in get_crypto_quotes_latest).
    _CMC_IDS = {
        "1": "BTC", "1027": "ETH", "1839": "BNB", "5426": "SOL",
        "52": "XRP", "2010": "ADA", "74": "DOGE", "3408": "USDC", "7186": "CAKE",
    }

    def _remote_signal(self, mode: str, bases: List[str]) -> Optional[MarketSignal]:
        """Call the real CMC Agent Hub tools and DERIVE a MarketSignal.

        There is no single "give me the regime" tool — the Agent Hub exposes 12
        data tools. We build the decision-grade signal from:
          * ``get_crypto_quotes_latest``                 -> BTC price + 24h change
                                                            (btc_trend, base regime)
                                                            + per-major volume (liquidity)
          * ``get_crypto_marketcap_technical_analysis``  -> market RSI -> regime refine
          * ``get_global_crypto_derivatives_metrics``    -> funding -> risk_flags
        Returns ``None`` (caller degrades) when nothing usable comes back.
        """
        url = self.config.x402_url if mode == "x402" else self.config.mcp_url
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if mode == "mcp" and self.config.api_key:
            # confirmed CMC Agent Hub MCP auth header. (x402 carries no key — it
            # pays $0.01 USDC per call via the HTTP-402 flow instead.)
            headers["X-CMC-MCP-API-KEY"] = self.config.api_key

        notes: List[str] = []

        # 1) latest quotes for BTC + majors in one call -> price/trend/liquidity.
        ids = ",".join(self._CMC_IDS.keys())
        quotes = self._call_tool(url, headers, "get_crypto_quotes_latest", {"id": ids})
        btc_price, btc_change_24h, token_liq = self._parse_quotes(quotes)
        btc_trend = self._trend_from_change(btc_change_24h)
        regime = self._regime_from_change(btc_change_24h)

        # 2) market-cap technical analysis -> refine the regime (best-effort).
        try:
            mta = self._call_tool(url, headers, "get_crypto_marketcap_technical_analysis", {})
            refined = self._regime_from_ta(mta)
            if refined is not MarketRegime.UNKNOWN:
                regime = refined
        except Exception as exc:  # noqa: BLE001 - optional refinement
            notes.append("mcap_ta_skipped:" + type(exc).__name__)

        # 3) global derivatives -> risk flags (best-effort).
        risk_flags: List[str] = []
        try:
            deriv = self._call_tool(url, headers, "get_global_crypto_derivatives_metrics", {})
            risk_flags = self._risk_flags_from_deriv(deriv)
        except Exception as exc:  # noqa: BLE001
            notes.append("derivatives_skipped:" + type(exc).__name__)

        if btc_price is None and regime is MarketRegime.UNKNOWN:
            return None  # couldn't extract anything usable -> degrade

        token_signals = self._build_token_signals(bases, token_liq, source=mode)
        return MarketSignal(
            regime=regime,
            btc_trend=btc_trend,
            btc_price=btc_price,
            fear_greed=None,
            risk_flags=risk_flags,
            source=mode,
            as_of=_utc_now_iso(),
            notes=["cmc_{}_ok".format(mode)] + notes,
            token_signals=token_signals,
            raw={"mode": mode, "btc_change_24h": btc_change_24h},
        )

    def _call_tool(
        self, url: str, headers: Dict[str, str], name: str, arguments: Dict[str, Any]
    ) -> Any:
        """JSON-RPC ``tools/call``; returns the unwrapped tool result (the data
        inside ``result.content[0].text``). Raises on HTTP / RPC / tool error."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        status, body = self._http_post(url, headers, payload)
        if status == 402:
            raise RuntimeError("x402_payment_required (HTTP 402, $0.01 USDC/call)")
        if status != 200 or not body:
            raise RuntimeError("http_status={}".format(status))
        doc = json.loads(body)
        if isinstance(doc, dict) and doc.get("error"):
            raise RuntimeError("rpc_error:{}".format(str(doc.get("error"))[:80]))
        result = doc.get("result", doc) if isinstance(doc, dict) else doc
        if isinstance(result, dict) and result.get("isError"):
            text = ""
            try:
                text = str(result["content"][0]["text"])[:120]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError("tool_error:{}:{}".format(name, text))
        return self._unwrap_result(result)

    # ------------------------------------------------------------------ #
    # derivation (defensive — CMC MCP field shapes are tolerated, not assumed)
    # ------------------------------------------------------------------ #
    def fetch_quotes(self) -> Dict[str, Dict[str, float]]:
        """Return ``{BASE: {price, change_24h, volume_24h}}`` for the CMC majors.

        This is the **CMC-sourced** market-data feed the scanner falls back to when
        the 拾贝 board is unavailable — so the agent's price/momentum input is CMC,
        never a centralized-exchange API. Returns ``{}`` in mock mode or on any
        failure (the caller then degrades to the deterministic offline mock).
        """
        mode = (self.config.mode or "mock").lower()
        if mode not in ("mcp", "x402"):
            return {}
        url = self.config.x402_url if mode == "x402" else self.config.mcp_url
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if mode == "mcp" and self.config.api_key:
            headers["X-CMC-MCP-API-KEY"] = self.config.api_key
        try:
            ids = ",".join(self._CMC_IDS.keys())
            data = self._call_tool(url, headers, "get_crypto_quotes_latest", {"id": ids})
        except Exception:  # noqa: BLE001 - never raise; caller degrades
            return {}
        out: Dict[str, Dict[str, float]] = {}
        for row in self._quote_rows(data):
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper()
            base = sym or self._CMC_IDS.get(str(row.get("id") or ""), "")
            if not base:
                continue
            price = _to_float(_find_first(row, ("price",)))
            if not price or price <= 0:
                continue
            change = _to_float(_find_first(row, ("percentChange24h", "percent_change_24h")))
            vol = _to_float(_find_first(row, ("volume24h", "volume_24h")))
            out[base] = {
                "price": float(price),
                "change_24h": float(change or 0.0),
                "volume_24h": float(vol or 0.0),
            }
        return out

    def _parse_quotes(self, data: Any) -> Tuple[Optional[float], Optional[float], Dict[str, float]]:
        """Return (btc_price, btc_pct_change_24h, {base: liquidity_score})."""
        btc_price: Optional[float] = None
        btc_change: Optional[float] = None
        liq: Dict[str, float] = {}
        for row in self._quote_rows(data):
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper()
            base = sym or self._CMC_IDS.get(str(row.get("id") or ""), "")
            price = _find_first(row, ("price",))
            change = _find_first(row, ("percentChange24h", "percent_change_24h"))
            vol = _find_first(row, ("volume24h", "volume_24h"))
            if base == "BTC":
                btc_price = _to_float(price)
                btc_change = _to_float(change)
            if base and vol is not None:
                v = _to_float(vol, 0.0) or 0.0
                liq[base] = max(0.0, min(1.0, v / 3.0e10))  # ~$30B 24h vol -> 1.0
        return btc_price, btc_change, liq

    @staticmethod
    def _quote_rows(data: Any) -> List[Any]:
        """Flatten the various quote-response shapes into a list of row dicts.

        The live ``get_crypto_quotes_latest`` returns a COLUMNAR table
        ``{"headers": [...], "rows": [[...], ...]}`` — zip each row against the
        headers. Also tolerates list / ``{data: [...]}`` / ``{id: row}`` shapes."""
        if isinstance(data, dict) and isinstance(data.get("headers"), list) and isinstance(data.get("rows"), list):
            headers = data["headers"]
            return [dict(zip(headers, row)) for row in data["rows"] if isinstance(row, list)]
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = data.get("data", data)
            if isinstance(inner, list):
                return inner
            if isinstance(inner, dict):
                vals = list(inner.values())
                if vals and all(isinstance(v, dict) for v in vals):
                    return vals
                return [inner]
        return []

    @staticmethod
    def _trend_from_change(change: Any) -> BtcTrend:
        c = _to_float(change)
        if c is None:
            return BtcTrend.UNKNOWN
        if c > 1.5:
            return BtcTrend.UP
        if c < -1.5:
            return BtcTrend.DOWN
        return BtcTrend.FLAT

    @staticmethod
    def _regime_from_change(change: Any) -> MarketRegime:
        c = _to_float(change)
        if c is None:
            return MarketRegime.UNKNOWN
        if c > 2.5:
            return MarketRegime.RISK_ON
        if c < -2.5:
            return MarketRegime.RISK_OFF
        return MarketRegime.NEUTRAL

    @staticmethod
    def _regime_from_ta(data: Any) -> MarketRegime:
        # market-cap TA nests RSI as {"rsi": {"rsi7","rsi14","rsi21"}} — use rsi14.
        rsi = _to_float(_find_first(data, ("rsi14", "rsi", "RSI")))
        if rsi is None:
            return MarketRegime.UNKNOWN
        if rsi >= 55:
            return MarketRegime.RISK_ON
        if rsi <= 45:
            return MarketRegime.RISK_OFF
        return MarketRegime.NEUTRAL

    @staticmethod
    def _risk_flags_from_deriv(data: Any) -> List[str]:
        flags: List[str] = []
        funding = _to_float(_find_first(data, ("fundingRate", "funding_rate", "avgFundingRate")))
        if funding is not None and abs(funding) >= 0.05:
            flags.append("extreme_funding")
        # sharp 24h open-interest drop = forced deleveraging / risk stress.
        oi = data.get("totalOpenInterest") if isinstance(data, dict) else None
        if isinstance(oi, dict):
            oi_chg = _pct_to_float(oi.get("percentage_change_24h"))
            if oi_chg is not None and oi_chg <= -10.0:
                flags.append("oi_deleveraging")
        return flags

    def _build_token_signals(
        self, bases: List[str], token_liq: Dict[str, float], source: str
    ) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for base in bases:
            ref = _MAJORS.get(base)
            if base in token_liq:
                out[base] = {
                    "liquidity_score": float(token_liq[base]),
                    "ref_price": float(ref["ref_price"]) if ref else 0.0,
                    "risk_flag": "",
                    "source": source,
                }
            elif ref:
                out[base] = {
                    "liquidity_score": float(ref["liquidity_score"]),
                    "ref_price": float(ref["ref_price"]),
                    "risk_flag": "",
                    "source": "mock_backfill",
                }
            else:
                out[base] = {"liquidity_score": 0.40, "ref_price": 0.0,
                             "risk_flag": "unlisted", "source": "mock_backfill"}
        return out

    @staticmethod
    def _unwrap_result(result: Any) -> Any:
        """Dig through the MCP content wrapper to the underlying JSON payload."""
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict) and "text" in first:
                    try:
                        return json.loads(first["text"])
                    except (ValueError, TypeError):
                        return first.get("text")
            return result
        return result

    # ------------------------------------------------------------------ #
    # HTTP — lazy requests, urllib fallback (no top-level third-party import)
    # ------------------------------------------------------------------ #
    def _http_post(
        self, url: str, headers: Dict[str, str], payload: Dict[str, Any]
    ) -> Tuple[int, str]:
        """POST JSON, returning ``(status_code, body_text)``.

        Tries ``requests`` (lazy import); falls back to stdlib ``urllib`` so the
        module needs zero third-party deps. Raises on transport error — the
        caller converts that into a visible degrade.
        """
        timeout = max(1.0, float(self.config.timeout_seconds))
        body = json.dumps(payload).encode("utf-8")

        # 1) preferred: requests, imported lazily inside try/except.
        try:
            import requests  # type: ignore

            resp = requests.post(url, data=body, headers=headers, timeout=timeout)
            return int(resp.status_code), resp.text or ""
        except ImportError:
            pass  # fall through to urllib
        # any other requests-level error propagates to caller as a degrade.

        # 2) stdlib fallback.
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - controlled url
                status = int(getattr(resp, "status", 200) or 200)
                text = resp.read().decode("utf-8", errors="replace")
                return status, text
        except urllib.error.HTTPError as exc:
            # HTTP errors (e.g. 402 Payment Required) carry a status + body.
            try:
                text = exc.read().decode("utf-8", errors="replace")
            except Exception:
                text = ""
            return int(exc.code), text


# --------------------------------------------------------------------------- #
# small coercion helper
# --------------------------------------------------------------------------- #
def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct_to_float(value: Any) -> Optional[float]:
    """Parse a percentage string like ``"+5.76%"`` / ``"-10.23%"`` to a float."""
    if value is None:
        return None
    try:
        return float(str(value).replace("%", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return None


def _find_first(obj: Any, keys: Tuple[str, ...], _depth: int = 0) -> Any:
    """Recursively find the first scalar value under any of ``keys`` in a nested
    dict/list. Lets us extract price/RSI/funding without assuming CMC's exact
    (and undocumented-here) response nesting. Bounded depth; never raises."""
    if _depth > 6:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and not isinstance(obj[k], (dict, list)):
                return obj[k]
        for v in obj.values():
            found = _find_first(v, keys, _depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_first(v, keys, _depth + 1)
            if found is not None:
                return found
    return None
