# 拾贝 Leaderboard API — for judges (read-only)

A **read-only mirror** of the live 拾贝 (Shibei) ranking board — the proprietary
signal the agent uses to pick coins. It is a static snapshot served on a
dedicated port, **fully isolated from the production trading system** (a 1-minute
cron reads the board over localhost and publishes the JSON; the live app is never
touched). Judges can fetch it directly or point the agent at it — no login, no
credentials.

## Endpoint

```
GET http://154.37.219.63:8770/board/<TOKEN>.json
```

- **TOKEN** (acts as the read-only access key — keep it to the judging group):
  provided privately in the DoraHacks submission notes (kept out of the public
  repo so the board signal isn't leaked to the whole internet; revocable after judging).
- Full URL:
  `http://154.37.219.63:8770/board/<TOKEN>.json`
- No auth header / no login. Only this exact path is served (any other path → 403).
- CORS enabled (`Access-Control-Allow-Origin: *`), so a browser/agent can fetch it.
- Refresh cadence: **~1 minute** (snapshot of the live board). `as_of` carries the
  board's own timestamp.
- Please poll politely (≤ once/minute is plenty; the agent decides hourly).
- The owner can revoke this at any time after judging (delete the token file).

## Quick check

```bash
curl -s 'http://154.37.219.63:8770/board/<TOKEN>.json' | head -c 400
```

## Response schema

Top-level object:

| field | type | meaning |
|---|---|---|
| `as_of` | string (ISO-8601 UTC) | when the board was computed |
| `run_id` | string | board run identifier |
| `schema_version` | string | `public_leaderboard_latest/v1` |
| `timeframe` | string | `live` |
| `source` | string | `leaderboard_frames` |
| `summary` | object | board-level summary counts |
| `tracks` | object | the three ranked tracks (below) |

`tracks` has three keys, each an **array of ranked entries**:
`main_track`, `fast_track`, `strong_supplement`.

Each entry:

| field | type | meaning |
|---|---|---|
| `symbol` | string | e.g. `BTCUSDT` (long/momentum candidate) |
| `rank` | int | rank within the track |
| `score` | float | board score (higher = stronger) |
| `track_name` | string | which track produced it |
| `priority` | string | `A` / `B` … internal priority bucket |
| `current_price` | float | latest price (USD) |
| `price_close_1h` | float | 1h close |
| `ema21_1h` | float | 1h EMA(21) |
| `ema21_filter_passed` | bool | passed the EMA trend filter |
| `oi_usd` | float | open interest (USD) |
| `vol24h_usd` | float | 24h volume (USD) |
| `relative_strength_score` | float \| null | RS score (may be null on fast track) |
| `relative_strength_label` | string \| null | RS label |
| `matched_tracks` | string[] | every track this symbol matched |
| `also_fast_signal` / `also_strong_signal` | bool | cross-track flags |
| `include_reason` | string[] | why it was included (e.g. `liquidity_passed`, `ema21_passed`) |
| `push_count_48h` / `weighted_push_score_48h` | number | recent momentum push stats |

> The board is **long-only / momentum-strong**; liquidity, OI, market-cap and
> EMA-cross filters are already applied upstream when the board is built, so every
> listed symbol has passed them (see `include_reason`).

### Example entry

```json
{
  "symbol": "BTCUSDT", "rank": 1, "score": 85.0, "track_name": "fast_track",
  "priority": "A", "current_price": 64098.4, "ema21_1h": 64019.6,
  "ema21_filter_passed": true, "oi_usd": 6378492596.67, "vol24h_usd": 5817178014.27,
  "matched_tracks": ["fast_track"], "include_reason": ["liquidity_passed", "ema21_passed"]
}
```

## Point the agent at it (judge reproduction)

The agent consumes this board with **no credentials** — set three env vars (or the
matching `.env` lines) and run any command:

```bash
export SHIBEI_ONCHAIN_BOARD_MODE=api
export SHIBEI_ONCHAIN_BOARD_BASE_URL=http://154.37.219.63:8770
export SHIBEI_ONCHAIN_BOARD_LEADERBOARD_PATH=/board/<TOKEN>.json
# leave BOARD_USERNAME / BOARD_PASSWORD empty -> no login

PYTHONPATH=src python -m shibei_onchain.cli scan      # ranked long candidates from the live board
PYTHONPATH=src python -m shibei_onchain.cli plan      # board -> universe filter -> risk stack -> sized orders
```

If the mirror is unreachable, the scanner falls back to **CoinMarketCap** quotes
(never a centralized-exchange API), so the agent still runs.

## Notes on the data source

This board is served by the team's own 拾贝 server (`154.37.219.63`). The mirror
endpoint above exists solely so judges can read it without the production cookie
login; it is rate-limited by the 1-minute snapshot and isolated from the live
trading process.
