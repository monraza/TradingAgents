#!/usr/bin/env python
"""Data bridge for running TradingAgents as a Claude Code agent workflow.

The Python package normally drives the agents itself, calling an LLM API for
every step. In the Claude Code workflow (``.claude/skills/trading-analysis``
and ``.claude/agents/ta-*.md``) Claude Code does the reasoning instead, so no
LLM API key is needed. This script gives those agents the same data tools the
LangGraph agents use (prices, indicators, fundamentals, news, macro, prediction
markets, social sentiment) and the same persistent decision log, through a
plain command line. Every subcommand prints markdown or JSON to stdout.

Run ``python scripts/ta_data.py --help`` for the full list of subcommands.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

# Make the repo importable when the package has not been pip-installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tradingagents.dataflows.config import get_config  # noqa: E402
from tradingagents.dataflows.interface import route_to_vendor  # noqa: E402

DEFAULT_RUNS_DIR = _REPO_ROOT / "reports"

# Debate turn files are named NN_<speaker>.md so a plain sort gives speaking order.
_TURN_RE = re.compile(r"^(\d+)_([a-z]+)\.md$")
_SPEAKER_LABELS = {
    "bull": "Bull Analyst",
    "bear": "Bear Analyst",
    "aggressive": "Aggressive Analyst",
    "conservative": "Conservative Analyst",
    "neutral": "Neutral Analyst",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc
    return value


def _normalize(ticker: str) -> str:
    from tradingagents.dataflows.symbol_utils import normalize_symbol

    return normalize_symbol(ticker)


def _asset_type(ticker: str) -> str:
    from cli.utils import detect_asset_type

    return detect_asset_type(ticker).value


def _memory_log():
    from tradingagents.agents.utils.memory import TradingMemoryLog

    return TradingMemoryLog(get_config())


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _turns(debate_dir: Path) -> list[tuple[str, str]]:
    """Return ``[(speaker, text), ...]`` for a debate directory, in speaking order."""
    if not debate_dir.is_dir():
        return []
    turns = []
    for path in sorted(debate_dir.iterdir()):
        match = _TURN_RE.match(path.name)
        if match and match.group(2) in _SPEAKER_LABELS:
            turns.append((match.group(2), _read(path)))
    return turns


def _format_turns(turns: list[tuple[str, str]]) -> str:
    """Render turns the way the LangGraph debaters build their history strings."""
    return "\n".join(f"{_SPEAKER_LABELS[s]}: {text}" for s, text in turns)


def _emit(text: str) -> None:
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


# ---------------------------------------------------------------------------
# Run setup and report assembly
# ---------------------------------------------------------------------------


def cmd_init_run(args) -> None:
    """Create the run directory and write run.json + past_context.md."""
    from tradingagents.agents.utils.agent_utils import (
        build_instrument_context,
        resolve_instrument_identity,
    )
    from tradingagents.dataflows.utils import safe_ticker_component

    ticker = _normalize(args.ticker)
    asset_type = _asset_type(ticker)
    trade_date = args.date or date.today().isoformat()
    identity = resolve_instrument_identity(ticker)
    config = get_config()

    run_dir = Path(args.out).resolve() / safe_ticker_component(ticker) / trade_date
    already_existed = (run_dir / "run.json").exists()
    run_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("1_analysts", "2_research/debate", "3_trading", "4_risk/debate", "5_portfolio"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    analysts = ["market", "sentiment", "news", "fundamentals"]
    if asset_type == "crypto":
        analysts.remove("fundamentals")  # no company financials for crypto

    run = {
        "ticker": ticker,
        "trade_date": trade_date,
        "asset_type": asset_type,
        "identity": identity,
        "instrument_context": build_instrument_context(ticker, asset_type, identity),
        "output_language": config.get("output_language", "English"),
        "analysts": analysts,
        "run_dir": str(run_dir),
    }
    (run_dir / "run.json").write_text(json.dumps(run, indent=2), encoding="utf-8")

    past = _memory_log().get_past_context(ticker)
    (run_dir / "past_context.md").write_text(
        past or "No prior decisions are on record yet.", encoding="utf-8"
    )
    (run_dir / "reflections").mkdir(exist_ok=True)
    _emit(json.dumps({**run, "already_existed": already_existed}, indent=2))


def cmd_transcript(args) -> None:
    """Print a debate so far, labelled by speaker, in speaking order."""
    turns = _turns(Path(args.debate_dir))
    _emit(_format_turns(turns) if turns else "(No arguments yet: you are opening the debate.)")


def cmd_next_turn(args) -> None:
    """Print the file path the next debate turn should be written to."""
    debate_dir = Path(args.debate_dir)
    debate_dir.mkdir(parents=True, exist_ok=True)
    numbers = [
        int(m.group(1)) for p in debate_dir.iterdir() if (m := _TURN_RE.match(p.name))
    ]
    _emit(str(debate_dir / f"{max(numbers, default=0) + 1:02d}_{args.speaker}.md"))


def cmd_compile_report(args) -> None:
    """Assemble the run's files into the standard TradingAgents report tree."""
    from tradingagents.reporting import write_report_tree

    run_dir = Path(args.run_dir)
    run = json.loads(_read(run_dir / "run.json"))

    research = _turns(run_dir / "2_research" / "debate")
    risk = _turns(run_dir / "4_risk" / "debate")

    def history(turns, speaker):
        return _format_turns([t for t in turns if t[0] == speaker])

    final_state = {
        "market_report": _read(run_dir / "1_analysts" / "market.md"),
        "sentiment_report": _read(run_dir / "1_analysts" / "sentiment.md"),
        "news_report": _read(run_dir / "1_analysts" / "news.md"),
        "fundamentals_report": _read(run_dir / "1_analysts" / "fundamentals.md"),
        "investment_debate_state": {
            "bull_history": history(research, "bull"),
            "bear_history": history(research, "bear"),
            "judge_decision": _read(run_dir / "2_research" / "manager.md"),
        },
        "trader_investment_plan": _read(run_dir / "3_trading" / "trader.md"),
        "risk_debate_state": {
            "aggressive_history": history(risk, "aggressive"),
            "conservative_history": history(risk, "conservative"),
            "neutral_history": history(risk, "neutral"),
            "judge_decision": _read(run_dir / "5_portfolio" / "decision.md"),
        },
    }
    path = write_report_tree(final_state, run["ticker"], run_dir)
    _emit(str(path))


