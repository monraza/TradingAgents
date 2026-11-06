"""Tests for scripts/ta_data.py, the data bridge behind the Claude Code workflow.

Network access is mocked throughout. These tests cover the run directory, the
point-in-time clamping the script inherits from the graph's tools, debate turn
ordering, report assembly and the memory log round trip.
"""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import tradingagents.dataflows.config as config_module
from tradingagents.dataflows.errors import VendorUnavailableError

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ta_data.py"
_spec = importlib.util.spec_from_file_location("ta_data", _SCRIPT)
ta_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ta_data)

pytestmark = pytest.mark.unit

TRADE_DATE = "2026-01-15"
DECISION = (
    "**Rating**: Overweight\n\n**Executive Summary**: Add gradually.\n\n"
    "**Investment Thesis**: Strong demand."
)
IDENTITY = "tradingagents.agents.context.resolve_instrument_identity"
FETCH_RETURNS = "tradingagents.memory.settlement.fetch_returns"
ROUTE = "tradingagents.agents.tools.route_to_vendor"


@pytest.fixture()
def memory_path(tmp_path):
    path = tmp_path / "memory" / "trading_memory.md"
    config_module.set_config({"memory_log_path": str(path)})
    return path


def _init(tmp_path, capsys, *extra, ticker="nvda", identity=None):
    with patch(IDENTITY, return_value=identity or {}):
        code = ta_data.main(["init-run", ticker, TRADE_DATE, "--out", str(tmp_path / "runs"), *extra])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out)


@pytest.fixture()
def run(tmp_path, memory_path, capsys):
    out = _init(tmp_path, capsys, identity={"company_name": "NVIDIA Corporation"})
    return Path(out["run_dir"]), out


# ---------------------------------------------------------------------------
# init-run
# ---------------------------------------------------------------------------


def test_init_run_creates_layout_and_run_json(run):
    path, out = run
    assert out["ticker"] == "NVDA"
    assert out["trade_date"] == TRADE_DATE
    assert out["asset_type"] == "stock"
    assert out["analysts"] == ["market", "sentiment", "news", "fundamentals"]
    assert out["already_existed"] is False
    assert out["portfolio_provided"] is False
    assert "NVIDIA Corporation" in out["instrument_context"]
    for sub in ("1_analysts", "2_research/debate", "3_trading", "4_risk/debate",
                "5_portfolio", "reflections"):
        assert (path / sub).is_dir()
    assert "already_existed" not in json.loads((path / "run.json").read_text())
    assert (path / "past_context.md").read_text().startswith("No prior decisions")
    assert "not provided" in (path / "portfolio_context.md").read_text()


def test_init_run_reports_an_existing_run(tmp_path, memory_path, capsys):
    _init(tmp_path, capsys)
    assert _init(tmp_path, capsys)["already_existed"] is True


def test_init_run_selects_analysts_in_canonical_order(tmp_path, memory_path, capsys):
    out = _init(tmp_path, capsys, "--analysts", "news, market,news")
    assert out["analysts"] == ["market", "news"]


def test_init_run_drops_fundamentals_for_crypto(tmp_path, memory_path, capsys):
    out = _init(tmp_path, capsys, ticker="BTC-USD")
    assert out["asset_type"] == "crypto"
    assert "fundamentals" not in out["analysts"]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["init-run", "NVDA", "2099-01-01"], "cannot be in the future"),
        (["init-run", "NVDA", TRADE_DATE, "--analysts", "market,astrology"], "unknown analyst"),
    ],
)
def test_init_run_rejects_bad_input(tmp_path, memory_path, capsys, argv, message):
    with patch(IDENTITY, return_value={}):
        assert ta_data.main([*argv, "--out", str(tmp_path)]) == 1
    assert message in capsys.readouterr().err


def test_init_run_rejects_a_malformed_date(tmp_path):
    with pytest.raises(SystemExit):
        ta_data.main(["init-run", "NVDA", "15-01-2026", "--out", str(tmp_path)])


def test_init_run_renders_the_portfolio(tmp_path, memory_path, capsys):
    book = tmp_path / "book.json"
    book.write_text(json.dumps({
        "cash": 25000, "currency": "USD",
        "positions": [{"ticker": "NVDA", "quantity": 120, "average_price": 150.0}],
    }))
    out = _init(tmp_path, capsys, "--portfolio", str(book))
    assert out["portfolio_provided"] is True
    text = (Path(out["run_dir"]) / "portfolio_context.md").read_text()
    assert "Current position in NVDA: 120 units, average price 150.00" in text
    assert "Cash available: 25,000.00 USD" in text


