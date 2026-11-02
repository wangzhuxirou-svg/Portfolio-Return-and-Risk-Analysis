"""English report presentation. All dates, values and source records come from this run."""
from __future__ import annotations
import html
import json
import re
from pathlib import Path
import pandas as pd

RPMC_URL='https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/finance-research-and-trading-lab/events/rpmc/'
LABELS={
 'observations':'Return observations','cumulative_return':'Cumulative return','annual_volatility':'Annualized volatility',
 'max_drawdown':'Maximum drawdown','sharpe':'Sharpe ratio','start':'Start','end':'End','series':'Series',
 'realized_gross_CAD_estimate':'Est. realized gross P/L (CAD)','unrealized_gross_CAD_estimate':'Est. unrealized gross P/L (CAD)',
 'price_effect_CAD_estimate':'Est. price effect (CAD)','fx_effect_CAD_estimate':'Est. FX effect (CAD)',
 'dividends_confirmed_CAD':'Confirmed dividends (CAD)','commission_cash_effect_CAD_supported':'Supported signed commission (CAD)',
 'net_contribution_CAD_estimate':'Est. net contribution (CAD)','weight_of_initial_capital_estimate':'Est. contribution / initial capital',
 'remaining_gross_cost_CAD_estimate':'Est. remaining gross cost (CAD)','sharpe_estimate':'Short-sample Sharpe estimate',
 'market_return_observations':'Market-day return observations','sharpe_sample_start':'Sharpe sample start',
 'sharpe_sample_end':'Sharpe sample end','annual_volatility_estimate':'Est. annualized volatility',
 'fund':'Fund','holdings_asof':'Holdings date','security_weight_coverage':'Identified security-weight coverage',
 'list_status':'Coverage and limitations','source':'Official source URL','fetched_at_utc':'Retrieved at (UTC)',
 'direct_weight':'Direct weight','identified_indirect_weight':'Identified indirect ETF weight',
 'total_identified_weight':'Total identified weight','funds':'Source funds','note':'Limitations',
 'portfolio_weight':'Portfolio weight','portfolio_exposure':'Identified portfolio exposure','name':'Security / instrument',
 'level':'Severity','code':'Check','detail':'Details','currency':'Currency','method':'Method','weight':'Weight'
}

def pretty(value):
    return LABELS.get(str(value),str(value).replace('_',' ').strip().capitalize())

