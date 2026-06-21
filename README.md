# Shibei On-Chain Agent (拾贝链上交易 Agent)

> An autonomous, self-custody **AI trading agent on BNB Chain** for the BNB Hack
> (Track 1 · Autonomous Trading Agents). It reads the market, decides with a
> multi-layer risk engine, and executes **end-to-end on-chain** — no human in the
> loop, no custodial transfer of funds.

It uses **all three sponsor capabilities**:

| Layer | Sponsor capability | What it does here |
|---|---|---|
| **Signal** | **CoinMarketCap Agent Hub** (MCP / x402) | Market regime, BTC trend, liquidity & risk flags — used to enrich candidates **and** as a **hard risk gate**. CMC is also the scanner's live market-data feed — **no centralized-exchange API anywhere**. |
| **Decision** | Ranking board + risk engine | Picks long candidates from a live ranking board, sizes positions, and runs a **multi-layer risk stack**. |
| **Execution** | **Trust Wallet Agent Kit** @ **BNB Chain** | Self-custody execution on **Aster** (the BNB-native perp DEX). The private key never leaves the device; no custodial transfer. |

**Flow:** CMC signal + ranking board → on-chain universe filter → risk stack →
sized orders → Aster execution + active management → reconcile.
See [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## What it enforces (before any order reaches the chain)

- **2% risk per trade**, **2.5R take-profit**, **+1R move-to-breakeven**, 6% initial stop
- **Active position management** — move the stop to breakeven at +1R; a gentle **72h max-hold** backstop
- **Account kill-switches** — freeze **all** new opens on **−25% drawdown** or **−10% daily loss** (open positions keep being managed)
- **≤ 3 new orders/hour**, **≤ 5× notional cap**, **same-coin 24h circuit breaker**
- **Time-of-day filter** — no new opens during 北京时 08:00–15:59
- **BTC environment hard gate** fed by CMC regime / BTC-trend / risk-flags
- Self-custody **EIP-712** signing (key local, never logged), slippage + gas/margin guards, **DRY-RUN by default**

Every rejected candidate is recorded with a machine-readable reason — failures are
always visible, never dropped silently.

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

# 2. read-only live-readiness preflight (wallet/key, listings, marks, kill-switches)
python -m shibei_onchain.cli doctor

# 3. a CMC Agent Hub signal + the BTC environment gate it drives
python -m shibei_onchain.cli signal

# 4. the ranked candidate board
python -m shibei_onchain.cli scan

# 5. scan -> on-chain universe filter -> risk stack -> sized planned orders
python -m shibei_onchain.cli plan

# 6. one full autonomous cycle (read -> decide -> manage -> dry-run execute -> reconcile)
python -m shibei_onchain.cli run
```

Run the test suite (pure stdlib + pytest):

```bash
PYTHONPATH=src python -m pytest -q
```

**Going live (multi-day unattended run):** preflight with `cli doctor`, then follow
**[`docs/GOLIVE.md`](docs/GOLIVE.md)** — a safety-first runbook (arm the gate,
`cli loop --interval 3600` for hourly cadence, monitor, stop). The loop is hardened
so a single bad cycle can't kill a days-long run, and all risk state (circuit
breaker, high-water mark, day-start equity, position metadata) persists across
restarts.

---

## Live gate (why it won't trade by accident)

A real on-chain order is submitted only when **every** condition holds:

```
trading_enabled  AND  not dry_run  AND  confirm_text == I_UNDERSTAND_SHIBEI_ONCHAIN_LIVE_RISK
AND execution_adapter_ready  AND  wallet_address present  AND  private_key present
```

`status` prints exactly which conditions are still blocking. The private key is
read locally, used to sign locally, and never leaves the machine or gets logged.
All configuration is via `SHIBEI_ONCHAIN_*` environment variables (see `.env.example`).

---

## Project layout

```
shibei-onchain-agent/
├─ config/tokens.bsc.json          on-chain tradeable universe (mainnet + testnet)
├─ src/shibei_onchain/
│  ├─ models.py                    data contracts (Candidate, PlannedOrder, ...)
│  ├─ config.py                    env -> AgentConfig + RiskParams
│  ├─ signals/
│  │  ├─ cmc_agent_hub.py          CMC Agent Hub client (mock | mcp | x402)
│  │  └─ regime_gate.py            BTC environment hard gate
│  ├─ brain/
│  │  ├─ scanner.py                ranked candidate board (live board | CMC quotes | mock)
│  │  ├─ risk.py                   the risk stack
│  │  ├─ planner.py                sizing -> planned_orders
│  │  └─ position_manager.py       +1R breakeven move + max-hold backstop
│  ├─ onchain/
│  │  ├─ aster_perp.py             Aster Futures client (mock | v3 EIP-712 REST)
│  │  ├─ aster_adapter.py          Aster perp venue + reconcile
│  │  ├─ universe_filter.py        ranking ∩ on-chain tradeable + final guard
│  │  ├─ twak_client.py            Trust Wallet Agent Kit wrapper (self-custody)
│  │  └─ reconcile.py              on-chain balances -> account_state, exits
│  ├─ backtest.py                  offline deterministic backtest engine
│  ├─ exec_live.py                 orchestrator (read -> decide -> manage -> execute)
│  └─ cli.py                       status / doctor / signal / scan / plan / run / backtest / loop
├─ tests/                          unit tests (run with zero extra deps)
└─ docs/                           ARCHITECTURE, INTERFACES, GOLIVE, ...
```

---

## Safety boundary

This repository builds and simulates everything end-to-end. It will **not**
execute real on-chain trades or move funds on its own: the private key and the
final live switch stay with you and your Trust Wallet device. DRY-RUN by default;
go live only deliberately, with your own key, via the documented live gate.
