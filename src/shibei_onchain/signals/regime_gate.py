"""The BTC-environment / risk-flag gate — enforced, not merely logged.

拾贝 V3.0 computed the BTC environment ("is the macro tape risk-on or risk-off?
is BTC trending down? are there exchange-halt / extreme-volatility flags?") and
*logged* it — but never actually used it to block an open. That log-only gap is
exactly the kind of silent-risk hole this project exists to close. Here the gate
is a **pure, side-effect-free decision function**: given a normalized
``MarketSignal`` and the ``RiskParams`` thresholds, it returns whether new LONG
and/or new SHORT opens are blocked, plus machine-readable reason codes and
human-readable notes.

Design notes:

* Asymmetric by intent. A weak BTC tape (risk_off regime, downward btc_trend)
  blocks **longs only** — shorts are *favoured* when BTC is weak, so a plain
  risk-off / downtrend does not block them. Only a *hard* risk flag (e.g.
  ``market_halt`` / ``extreme_volatility``) — a condition where trading anything
  is unsafe — blocks **both** sides.
* Enum-vs-tuple care. ``RiskParams.btc_gate_block_regimes`` /
  ``btc_gate_block_trends`` / ``block_risk_flags`` are tuples of plain *strings*.
  ``MarketSignal.regime`` / ``btc_trend`` are ``Enum`` members. We compare the
  enum's ``.value`` (e.g. ``MarketRegime.RISK_OFF.value == "risk_off"``) against
  those string tuples — never the enum member itself.
* Pure & total. No I/O, no network, never raises; always returns the four keys.

This module imports only stdlib + frozen model/config types, so it loads with
zero third-party dependencies.
"""

from __future__ import annotations

from typing import Any, Dict, List

from shibei_onchain.config import RiskParams
from shibei_onchain.models import MarketSignal


# Stable reason codes emitted for the common gate triggers. Anything not in this
# map (e.g. a custom configured regime/trend) falls back to a generic
# ``regime_<value>`` / ``btc_trend_<value>`` code so the reason is still visible.
_REGIME_REASON_CODES = {
    "risk_off": "regime_risk_off",
    "neutral": "regime_neutral",
    "risk_on": "regime_risk_on",
    "unknown": "regime_unknown",
}
_TREND_REASON_CODES = {
    "down": "btc_trend_down",
    "flat": "btc_trend_flat",
    "up": "btc_trend_up",
    "unknown": "btc_trend_unknown",
}


def _regime_reason(value: str) -> str:
    return _REGIME_REASON_CODES.get(value, "regime_{}".format(value or "unknown"))


def _trend_reason(value: str) -> str:
    return _TREND_REASON_CODES.get(value, "btc_trend_{}".format(value or "unknown"))


def evaluate_regime_gate(signal: MarketSignal, risk: RiskParams) -> Dict[str, Any]:
    """Decide whether new LONG / SHORT opens are blocked by the BTC environment.

    Parameters
    ----------
    signal:
        Normalized ``MarketSignal`` (regime, btc_trend, risk_flags). Its enum
        fields are compared by ``.value`` against the ``RiskParams`` string
        tuples.
    risk:
        ``RiskParams`` carrying ``btc_gate_enabled``, ``btc_gate_block_regimes``,
        ``btc_gate_block_trends`` and ``block_risk_flags``.

    Returns
    -------
    dict with keys:
        ``long_blocked`` (bool), ``short_blocked`` (bool),
        ``reasons`` (List[str] of machine-readable codes),
        ``notes`` (List[str] of human-readable explanations).

    The gate never raises; on a disabled gate it returns both-allowed with a
    single explanatory note.
    """
    reasons: List[str] = []
    notes: List[str] = []

    # Gate disabled => nothing is blocked. Still report *why* nothing blocked so
    # an operator reading the cycle log sees the gate ran and was a no-op.
    if not risk.btc_gate_enabled:
        notes.append("btc_gate_disabled")
        return {
            "long_blocked": False,
            "short_blocked": False,
            "reasons": reasons,
            "notes": notes,
        }

    long_blocked = False
    short_blocked = False

    # --- regime gate (long-only): e.g. risk_off blocks new longs ------------- #
    block_regimes = tuple(risk.btc_gate_block_regimes or ())
    regime_value = signal.regime.value
    if regime_value in block_regimes:
        long_blocked = True
        code = _regime_reason(regime_value)
        reasons.append(code)
        notes.append(
            "long blocked: BTC regime '{}' is in block set {}".format(
                regime_value, block_regimes
            )
        )

    # --- btc_trend gate (long-only): e.g. down blocks new longs -------------- #
    block_trends = tuple(risk.btc_gate_block_trends or ())
    trend_value = signal.btc_trend.value
    if trend_value in block_trends:
        long_blocked = True
        code = _trend_reason(trend_value)
        reasons.append(code)
        notes.append(
            "long blocked: BTC trend '{}' is in block set {}".format(
                trend_value, block_trends
            )
        )

    # --- hard risk flags (both sides): market_halt / extreme_volatility ------ #
    block_flags = tuple(risk.block_risk_flags or ())
    block_flag_set = {str(f) for f in block_flags}
    # Preserve signal order; de-dup while keeping first occurrence.
    seen = set()
    triggered_flags: List[str] = []
    for raw_flag in signal.risk_flags or ():
        flag = str(raw_flag)
        if flag in block_flag_set and flag not in seen:
            seen.add(flag)
            triggered_flags.append(flag)

    if triggered_flags:
        long_blocked = True
        short_blocked = True
        for flag in triggered_flags:
            reasons.append("risk_flag:{}".format(flag))
        notes.append(
            "both sides blocked: hard risk flag(s) present {} — trading is "
            "unsafe regardless of side".format(triggered_flags)
        )

    # Make the asymmetry explicit whenever longs are gated by a weak tape but no
    # hard flag forces shorts off too (shorts are favoured when BTC is weak).
    if long_blocked and not short_blocked:
        notes.append(
            "shorts still allowed: a weak BTC tape favours shorts; only hard "
            "risk flags block shorts"
        )

    return {
        "long_blocked": long_blocked,
        "short_blocked": short_blocked,
        "reasons": reasons,
        "notes": notes,
    }
