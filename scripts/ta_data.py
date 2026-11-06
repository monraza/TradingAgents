#!/usr/bin/env python
"""Data bridge for running TradingAgents as a Claude Code agent workflow.

The Python package normally drives the agents itself, calling an LLM API for
every step. In the Claude Code workflow (``.claude/skills/trading-analysis``
and ``.claude/agents/ta-*.md``) Claude Code does the reasoning instead, so no
LLM API key is needed. This script gives those agents what the LangGraph run
gives its agents:

- the data tools of ``tradingagents.agents.tools``, called with the run's own
  ticker and trade date, so every request is clamped to the trade date exactly
  as in a graph run and no tool serves data from after it;
- the Sentiment Analyst's pre-fetched news, StockTwits and Reddit blocks;
- the memory log: settling past decisions, the lessons known by the trade date,
  and recording the final decision;
- the standard report tree (markdown and HTML) built by ``write_report_tree``.

Run ``python scripts/ta_data.py --help`` for the subcommands. Data commands take
the run directory first; ``init-run`` creates it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Make the repo importable when the package has not been pip-installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tradingagents.agents import tools  # noqa: E402
from tradingagents.dataflows.config import get_config  # noqa: E402
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError  # noqa: E402
from tradingagents.dataflows.router import no_data_available, vendor_unavailable  # noqa: E402

DEFAULT_RUNS_DIR = _REPO_ROOT / "reports"
_AGENTS_DIR = _REPO_ROOT / ".claude" / "agents"
ANALYSTS = ("market", "sentiment", "news", "fundamentals")

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
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


def _normalize(ticker: str) -> str:
    from tradingagents.dataflows.symbols import normalize_symbol

    return normalize_symbol(ticker)


def _memory_log():
    from tradingagents.memory import TradingMemoryLog

    return TradingMemoryLog(get_config())


def _memory_as_of(trade_date: str) -> str | None:
    """Lessons cutoff, as the graph applies it: only a historical run is filtered (#1251)."""
    from tradingagents.dataflows.date_window import is_historical

    return trade_date if is_historical(trade_date) else None


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _load_run(run_dir: str) -> dict:
    path = Path(run_dir) / "run.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: create the run with init-run first")
    return json.loads(path.read_text(encoding="utf-8"))


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


def _agent_model(name: str) -> str:
    """The ``model:`` an agent file sets, for the report header."""
    text = _read(_AGENTS_DIR / f"{name}.md")
    match = re.search(r"^model:\s*(\S+)", text, re.MULTILINE)
    model = match.group(1) if match else "inherit"
    return "session model" if model == "inherit" else model


def _emit(text: str) -> None:
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


# ---------------------------------------------------------------------------
# Run setup and report assembly
# ---------------------------------------------------------------------------


