# Portfolio Return and Risk Analysis

A repeatable Python tool for a StockTrak stock, ETF and cash portfolio, with **CAD as the reporting currency**. Documentation, reports, charts, table labels and validation messages are in English. Reports are generated from each run's data; no account dates, balances or narrative results are frozen into a translated snapshot.

Actual account performance, a benchmark using the official competition allocation, and the hypothetical history of current holdings are presented separately. **Historical results are not forecasts.**

## What it does

- Reads StockTrak positions, complete transaction history, daily account values and portfolio summaries. Original inputs are preserved and cleaned data saved separately.
- Calculates actual NAV changes, cumulative returns and drawdowns, with explicit external-cash-flow confirmation or supported end-of-day adjustment.
- Separates trades from dividends, uses FIFO accounting, and checks position quantities, market values, cash, borrowing and account-value reconciliation.
- Labels estimated CAD costs, FX effects and contributions. Unknown fees remain unavailable rather than zero; settlement evidence is bound to an exact transaction-file SHA256.
- Downloads and caches approximately three years of split/dividend-adjusted prices, USD/CAD and Canadian three-month Treasury-bill yields, with source URLs, retrieval times and actual available windows.
- Measures volatility, drawdown, Sharpe, correlations, weights and Euler volatility-risk contributions for a hypothetical current-weight simulation.
- Obtains ETF disclosures from official BlackRock/iShares, Vanguard, BMO and State Street sources, with holdings dates, partial coverage, unresolved exposure and nested funds disclosed.
- Automatically creates an **English HTML report, seven charts, a standalone offline HTML copy, and a ZIP with supporting CSV/JSON records**.

Private account inputs, local configuration, caches and generated reports are excluded from Git.

## Install

Python 3.10 or newer is required. From PowerShell:

```powershell
git clone https://github.com/wangzhuxirou-svg/Portfolio-Return-and-Risk-Analysis.git
cd Portfolio-Return-and-Risk-Analysis
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
New-Item -ItemType Directory -Force data/raw | Out-Null
```

On macOS/Linux, create the environment with `python3 -m venv .venv`, then use `.venv/bin/python` for the commands below. Charts use Matplotlib's bundled DejaVu Sans font. `requirements-lock.txt` records the exact environment used for validation; `requirements.txt` provides supported dependency ranges.

## Supply your own inputs

Export complete competition-to-date files from StockTrak, using these local filenames. Empty headers are provided in `templates/`.

| File | Required fields and interpretation |
|---|---|
| `data/raw/positions.csv` | `Symbol, Quantity, Currency, LastPrice, PricePaid, MarketValue`. Prices are in the asset trading currency; `MarketValue` is already CAD. |
| `data/raw/transactions.csv` | `Symbol, TransactionType, Exchange, Quantity, Currency, Price, Amount, CreateDate`. Timestamp format: `MM/DD/YYYY - HH:MM`. Preserve original amounts and signs; currency must be verified field by field. |
| `data/raw/nav.csv` | `Date, Value`. Date format: `MM/DD/YYYY hh:mm:ss AM/PM`. `Value` is CAD account NAV, including weekend records. |
| `data/raw/summary.csv` | StockTrak's native multi-column key/value format, including `Date`, `Cash Balance`, `Loan Balance`, `Market Value of Long Positions`, `Market Value of Short Positions`, and `Portfolio Value`. |

Back up old inputs before replacing them. Do not append a new complete export to an old complete export. Each run starts from the source files again. Identical-looking transactions are retained and flagged rather than automatically removed; duplicate NAV dates are rejected. Input timestamps without timezone labels are interpreted in `America/Toronto`; midnight labels do not establish beginning-of-day valuation.

## Configure and run

Edit your ignored local `config.json`:

- `positions_asof`: actual holdings date, preferably matching the Summary date.
- `initial_capital_CAD`: actual initial account capital.
- `external_flows`: defaults to `unknown`. Change to `none` only after confirming no additional external deposits/withdrawals through the valuation date. Use `provided` with `data/external_flows.csv` for supported, confirmed end-of-day CAD flows. Trades are internal movements; dividends and interest are income, not external deposits.
- `assets`: verify each Yahoo ticker, trading currency, StockTrak exchange and allowed Yahoo exchange codes. Example mappings are not actual portfolio quantities or recommendations.
- `accounting_evidence`: defaults to no verified commission evidence. Supply the transaction-file SHA256, commission evidence and amounts only when supported by this competition's records. Never reuse another competition's default fees.

```powershell
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe main.py --offline
.\.venv\Scripts\python.exe main.py --refresh
.\.venv\Scripts\python.exe -m pytest -q
```

Normal mode downloads missing data and reuses caches. `--offline` never downloads. `--refresh` requests fresh market and issuer data. `--validate-only` skips historical-market acquisition but still processes account inputs and issuer disclosures; missing historical sections are explicitly unavailable. Newly downloaded issuer holdings dated after the valuation date are rejected for that historical analysis, so preserve historical caches when working with old snapshots.

Each run creates a separate directory under the configured `output_dir`. `LATEST.txt` points to the latest completed run. Review `validation.csv`: exit code 0 means a report was produced, not that all data or indicators were available. Fatal input/calculation errors return 2 and save `failure.json`.

## English reports and downloads

Every successful analysis run produces:

| Output | Purpose |
|---|---|
| `report.html` | English report with local chart files. |
| `report_en.html` | Standalone English report with every chart embedded. |
| `Portfolio_Report_EN_<valuation-date>.html` | Date-labelled standalone download. |
| `Portfolio_Report_EN_<valuation-date>.zip` | Standalone report plus supporting CSV/JSON records. |

