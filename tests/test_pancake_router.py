"""Pure-mock unit tests for onchain/pancake_router.py.

No network, no third-party deps. The single web3-path test uses a hand-rolled
fake object (duck-typed) so it runs without the real ``web3`` package.
"""

from __future__ import annotations

import pytest

from shibei_onchain.config import OnChainConfig
from shibei_onchain.onchain.pancake_router import PancakeRouter


# Deterministic test addresses (lower/mixed case on purpose).
WBNB = "0xae13d989daC2f0dEbFf460aC112a837C89BAa7cd"
USDT = "0x337610d27c682E347C9cD60BD4b3b107C9d34dDd"
ROUTER = "0xD99D1c33F9fC3444f8101754aBC46c52416550D1"
TOKEN = "0x1111111111111111111111111111111111111111"  # an arbitrary non-anchor token
OTHER = "0x2222222222222222222222222222222222222222"


def _cfg(**over) -> OnChainConfig:
    base = dict(
        pancake_router=ROUTER,
        wbnb_address=WBNB,
        usdt_address=USDT,
        max_slippage_bps=100,
    )
    base.update(over)
    return OnChainConfig(**base)


def _router(**over) -> PancakeRouter:
    return PancakeRouter(_cfg(**over))


# --------------------------------------------------------------------------- #
# build_path
# --------------------------------------------------------------------------- #
def test_build_path_direct_when_usdt_leg():
    r = _router()
    # USDT -> token is a natural direct pair (USDT is an anchor hub).
    assert r.build_path(USDT, TOKEN) == [USDT, TOKEN]
    # token -> USDT likewise direct.
    assert r.build_path(TOKEN, USDT) == [TOKEN, USDT]


def test_build_path_direct_when_wbnb_leg():
    r = _router()
    assert r.build_path(WBNB, TOKEN) == [WBNB, TOKEN]
    assert r.build_path(TOKEN, WBNB) == [TOKEN, WBNB]


def test_build_path_via_wbnb_when_neither_side_is_anchor():
    r = _router()
    # token -> other: neither is WBNB/USDT, so hop through WBNB.
    assert r.build_path(TOKEN, OTHER) == [TOKEN, WBNB, OTHER]


def test_build_path_equal_addresses_collapse():
    r = _router()
    assert r.build_path(TOKEN, TOKEN) == [TOKEN]
    # case-insensitive equality also collapses
    assert r.build_path(TOKEN.upper(), TOKEN.lower()) == [TOKEN.upper()]


def test_build_path_no_wbnb_configured_falls_back_direct():
    r = _router(wbnb_address="")
    # With no WBNB hub, even token->other must be direct.
    assert r.build_path(TOKEN, OTHER) == [TOKEN, OTHER]


# --------------------------------------------------------------------------- #
# min_amount_out math
# --------------------------------------------------------------------------- #
def test_min_amount_out_basic_slippage():
    r = _router()
    # 1% slippage (100 bps) off 1_000_000 wei -> 990_000.
    assert r.min_amount_out(1_000_000, 100) == 990_000
    # 0 bps -> unchanged.
    assert r.min_amount_out(1_000_000, 0) == 1_000_000


def test_min_amount_out_integer_floor():
    r = _router()
    # 50 bps off 12_345 -> floor(12_345 * 9950 / 10000) = floor(12283.275) = 12283
    assert r.min_amount_out(12_345, 50) == 12_283


def test_min_amount_out_edge_cases():
    r = _router()
    assert r.min_amount_out(0, 100) == 0
    assert r.min_amount_out(-5, 100) == 0
    # bps clamped to <= 10000: full slippage yields 0, never negative.
    assert r.min_amount_out(1_000_000, 999_999) == 0
    # negative bps clamped to 0.
    assert r.min_amount_out(1_000_000, -10) == 1_000_000


# --------------------------------------------------------------------------- #
# mock quote — both directions
# --------------------------------------------------------------------------- #
def test_mock_quote_usdt_to_token_buy():
    r = _router()
    # Buy TOKEN with 600 USDT at ref price 300 USD/token -> 2.0 tokens.
    q = r.quote(
        token_in=USDT,
        token_out=TOKEN,
        amount_in=600.0,
        decimals_in=18,
        decimals_out=18,
        ref_price=300.0,
    )
    assert q["source"] == "mock"
    assert q["amount_out"] == pytest.approx(2.0)
    assert q["amount_in_wei"] == 600 * 10 ** 18
    assert q["price"] == pytest.approx(300.0)
    assert q["path"] == [USDT, TOKEN]
    # min_amount_out is in WEI and slippage-protected (default 100 bps -> 1%).
    expected_out_wei = 2 * 10 ** 18
    assert q["min_amount_out"] == (expected_out_wei * 9900) // 10000


def test_mock_quote_token_to_usdt_sell():
    r = _router()
    # Sell 2 tokens at ref price 300 -> 600 USDT.
    q = r.quote(
        token_in=TOKEN,
        token_out=USDT,
        amount_in=2.0,
        decimals_in=18,
        decimals_out=18,
        ref_price=300.0,
    )
    assert q["source"] == "mock"
    assert q["amount_out"] == pytest.approx(600.0)
    assert q["amount_in_wei"] == 2 * 10 ** 18
    assert q["path"] == [TOKEN, USDT]
    expected_out_wei = 600 * 10 ** 18
    assert q["min_amount_out"] == (expected_out_wei * 9900) // 10000


def test_mock_quote_respects_decimals():
    r = _router()
    # USDT with 6 decimals -> 1.5 token out (token 18 decimals).
    q = r.quote(
        token_in=USDT,
        token_out=TOKEN,
        amount_in=300.0,
        decimals_in=6,
        decimals_out=18,
        ref_price=200.0,
    )
    assert q["amount_out"] == pytest.approx(1.5)
    assert q["amount_in_wei"] == 300 * 10 ** 6
    assert q["min_amount_out"] == (int(round(1.5 * 10 ** 18)) * 9900) // 10000