# ---------------------------------------------------------------------------
# Decision log (memory)
# ---------------------------------------------------------------------------


def cmd_memory_pending(args) -> None:
    """List this ticker's unresolved decisions whose realised returns are now known."""
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    ticker = _normalize(args.ticker)
    # _resolve_benchmark only reads self.config and _fetch_returns does not use
    # self, so both are called unbound (as the test-suite does) rather than
    # building a full graph, which would need an LLM client.
    benchmark = TradingAgentsGraph._resolve_benchmark(
        SimpleNamespace(config=get_config()), ticker
    )
    ready = []
    for entry in _memory_log().get_pending_entries():
        if entry["ticker"] != ticker:
            continue
        raw, alpha, days = TradingAgentsGraph._fetch_returns(
            None, ticker, entry["date"], benchmark=benchmark
        )
        if raw is None:
            continue  # too recent (or no price data yet); retried on a later run
        ready.append({
            "ticker": ticker,
            "trade_date": entry["date"],
            "rating": entry["rating"],
            "benchmark": benchmark,
            "raw_return": raw,
            "alpha_return": alpha,
            "holding_days": days,
            "raw_return_pct": f"{raw:+.1%}",
            "alpha_return_pct": f"{alpha:+.1%}",
            "decision": entry["decision"],
        })
    _emit(json.dumps(ready, indent=2))