All report dates, observations, returns, holdings, risk contributors, warnings and provenance come from the current run. A changed input date changes the output date and content automatically. Missing data remain visible. The exporter does not translate an old fixed report or inject past account balances.

To recreate downloads from an existing completed English run without fetching data or rerunning calculations:

```powershell
.\.venv\Scripts\python.exe export_report.py
.\.venv\Scripts\python.exe export_report.py --run outputs/<run-directory>
.\.venv\Scripts\python.exe export_report.py --run outputs/<run-directory> --output-dir downloads
```

The default exporter finds `LATEST.txt` using the local config's `output_dir`; `--config` selects a different configuration. Generated reports may contain private account information and should only be shared intentionally.

## Official benchmark and additional reference

The example includes the [RPMC official allocation](https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/finance-research-and-trading-lab/events/rpmc/), established at the 2026-10-09 close: SPY 35%, XIC.TO 15%, IEMG.K 20%, XBB.TO 20%, and GLD 10%. The official fund name identifies IEMG.K as issuer ticker IEMG (NYSE Arca, USD), used by Yahoo Finance.

Platform NAV from the **RPMC Benchmark** account in `data/official_benchmark_nav.csv` (`date,value,currency`, CAD) is preferred. Without it, the report uses a **simulated benchmark using the official allocation**. Assumptions: CAD allocation at inception close, buy and hold across funds with no subsequent cross-fund rebalancing, total-return reinvestment within each fund, and no separate transaction fees, taxes or FX spreads. These are analytical assumptions, not confirmed official reinvestment/rebalancing/fee rules. SPY remains an additional reference, not the official benchmark by itself. Comparisons use identical account/benchmark start and end dates.

## Methods and limitations

**Actual performance.** Uses actual NAV, preserving weekend records. Income and fees already reflected in NAV are not applied again. Unconfirmed external flows allow only an unadjusted NAV-change interpretation. Confirmed end-of-day external-flow returns use `(NAV_t - flow_t) / NAV_(t-1) - 1`, geometrically linked; unknown intraday timing is rejected. Competition-period Sharpe is only a short-sample common-market-day estimate, not the official platform score.

**Accounting.** FIFO separates trades and dividends. Verified settlement inputs are separately retained; reconciliation-supported or public-FX estimates remain labelled. Price effects use acquisition FX, FX effects use terminal prices, and the interaction term is allocated to FX. Unknown fees are not zero. Stock splits, mergers, transfers and opening holdings require complete records; quantity mismatches are flagged rather than guessed. Borrowing and short positions are unsupported in this version.

**Historical prices and FX.** Yahoo Finance `Adj Close` is a split/dividend-adjusted total-return proxy; distributions are not added again. USD history is converted using corresponding-date Bank of Canada `FXUSDCAD` (CAD per USD), not today's FX. Public daily FX is an estimate, not platform execution FX. Security prices are not filled; FX can explicitly carry a previous observation for up to four calendar days, with audit records. Download failures and actual data windows are disclosed.

**Current-holdings simulation.** Daily rebalancing to current fixed weights; zero CAD cash return; no trading costs, taxes or impact. This is neither buy-and-hold nor actual competition history. Assets share a common valid window; US/Canadian holiday differences cause excluded dates and interval returns, making 252 annualization approximate. Volatility is sample standard deviation times sqrt(252). Sharpe is mean interval excess return divided by its sample standard deviation, times sqrt(252). The CAD three-month Treasury-bill proxy `TB.CDN.90D.MID` uses yields published before the interval, compounded over actual days/365; quoted yield is only a return proxy. Euler volatility-risk contributions sum to portfolio volatility and may be negative.

**ETF look-through.** Current issuer disclosures describe current exposure, not holdings over the preceding three years. BlackRock/iShares full CSVs, State Street SPY holdings and issuer summaries are cached. XEF's IEFA position is recursively multiplied. VEE/ZCN may have only partial security lists, supplemented with official sector/country aggregates; top-ten lists are not represented as complete. Dates, coverage, rounding residuals, negative cash and unknown portions are retained. GLD remains a physical-gold category. Sector taxonomies are not forced into one standard. Underlying quotation currency is not revenue exposure or net economic exposure after hedging; CAD-listed international ETFs can still have FX risk.

## Optional evidence files

Templates include external cash flows, exact settlement records, a verified CAD cash-event ledger, and official benchmark NAV. Settlement fees retain their raw sign alongside the verified signed CAD cash effect. Verified cash events are listed separately rather than added to NAV again. The legacy manual ETF template is retained as a schema reference; automatic issuer sourcing is used for look-through.

## Project layout

```text
main.py                 Input validation, market acquisition, actual returns and risk
enrichment.py           Official issuer disclosures, benchmark and CAD estimates
reporting.py            Dynamic English narrative, tables, coverage and provenance
export_report.py        Standalone offline HTML and ZIP export for any completed run
config.example.json     Example mappings without private settlement evidence
templates/              CSV input headers
tests/                  Synthetic calculation and dynamic-report regression tests
requirements.txt        Supported dependency ranges
requirements-lock.txt   Exact validated environment
```

Official references: [StockTrak automatic FX](https://www.stocktrak.com/faq/want-trade-foreign-markets/), [administrator-set commissions](https://www.stocktrak.com/faq/brokerage-commission-determined/), [IEMG issuer](https://www.ishares.com/us/products/244050/ishares-msci-emerging-markets-etf), and [Bank of Canada Valet](https://www.bankofcanada.ca/valet/docs). Per-run `sources.json` and `issuer_sources.json` retain actual source URLs, retrieval times and available dates. Input hashes, configuration, alignment audits and validation records are included in the download bundle.
