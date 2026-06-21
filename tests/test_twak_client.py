"""Pure-mock unit tests for onchain.twak_client.TwakClient.

These run with **no network, no private key, no third-party deps**: they only
exercise the default ``mock`` mode and the secret-free invariants. Everything is
deterministic — the synthetic tx hashes are reproducible across runs.
"""

from __future__ import annotations

from dataclasses import replace

from shibei_onchain.config import OnChainConfig
from shibei_onchain.onchain.twak_client import SwapResult, TwakClient, _deterministic_hash


USDT = "0x337610d27c682E347C9cD60BD4b3b107C9d34dDd"
ROUTER = "0xD99D1c33F9fC3444f8101754aBC46c52416550D1"
TOKEN = "0xBB4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"  # some WBNB-like token


def _mock_config(**kw) -> OnChainConfig:
    base = OnChainConfig(twak_mode="mock", usdt_address=USDT, pancake_router=ROUTER)
    return replace(base, **kw) if kw else base


def test_default_mode_is_mock():
    client = TwakClient(_mock_config())
    assert client.mode == "mock"
    health = client.health()
    assert health["mode"] == "mock"
    assert health["self_custody"] is True
    assert health["key_present"] is False


def test_address_is_deterministic_and_stable():
    a = TwakClient(_mock_config()).address()
    b = TwakClient(_mock_config()).address()
    assert a == b
    assert a.startswith("0x")
    assert len(a) == 42
    # explicit wallet_address wins
    cfg = _mock_config(wallet_address="0xABCDEF0000000000000000000000000000000001")
    assert TwakClient(cfg).address() == "0xABCDEF0000000000000000000000000000000001"


def test_native_and_token_balances():
    client = TwakClient(_mock_config())
    # native ~0.5 BNB synthetic
    assert client.native_balance() == 0.5
    # configured USDT seeded into the synthetic ledger
    assert client.token_balance(USDT, 18) == 1000.0
    # unknown token -> 0 (no exception)
    assert client.token_balance(TOKEN, 18) == 0.0


def test_approve_returns_ok_with_deterministic_hash():
    c1 = TwakClient(_mock_config())
    c2 = TwakClient(_mock_config())
    r1 = c1.approve(USDT, ROUTER, 1_000_000)
    r2 = c2.approve(USDT, ROUTER, 1_000_000)
    assert isinstance(r1, SwapResult)
    assert r1.status == "ok" and r1.ok
    assert r1.tx_hash.startswith("0x") and len(r1.tx_hash) == 66
    # deterministic across independent clients
    assert r1.tx_hash == r2.tx_hash
    # different inputs -> different hash
    r3 = c1.approve(USDT, ROUTER, 2_000_000)
    assert r3.tx_hash != r1.tx_hash


def test_swap_fills_at_min_out_and_is_deterministic():
    client = TwakClient(_mock_config())
    path = [USDT, TOKEN]
    res = client.swap_exact_tokens_for_tokens(
        token_in=USDT,
        token_out=TOKEN,
        amount_in_wei=10 * 10**18,
        min_amount_out_wei=3 * 10**18,
        path=path,
    )
    assert res.status == "ok"
    assert res.amount_in == float(10 * 10**18)
    # conservative deterministic fill at exactly min_amount_out
    assert res.amount_out == float(3 * 10**18)
    assert res.gas_used > 0
    assert res.tx_hash.startswith("0x") and len(res.tx_hash) == 66

    # same inputs on a fresh client -> identical hash (reproducible)
    res2 = TwakClient(_mock_config()).swap_exact_tokens_for_tokens(
        token_in=USDT,
        token_out=TOKEN,
        amount_in_wei=10 * 10**18,
        min_amount_out_wei=3 * 10**18,
        path=path,
    )
    assert res2.tx_hash == res.tx_hash


