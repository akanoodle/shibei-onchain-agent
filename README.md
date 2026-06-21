# Shibei On-Chain Agent (拾贝链上交易 Agent)

> An autonomous, self-custody **AI trading agent on BNB Chain** for the BNB Hack
> (Track 1 · Autonomous Trading Agents). It reads the market, decides with a
> battle-tested multi-layer risk brain, and executes **end-to-end on-chain** —
> no human in the loop, no custodial transfer of funds.

It uses **all three sponsor capabilities**:

| Layer | Sponsor capability | What it does here |
|---|---|---|
| **Signal** | **CoinMarketCap Agent Hub** (MCP / x402) | Decision-grade market regime, BTC trend, liquidity & risk flags — used to enrich candidates **and** as a **hard risk gate**. CMC is also the market-data feed for the scanner's live fallback — **no centralized-exchange API anywhere**. |
| **Decision** | 拾贝 brain + 汲水 V0.2 strategy | Picks coins from the live 拾贝 board, sizes positions, and runs the full **multi-layer risk stack** (the moat vs. toy bots). |
| **Execution** | **Trust Wallet Agent Kit** @ **BNB Chain** | Self-custody execution. Default venue is **Aster** (BNB-native perp DEX that Trust Wallet integrates); a PancakeSwap long-spot venue is also included. The private key never leaves the device; no custodial transfer. |

**Strategy = 汲水 (Jishui) V0.2 — long-only.** A lightweight long-only momentum
strategy over the 拾贝 ranking board. We keep the production **brain and risk
engine intact** and swap only the *execution layer* for on-chain self-custody —
because 拾贝 already decouples *plan generation* (`planned_orders`) from
*execution* (`execute_*_prepared_orders`). This is an execution-adapter swap, not
a rewrite.

> **Honest framing — read this.** We do **not** claim V0.2 is profitable; in our
> own real-caliber backtest (2026 H1) V0.2 long-only is **−22.5%** (a
> negative-expectancy leg in that window). Track 1 is judged on **live PnL
> (returns + drawdown + risk-adjusted performance) over the Jun 22–28 window**,
> plus rule adherence — so this strategy's edge is **drawdown control**, not
> returns: the account kill-switches (−25% / −10%) cap the bleed while the agent
> demonstrates full autonomy and all three capabilities. Pick the strategy
> deliberately for the live window. See [`docs/BUIDL_SUBMISSION.md`](docs/BUIDL_SUBMISSION.md).

---

## Why this is not a toy bot

Most "AI trading agent" demos are a price feed wired straight to a swap. This one
carries 拾贝's production risk stack, enforced **before any order reaches the chain**:

- **2% risk per trade**, **2.5R take-profit**, **+1R move-to-breakeven**, 6% initial stop
- **汲水 V0.2 time filter** — no new opens during 北京时 08:00–15:59
- **Active position management** — once a position is up +1R the stop is pulled to
  **breakeven** on-exchange (`MOVE_STOP`); a gentle **72h max-hold backstop**
  flattens anything that sits too long (V0.2 has no time stop, so this is a safety
  net, not the strategy exit)
- **Account kill-switches** — freeze **all** new opens on **−25% drawdown** (from peak) or **−10% daily loss**; open positions keep being managed. (The P0 safety gap 拾贝 V3.0 lacked — critical for unattended multi-day live.)
- **≤ 3 new orders per hour**, **≤ 12% total initial portfolio risk**, **≤ 5× notional cap**
- **Same-coin 24h circuit breaker** (2 stop-losses → stop trading that coin)
- **BTC environment gate** — the one rule 拾贝 V3.0 only *logged* but never enforced; here it is a **hard gate** fed by CMC regime/BTC-trend/risk-flags
- **20% max stop-distance** sanity bound
- On-chain safety: self-custody **EIP-712** agent-wallet signing (key local, never logged), **`minAmountOut` slippage guard**, **gas/margin floor check** (fail visibly), **DRY-RUN by default**

Every rejected candidate is recorded with a machine-readable reason — 拾贝's
principle that *failures must be visible*, never dropped silently.

---

## Architecture

