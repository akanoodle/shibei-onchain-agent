# BSC Testnet runbook (verified)

The default venue is **Aster perps (`venue=aster`)** — both legs (long perp +
short perp) on Aster, the BNB-native perp DEX that Trust Wallet itself integrates.
Section A covers Aster; section B keeps the PancakeSwap-spot path
(`venue=pancake`). Signing/broadcast is the **one step only you do** — your key
never leaves your machine, and this repo never executes it for you.

---

# A. Aster perps (both legs) — `venue=aster` (default)

The whole dry-run pipeline already works end-to-end in mock mode (both legs):

```bash
cd shibei-onchain-agent && pip install -e '.[onchain]' && export PYTHONPATH=src
python -m shibei_onchain.cli status     # venue: aster, short leg enabled
python -m shibei_onchain.cli plan       # 拾贝 ranking -> long perp + short perp, sized
python -m shibei_onchain.cli run        # one autonomous cycle (dry-run, both legs)
```

To go to a **real Aster testnet trade**, the agent uses Aster's v3 EIP-712 auth:
your main wallet approves a separate *agent/signer* wallet that signs each order
(self-custody — exactly the Trust Wallet Agent Kit model).

1. **Create the Aster API/agent wallet.** Go to
   `https://www.asterdex-testnet.com/en/api-wallet`, switch to **Pro API**, and
   create an agent — you get a `signer` address + signer private key. (The Aster
   *trading UI* testnet is whitelist-gated; the **API/agent** path is self-serve.
   If blocked, request access in their Discord.)
2. **Deposit USDT collateral.** Perps need margin — your wallet currently has
   test BNB (gas) but **0 USDT**. Fund test USDT and deposit it to your Aster
   futures account (via the api-wallet/deposit flow).
3. **Configure `.env`:**
   ```
   SHIBEI_ONCHAIN_ASTER_MODE=api
   SHIBEI_ONCHAIN_ASTER_SIGNER_ADDRESS=0x...        # the agent wallet
   SHIBEI_ONCHAIN_ASTER_SIGNER_PRIVATE_KEY=0x...    # the agent key (self-custody, local only)
   ```
   (If you skip these, the signer defaults to your main wallet/key.)
4. **Arm the live gate** (same four flips as section B step 4).
5. **Trade:**
   ```bash
   python -m shibei_onchain.cli run     # opens long/short perps with resting stop + TP
   ```
   Receipts carry the Aster order ids; positions show up in
   `python -m shibei_onchain.cli status`-adjacent state and on the Aster testnet UI.

> Status (VERIFIED 2026-06-15): the v3 EIP-712 self-custody signing
> **round-trips against the live Aster testnet** — `scripts/verify_aster.py`
> returns `PASS` (signed balance + positions read). Two details learned the hard
> way and now baked in: (1) the EIP-712 domain **chainId is 714 on testnet /
> 1666 on mainnet** (auto-derived from the base URL); (2) signed params must
> include **both `user` (main account) and `signer` (agent wallet)** — `user`
> alone or `signer` alone is rejected with `-1000 Signature check failed`. The
> only thing left before a live trade is **depositing USDT collateral** (an
> account with 0 margin reads `equity=0` and the risk stack rejects every open).

---

# B. PancakeSwap spot (long-only) — `venue=pancake`

Set `SHIBEI_ONCHAIN_VENUE=pancake` to use this path instead. The read-only parts
below were verified live (RPC reachable, POA OK, real PancakeSwap quotes for
USDT→BNB and USDT→BTC).

## 0. Install on-chain extras (once)

```bash
cd shibei-onchain-agent
pip install -e '.[onchain]'        # web3 + eth-account  (web3 6.15.1 verified)
export PYTHONPATH=src
```

## 1. Read-only verification (no key, safe now)

`.env` already ships configured for BSC testnet web3 mode. Run:

```bash
python scripts/verify_onchain.py
```

Expected (verified 2026-06-15): RPC connected, `chain_id 97`, **POA middleware
OK**, and live `getAmountsOut` quotes. Findings on public testnet:

| pair | testnet pool | use it? |
|---|---|---|
| USDT → **BNB** | deep, sane quote | ✅ **recommended for the smoke swap** |
| USDT → BTC | has liquidity | ok |
| USDT → ETH | **no pool** | skip on testnet |
| USDT → CAKE | imbalanced test pool | skip |

> Testnet pool prices are **not** real market prices (e.g. ~$15/BNB on testnet).
> That's why a full `run` cycle — which sizes from the scanner's real-world mock
> prices — will trip the slippage `final_guard` on testnet. For a real testnet
> swap use `swap-hello` (below) with **no `--price`**, so the swap is driven by
> the live on-chain quote and `minAmountOut`, and the slippage guard is bypassed.
> On **mainnet**, real prices align and the full `run` cycle is coherent.

## 2. Wallet + faucet (you)

1. Create a fresh BSC **testnet** wallet (MetaMask / Trust Wallet). Use a
   throwaway key — never a key with real funds.
2. Get test BNB (gas): https://testnet.bnbchain.org/faucet-smart
3. Get the test USDT used here (`0x3376...4dDd`). If your faucet hands a
   different stable, set `SHIBEI_ONCHAIN_USDT_ADDRESS` in `.env` to it and
   re-run `verify_onchain.py`.

Put the address (and, when ready, the key) in `.env`:

```
SHIBEI_ONCHAIN_WALLET_ADDRESS=0xYourTestnetAddress
SHIBEI_ONCHAIN_PRIVATE_KEY=0xYourTestnetPrivateKey   # throwaway, testnet only
```

Re-run `python scripts/verify_onchain.py` — it should now also print your
BNB/USDT balances (still no signing).

## 3. Dry-run the swap first

```bash
python -m shibei_onchain.cli swap-hello --token BNB --notional 5
# dry_run=True, live=False -> a fully-formed simulated receipt with the real quote
```

## 4. Arm the live gate (you, deliberately)

Flip all four in `.env`:

```
SHIBEI_ONCHAIN_DRY_RUN=false
SHIBEI_ONCHAIN_TRADING_ENABLED=true
SHIBEI_ONCHAIN_EXECUTION_ADAPTER_READY=true
SHIBEI_ONCHAIN_CONFIRM_TEXT=I_UNDERSTAND_SHIBEI_ONCHAIN_LIVE_RISK
```

`python -m shibei_onchain.cli status` should now show `mode: LIVE` and no
`live blocked by` line.

## 5. The real testnet swap (you)

```bash
python -m shibei_onchain.cli swap-hello --token BNB --notional 5
```

This does `approve(exact)` then `swapExactTokensForTokens` (USDT→WBNB), signed
**locally** with your key, broadcast to testnet. The output prints the tx hash
and an explorer URL:

```
https://testnet.bscscan.com/tx/0x...
```

Open it to confirm the fill. To sell back: add `--close`.

## 6. (Optional) mainnet smoke

Only after testnet works, and only tiny amounts you execute yourself: set
`CHAIN_ID=56`, a mainnet `RPC_URL`, the mainnet router
(`0x10ED43C718714eb63d5aA57B78B54704E256024E`) and real token addresses (already
in `config/tokens.bsc.json` under `"56"`), fund a small mainnet wallet, and
repeat steps 3–5 with `--notional 1`. Keep the tx hash for your BUIDL submission.

## Safety recap

- Key is read locally, signs locally, never logged, never transmitted.
- Exact-amount approvals (no infinite allowance); `minAmountOut` slippage guard;
  gas-floor check fails *visibly*.
- Gate is closed by default; four explicit flips required to go live.
- The agent never auto-executes a real swap for you in this session — you run
  step 5.
