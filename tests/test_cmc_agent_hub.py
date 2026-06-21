"""Unit tests for signals/cmc_agent_hub.py — pure MOCK mode, no network, no deps.

Every test is deterministic: the default ``mock`` mode never touches the
network, and the remote (mcp/x402) modes are forced to degrade by pointing at
an unroutable URL / a tiny timeout, then asserting the visible-fallback contract.
"""

from __future__ import annotations

import json

from shibei_onchain.config import CmcConfig
from shibei_onchain.models import BtcTrend, MarketRegime, MarketSignal
from shibei_onchain.signals.cmc_agent_hub import CmcAgentHub


MAJORS = ("BNB", "ETH", "BTC", "CAKE", "XRP", "ADA", "DOGE", "USDC")


def _mock_hub() -> CmcAgentHub:
    return CmcAgentHub(CmcConfig(mode="mock"))


# --------------------------------------------------------------------------- #
# mock mode: deterministic, complete, no network
# --------------------------------------------------------------------------- #
def test_mock_signal_shape_and_source():
    hub = _mock_hub()
    sig = hub.fetch_market_signal()

    assert isinstance(sig, MarketSignal)
    assert sig.source == "mock"
    assert sig.regime is MarketRegime.NEUTRAL
    assert sig.btc_trend is BtcTrend.FLAT
    assert sig.btc_price == 60000.0
    assert sig.fear_greed == 50.0
    assert sig.risk_flags == []
    assert sig.as_of.endswith("Z")


def test_mock_has_all_majors_with_liquidity_scores():
    hub = _mock_hub()
    sig = hub.fetch_market_signal()
    for base in MAJORS:
        ts = sig.token_signal(base)
        assert ts, "missing token signal for {}".format(base)
        score = ts["liquidity_score"]
        assert 0.0 <= score <= 1.0
    # blue chips must out-rank the thinner CAKE book
    assert sig.token_signal("BTC")["liquidity_score"] >= sig.token_signal("CAKE")["liquidity_score"]


def test_mock_is_deterministic():
    a = _mock_hub().fetch_market_signal()
    b = _mock_hub().fetch_market_signal()
    assert a.token_signals == b.token_signals
    assert a.regime == b.regime
    assert a.btc_price == b.btc_price


def test_unknown_symbol_backfills_conservative_liquidity():
    hub = _mock_hub()
    sig = hub.fetch_market_signal(symbols=["FOOUSDT", "BNBUSDT"])
    foo = sig.token_signal("FOO")
    assert foo, "unknown base should still be present"
    assert foo["liquidity_score"] == 0.40
    assert foo["risk_flag"] == "unlisted"
    # known one still present and rich
    assert sig.token_signal("BNB")["liquidity_score"] == 0.95


def test_symbols_argument_does_not_drop_majors():
    hub = _mock_hub()
    sig = hub.fetch_market_signal(symbols=["ETHUSDT"])
    # passing a symbol must still keep the full majors set present
    for base in MAJORS:
        assert sig.token_signal(base)


# --------------------------------------------------------------------------- #
# remote modes degrade visibly (no network reachable / unroutable)
# --------------------------------------------------------------------------- #
def _unroutable_config(mode: str) -> CmcConfig:
    # ".invalid" TLD never resolves; tiny timeout keeps the test fast+offline.
    return CmcConfig(
        mode=mode,
        mcp_url="http://nonexistent.invalid/mcp",
        x402_url="http://nonexistent.invalid/x402",
        api_key="",
        timeout_seconds=1.0,
        cache_ttl_seconds=300.0,
    )


def test_mcp_failure_falls_back_to_mock_with_note():
    hub = CmcAgentHub(_unroutable_config("mcp"))
    sig = hub.fetch_market_signal()
    assert isinstance(sig, MarketSignal)
    # never raised; degraded to mock; failure is visible
    assert sig.source == "mock"
    assert any("mcp_fallback" in n for n in sig.notes)
    # still a fully usable signal
    for base in MAJORS:
        assert sig.token_signal(base)


def test_x402_failure_falls_back_to_mock_with_note():
    hub = CmcAgentHub(_unroutable_config("x402"))
    sig = hub.fetch_market_signal()
    assert sig.source == "mock"
    assert any("x402_fallback" in n for n in sig.notes)


def test_unknown_mode_returns_mock_and_notes_it():
    hub = CmcAgentHub(CmcConfig(mode="banana"))
    sig = hub.fetch_market_signal()
    assert sig.source == "mock"
    assert any("unknown_cmc_mode:banana" in n for n in sig.notes)


def test_never_raises_on_any_mode():
    for mode in ("mock", "mcp", "x402", "weird"):
        cfg = _unroutable_config(mode)
        # should never raise regardless of mode/network
        sig = CmcAgentHub(cfg).fetch_market_signal()
        assert isinstance(sig, MarketSignal)


# --------------------------------------------------------------------------- #
# derivation from the real CMC tools (no network: feed decoded tool payloads)
# --------------------------------------------------------------------------- #
def test_parse_quotes_extracts_btc_and_liquidity():
    hub = _mock_hub()
    data = {"data": [
        {"id": 1, "symbol": "BTC", "price": 65000.0, "percentChange24h": 3.2, "volume24h": 3.0e10},
        {"id": 1839, "symbol": "BNB", "price": 600.0, "percentChange24h": 1.0, "volume24h": 1.5e10},
    ]}
    btc_price, btc_change, liq = hub._parse_quotes(data)
    assert btc_price == 65000.0
    assert btc_change == 3.2
    assert liq["BTC"] == 1.0                 # ~$30B 24h vol -> capped at 1.0
    assert 0.0 < liq["BNB"] < 1.0


