"""Command-line entry point for the Shibei On-Chain Agent.

    shibei-onchain status        # show config, live-gate, on-chain/CMC health
    shibei-onchain signal        # fetch a CMC Agent Hub market signal
    shibei-onchain scan          # show the ranked candidate board
    shibei-onchain plan          # scan -> universe filter -> risk -> planned orders (no execution)
    shibei-onchain run           # one full read->decide->execute(dry-run) cycle
    shibei-onchain swap-hello    # a single hello-world swap through Trust Wallet Agent Kit
    shibei-onchain loop --interval 60   # run cycles on an interval

Everything defaults to DRY-RUN: no swap is signed until the full live gate in
.env is satisfied. Add --json to any command for machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional

from shibei_onchain.config import AgentConfig, load_config
from shibei_onchain.models import OrderAction, PlannedOrder, Side, base_asset_of


def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    else:
        print(obj)


def _hr(title: str) -> None:
    print("\n" + title)
    print("─" * max(8, len(title)))


# --------------------------------------------------------------------------- #
def cmd_status(args: argparse.Namespace, config: AgentConfig) -> int:
    from shibei_onchain.exec_live import build_venue

    venue = build_venue(config)
    try:
        health = venue.health()
    except Exception as exc:  # pragma: no cover
        health = {"status": "error", "error": str(exc)}

    if args.json:
        _print({"config": config.redacted(), "venue": venue.name, "venue_health": health}, True)
        return 0

    red = config.redacted()
    strat = red.get("strategy", "v3")
    strat_label = "汲水 V0.2 (long-only)" if strat == "water_v02" else "拾贝 V3.0 (long+short)"
    _hr("Shibei On-Chain Agent · status")
    print(f"  mode              : {'LIVE' if red['live_orders_allowed'] else 'DRY-RUN / preview'}")
    print(f"  strategy          : {strat_label}")
    print(f"  venue             : {venue.name}")
    print(f"  chain             : {red['chain']} (id {red['chain_id']})")
    print(f"  CMC signal mode   : {red['cmc_mode']}")
    print(f"  ranking source    : {'拾贝 board API (' + red['board_auth'] + ')' if red['board_mode'] == 'api' else 'fallback (latest.json/klines/mock)'}")
    print(f"  wallet            : {red['wallet_address']}")
    print(f"  private key       : {red['private_key']}")
    if not red["live_orders_allowed"]:
        print(f"  live blocked by   : {', '.join(red['blocked_reasons']) or '(none)'}")
    _hr("Risk stack + management ({})".format(strat_label))
    r = red["risk"]
    print(f"  per-trade risk    : long {r['long_risk_pct']:.1%} / short {r['short_risk_pct']:.1%}")
    print(f"  take-profit       : {r['take_profit_r']}R   move-to-breakeven: +{r.get('long_breakeven_r', 1.0)}R")
    bj = r.get("excluded_entry_beijing_hours") or []
    print(f"  entry time filter : {('北京时 ' + str(min(bj)) + '–' + str(max(bj) + 1) + ' no-open') if bj else 'off'}")
    mh = r.get("long_max_hold_hours") or 0
    print(f"  max-hold backstop : {(str(mh) + 'h') if mh else 'off'}   total notional cap: {r['max_total_notional_multiple']}x")
    print(f"  total init risk   : <= {r['max_total_initial_risk_pct']:.0%}   orders/hour: <= {r['max_new_orders_per_hour']}")
    dd = r.get("max_account_drawdown_pct") or 0
    dl = r.get("max_daily_loss_pct") or 0
    print(f"  kill-switch       : drawdown {('-' + format(dd, '.0%')) if dd else 'off'} / daily {('-' + format(dl, '.0%')) if dl else 'off'}")
    print(f"  BTC gate enforced : {r['btc_gate_enabled']}")
    if venue.name == "aster":
        leg = "long-only" if not config.enable_short_leg else "long + short"
        _hr("Execution · Aster perps ({})".format(leg))
        print(f"  aster mode        : {red['aster_mode']}")
        print(f"  base url          : {red['aster_base_url']}")
        print(f"  agent signer      : {red['aster_signer']}")
        print(f"  signer key        : {red['aster_signer_key']}")
        print(f"  self-custody      : {health.get('self_custody')}")
        print(f"  short leg enabled : {config.enable_short_leg}")
    else:
        _hr("Execution · PancakeSwap spot")
        print(f"  TWAK self-custody : {health.get('self_custody')}  (mode={health.get('mode')}, key_present={health.get('key_present')})")
    return 0


def cmd_signal(args: argparse.Namespace, config: AgentConfig) -> int:
    from shibei_onchain.signals.cmc_agent_hub import CmcAgentHub
    from shibei_onchain.signals.regime_gate import evaluate_regime_gate

    sig = CmcAgentHub(config.cmc).fetch_market_signal()
    gate = evaluate_regime_gate(sig, config.risk)
    if args.json:
        from shibei_onchain.exec_live import _json_safe

        _print({"signal": _json_safe(sig), "regime_gate": gate}, True)
        return 0
    _hr("CMC Agent Hub · market signal")
    print(f"  source      : {sig.source}")
    print(f"  regime      : {sig.regime.value}")
    print(f"  btc_trend   : {sig.btc_trend.value}   btc_price: {sig.btc_price}")
    print(f"  fear_greed  : {sig.fear_greed}")
    print(f"  risk_flags  : {', '.join(sig.risk_flags) or '(none)'}")
    print(f"  notes       : {'; '.join(sig.notes) or '(none)'}")
    _hr("Regime gate (BTC environment hard gate)")
    print(f"  long_blocked : {gate.get('long_blocked')}")
    print(f"  short_blocked: {gate.get('short_blocked')}")
    print(f"  reasons      : {', '.join(gate.get('reasons') or []) or '(none)'}")
    return 0


def cmd_scan(args: argparse.Namespace, config: AgentConfig) -> int:
    from shibei_onchain.signals.cmc_agent_hub import CmcAgentHub
    from shibei_onchain.brain.scanner import Scanner

    sig = CmcAgentHub(config.cmc).fetch_market_signal()
    candidates = Scanner(config).scan(signal=sig)
    if args.json:
        from shibei_onchain.exec_live import _json_safe

        _print([_json_safe(c) for c in candidates], True)
        return 0
    _hr(f"Ranked candidates ({len(candidates)})")
    print(f"  {'#':>2}  {'symbol':<10} {'side':<5} {'price':>12} {'stop':>12} {'score':>7} {'rss':>6}")
    for i, c in enumerate(candidates, 1):
        print(
            f"  {i:>2}  {c.symbol:<10} {c.side.value:<5} {c.price:>12.4f} "
            f"{c.stop_loss_price:>12.4f} {c.score:>7.1f} {c.relative_strength_score:>6.1f}"
        )
    return 0


def cmd_plan(args: argparse.Namespace, config: AgentConfig) -> int:
    from shibei_onchain.signals.cmc_agent_hub import CmcAgentHub
    from shibei_onchain.brain.scanner import Scanner
    from shibei_onchain.brain.risk import apply_risk_stack
    from shibei_onchain.brain.planner import build_planned_orders
    from shibei_onchain.exec_live import build_venue

    sig = CmcAgentHub(config.cmc).fetch_market_signal()
    venue = build_venue(config)
    account = venue.read_account()
    candidates = Scanner(config).scan(signal=sig)
    kept, uskip = venue.filter_candidates(candidates, sig)
    decision = apply_risk_stack(kept, account, sig, config.risk)
    orders = build_planned_orders(decision.approved, account, config.risk)

    if args.json:
        from shibei_onchain.exec_live import _json_safe

        _print(
            {
                "universe_skipped": [_json_safe(s) for s in uskip],
                "risk_rejected": [_json_safe(s) for s in decision.rejected],
                "planned_orders": [_json_safe(o) for o in orders],
            },
            True,
        )
        return 0

    _hr(f"Planned orders ({len(orders)})")
    print(f"  {'symbol':<10} {'side':<5} {'action':<6} {'notional$':>12} {'qty':>14} {'risk$':>9} {'stop':>12}")
    for o in orders:
        print(
            f"  {o.symbol:<10} {o.side.value:<5} {o.action.value:<6} {o.notional_usd:>12.2f} "
            f"{o.quantity:>14.6f} {o.risk_budget_usd:>9.2f} {o.stop_loss_price:>12.4f}"
        )
    if uskip or decision.rejected:
        _hr("Rejected / skipped")
        for s in uskip + decision.rejected:
            print(f"  - {s.symbol:<10} [{s.stage}] {s.reason}")
    return 0


def cmd_run(args: argparse.Namespace, config: AgentConfig) -> int:
    from shibei_onchain.exec_live import OnChainAgent

    result = OnChainAgent(config).run_cycle(persist=not args.no_persist)
    if args.json:
        _print(result.to_dict(), True)
        return 0
    summary = result.summary()
    _hr("Cycle complete")
    for key, val in summary.items():
        print(f"  {key:<18}: {val}")
    if result.receipts:
        _hr("Receipts")
        for r in result.receipts:
            line = f"  {r.symbol:<10} {r.side.value:<5} {r.action.value:<6} {r.status:<8}"
            if r.tx_hash:
                line += f" tx={r.tx_hash[:14]}…"
            if r.error:
                line += f" err={r.error}"
            print(line)
    return 0


def cmd_swap_hello(args: argparse.Namespace, config: AgentConfig) -> int:
    """A single hello-world swap through the Trust Wallet Agent Kit path."""
    from shibei_onchain.exec_live import execute_shibei_v3_onchain_prepared_orders

    base = (args.token or "BNB").upper()
    symbol = base + "USDT"
    side = Side.LONG
    action = OrderAction.CLOSE if args.close else OrderAction.OPEN
    order = PlannedOrder(
        symbol=symbol,
        side=side,
        action=action,
        base_asset=base,
        quote_asset="USDT",
        notional_usd=float(args.notional),
        entry_price=float(args.price) if args.price else 0.0,
        stop_loss_price=float(args.price) * 0.94 if args.price else 0.0,
        risk_budget_usd=float(args.notional) * 0.06,
        max_slippage_bps=config.onchain.max_slippage_bps,
        strategy_id="hello_world",
        reason="swap-hello demo",
    )
    ref_prices = {base: float(args.price)} if args.price else None
    result = execute_shibei_v3_onchain_prepared_orders(
        [order], config, dry_run=None if not args.force_dry_run else True, ref_prices=ref_prices
    )
    if args.json:
        from shibei_onchain.exec_live import _json_safe

        _print(_json_safe(result), True)
        return 0
    _hr("swap-hello")
    print(f"  dry_run            : {result['dry_run']}")
    print(f"  live_orders_allowed: {result['live_orders_allowed']}")
    if result["blocked_reasons"]:
        print(f"  blocked_by         : {', '.join(result['blocked_reasons'])}")
    for r in result["receipts"]:
        print(f"  {r.symbol} {r.side.value} {r.action.value} -> {r.status}")
        print(f"     amount_in={r.amount_in} {r.token_in}  amount_out={r.amount_out} {r.token_out}")
        print(f"     min_out={r.min_amount_out}  price={r.price}")
        if r.tx_hash:
            print(f"     tx={r.tx_hash}")
        if r.explorer_url:
            print(f"     explorer={r.explorer_url}")
        if r.error:
            print(f"     error={r.error}")
    return 0


def cmd_twak_swap(args: argparse.Namespace, config: AgentConfig) -> int:
    """Genuine Trust Wallet Agent Kit swap on BNB Chain (quote by default)."""
    import os as _os

    from shibei_onchain.onchain.twak_client import TwakClient

    twak = TwakClient(config.onchain)
    if not args.no_signal:
        from shibei_onchain.signals.cmc_agent_hub import CmcAgentHub

        sig = CmcAgentHub(config.cmc).fetch_market_signal()
        _hr("CMC signal driving the swap")
        print(f"  regime={sig.regime.value}  btc_trend={sig.btc_trend.value}  btc_price={sig.btc_price}  (source={sig.source})")
    res = twak.cli_swap(
        amount=float(args.amount),
        from_symbol=args.from_symbol,
        to_symbol=args.to_symbol,
        quote_only=not args.execute,
        password=_os.environ.get("TWAK_WALLET_PASSWORD") if args.execute else None,
    )
    if args.json:
        from shibei_onchain.exec_live import _json_safe

        _print(_json_safe(res), True)
        return 0
    _hr("Trust Wallet Agent Kit · swap " + ("(EXECUTE)" if args.execute else "(quote-only)"))
    print(f"  {args.amount} {args.from_symbol} -> {args.to_symbol} on bsc  =>  {res.status}")
    if res.amount_out:
        print(f"  expected out: {res.amount_out} {args.to_symbol}")
    if res.tx_hash:
        print(f"  tx: {res.tx_hash}")
    if res.error:
        print(f"  error: {res.error}")
    return 0 if res.ok else 1


def cmd_doctor(args: argparse.Namespace, config: AgentConfig) -> int:
    """Preflight: read-only check that everything is in place to go live safely.
    Never sends an order. Verifies wallet/key, venue listings + mark prices,
    collateral, board + CMC reachability, and the safety kill-switches."""
    from shibei_onchain.exec_live import build_venue
    from shibei_onchain.signals.cmc_agent_hub import CmcAgentHub
    from shibei_onchain.signals.shibei_board import ShibeiBoardClient

    red = config.redacted()
    r = red["risk"]
    checks: List[Dict[str, Any]] = []

    def chk(ok, label, detail="", critical=True):
        checks.append({"ok": ok, "label": label, "detail": detail, "critical": critical})

    chk(True, "strategy", red.get("strategy"), critical=False)
    chk(True, "venue", "{} ({})".format(red.get("venue"), red.get("aster_base_url")), critical=False)
    chk(bool(config.onchain.wallet_address), "wallet address", config.onchain.wallet_address or "(unset)")
    chk(bool(config.onchain.private_key), "private key", "set (local only)" if config.onchain.private_key else "MISSING")

    venue = build_venue(config)
    equity = 0.0
    try:
        acct = venue.read_account()
        equity = round(acct.equity_usd, 4)
        chk(acct.equity_usd > 0, "collateral / equity",
            "equity=${} available=${} positions={}".format(equity, round(acct.quote_balance_usd, 4), len(acct.positions)))
        if 0 < acct.equity_usd < 20:
            chk(False, "funding", "equity ${} is tiny — fund the wallet to open real trades".format(equity), critical=False)
    except Exception as exc:  # noqa: BLE001
        chk(False, "account read", "FAILED: {}".format(type(exc).__name__))

    if venue.name == "aster":
        try:
            health = venue.health()
            listed = health.get("listed_symbols") or []
            chk(len(listed) > 0, "aster listings", "{} TRADING symbols".format(len(listed)))
            sample = "BTCUSDT" if "BTCUSDT" in listed else (listed[0] if listed else "BTCUSDT")
            mark = venue.client.get_mark_price(sample)
            chk(mark > 0, "aster mark price", "{}={}".format(sample, mark))
            chk(bool(health.get("self_custody")), "self-custody signing", "EIP-712 signer present" if config.aster.signer_private_key else "no signer key", critical=False)
        except Exception as exc:  # noqa: BLE001
            chk(False, "aster venue", "FAILED: {}".format(type(exc).__name__))

    try:
        if config.board.mode == "api":
            rows = ShibeiBoardClient(config.board).fetch_rows()
            chk(len(rows) > 0, "拾贝 board", "{} rows".format(len(rows)) if rows else "fetch returned 0 (will fall back)", critical=False)
        else:
            chk(None, "拾贝 board", "mode=off (fallback source)", critical=False)
    except Exception as exc:  # noqa: BLE001
        chk(False, "拾贝 board", "FAILED: {}".format(type(exc).__name__), critical=False)

    try:
        sig = CmcAgentHub(config.cmc).fetch_market_signal()
        chk(True, "CMC signal", "source={} btc=${}".format(sig.source, sig.btc_price), critical=False)
    except Exception as exc:  # noqa: BLE001
        chk(False, "CMC signal", "FAILED: {}".format(type(exc).__name__), critical=False)

    dd = r["max_account_drawdown_pct"]; dl = r["max_daily_loss_pct"]; mh = r["long_max_hold_hours"]
    chk(dd > 0, "drawdown kill-switch", "halt at -{:.0%}".format(dd) if dd > 0 else "OFF")
    chk(dl > 0, "daily-loss kill-switch", "halt at -{:.0%}".format(dl) if dl > 0 else "OFF")
    chk(mh > 0, "max-hold backstop", "{}h".format(mh) if mh > 0 else "OFF", critical=False)

    gate_ok = red["live_orders_allowed"]
    chk(None, "live gate", "OPEN — will send real orders" if gate_ok else "CLOSED — " + (", ".join(red["blocked_reasons"]) or "none"), critical=False)

    crit_fail = [c for c in checks if c["critical"] and c["ok"] is False]
    infra_ready = not crit_fail
    verdict = "READY to arm live" if infra_ready else "NOT READY — fix the ✗ items below"

    if args.json:
        _print({"verdict": verdict, "infra_ready": infra_ready, "live_gate_open": gate_ok, "checks": checks}, True)
        return 0 if infra_ready else 1

    _hr("Shibei On-Chain Agent · preflight (doctor)")
    for c in checks:
        mark = "✓" if c["ok"] is True else ("✗" if c["ok"] is False else "•")
        star = "" if c["critical"] else " (non-critical)"
        print("  {} {:24} {}{}".format(mark, c["label"], c["detail"], star if c["ok"] is False else ""))
    _hr("Verdict")
    print("  " + verdict)
    if not gate_ok:
        print("  (live gate is CLOSED — this is expected until you deliberately arm it; see docs/GOLIVE.md)")
    return 0 if infra_ready else 1


def cmd_backtest(args: argparse.Namespace, config: AgentConfig) -> int:
    """Offline, deterministic backtest of the agent's risk + management engine.
    Judges run a built-in scenario or supply their own close-price panel."""
    from shibei_onchain.backtest import make_scenario, run_backtest, load_panel

    if getattr(args, "data", None):
        try:
            panel = load_panel(args.data)
        except Exception as exc:  # noqa: BLE001
            _print({"error": "bad_panel:{}".format(exc)}, getattr(args, "json", False))
            return 1
        label = panel.get("name") or args.data
    else:
        panel = make_scenario(args.scenario, bars=int(args.bars))
        label = "scenario:" + str(args.scenario)
    result = run_backtest(panel, config, top_n=int(args.top_n))
    m = result.metrics()

    if args.json:
        _print({"label": label, "metrics": m, "equity_curve": result.equity_curve}, True)
        return 0

    _hr("Backtest · {} ({})".format(label, config.strategy))
    print("  bars / interval   : {} bars".format(m["bars"]))
    print("  initial → final   : ${} → ${}".format(m["initial_equity"], m["final_equity"]))
    print("  total return      : {:+.2f}%".format(m["total_return_pct"]))
    print("  max drawdown      : {:.2f}%".format(m["max_drawdown_pct"]))
    print("  trades            : {}  (win {:.1f}%, PF {})".format(
        m["trades"], m["win_rate_pct"], m["profit_factor"]))
    print("  exit breakdown    : {}".format(m["exit_breakdown"]))
    print("  kill-switch fired : {} bar(s) {}".format(m["kill_switch_bars"], m["halt_reasons"] or ""))
    # tiny text sparkline of the equity curve
    curve = result.equity_curve
    if curve:
        lo, hi = min(curve), max(curve)
        blocks = "▁▂▃▄▅▆▇█"
        spark = "".join(blocks[min(7, int((e - lo) / (hi - lo) * 7))] if hi > lo else "▄" for e in curve)
        print("  equity curve      : {}".format(spark))
    _hr("Note")
    print("  Engine runs the agent's REAL risk/management/kill-switch logic.")
    print("  Authoritative full-universe V0.2 backtest (real 拾贝 board + prices,")
    print("  2026 H1) = −22.5%. See docs/BACKTEST.md. Supply --data <panel.json>")
    print("  to backtest on your own real prices.")
    return 0


def cmd_loop(args: argparse.Namespace, config: AgentConfig) -> int:
    from shibei_onchain.exec_live import OnChainAgent

    agent = OnChainAgent(config)
    interval = max(5, int(args.interval))
    count = 0
    errors = 0
    try:
        while args.max_cycles == 0 or count < args.max_cycles:
            count += 1
            # A single bad cycle (transient RPC/exchange/parse error) must never
            # kill a multi-day unattended run — log it and carry on next interval.
            try:
                result = agent.run_cycle(persist=not args.no_persist)
                s = result.summary()
                halt = ""
                try:
                    hr = result.decision.risk_state.get("account_halt_reason")
                    halt = f" HALT={hr}" if hr else ""
                except Exception:  # pragma: no cover
                    pass
                print(
                    f"[cycle {count}] {s['as_of']} regime={s['regime']} "
                    f"equity={s['equity_usd']} opens={s['planned_opens']} exits={s['exits']} "
                    f"executed={s['executed']} live={s['live_orders']}{halt}"
                )
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                errors += 1
                print(f"[cycle {count}] ERROR ({type(exc).__name__}: {exc}); continuing", file=sys.stderr)
            if args.max_cycles and count >= args.max_cycles:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.")
    if errors:
        print(f"({errors} cycle error(s) over {count} cycles)")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    # --json is accepted both before and after the subcommand (shared parent).
    common = argparse.ArgumentParser(add_help=False)
    # default=SUPPRESS so a subparser parse can't clobber a --json set before the
    # subcommand; main() normalizes the attribute back to a plain bool.
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help="machine-readable JSON output",
    )

    parser = argparse.ArgumentParser(
        prog="shibei-onchain", description="Shibei On-Chain Agent", parents=[common]
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", parents=[common], help="show config, live-gate and health")
    sub.add_parser("doctor", parents=[common], help="preflight: read-only live-readiness check (no orders)")
    p_bt = sub.add_parser("backtest", parents=[common], help="offline deterministic backtest of the risk/management engine")
    p_bt.add_argument("--scenario", default="bull", choices=["bull", "chop", "crash", "deepv"],
                      help="built-in market regime (default bull)")
    p_bt.add_argument("--bars", default="60", help="number of bars for a built-in scenario (default 60)")
    p_bt.add_argument("--data", default="", help="path to a judge-supplied close-price panel JSON (overrides --scenario)")
    p_bt.add_argument("--top-n", default="3", help="max concurrent long candidates per bar (default 3)")
    sub.add_parser("signal", parents=[common], help="fetch a CMC Agent Hub market signal")
    sub.add_parser("scan", parents=[common], help="show the ranked candidate board")
    sub.add_parser("plan", parents=[common], help="scan -> filter -> risk -> planned orders (no execution)")

    p_run = sub.add_parser("run", parents=[common], help="one full read->decide->execute(dry-run) cycle")
    p_run.add_argument("--no-persist", action="store_true", help="do not write state json")

    p_swap = sub.add_parser("swap-hello", parents=[common], help="single hello-world swap via Trust Wallet Agent Kit")
    p_swap.add_argument("--token", default="BNB", help="base asset to buy/sell (default BNB)")
    p_swap.add_argument("--notional", default="20", help="USDT notional (default 20)")
    p_swap.add_argument("--price", default="", help="reference price (enables mock quote)")
    p_swap.add_argument("--close", action="store_true", help="sell token->USDT instead of buy")
    p_swap.add_argument("--force-dry-run", action="store_true", help="force dry-run even if live gate open")

    p_tw = sub.add_parser("twak-swap", parents=[common], help="genuine Trust Wallet Agent Kit swap on BNB Chain")
    p_tw.add_argument("--amount", default="10", help="amount of the from-token (default 10)")
    p_tw.add_argument("--from", dest="from_symbol", default="USDT", help="source token symbol (default USDT)")
    p_tw.add_argument("--to", dest="to_symbol", default="BNB", help="destination token symbol (default BNB)")
    p_tw.add_argument("--execute", action="store_true", help="actually execute (needs TWAK_WALLET_PASSWORD + funds); default is quote-only")
    p_tw.add_argument("--no-signal", action="store_true", help="skip the CMC signal print")

    p_loop = sub.add_parser("loop", parents=[common], help="run cycles on an interval")
    p_loop.add_argument("--interval", default="60", help="seconds between cycles (default 60)")
    p_loop.add_argument("--max-cycles", type=int, default=0, help="stop after N cycles (0 = forever)")
    p_loop.add_argument("--no-persist", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    if not args.command:
        parser.print_help()
        return 0
    config = load_config()
    dispatch = {
        "status": cmd_status,
        "doctor": cmd_doctor,
        "backtest": cmd_backtest,
        "signal": cmd_signal,
        "scan": cmd_scan,
        "plan": cmd_plan,
        "run": cmd_run,
        "swap-hello": cmd_swap_hello,
        "twak-swap": cmd_twak_swap,
        "loop": cmd_loop,
    }
    handler = dispatch[args.command]
    return handler(args, config)


if __name__ == "__main__":
    sys.exit(main())