```
   CoinMarketCap Agent Hub          拾贝 board + 汲水 V0.2 brain               Trust Wallet Agent Kit @ BNB Chain
 ┌───────────────────────────┐   ┌──────────────────────────────────────┐   ┌─────────────────────────────────────┐
 │ MCP / x402                │   │ scanner ─ 拾贝 board (long candidates) │   │ Aster perp execution (long-only)    │
 │  market regime            │──►│ universe_filter (on-chain ∩ Aster)    │   │  open + resting 2.5R TP / stop      │
 │  BTC trend / risk flags   │   │ risk stack (2% · ≤3/h · ≤5× · time    │──►│  manage: +1R→breakeven, 72h max-hold│
 │  (also data fallback)     │──►│   filter · circuit breaker · BTC gate │   │  kill-switch: −25% DD / −10% daily  │
 └───────────────────────────┘   │   · account kill-switches) → sizing   │   │  self-custody EIP-712 · key local   │
        signal + HARD gate        └──────────────────────────────────────┘   └─────────────────────────────────────┘
                                                  ▲                                          │
                                                  └────────────── account_state ◄────────────┘
```

See [`docs/INTERFACES.md`](docs/INTERFACES.md) for the module contract and
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the full flow.

---

## Quickstart (zero dependencies, dry-run)

The core pipeline — signal → decision → **dry-run** execution → reconcile — runs
on the **Python 3.9+ standard library alone**. No keys, no network, nothing to
install. Live on-chain execution is an opt-in extra (`web3`, `eth-account`).

```bash
cd shibei-onchain-agent
export PYTHONPATH=src

# 1. health + live-gate (everything is dry-run until .env opens the gate)
python -m shibei_onchain.cli status

# 2. a CMC Agent Hub signal + the BTC environment gate it drives
python -m shibei_onchain.cli signal

# 3. the ranked candidate board (拾贝 scanner)
python -m shibei_onchain.cli scan

# 4. scan -> on-chain universe filter -> risk stack -> sized planned orders
python -m shibei_onchain.cli plan

# 5. one full autonomous cycle (read -> decide -> manage -> dry-run execute -> reconcile)
python -m shibei_onchain.cli run

# 6. a single hello-world swap through the Trust Wallet Agent Kit path
python -m shibei_onchain.cli swap-hello --token BNB --notional 20 --price 600
```

**One-command live-data demo** (real 拾贝 board + real CMC + real Aster *mainnet*
listings; still DRY-RUN, never signs):

```bash
PYTHONPATH=src SHIBEI_ONCHAIN_ASTER_BASE_URL=https://fapi.asterdex.com \
  python -m shibei_onchain.cli run
```

This runs the full funnel on live data: 拾贝 board → on-chain universe filter →
risk stack (5× notional cap · V0.2 time filter · circuit breaker) → sized orders,
plus **active management** of any open position (+1R→breakeven, 72h max-hold).
The default `.env` points Aster at **testnet** (tiny listing set), so use the
mainnet override above to see the real candidate funnel. If you demo during
北京时 08:00–15:59, opens are blocked by the V0.2 time filter — set
`SHIBEI_ONCHAIN_EXCLUDED_ENTRY_BEIJING_HOURS=` (empty) to allow them.

Run the test suite (pure stdlib + pytest):

```bash
PYTHONPATH=src python -m pytest -q          # 234 passing, no network
```

**Going live (multi-day unattended run):** preflight with `cli doctor` (read-only
readiness check — wallet/key, listings, marks, collateral, kill-switches), then
follow **[`docs/GOLIVE.md`](docs/GOLIVE.md)** — a safety-first runbook (arm the
gate, `cli loop --interval 3600` for hourly cadence, monitor, stop). The loop is
hardened so a single bad cycle can't kill a days-long run, and all risk state
(circuit breaker, high-water mark, day-start equity, position metadata) persists
across restarts.

```bash
PYTHONPATH=src python -m shibei_onchain.cli doctor    # READY to arm live?
```

**Backtest it yourself** (offline, deterministic — runs the real risk/management
engine): `cli backtest --scenario bull|chop|crash|deepv`, or `--data your.json`.
See [`docs/BACKTEST.md`](docs/BACKTEST.md). The live 拾贝 board is also exposed
read-only for judges — see [`docs/BOARD_API.md`](docs/BOARD_API.md).

---

## Going live on BSC testnet

Full step-by-step (verified live — RPC, POA, real PancakeSwap quotes) is in
**[`docs/TESTNET.md`](docs/TESTNET.md)**. In short:

```bash
pip install -e '.[onchain]'              # web3 + eth-account (web3 6.15.1 verified)
# .env already ships configured for BSC testnet web3 mode (dry-run, gate OFF)

# read-only verification — no private key, can't move funds:
PYTHONPATH=src python scripts/verify_onchain.py
# -> RPC connected, chain_id 97, POA OK, live USDT->BNB / USDT->BTC quotes

# dry-run the swap (real on-chain quote, no signing):
PYTHONPATH=src python -m shibei_onchain.cli swap-hello --token BNB --notional 5
```

