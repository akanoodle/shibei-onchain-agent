"""Unit tests for brain/scanner.py — pure MOCK mode (no network, no deps).

These tests force the deterministic offline path by:
  * pointing state_dir at an empty temp dir (no latest.json), and
  * disabling the live CMC source so scan() falls through to the mock.

Everything here runs on the stdlib alone and is deterministic.
"""

from __future__ import annotations

import json
import os

import pytest

from shibei_onchain.config import AgentConfig
from shibei_onchain.models import Candidate, Side
from shibei_onchain.brain.scanner import (
    Scanner,
    STRATEGY_ID,
    LONG_LEG,
    SHORT_LEG,
    _STOP_DISTANCE_PCT,
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def mock_config(tmp_path):
    """An AgentConfig wired to an empty state dir + the real tokens registry."""
    cfg = AgentConfig()
    cfg.state_dir = str(tmp_path / "state")  # empty -> no latest.json
    os.makedirs(cfg.state_dir, exist_ok=True)
    # use the testnet (chain_id 97) universe in the repo registry.
    return cfg


def _force_mock(scanner: Scanner) -> None:
    """Disable the live CMC source so scan() falls through to the mock."""
    scanner._load_cmc_rows = lambda *a, **k: None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_scan_returns_ranked_long_candidates(mock_config):
    scanner = Scanner(mock_config)
    _force_mock(scanner)

    candidates = scanner.scan()

    assert scanner.last_source == "mock"
    assert isinstance(candidates, list)
    assert candidates, "mock scan should yield candidates"
    assert all(isinstance(c, Candidate) for c in candidates)

    # default config disables the short leg -> only LONG candidates.
    assert all(c.side is Side.LONG for c in candidates)

    # ranking is descending by score.
    scores = [c.score for c in candidates]
    assert scores == sorted(scores, reverse=True)


def test_candidate_fields_are_well_formed(mock_config):
    scanner = Scanner(mock_config)
    _force_mock(scanner)

    candidates = scanner.scan()
    c = candidates[0]

    # identity / strategy fields.
    assert c.strategy_id == STRATEGY_ID
    assert c.strategy_leg == LONG_LEG
    assert c.signal_key.startswith(STRATEGY_ID)
    assert c.source_boards == [c.symbol]
    assert c.metrics.get("source") == "mock"
    assert c.metrics.get("mock") is True

    # risk fields pulled from RiskParams defaults.
    assert c.risk_per_trade_pct == mock_config.risk.long_risk_pct
    assert c.take_profit_r_multiple == mock_config.risk.take_profit_r
    assert c.breakeven_r_multiple == mock_config.risk.long_breakeven_r

    # prices: positive entry, long stop ~6% below entry.
    assert c.price > 0
    assert c.stop_loss_price > 0
    assert c.stop_loss_price < c.price
    expected_stop = c.price * (1.0 - _STOP_DISTANCE_PCT)
    assert c.stop_loss_price == pytest.approx(expected_stop, rel=1e-6)
    # stop_distance_pct derived from the candidate matches the configured gap.
    assert c.stop_distance_pct == pytest.approx(_STOP_DISTANCE_PCT, rel=1e-6)


def test_sensible_major_prices(mock_config):
    scanner = Scanner(mock_config)
    _force_mock(scanner)

    by_base = {c.base_asset: c for c in scanner.scan()}
    # testnet registry has BNB/ETH/BTC/CAKE.
    assert "BNB" in by_base and by_base["BNB"].price == pytest.approx(600.0)
    assert "BTC" in by_base and by_base["BTC"].price == pytest.approx(65000.0)
    assert "ETH" in by_base and by_base["ETH"].price == pytest.approx(3000.0)
    assert "CAKE" in by_base and by_base["CAKE"].price == pytest.approx(2.5)


def test_max_candidates_respected(mock_config):
    mock_config.max_candidates = 2
    scanner = Scanner(mock_config)
    _force_mock(scanner)

    candidates = scanner.scan()
    assert len(candidates) == 2


def test_determinism(mock_config):
    s1 = Scanner(mock_config)
    _force_mock(s1)
    s2 = Scanner(mock_config)
    _force_mock(s2)

    out1 = [(c.symbol, c.side, c.score, c.price) for c in s1.scan()]
    out2 = [(c.symbol, c.side, c.score, c.price) for c in s2.scan()]
    assert out1 == out2


def test_short_leg_only_when_enabled(mock_config):
    mock_config.enable_short_leg = True
    mock_config.max_candidates = 64
    scanner = Scanner(mock_config)
    _force_mock(scanner)

    candidates = scanner.scan()
    shorts = [c for c in candidates if c.side is Side.SHORT]
    # short leg enabled -> at least one short candidate is produced.
    assert shorts, "enabling the short leg should yield short candidates"
    for s in shorts:
        assert s.strategy_leg == SHORT_LEG
        # short stop sits ABOVE entry.
        assert s.stop_loss_price > s.price


def test_latest_json_source_precedence(mock_config):
    """A latest.json board ranking takes precedence over the mock path."""
    latest = {
        "candidates": [
            {"symbol": "BTCUSDT", "price": 70000.0, "score": 99.0},
            {"symbol": "CAKEUSDT", "price": 3.0, "score": 80.0},
            # not in the on-chain universe -> must be dropped.
            {"symbol": "PEPEUSDT", "price": 0.000001, "score": 88.0},
        ]
    }
    path = os.path.join(mock_config.state_dir, "latest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(latest, fh)

    scanner = Scanner(mock_config)
    _force_mock(scanner)  # also block live source; latest.json should win anyway

    candidates = scanner.scan()
    assert scanner.last_source == "latest_json"

    bases = [c.base_asset for c in candidates]
    assert "BTC" in bases
    assert "CAKE" in bases
    # PEPE is not in the testnet registry, so it is filtered out.
    assert "PEPE" not in bases

    btc = next(c for c in candidates if c.base_asset == "BTC")
    assert btc.price == pytest.approx(70000.0)
    assert btc.metrics.get("source") == "latest_json"
    # BTC ranked highest by score.
    assert candidates[0].base_asset == "BTC"


def test_never_raises_on_bad_tokens_path(tmp_path):
    cfg = AgentConfig()
    cfg.state_dir = str(tmp_path)
    cfg.onchain.tokens_path = str(tmp_path / "does_not_exist.json")
    scanner = Scanner(cfg)
    _force_mock(scanner)

    # falls back to built-in majors; must not raise and must produce candidates.
    candidates = scanner.scan()
    assert candidates
    bases = {c.base_asset for c in candidates}
    assert {"BNB", "ETH", "BTC", "CAKE"}.issubset(bases)


def test_bad_latest_json_falls_through(mock_config):
    """Corrupt latest.json must not crash; scanner degrades to mock."""
    path = os.path.join(mock_config.state_dir, "latest.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ this is not valid json ")

    scanner = Scanner(mock_config)
    _force_mock(scanner)

    candidates = scanner.scan()
    assert scanner.last_source == "mock"
    assert candidates


# --------------------------------------------------------------------------- #
# CMC fallback source (replaces the old Binance klines source)
# --------------------------------------------------------------------------- #
def test_cmc_fallback_disabled_in_mock_mode(mock_config):
    # default config has cmc.mode == "mock" -> _load_cmc_rows returns None,
    # so scan() lands on the deterministic offline mock (no CEX API anywhere).
    scanner = Scanner(mock_config)
    rows = scanner._load_cmc_rows(scanner._load_universe())
    assert rows is None
    assert scanner.scan() and scanner.last_source == "mock"


def test_scanner_has_no_binance_source():
    # the Binance klines source is gone — only CMC + board + latest.json + mock.
    assert not hasattr(Scanner, "_load_binance_rows")
    assert not hasattr(Scanner, "_http_getter")
    import shibei_onchain.brain.scanner as sc
    assert not hasattr(sc, "_BINANCE_KLINES_URL")
