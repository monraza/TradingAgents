"""Tests for scripts/ta_data.py, the data bridge behind the Claude Code workflow.

Network access is mocked throughout: these tests cover argument handling, the
run-directory layout, debate turn ordering, report assembly and the decision
log round trip, not the vendors themselves (those have their own tests).
"""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import tradingagents.dataflows.config as config_module

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ta_data.py"
_spec = importlib.util.spec_from_file_location("ta_data", _SCRIPT)
ta_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ta_data)

pytestmark = pytest.mark.unit

DECISION = "**Rating**: Overweight\n\n**Executive Summary**: Add gradually.\n\n**Investment Thesis**: Strong demand."


@pytest.fixture()
def memory_path(tmp_path):
    path = tmp_path / "memory" / "trading_memory.md"
    config_module.set_config({"memory_log_path": str(path)})
    return path


@pytest.fixture()
def run_dir(tmp_path, memory_path, capsys):
    identity = {"company_name": "NVIDIA Corporation", "sector": "Technology"}
    with patch(
        "tradingagents.agents.utils.agent_utils.resolve_instrument_identity",
        return_value=identity,
    ):
        assert ta_data.main(["init-run", "nvda", "2026-01-15", "--out", str(tmp_path / "runs")]) == 0
    out = json.loads(capsys.readouterr().out)
    return Path(out["run_dir"]), out


def test_init_run_creates_layout_and_run_json(run_dir):
    path, out = run_dir
    assert out["ticker"] == "NVDA"
    assert out["trade_date"] == "2026-01-15"
    assert out["asset_type"] == "stock"
    assert out["analysts"] == ["market", "sentiment", "news", "fundamentals"]
    assert out["already_existed"] is False
    assert "NVIDIA Corporation" in out["instrument_context"]
    for sub in ("1_analysts", "2_research/debate", "3_trading", "4_risk/debate", "5_portfolio"):
        assert (path / sub).is_dir()
    saved = json.loads((path / "run.json").read_text())
    assert "already_existed" not in saved
    assert (path / "past_context.md").read_text().startswith("No prior decisions")