def test_init_run_rejects_an_unusable_portfolio(tmp_path, memory_path, capsys):
    book = tmp_path / "book.json"
    book.write_text("{not json")
    with patch(IDENTITY, return_value={}):
        assert ta_data.main(["init-run", "NVDA", TRADE_DATE, "--portfolio", str(book),
                             "--out", str(tmp_path)]) == 1
    assert "not usable" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Debates and the report
# ---------------------------------------------------------------------------


def test_next_turn_numbers_in_speaking_order(run, capsys):
    path, _ = run
    debate = path / "2_research" / "debate"
    ta_data.main(["next-turn", str(debate), "bull"])
    first = Path(capsys.readouterr().out.strip())
    assert first.name == "01_bull.md"
    first.write_text("Demand is accelerating.")
    ta_data.main(["next-turn", str(debate), "bear"])
    assert Path(capsys.readouterr().out.strip()).name == "02_bear.md"


def test_transcript_labels_speakers_in_order(run, capsys):
    path, _ = run
    debate = path / "2_research" / "debate"
    ta_data.main(["transcript", str(debate)])
    assert "open the debate with your own case" in capsys.readouterr().out
    (debate / "01_bull.md").write_text("Demand is accelerating.")
    (debate / "02_bear.md").write_text("Valuation is stretched.")
    (debate / "notes.txt").write_text("ignored")
    ta_data.main(["transcript", str(debate)])
    out = capsys.readouterr().out
    assert out.index("Bull Analyst: Demand") < out.index("Bear Analyst: Valuation")
    assert "ignored" not in out


def _write_full_run(path: Path) -> None:
    (path / "1_analysts" / "market.md").write_text("RSI is 61.")
    (path / "2_research" / "debate" / "01_bull.md").write_text("Bull case.")
    (path / "2_research" / "debate" / "02_bear.md").write_text("Bear case.")
    (path / "2_research" / "manager.md").write_text("**Recommendation**: Buy")
    (path / "3_trading" / "trader.md").write_text("**Action**: Buy")
    (path / "4_risk" / "debate" / "01_aggressive.md").write_text("Go big.")
    (path / "4_risk" / "debate" / "02_conservative.md").write_text("Go small.")
    (path / "4_risk" / "debate" / "03_neutral.md").write_text("Go medium.")
    (path / "5_portfolio" / "decision.md").write_text(DECISION)


def test_compile_report_builds_the_standard_tree(run, capsys):
    path, _ = run
    _write_full_run(path)
    assert ta_data.main(["compile-report", str(path)]) == 0
    report = Path(capsys.readouterr().out.strip())
    text = report.read_text()
    assert report.name == "complete_report.md"
    assert (path / "complete_report.html").exists()
    for expected in ("RSI is 61.", "Bull Analyst: Bull case.", "Bear Analyst: Bear case.",
                     "### Research Manager\n**Recommendation**: Buy", "Go medium.",
                     "### Portfolio Manager\n**Rating**: Overweight",
                     f"- Analysis date: {TRADE_DATE}", "- Rating: Overweight",
                     "Claude Code", "research debate rounds 1, risk debate rounds 1"):
        assert expected in text, expected
    assert (path / "2_research" / "bull.md").read_text() == "Bull Analyst: Bull case."
    assert (path / "4_risk" / "conservative.md").read_text() == "Conservative Analyst: Go small."
    assert (path / "5_portfolio" / "decision.md").read_text() == DECISION


def test_compile_report_names_the_agents_models(run, capsys):
    path, _ = run
    ta_data.main(["compile-report", str(path)])
    text = Path(capsys.readouterr().out.strip()).read_text()
    assert "deep session model, quick sonnet" in text


# ---------------------------------------------------------------------------
# Memory log
# ---------------------------------------------------------------------------


def _store(path: Path, ticker="NVDA", day=TRADE_DATE, decision=DECISION):
    decision_file = path / "5_portfolio" / "decision.md"
    decision_file.write_text(decision)
    return ta_data.main(["memory-store", ticker, day, str(decision_file)])


