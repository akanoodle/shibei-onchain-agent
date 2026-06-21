"""Aster Finance perpetual-futures client — the long+short execution venue.

Aster (formerly APX Finance / Astherus, YZi Labs backed) is the largest perp DEX
native to BNB Chain, and Trust Wallet itself ships a perps integration through
Aster — so routing both legs of the 拾贝 long/short strategy here is maximally
aligned with all three BNB-Hack sponsors and is the natural fit for the Trust
Wallet Agent Kit self-custody model.

Aster's Futures API is **Binance-USDⓈ-M-shaped** (``/fapi/v3/*``), so 拾贝's
Binance order semantics transfer directly. Auth is **v3 EIP-712**: the user's
main wallet approves a separate *agent / signer* wallet once, and every request
is signed by that signer with an EIP-712 ``encode_structured_data`` over the
urlencoded params (domain ``AsterSignTransaction`` / chainId 1666 /
``verifyingContract`` zero address, ``primaryType="Message"`` with a single
``msg`` string, ``nonce`` = current time in microseconds). The signing key never
leaves the process and is never logged — pure self-custody.

Two modes (``aster.mode``):

    ``mock`` (default)
        Fully offline, deterministic. Maintains a tiny in-process position
        ledger; no network, no key, no third-party import. This is what the unit
        tests and the dry-run pipeline exercise.

    ``api``
        Real signed calls to ``aster.base_url`` (testnet
        ``https://fapi.asterdex-testnet.com`` / prod ``https://fapi.asterdex.com``)
        via lazily-imported ``requests`` + ``eth_account``.

Failure contract (拾贝 principle — failures must be visible): no public method
ever raises on a network / credential / dep error; it returns a
``PerpResult(status="failed", error=...)`` or a degraded read, so the cycle
records the reason and keeps running.

NOTE on live readiness: the v3 EIP-712 scheme below is implemented from Aster's
official ``api-docs`` example. Before any *mainnet* live run, confirm the exact
``msg`` serialization against the current official demo
(github.com/asterdex/api-docs, ``V3(Recommended)``) — the request building is
isolated in ``_sign_query`` so it is a one-spot change.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shibei_onchain.config import AsterConfig
from shibei_onchain.models import Side


_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Deterministic mark prices for mock mode (rough real-world levels; the live
# path reads them from the exchange instead).
_MOCK_MARKS = {
    "BNBUSDT": 600.0,
    "ETHUSDT": 3000.0,
    "BTCUSDT": 65000.0,
    "CAKEUSDT": 2.5,
    "XRPUSDT": 0.6,
    "ADAUSDT": 0.45,
    "DOGEUSDT": 0.15,
    "SOLUSDT": 150.0,
    "ASTERUSDT": 0.5,
}
_MOCK_COLLATERAL_USDT = 1000.0


@dataclass
class PerpResult:
    """Outcome of an Aster perp action (order / leverage / close)."""

    status: str                       # ok | failed
    order_id: str = ""
    symbol: str = ""
    side: str = ""                    # BUY | SELL
    position_side: str = ""           # LONG | SHORT
    quantity: float = 0.0
    avg_price: float = 0.0
    reduce_only: bool = False
    error: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class AsterPerpClient:
    """Self-custody Aster Futures client (mock | signed v3 REST)."""

    def __init__(self, aster: AsterConfig) -> None:
        self._cfg = aster
        self.mode = (aster.mode or "mock").lower()
        self.base_url = (aster.base_url or "https://fapi.asterdex-testnet.com").rstrip("/")
        self.user_address = aster.user_address
        self.signer_address = aster.signer_address or aster.user_address
        self._signer_key = aster.signer_private_key
        self.eip712_chain_id = int(aster.eip712_chain_id or 1666)
        self._notes: List[str] = []
        self._pos_mode: Optional[str] = None   # "hedge" | "oneway" (cached)
        # mock ledger: symbol -> {"position_side","qty","entry"}
        self._mock_positions: Dict[str, Dict[str, Any]] = {}
        self._mock_leverage: Dict[str, int] = {}
        # monotonic microsecond nonce
        self._nonce_lock = threading.Lock()
        self._last_ns = 0
        self._nonce_i = 0

    # ------------------------------------------------------------------ #
    # diagnostics
    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        return {
            "venue": "aster",
            "mode": self.mode,
            "base_url": self.base_url,
            "user": self.user_address or "(unset)",
            "signer": self.signer_address or "(unset)",
            "signer_key_present": bool(self._signer_key),
            "eip712_chain_id": self.eip712_chain_id,
            "self_custody": True,
            "notes": list(self._notes),
        }

    def position_mode(self) -> str:
        """'hedge' (dual side) or 'oneway' — determines the positionSide param.
        Cached; defaults to one-way (the Aster/Binance default)."""
        if self._pos_mode is not None:
            return self._pos_mode
        if self.mode != "api":
            self._pos_mode = "oneway"
            return self._pos_mode
        ok, data, _ = self._signed_request("GET", "/fapi/v3/positionSide/dual", {})
        dual = bool(_as_dict(data).get("dualSidePosition")) if ok else False
        self._pos_mode = "hedge" if dual else "oneway"
        return self._pos_mode

    def _ps(self, position_side: str) -> str:
        """In one-way mode Aster requires positionSide=BOTH; in hedge mode it is
        the leg's own LONG/SHORT."""
        return "BOTH" if self.position_mode() == "oneway" else position_side

    # ------------------------------------------------------------------ #
    # market data
    # ------------------------------------------------------------------ #
    def get_mark_price(self, symbol: str) -> float:
        sym = (symbol or "").upper()
        if self.mode != "api":
            return float(_MOCK_MARKS.get(sym, 0.0))
        # last-trade price first (may be empty on a quiet/testnet market) ...
        ok, data, _ = self._public_get("/fapi/v3/ticker/price", {"symbol": sym})
        if ok and isinstance(data, dict):
            px = _f(data.get("price"))
            if px > 0:
                return px
        # ... then fall back to the mark price, which a listed perp always has.
        ok2, data2, _ = self._public_get("/fapi/v3/premiumIndex", {"symbol": sym})
        if ok2 and isinstance(data2, dict):
            return _f(data2.get("markPrice"))
        return 0.0

    def get_balance(self) -> Dict[str, Any]:
        """Available margin collateral, in USD terms."""
        if self.mode == "api":
            ok, data, _ = self._signed_request("GET", "/fapi/v3/balance", {})
            if ok and isinstance(data, list):
                for row in data:
                    if str(row.get("asset")) == self._cfg.margin_asset:
                        return {
                            "asset": self._cfg.margin_asset,
                            "available": _f(row.get("availableBalance") or row.get("balance")),
                            "balance": _f(row.get("balance")),
                        }
            return {"asset": self._cfg.margin_asset, "available": 0.0, "balance": 0.0}
        # mock: collateral minus margin locked by open positions
        locked = sum(
            abs(p["qty"]) * p["entry"] / max(1, self._mock_leverage.get(sym, self._cfg.default_leverage))
            for sym, p in self._mock_positions.items()
        )
        return {
            "asset": self._cfg.margin_asset,
            "available": max(0.0, _MOCK_COLLATERAL_USDT - locked),
            "balance": _MOCK_COLLATERAL_USDT,
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        """Open positions, normalized to {symbol, position_side, qty, entry, mark}."""
        if self.mode == "api":
            ok, data, _ = self._signed_request("GET", "/fapi/v3/positionRisk", {})
            out: List[Dict[str, Any]] = []
            if ok and isinstance(data, list):
                for row in data:
                    amt = _f(row.get("positionAmt"))
                    if amt == 0.0:
                        continue
                    out.append({
                        "symbol": str(row.get("symbol")),
                        "position_side": "LONG" if amt > 0 else "SHORT",
                        "qty": abs(amt),
                        "entry": _f(row.get("entryPrice")),
                        "mark": _f(row.get("markPrice")),
                    })
            return out
        return [
            {
                "symbol": sym,
                "position_side": p["position_side"],
                "qty": abs(p["qty"]),
                "entry": p["entry"],
                "mark": self.get_mark_price(sym),
            }
            for sym, p in self._mock_positions.items()
            if abs(p["qty"]) > 0
        ]

    # ------------------------------------------------------------------ #
    # trading
    # ------------------------------------------------------------------ #
    def set_leverage(self, symbol: str, leverage: int) -> PerpResult:
        sym = (symbol or "").upper()
        lev = max(1, int(leverage))
        if self.mode == "api":
            ok, data, err = self._signed_request(
                "POST", "/fapi/v3/leverage", {"symbol": sym, "leverage": lev}
            )
            return PerpResult(status="ok" if ok else "failed", symbol=sym, error=err, raw=_as_dict(data))
        self._mock_leverage[sym] = lev
        return PerpResult(status="ok", symbol=sym, raw={"leverage": lev, "mode": "mock"})

    def open_market(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: float,
        leverage: Optional[int] = None,
        position_side: Optional[str] = None,
    ) -> PerpResult:
        """Open (or add to) a perp position with a MARKET order.

        LONG  -> BUY, SHORT -> SELL. ``position_side`` defaults to match the leg
        (hedge mode); pass it explicitly if the account is in one-way mode."""
        sym = (symbol or "").upper()
        order_side = "BUY" if side is Side.LONG else "SELL"
        pos_side = position_side or side.position_side
        lev = int(leverage or self._cfg.default_leverage)
        if quantity <= 0:
            return PerpResult(status="failed", symbol=sym, side=order_side, error="zero_quantity")

        if self.mode == "api":
            self.set_leverage(sym, lev)
            params = {
                "symbol": sym,
                "side": order_side,
                "type": "MARKET",
                "quantity": _fmt_qty(quantity),
                "positionSide": self._ps(pos_side),
            }
            ok, data, err = self._signed_request("POST", "/fapi/v3/order", params)
            return PerpResult(
                status="ok" if ok else "failed",
                order_id=str(_as_dict(data).get("orderId", "")),
                symbol=sym, side=order_side, position_side=pos_side,
                quantity=quantity, avg_price=_f(_as_dict(data).get("avgPrice")),
                error=err, raw=_as_dict(data),
            )
        # mock fill at the mark price; update ledger
        mark = self.get_mark_price(sym) or 0.0
        self._mock_leverage[sym] = lev
        self._mock_apply(sym, pos_side, quantity, mark)
        return PerpResult(
            status="ok",
            order_id=self._mock_order_id("open", sym, order_side, quantity),
            symbol=sym, side=order_side, position_side=pos_side,
            quantity=quantity, avg_price=mark,
            raw={"type": "MARKET", "leverage": lev, "mode": "mock"},
        )

    def close_market(self, *, symbol: str, position_side: str, quantity: float) -> PerpResult:
        """Reduce-only MARKET close of an existing position."""
        sym = (symbol or "").upper()
        ps = (position_side or "LONG").upper()
        # to close a LONG you SELL; to close a SHORT you BUY
        order_side = "SELL" if ps == "LONG" else "BUY"
        if quantity <= 0:
            return PerpResult(status="failed", symbol=sym, side=order_side, error="zero_quantity")

        if self.mode == "api":
            params = {
                "symbol": sym,
                "side": order_side,
                "type": "MARKET",
                "quantity": _fmt_qty(quantity),
                "positionSide": self._ps(ps),
                "reduceOnly": "true",
            }
            ok, data, err = self._signed_request("POST", "/fapi/v3/order", params)
            return PerpResult(
                status="ok" if ok else "failed",
                order_id=str(_as_dict(data).get("orderId", "")),
                symbol=sym, side=order_side, position_side=ps,
                quantity=quantity, reduce_only=True,
                error=err, raw=_as_dict(data),
            )
        mark = self.get_mark_price(sym) or 0.0
        self._mock_reduce(sym, ps, quantity)
        return PerpResult(
            status="ok",
            order_id=self._mock_order_id("close", sym, order_side, quantity),
            symbol=sym, side=order_side, position_side=ps,
            quantity=quantity, avg_price=mark, reduce_only=True,
            raw={"type": "MARKET", "reduceOnly": True, "mode": "mock"},
        )

    def place_stop(
        self,
        *,
        symbol: str,
        position_side: str,
        stop_price: float,
        quantity: float,
    ) -> PerpResult:
        """Reduce-only STOP_MARKET protective stop for an open position."""
        sym = (symbol or "").upper()
        ps = (position_side or "LONG").upper()
        order_side = "SELL" if ps == "LONG" else "BUY"
        if self.mode == "api":
            params = {
                "symbol": sym,
                "side": order_side,
                "type": "STOP_MARKET",
                "stopPrice": _fmt_price(stop_price),
                "quantity": _fmt_qty(quantity),
                "positionSide": self._ps(ps),
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
            }
            ok, data, err = self._signed_request("POST", "/fapi/v3/order", params)
            return PerpResult(
                status="ok" if ok else "failed",
                order_id=str(_as_dict(data).get("orderId", "")),
                symbol=sym, side=order_side, position_side=ps,
                quantity=quantity, reduce_only=True, error=err, raw=_as_dict(data),
            )
        return PerpResult(
            status="ok",
            order_id=self._mock_order_id("stop", sym, order_side, stop_price),
            symbol=sym, side=order_side, position_side=ps, quantity=quantity,
            reduce_only=True, raw={"type": "STOP_MARKET", "stopPrice": stop_price, "mode": "mock"},
        )

    def place_take_profit(
        self,
        *,
        symbol: str,
        position_side: str,
        take_profit_price: float,
        quantity: float,
    ) -> PerpResult:
        """Reduce-only TAKE_PROFIT_MARKET resting order for an open position."""
        sym = (symbol or "").upper()
        ps = (position_side or "LONG").upper()
        order_side = "SELL" if ps == "LONG" else "BUY"
        if self.mode == "api":
            params = {
                "symbol": sym,
                "side": order_side,
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": _fmt_price(take_profit_price),
                "quantity": _fmt_qty(quantity),
                "positionSide": self._ps(ps),
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
            }
            ok, data, err = self._signed_request("POST", "/fapi/v3/order", params)
            return PerpResult(
                status="ok" if ok else "failed",
                order_id=str(_as_dict(data).get("orderId", "")),
                symbol=sym, side=order_side, position_side=ps,
                quantity=quantity, reduce_only=True, error=err, raw=_as_dict(data),
            )
        return PerpResult(
            status="ok",
            order_id=self._mock_order_id("tp", sym, order_side, take_profit_price),
            symbol=sym, side=order_side, position_side=ps, quantity=quantity,
            reduce_only=True, raw={"type": "TAKE_PROFIT_MARKET", "stopPrice": take_profit_price, "mode": "mock"},
        )

    def cancel_open_orders(self, *, symbol: str) -> PerpResult:
        """Cancel ALL resting open orders for a symbol (stop + take-profit).

        Used by the breakeven move: cancel the existing protective orders so the
        adapter can re-place the stop at entry (and re-place the take-profit).
        Mock mode is a no-op (the mock ledger has no resting orders)."""
        sym = (symbol or "").upper()
        if self.mode == "api":
            ok, data, err = self._signed_request("DELETE", "/fapi/v3/allOpenOrders", {"symbol": sym})
            return PerpResult(status="ok" if ok else "failed", symbol=sym, error=err, raw=_as_dict(data))
        return PerpResult(status="ok", symbol=sym, raw={"cancelled": "all", "mode": "mock"})

    # ================================================================== #
    # mock ledger
    # ================================================================== #
    def _mock_apply(self, symbol: str, position_side: str, qty: float, price: float) -> None:
        pos = self._mock_positions.get(symbol)
        signed = qty if position_side == "LONG" else -qty
        if pos is None:
            self._mock_positions[symbol] = {"position_side": position_side, "qty": signed, "entry": price}
            return
        # weighted-average entry on same-direction add; net on opposite
        new_qty = pos["qty"] + signed
        if pos["qty"] * signed > 0 and (pos["qty"] + signed) != 0:
            total = abs(pos["qty"]) + abs(signed)
            pos["entry"] = (pos["entry"] * abs(pos["qty"]) + price * abs(signed)) / total
        pos["qty"] = new_qty
        pos["position_side"] = "LONG" if new_qty >= 0 else "SHORT"
        if abs(new_qty) < 1e-12:
            self._mock_positions.pop(symbol, None)

    def _mock_reduce(self, symbol: str, position_side: str, qty: float) -> None:
        pos = self._mock_positions.get(symbol)
        if pos is None:
            return
        if position_side == "LONG":
            pos["qty"] = max(0.0, pos["qty"] - qty)
        else:
            pos["qty"] = min(0.0, pos["qty"] + qty)
        if abs(pos["qty"]) < 1e-12:
            self._mock_positions.pop(symbol, None)

    def _mock_order_id(self, *parts: Any) -> str:
        payload = "|".join(str(p) for p in parts)
        return "mock-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    # ================================================================== #
    # signed v3 (EIP-712) REST
    # ================================================================== #
    def _nonce(self) -> int:
        """Strictly-monotonic microsecond nonce (Aster requires µs precision,
        unique & increasing). Always returns ``max(now, last+1)`` and stores it,
        so two calls in the same microsecond — or a real clock that hasn't yet
        advanced past a prior collision bump — can never collide or go backwards."""
        with self._nonce_lock:
            now_ns = int(time.time() * 1_000_000)
            if now_ns <= self._last_ns:
                now_ns = self._last_ns + 1
            self._last_ns = now_ns
            return now_ns

    def _sign_query(self, params: Dict[str, Any]) -> Optional[str]:
        """Build the signed query string: urlencode(params + nonce + signer),
        EIP-712-sign that exact string as ``msg``, append ``&signature=``.

        Returns ``None`` (and notes a reason) if signing is not possible."""
        if not self._signer_key:
            self._note("aster_signer_key_missing")
            return None
        try:
            from eth_account import Account  # lazy, optional
        except Exception as exc:  # noqa: BLE001
            self._note("eth_account_import_failed:" + type(exc).__name__)
            return None

        ordered: Dict[str, Any] = {}
        for key, value in params.items():
            if value is None:
                continue
            ordered[key] = value
        # Aster v3 requires BOTH `user` (main account) and `signer` (agent wallet)
        # in the signed params — verified live against testnet. Harmless when the
        # wallet is self-authorized (user == signer).
        user = self.user_address or self.signer_address
        if user:
            ordered["user"] = user
        ordered["nonce"] = str(self._nonce())
        ordered["signer"] = self.signer_address
        param_str = urllib.parse.urlencode(ordered)

        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Message": [{"name": "msg", "type": "string"}],
            },
            "primaryType": "Message",
            "domain": {
                "name": "AsterSignTransaction",
                "version": "1",
                "chainId": self.eip712_chain_id,
                "verifyingContract": _ZERO_ADDRESS,
            },
            "message": {"msg": param_str},
        }
        try:
            message = self._encode_typed(typed_data)
            signed = Account.sign_message(message, private_key=self._signer_key)
            sig = signed.signature.hex()
            if not sig.startswith("0x"):
                sig = "0x" + sig
        except Exception as exc:  # noqa: BLE001 - never raise, never log key
            self._note("aster_sign_failed:" + type(exc).__name__)
            return None
        return param_str + "&signature=" + sig

    @staticmethod
    def _encode_typed(typed_data: Dict[str, Any]):
        """Encode an EIP-712 typed-data dict, tolerating eth_account versions."""
        try:
            from eth_account.messages import encode_typed_data  # newer eth_account
            return encode_typed_data(full_message=typed_data)
        except Exception:  # noqa: BLE001 - fall back to the legacy helper
            from eth_account.messages import encode_structured_data  # eth_account 0.x
            return encode_structured_data(typed_data)

    def _signed_request(self, method: str, path: str, params: Dict[str, Any]):
        """Signed request -> (ok, parsed_json, error). Never raises."""
        if self.mode != "api":
            return False, None, "not_api_mode"
        query = self._sign_query(params)
        if query is None:
            return False, None, "sign_unavailable"
        url = self.base_url + path + "?" + query
        return self._http(method, url)

    def _public_get(self, path: str, params: Dict[str, Any]):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self._http("GET", url)

    def _http(self, method: str, url: str):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "ShibeiOnChainAgent/0.1",
        }
        timeout = self._cfg.timeout_seconds
        try:
            import requests  # lazy, optional
            resp = requests.request(method, url, headers=headers, timeout=timeout)
            text, code = resp.text, resp.status_code
        except Exception:  # noqa: BLE001 - fall back to urllib
            try:
                from urllib import request as _request
                req = _request.Request(url, headers=headers, method=method)
                with _request.urlopen(req, timeout=timeout) as r:
                    text = r.read().decode("utf-8")
                    code = getattr(r, "status", 200)
            except Exception as exc:  # noqa: BLE001 - never raise
                self._note("aster_http_failed:" + type(exc).__name__)
                return False, None, "http_failed:" + type(exc).__name__
        if code and int(code) >= 400:
            self._note("aster_http_%s" % code)
            return False, _safe_json(text), "http_%s:%s" % (code, (text or "")[:160])
        return True, _safe_json(text), ""

    # ------------------------------------------------------------------ #
    def _note(self, msg: str) -> None:
        if msg not in self._notes:
            self._notes.append(msg)
            if len(self._notes) > 50:
                self._notes.pop(0)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text) if text else {}
    except (ValueError, TypeError):
        return {"_raw": (text or "")[:200]}


def _fmt_qty(q: float) -> str:
    return ("%.8f" % float(q)).rstrip("0").rstrip(".") or "0"


def _fmt_price(p: float) -> str:
    return ("%.8f" % float(p)).rstrip("0").rstrip(".") or "0"
