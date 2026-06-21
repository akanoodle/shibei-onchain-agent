"""Pure-mock unit tests for onchain/reconcile.py.

Deterministic, no network, no private key, no third-party deps. Everything runs
through the default ``mock`` TwakClient and the on-disk token registry.

Exercises:
  * read_account in mock mode -> deterministic equity == initial_equity_usd held
    in USDT, no positions, gas balance present, source "mock",
  * carry-forward of stop_loss_events / open_orders_this_hour from a prior state,
  * read_account valuing a carried prior Position (no live token balance) at a
    ref_price and carrying its entry/stop/side,
  * detect_exits: a take-profit case (rr >= take_profit_r) and a stop-hit case,
    each emitting a PlannedOrder(action=CLOSE) with the right reason,
  * a SHORT max-hold time-stop,
  * edge case: a flat position with neither TP nor stop nor time-stop -> no exit,
  * edge case: a failing balance read degrades to 0.0 with a visible note.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shibei_onchain.config import AgentConfig, OnChainConfig, RiskParams
from shibei_onchain.models import (
    AccountState,
    OrderAction,
    Position,
    Side,
    StopLossEvent,
)
from shibei_onchain.onchain.reconcile import Reconciler
from shibei_onchain.onchain.twak_client import TwakClient
from shibei_onchain.onchain.universe_filter import UniverseFilter


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _config(initial_equity: float = 1000.0) -> AgentConfig:
    # default OnChainConfig is mock twak_mode, chain 97 (testnet); the testnet
    # registry usdt address matches the config usdt_address so the synthetic
    # ledger seeds exactly initial_equity_usd of USDT.
    return AgentConfig(initial_equity_usd=initial_equity, onchain=OnChainConfig())


def _reconciler(config: AgentConfig = None) -> Reconciler:
    config = config or _config()
    twak = TwakClient(config.onchain)
    universe = UniverseFilter(config.onchain)
    return Reconciler(config, twak, universe)


def _long_position(**kw) -> Position:
    base = dict(
        symbol="BTCUSDT",
        side=Side.LONG,
        base_asset="BTC",
        quantity=0.01,
        entry_price=100.0,
        notional_usd=1.0,
        current_price=100.0,
        stop_loss_price=94.0,
        take_profit_r_multiple=2.5,
        breakeven_r_multiple=1.0,
        max_hold_hours=0.0,
        opened_at="2026-06-15T00:00:00+00:00",
        strategy_id="s16a",
        strategy_leg="v1",
    )
    base.update(kw)
    return Position(**base)


# --------------------------------------------------------------------------- #
# read_account — mock mode is deterministic
# --------------------------------------------------------------------------- #
def test_read_account_mock_deterministic_equity():
    rec = _reconciler(_config(initial_equity=1000.0))
    acct = rec.read_account()

    assert acct.source == "mock"
    # equity is exactly the configured initial equity, held in USDT, no positions
    assert acct.equity_usd == 1000.0
    assert acct.quote_balance_usd == 1000.0
    assert acct.positions == []
    # native BNB gas present (mock synthetic 0.5)
    assert acct.gas_balance == 0.5
    # fresh state carries no memory
    assert acct.open_orders_this_hour == 0
    assert acct.stop_loss_events == []

    # deterministic across calls
    acct2 = _reconciler(_config(initial_equity=1000.0)).read_account()
    assert acct2.equity_usd == acct.equity_usd
    assert acct2.quote_balance_usd == acct.quote_balance_usd


def test_read_account_carries_forward_memory():
    rec = _reconciler()
    event = StopLossEvent(base_asset="BTC", side=Side.LONG, at="2026-06-15T01:00:00+00:00")
    prior = AccountState(
        equity_usd=900.0,
        open_orders_this_hour=2,
        stop_loss_events=[event],
    )
    acct = rec.read_account(prior=prior)
    # circuit-breaker history and per-hour counter survive the cycle
    assert acct.open_orders_this_hour == 2
    assert len(acct.stop_loss_events) == 1
    assert acct.stop_loss_events[0].base_asset == "BTC"


def test_read_account_values_carried_prior_position_at_ref_price():
    rec = _reconciler()
    # carry a prior BTC long; the mock wallet holds no BTC token, so the position
    # is reconstructed from the prior and valued at the supplied ref_price.
    prior_pos = _long_position(quantity=0.5, entry_price=100.0, current_price=100.0)
    prior = AccountState(positions=[prior_pos])

    acct = rec.read_account(prior=prior, ref_prices={"BTC": 120.0})
    pos = acct.position_for("BTC")
    assert pos is not None
    assert pos.quantity == 0.5
    assert pos.entry_price == 100.0          # carried from prior
    assert pos.current_price == 120.0        # from ref_prices
    assert pos.side is Side.LONG
    assert pos.notional_usd == 0.5 * 120.0   # valued at the live ref price
    # equity = free USDT (1000 mock) + position notional (60)
    assert acct.equity_usd == 1000.0 + 60.0


# --------------------------------------------------------------------------- #
# detect_exits — TP and stop-hit
# --------------------------------------------------------------------------- #
def test_detect_exits_take_profit():
    rec = _reconciler()
    # entry 100, stop 94 -> risk 6. price 115 -> move 15 -> rr 2.5 == TP r.
    pos = _long_position(entry_price=100.0, stop_loss_price=94.0, current_price=115.0,
                         take_profit_r_multiple=2.5)
    acct = AccountState(positions=[pos])

    orders = rec.detect_exits(acct, RiskParams())
    assert len(orders) == 1
    order = orders[0]
    assert order.action is OrderAction.CLOSE
    assert order.reason == "take_profit"
    assert order.base_asset == "BTC"
    assert order.side is Side.LONG
    assert order.metadata["rr_multiple"] >= 2.5


def test_detect_exits_stop_hit():
    rec = _reconciler()
    # long with price below the stop -> stop-loss exit (dominates any TP).
    pos = _long_position(entry_price=100.0, stop_loss_price=94.0, current_price=92.0)
    acct = AccountState(positions=[pos])

    orders = rec.detect_exits(acct, RiskParams())
    assert len(orders) == 1
    assert orders[0].action is OrderAction.CLOSE
    assert orders[0].reason == "stop_loss"
    assert orders[0].metadata["current_price"] == 92.0


def test_detect_exits_short_max_hold():
    rec = _reconciler()
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    opened = (now - timedelta(hours=30)).isoformat()
    # short, 5h before now -> within hold; 30h held with a 24h cap -> time-stop.
    pos = Position(
        symbol="BTCUSDT",
        side=Side.SHORT,
        base_asset="BTC",
        quantity=0.01,
        entry_price=100.0,
        notional_usd=1.0,
        current_price=99.0,          # in profit but not at TP, stop not hit
        stop_loss_price=106.0,
        take_profit_r_multiple=2.5,
        max_hold_hours=24.0,
        opened_at=opened,
    )
    acct = AccountState(positions=[pos])

    orders = rec.detect_exits(acct, RiskParams(), now=now)
    assert len(orders) == 1
    assert orders[0].reason == "short_max_hold"
    assert orders[0].side is Side.SHORT


def test_detect_exits_flat_position_no_exit():
    rec = _reconciler()
    # entry == current, stop far below, long -> nothing fires.
    pos = _long_position(entry_price=100.0, stop_loss_price=94.0, current_price=100.0)
    acct = AccountState(positions=[pos])
    assert rec.detect_exits(acct, RiskParams()) == []


def test_detect_exits_short_within_hold_no_exit():
    rec = _reconciler()
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    opened = (now - timedelta(hours=5)).isoformat()
    pos = Position(
        symbol="BTCUSDT",
        side=Side.SHORT,
        base_asset="BTC",
        quantity=0.01,
        entry_price=100.0,
        notional_usd=1.0,
        current_price=99.0,
        stop_loss_price=106.0,
        max_hold_hours=24.0,
        opened_at=opened,
    )
    acct = AccountState(positions=[pos])
    assert rec.detect_exits(acct, RiskParams(), now=now) == []


# --------------------------------------------------------------------------- #
# edge case: a failing balance read degrades, never raises
# --------------------------------------------------------------------------- #
def test_read_account_degrades_on_balance_read_failure():
    config = _config()
    universe = UniverseFilter(config.onchain)

    class _BrokenTwak(TwakClient):
        def native_balance(self) -> float:
            raise RuntimeError("rpc down")

        def token_balance(self, token_address, decimals):  # noqa: ANN001
            raise RuntimeError("rpc down")

    rec = Reconciler(config, _BrokenTwak(config.onchain), universe)
    acct = rec.read_account()  # must not raise
    # gas + USDT both degraded to 0.0; mock anchor kicks in for equity.
    assert acct.gas_balance == 0.0
    assert acct.equity_usd == config.initial_equity_usd
    assert any(n.startswith("read_failed:") for n in acct.notes)
