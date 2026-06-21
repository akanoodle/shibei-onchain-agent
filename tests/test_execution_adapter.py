"""Unit tests for onchain/execution_adapter.py — pure MOCK mode.

Deterministic, no network, no third-party deps. All collaborators run in their
default offline modes (twak_mode="mock", router without a web3 instance, the
universe filter against the checked-in ``config/tokens.bsc.json`` registry).

Exercises the 7-step ``execute_order`` flow:
  * a long OPEN under dry_run -> "dry_run" receipt carrying a quote-derived
    ``min_amount_out`` and a token_in/out pair,
  * a SHORT order with ``enable_short_leg=False`` -> "skipped" short_leg_disabled,
  * an unknown token -> "skipped" not_onchain_tradeable,
  * the live path (dry_run=False) under mock twak -> "filled" with a tx hash,
  * the gas-floor failure edge case -> "failed" insufficient_gas,
  * batch fan-out produces one receipt per order, and never raises.
"""

from __future__ import annotations

from dataclasses import replace

from shibei_onchain.config import AgentConfig, OnChainConfig
from shibei_onchain.models import OrderAction, PlannedOrder, Side
from shibei_onchain.onchain.execution_adapter import OnChainExecutionAdapter
from shibei_onchain.onchain.pancake_router import PancakeRouter
from shibei_onchain.onchain.twak_client import TwakClient
from shibei_onchain.onchain.universe_filter import UniverseFilter


# --------------------------------------------------------------------------- #
# fixtures / builders — everything offline & deterministic
# --------------------------------------------------------------------------- #
def _config(enable_short_leg: bool = False, gas_min_bnb: float = 0.01) -> AgentConfig:
    onchain = OnChainConfig(chain_id=97, gas_min_bnb=gas_min_bnb, twak_mode="mock")
    return AgentConfig(enable_short_leg=enable_short_leg, onchain=onchain)


def _adapter(config: AgentConfig) -> OnChainExecutionAdapter:
    twak = TwakClient(config.onchain)
    router = PancakeRouter(config.onchain)          # no web3 -> mock quote path
    universe = UniverseFilter(config.onchain)
    return OnChainExecutionAdapter(config, twak, router, universe)


def _order(
    symbol: str = "CAKEUSDT",
    base_asset: str = "CAKE",
    side: Side = Side.LONG,
    action: OrderAction = OrderAction.OPEN,
    notional_usd: float = 100.0,
    entry_price: float = 2.5,
    quantity: float = 0.0,
) -> PlannedOrder:
    return PlannedOrder(
        symbol=symbol,
        side=side,
        action=action,
        base_asset=base_asset,
        quote_asset="USDT",
        notional_usd=notional_usd,
        quantity=quantity,
        entry_price=entry_price,
        stop_loss_price=entry_price * 0.94,
        max_slippage_bps=100,
    )


# --------------------------------------------------------------------------- #
# readiness
# --------------------------------------------------------------------------- #
def test_adapter_ready_with_registry():
    adapter = _adapter(_config())
    # registry resolves the quote asset + router for chain 97.
    assert adapter.ready is True


# --------------------------------------------------------------------------- #
# step 6 — dry-run long OPEN
# --------------------------------------------------------------------------- #
def test_long_open_dry_run_produces_quote_receipt():
    adapter = _adapter(_config())
    order = _order(notional_usd=100.0, entry_price=2.5)

    receipt = adapter.execute_order(order, dry_run=True, ref_price=2.5)

    assert receipt.status == "dry_run"
    assert receipt.dry_run is True
    assert receipt.tx_hash == ""            # no signing in dry-run
    assert receipt.action is OrderAction.OPEN
    assert receipt.side is Side.LONG
    # OPEN long spends USDT, receives the token.
    usdt_addr = adapter.universe.usdt().address
    token_addr = adapter.universe.token_info("CAKE").address
    assert receipt.token_in == usdt_addr
    assert receipt.token_out == token_addr
    # a real quote was computed: 100 USDT / 2.5 = 40 CAKE, with a slippage floor.
    assert receipt.amount_in == 100.0
    assert receipt.amount_out > 0.0
    assert receipt.min_amount_out > 0.0
    assert receipt.min_amount_out < receipt.amount_out   # slippage applied
    assert abs(receipt.amount_out - 40.0) < 1e-6
    # quote snapshot + path are carried in raw for audit.
    assert "quote" in receipt.raw
    assert receipt.raw["quote"]["source"] == "mock"
    assert receipt.raw["path"][0] == usdt_addr


def test_dry_run_is_deterministic():
    adapter = _adapter(_config())
    order = _order()
    r1 = adapter.execute_order(order, dry_run=True, ref_price=2.5)
    r2 = adapter.execute_order(order, dry_run=True, ref_price=2.5)
    assert r1.amount_out == r2.amount_out
    assert r1.min_amount_out == r2.min_amount_out
    assert r1.token_in == r2.token_in and r1.token_out == r2.token_out


