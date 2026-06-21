"""Unit tests for onchain/universe_filter.py — pure MOCK mode.

Deterministic, no network, no third-party deps. Exercises:
  * registry load for both chain 97 (testnet) and chain 56 (mainnet),
  * token_info resolution by base asset AND by board symbol,
  * the HARD pre-trade filter: majors kept, junk symbol rejected
    (not_onchain_tradeable), and a liquidity-floor rejection,
  * the execution-time final_guard: zero output + web3 slippage breach,
  * edge case: missing tokens file degrades to a config fallback (no crash).
"""

from __future__ import annotations

from dataclasses import replace

from shibei_onchain.config import OnChainConfig
from shibei_onchain.models import (
    Candidate,
    MarketSignal,
    OrderAction,
    PlannedOrder,
    Side,
)
from shibei_onchain.onchain.universe_filter import UniverseFilter


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #
def _cfg(chain_id: int) -> OnChainConfig:
    # tokens_path defaults to the relative "config/tokens.bsc.json"; the filter
    # resolves it against cwd then the repo root, so this works regardless of
    # where pytest is launched from.
    return OnChainConfig(chain_id=chain_id)


def _cand(symbol: str, side: Side = Side.LONG, price: float = 100.0) -> Candidate:
    return Candidate(
        symbol=symbol,
        side=side,
        price=price,
        stop_loss_price=price * 0.94,
    )


# --------------------------------------------------------------------------- #
# registry loading
# --------------------------------------------------------------------------- #
def test_testnet_registry_loads():
    uf = UniverseFilter(_cfg(97))
    assert uf.source == "registry"
    assert uf.chain_id == 97
    # testnet router from the registry, not a config fallback.
    assert uf.router_address() == "0xD99D1c33F9fC3444f8101754aBC46c52416550D1"
    # testnet lists BNB / BTC / ETH / CAKE.
    assert uf.is_tradeable("BNB")
    assert uf.is_tradeable("CAKE")
    # USDT always resolvable as the quote asset.
    assert uf.usdt().base_asset == "USDT"
    assert uf.usdt().address


def test_mainnet_registry_loads():
    uf = UniverseFilter(_cfg(56))
    assert uf.source == "registry"
    assert uf.router_address() == "0x10ED43C718714eb63d5aA57B78B54704E256024E"
    # mainnet has a broader universe.
    for base in ("BNB", "BTC", "ETH", "CAKE", "XRP", "ADA", "DOGE", "USDC"):
        assert uf.is_tradeable(base), base
    # DOGE carries 8 decimals in the registry.
    assert uf.token_info("DOGE").decimals == 8


def test_token_info_by_base_and_by_symbol():
    uf = UniverseFilter(_cfg(56))
    by_base = uf.token_info("BTC")
    by_symbol = uf.token_info("BTCUSDT")
    assert by_base is not None and by_symbol is not None
    # same registry entry resolved either way.
    assert by_base.address == by_symbol.address
    assert by_base.base_asset == "BTC"
    # case-insensitive.
    assert uf.token_info("bnb").base_asset == "BNB"
    # unknown -> None.
    assert uf.token_info("RANDOMSHIT") is None


# --------------------------------------------------------------------------- #
# the HARD pre-trade filter
# --------------------------------------------------------------------------- #
def test_filter_keeps_majors_rejects_junk():
    uf = UniverseFilter(_cfg(56))
    candidates = [
        _cand("BNBUSDT"),
        _cand("BTCUSDT"),
        _cand("RANDOMSHITUSDT"),   # junk, not on-chain
        _cand("CAKEUSDT"),
    ]
    kept, skipped = uf.filter(candidates)

    kept_syms = {c.symbol for c in kept}
    assert kept_syms == {"BNBUSDT", "BTCUSDT", "CAKEUSDT"}

    assert len(skipped) == 1
    junk = skipped[0]
    assert junk.symbol == "RANDOMSHITUSDT"
    assert junk.stage == "universe_filter"
    assert junk.reason == "not_onchain_tradeable"
    assert junk.detail["in_registry"] is False


