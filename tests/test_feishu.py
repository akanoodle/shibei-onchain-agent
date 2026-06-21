"""Unit tests for delivery/feishu.py — pure mock mode, no network, no deps.

Covered behaviours:
  * disabled / empty-webhook short-circuit returns the skipped dict and makes
    NO network call (we sabotage ``_post`` to prove it is never invoked).
  * signature builder is correct vs an independent HMAC-SHA256 computation and
    is deterministic for a fixed timestamp.
  * payload card structure (header template by level, joined lines).
  * notify_cycle digest extraction (fills / skips / regime / tx samples) built
    from lightweight stand-in objects, again with no real POST.
  * edge case: notify_cycle with empty/None inputs and no secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, List

import pytest

from shibei_onchain.config import AgentConfig
from shibei_onchain.delivery.feishu import FeishuNotifier
from shibei_onchain.models import (
    AccountState,
    BtcTrend,
    ExecutionReceipt,
    MarketRegime,
    MarketSignal,
    OrderAction,
    Side,
    SkippedOrder,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _expected_sign(timestamp: int, secret: str) -> str:
    string_to_sign = "{}\n{}".format(timestamp, secret)
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class _ExplodingPost:
    """Sentinel: if transport is ever reached in a disabled test, fail loudly."""

    def __init__(self):
        self.called = False

    def __call__(self, *args, **kwargs):
        self.called = True
        raise AssertionError("network transport (_post) must not run when disabled")


# --------------------------------------------------------------------------- #
# disabled path
# --------------------------------------------------------------------------- #
def test_disabled_skips_without_network():
    notifier = FeishuNotifier(webhook_url="https://example.com/hook", secret="s", enabled=False)
    notifier._post = _ExplodingPost()  # type: ignore[assignment]

    res = notifier.notify("t", ["a", "b"])
    assert res == {"status": "skipped", "reason": "disabled_or_no_webhook"}
    assert notifier._post.called is False
    assert notifier.active is False


def test_empty_webhook_skips_even_when_enabled():
    notifier = FeishuNotifier(webhook_url="", secret="s", enabled=True)
    notifier._post = _ExplodingPost()  # type: ignore[assignment]

    res = notifier.notify_cycle(signal=None, account=None, receipts=[], skipped=[])
    assert res == {"status": "skipped", "reason": "disabled_or_no_webhook"}
    assert notifier._post.called is False
    assert notifier.active is False


def test_from_config_defaults_to_disabled():
    cfg = AgentConfig()  # feishu_enabled defaults False, webhook empty
    notifier = FeishuNotifier.from_config(cfg)
    assert notifier.enabled is False
    assert notifier.active is False
    assert notifier.notify("x", ["y"]) == {
        "status": "skipped",
        "reason": "disabled_or_no_webhook",
    }


def test_from_config_reads_feishu_fields():
    cfg = AgentConfig(
        feishu_enabled=True,
        feishu_webhook_url="https://open.feishu.cn/hook/abc",
        feishu_webhook_secret="topsecret",
    )
    notifier = FeishuNotifier.from_config(cfg)
    assert notifier.enabled is True
    assert notifier.webhook_url == "https://open.feishu.cn/hook/abc"
    assert notifier.active is True


# --------------------------------------------------------------------------- #
# signature + payload builder (isolation, no POST)
# --------------------------------------------------------------------------- #
def test_sign_matches_independent_hmac_and_is_deterministic():
    notifier = FeishuNotifier(secret="my-secret")
    ts = 1718000000
    sig = notifier._sign(ts)
    assert sig == _expected_sign(ts, "my-secret")
    # deterministic
    assert notifier._sign(ts) == sig
    # different timestamp -> different signature
    assert notifier._sign(ts + 1) != sig


def test_sign_empty_without_secret():
    notifier = FeishuNotifier(secret="")
    assert notifier._sign(123) == ""


def test_build_payload_with_secret_includes_timestamp_and_sign():
    notifier = FeishuNotifier(
        webhook_url="https://x/hook", secret="abc", enabled=True
    )
    ts = 1700000000
    payload = notifier.build_payload("Title", ["line1", "line2"], level="error", timestamp=ts)

    assert payload["msg_type"] == "interactive"
    assert payload["timestamp"] == str(ts)
    assert payload["sign"] == _expected_sign(ts, "abc")
    # secret never leaks into the body
    assert "abc" not in repr(payload).replace(payload["sign"], "")
    # header template maps level "error" -> "red"
    assert payload["card"]["header"]["template"] == "red"
    assert payload["card"]["header"]["title"]["content"] == "Title"
    # lines joined into a single lark_md block
    content = payload["card"]["elements"][0]["text"]["content"]
    assert content == "line1\nline2"


def test_build_payload_without_secret_omits_signature():
    notifier = FeishuNotifier(webhook_url="https://x/hook", enabled=True)
    payload = notifier.build_payload("T", ["a"], timestamp=42)
    assert "sign" not in payload
    assert "timestamp" not in payload
    # default level -> blue
    assert payload["card"]["header"]["template"] == "blue"


def test_build_card_handles_empty_lines():
    notifier = FeishuNotifier()
    card = notifier.build_card("T", [])
    assert card["card"]["elements"][0]["text"]["content"] == "(no detail)"


# --------------------------------------------------------------------------- #
# notify_cycle digest (built into payload, captured instead of POSTed)
# --------------------------------------------------------------------------- #
def _capture_notifier():
    """Enabled notifier whose _post just records the payload (no network)."""
    notifier = FeishuNotifier(
        webhook_url="https://x/hook", secret="k", enabled=True
    )
    captured = {}

    def fake_post(payload):
        captured["payload"] = payload
        return {"status": "sent", "http_status": 200}

    notifier._post = fake_post  # type: ignore[assignment]
    return notifier, captured


def _receipt(status: str, tx_hash: str = "") -> ExecutionReceipt:
    return ExecutionReceipt(
        symbol="BNBUSDT",
        side=Side.LONG,
        action=OrderAction.OPEN,
        status=status,
        tx_hash=tx_hash,
    )


def test_notify_cycle_digest_counts_and_level():
    notifier, captured = _capture_notifier()
    signal = MarketSignal(
        regime=MarketRegime.RISK_ON, btc_trend=BtcTrend.UP, source="mock"
    )
    account = AccountState(equity_usd=1234.5, gas_balance=0.5)
    receipts = [
        _receipt("filled", "0x" + "a" * 64),
        _receipt("dry_run"),
        _receipt("failed"),
        _receipt("skipped"),
    ]
    skipped = [SkippedOrder(symbol="ETHUSDT", side=Side.LONG, reason="not_onchain_tradeable")]

    res = notifier.notify_cycle(
        signal=signal, account=account, receipts=receipts, skipped=skipped
    )
    assert res["status"] == "sent"

    payload = captured["payload"]
    # any failed receipt -> error template (red)
    assert payload["card"]["header"]["template"] == "red"
    content = payload["card"]["elements"][0]["text"]["content"]
    assert "regime=`risk_on`" in content
    assert "btc=`up`" in content
    assert "filled=1" in content
    assert "dry_run=1" in content
    assert "failed=1" in content
    # 1 explicit skip + 1 skipped receipt = 2
    assert "**跳过**: 2 单" in content
    # tx sample present and shortened
    assert "**TX**" in content
    assert "not_onchain_tradeable" in content
    # signed
    assert "sign" in payload


def test_notify_cycle_empty_inputs_edge_case():
    # no secret, no positions, nothing executed -> still builds a valid card
    notifier = FeishuNotifier(webhook_url="https://x/hook", enabled=True)
    captured = {}

    def fake_post(payload):
        captured["payload"] = payload
        return {"status": "sent"}

    notifier._post = fake_post  # type: ignore[assignment]

    res = notifier.notify_cycle(signal=None, account=None, receipts=None, skipped=None)
    assert res["status"] == "sent"

    payload = captured["payload"]
    # no secret -> unsigned
    assert "sign" not in payload
    content = payload["card"]["elements"][0]["text"]["content"]
    assert "regime=`unknown`" in content
    assert "filled=0" in content
    assert "**跳过**: 0 单" in content
    # nothing executed and nothing skipped -> info (blue)
    assert payload["card"]["header"]["template"] == "blue"


def test_short_hash():
    notifier = FeishuNotifier()
    assert notifier._short_hash("0x" + "f" * 64) == "0xffffff…ffffff"
    assert notifier._short_hash("short") == "short"


def test_response_interpretation():
    notifier = FeishuNotifier()
    assert notifier._interpret_response(200, '{"code":0,"msg":"success"}')["status"] == "sent"
    bad = notifier._interpret_response(200, '{"code":19021,"msg":"sign match fail"}')
    assert bad["status"] == "error"
    assert bad["reason"] == "feishu_rejected"
    assert notifier._interpret_response(500, "")["status"] == "error"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