def test_quote_rows_tolerates_shapes():
    hub = _mock_hub()
    assert len(hub._quote_rows([{"a": 1}])) == 1                                # list
    assert len(hub._quote_rows({"data": {"1": {"x": 1}, "2": {"y": 2}}})) == 2  # id-keyed dict
    assert len(hub._quote_rows({"symbol": "BTC", "price": 1})) == 1             # single row


def test_parse_quotes_columnar_real_shape():
    # the REAL get_crypto_quotes_latest returns columnar {headers, rows}
    hub = _mock_hub()
    data = {
        "headers": ["id", "symbol", "price", "percent_change_24h", "volume_24h"],
        "rows": [
            [1, "BTC", 65853.70, 0.4, 3.0e10],
            [1839, "BNB", 600.0, 2.0, 1.5e10],
        ],
    }
    btc_price, btc_change, liq = hub._parse_quotes(data)
    assert btc_price == 65853.70
    assert btc_change == 0.4
    assert liq["BTC"] == 1.0


def test_regime_from_ta_nested_rsi14():
    hub = _mock_hub()
    # real market-cap TA nests RSI; rsi14 is the standard one
    data = {"rsi": {"rsi7": "81.53", "rsi14": "45.56", "rsi21": "32.46"}}
    assert hub._regime_from_ta(data) is MarketRegime.NEUTRAL    # rsi14 45.56 in (45,55)
    assert hub._regime_from_ta({"rsi": {"rsi14": "40"}}) is MarketRegime.RISK_OFF
    data2 = {"rsi": {"rsi7": "81", "rsi14": "70", "rsi21": "60"}}
    assert hub._regime_from_ta(data2) is MarketRegime.RISK_ON   # rsi14 70 -> risk_on


def test_risk_flags_oi_deleveraging():
    from shibei_onchain.signals.cmc_agent_hub import _pct_to_float

    assert _pct_to_float("+5.76%") == 5.76
    assert _pct_to_float("-10.23%") == -10.23
    hub = _mock_hub()
    deriv = {"totalOpenInterest": {"percentage_change_24h": "-12.5%"}}
    assert "oi_deleveraging" in hub._risk_flags_from_deriv(deriv)
    calm = {"totalOpenInterest": {"percentage_change_24h": "+5.76%"}}
    assert hub._risk_flags_from_deriv(calm) == []


def test_trend_and_regime_thresholds():
    hub = _mock_hub()
    assert hub._trend_from_change(3.0) is BtcTrend.UP
    assert hub._trend_from_change(-3.0) is BtcTrend.DOWN
    assert hub._trend_from_change(0.5) is BtcTrend.FLAT
    assert hub._trend_from_change(None) is BtcTrend.UNKNOWN
    assert hub._regime_from_change(3.0) is MarketRegime.RISK_ON
    assert hub._regime_from_change(-3.0) is MarketRegime.RISK_OFF
    assert hub._regime_from_change(0.0) is MarketRegime.NEUTRAL


def test_regime_from_ta_rsi():
    hub = _mock_hub()
    assert hub._regime_from_ta({"indicators": {"rsi": 70}}) is MarketRegime.RISK_ON
    assert hub._regime_from_ta({"rsi": 30}) is MarketRegime.RISK_OFF
    assert hub._regime_from_ta({"rsi": 50}) is MarketRegime.NEUTRAL
    assert hub._regime_from_ta({"nope": 1}) is MarketRegime.UNKNOWN


def test_unwrap_mcp_content_text_wrapper():
    hub = _mock_hub()
    inner = {"data": [{"symbol": "BTC", "price": 61000.0}]}
    result = {"content": [{"type": "text", "text": json.dumps(inner)}]}
    assert hub._unwrap_result(result) == inner


def test_find_first_recursive():
    from shibei_onchain.signals.cmc_agent_hub import _find_first

    assert _find_first({"a": {"b": {"price": 42}}}, ("price",)) == 42
    assert _find_first([{"x": 1}, {"rsi": 55}], ("rsi",)) == 55
    assert _find_first({"a": 1}, ("missing",)) is None


def test_build_token_signals_remote_then_backfill():
    hub = _mock_hub()
    ts = hub._build_token_signals(["BTC", "ETH"], {"BTC": 0.9}, source="mcp")
    assert ts["BTC"]["liquidity_score"] == 0.9
    assert ts["BTC"]["source"] == "mcp"
    assert ts["ETH"]["source"] == "mock_backfill"   # absent from remote liq -> backfill


if __name__ == "__main__":  # pragma: no cover
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------- #
# fetch_quotes — CMC-sourced market data for the scanner fallback (no CEX API)
# --------------------------------------------------------------------------- #
def test_fetch_quotes_empty_in_mock_mode():
    # mock mode never hits the network -> returns {} so the scanner uses its
    # deterministic offline mock instead.
    assert _mock_hub().fetch_quotes() == {}


def test_fetch_quotes_empty_on_unroutable_remote():
    hub = CmcAgentHub(_unroutable_config("mcp"))
    assert hub.fetch_quotes() == {}     # network fails -> {} (caller degrades)
