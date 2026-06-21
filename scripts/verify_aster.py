#!/usr/bin/env python3
"""Read-only Aster Futures API checker (no trading, no order placement).

Confirms the v3 EIP-712 self-custody signing actually round-trips against
Aster's real endpoint, by calling only READ endpoints:

    * public  : GET /fapi/v3/ticker/price   (mark/last price — no signature)
    * signed  : GET /fapi/v3/balance        (USER_DATA — needs EIP-712 signature)
    * signed  : GET /fapi/v3/positionRisk    (USER_DATA — needs EIP-712 signature)

It never places, cancels, or modifies an order, and never needs the live gate.
Run it after you set SHIBEI_ONCHAIN_ASTER_MODE=api and have an authorized
Pro API (V3) wallet.

Usage:
    PYTHONPATH=src python scripts/verify_aster.py
"""

from __future__ import annotations

import sys

from shibei_onchain.config import load_config
from shibei_onchain.onchain.aster_perp import AsterPerpClient


def main() -> int:
    cfg = load_config()
    a = cfg.aster
    client = AsterPerpClient(a)

    print("=== Shibei On-Chain · Aster read-only verification (no trading) ===")
    print(f"venue            : {cfg.venue}")
    print(f"aster mode       : {a.mode}")
    print(f"base url         : {a.base_url}")
    print(f"user (main)      : {a.user_address or '(unset)'}")
    print(f"signer (agent)   : {a.signer_address or '(defaults to user)'}")
    print(f"signer key       : {'***set***' if a.signer_private_key else '(unset)'}")
    print(f"margin asset     : {a.margin_asset}")
    print("-" * 68)

    if a.mode != "api":
        print("INFO  ASTER_MODE is not 'api' (currently '%s') — this only checks the" % a.mode)
        print("      offline mock client. Set SHIBEI_ONCHAIN_ASTER_MODE=api to hit the")
        print("      real endpoint. Showing mock values:")

    net_hint = "TESTNET" if "testnet" in a.base_url else "MAINNET (real funds!)"
    print(f"network          : {net_hint}")
    print("-" * 68)

    ok_overall = True

    # 1. public price (no signature) — proves base_url + connectivity
    price = client.get_mark_price("BNBUSDT")
    if price > 0:
        print(f"OK    public price   BNBUSDT = {price}")
    else:
        ok_overall = False
        print("FAIL  public price   BNBUSDT returned 0 (base_url / symbol / connectivity?)")

    # 2. signed balance — proves the EIP-712 signature is accepted
    bal = client.get_balance()
    available = bal.get("available", 0.0)
    balance = bal.get("balance", 0.0)
    notes = client.health().get("notes") or []
    sign_rejected = any(("http_4" in n or "sign" in n) for n in notes)
    if a.mode == "api" and sign_rejected:
        ok_overall = False
        print(f"FAIL  signed balance — endpoint rejected the request. notes={notes}")
        print("      -> check: ASTER_MODE=api, signer key matches the authorized API")
        print("         wallet, base_url network matches where you authorized it, and")
        print("         the agent is approved (Read permission).")
    else:
        print(f"OK    signed balance {a.margin_asset}: available={available}  balance={balance}")
        if a.mode == "api" and balance <= 0:
            print("      WARN  0 collateral — deposit USDT margin before opening a perp.")

    # 3. signed positions
    positions = client.get_positions()
    print(f"OK    open positions : {len(positions)}")
    for p in positions:
        print(f"        {p['symbol']:<10} {p['position_side']:<5} qty={p['qty']} entry={p['entry']} mark={p['mark']}")

    print("-" * 68)
    if a.mode == "api" and not sign_rejected and price > 0:
        print("RESULT: PASS — Aster live read path + EIP-712 signing reachable.")
        print("        (Order placement still requires the live gate to be armed.)")
        verdict = 0
    elif a.mode != "api":
        print("RESULT: mock only — set ASTER_MODE=api to verify the live signing.")
        verdict = 0 if price > 0 else 1
    else:
        print("RESULT: PARTIAL — see FAIL/notes above.")
        verdict = 1
    if notes:
        print("notes:", notes)
    return verdict if ok_overall or a.mode != "api" else 1


if __name__ == "__main__":
    sys.exit(main())
