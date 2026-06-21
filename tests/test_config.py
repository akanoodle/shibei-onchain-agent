"""Unit tests for the 汲水 V0.2 strategy switch in config.py.

Hermetic: every case passes an explicit ``source`` mapping to ``load_config`` /
``AgentConfig.from_env`` so no real environment or ``.env`` file is read.
"""

from __future__ import annotations

from shibei_onchain.config import AgentConfig, RiskParams, load_config


def _cfg(**env) -> AgentConfig:
    src = {("SHIBEI_ONCHAIN_" + k): str(v) for k, v in env.items()}
    return load_config(src)


# --------------------------------------------------------------------------- #
# water_v02 strategy → long-only + baked-in time filter
# --------------------------------------------------------------------------- #
def test_water_v02_forces_long_only_even_on_aster():
    # On V3.0, venue=aster defaults the short leg ON; V0.2 must force it OFF.
    cfg = _cfg(STRATEGY="water_v02", VENUE="aster")
    assert cfg.strategy == "water_v02"
    assert cfg.enable_short_leg is False
    assert cfg.venue == "aster"


def test_water_v02_bakes_in_beijing_time_filter():
    cfg = _cfg(STRATEGY="water_v02")
    assert cfg.risk.excluded_entry_beijing_hours == tuple(range(8, 16))


def test_water_v02_time_filter_is_overridable_to_empty():
    # Demo override: explicit empty string disables the filter.
    cfg = _cfg(STRATEGY="water_v02", EXCLUDED_ENTRY_BEIJING_HOURS="")
    assert cfg.risk.excluded_entry_beijing_hours == ()


def test_water_v02_short_leg_still_explicitly_overridable():
    cfg = _cfg(STRATEGY="water_v02", VENUE="aster", ENABLE_SHORT_LEG="true")
    assert cfg.enable_short_leg is True


# --------------------------------------------------------------------------- #
# v3 strategy → unchanged long+short behaviour
# --------------------------------------------------------------------------- #
def test_v3_default_keeps_aster_short_leg_on():
    cfg = _cfg(VENUE="aster")              # strategy defaults to v3
    assert cfg.strategy == "v3"
    assert cfg.enable_short_leg is True
    assert cfg.risk.excluded_entry_beijing_hours == ()


def test_v3_pancake_short_leg_off_by_default():
    cfg = _cfg(VENUE="pancake")
    assert cfg.enable_short_leg is False


def test_explicit_excluded_hours_parse_and_sort():
    cfg = _cfg(EXCLUDED_ENTRY_BEIJING_HOURS="15, 8 ,12,99,foo")
    # 99 and 'foo' dropped; rest sorted+deduped.
    assert cfg.risk.excluded_entry_beijing_hours == (8, 12, 15)


def test_redacted_exposes_strategy_and_window():
    cfg = _cfg(STRATEGY="water_v02")
    red = cfg.redacted()
    assert red["strategy"] == "water_v02"
    assert red["risk"]["excluded_entry_beijing_hours"] == list(range(8, 16))


def test_riskparams_from_env_strategy_default():
    # RiskParams.from_env honours the strategy kwarg for the window default.
    assert RiskParams.from_env({}, strategy="water_v02").excluded_entry_beijing_hours == tuple(range(8, 16))
    assert RiskParams.from_env({}, strategy="v3").excluded_entry_beijing_hours == ()