def test_memory_round_trip(run, memory_path, capsys):
    path, _ = run
    assert _store(path, day="2026-01-02") == 0
    assert "[2026-01-02 | NVDA | Overweight | pending]" in memory_path.read_text()
    capsys.readouterr()

    with patch(FETCH_RETURNS, return_value=(0.05, 0.02, 5, "2026-01-09")) as fetch:
        ta_data.main(["memory-pending"])
    out = json.loads(capsys.readouterr().out)
    assert fetch.call_args.kwargs["benchmark"] == "SPY"
    assert fetch.call_args.args[2] == 5  # holding_period_days
    assert out["failed"] == []
    [entry] = out["ready"]
    assert entry["rating"] == "Overweight"
    assert entry["alpha_return_pct"] == "+2.0%"
    assert entry["resolution_date"] == "2026-01-09"

    reflection = path / "reflections" / "2026-01-02.md"
    reflection.write_text("Over 5 days the Overweight call beat SPY by 2.0%. Size up sooner.")
    assert ta_data.main([
        "memory-resolve", "NVDA", "2026-01-02", "--raw-return", "0.05", "--alpha-return", "0.02",
        "--holding-days", "5", "--resolution-date", "2026-01-09",
        "--reflection-file", str(reflection),
    ]) == 0
    assert "Settled NVDA 2026-01-02" in capsys.readouterr().out
    assert "| +5.0% | +2.0% | 5d" in memory_path.read_text()

    ta_data.main(["memory-context", str(path)])
    assert "Size up sooner." in (path / "past_context.md").read_text()


def test_memory_context_only_uses_lessons_known_by_the_trade_date(run, memory_path, capsys):
    path, _ = run
    for day, resolved, lesson in (("2026-01-02", "2026-01-09", "Known lesson."),
                                  ("2026-01-12", "2026-01-20", "Future lesson.")):
        _store(path, day=day)
        ta_data.main(["memory-resolve", "NVDA", day, "--raw-return", "0.01", "--alpha-return",
                      "0.01", "--holding-days", "5", "--resolution-date", resolved,
                      "--reflection", lesson])
    ta_data.main(["memory-context", str(path)])
    text = (path / "past_context.md").read_text()
    assert "Known lesson." in text
    assert "Future lesson." not in text


def test_memory_resolve_says_when_nothing_was_pending(run, memory_path, capsys):
    assert ta_data.main(["memory-resolve", "NVDA", "2026-01-02", "--raw-return", "0.01",
                         "--alpha-return", "0.01", "--holding-days", "5",
                         "--reflection", "x"]) == 0
    assert "No pending decision" in capsys.readouterr().out


def test_memory_pending_covers_every_ticker_and_reports_failures(run, memory_path, capsys):
    path, _ = run
    _store(path, ticker="NVDA", day="2026-01-02")
    _store(path, ticker="7203.T", day="2026-01-02")
    _store(path, ticker="AAPL", day="2026-01-14")
    capsys.readouterr()

    def fake(ticker, day, holding_days, benchmark):
        if ticker == "NVDA":
            raise ConnectionError("blocked")
        if ticker == "AAPL":
            return None, None, None, None  # window not traded yet
        return 0.01, -0.01, 5, "2026-01-09"

    with patch(FETCH_RETURNS, side_effect=fake):
        ta_data.main(["memory-pending"])
    out = json.loads(capsys.readouterr().out)
    assert [(e["ticker"], e["benchmark"]) for e in out["ready"]] == [("7203.T", "^N225")]
    assert out["failed"][0]["ticker"] == "NVDA"
    assert "blocked" in out["failed"][0]["reason"]

    with patch(FETCH_RETURNS, side_effect=fake):
        ta_data.main(["memory-pending", "7203.T"])
    assert len(json.loads(capsys.readouterr().out)["ready"]) == 1


def test_memory_store_flags_a_decision_without_a_rating(run, memory_path, capsys):
    path, _ = run
    assert _store(path, decision="We could not reach a view.") == 0
    out = capsys.readouterr().out
    assert "as REVIEW" in out and "WARNING" in out