To send a **real** testnet swap you (and only you) fill `WALLET_ADDRESS` +
`PRIVATE_KEY` (throwaway testnet key) in `.env`, faucet some test BNB, then flip
the four live-gate lines — the agent prints the tx hash + a `testnet.bscscan.com`
link. Use **BNB** (deepest testnet pool); ETH/CAKE testnet pools are unreliable.
Note: a full `run` cycle uses real-world prices, so on *testnet* (whose pool
prices are synthetic) the slippage guard will block opens — use `swap-hello` for
the testnet smoke; the full cycle is coherent on mainnet.

> Mainnet is for tiny smoke amounts only, executed by **you** with **your** key.
> This agent never asks for, transmits, or stores your key anywhere but your env.

---

## Live gate (why it won't trade by accident)

A real on-chain swap is submitted only when **every** condition holds (faithful to
拾贝's multi-condition live switch):

```
trading_enabled  AND  not dry_run  AND  confirm_text == I_UNDERSTAND_SHIBEI_ONCHAIN_LIVE_RISK
AND execution_adapter_ready  AND  wallet_address present  AND  private_key present
```

`status` prints exactly which conditions are still blocking.

---

## Project layout

```
shibei-onchain-agent/
├─ config/tokens.bsc.json          on-chain tradeable universe (mainnet + testnet)
├─ src/shibei_onchain/
│  ├─ models.py                    frozen data contracts (Candidate, PlannedOrder, ...)
│  ├─ config.py                    env -> AgentConfig + RiskParams (拾贝 defaults)
│  ├─ signals/
│  │  ├─ cmc_agent_hub.py          CMC Agent Hub client (mock | mcp | x402)
│  │  └─ regime_gate.py            BTC environment hard gate
│  ├─ brain/
│  │  ├─ scanner.py                ranked candidate board (拾贝 board | latest.json | CMC quotes | mock)
│  │  ├─ risk.py                   THE risk stack
│  │  └─ planner.py                sizing -> planned_orders
│  ├─ onchain/
│  │  ├─ aster_perp.py             Aster Futures client (mock | v3 EIP-712 REST)
│  │  ├─ aster_adapter.py          Aster venue: both legs (long/short perp) + reconcile
│  │  ├─ universe_filter.py        ranking ∩ on-chain tradeable + final guard
│  │  ├─ pancake_router.py         PancakeSwap quote / path / minAmountOut
│  │  ├─ twak_client.py            Trust Wallet Agent Kit wrapper (self-custody)
│  │  ├─ execution_adapter.py      OnChainExecutionAdapter (PancakeSwap spot)
│  │  └─ reconcile.py              on-chain balances -> account_state, exits
│  ├─ delivery/feishu.py           signed Feishu alerts
│  ├─ exec_live.py                 orchestrator + execute_shibei_v3_onchain_prepared_orders
│  └─ cli.py                       status / signal / scan / plan / run / swap-hello / loop
├─ tests/                          unit tests (run with zero extra deps)
└─ docs/                           INTERFACES.md, BUIDL submission notes
```

## Relationship to 拾贝 V3.0

`planned_orders` and the risk parameters here mirror 拾贝's production
`normalize_v2_execution_candidate` / `build_combo_config` semantics
(2% risk, 2.5R TP, 1.0R breakeven, 20% max stop, 5× notional, conflict
long-priority, S16-A short max-hold 16h/24h). The same brain output can feed
either a Binance perp adapter or this on-chain adapter — that decoupling is the
whole reason a 5-day on-chain port is feasible.

**Both legs run on Aster perps by default** (`venue=aster`): a long signal opens
a long perp, a short signal (拾贝 S16-A) opens a short perp, with the 2.5R
take-profit and stop placed as resting reduce-only orders on-exchange. Aster's
Futures API is Binance-USDⓈ-M-shaped and uses **v3 EIP-712** auth — a registered
agent/signer wallet signs each order, so it is pure self-custody (the Trust
Wallet Agent Kit model). A PancakeSwap long-spot venue (`venue=pancake`) is also
included as a simpler fallback. See [`docs/TESTNET.md`](docs/TESTNET.md).

## Safety boundary

This repository builds and simulates everything end-to-end. It will **not**
execute real on-chain trades or move funds on its own: the private key and the
final live switch stay with you and your Trust Wallet device. Testnet-first;
mainnet only tiny, manual smoke amounts.
