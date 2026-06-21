"""Tests for the Aster perp client (mock ledger + EIP-712 signing mechanics)."""

from __future__ import annotations

from shibei_onchain.config import AsterConfig
from shibei_onchain.onchain.aster_perp import AsterPerpClient
from shibei_onchain.models import Side


def _client():
    return AsterPerpClient(AsterConfig(mode="mock"))


def test_open_long_and_short_tracked():
    c = _client()
    assert c.open_market(symbol="BNBUSDT", side=Side.LONG, quantity=0.5).ok
    assert c.open_market(symbol="ETHUSDT", side=Side.SHORT, quantity=0.1).ok
    pos = {p["symbol"]: p for p in c.get_positions()}
    assert pos["BNBUSDT"]["position_side"] == "LONG"
    assert pos["ETHUSDT"]["position_side"] == "SHORT"
    assert abs(pos["BNBUSDT"]["qty"] - 0.5) < 1e-9


def test_close_removes_position():
    c = _client()
    c.open_market(symbol="BNBUSDT", side=Side.LONG, quantity=0.5)
    assert c.close_market(symbol="BNBUSDT", position_side="LONG", quantity=0.5).ok
    assert all(p["symbol"] != "BNBUSDT" for p in c.get_positions())


def test_balance_reflects_locked_margin():
    c = _client()
    base = c.get_balance()["available"]
    c.open_market(symbol="BNBUSDT", side=Side.LONG, quantity=0.5, leverage=5)
    after = c.get_balance()["available"]
    assert after < base  # collateral locked by the open position


def test_zero_quantity_fails_visibly():
    c = _client()
    res = c.open_market(symbol="BNBUSDT", side=Side.LONG, quantity=0.0)
    assert not res.ok
    assert res.error == "zero_quantity"


def test_set_leverage_and_stop_and_tp():
    c = _client()
    assert c.set_leverage("BNBUSDT", 5).ok
    assert c.place_stop(symbol="BNBUSDT", position_side="LONG", stop_price=560.0, quantity=0.5).ok
    assert c.place_take_profit(symbol="BNBUSDT", position_side="LONG", take_profit_price=700.0, quantity=0.5).ok


def test_mark_price_deterministic():
    c = _client()
    assert c.get_mark_price("BNBUSDT") == c.get_mark_price("BNBUSDT")
    assert c.get_mark_price("BNBUSDT") > 0


def test_eip712_signing_produces_signature():
    # signing mechanics must work (validates the live v3 path) without any network.
    import eth_account

    acct = eth_account.Account.create()
    cfg = AsterConfig(
        mode="api",
        user_address=acct.address,
        signer_address=acct.address,
        signer_private_key=acct.key.hex(),
    )
    c = AsterPerpClient(cfg)
    query = c._sign_query(
        {"symbol": "BNBUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.5", "positionSide": "LONG"}
    )
    assert query is not None
    assert "&signature=0x" in query
    assert "nonce=" in query and "signer=" in query


def test_nonce_is_monotonic():
    c = _client()
    assert c._nonce() < c._nonce() < c._nonce()


def test_sign_without_key_degrades():
    c = AsterPerpClient(AsterConfig(mode="api"))  # no signer key
    assert c._sign_query({"symbol": "BNBUSDT"}) is None
    assert "aster_signer_key_missing" in c.health()["notes"]


def test_health_never_leaks_key():
    c = AsterPerpClient(AsterConfig(mode="api", signer_private_key="0xdeadbeef"))
    h = c.health()
    assert h["signer_key_present"] is True
    assert "0xdeadbeef" not in str(h)