def test_mock_swap_updates_synthetic_ledger():
    client = TwakClient(_mock_config())
    assert client.token_balance(USDT, 18) == 1000.0
    assert client.token_balance(TOKEN, 18) == 0.0
    client.swap_exact_tokens_for_tokens(
        token_in=USDT,
        token_out=TOKEN,
        amount_in_wei=100 * 10**18,
        min_amount_out_wei=2 * 10**18,
        path=[USDT, TOKEN],
    )
    # USDT debited by amount_in, TOKEN credited by amount_out (human units)
    assert client.token_balance(USDT, 18) == 1000.0 - 100.0
    assert client.token_balance(TOKEN, 18) == 2.0


def test_deterministic_hash_helper_is_pure():
    h1 = _deterministic_hash("a", 1, None)
    h2 = _deterministic_hash("a", 1, None)
    assert h1 == h2
    assert h1.startswith("0x") and len(h1) == 66
    assert _deterministic_hash("a", 1, "x") != h1


def test_mcp_mode_without_endpoint_fails_visibly_not_raises():
    # edge case: mcp mode but no endpoint configured -> failed, never raises
    cfg = _mock_config(twak_mode="mcp", twak_endpoint="")
    client = TwakClient(cfg)
    res = client.approve(USDT, ROUTER, 1)
    assert res.status == "failed"
    assert res.error == "twak_endpoint_not_configured"
    # reads degrade to 0.0 rather than raising
    assert client.native_balance() == 0.0
    assert client.token_balance(USDT, 18) == 0.0


def test_health_never_exposes_private_key():
    cfg = _mock_config(private_key="0x" + "11" * 32, wallet_address="0xDEAD")
    client = TwakClient(cfg)
    health = client.health()
    assert health["key_present"] is True
    # the raw key must never appear anywhere in the health payload
    flat = repr(health)
    assert "11" * 32 not in flat
    assert cfg.private_key not in flat


# --------------------------------------------------------------------------- #
# real TWAK CLI swap path (subprocess mocked — no binary / network needed)
# --------------------------------------------------------------------------- #
def test_cli_swap_quote_parses(monkeypatch):
    from shibei_onchain.onchain import twak_client as tw

    class _Proc:
        returncode = 0
        stdout = '{"fromAmount":"10","toAmount":"0.0163","txHash":""}'
        stderr = ""

    monkeypatch.setattr(tw.subprocess, "run", lambda *a, **k: _Proc())
    client = TwakClient(OnChainConfig(twak_mode="cli"))
    res = client.cli_swap(amount=10, from_symbol="USDT", to_symbol="BNB", quote_only=True)
    assert res.ok
    assert res.amount_out == 0.0163
    assert res.raw["quote_only"] is True


def test_cli_swap_error_is_visible(monkeypatch):
    from shibei_onchain.onchain import twak_client as tw

    class _Proc:
        returncode = 1
        stdout = '{"error":"fetch failed","errorCode":"NETWORK_ERROR"}'
        stderr = ""

    monkeypatch.setattr(tw.subprocess, "run", lambda *a, **k: _Proc())
    client = TwakClient(OnChainConfig(twak_mode="cli"))
    res = client.cli_swap(amount=10, from_symbol="USDT", to_symbol="BNB")
    assert not res.ok
    assert "fetch failed" in res.error


def test_cli_swap_builds_bsc_args(monkeypatch):
    from shibei_onchain.onchain import twak_client as tw

    captured = {}

    class _Proc:
        returncode = 0
        stdout = '{"toAmount":"1"}'
        stderr = ""

    def _fake_run(argv, **k):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(tw.subprocess, "run", _fake_run)
    TwakClient(OnChainConfig(twak_mode="cli")).cli_swap(
        amount=5, from_symbol="USDT", to_symbol="BNB", quote_only=True
    )
    argv = captured["argv"]
    assert argv[0] == "twak"
    assert "swap" in argv and "--chain" in argv and "bsc" in argv
    assert "--quote-only" in argv and "--json" in argv