# --------------------------------------------------------------------------- #
# step 2 — short leg disabled
# --------------------------------------------------------------------------- #
def test_short_order_skipped_when_short_leg_disabled():
    adapter = _adapter(_config(enable_short_leg=False))
    order = _order(side=Side.SHORT)

    receipt = adapter.execute_order(order, dry_run=True, ref_price=2.5)

    assert receipt.status == "skipped"
    assert receipt.error == "short_leg_disabled"
    assert receipt.side is Side.SHORT


def test_short_order_proceeds_when_short_leg_enabled():
    # When the stretch flag is on, a SHORT is no longer gated at step 2; it
    # flows through to a quote (still long-spot mechanics in the MVP router).
    adapter = _adapter(_config(enable_short_leg=True))
    order = _order(side=Side.SHORT)
    receipt = adapter.execute_order(order, dry_run=True, ref_price=2.5)
    assert receipt.error != "short_leg_disabled"
    assert receipt.status in ("dry_run", "skipped", "failed")


# --------------------------------------------------------------------------- #
# step 1 — unknown token
# --------------------------------------------------------------------------- #
def test_unknown_token_skipped_not_tradeable():
    adapter = _adapter(_config())
    order = _order(symbol="RANDOMSHITUSDT", base_asset="RANDOMSHIT")

    receipt = adapter.execute_order(order, dry_run=True, ref_price=1.0)

    assert receipt.status == "skipped"
    assert receipt.error == "not_onchain_tradeable"


# --------------------------------------------------------------------------- #
# step 7 — live path with mock twak
# --------------------------------------------------------------------------- #
def test_live_path_mock_twak_fills_with_tx_hash():
    # dry_run=False but twak_mode="mock" -> synthetic deterministic fill.
    adapter = _adapter(_config())
    order = _order(notional_usd=100.0, entry_price=2.5)

    receipt = adapter.execute_order(order, dry_run=False, ref_price=2.5)

    assert receipt.status == "filled"
    assert receipt.dry_run is False
    assert receipt.tx_hash.startswith("0x")
    assert len(receipt.tx_hash) == 66        # 0x + 64 hex chars
    assert receipt.gas_used > 0
    # explorer url built from the configured explorer_base.
    assert receipt.explorer_url == "https://testnet.bscscan.com/tx/" + receipt.tx_hash
    # the approve tx is recorded for audit.
    assert receipt.raw["approve_tx"].startswith("0x")


# --------------------------------------------------------------------------- #
# step 3 — gas floor edge case
# --------------------------------------------------------------------------- #
def test_insufficient_gas_fails_visibly():
    # mock native balance is 0.5 BNB; set the floor above it to trip the gate.
    # The gas floor is a pre-broadcast safety, so it only applies to a LIVE
    # (dry_run=False) execution — a dry-run simulates the quote regardless.
    adapter = _adapter(_config(gas_min_bnb=1.0))
    order = _order()

    receipt = adapter.execute_order(order, dry_run=False, ref_price=2.5)

    assert receipt.status == "failed"
    assert receipt.error == "insufficient_gas"
    assert receipt.raw["native_balance"] == 0.5


def test_dry_run_skips_gas_floor():
    # Same low-balance config, but a dry-run must NOT be blocked by the gas floor.
    adapter = _adapter(_config(gas_min_bnb=1.0))
    receipt = adapter.execute_order(_order(), dry_run=True, ref_price=2.5)
    assert receipt.status == "dry_run"


# --------------------------------------------------------------------------- #
# batch fan-out
# --------------------------------------------------------------------------- #
def test_execute_orders_batch_one_receipt_each():
    adapter = _adapter(_config())
    orders = [
        _order(symbol="CAKEUSDT", base_asset="CAKE", entry_price=2.5),
        _order(symbol="RANDOMUSDT", base_asset="RANDOM", entry_price=1.0),  # unknown
        _order(symbol="BTCUSDT", base_asset="BTC", entry_price=60000.0),
    ]
    receipts = adapter.execute_orders(
        orders,
        dry_run=True,
        ref_prices={"CAKE": 2.5, "BTC": 60000.0},
    )
    assert len(receipts) == 3
    assert receipts[0].status == "dry_run"
    assert receipts[1].status == "skipped"          # unknown token
    assert receipts[2].status == "dry_run"
    # batch never raises and preserves input order.
    assert [r.symbol for r in receipts] == ["CAKEUSDT", "RANDOMUSDT", "BTCUSDT"]


def test_close_long_sells_token_for_usdt():
    adapter = _adapter(_config())
    order = _order(action=OrderAction.CLOSE, quantity=40.0, entry_price=2.5)
    receipt = adapter.execute_order(order, dry_run=True, ref_price=2.5)
    assert receipt.status == "dry_run"
    # CLOSE long sells the token for USDT (legs reversed vs OPEN).
    usdt_addr = adapter.universe.usdt().address
    token_addr = adapter.universe.token_info("CAKE").address
    assert receipt.token_in == token_addr
    assert receipt.token_out == usdt_addr
    assert receipt.amount_in == 40.0
