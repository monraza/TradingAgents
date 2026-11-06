---
name: trading-analysis
description: Run the TradingAgents multi-agent analysis (four analysts, bull/bear debate, research manager, trader, risk debate, portfolio manager) on a stock or crypto ticker inside Claude Code, with no LLM API key. Use when the user runs /trading-analysis or asks to analyse a ticker with TradingAgents.
argument-hint: TICKER [YYYY-MM-DD] [rounds=N] [risk_rounds=N] [analysts=market,sentiment,news,fundamentals]
---

# TradingAgents workflow

Arguments: `$ARGUMENTS`

You are the orchestrator. Every role is a subagent defined in `.claude/agents/ta-*.md`. The subagents read and write files in a run directory and reply with one line each, so you don't write any analysis yourself and don't need to read the reports until the end. Run the steps in order. Every step after the analysts depends on the files the step before it wrote.

## 1. Parse the arguments

- `TICKER` (required). If it's missing, ask for it. Exchange suffixes work (`0700.HK`, `RELIANCE.NS`, `AZN.L`), and so does crypto (`BTC-USD`).
- `DATE` (optional, `YYYY-MM-DD`). Defaults to today and must not be in the future. A weekend or holiday is fine: the tools use the last trading day on or before it.
- `rounds=N`: bull/bear debate rounds, default 1.
- `risk_rounds=N`: risk debate rounds, default 1.
- `analysts=...`: a subset of `market,sentiment,news,fundamentals`. Defaults to the list in `run.json`.

More rounds give a deeper analysis but use more of the subscription's usage. Each extra round adds 2 (debate) or 3 (risk) subagent runs.

## 2. Check Python and set up the run

Run `python scripts/ta_data.py --help`. If `python` isn't found, try `python3`. Call whichever works `PY`, and pass it to every subagent. If you get an import error, tell the user to run `pip install -e .` in the repo and stop.

Then run:

```
PY scripts/ta_data.py init-run TICKER [DATE]
```

It prints JSON. Keep `run_dir` (call it `RUN_DIR`), `ticker` (the normalised symbol, which you use from here on), `trade_date`, `asset_type` and `analysts`.

If `already_existed` is `true`, an earlier run for this ticker and date left files in `RUN_DIR`. Unless the user asked for a fresh run, resume: skip every step whose output file already exists. For a fresh run, delete `RUN_DIR` and run `init-run` again.

## 3. Learn from past decisions

```
PY scripts/ta_data.py memory-pending TICKER
```

This prints a JSON list of earlier decisions on this ticker whose outcome is now known. For each entry, write a reflection yourself: **2 to 4 sentences of plain prose**, with no bullets, headers or markdown, covering in order:

1. whether the directional call was right (cite `alpha_return_pct` against `benchmark`);
2. which part of the investment thesis held or failed;
3. one concrete lesson for the next similar analysis.

Be specific and terse, because future runs re-read it verbatim. Write it to `RUN_DIR/reflections/<trade_date>.md` with the Write tool, then record it:

```
PY scripts/ta_data.py memory-resolve TICKER <trade_date> --raw-return <raw_return> --alpha-return <alpha_return> --holding-days <holding_days> --reflection-file RUN_DIR/reflections/<trade_date>.md
```

If you resolved any entries, refresh the context the Portfolio Manager reads:

```
PY scripts/ta_data.py memory-context TICKER --out RUN_DIR/past_context.md
```

## 4. Analyst team (run in parallel)

In a **single message**, launch one Agent call per selected analyst so they run in parallel:

| Analyst | `subagent_type` | Writes |
|---|---|---|
| market | `ta-market-analyst` | `1_analysts/market.md` |
| sentiment | `ta-sentiment-analyst` | `1_analysts/sentiment.md` |
| news | `ta-news-analyst` | `1_analysts/news.md` |
| fundamentals | `ta-fundamentals-analyst` | `1_analysts/fundamentals.md` |

Skip fundamentals for crypto. `run.json` already leaves it out.

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

Read `RUN_DIR/5_portfolio/decision.md` and give the user a short summary:

- the final **rating** and the executive summary;
- the Research Manager's recommendation and the trader's action, if they differ from the final rating;
- any report that was missing or any data source that failed;
- the path to `RUN_DIR/complete_report.md`.

End with one line saying this is LLM-generated research for study purposes, not financial advice.
