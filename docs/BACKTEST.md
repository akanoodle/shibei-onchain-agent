# Backtest — run it yourself

The agent ships a **self-contained, deterministic backtest** so anyone (judges
included) can replay its decision + risk + management engine offline. No network,
no keys, no centralized-exchange API.

```bash
PYTHONPATH=src python -m shibei_onchain.cli backtest --scenario bull
PYTHONPATH=src python -m shibei_onchain.cli backtest --scenario crash
PYTHONPATH=src python -m shibei_onchain.cli backtest --data my_prices.json   # your own data
```

## What it actually runs

The backtest drives the agent's **real** modules — `apply_risk_stack`,
`compute_sizing`, the +1R→breakeven / 2.5R-TP / 72h-max-hold logic, and the
−25% / −10% account kill-switches — over a close-price panel, bar by bar. So you
are validating the **shipping engine**, not a re-implementation. It reports the
metrics the hackathon scores on:

- total return %, max drawdown %, win rate, profit factor
- exit-reason breakdown (take_profit / stop_loss / max_hold / data_end)
- how many bars the kill-switch froze new opens
- a text equity-curve sparkline

## Two data modes

1. **Built-in scenarios** (`--scenario bull|chop|crash|deepv`) — deterministic
   price paths, so everyone gets byte-identical results. Great for seeing the risk
   engine behave per regime (e.g. stops + a frozen book in `crash`).
2. **Bring your own prices** (`--data panel.json`) — backtest on real data you
   supply:

   ```json
   {
     "interval_hours": 24,
     "start": "2026-01-01T12:00:00Z",
     "closes": {
       "BTCUSDT": [65000.0, 65400.0, "..."],
       "ETHUSDT": [3000.0, 3020.0, "..."]
     }
   }
   ```

   Selection is a momentum ranking over the panel (the same logic as the live
   scanner's CMC fallback), since historical 拾贝-board snapshots aren't published.

## Honest note on the strategy's return

The built-in scenarios are **illustrative regimes**, not a forecast. The
**authoritative** backtest of 汲水 V0.2 — real 拾贝 board + real prices, 2026 H1,
1448 trades, full risk stack, independently audited — is **−22.5%** (a
negative-expectancy long-only leg in that window; the edge is drawdown control,
not returns). We do not claim otherwise. See `docs/BUIDL_SUBMISSION.md`.
