"""Unit tests for signals/regime_gate.evaluate_regime_gate.

Pure MOCK mode: no network, no third-party deps. Every signal is constructed
in-process, so the tests are fully deterministic. We exercise the asymmetric
gate (long-only blocks for a weak tape; both-side blocks only on hard flags),
the disabled gate, and a couple of edge cases.
"""

from __future__ import annotations

from dataclasses import replace

from shibei_onchain.config import RiskParams
from shibei_onchain.models import BtcTrend, MarketRegime, MarketSignal
from shibei_onchain.signals.regime_gate import evaluate_regime_gate


def _result_keys_ok(result):
    assert set(result.keys()) == {"long_blocked", "short_blocked", "reasons", "notes"}
    assert isinstance(result["long_blocked"], bool)
    assert isinstance(result["short_blocked"], bool)
    assert isinstance(result["reasons"], list)
    assert isinstance(result["notes"], list)


def test_risk_on_allows_both_sides():
    """A clean risk_on / up tape with no flags blocks nothing."""
    risk = RiskParams()
    signal = MarketSignal(regime=MarketRegime.RISK_ON, btc_trend=BtcTrend.UP)
    result = evaluate_regime_gate(signal, risk)
    _result_keys_ok(result)
    assert result["long_blocked"] is False
    assert result["short_blocked"] is False
    assert result["reasons"] == []


def test_risk_off_blocks_long_only():
    """risk_off regime gates new longs but leaves shorts available."""
    risk = RiskParams()
    # btc_trend FLAT so only the regime triggers (not the trend gate too).
    signal = MarketSignal(regime=MarketRegime.RISK_OFF, btc_trend=BtcTrend.FLAT)
    result = evaluate_regime_gate(signal, risk)
    _result_keys_ok(result)
    assert result["long_blocked"] is True
    assert result["short_blocked"] is False
    assert "regime_risk_off" in result["reasons"]
    # The shorts-still-allowed asymmetry must be made visible in notes.
    assert any("shorts still allowed" in n for n in result["notes"])


def test_btc_trend_down_blocks_long_only():
    """A downward BTC trend gates longs; shorts stay open."""
    risk = RiskParams()
    # Neutral regime so only the trend gate triggers.
    signal = MarketSignal(regime=MarketRegime.NEUTRAL, btc_trend=BtcTrend.DOWN)
    result = evaluate_regime_gate(signal, risk)
    _result_keys_ok(result)
    assert result["long_blocked"] is True
    assert result["short_blocked"] is False
    assert "btc_trend_down" in result["reasons"]


def test_market_halt_flag_blocks_both_sides():
    """A hard risk flag (market_halt) blocks longs AND shorts."""
    risk = RiskParams()
    signal = MarketSignal(
        regime=MarketRegime.RISK_ON,   # otherwise-friendly tape
        btc_trend=BtcTrend.UP,
        risk_flags=["market_halt"],
    )
    result = evaluate_regime_gate(signal, risk)
    _result_keys_ok(result)
    assert result["long_blocked"] is True
    assert result["short_blocked"] is True
    assert "risk_flag:market_halt" in result["reasons"]
    # No "shorts still allowed" note when shorts are actually blocked.
    assert not any("shorts still allowed" in n for n in result["notes"])


def test_gate_disabled_blocks_nothing():
    """btc_gate_enabled=False is a no-op: both sides allowed, single note."""
    risk = replace(RiskParams(), btc_gate_enabled=False)
    # Even the worst possible tape must pass through when the gate is off.
    signal = MarketSignal(
        regime=MarketRegime.RISK_OFF,
        btc_trend=BtcTrend.DOWN,
        risk_flags=["market_halt", "extreme_volatility"],
    )
    result = evaluate_regime_gate(signal, risk)
    _result_keys_ok(result)
    assert result["long_blocked"] is False
    assert result["short_blocked"] is False
    assert result["reasons"] == []
    assert "btc_gate_disabled" in result["notes"]


def test_combined_regime_and_trend_and_flag():
    """Edge case: every trigger active at once -> both blocked, all codes present."""
    risk = RiskParams()
    signal = MarketSignal(
        regime=MarketRegime.RISK_OFF,
        btc_trend=BtcTrend.DOWN,
        risk_flags=["extreme_volatility"],
    )
    result = evaluate_regime_gate(signal, risk)
    _result_keys_ok(result)
    assert result["long_blocked"] is True
    assert result["short_blocked"] is True
    assert "regime_risk_off" in result["reasons"]
    assert "btc_trend_down" in result["reasons"]
    assert "risk_flag:extreme_volatility" in result["reasons"]


def test_unknown_flags_are_ignored():
    """Edge case: a risk flag NOT in block_risk_flags must not block anything."""
    risk = RiskParams()
    signal = MarketSignal(
        regime=MarketRegime.RISK_ON,
        btc_trend=BtcTrend.UP,
        risk_flags=["some_benign_note", "low_volume"],
    )
    result = evaluate_regime_gate(signal, risk)
    _result_keys_ok(result)
    assert result["long_blocked"] is False
    assert result["short_blocked"] is False
    assert result["reasons"] == []


def test_duplicate_block_flags_deduped_in_reasons():
    """Edge case: a repeated hard flag yields exactly one reason code."""
    risk = RiskParams()
    signal = MarketSignal(
        regime=MarketRegime.NEUTRAL,
        btc_trend=BtcTrend.FLAT,
        risk_flags=["market_halt", "market_halt"],
    )
    result = evaluate_regime_gate(signal, risk)
    _result_keys_ok(result)
    assert result["reasons"].count("risk_flag:market_halt") == 1
    assert result["long_blocked"] is True
    assert result["short_blocked"] is True


def test_custom_block_set_uses_value_comparison():
    """Configured custom regime/trend strings gate via enum.value comparison."""
    risk = replace(
        RiskParams(),
        btc_gate_block_regimes=("neutral",),
        btc_gate_block_trends=("flat",),
    )
    signal = MarketSignal(regime=MarketRegime.NEUTRAL, btc_trend=BtcTrend.FLAT)
    result = evaluate_regime_gate(signal, risk)
    _result_keys_ok(result)
    assert result["long_blocked"] is True
    assert result["short_blocked"] is False
    assert "regime_neutral" in result["reasons"]
    assert "btc_trend_flat" in result["reasons"]
