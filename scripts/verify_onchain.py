#!/usr/bin/env python3
"""Read-only BNB Chain connectivity & liquidity checker.

Verifies everything the live execution path needs *up to (but not including)
signing* — so it never touches a private key and can never move funds:

    * RPC reachable + chain id matches config
    * POA middleware works (reads the latest block — the check that fails on
      BNB Chain without POA middleware)
    * wallet native (BNB / gas) + USDT balances (only if WALLET_ADDRESS is set)
    * a real PancakeSwap ``getAmountsOut`` quote for USDT -> each tradeable token
      (this is what tells you whether a testnet pool actually has liquidity)

Usage:
    PYTHONPATH=src python scripts/verify_onchain.py
    # reads SHIBEI_ONCHAIN_* env (set TWAK_MODE=web3, RPC_URL, addresses, and
    # optionally WALLET_ADDRESS — NO private key needed or read here).

Exit code 0 if RPC + POA + at least one quote succeed, else 1.
"""

from __future__ import annotations

import sys

from shibei_onchain.config import load_config
from shibei_onchain.onchain.twak_client import TwakClient
from shibei_onchain.onchain.pancake_router import PancakeRouter
from shibei_onchain.onchain.universe_filter import UniverseFilter


def main() -> int:
    cfg = load_config()
    oc = cfg.onchain
    ok = True

    print("=== Shibei On-Chain · read-only verification (no private key used) ===")
    print(f"chain={oc.chain} chain_id={oc.chain_id} rpc={oc.rpc_url}")
    print(f"router={oc.pancake_router}")
    print(f"usdt={oc.usdt_address}  wbnb={oc.wbnb_address}")
    print(f"twak_mode={oc.twak_mode}  wallet={oc.wallet_address or '(unset)'}")
    print("-" * 68)

    # 1. web3 connect (lazy via TwakClient, which injects POA middleware)
    twak = TwakClient(oc)
    w3 = twak._ensure_web3()  # read-only handle; no key involved
    if w3 is None:
        print("FAIL  web3 provider could not be created (is web3 installed? RPC set?)")
        print("      notes:", twak.health().get("notes"))
        return 1
    try:
        connected = bool(w3.is_connected())
    except Exception as exc:  # noqa: BLE001
        connected = False
        print("FAIL  is_connected error:", type(exc).__name__, exc)
    print(f"{'OK  ' if connected else 'FAIL'}  RPC connected = {connected}")
    ok = ok and connected

    # 2. chain id
    try:
        cid = w3.eth.chain_id
        match = int(cid) == int(oc.chain_id)
        print(f"{'OK  ' if match else 'WARN'}  chain_id on-chain = {cid} (config {oc.chain_id})")
        ok = ok and match
    except Exception as exc:  # noqa: BLE001
        print("FAIL  chain_id read error:", type(exc).__name__, exc)
        ok = False

    # 3. POA middleware — read latest block (fails without POA on BNB Chain)
    try:
        block = w3.eth.get_block("latest")
        print(f"OK    POA middleware OK — latest block #{block['number']}")
    except Exception as exc:  # noqa: BLE001
        print("FAIL  get_block('latest') failed (POA middleware?):", type(exc).__name__, exc)
        ok = False

    # 4. balances (only if a wallet address is configured; still no key)
    if oc.wallet_address:
        try:
            bnb = twak.native_balance()
            usdt = twak.token_balance(oc.usdt_address, 18)
            print(f"OK    wallet BNB(gas)={bnb:.6f}  USDT={usdt:.4f}")
            if bnb < oc.gas_min_bnb:
                print(f"WARN  BNB {bnb:.6f} < gas floor {oc.gas_min_bnb} — fund gas before live")
        except Exception as exc:  # noqa: BLE001
            print("WARN  balance read failed:", type(exc).__name__, exc)
    else:
        print("INFO  WALLET_ADDRESS unset — skipping balance read")

    # 5. real getAmountsOut quotes (liquidity probe)
    universe = UniverseFilter(oc)
    router = PancakeRouter(oc, web3=w3)
    usdt = universe.usdt()
    any_quote = False
    print("-" * 68)
    print("Liquidity probe — USDT -> token (real getAmountsOut):")
    for base in ("BNB", "ETH", "BTC", "CAKE"):
        info = universe.token_info(base)
        if info is None or not info.address:
            print(f"  {base:<5} skip (not in registry for chain {oc.chain_id})")
            continue
        try:
            q = router.quote(
                token_in=usdt.address,
                token_out=info.address,
                amount_in=20.0,
                decimals_in=usdt.decimals,
                decimals_out=info.decimals,
                ref_price=None,
            )
            src = q.get("source")
            out = q.get("amount_out")
            if src == "web3" and out and float(out) > 0:
                any_quote = True
                print(f"  {base:<5} OK   20 USDT -> {float(out):.8f} {base}  (source=web3)")
            else:
                print(f"  {base:<5} no pool / no quote (source={src}) — pick a token with testnet liquidity")
        except Exception as exc:  # noqa: BLE001
            print(f"  {base:<5} quote error: {type(exc).__name__} {exc}")

    print("-" * 68)
    verdict = ok and any_quote
    print("RESULT:", "PASS — live path reachable" if verdict else
          "PARTIAL — RPC/POA may be ok but no on-chain quote (testnet liquidity?)")
    if twak.health().get("notes"):
        print("notes:", twak.health()["notes"])
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
