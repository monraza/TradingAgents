---
name: trading-analysis
description: Run the TradingAgents multi-agent analysis (four analysts, bull/bear debate, research manager, trader, risk debate, portfolio manager) on a stock or crypto ticker inside Claude Code, with no LLM API key. Use when the user runs /trading-analysis or asks to analyse a ticker with TradingAgents.
argument-hint: TICKER [YYYY-MM-DD] [rounds=N] [risk_rounds=N] [analysts=market,sentiment,news,fundamentals] [portfolio=FILE]
---

# TradingAgents workflow

Arguments: `$ARGUMENTS`

You are the orchestrator. Every role is a subagent defined in `.claude/agents/ta-*.md`. The subagents read and write files in a run directory and reply with one line each, so you don't write any analysis yourself and don't need to read the reports until the end. Run the steps in order. Every step after the analysts depends on the files the step before it wrote.

## 1. Parse the arguments

- `TICKER` (required). If it's missing, ask for it. Exchange suffixes work (`0700.HK`, `RELIANCE.NS`, `AZN.L`), and so does crypto (`BTC-USD`).
- `DATE` (optional, `YYYY-MM-DD`). Defaults to today and must not be in the future. A weekend or holiday is fine: the tools use the last trading day on or before it. For a past date, every data tool serves data as it stood that day.
- `rounds=N`: bull/bear debate rounds, default 1.
- `risk_rounds=N`: risk debate rounds, default 1.
- `analysts=...`: a subset of `market,sentiment,news,fundamentals`, default all four.
- `portfolio=FILE`: optional JSON file with the caller's holdings and cash, for example `{"cash": 25000, "currency": "USD", "positions": [{"ticker": "NVDA", "quantity": 120, "average_price": 150.0}]}`. The trader, risk analysts and portfolio manager then work against that book. An empty `positions` list means a flat book; no file means the holdings are unknown.

More rounds give a deeper analysis but use more of the subscription's usage. Each extra round adds 2 (debate) or 3 (risk) subagent runs.

## 2. Check Python and set up the run

Run `python scripts/ta_data.py --help`. If `python` isn't found, try `python3`. Call whichever works `PY`, and pass it to every subagent. If you get an import error, tell the user to run `pip install -e .` in the repo and stop.

Then run:

```
PY scripts/ta_data.py init-run TICKER [DATE] [--analysts LIST] [--portfolio FILE]
```

It prints JSON. Keep `run_dir` (call it `RUN_DIR`), `ticker` (the normalised symbol), `trade_date`, `asset_type` and `analysts`. If it prints an error (a future date, an unknown analyst, an unusable portfolio file), show it to the user and stop.

If `already_existed` is `true`, an earlier run for this ticker and date left files in `RUN_DIR`. Unless the user asked for a fresh run, resume: skip every step whose output file already exists. For a fresh run, delete `RUN_DIR` and run `init-run` again.

## 3. Settle past decisions

```
PY scripts/ta_data.py memory-pending
```

This prints `{"ready": [...], "failed": [...]}` covering every ticker in the memory log. `ready` lists earlier decisions whose holding window has now traded. For each one, write a reflection yourself: **2 to 4 sentences of plain prose**, with no bullets, headers or markdown. The outcome covers `holding_days` trading days from the first close after the decision date, which may be shorter than the horizon the decision was written for. Cover, in order:

1. what the `holding_days`-day alpha shows about the directional call (cite `alpha_return_pct` against `benchmark`), saying plainly if the window is too short to judge the thesis;
2. which part of the investment thesis this window supports or undercuts;
3. one concrete lesson for the next similar analysis.

Be specific and terse, because future runs re-read it verbatim. Write it to `RUN_DIR/reflections/<ticker>_<trade_date>.md` with the Write tool, then record it:

```
PY scripts/ta_data.py memory-resolve <ticker> <trade_date> --raw-return <raw_return> --alpha-return <alpha_return> --holding-days <holding_days> --resolution-date <resolution_date> --reflection-file RUN_DIR/reflections/<ticker>_<trade_date>.md
```

If any entries were settled, refresh the lessons the Portfolio Manager reads (only those known by the trade date are included):

```
PY scripts/ta_data.py memory-context RUN_DIR
```

If `failed` is not empty, write one line to `RUN_DIR/memory_note.md`, e.g. `2 past decision(s) could not be settled this run and stay pending.` The report header will show it.

## 4. Analyst team (run in parallel)

In a **single message**, launch one Agent call per analyst in `analysts` so they run in parallel:

| Analyst | `subagent_type` | Writes |
|---|---|---|
| market | `ta-market-analyst` | `1_analysts/market.md` |
| sentiment | `ta-sentiment-analyst` | `1_analysts/sentiment.md` |
| news | `ta-news-analyst` | `1_analysts/news.md` |
| fundamentals | `ta-fundamentals-analyst` | `1_analysts/fundamentals.md` |

Prompt each one with:

```
RUN_DIR=<RUN_DIR>
PY=<PY>
Write your report for <TICKER> as of <trade_date>.
```

Wait for all of them to finish. Check that each report file exists. Retry a missing one once; if it still fails, carry on and tell the user which report is missing at the end.

## 5. Investment debate (sequential)

For each round `r` from 1 to `rounds`, run the bull and then the bear, one at a time:

1. `PY scripts/ta_data.py next-turn RUN_DIR/2_research/debate bull` prints `TURN_FILE`.
2. Launch `ta-bull-researcher` with `RUN_DIR`, `PY` and `TURN_FILE`.
3. Repeat steps 1 and 2 with `bear` and `ta-bear-researcher`.

## 6. Research Manager

Launch `ta-research-manager` with `RUN_DIR` and `PY`. It writes `2_research/manager.md`.

## 7. Trader

Launch `ta-trader` with `RUN_DIR`. It writes `3_trading/trader.md`.

## 8. Risk debate (sequential)

For each round from 1 to `risk_rounds`, run these in order, getting each `TURN_FILE` from `next-turn RUN_DIR/4_risk/debate <speaker>`:

1. `aggressive` → `ta-aggressive-risk`
2. `conservative` → `ta-conservative-risk`
3. `neutral` → `ta-neutral-risk`

## 9. Portfolio Manager

Launch `ta-portfolio-manager` with `RUN_DIR` and `PY`. It writes `5_portfolio/decision.md`.

## 10. Finish

```
PY scripts/ta_data.py compile-report RUN_DIR
PY scripts/ta_data.py memory-store TICKER <trade_date> RUN_DIR/5_portfolio/decision.md
```

If `memory-store` warns that the decision was logged as `REVIEW`, no rating could be read from it: tell the user, and ask whether to re-run the Portfolio Manager.

Read `RUN_DIR/5_portfolio/decision.md` and give the user a short summary:

- the final **rating** and the executive summary;
- the Research Manager's recommendation and the trader's action, if they differ from the final rating;
- any report that was missing, any data source that was unavailable, and any past decision that could not be settled;
- the paths to `RUN_DIR/complete_report.md` and `RUN_DIR/complete_report.html`.

End with one line saying this is LLM-generated research for study purposes, not financial advice.