def test_init_run_drops_fundamentals_for_crypto(tmp_path, memory_path, capsys):
    with patch(
        "tradingagents.agents.utils.agent_utils.resolve_instrument_identity", return_value={}
    ):
        ta_data.main(["init-run", "BTC-USD", "2026-01-15", "--out", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert out["asset_type"] == "crypto"
    assert "fundamentals" not in out["analysts"]


def test_init_run_rejects_bad_date(tmp_path):
    with pytest.raises(SystemExit):
        ta_data.main(["init-run", "NVDA", "15-01-2026", "--out", str(tmp_path)])


def test_next_turn_numbers_in_speaking_order(run_dir, capsys):
    path, _ = run_dir
    debate = path / "2_research" / "debate"
    ta_data.main(["next-turn", str(debate), "bull"])
    first = Path(capsys.readouterr().out.strip())
    assert first.name == "01_bull.md"
    first.write_text("Demand is accelerating.")
    ta_data.main(["next-turn", str(debate), "bear"])
    second = Path(capsys.readouterr().out.strip())
    assert second.name == "02_bear.md"


def test_transcript_labels_speakers_in_order(run_dir, capsys):
    path, _ = run_dir
    debate = path / "2_research" / "debate"
    ta_data.main(["transcript", str(debate)])
    assert "opening the debate" in capsys.readouterr().out
    (debate / "01_bull.md").write_text("Demand is accelerating.")
    (debate / "02_bear.md").write_text("Valuation is stretched.")
    (debate / "notes.txt").write_text("ignored")
    ta_data.main(["transcript", str(debate)])
    out = capsys.readouterr().out
    assert out.index("Bull Analyst: Demand") < out.index("Bear Analyst: Valuation")
    assert "ignored" not in out


def test_compile_report_builds_standard_tree(run_dir, capsys):
    path, _ = run_dir
    (path / "1_analysts" / "market.md").write_text("RSI is 61.")
    (path / "2_research" / "debate" / "01_bull.md").write_text("Bull case.")
    (path / "2_research" / "debate" / "02_bear.md").write_text("Bear case.")
    (path / "2_research" / "manager.md").write_text("**Recommendation**: Buy")
    (path / "3_trading" / "trader.md").write_text("FINAL TRANSACTION PROPOSAL: **BUY**")
    (path / "4_risk" / "debate" / "01_aggressive.md").write_text("Go big.")
    (path / "4_risk" / "debate" / "02_conservative.md").write_text("Go small.")
    (path / "4_risk" / "debate" / "03_neutral.md").write_text("Go medium.")
    (path / "5_portfolio" / "decision.md").write_text(DECISION)

    assert ta_data.main(["compile-report", str(path)]) == 0
    report = Path(capsys.readouterr().out.strip())
    text = report.read_text()
    assert report.name == "complete_report.md"
    for expected in ("RSI is 61.", "Bull Analyst: Bull case.", "Bear Analyst: Bear case.",
                     "Go medium.", "**Rating**: Overweight"):
        assert expected in text
    assert (path / "2_research" / "bull.md").read_text() == "Bull Analyst: Bull case."
    assert (path / "4_risk" / "conservative.md").read_text() == "Conservative Analyst: Go small."


def test_memory_round_trip(run_dir, memory_path, capsys):
    path, _ = run_dir
    decision_file = path / "5_portfolio" / "decision.md"
    decision_file.write_text(DECISION)
    assert ta_data.main(["memory-store", "NVDA", "2026-01-15", str(decision_file)]) == 0
    assert "[2026-01-15 | NVDA | Overweight | pending]" in memory_path.read_text()
    capsys.readouterr()

    with patch(
        "tradingagents.graph.trading_graph.TradingAgentsGraph._fetch_returns",
        return_value=(0.05, 0.02, 5),
    ) as fetch:
        ta_data.main(["memory-pending", "NVDA"])
    pending = json.loads(capsys.readouterr().out)
    assert fetch.call_args.kwargs["benchmark"] == "SPY"
    assert len(pending) == 1
    assert pending[0]["rating"] == "Overweight"
    assert pending[0]["alpha_return_pct"] == "+2.0%"

    reflection = path / "reflections" / "2026-01-15.md"
    reflection.write_text("The Overweight call beat SPY by 2.0%. Demand held. Size up sooner.")
    assert ta_data.main([
        "memory-resolve", "NVDA", "2026-01-15", "--raw-return", "0.05",
        "--alpha-return", "0.02", "--holding-days", "5", "--reflection-file", str(reflection),
    ]) == 0
    assert "| +5.0% | +2.0% | 5d]" in memory_path.read_text()

    out_file = path / "past_context.md"
    ta_data.main(["memory-context", "NVDA", "--out", str(out_file)])
    assert "Size up sooner." in out_file.read_text()


def test_memory_pending_skips_entries_without_prices(run_dir, memory_path, capsys):
    path, _ = run_dir
    decision_file = path / "5_portfolio" / "decision.md"
    decision_file.write_text(DECISION)
    ta_data.main(["memory-store", "NVDA", "2026-01-15", str(decision_file)])
    capsys.readouterr()
    with patch(
        "tradingagents.graph.trading_graph.TradingAgentsGraph._fetch_returns",
        return_value=(None, None, None),
    ):
        ta_data.main(["memory-pending", "NVDA"])
    assert json.loads(capsys.readouterr().out) == []


def test_memory_pending_uses_regional_benchmark(memory_path, capsys):
    ta_data._memory_log().store_decision("7203.T", "2026-01-15", DECISION)
    with patch(
        "tradingagents.graph.trading_graph.TradingAgentsGraph._fetch_returns",
        return_value=(0.01, -0.01, 5),
    ) as fetch:
        ta_data.main(["memory-pending", "7203.T"])
    assert fetch.call_args.kwargs["benchmark"] == "^N225"
    assert json.loads(capsys.readouterr().out)[0]["benchmark"] == "^N225"


@pytest.mark.parametrize(
    ("argv", "expected_call"),
    [
        (["stock", "NVDA", "2026-01-01", "2026-01-15"],
         ("get_stock_data", "NVDA", "2026-01-01", "2026-01-15")),
        (["fundamentals", "NVDA", "2026-01-15"], ("get_fundamentals", "NVDA", "2026-01-15")),
        (["balance-sheet", "NVDA", "2026-01-15", "--freq", "annual"],
         ("get_balance_sheet", "NVDA", "annual", "2026-01-15")),
        (["income-statement", "NVDA", "2026-01-15"],
         ("get_income_statement", "NVDA", "quarterly", "2026-01-15")),
        (["news", "NVDA", "2026-01-08", "2026-01-15"],
         ("get_news", "NVDA", "2026-01-08", "2026-01-15")),
        (["global-news", "2026-01-15", "--limit", "5"],
         ("get_global_news", "2026-01-15", None, 5)),
        (["insider", "NVDA"], ("get_insider_transactions", "NVDA")),
        (["macro", "cpi", "2026-01-15"], ("get_macro_indicators", "cpi", "2026-01-15", None)),
        (["prediction-markets", "Fed rate cut"], ("get_prediction_markets", "Fed rate cut", None)),
    ],
)
def test_data_commands_route_to_vendor(argv, expected_call, capsys):
    with patch.object(ta_data, "route_to_vendor", return_value="DATA") as route:
        assert ta_data.main(argv) == 0
    route.assert_called_once_with(*expected_call)
    assert capsys.readouterr().out == "DATA\n"


def test_indicators_split_and_keep_going_on_bad_name(capsys):
    def fake(method, ticker, name, curr_date, look_back):
        if name == "bogus":
            raise ValueError("unknown indicator bogus")
        return f"{name} ok"

    with patch.object(ta_data, "route_to_vendor", side_effect=fake):
        ta_data.main(["indicators", "NVDA", "RSI, bogus ,macd", "2026-01-15"])
    out = capsys.readouterr().out
    assert "rsi ok" in out and "macd ok" in out and "unknown indicator bogus" in out


def test_vendor_errors_become_readable_messages(capsys):
    with patch.object(ta_data, "route_to_vendor", side_effect=RuntimeError("rate limited")):
        assert ta_data.main(["stock", "NVDA", "2026-01-01", "2026-01-15"]) == 1
    assert "ERROR (RuntimeError): rate limited" in capsys.readouterr().err