def cmd_memory_resolve(args) -> None:
    """Record a pending decision's outcome and its reflection."""
    reflection = (
        Path(args.reflection_file).read_text(encoding="utf-8")
        if args.reflection_file
        else args.reflection
    )
    if not reflection or not reflection.strip():
        sys.exit("A reflection is required (--reflection or --reflection-file).")
    _memory_log().update_with_outcome(
        _normalize(args.ticker),
        args.date,
        args.raw_return,
        args.alpha_return,
        args.holding_days,
        reflection.strip(),
    )
    _emit(f"Resolved {args.ticker} {args.date}.")


def cmd_memory_store(args) -> None:
    """Append the Portfolio Manager's final decision to the decision log."""
    decision = Path(args.decision_file).read_text(encoding="utf-8").strip()
    _memory_log().store_decision(_normalize(args.ticker), args.date, decision)
    _emit(f"Stored decision for {args.ticker} {args.date} in {get_config()['memory_log_path']}")


def cmd_memory_context(args) -> None:
    """Print (or write to --out) past decisions and lessons for the Portfolio Manager."""
    past = _memory_log().get_past_context(_normalize(args.ticker))
    text = past or "No prior decisions are on record yet."
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        _emit(f"Wrote {args.out}")
    else:
        _emit(text)


# ---------------------------------------------------------------------------
# Data tools (same vendor routing as the LangGraph agents' tools)
# ---------------------------------------------------------------------------


def cmd_stock(args) -> None:
    _emit(route_to_vendor("get_stock_data", args.ticker, args.start, args.end))


def cmd_indicators(args) -> None:
    results = []
    for name in (i.strip().lower() for i in args.indicators.split(",") if i.strip()):
        try:
            results.append(
                route_to_vendor("get_indicators", args.ticker, name, args.date, args.look_back_days)
            )
        except ValueError as exc:
            results.append(str(exc))
    _emit("\n\n".join(results))


def cmd_snapshot(args) -> None:
    from tradingagents.dataflows.market_data_validator import build_verified_market_snapshot

    _emit(build_verified_market_snapshot(args.ticker, args.date, args.look_back_days))


def cmd_fundamentals(args) -> None:
    _emit(route_to_vendor("get_fundamentals", args.ticker, args.date))


def _statement(method):
    def run(args) -> None:
        _emit(route_to_vendor(method, args.ticker, args.freq, args.date))

    return run


def cmd_news(args) -> None:
    _emit(route_to_vendor("get_news", args.ticker, args.start, args.end))


def cmd_global_news(args) -> None:
    _emit(route_to_vendor("get_global_news", args.date, args.look_back_days, args.limit))


def cmd_insider(args) -> None:
    _emit(route_to_vendor("get_insider_transactions", args.ticker))


def cmd_macro(args) -> None:
    _emit(route_to_vendor("get_macro_indicators", args.indicator, args.date, args.look_back_days))


def cmd_prediction_markets(args) -> None:
    _emit(route_to_vendor("get_prediction_markets", args.topic, args.limit))


