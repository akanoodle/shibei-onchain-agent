# BUIDL submission notes (BNB Hack · Track 1)

Submission guide for the DoraHacks BUIDL page. **Verify the exact required
fields, judging weights, and deadline timezone on the live DoraHacks page** —
that SPA content can't be read from here.

- Hackathon: https://dorahacks.io/hackathon/bnbhack-twt-cmc
- Track: **Track 1 · Autonomous Trading Agents**
- Project name: **Shibei On-Chain Agent (拾贝链上交易 Agent)**

## One-line pitch

An autonomous, self-custody trading agent on BNB Chain that picks coins from the
live **拾贝 (Shibei) ranking board**, sizes & guards every trade with a
production multi-layer **risk brain**, and executes **long-only perp trades on
Aster** — using all three sponsor capabilities, no human in the loop.

## Strategy: 汲水 V0.2 (long-only) — and an honest framing

The agent runs the **汲水 (Jishui) V0.2** strategy: a *long-only* momentum
strategy over the 拾贝 board (`main_track / fast_track / strong_supplement`),
with the 北京时 08:00–15:59 no-entry window, a 6% initial stop, +1R→breakeven,
2.5R take-profit, and a gentle 72h max-hold backstop.

**We do not claim this strategy is profitable.** In our own backtest (2026 H1,
real caliber) V0.2 long-only returns **−22.5%** — it is a negative-expectancy leg
in that window. Track 1 is judged on **live PnL (returns + drawdown + risk-adjusted
performance) over the Jun 22–28 live window** plus rule adherence — so this
strategy's edge here is **drawdown control** (the −25% / −10% kill-switches),
not returns, while it demonstrates full autonomy and all three capabilities. The strategy is the
*payload*; the engineering (signal → ranking → risk → on-chain execution →
reconcile → active management) is the product. Choosing the honest, lightweight
long-only payload over the heavier long+short one is a deliberate scope call for
the deadline.

## Three capabilities — how each is genuinely used

1. **CoinMarketCap Agent Hub (signal).** `signals/cmc_agent_hub.py` pulls
   decision-grade market state via **MCP** (`https://mcp.coinmarketcap.com/mcp`,
   header `X-CMC-MCP-API-KEY`) — real BTC price/trend, market-cap RSI regime, and
   derivatives OI risk flags, derived from the live tools. Used for candidate
   enrichment **and** as a **hard BTC-environment risk gate**, which also closes a
   known 拾贝 V3.0 gap (its BTC gate previously only logged). Keyless **x402**
   variant supported (~$0.01 USDC/call). CMC is **also** the scanner's live
   market-data fallback — the agent uses **no centralized-exchange (CEX) API**.
2. **Trust Wallet Agent Kit (execution / self-custody).** `onchain/twak_client.py`
   wraps the official `twak` CLI (`@trustwallet/cli`) for self-custody swaps on
   BNB Chain — wallet create/register/address/balance + `twak swap … --chain bsc`.
   The model is identical to how the Aster venue signs: the user approves an
   agent/signer wallet once; the key never leaves the device, no custodial
   transfer.
3. **BNB Chain (venue).** Execution settles on BNB Chain via **Aster** — the
   BNB-native perpetual DEX (formerly APX/Astherus, YZi Labs backed) that Trust
   Wallet integrates. v3 **EIP-712** self-custody auth (`/fapi/v3/*`). A real
   **mainnet** fill was executed during development (ETH long + resting
   stop/TP). A PancakeSwap long-spot venue (`venue=pancake`) is also included.

## What the agent actually does each cycle (`cli run`)

```
CMC signal ─► read Aster account (reconcile + enrich open positions)
拾贝 board ─► universe filter (on-chain tradeable ∩ board) ─► risk stack
   ─► sized planned orders (opens)
   ─► active management: +1R→breakeven (MOVE_STOP), 72h max-hold (CLOSE)
   ─► execute on Aster (self-custody EIP-712)  ─► account writeback + state
```

The full funnel is proven on live data: real 拾贝 board → 12 long candidates →
Aster mainnet listing filter → risk stack (5x notional cap / time filter /
circuit breaker) → sized orders, plus active management of open positions.

## Risk discipline (the moat vs. demo bots) — enforced before any order

