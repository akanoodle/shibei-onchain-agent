"""backtest.py — a self-contained, judge-runnable backtest of the 汲水 V0.2 agent.

This replays the agent's **real brain** — the same ``apply_risk_stack`` /
``compute_sizing`` and the same +1R-breakeven / 2.5R-TP / max-hold / kill-switch
logic that runs live — over a price panel, and reports the metrics judges score
on (returns, max drawdown, win rate, profit factor, exit breakdown).

It is fully **offline, deterministic, and dependency-free** (stdlib + the agent's
own pure modules). No centralized-exchange API, no network, no keys.

Two data modes:

    * built-in **scenarios** (``bull`` / ``chop`` / ``crash`` / ``deepv``) —
      deterministic price paths so anyone gets byte-identical results; useful to
      see how the risk engine behaves across regimes (e.g. the drawdown
      kill-switch firing in ``crash``).
    * **bring-your-own data** (``--data file.json``) — a judge supplies a real
      close-price panel and backtests the agent on it themselves. Format::

          {"interval_hours": 24, "start": "2026-01-01T12:00:00Z",
           "closes": {"BTCUSDT": [...], "ETHUSDT": [...], ...}}

Selection is a momentum ranking over the panel (the same logic as the live
scanner's CMC fallback), since historical 拾贝-board snapshots aren't published.
The **authoritative** full-universe V0.2 backtest (real 拾贝 board + real prices,
2026 H1) is **−22.5%** — see docs/BACKTEST.md; this harness reproduces the
*engine's* behavior, not that exact figure.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from shibei_onchain.config import AgentConfig, RiskParams, load_config
from shibei_onchain.models import (
    AccountState,
    BtcTrend,
    Candidate,
    MarketRegime,
    MarketSignal,
    Position,
    Side,
    StopLossEvent,
    base_asset_of,
)
from shibei_onchain.brain.risk import apply_risk_stack
from shibei_onchain.brain.planner import compute_sizing


_DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
                    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT"]
_BASE_PRICES = {"BTCUSDT": 65000.0, "ETHUSDT": 3000.0, "BNBUSDT": 600.0,
                "SOLUSDT": 150.0, "XRPUSDT": 0.6, "DOGEUSDT": 0.15,
                "ADAUSDT": 0.45, "LINKUSDT": 15.0}

_LOOKBACK = 3          # bars used for the momentum ranking
_FEE_RATE = 0.0005     # taker fee per side
_SLIP_RATE = 0.0005    # slippage per side


# --------------------------------------------------------------------------- #
# deterministic scenario generator (no RNG — reproducible byte-for-byte)
# --------------------------------------------------------------------------- #
def make_scenario(name: str, bars: int = 60) -> Dict[str, Any]:
    """Return a deterministic price panel for a named market regime."""
    name = (name or "bull").lower()
    closes: Dict[str, List[float]] = {}
    for s_idx, sym in enumerate(_DEFAULT_SYMBOLS):
        base = _BASE_PRICES.get(sym, 100.0)
        phase = s_idx * 0.7
        series: List[float] = []
        for i in range(bars):
            wave = math.sin(i / 5.0 + phase)
            if name == "bull":
                drift = 0.006 * i
                amp = 0.03
            elif name == "chop":
                drift = 0.0
                amp = 0.06
            elif name == "crash":
                drift = -0.008 * i
                amp = 0.025
            elif name == "deepv":
                # down then up (V): linear in |i - mid|
                mid = bars / 2.0
                drift = -0.012 * (mid - abs(i - mid))
                amp = 0.03
            else:
                drift = 0.004 * i
                amp = 0.03
            factor = 1.0 + drift + amp * wave
            series.append(round(base * max(0.05, factor), 8))
        closes[sym] = series
    return {
        "name": name,
        "interval_hours": 24,
        "start": "2026-01-01T12:00:00Z",     # 12:00 UTC == 20:00 北京时 (entry allowed)
        "closes": closes,
    }


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    symbol: str
    entry_bar: int
    exit_bar: int
    entry: float
    exit: float
    qty: float
    pnl: float
    exit_reason: str          # take_profit | stop_loss | max_hold | data_end


@dataclass
class BacktestResult:
    name: str
    bars: int
    initial_equity: float
    final_equity: float
    equity_curve: List[float] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)
    kill_switch_bars: int = 0
    halt_reasons: Dict[str, int] = field(default_factory=dict)

    @property
    def total_return_pct(self) -> float:
        if self.initial_equity <= 0:
            return 0.0
        return (self.final_equity / self.initial_equity - 1.0) * 100.0

    @property
    def max_drawdown_pct(self) -> float:
        peak = self.initial_equity
        mdd = 0.0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                mdd = max(mdd, (peak - eq) / peak)
        return mdd * 100.0

    def metrics(self) -> Dict[str, Any]:
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        exit_breakdown: Dict[str, int] = {}
        for t in self.trades:
            exit_breakdown[t.exit_reason] = exit_breakdown.get(t.exit_reason, 0) + 1
        return {
            "scenario": self.name,
            "bars": self.bars,
            "initial_equity": round(self.initial_equity, 2),
            "final_equity": round(self.final_equity, 2),
            "total_return_pct": round(self.total_return_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "trades": len(self.trades),
            "win_rate_pct": round(100.0 * len(wins) / len(self.trades), 2) if self.trades else 0.0,
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
            "exit_breakdown": exit_breakdown,
            "kill_switch_bars": self.kill_switch_bars,
            "halt_reasons": self.halt_reasons,
        }


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #
def _parse_start(s: str) -> datetime:
    raw = (s or "2026-01-01T12:00:00Z").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _signal_from_btc(closes: Dict[str, List[float]], i: int) -> MarketSignal:
    """Derive a BTC-environment signal from the panel so the BTC gate is live."""
    btc = closes.get("BTCUSDT")
    trend, regime, price = BtcTrend.FLAT, MarketRegime.NEUTRAL, None
    if btc and i >= _LOOKBACK and btc[i - _LOOKBACK] > 0:
        price = btc[i]
        chg = (btc[i] / btc[i - _LOOKBACK] - 1.0) * 100.0
        if chg > 1.5:
            trend, regime = BtcTrend.UP, MarketRegime.RISK_ON
        elif chg < -1.5:
            trend, regime = BtcTrend.DOWN, MarketRegime.RISK_OFF
    return MarketSignal(regime=regime, btc_trend=trend, btc_price=price, source="backtest")


def run_backtest(
    data: Dict[str, Any],
    config: Optional[AgentConfig] = None,
    *,
    top_n: int = 3,
) -> BacktestResult:
    """Replay the agent's risk + management engine over a close-price panel."""
    cfg = config or load_config()
    risk: RiskParams = cfg.risk
    closes: Dict[str, List[float]] = {k: [float(x) for x in v] for k, v in (data.get("closes") or {}).items()}
    symbols = [s for s in (data.get("symbols") or list(closes.keys())) if s in closes]
    if not symbols:
        symbols = list(closes.keys())
    n_bars = min((len(closes[s]) for s in symbols), default=0)
    interval_h = float(data.get("interval_hours") or 24.0)
    start = _parse_start(str(data.get("start") or "2026-01-01T12:00:00Z"))
    stop_pct = float(getattr(risk, "initial_stop_distance_pct", 0.06) or 0.06)
    tp_r = float(getattr(risk, "take_profit_r", 2.5) or 2.5)
    be_r = float(getattr(risk, "long_breakeven_r", 1.0) or 1.0)
    max_hold_h = float(getattr(risk, "long_max_hold_hours", 0.0) or 0.0)
    max_hold_bars = int(max_hold_h / interval_h) if (max_hold_h > 0 and interval_h > 0) else 0

    equity = float(getattr(cfg, "initial_equity_usd", 1000.0) or 1000.0)
    result = BacktestResult(name=str(data.get("name") or "custom"), bars=n_bars,
                            initial_equity=equity, final_equity=equity)
    # open positions: symbol -> dict(entry, stop, qty, tp, risk_per_unit, opened_bar)
    book: Dict[str, Dict[str, Any]] = {}
    stop_events: List[StopLossEvent] = []
    peak_equity = equity
    day_start_equity = equity
    day_key = start.strftime("%Y-%m-%d")

    def _close_trade(sym: str, pos: Dict[str, Any], i: int, exit_px: float, reason: str) -> None:
        nonlocal equity
        qty = pos["qty"]
        gross = qty * (exit_px - pos["entry"])
        cost = qty * exit_px * (_FEE_RATE + _SLIP_RATE) + qty * pos["entry"] * (_FEE_RATE + _SLIP_RATE)
        pnl = gross - cost
        equity += pnl
        result.trades.append(Trade(symbol=sym, entry_bar=pos["opened_bar"], exit_bar=i,
                                   entry=pos["entry"], exit=exit_px, qty=qty, pnl=pnl, exit_reason=reason))
        if reason == "stop_loss":
            stop_events.append(StopLossEvent(base_asset=base_asset_of(sym), side=Side.LONG,
                                             at=(start + timedelta(hours=interval_h * i)).isoformat()))

    for i in range(n_bars):
        now = start + timedelta(hours=interval_h * i)
        # ---- 1) manage / exit open positions at this bar's close --------- #
        for sym in list(book.keys()):
            pos = book[sym]
            px = closes[sym][i]
            # +1R -> breakeven (move stop to entry once)
            if pos["risk_per_unit"] > 0:
                rr = (px - pos["entry"]) / pos["risk_per_unit"]
                if rr >= be_r and pos["stop"] < pos["entry"]:
                    pos["stop"] = pos["entry"]
            # exit checks (priority: stop, then take-profit, then max-hold)
            if px <= pos["stop"]:
                _close_trade(sym, pos, i, pos["stop"], "stop_loss"); del book[sym]
            elif px >= pos["tp"]:
                _close_trade(sym, pos, i, pos["tp"], "take_profit"); del book[sym]
            elif max_hold_bars and (i - pos["opened_bar"]) >= max_hold_bars:
                _close_trade(sym, pos, i, px, "max_hold"); del book[sym]

        # ---- 2) high-water mark + day-start equity (kill-switch inputs) -- #
        if equity > 0:
            peak_equity = max(peak_equity, equity)
            today = now.strftime("%Y-%m-%d")
            if today != day_key:
                day_start_equity, day_key = equity, today

        # ---- 3) build candidates from a momentum ranking (long-only) ----- #
        candidates: List[Candidate] = []
        if i >= _LOOKBACK:
            ranked = []
            for sym in symbols:
                if closes[sym][i - _LOOKBACK] <= 0:
                    continue
                mom = closes[sym][i] / closes[sym][i - _LOOKBACK] - 1.0
                ranked.append((mom, sym))
            ranked.sort(reverse=True)
            for rank, (mom, sym) in enumerate(ranked[:max(0, top_n)]):
                price = closes[sym][i]
                if price <= 0 or sym in book:
                    continue
                candidates.append(Candidate(
                    symbol=sym, side=Side.LONG, price=price,
                    stop_loss_price=price * (1.0 - stop_pct),
                    risk_per_trade_pct=risk.long_risk_pct,
                    take_profit_r_multiple=tp_r, breakeven_r_multiple=be_r,
                    score=round(100.0 - rank, 4),
                ))

        # ---- 4) run the REAL risk stack (caps, kill-switch, BTC gate) ---- #
        positions = [Position(symbol=s, side=Side.LONG, base_asset=base_asset_of(s),
                              quantity=p["qty"], entry_price=p["entry"],
                              notional_usd=p["qty"] * closes[s][i],
                              stop_loss_price=p["stop"]) for s, p in book.items()]
        account = AccountState(equity_usd=equity, positions=positions,
                               stop_loss_events=list(stop_events),
                               peak_equity_usd=peak_equity, day_start_equity_usd=day_start_equity)
        signal = _signal_from_btc(closes, i)
        decision = apply_risk_stack(candidates, account, signal, risk, now=now)
        halt = decision.risk_state.get("account_halt_reason")
        if halt:
            result.kill_switch_bars += 1
            result.halt_reasons[halt] = result.halt_reasons.get(halt, 0) + 1

        # ---- 5) open approved candidates at this bar's close ------------- #
        for cand in decision.approved:
            sym = cand.symbol
            if sym in book:
                continue
            sizing = compute_sizing(cand, equity, risk)
            notional = sizing["notional_usd"]
            qty = sizing["quantity"]
            if qty <= 0 or notional <= 0:
                continue
            entry = cand.price
            risk_per_unit = entry - cand.stop_loss_price
            book[sym] = {
                "entry": entry, "stop": cand.stop_loss_price, "qty": qty,
                "tp": entry + tp_r * risk_per_unit, "risk_per_unit": risk_per_unit,
                "opened_bar": i,
            }

        result.equity_curve.append(round(equity, 6))

    # ---- close anything still open at the last bar (data-end) ------------ #
    last = n_bars - 1
    for sym in list(book.keys()):
        _close_trade(sym, book[sym], last, closes[sym][last], "data_end"); del book[sym]
    result.final_equity = round(equity, 6)
    if result.equity_curve:
        result.equity_curve[-1] = result.final_equity
    return result


def load_panel(path: str) -> Dict[str, Any]:
    """Load a judge-supplied close-price panel JSON (raises on bad file)."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("closes"), dict):
        raise ValueError("panel must be an object with a 'closes' map")
    return data
