"""Tests for the offline backtest engine (deterministic, no network)."""

from __future__ import annotations

from shibei_onchain.config import load_config
from shibei_onchain.backtest import make_scenario, run_backtest, load_panel


def _cfg():
    return load_config({"SHIBEI_ONCHAIN_STRATEGY": "water_v02",
                        "SHIBEI_ONCHAIN_BOARD_MODE": "off",
                        "SHIBEI_ONCHAIN_CMC_MODE": "mock"})


def test_scenarios_are_deterministic():
    a = run_backtest(make_scenario("bull"), _cfg()).metrics()
    b = run_backtest(make_scenario("bull"), _cfg()).metrics()
    assert a == b                      # byte-identical, reproducible for judges


def test_all_scenarios_run_and_report():
    for sc in ("bull", "chop", "crash", "deepv"):
        r = run_backtest(make_scenario(sc), _cfg())
        m = r.metrics()
        assert m["bars"] == 60
        assert m["initial_equity"] == 1000.0
        assert len(r.equity_curve) == 60
        assert isinstance(m["total_return_pct"], float)
        assert m["max_drawdown_pct"] >= 0.0


def test_bull_beats_crash():
    bull = run_backtest(make_scenario("bull"), _cfg()).total_return_pct
    crash = run_backtest(make_scenario("crash"), _cfg()).total_return_pct
    assert bull > crash                # uptrend longs out-perform a crash


def test_custom_panel_via_dict():
    panel = {"name": "flat", "interval_hours": 24, "start": "2026-02-01T12:00:00Z",
             "closes": {"BTCUSDT": [100.0] * 20, "ETHUSDT": [50.0] * 20}}
    r = run_backtest(panel, _cfg())
    assert r.bars == 20
    # flat prices -> no +1R, eventually data-end/max-hold; never raises
    assert isinstance(r.metrics()["total_return_pct"], float)


def test_load_panel_rejects_bad(tmp_path):
    import json
    import pytest
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"nope": 1}))
    with pytest.raises(ValueError):
        load_panel(str(p))
