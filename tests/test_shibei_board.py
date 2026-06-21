"""Tests for the 拾贝 board client mapping (offline; no network)."""

from __future__ import annotations

from shibei_onchain.config import BoardConfig
from shibei_onchain.signals.shibei_board import ShibeiBoardClient


_SAMPLE = {
    "schema_version": "public_leaderboard_latest/v1",
    "as_of": "2026-06-16T02:44:59Z",
    "run_id": "20260616T024459Z",
    "tracks": {
        "main_track": [
            {"symbol": "ETHUSDT", "rank": 1, "score": 87.2, "current_price": 1775.0,
             "vol24h_usd": 1.2e9, "oi_usd": 9e8, "relative_strength_score": 65.0,
             "matched_tracks": ["main_track", "fast_track"]},
            {"symbol": "BTCUSDT", "rank": 2, "score": 81.4, "current_price": 66000.0,
             "relative_strength_score": 58.0, "matched_tracks": ["main_track"]},
        ],
        "fast_track": [
            # ETH also appears here with a lower score -> must dedupe to the main entry
            {"symbol": "ETHUSDT", "rank": 3, "score": 40.0, "current_price": 1775.0,
             "matched_tracks": ["fast_track"]},
            {"symbol": "PENGUUSDT", "rank": 1, "score": 43.0, "current_price": 0.03,
             "matched_tracks": ["fast_track"]},
        ],
        "strong_supplement": [
            {"symbol": "EVAAUSDT", "rank": 1, "score": 100.0, "current_price": 1.5,
             "matched_tracks": ["strong_supplement"]},
        ],
    },
}


class _StubClient(ShibeiBoardClient):
    """Override the HTTP layer to return a fixed payload — no network."""

    def __init__(self, board, payload):
        super().__init__(board)
        self._payload = payload

    def _fetch_payload(self):
        return self._payload


def _board():
    return BoardConfig(mode="api", username="x", password="y")


def test_flatten_dedupe_and_map():
    rows = _StubClient(_board(), _SAMPLE).fetch_rows()
    by_sym = {r["symbol"]: r for r in rows}
    # all symbols surfaced, ETH de-duplicated to its strongest (main) appearance
    assert set(by_sym) == {"ETHUSDT", "BTCUSDT", "PENGUUSDT", "EVAAUSDT"}
    assert by_sym["ETHUSDT"]["score"] == 87.2            # not the fast 40.0
    assert by_sym["ETHUSDT"]["price"] == 1775.0          # current_price -> price
    assert by_sym["ETHUSDT"]["relative_strength_score"] == 65.0
    assert by_sym["ETHUSDT"]["side"] == "long"
    # board provenance merged across tracks
    assert "fast_track" in by_sym["ETHUSDT"]["source_boards"]
    assert "main_track" in by_sym["ETHUSDT"]["source_boards"]


def test_sorted_by_score_desc():
    rows = _StubClient(_board(), _SAMPLE).fetch_rows()
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    assert rows[0]["symbol"] == "EVAAUSDT"               # 100.0


def test_api_error_payload_returns_empty():
    rows = _StubClient(_board(), {"error": {"code": "auth_required"}}).fetch_rows()
    assert rows == []


def test_missing_tracks_returns_empty():
    assert _StubClient(_board(), {"schema_version": "x"}).fetch_rows() == []


def test_health_never_leaks_password():
    c = ShibeiBoardClient(BoardConfig(mode="api", username="boarduser", password="secret123"))
    h = c.health()
    assert "secret123" not in str(h)
    assert h["auth"] == "user:boarduser"


def test_off_mode_via_scanner_does_not_call_board():
    # When board mode is off, the scanner's board loader returns None (no network).
    from shibei_onchain.config import load_config
    from shibei_onchain.brain.scanner import Scanner

    cfg = load_config({"SHIBEI_ONCHAIN_BOARD_MODE": "off"})
    scanner = Scanner(cfg)
    assert scanner._load_board_rows(scanner._load_universe()) is None