# ---------------------------------------------------------------------------
# Data commands: the graph's tools, with the run's ticker and trade date
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected_call"),
    [
        (["stock", "2025-12-01", "2026-01-10"], ("get_stock_data", "NVDA", "2025-12-01", "2026-01-10")),
        # A window running past the trade date is cut off there.
        (["stock", "2025-12-01", "2026-03-01"], ("get_stock_data", "NVDA", "2025-12-01", TRADE_DATE)),
        (["news", "2026-01-08", "2026-02-01"], ("get_news", "NVDA", "2026-01-08", TRADE_DATE)),
        (["fundamentals"], ("get_fundamentals", "NVDA", TRADE_DATE)),
        (["fundamentals", "--date", "2026-06-01"], ("get_fundamentals", "NVDA", TRADE_DATE)),
        (["balance-sheet", "--freq", "annual"], ("get_balance_sheet", "NVDA", "annual", TRADE_DATE)),
        (["income-statement", "--date", "2025-12-31"],
         ("get_income_statement", "NVDA", "quarterly", "2025-12-31")),
        (["cashflow"], ("get_cashflow", "NVDA", "quarterly", TRADE_DATE)),
        (["insider"], ("get_insider_transactions", "NVDA", TRADE_DATE)),
        (["global-news", "--limit", "5"], ("get_global_news", TRADE_DATE, None, 5)),
        (["macro", "cpi"], ("get_macro_indicators", "cpi", TRADE_DATE, None)),
        (["prediction-markets", "Fed rate cut"],
         ("get_prediction_markets", "Fed rate cut", None, TRADE_DATE)),
    ],
)
def test_data_commands_use_the_runs_ticker_and_trade_date(run, capsys, argv, expected_call):
    path, _ = run
    with patch(ROUTE, return_value="DATA") as route:
        assert ta_data.main([argv[0], str(path), *argv[1:]]) == 0
    route.assert_called_once_with(*expected_call)
    assert capsys.readouterr().out == "DATA\n"


def test_indicators_split_clamp_and_keep_going_on_a_bad_name(run, capsys):
    path, _ = run

    def fake(method, ticker, name, curr_date, look_back):
        assert (ticker, curr_date) == ("NVDA", TRADE_DATE)
        if name == "bogus":
            raise ValueError("unknown indicator bogus")
        return f"{name} ok"

    with patch(ROUTE, side_effect=fake):
        ta_data.main(["indicators", str(path), "RSI, bogus ,macd", "--date", "2026-09-01"])
    out = capsys.readouterr().out
    assert "rsi ok" in out and "macd ok" in out and "unknown indicator bogus" in out


def test_snapshot_is_clamped_to_the_trade_date(run, capsys):
    path, _ = run
    with patch("tradingagents.agents.tools.build_verified_market_snapshot",
               return_value="SNAP") as snap:
        ta_data.main(["snapshot", str(path), "--date", "2026-09-01"])
    snap.assert_called_once_with("NVDA", TRADE_DATE, 30)
    assert capsys.readouterr().out == "SNAP\n"


def test_a_window_that_cannot_be_read_goes_back_to_the_agent(run, capsys):
    path, _ = run
    with patch(ROUTE) as route:
        assert ta_data.main(["stock", str(path), "2026-01-10", "2026-01-01"]) == 0
    route.assert_not_called()
    assert "after end_date" in capsys.readouterr().out


def test_vendor_outages_become_the_graphs_notice(run, capsys):
    path, _ = run
    with patch(ROUTE, side_effect=VendorUnavailableError("rate limited")):
        assert ta_data.main(["fundamentals", str(path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("DATA_UNAVAILABLE") and "do not estimate or fabricate" in out


def test_other_errors_become_readable_messages(run, capsys):
    path, _ = run
    with patch(ROUTE, side_effect=RuntimeError("boom")):
        assert ta_data.main(["fundamentals", str(path)]) == 1
    assert "ERROR (RuntimeError): boom" in capsys.readouterr().err


def test_data_commands_need_a_run(tmp_path, capsys):
    assert ta_data.main(["insider", str(tmp_path)]) == 1
    assert "init-run" in capsys.readouterr().err


def test_sentiment_data_fetches_the_week_to_the_trade_date(tmp_path, memory_path, capsys):
    out = _init(tmp_path, capsys, ticker="BTC-USD")
    with patch(ROUTE, return_value="HEADLINES") as route, \
         patch("tradingagents.agents.post_screen.jev_screen", return_value=None), \
         patch("tradingagents.dataflows.vendors.stocktwits.fetch_stocktwits_messages",
               return_value="TWITS") as twits, \
         patch("tradingagents.dataflows.vendors.reddit.fetch_reddit_posts",
               return_value="POSTS") as reddit:
        assert ta_data.main(["sentiment-data", out["run_dir"]]) == 0
    text = capsys.readouterr().out
    route.assert_called_once_with("get_news", "BTC-USD", "2026-01-08", TRADE_DATE)
    assert twits.call_args.kwargs["start_date"] == "2026-01-08"
    assert twits.call_args.kwargs["end_date"] == TRADE_DATE
    subreddits = reddit.call_args.args[1]
    assert "CryptoCurrency" in subreddits
    for block in ("HEADLINES", "TWITS", "POSTS", "r/CryptoCurrency"):
        assert block in text