def cmd_init_run(args) -> None:
    """Create the run directory: run.json, past_context.md, portfolio_context.md."""
    from cli.prompts import detect_asset_type
    from tradingagents.agents.context import (
        build_instrument_context,
        get_portfolio_context_from_state,
        resolve_instrument_identity,
    )
    from tradingagents.dataflows.symbols import safe_ticker_component
    from tradingagents.graph.trading_graph import _validate_trade_date

    ticker = _normalize(args.ticker)
    asset_type = detect_asset_type(ticker).value
    trade_date = _validate_trade_date(args.date or date.today().isoformat())

    analysts = [a.strip().lower() for a in (args.analysts or ",".join(ANALYSTS)).split(",") if a.strip()]
    unknown = sorted(set(analysts) - set(ANALYSTS))
    if unknown:
        raise ValueError(f"unknown analyst(s) {', '.join(unknown)}; choose from {', '.join(ANALYSTS)}")
    if asset_type == "crypto" and "fundamentals" in analysts:
        analysts.remove("fundamentals")  # no company financials for crypto
    analysts = [a for a in ANALYSTS if a in analysts]  # canonical order, no duplicates
    if not analysts:
        raise ValueError("at least one analyst is required")

    if args.portfolio:
        from tradingagents.portfolio import load_portfolio

        portfolio_context = load_portfolio(args.portfolio).render(ticker)
    else:
        portfolio_context = get_portfolio_context_from_state({})

    identity = resolve_instrument_identity(ticker)
    config = get_config()
    run_dir = Path(args.out).resolve() / safe_ticker_component(ticker) / trade_date
    already_existed = (run_dir / "run.json").exists()
    for sub in ("1_analysts", "2_research/debate", "3_trading", "4_risk/debate",
                "5_portfolio", "reflections"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    run = {
        "ticker": ticker,
        "trade_date": trade_date,
        "asset_type": asset_type,
        "identity": identity,
        "instrument_context": build_instrument_context(ticker, asset_type, identity, trade_date),
        "output_language": config.get("output_language", "English"),
        "analysts": analysts,
        "portfolio_provided": bool(args.portfolio),
        "run_dir": str(run_dir),
    }
    (run_dir / "run.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
    (run_dir / "portfolio_context.md").write_text(portfolio_context, encoding="utf-8")
    _write_past_context(ticker, trade_date, run_dir / "past_context.md")
    _emit(json.dumps({**run, "already_existed": already_existed}, indent=2))


def _write_past_context(ticker: str, trade_date: str, path: Path) -> None:
    past = _memory_log().get_past_context(ticker, as_of=_memory_as_of(trade_date))
    path.write_text(past or "No prior decisions are on record yet.", encoding="utf-8")


def cmd_transcript(args) -> None:
    """Print a debate so far, labelled by speaker, in speaking order."""
    turns = _turns(Path(args.debate_dir))
    _emit(_format_turns(turns) if turns else "(No arguments yet: open the debate with your own case.)")


def cmd_next_turn(args) -> None:
    """Print the file path the next debate turn should be written to."""
    debate_dir = Path(args.debate_dir)
    debate_dir.mkdir(parents=True, exist_ok=True)
    numbers = [int(m.group(1)) for p in debate_dir.iterdir() if (m := _TURN_RE.match(p.name))]
    _emit(str(debate_dir / f"{max(numbers, default=0) + 1:02d}_{args.speaker}.md"))


def _final_state(run_dir: Path, run: dict) -> tuple[dict, int, int]:
    """The run's files as the graph's final state, plus the research and risk turn counts."""
    from tradingagents.agents.rating import parse_rating

    research = _turns(run_dir / "2_research" / "debate")
    risk = _turns(run_dir / "4_risk" / "debate")

    def history(turns, speaker):
        return _format_turns([t for t in turns if t[0] == speaker])

    decision = _read(run_dir / "5_portfolio" / "decision.md")
    state = {
        "company_of_interest": run["ticker"],
        "trade_date": run["trade_date"],
        "market_report": _read(run_dir / "1_analysts" / "market.md"),
        "sentiment_report": _read(run_dir / "1_analysts" / "sentiment.md"),
        "news_report": _read(run_dir / "1_analysts" / "news.md"),
        "fundamentals_report": _read(run_dir / "1_analysts" / "fundamentals.md"),
        "investment_debate_state": {
            "bull_history": history(research, "bull"),
            "bear_history": history(research, "bear"),
        },
        "investment_plan": _read(run_dir / "2_research" / "manager.md"),
        "trader_investment_plan": _read(run_dir / "3_trading" / "trader.md"),
        "risk_debate_state": {
            "aggressive_history": history(risk, "aggressive"),
            "conservative_history": history(risk, "conservative"),
            "neutral_history": history(risk, "neutral"),
        },
        "final_trade_decision": decision,
    }
    if decision:
        state["final_rating"] = parse_rating(decision)
    note = _read(run_dir / "memory_note.md")
    if note:
        state["memory_note"] = note
    return state, len(research), len(risk)


def cmd_compile_report(args) -> None:
    """Assemble the run's files into the standard report tree (markdown and HTML)."""
    import tradingagents
    from tradingagents.reporting import write_report_tree

    run_dir = Path(args.run_dir)
    run = _load_run(args.run_dir)
    final_state, research_turns, risk_turns = _final_state(run_dir, run)
    config = get_config()
    settings = {
        "version": tradingagents.__version__,
        "llm_provider": "Claude Code",
        "deep_think_llm": _agent_model("ta-portfolio-manager"),
        "quick_think_llm": _agent_model("ta-market-analyst"),
        "analysts": ["social" if a == "sentiment" else a for a in run.get("analysts", [])],
        "max_debate_rounds": research_turns // 2,
        "max_risk_discuss_rounds": risk_turns // 3,
        "data_vendors": dict(config.get("data_vendors") or {}),
        "tool_vendors": dict(config.get("tool_vendors") or {}),
    }
    path = write_report_tree(final_state, run["ticker"], run_dir, settings=settings)
    _emit(str(path))


# ---------------------------------------------------------------------------
# Memory log
# ---------------------------------------------------------------------------


def cmd_memory_pending(args) -> None:
    """Pending decisions whose holding window has traded, with their outcome (JSON).

    Like the graph's Memory Log step, this covers every ticker's pending
    decisions unless one ticker is given (#1445).
    """
    from tradingagents.memory.settlement import fetch_returns, resolve_benchmark

    config = get_config()
    holding_days = config.get("holding_period_days", 5)
    only = _normalize(args.ticker) if args.ticker else None
    ready, failed = [], []
    for entry in _memory_log().get_pending_entries():
        ticker = entry["ticker"]
        if only and ticker != only:
            continue
        benchmark = resolve_benchmark(ticker, config)
        try:
            raw, alpha, days, resolved = fetch_returns(
                ticker, entry["date"], holding_days, benchmark=benchmark
            )
        except Exception as exc:  # noqa: BLE001 — reported, the entry stays pending
            failed.append({"ticker": ticker, "trade_date": entry["date"],
                           "reason": f"prices unavailable: {exc}"})
            continue
        if raw is None:
            continue  # the holding window has not traded yet: retried on a later run
        ready.append({
            "ticker": ticker,
            "trade_date": entry["date"],
            "rating": entry["rating"],
            "benchmark": benchmark,
            "raw_return": raw,
            "alpha_return": alpha,
            "holding_days": days,
            "resolution_date": resolved,
            "raw_return_pct": f"{raw:+.1%}",
            "alpha_return_pct": f"{alpha:+.1%}",
            "decision": entry["decision"],
        })
    _emit(json.dumps({"ready": ready, "failed": failed}, indent=2))


def cmd_memory_resolve(args) -> None:
    """Record a pending decision's outcome and its reflection."""
    reflection = (
        Path(args.reflection_file).read_text(encoding="utf-8")
        if args.reflection_file
        else args.reflection
    )
    if not reflection or not reflection.strip():
        raise ValueError("a reflection is required (--reflection or --reflection-file)")
    updated = _memory_log().update_with_outcome(
        _normalize(args.ticker), args.date, args.raw_return, args.alpha_return,
        args.holding_days, reflection.strip(), resolution_date=args.resolution_date,
    )
    if updated:
        _emit(f"Settled {args.ticker} {args.date}.")
    else:
        _emit(f"No pending decision for {args.ticker} {args.date}: it was already settled or never logged.")


def cmd_memory_store(args) -> None:
    """Append the Portfolio Manager's final decision to the memory log."""
    from tradingagents.agents.rating import is_review, parse_rating

    decision = Path(args.decision_file).read_text(encoding="utf-8").strip()
    if not decision:
        raise ValueError(f"{args.decision_file} is empty")
    rating = parse_rating(decision)
    _memory_log().store_decision(_normalize(args.ticker), args.date, decision, rating=rating)
    _emit(f"Logged {args.ticker} {args.date} as {rating} in {get_config()['memory_log_path']}")
    if is_review(rating):
        _emit("WARNING: no rating could be read from the decision; it is logged as REVIEW.")


def cmd_memory_context(args) -> None:
    """Write the lessons known by the trade date for the Portfolio Manager."""
    run = _load_run(args.run_dir)
    path = Path(args.run_dir) / "past_context.md"
    _write_past_context(run["ticker"], run["trade_date"], path)
    _emit(f"Wrote {path}")


# ---------------------------------------------------------------------------
# Data tools: the graph's own tools, called with the run's ticker and trade date
# ---------------------------------------------------------------------------


def _data(func):
    """Wrap a data command: load the run and turn vendor failures into the graph's notices."""

    def command(args) -> None:
        meta = _load_run(args.run_dir)
        try:
            _emit(func(args, meta["ticker"], meta["trade_date"]))
        except VendorUnavailableError as exc:
            _emit(vendor_unavailable(args.command, exc))
        except NoMarketDataError as exc:
            _emit(no_data_available(exc))

    return command


@_data
def cmd_stock(args, ticker, trade_date):
    return tools.get_stock_data.func(ticker, args.start, args.end, trade_date)


@_data
def cmd_indicators(args, ticker, trade_date):
    return tools.get_indicators.func(ticker, args.indicators, args.date or trade_date,
                                     args.look_back_days, trade_date)


@_data
def cmd_snapshot(args, ticker, trade_date):
    return tools.get_verified_market_snapshot.func(ticker, args.date or trade_date,
                                                   args.look_back_days, trade_date)


@_data
def cmd_fundamentals(args, ticker, trade_date):
    return tools.get_fundamentals.func(ticker, args.date or trade_date, trade_date)


def _statement(tool):
    @_data
    def statement(args, ticker, trade_date):
        return tool.func(ticker, args.freq, args.date or trade_date, trade_date)

    return statement


@_data
def cmd_insider(args, ticker, trade_date):
    return tools.get_insider_transactions.func(ticker, trade_date)


@_data
def cmd_news(args, ticker, trade_date):
    return tools.get_news.func(ticker, args.start, args.end, trade_date)


@_data
def cmd_global_news(args, ticker, trade_date):
    return tools.get_global_news.func(args.date or trade_date, args.look_back_days, args.limit, trade_date)


@_data
def cmd_macro(args, ticker, trade_date):
    return tools.get_macro_indicators.func(args.indicator, args.date or trade_date,
                                           args.look_back_days, trade_date)


@_data
def cmd_prediction_markets(args, ticker, trade_date):
    return tools.get_prediction_markets.func(args.topic, args.limit, trade_date)


@_data
def cmd_sentiment_data(args, ticker, trade_date):
    """The three blocks the Sentiment Analyst pre-fetches, over the 7 days to the trade date."""
    from tradingagents.agents.post_screen import jev_screen
    from tradingagents.dataflows.vendors.reddit import fetch_reddit_posts, subreddits_for
    from tradingagents.dataflows.vendors.stocktwits import fetch_stocktwits_messages

    start = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    screen = jev_screen(ticker)
    subreddits = subreddits_for(ticker)
    news = tools.get_news.func(ticker, start, trade_date, trade_date)
    stocktwits = fetch_stocktwits_messages(
        ticker, limit=30, start_date=start, end_date=trade_date, screen=screen
    )
    reddit = fetch_reddit_posts(ticker, subreddits, start_date=start, end_date=trade_date, screen=screen)
    return (
        f"# Sentiment sources for {ticker}, {start} to {trade_date}\n\n"
        f"## News headlines (Yahoo Finance)\n\n{news}\n\n"
        f"## StockTwits messages\n\n{stocktwits}\n\n"
        f"## Reddit posts ({', '.join(f'r/{s}' for s in subreddits)})\n\n{reddit}"
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

    def add(name, func, help_text, run_dir=False):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        if run_dir:
            p.add_argument("run_dir", help="The run directory init-run created")
        return p

    def date_opt(p):
        p.add_argument("--date", type=_valid_date,
                       help="As-of date (default and maximum: the run's trade date)")

    # Run setup / reports
    p = add("init-run", cmd_init_run, "Create a run directory; print run.json.")
    p.add_argument("ticker")
    p.add_argument("date", nargs="?", type=_valid_date, help="Trade date (default: today)")
    p.add_argument("--analysts", help=f"Comma-separated subset of {','.join(ANALYSTS)}")
    p.add_argument("--portfolio", help="JSON file with holdings and cash")
    p.add_argument("--out", default=str(DEFAULT_RUNS_DIR), help="Base directory for runs")

    p = add("transcript", cmd_transcript, "Print a debate so far, in speaking order.")
    p.add_argument("debate_dir")

    p = add("next-turn", cmd_next_turn, "Print the file the next debate turn goes in.")
    p.add_argument("debate_dir")
    p.add_argument("speaker", choices=sorted(_SPEAKER_LABELS))

    add("compile-report", cmd_compile_report,
        "Write complete_report.md and complete_report.html for a run.", run_dir=True)

    # Memory
    p = add("memory-pending", cmd_memory_pending,
            "Pending decisions whose outcome is now known (JSON); all tickers by default.")
    p.add_argument("ticker", nargs="?")

    p = add("memory-resolve", cmd_memory_resolve, "Record a decision's outcome and reflection.")
    p.add_argument("ticker")
    p.add_argument("date", type=_valid_date)
    p.add_argument("--raw-return", type=float, required=True)
    p.add_argument("--alpha-return", type=float, required=True)
    p.add_argument("--holding-days", type=int, required=True)
    p.add_argument("--resolution-date", type=_valid_date)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--reflection")
    group.add_argument("--reflection-file")

    p = add("memory-store", cmd_memory_store, "Log the final decision for a ticker and date.")
    p.add_argument("ticker")
    p.add_argument("date", type=_valid_date)
    p.add_argument("decision_file")

    add("memory-context", cmd_memory_context,
        "Rewrite the run's past_context.md with the lessons known by its trade date.", run_dir=True)

    # Market data
    p = add("stock", cmd_stock, "OHLCV price history.", run_dir=True)
    p.add_argument("start", type=_valid_date)
    p.add_argument("end", type=_valid_date)

    p = add("indicators", cmd_indicators,
            "Technical indicators, comma-separated (e.g. rsi,macd).", run_dir=True)
    p.add_argument("indicators")
    date_opt(p)
    p.add_argument("--look-back-days", type=int, default=30)

    p = add("snapshot", cmd_snapshot,
            "Verified OHLCV and indicator snapshot (the source of truth).", run_dir=True)
    date_opt(p)
    p.add_argument("--look-back-days", type=int, default=30)

    # Fundamentals
    p = add("fundamentals", cmd_fundamentals, "Company profile and key metrics.", run_dir=True)
    date_opt(p)

    for name, tool, help_text in (
        ("balance-sheet", tools.get_balance_sheet, "Balance sheet."),
        ("cashflow", tools.get_cashflow, "Cash flow statement."),
        ("income-statement", tools.get_income_statement, "Income statement."),
    ):
        p = add(name, _statement(tool), help_text, run_dir=True)
        p.add_argument("--freq", choices=["quarterly", "annual"], default="quarterly")
        date_opt(p)

    add("insider", cmd_insider, "Insider transactions.", run_dir=True)

    # News / macro / sentiment
    p = add("news", cmd_news, "News about the instrument.", run_dir=True)
    p.add_argument("start", type=_valid_date)
    p.add_argument("end", type=_valid_date)

    p = add("global-news", cmd_global_news, "Global and macro headlines.", run_dir=True)
    date_opt(p)
    p.add_argument("--look-back-days", type=int, default=None)
    p.add_argument("--limit", type=int, default=None)

    p = add("macro", cmd_macro,
            "FRED macro series (cpi, core_pce, unemployment, fed_funds_rate, ecb_deposit_rate, ...).",
            run_dir=True)
    p.add_argument("indicator")
    date_opt(p)
    p.add_argument("--look-back-days", type=int, default=None)

    p = add("prediction-markets", cmd_prediction_markets, "Polymarket implied probabilities.",
            run_dir=True)
    p.add_argument("topic")
    p.add_argument("--limit", type=int, default=None)

    add("sentiment-data", cmd_sentiment_data,
        "News, StockTwits and Reddit blocks for the Sentiment Analyst.", run_dir=True)

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