- **2% risk/trade**, **2.5R TP**, **+1R→breakeven**, 6% initial stop, **72h max-hold backstop**
- **Account kill-switches** — freeze all opens on **−25% drawdown** / **−10% daily loss** (the P0 gap 拾贝 V3.0 lacked; lets it run unattended for days without bleeding out)
- **汲水 V0.2 time filter** — no opens during 北京时 08:00–15:59
- **≤3 new orders/hour**, **≤12% total initial risk**, **≤5× notional cap**
- **Same-coin 24h circuit breaker** (2 stop-losses → halt that coin)
- **BTC environment hard gate** (CMC-fed) — the rule 拾贝 V3.0 only logged
- Every rejection carries a machine-readable reason (`failures must be visible`)
- **DRY-RUN by default** behind a 6-condition live gate; key read locally, never logged

## Live-readiness (multi-day unattended run)

- **`cli doctor`** — one-command read-only preflight: wallet/key, Aster listings +
  live marks, collateral, board + CMC reachability, kill-switches → `READY to arm live`.
- **`cli loop --interval 3600`** — hourly cadence (≈ V0.2 整点决策); hardened so a
  bad cycle can't kill a days-long run; all risk state persists across restarts.
- **[`docs/GOLIVE.md`](GOLIVE.md)** — safety-first runbook (testnet-or-tiny-mainnet,
  arm the gate, run, monitor, stop).

## Scoring alignment (aim for the top band)

1. **All three capabilities genuinely used** (CMC MCP ✅ · Trust Wallet Agent Kit ✅ · BNB Chain/Aster ✅).
2. **End-to-end autonomy** — `cli run` / `cli loop`: read → decide → execute → manage, no human in the loop.
3. **Real risk control** — multi-layer stack + circuit breaker + active management + reconcile (`tests/test_risk.py`, `tests/test_position_manager.py`).
4. **Self-custody security** — key never leaves device, EIP-712 agent-wallet signing, slippage/gas guards, DRY-RUN default.
5. **x402 narrative** — CMC over x402 + Trust Wallet's native x402 = machine-autonomous-payment story.

## Demo script (≈3 min)

> One-shot demo command (mainnet listings, read-only, DRY-RUN — never signs):
> ```bash
> PYTHONPATH=src SHIBEI_ONCHAIN_ASTER_BASE_URL=https://fapi.asterdex.com \
>   python -m shibei_onchain.cli run
> ```

1. `cli status` — strategy = **water_v02**, long-only, DRY-RUN, closed live gate, risk params + V0.2 time window. ("Safe and real.")
2. `cli signal` — a live CMC signal (`source: mcp`, real BTC price) + the BTC gate it drives.
3. `cli scan` — the live 拾贝 board → ranked **long** candidates.
4. `cli plan` (mainnet override) — board → on-chain universe filter → risk stack → sized orders, with every rejected candidate's reason (the risk stack *working*).
5. Active management — show `+1R → breakeven` MOVE_STOP and the 72h max-hold backstop (see `tests/test_position_manager.py`; reproducible with a mock +1R position).
6. (Optional) the proven Aster **mainnet** fill (tx/order id) from development.

## Backtest & data access (for judges)

- **`cli backtest`** — offline, deterministic replay of the real risk/management
  engine (`--scenario bull|chop|crash|deepv` or `--data your.json`). See
  [`docs/BACKTEST.md`](BACKTEST.md). Authoritative V0.2 result: **−22.5%** (拾贝, 2026 H1).
- **Live board, read-only** — judges can read the proprietary 拾贝 leaderboard via
  a read-only mirror (no login), isolated from production. See [`docs/BOARD_API.md`](BOARD_API.md).

## Test suite

```bash
PYTHONPATH=src python -m pytest -q     # 234 passing, pure stdlib + pytest, no network
```

## Open items (owner to confirm before submit)

- [ ] DoraHacks registration (individual or team?).
- [ ] Record the demo video (loop above).
- [ ] `git init` + first commit + push public repo.
- [ ] **Rotate every credential** shared during development (Aster/main key, CMC key, board password, TWAK creds) — they were exchanged in chat.
- [ ] Decide demo venue config: Aster **testnet** (tiny listing set → mostly `not_aster_listed`) vs **mainnet** override (475 perps, real funnel) on a tiny throwaway wallet.