def test_filter_liquidity_floor_via_signal_score():
    uf = UniverseFilter(_cfg(56))
    # BNB has a 5,000,000 registry floor on mainnet; a zero liquidity_score from
    # the signal layer is a hard reject; CAKE with no score passes.
    signal = MarketSignal(token_signals={"BNB": {"liquidity_score": 0.0}})
    kept, skipped = uf.filter([_cand("BNBUSDT"), _cand("CAKEUSDT")], signal)

    assert {c.symbol for c in kept} == {"CAKEUSDT"}
    assert len(skipped) == 1
    assert skipped[0].symbol == "BNBUSDT"
    assert skipped[0].reason == "insufficient_liquidity"
    assert skipped[0].stage == "universe_filter"


def test_filter_testnet_zero_floor_passes_without_signal():
    # testnet floors are all 0 -> nothing to enforce, majors pass.
    uf = UniverseFilter(_cfg(97))
    kept, skipped = uf.filter([_cand("BNBUSDT"), _cand("ETHUSDT")])
    assert {c.symbol for c in kept} == {"BNBUSDT", "ETHUSDT"}
    assert skipped == []


def test_filter_empty_input():
    uf = UniverseFilter(_cfg(56))
    kept, skipped = uf.filter([])
    assert kept == []
    assert skipped == []


# --------------------------------------------------------------------------- #
# final_guard (execution-time re-check)
# --------------------------------------------------------------------------- #
def _order(entry_price: float = 100.0, slippage_bps: int = 100) -> PlannedOrder:
    return PlannedOrder(
        symbol="BNBUSDT",
        side=Side.LONG,
        action=OrderAction.OPEN,
        base_asset="BNB",
        entry_price=entry_price,
        max_slippage_bps=slippage_bps,
    )


def test_final_guard_zero_output_rejected():
    uf = UniverseFilter(_cfg(56))
    guard = uf.final_guard(_order(), {"amount_out": 0.0, "source": "web3", "price": 100.0})
    assert guard is not None
    assert guard.stage == "final_guard"
    assert guard.reason == "zero_output_quote"


def test_final_guard_web3_slippage_breach():
    uf = UniverseFilter(_cfg(56))
    # entry 100, realized quote price 102 -> 200 bps > 100 bps budget -> reject.
    quote = {"amount_out": 5.0, "price": 102.0, "source": "web3"}
    guard = uf.final_guard(_order(entry_price=100.0, slippage_bps=100), quote)
    assert guard is not None
    assert guard.reason == "slippage_exceeds_budget"
    assert guard.detail["slippage_bps"] == 200.0


def test_final_guard_web3_within_budget_passes():
    uf = UniverseFilter(_cfg(56))
    # 50 bps drift, within a 100 bps budget -> None.
    quote = {"amount_out": 5.0, "price": 100.5, "source": "web3"}
    assert uf.final_guard(_order(entry_price=100.0, slippage_bps=100), quote) is None


def test_final_guard_mock_quote_skips_slippage_check():
    uf = UniverseFilter(_cfg(56))
    # huge drift, but source is mock -> slippage is meaningless, only the
    # zero-output rule applies (amount_out > 0 here) -> None.
    quote = {"amount_out": 5.0, "price": 999.0, "source": "mock"}
    assert uf.final_guard(_order(entry_price=100.0, slippage_bps=100), quote) is None


# --------------------------------------------------------------------------- #
# edge case: missing tokens file degrades, never crashes
# --------------------------------------------------------------------------- #
def test_missing_tokens_file_degrades_to_config_fallback():
    cfg = replace(_cfg(56), tokens_path="config/__does_not_exist__.json")
    uf = UniverseFilter(cfg)
    # no crash; degraded source + visible note.
    assert uf.source == "config_fallback"
    assert any("tokens_file_not_found" in n or "tokens_path" in n for n in uf.notes)
    # the config fallback still synthesizes the quote asset + native token.
    assert uf.usdt().base_asset == "USDT"
    # a junk symbol is still rejected, not crashing.
    kept, skipped = uf.filter([_cand("RANDOMSHITUSDT")])
    assert kept == []
    assert skipped and skipped[0].reason == "not_onchain_tradeable"