def test_mock_quote_custom_slippage_bps():
    r = _router(max_slippage_bps=250)  # 2.5%
    q = r.quote(
        token_in=USDT,
        token_out=TOKEN,
        amount_in=100.0,
        decimals_in=18,
        decimals_out=18,
        ref_price=50.0,
    )
    assert q["amount_out"] == pytest.approx(2.0)
    expected_out_wei = 2 * 10 ** 18
    assert q["min_amount_out"] == (expected_out_wei * 9750) // 10000


def test_mock_quote_via_wbnb_path():
    r = _router()
    # token -> other, neither anchor: path must hop through WBNB.
    q = r.quote(
        token_in=TOKEN,
        token_out=OTHER,
        amount_in=1.0,
        decimals_in=18,
        decimals_out=18,
        ref_price=10.0,
    )
    assert q["path"] == [TOKEN, WBNB, OTHER]
    # token_in is not the stable, so it is treated as a sell: out = in * price.
    assert q["amount_out"] == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# mock quote — edge cases (degraded / no price)
# --------------------------------------------------------------------------- #
def test_mock_quote_missing_ref_price_is_visible():
    r = _router()
    q = r.quote(
        token_in=USDT,
        token_out=TOKEN,
        amount_in=100.0,
        decimals_in=18,
        decimals_out=18,
        ref_price=None,
    )
    assert q["source"] == "mock"
    assert q["amount_out"] == 0.0
    assert q["min_amount_out"] == 0
    assert q["reason"] == "no_ref_price"


def test_mock_quote_zero_amount_in():
    r = _router()
    q = r.quote(
        token_in=USDT,
        token_out=TOKEN,
        amount_in=0.0,
        decimals_in=18,
        decimals_out=18,
        ref_price=300.0,
    )
    assert q["amount_out"] == 0.0
    assert q["amount_in_wei"] == 0
    assert q["min_amount_out"] == 0


def test_quote_without_web3_reports_no_provider_reason():
    r = _router()
    q = r.quote(
        token_in=USDT,
        token_out=TOKEN,
        amount_in=10.0,
        decimals_in=18,
        decimals_out=18,
        ref_price=100.0,
    )
    assert q["source"] == "mock"
    assert q["reason"] == "no_web3_provider"


# --------------------------------------------------------------------------- #
# web3 path via a fake duck-typed provider (no real web3 dependency)
# --------------------------------------------------------------------------- #
class _FakeCall:
    def __init__(self, amounts):
        self._amounts = amounts

    def call(self):
        return self._amounts


class _FakeFunctions:
    def __init__(self, amounts):
        self._amounts = amounts

    def getAmountsOut(self, amount_in, path):  # noqa: N802 (mirrors ABI name)
        return _FakeCall(self._amounts)


class _FakeContract:
    def __init__(self, amounts):
        self.functions = _FakeFunctions(amounts)


class _FakeEth:
    def __init__(self, amounts):
        self._amounts = amounts

    def contract(self, address, abi):
        return _FakeContract(self._amounts)


class _FakeWeb3:
    def __init__(self, amounts):
        self.eth = _FakeEth(amounts)


def test_web3_path_uses_get_amounts_out(monkeypatch):
    # The router will try to `from web3 import Web3`; stub Web3 with a tiny
    # checksum shim so this test never needs the real package.
    import shibei_onchain.onchain.pancake_router as mod

    class _StubWeb3:
        @staticmethod
        def to_checksum_address(addr):
            return addr

    import sys
    import types

    fake_web3_module = types.ModuleType("web3")
    fake_web3_module.Web3 = _StubWeb3
    monkeypatch.setitem(sys.modules, "web3", fake_web3_module)

    # getAmountsOut returns [amount_in_wei, amount_out_wei]; out = 3 tokens.
    fake = _FakeWeb3([5 * 10 ** 18, 3 * 10 ** 18])
    r = PancakeRouter(_cfg(), web3=fake)

    q = r.quote(
        token_in=USDT,
        token_out=TOKEN,
        amount_in=5.0,
        decimals_in=18,
        decimals_out=18,
        ref_price=999.0,  # ignored on the web3 path
    )
    assert q["source"] == "web3"
    assert q["amount_out"] == pytest.approx(3.0)
    # min_amount_out computed off the on-chain out wei with default 100 bps.
    assert q["min_amount_out"] == (3 * 10 ** 18 * 9900) // 10000


def test_web3_failure_degrades_to_mock(monkeypatch):
    # A provider whose contract call blows up must degrade to mock, never raise.
    class _BoomEth:
        def contract(self, address, abi):
            raise RuntimeError("rpc down")

    class _BoomWeb3:
        eth = _BoomEth()

    import sys
    import types

    class _StubWeb3:
        @staticmethod
        def to_checksum_address(addr):
            return addr

    fake_web3_module = types.ModuleType("web3")
    fake_web3_module.Web3 = _StubWeb3
    monkeypatch.setitem(sys.modules, "web3", fake_web3_module)

    r = PancakeRouter(_cfg(), web3=_BoomWeb3())
    q = r.quote(
        token_in=USDT,
        token_out=TOKEN,
        amount_in=10.0,
        decimals_in=18,
        decimals_out=18,
        ref_price=100.0,
    )
    assert q["source"] == "mock"
    assert q["amount_out"] == pytest.approx(0.1)  # 10 USDT / 100 = 0.1 token
    assert q["reason"].startswith("web3_quote_failed")