def render_report(a,cfg,p,weights,actual,estimate,risk,coverage,out):
    """Render numeric tables without recalculating financial results or translating snapshots."""
    def table(df,percent=()):
        f=df.copy()
        for col in percent:
            if col in f: f[col]=f[col].map(lambda x:'Unavailable' if pd.isna(x) else f'{x:.2%}')
        f=f.rename(columns=pretty)
        if f.index.name: f.index.name=pretty(f.index.name)
        return f.to_html(border=0,escape=True,float_format=lambda x:f'{x:,.4f}',na_rep='Unknown / unavailable')
    def read(name):
        file=out/name
        if not file.exists(): return pd.DataFrame()
        try: return pd.read_csv(file,index_col=0)
        except pd.errors.EmptyDataError: return pd.DataFrame()
    def saved_table(name,percent=()):
        f=read(name)
        return table(f,percent) if not f.empty else '<p class="muted">Unavailable for this run; see validation records.</p>'
    def image(name,title):
        a.unavailable(out/f'{name}.png',title)
        return f'<figure><img src="{name}.png" alt="{html.escape(title)}"><figcaption>{html.escape(title)}</figcaption></figure>'
    asof=html.escape(str(cfg['positions_asof']))
    parts=['''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Portfolio Return and Risk Analysis (CAD)</title><style>body{font-family:Arial,sans-serif;max-width:1240px;margin:32px auto;padding:0 22px;color:#24364b;background:#fafcfe}h1,h2,h3{color:#153c5c}h2{margin-top:36px;border-bottom:2px solid #d4e1ec;padding-bottom:8px}p,li{line-height:1.7}table{display:block;overflow:auto;border-collapse:collapse;font-size:13px;max-width:100%;background:white}td,th{padding:9px;border-bottom:1px solid #d8e3ed;text-align:right}th{background:#eaf2f8}img{width:100%;max-width:100%;background:white}figure{margin:20px 0}figcaption,.muted{color:#53697c;font-size:13px}.notice{background:#fff2d5;border-left:4px solid #dba134;padding:15px}a{color:#175b91}@media print{body{max-width:none;background:white}figure,table{break-inside:avoid}table{font-size:10px}}</style></head><body>''',
        '<h1>Portfolio Return and Risk Analysis (CAD)</h1>',
        f'<p>Valuation date: <b>{asof}</b>. Configured initial capital: <b>CAD {cfg["initial_capital_CAD"]:,.2f}</b>. All values, holdings and dates below are generated from this run.</p>',
        '<p class="notice">Actual account performance, the simulated benchmark using the official allocation, and the hypothetical historical simulation of current holdings are separate. Estimates are not independently confirmed settlement records. Historical results are not forecasts. Unknown values are not zero.</p>',
        '<h2>1. Actual account performance</h2>',
        f'<p><b>{html.escape(actual["label"])}</b>. From {actual["start"]} to {actual["end"]}: {actual["calendar_observations"]} calendar-day valuations; cumulative change <b>{actual["cumulative_change"]:.3%}</b>; maximum drawdown <b>{actual["max_drawdown"]:.3%}</b>. Interpret NAV change as investment return only when external flows have been ruled out or correctly adjusted. Dividends, interest and fees already reflected in NAV are not applied again.</p>']
    if cfg['external_flows']=='none':
        parts.append('<p>Additional external deposits and withdrawals have been confirmed absent through this valuation date. Security trades are internal account movements; dividends and interest are investment income.</p>')
    elif cfg['external_flows']=='provided':
        parts.append('<p>Confirmed end-of-day external flows are deducted from each closing valuation before computing and geometrically linking interval returns. Intraday flow timing cannot be guessed.</p>')
    else:
        parts.append('<p class="notice">External flows are unconfirmed. The account series shows unadjusted NAV change and must not be represented as verified flow-adjusted investment performance.</p>')
    parts.append(saved_table('actual_comparison_summary.csv',['cumulative_return','max_drawdown']))
    benchmark=cfg.get('benchmark',{})
    if benchmark:
        weights_text=', '.join(f'{html.escape(cfg.get("assets",{}).get(s,cfg.get("benchmark_assets",{}).get(s,{})).get("ticker",s))}: {w:.0%}' for s,w in benchmark['weights'].items())
        parts.append(f'<p><a href="{html.escape(benchmark.get("source",RPMC_URL))}">Official benchmark configuration</a>: {weights_text}; inception at the {html.escape(benchmark["inception"])} close. IEMG.K is mapped by the issuer fund name to IEMG (NYSE Arca, USD), rather than mechanically editing a suffix.</p>')
        label=actual.get('comparison',{}).get('label','Benchmark unavailable')
        parts.append(f'<p>Benchmark used in this run: <b>{html.escape(label)}</b>. Platform RPMC Benchmark NAV is preferred. If it is absent, the official allocation is simulated: CAD allocation at inception close, buy and hold across ETFs with no subsequent cross-fund rebalancing, total-return reinvestment within each fund, and no additional brokerage fees, tax or FX spreads. These are analytical assumptions, not confirmed official reinvestment/rebalancing/fee rules. Fund expenses are already reflected in prices. SPY is an additional reference, not the official benchmark by itself.</p>')
    parts.append('<p>Comparisons use the same start and end dates for the account and benchmark; full-period account results remain separate if benchmark inception is later. Weekends and holidays explicitly carry the latest closing values and corresponding FX for up to four calendar days, with an audit trail. Actual weekend NAV interest is retained. Competition Sharpe is a short-sample estimate using common market-day observations and prior-published CAD Treasury-bill yields with a 252 factor. It is not claimed to equal the platform score; calendar-day returns are not simply annualized using 252.</p>')
    parts.extend([image('actual_benchmark','Actual account and benchmark: identical comparison dates'),image('actual_drawdown','Actual account drawdown: calendar-day valuations')])
    parts.append('<h3>CAD profit/loss and contribution estimates</h3>')
    columns=['realized_gross_CAD_estimate','unrealized_gross_CAD_estimate','price_effect_CAD_estimate','fx_effect_CAD_estimate','dividends_confirmed_CAD','commission_cash_effect_CAD_supported','net_contribution_CAD_estimate','weight_of_initial_capital_estimate']
    parts.append(table(estimate.reindex(columns=columns),['weight_of_initial_capital_estimate']))
    methods='; '.join(str(x) for x in estimate.get('method',pd.Series(dtype=str)).dropna().unique())
    parts.append('<p>Methods used: '+html.escape(methods or 'Unavailable')+'. FIFO is used; dividends do not add shares. Settlement evidence must match the exact transaction-file SHA256 and pass cash reconciliation. Otherwise, public daily FX is used only as an explicitly labelled estimate. Missing fees are not assumed zero. Price effects use acquisition FX, FX effects use terminal prices, and their interaction term is allocated to FX. The two effects sum to gross CAD P/L; fees are booked separately once. Contribution percentages are relative to configured initial capital, not a multi-period TWR attribution.</p>')
    parts.append('<h3>Cash and P/L bridge (CAD)</h3>'+saved_table('cash_and_pnl_bridge.csv'))
    parts.append('<h3>Position and account reconciliation (CAD)</h3>'+saved_table('reconciliation.csv'))
    parts.append('<p>Position MarketValue is already in CAD and is never converted again. Buying Power is not cash. Discrepancies are retained without altering cash or inventing fees. The Summary interest field requires documentation or reconciliation support before being treated as cumulative interest; it is not assumed to be an annual interest rate. Commission amounts, currencies and signs must follow this competition Session Rules, not another competition defaults.</p>')
    parts.append('<h2>2. Hypothetical historical simulation of current holdings</h2>')
    if risk is not None:
        parts.append(table(risk[0],['cumulative_return','annual_volatility','max_drawdown']))
        contributions=risk[1]
        if contributions.risk_share.notna().any():
            top=contributions.risk_share.idxmax()
            parts.append(f'<p>Largest historical volatility-risk contributor: <b>{html.escape(str(top))}</b>, with asset weight {contributions.loc[top,"weight"]:.2%} and volatility-risk share {contributions.loc[top,"risk_share"]:.2%}. Risk contribution reflects weight, volatility and covariance; a small high-volatility position can have a disproportionate effect.</p>')
    else: parts.append('<p>Complete portfolio historical risk is unavailable; missing assets are not dropped and the rest renormalized as if complete.</p>')
    parts.append('<p>Approximately three years are requested. Assets use the same common valid price/FX dates. Yahoo Finance Adj Close is a split- and dividend-adjusted total-return proxy; dividends are not added again. USD history is converted using corresponding-date Bank of Canada USD/CAD. Security prices are not filled; FX may explicitly carry the latest previously published value for up to four calendar days. Holiday gaps yield interval returns, making 252 annualization approximate.</p>')
    parts.append('<p>Simulation assumptions: daily fixed-current-weight rebalancing, zero CAD cash return, no transaction costs, tax or market impact. This is not buy-and-hold or actual competition history. Annualized volatility = sample standard deviation × sqrt(252); Sharpe = mean interval excess return / sample standard deviation × sqrt(252). The risk-free proxy TB.CDN.90D.MID uses yields published before each interval, compounded over actual days/365; quoted yield is only a return proxy. Euler covariance volatility contributions sum to portfolio volatility and can be negative. Historical drawdown and correlations do not predict future outcomes.</p>')
    for name,title in [('simulated_return','Hypothetical historical return: daily fixed-weight rebalancing'),('simulated_drawdown','Hypothetical historical drawdown'),('correlation','CAD total-return correlations: common historical window'),('weights','Current identifiable asset weights: detailed positions plus cash'),('risk_contribution','Current weights and historical volatility-risk contributions')]: parts.append(image(name,title))
    parts.append('<h2>3. Official issuer ETF look-through: current exposures only</h2>'+table(coverage,['security_weight_coverage']))
    parts.append('<p>Only official issuer sources are used, with source URLs, holdings dates and UTC retrieval times. Current holdings do not represent fund holdings over the preceding three years. Exposure equals current portfolio fund weight × internal security weight. XEF into IEFA is recursively multiplied. Partial VEE/ZCN security lists are not treated as complete; official sector/country aggregates and coverage are recorded separately. Stocks, bonds, Treasury bills, cash and gold remain distinct categories. Rounded residuals and negative cash are retained, not normalized away. GLD is classified structurally as physical gold; an exact gold/cash/liability split may remain unavailable.</p>')
    for name,title in [('direct_etf_overlap.csv','Overlap with direct stock holdings'),('lookthrough_sector.csv','Issuer sector / bond categories'),('lookthrough_country.csv','Issuer country-classification proxies'),('lookthrough_underlying_currency.csv','Underlying quotation-currency proxies')]:
        parts.append('<h3>'+title+'</h3>'+saved_table(name,['portfolio_weight','direct_weight','identified_indirect_weight','total_identified_weight']))
    under=read('underlying_exposure.csv')
    if not under.empty: parts.append('<h3>Largest identified underlying exposures (incomplete ranking)</h3>'+table(under.head(20),['portfolio_exposure']))
    parts.append('<p>CAD listing does not remove FX risk. Market Currency / Local Currency is a quotation proxy, not company revenue exposure or net hedged economic currency exposure. Issuer sector taxonomies are retained rather than forced into one standard. Absence from a partial list cannot prove zero indirect exposure. ISIN/CUSIP identifiers are preferred; name-based matching is only a proxy and does not establish identical share classes.</p>')
    a.csave(pd.DataFrame(a.ISSUES),out/'validation.csv')
    if a.ALIGNMENTS: a.csave(pd.concat(a.ALIGNMENTS,ignore_index=True),out/'alignment_audit.csv')
    parts.append('<h2>Validation, provenance and remaining limitations</h2>'+table(pd.DataFrame(a.ISSUES)))
    for filename,title in [('sources.json','Market and central-bank sources'),('issuer_sources.json','Official issuer and rule sources')]:
        path=out/filename
        if path.exists():
            sources=pd.DataFrame(json.loads(path.read_text(encoding='utf-8')))
            parts.append('<h3>'+title+'</h3>'+table(sources))
    parts.append('<p>Supporting records include run_config.json, input_manifest.json, alignment_audit.csv, validation.csv, etf_coverage.csv, issuer_allocations.csv and etf_lookthrough.csv. Source records contain the actual URLs, retrieval times and available windows. The download ZIP includes these CSV/JSON files. This report is generated directly from current-run data and contains no frozen prior-account narrative.</p></body></html>')
    (out/'report.html').write_text('\n'.join(parts),encoding='utf-8')
