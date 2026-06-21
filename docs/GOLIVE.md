# Going live — multi-day unattended run (safety-first runbook)

This is the procedure to run the agent **live for several days** (e.g. a
competition's live-verification window). Read the warning first.

## ⚠️ Read this first

- **汲水 V0.2 is a negative-expectancy strategy** in our own backtest (2026 H1,
  **−22.5%**). Running it live for days will, on expectation, **lose money**. The
  point of a live run is to prove **autonomy + safety + observability**, not
  profit. Size accordingly.
- **Prefer the smallest real stake** that satisfies the requirement. If the
  verification allows **testnet**, run on testnet (`aster.mode=api`,
  `base_url=https://fapi.asterdex-testnet.com`) — zero financial risk, identical
  code path.
- If it must be **mainnet**, use a **throwaway wallet with a tiny balance**
  (tens of USDT). The key shared during development is compromised — **rotate to
  a fresh key** before funding anything real.
- The agent self-custodies: the key signs locally (EIP-712) and is never logged
  or transmitted. You, and only you, arm the live gate.

## Built-in safety (already on for `water_v02`)

| Guard | Default | What it does |
|---|---|---|
| **Drawdown kill-switch** | halt at **−25%** from peak equity | freezes *all* new opens; open positions still managed |
| **Daily-loss kill-switch** | halt at **−10%** from day-start equity | same, resets each UTC day |
| **Max-hold backstop** | **72h** | flattens any position held too long |
| **+1R → breakeven** | on | pulls the stop to entry once up 1R |
| Resting stop + 2.5R TP | on | placed on-exchange at open (survive agent downtime) |
| Per-coin 24h circuit breaker | 2 stop-losses | halts that coin for 24h |
| ≤3 opens/hour · ≤5× notional · ≤12% total risk · BTC gate | on | the rest of the stack |

Tune via env (see `.env`): `MAX_ACCOUNT_DRAWDOWN_PCT`, `MAX_DAILY_LOSS_PCT`,
`LONG_MAX_HOLD_HOURS`.

## Step 1 — preflight (read-only, sends nothing)

```bash
PYTHONPATH=src python -m shibei_onchain.cli doctor          # testnet (.env default)
# or against mainnet listings:
PYTHONPATH=src SHIBEI_ONCHAIN_ASTER_BASE_URL=https://fapi.asterdex.com \
  python -m shibei_onchain.cli doctor
```

Must read **`Verdict: READY to arm live`**. Fix any `✗` critical item first
(wallet/key/collateral/listings/mark). "live gate CLOSED" is expected here.

## Step 2 — fund the wallet

Deposit USDT collateral to the Aster account for your wallet. `doctor` flags
equity under \$20 as too small to open real trades. Keep it small.

## Step 3 — arm the live gate (deliberate, 5 flips)

In `.env` (or exported env), set **all** of:

```bash
SHIBEI_ONCHAIN_DRY_RUN=false
SHIBEI_ONCHAIN_TRADING_ENABLED=true
SHIBEI_ONCHAIN_EXECUTION_ADAPTER_READY=true
SHIBEI_ONCHAIN_CONFIRM_TEXT=I_UNDERSTAND_SHIBEI_ONCHAIN_LIVE_RISK
# wallet + key already set
```

Re-run `cli doctor` — the live gate should now read **OPEN**. Until every one of
these holds, the agent simulates and signs nothing.

## Step 4 — run unattended (hourly = V0.2 整点 cadence)

```bash
# 3600s = decide once an hour, like V0.2's hourly decision cadence.
nohup env PYTHONPATH=src SHIBEI_ONCHAIN_ASTER_BASE_URL=https://fapi.asterdex.com \
  python -m shibei_onchain.cli loop --interval 3600 \
  > data/state/loop.out 2>&1 &
echo $! > data/state/loop.pid
```

The loop is hardened: a single bad cycle (transient RPC/exchange error) is logged
and the loop continues. State (circuit breaker, per-hour budget, position
metadata, high-water mark, day-start equity) persists to
`data/state/onchain_live.json` and survives restarts. A `systemd` unit or `tmux`
session works equally well.

## Step 5 — monitor (daily)

```bash
tail -f data/state/loop.out                         # live cycle log; watch for HALT=...
PYTHONPATH=src python -m shibei_onchain.cli status   # equity, kill-switch settings
python -c "import json;d=json.load(open('data/state/onchain_live.json'));\
print('equity',d['account_after']['equity_usd']);\
print('safety',d['safety_state']);\
print('halt',d['decision'].get('notes'))"
```

Each cycle line shows `equity=… opens=… exits=… executed=… live=True`. If a
kill-switch trips you'll see `HALT=account_drawdown_halt` (or `daily_loss_halt`)
and opens stop while open positions keep being managed.

## Step 6 — stop

```bash
kill "$(cat data/state/loop.pid)"        # graceful stop
# OR instantly disarm without killing the process: set DRY_RUN=true (next cycle simulates)
```

To flatten everything immediately, close positions from the Aster UI, or let the
resting stops / 72h max-hold do it.

## Post-run

- Rotate every credential used (key, CMC key, board password, TWAK creds).
- Keep `onchain_live.json` + `loop.out` as the run evidence for submission.