def cmd_social(args) -> None:
    """StockTwits + Reddit blocks, as pre-fetched by the Sentiment Analyst."""
    from tradingagents.dataflows.reddit import fetch_reddit_posts
    from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages

    _emit(
        "## StockTwits messages\n\n"
        f"{fetch_stocktwits_messages(args.ticker, limit=args.limit)}\n\n"
        "## Reddit posts (r/wallstreetbets, r/stocks, r/investing)\n\n"
        f"{fetch_reddit_posts(args.ticker)}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ta_data.py",
        description="TradingAgents data and memory tools for the Claude Code workflow.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    # Run setup / reports
    p = add("init-run", cmd_init_run, "Create a run directory; print run.json.")
    p.add_argument("ticker")
    p.add_argument("date", nargs="?", type=_valid_date, help="Trade date (default: today)")
    p.add_argument("--out", default=str(DEFAULT_RUNS_DIR), help="Base directory for runs")

    p = add("transcript", cmd_transcript, "Print a debate so far, in speaking order.")
    p.add_argument("debate_dir")

    p = add("next-turn", cmd_next_turn, "Print the file the next debate turn goes in.")
    p.add_argument("debate_dir")
    p.add_argument("speaker", choices=sorted(_SPEAKER_LABELS))

    p = add("compile-report", cmd_compile_report, "Write complete_report.md for a run.")
    p.add_argument("run_dir")

    # Memory
    p = add("memory-pending", cmd_memory_pending, "Pending decisions now ready to reflect on (JSON).")
    p.add_argument("ticker")

    p = add("memory-resolve", cmd_memory_resolve, "Record a decision's outcome and reflection.")
    p.add_argument("ticker")
    p.add_argument("date", type=_valid_date)
    p.add_argument("--raw-return", type=float, required=True)
    p.add_argument("--alpha-return", type=float, required=True)
    p.add_argument("--holding-days", type=int, required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--reflection")
    group.add_argument("--reflection-file")

    p = add("memory-store", cmd_memory_store, "Log the final decision for a ticker and date.")
    p.add_argument("ticker")
    p.add_argument("date", type=_valid_date)
    p.add_argument("decision_file")

    p = add("memory-context", cmd_memory_context, "Print past decisions and lessons.")
    p.add_argument("ticker")
    p.add_argument("--out", help="Write to this file instead of stdout")

    # Market data
    p = add("stock", cmd_stock, "OHLCV price history.")
    p.add_argument("ticker")
    p.add_argument("start", type=_valid_date)
    p.add_argument("end", type=_valid_date)

    p = add("indicators", cmd_indicators, "Technical indicators, comma-separated (e.g. rsi,macd).")
    p.add_argument("ticker")
    p.add_argument("indicators")
    p.add_argument("date", type=_valid_date)
    p.add_argument("--look-back-days", type=int, default=30)

    p = add("snapshot", cmd_snapshot, "Verified OHLCV + indicator snapshot (source of truth).")
    p.add_argument("ticker")
    p.add_argument("date", type=_valid_date)
    p.add_argument("--look-back-days", type=int, default=30)

    # Fundamentals
    p = add("fundamentals", cmd_fundamentals, "Company profile and key metrics.")
    p.add_argument("ticker")
    p.add_argument("date", type=_valid_date)

    for name, method, help_text in (
        ("balance-sheet", "get_balance_sheet", "Balance sheet."),
        ("cashflow", "get_cashflow", "Cash flow statement."),
        ("income-statement", "get_income_statement", "Income statement."),
    ):
        p = add(name, _statement(method), help_text)
        p.add_argument("ticker")
        p.add_argument("date", type=_valid_date)
        p.add_argument("--freq", choices=["quarterly", "annual"], default="quarterly")

    # News / macro / sentiment
    p = add("news", cmd_news, "Ticker-specific news.")
    p.add_argument("ticker")
    p.add_argument("start", type=_valid_date)
    p.add_argument("end", type=_valid_date)

    p = add("global-news", cmd_global_news, "Global and macro headlines.")
    p.add_argument("date", type=_valid_date)
    p.add_argument("--look-back-days", type=int, default=None)
    p.add_argument("--limit", type=int, default=None)

    p = add("insider", cmd_insider, "Insider transactions.")
    p.add_argument("ticker")

    p = add("macro", cmd_macro, "FRED macro series (cpi, core_pce, unemployment, fed_funds_rate, ...).")
    p.add_argument("indicator")
    p.add_argument("date", type=_valid_date)
    p.add_argument("--look-back-days", type=int, default=None)

    p = add("prediction-markets", cmd_prediction_markets, "Polymarket implied probabilities.")
    p.add_argument("topic")
    p.add_argument("--limit", type=int, default=None)

    p = add("social", cmd_social, "StockTwits and Reddit posts for a ticker.")
    p.add_argument("ticker")
    p.add_argument("--limit", type=int, default=30)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except Exception as exc:  # noqa: BLE001 — agents need a readable message, not a traceback
        print(f"ERROR ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
