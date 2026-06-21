"""Feishu (Lark) custom-bot webhook delivery for the on-chain agent.

拾贝's operating discipline is *failures must be visible*. This module is the
"make it visible to a human" leg: a compact, signed Feishu card that summarises
each agent cycle (market regime, equity, fills, skips, sample tx hashes).

Design constraints (shared by every module in this project):

* **Python 3.9** — annotations only, no runtime ``X | Y`` unions, no ``match``.
* **No hard third-party deps at import time.** ``requests`` is imported lazily
  inside the POST helper; if it is missing we fall back to ``urllib`` from the
  stdlib. The module therefore imports fine in a bare interpreter.
* **Never raise.** Any network / encoding / dependency failure degrades to a
  recorded ``{"status": ...}`` dict; the trading cycle is never crashed by a
  notification problem.
* **Never log secrets.** The webhook secret is used only to compute the HMAC
  signature; it is never placed in a payload field, returned, or logged.

Signing model
-------------
Feishu custom bots with "签名校验" (signature verification) enabled expect:

    string_to_sign = f"{timestamp}\\n{secret}"
    sign = base64( HMAC_SHA256(key=string_to_sign, msg=b"") )

i.e. the *secret* is the HMAC key material baked into ``string_to_sign`` and the
message body is empty. ``timestamp`` is a unix-second integer and is also sent
in the JSON body as ``"timestamp"``. We follow that scheme exactly so the card
verifies against a real Feishu bot, while keeping the whole thing testable
offline (the builder is pure and deterministic given a fixed timestamp).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, List, Optional

from shibei_onchain.config import AgentConfig


# Feishu interactive-card colour templates keyed by our log levels.
_LEVEL_TEMPLATE = {
    "info": "blue",
    "success": "green",
    "warn": "orange",
    "warning": "orange",
    "error": "red",
}

_DISABLED_RESULT = {"status": "skipped", "reason": "disabled_or_no_webhook"}


class FeishuNotifier:
    """Signed Feishu custom-bot webhook notifier.

    When ``enabled`` is False or ``webhook_url`` is empty, every public method
    short-circuits to ``{"status": "skipped", "reason": "disabled_or_no_webhook"}``
    *without performing any network call* — this is the default posture so the
    agent never depends on an outbound notification path being configured.
    """

    def __init__(
        self,
        webhook_url: str = "",
        secret: str = "",
        enabled: bool = False,
        *,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.webhook_url = (webhook_url or "").strip()
        self._secret = (secret or "").strip()
        self.enabled = bool(enabled)
        self.timeout_seconds = float(timeout_seconds)

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    @classmethod
    def from_config(cls, config: AgentConfig) -> "FeishuNotifier":
        """Build from the ``feishu_*`` fields on :class:`AgentConfig`."""
        return cls(
            webhook_url=getattr(config, "feishu_webhook_url", "") or "",
            secret=getattr(config, "feishu_webhook_secret", "") or "",
            enabled=bool(getattr(config, "feishu_enabled", False)),
        )

    # ------------------------------------------------------------------ #
    # status helpers
    # ------------------------------------------------------------------ #
    @property
    def active(self) -> bool:
        """True only when a real POST would be attempted."""
        return self.enabled and bool(self.webhook_url)

    # ------------------------------------------------------------------ #
    # signing + payload building (pure, deterministic, no network)
    # ------------------------------------------------------------------ #
    def _sign(self, timestamp: int) -> str:
        """Compute the Feishu HMAC-SHA256 signature for ``timestamp``.

        Returns an empty string when no secret is configured (Feishu bots
        without signature verification accept an unsigned body).
        """
        if not self._secret:
            return ""
        string_to_sign = "{}\n{}".format(timestamp, self._secret)
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def build_card(self, title: str, lines: List[str], *, level: str = "info") -> Dict[str, Any]:
        """Build the interactive-card body (no timestamp / signature)."""
        template = _LEVEL_TEMPLATE.get((level or "info").lower(), "blue")
        safe_lines = [str(ln) for ln in (lines or [])]
        # Feishu lark_md content: join lines with newlines into one text block.
        content = "\n".join(safe_lines) if safe_lines else "(no detail)"
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": template,
                    "title": {"tag": "plain_text", "content": str(title)},
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": content},
                    }
                ],
            },
        }

    def build_payload(
        self,
        title: str,
        lines: List[str],
        *,
        level: str = "info",
        timestamp: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Build the full signed request body sent to the webhook.

        Deterministic given a fixed ``timestamp`` — this is the unit under test
        for the signing/payload behaviour and performs no network I/O.
        """
        ts = int(timestamp) if timestamp is not None else int(time.time())
        payload = self.build_card(title, lines, level=level)
        if self._secret:
            payload["timestamp"] = str(ts)
            payload["sign"] = self._sign(ts)
        return payload

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #
    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST ``payload`` to the webhook, lazy ``requests`` -> ``urllib``.

        Never raises: any transport / encoding / dependency failure is mapped
        to ``{"status": "error", "reason": <code>, "detail": <str>}``.
        """
        try:
            body = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            return {"status": "error", "reason": "encode_failed", "detail": str(exc)}

        headers = {"Content-Type": "application/json"}

        # --- preferred: requests (lazy import) ---
        try:
            import requests  # type: ignore
        except Exception:  # noqa: BLE001 - requests simply not installed
            requests = None  # type: ignore

        if requests is not None:
            try:
                resp = requests.post(
                    self.webhook_url,
                    data=body,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
                return self._interpret_response(resp.status_code, resp.text)
            except Exception as exc:  # noqa: BLE001 - degrade, never crash
                return {"status": "error", "reason": "request_failed", "detail": str(exc)}

        # --- fallback: stdlib urllib ---
        try:
            import urllib.error
            import urllib.request

            req = urllib.request.Request(
                self.webhook_url, data=body, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                text = resp.read().decode("utf-8", "replace")
                status_code = getattr(resp, "status", 200) or 200
                return self._interpret_response(status_code, text)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash
            return {"status": "error", "reason": "request_failed", "detail": str(exc)}

    @staticmethod
    def _interpret_response(status_code: int, text: str) -> Dict[str, Any]:
        """Map an HTTP response into a result dict.

        Feishu returns ``{"code":0,...}`` on success (HTTP 200) and a non-zero
        ``code`` on application-level rejection (e.g. bad signature).
        """
        result: Dict[str, Any] = {"http_status": int(status_code)}
        feishu_code: Optional[int] = None
        feishu_msg = ""
        try:
            data = json.loads(text) if text else {}
            if isinstance(data, dict):
                feishu_code = data.get("code")
                feishu_msg = str(data.get("msg", ""))
        except (TypeError, ValueError):
            data = {}

        if 200 <= int(status_code) < 300 and (feishu_code in (None, 0)):
            result.update({"status": "sent"})
        else:
            result.update(
                {
                    "status": "error",
                    "reason": "feishu_rejected",
                    "feishu_code": feishu_code,
                    "feishu_msg": feishu_msg,
                }
            )
        return result

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def notify(self, title: str, lines: List[str], *, level: str = "info") -> Dict[str, Any]:
        """Send a titled, multi-line card. Skips silently when disabled."""
        if not self.active:
            return dict(_DISABLED_RESULT)
        try:
            payload = self.build_payload(title, lines, level=level)
        except Exception as exc:  # noqa: BLE001 - never raise out of notify
            return {"status": "error", "reason": "build_failed", "detail": str(exc)}
        return self._post(payload)

    def notify_cycle(self, *, signal, account, receipts, skipped) -> Dict[str, Any]:
        """Format and send a compact per-cycle summary card.

        Pulls a human-readable digest out of the cycle objects defensively
        (every field access is guarded so a partial / odd object can never
        crash the notification): market regime + BTC trend, equity, number of
        fills vs skips, and a few sample tx hashes.
        """
        if not self.active:
            return dict(_DISABLED_RESULT)
        try:
            lines, level = self._cycle_lines(signal, account, receipts, skipped)
            payload = self.build_payload("拾贝 On-Chain · 周期播报", lines, level=level)
        except Exception as exc:  # noqa: BLE001 - never raise out of notify_cycle
            return {"status": "error", "reason": "build_failed", "detail": str(exc)}
        return self._post(payload)

    # ------------------------------------------------------------------ #
    # cycle digest (pure helper, deterministic, network-free)
    # ------------------------------------------------------------------ #
    def _cycle_lines(self, signal, account, receipts, skipped):
        """Return ``(lines, level)`` describing the cycle. Pure / safe."""
        receipts = list(receipts or [])
        skipped = list(skipped or [])

        # --- signal digest ---
        regime = self._enum_value(getattr(signal, "regime", None), "unknown")
        btc_trend = self._enum_value(getattr(signal, "btc_trend", None), "unknown")
        sig_source = getattr(signal, "source", "") if signal is not None else ""

        # --- account digest ---
        equity = self._fnum(getattr(account, "equity_usd", None))
        gas = self._fnum(getattr(account, "gas_balance", None))
        positions = getattr(account, "positions", None) or []
        n_positions = len(positions)

        # --- receipt classification ---
        n_filled = 0
        n_dry = 0
        n_failed = 0
        n_recv_skipped = 0
        tx_hashes: List[str] = []
        for r in receipts:
            status = str(getattr(r, "status", "") or "").lower()
            if status == "filled":
                n_filled += 1
            elif status == "dry_run":
                n_dry += 1
            elif status == "failed":
                n_failed += 1
            elif status == "skipped":
                n_recv_skipped += 1
            tx = str(getattr(r, "tx_hash", "") or "")
            if tx:
                tx_hashes.append(tx)

        n_skips = len(skipped) + n_recv_skipped

        # severity: any failed -> error, any skip -> warn, else info/success.
        if n_failed:
            level = "error"
        elif n_skips:
            level = "warn"
        elif n_filled or n_dry:
            level = "success"
        else:
            level = "info"

        lines = [
            "**环境**: regime=`{}` · btc=`{}` · src=`{}`".format(
                regime, btc_trend, sig_source or "n/a"
            ),
            "**权益**: ${:,.2f} · gas={:.4f} BNB · 持仓={}".format(
                equity, gas, n_positions
            ),
            "**成交**: filled={} · dry_run={} · failed={}".format(
                n_filled, n_dry, n_failed
            ),
            "**跳过**: {} 单".format(n_skips),
        ]

        # sample tx hashes (cap at 3, shorten the middle for readability)
        if tx_hashes:
            sample = [self._short_hash(h) for h in tx_hashes[:3]]
            more = " (+{})".format(len(tx_hashes) - 3) if len(tx_hashes) > 3 else ""
            lines.append("**TX**: " + ", ".join("`{}`".format(s) for s in sample) + more)

        # surface up to two skip reasons for quick triage
        skip_reasons = [
            str(getattr(s, "reason", "") or "") for s in skipped if getattr(s, "reason", "")
        ]
        if skip_reasons:
            uniq = []
            for reason in skip_reasons:
                if reason not in uniq:
                    uniq.append(reason)
            lines.append("**跳过原因**: " + ", ".join("`{}`".format(r) for r in uniq[:3]))

        return lines, level

    # ------------------------------------------------------------------ #
    # tiny formatting helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _enum_value(obj: Any, default: str) -> str:
        if obj is None:
            return default
        # str-Enum members carry ``.value``; plain strings/ints pass through.
        return str(getattr(obj, "value", obj))

    @staticmethod
    def _fnum(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _short_hash(h: str) -> str:
        h = str(h)
        if len(h) <= 14:
            return h
        return "{}…{}".format(h[:8], h[-6:])
