"""拾贝 leaderboard (榜单) client — the REAL ranking feed.

拾贝 V3.0 publishes its live board (the alpha: main_track / fast_track /
strong_supplement, each a ranked list of symbols) over a read-only HTTP API.
This client pulls it, handling the cookie-session login that production requires
off-box, and flattens the three tracks into the flat row shape the on-chain
:class:`Scanner` already consumes.

Auth: production runs with ``MONITOR_WEB_AUTH_ENABLED=true`` and exempts
``/api/public/v1/*`` only for localhost, so a remote caller logs in
(``POST /api/auth/login`` with username/password → ``monitor_auth`` cookie) and
reuses the cookie. Credentials come from env (:class:`BoardConfig`), never the
repo, and are never logged.

Failure contract: ``fetch_rows`` never raises — on any login / network / parse
error it returns ``[]`` and the scanner falls back to its self-contained chain
(latest.json → Binance public klines → mock).

Per the public projection, each leaderboard entry is already flat:
``{symbol, rank, score, current_price, vol24h_usd, oi_usd, relative_strength_score,
relative_strength_label, matched_tracks, also_fast_signal, ...}`` — so this maps
``current_price → price`` and ``matched_tracks → source_boards`` and emits a LONG
row (the public board is the momentum-strong / long signal; the 拾贝 short leg
S16-A is a separate source not exposed here).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from shibei_onchain.config import BoardConfig


class ShibeiBoardClient:
    def __init__(self, board: BoardConfig) -> None:
        self._cfg = board
        self._notes: List[str] = []

    # ------------------------------------------------------------------ #
    def fetch_rows(self) -> List[Dict[str, Any]]:
        """Return flattened, de-duplicated LONG ranking rows (or ``[]``)."""
        try:
            payload = self._fetch_payload()
        except Exception as exc:  # noqa: BLE001 - never raise out of the loader
            self._note("board_fetch_failed:" + type(exc).__name__)
            return []
        if not isinstance(payload, dict):
            return []
        if payload.get("error"):
            self._note("board_api_error:" + str(payload.get("error"))[:80])
            return []
        return self._flatten(payload)

    def health(self) -> Dict[str, Any]:
        return {
            "mode": self._cfg.mode,
            "url": self._cfg.base_url + self._cfg.leaderboard_path,
            "auth": ("user:" + self._cfg.username) if self._cfg.username else "(none)",
            "notes": list(self._notes),
        }

    # ------------------------------------------------------------------ #
    # mapping: tracks dict -> flat de-duplicated rows
    # ------------------------------------------------------------------ #
    def _flatten(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        tracks = payload.get("tracks") if isinstance(payload.get("tracks"), dict) else {}
        wanted = self._cfg.tracks or ("main_track", "fast_track", "strong_supplement")
        by_symbol: Dict[str, Dict[str, Any]] = {}
        for track_name in wanted:
            entries = tracks.get(track_name)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                sym = str(entry.get("symbol") or "").upper()
                if not sym:
                    continue
                row = self._entry_to_row(entry, track_name)
                existing = by_symbol.get(sym)
                if existing is None or row["score"] > existing["score"]:
                    # keep the strongest appearance; merge board provenance
                    boards = set(existing["source_boards"]) if existing else set()
                    boards.update(row["source_boards"])
                    row["source_boards"] = sorted(boards)
                    by_symbol[sym] = row
                else:
                    existing["source_boards"] = sorted(
                        set(existing["source_boards"]) | set(row["source_boards"])
                    )
        rows = list(by_symbol.values())
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows

    @staticmethod
    def _entry_to_row(entry: Dict[str, Any], track_name: str) -> Dict[str, Any]:
        matched = entry.get("matched_tracks")
        boards = [str(t) for t in matched] if isinstance(matched, list) and matched else [track_name]
        return {
            "symbol": str(entry.get("symbol") or "").upper(),
            "side": "long",                       # public board = momentum-strong = long
            "price": _f(entry.get("current_price")),
            "score": _f(entry.get("score")),
            "relative_strength_score": _f(entry.get("relative_strength_score")),
            "source_boards": boards,
            "rank": entry.get("rank"),
            "track": track_name,
            "oi_usd": _f(entry.get("oi_usd")),
            "vol24h_usd": _f(entry.get("vol24h_usd")),
        }

    # ------------------------------------------------------------------ #
    # HTTP (lazy requests, urllib fallback) with cookie-session login
    # ------------------------------------------------------------------ #
    def _leaderboard_url(self) -> str:
        c = self._cfg
        return (
            c.base_url + c.leaderboard_path
            + "?timeframe=" + (c.timeframe or "live")
            + "&limit=" + str(int(c.limit))
        )

    def _fetch_payload(self) -> Optional[Dict[str, Any]]:
        try:
            import requests  # lazy
        except Exception:  # noqa: BLE001 - fall back to urllib
            return self._fetch_payload_urllib()
        session = requests.Session()
        if self._cfg.username:
            resp = session.post(
                self._cfg.base_url + self._cfg.login_path,
                json={"username": self._cfg.username, "password": self._cfg.password},
                timeout=self._cfg.timeout_seconds,
            )
            if resp.status_code >= 400:
                self._note("login_http_%s" % resp.status_code)
                return None
        resp = session.get(self._leaderboard_url(), timeout=self._cfg.timeout_seconds)
        if resp.status_code >= 400:
            self._note("board_http_%s" % resp.status_code)
            try:
                return resp.json()
            except Exception:  # noqa: BLE001
                return None
        return resp.json()

    def _fetch_payload_urllib(self) -> Optional[Dict[str, Any]]:
        from http.cookiejar import CookieJar
        from urllib import request as _req

        jar = CookieJar()
        opener = _req.build_opener(_req.HTTPCookieProcessor(jar))
        if self._cfg.username:
            body = json.dumps(
                {"username": self._cfg.username, "password": self._cfg.password}
            ).encode("utf-8")
            login_req = _req.Request(
                self._cfg.base_url + self._cfg.login_path,
                data=body,
                headers={"Content-Type": "application/json"},
            )
            try:
                opener.open(login_req, timeout=self._cfg.timeout_seconds).read()
            except Exception as exc:  # noqa: BLE001
                self._note("login_failed:" + type(exc).__name__)
                return None
        try:
            with opener.open(self._leaderboard_url(), timeout=self._cfg.timeout_seconds) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._note("board_request_failed:" + type(exc).__name__)
            return None

    def _note(self, msg: str) -> None:
        if msg not in self._notes:
            self._notes.append(msg)
            if len(self._notes) > 30:
                self._notes.pop(0)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
