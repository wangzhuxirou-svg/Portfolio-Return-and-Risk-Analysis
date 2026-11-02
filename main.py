"""StockTrak CAD analysis; actual NAV and hypothetical history stay separate."""
from __future__ import annotations
import argparse
import csv
import hashlib
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import numpy as np
import pandas as pd
import requests
import enrichment

ROOT = Path(__file__).resolve().parent
ISSUES, ALIGNMENTS = [], []
plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial']
plt.rcParams['axes.unicode_minus'] = False

def issue(level, code, detail):
    ISSUES.append(dict(level=level, code=code, detail=str(detail)))

def money(value):
    """Numeric parsing only; currency must be determined independently."""
    s = str(value).strip().replace(',', '').replace('C$', '').replace('$', '')
    if s.startswith('(') and s.endswith(')'): s = '-' + s[1:-1]
    return float(s)

def jsave(path, obj):
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

def csave(frame, path):
    frame.to_csv(path,index=True,encoding='utf-8-sig')

def mdd(index):
    return float((index/index.cummax()-1).min())

def metrics(r, rf=None):
    r=r.dropna()
    if len(r)<2: raise ValueError('At least two return observations are required')
    wealth=pd.concat([pd.Series([1.],index=[r.index[0]-pd.Timedelta(days=1)]),(1+r).cumprod()])
    sharpe=np.nan
    if rf is not None and rf.reindex(r.index).notna().all():
        excess=r-rf.reindex(r.index)
        if excess.std(ddof=1)>1e-14: sharpe=excess.mean()/excess.std(ddof=1)*np.sqrt(252)
    return dict(observations=len(r),cumulative_return=float((1+r).prod()-1),
                annual_volatility=float(r.std(ddof=1)*np.sqrt(252)),max_drawdown=mdd(wealth),sharpe=sharpe)

def nav_returns(nav, flows, status):
    """Only confirmed end-of-day external flows are supported."""
    if status=='unknown': return nav.pct_change(fill_method=None),'Unadjusted NAV change (external flows unconfirmed)'
    if status not in ('none','provided'): raise ValueError('external_flows must be unknown, none, or provided')
    cash=pd.Series(0.,index=nav.index)
    if status=='provided':
        if flows.empty or not flows.timing.eq('end_of_day').all():
            raise ValueError('Confirmed end-of-day cash flows are required; intraday flows need valuations around each flow')
        if not flows.currency.eq('CAD').all(): raise ValueError('External flows require actual CAD settlement amounts')
        f=flows.copy(); f['date']=pd.to_datetime(f.date,format='%Y-%m-%d')
        if not f.date.isin(nav.index).all() or f.date.min()<=nav.index.min():
            raise ValueError('Each cash flow must fall between valuations and have a matching valuation date')
        cash=f.groupby('date').amount.sum().reindex(nav.index,fill_value=0.)
    r=(nav-cash)/nav.shift(1)-1
    if (r.dropna()<=-1).any(): raise ValueError('Flow-adjusted return is <= -100%; check amounts and timing')
    return r,'Time-weighted return adjusted for end-of-day external flows' if status=='provided' else 'Actual NAV return (no external flows confirmed)'

def fifo(trades, quantity, last):
    lots,realized=[],0.
    for _,t in trades.sort_values(['date','source_row'],kind='stable').iterrows():
        q,p=float(t.Quantity),float(t.price_numeric)
        if t.kind=='buy': lots.append([q,p])
        elif t.kind=='sell':
            while q>1e-9:
                if not lots: raise ValueError('Sales exceed known holdings; opening positions are missing or shorting is unsupported')
                use=min(q,lots[0][0]); realized+=use*(p-lots[0][1]); q-=use; lots[0][0]-=use
                if lots[0][0]<1e-9: lots.pop(0)
    derived=sum(q for q,p in lots)
    if abs(derived-quantity)>1e-7: raise ValueError(f'Transaction-derived quantity: {derived}; current quantity: {quantity}')
    return realized,sum(q*(last-p) for q,p in lots),sum(q*p for q,p in lots)

def fifo_cad(trades, quantity, last, final_fx):
    """Verified CAD lots. FX cross-term belongs to FX effect; fees are separate."""
    lots=[]; realized=price_effect=fx_effect=0.
    for _,t in trades.sort_values(['date','source_row'],kind='stable').iterrows():
        q,p,f=float(t.Quantity),float(t.price_numeric),float(t.fx_CAD_per_local)
        if t.kind=='buy': lots.append([q,p,f])
        elif t.kind=='sell':
            while q>1e-9:
                if not lots: raise ValueError('CAD FIFO sales exceed holdings')
                use=min(q,lots[0][0]); _,cost,entry_fx=lots[0]
                realized+=use*(p*f-cost*entry_fx)
                price_effect+=use*(p-cost)*entry_fx
                fx_effect+=use*p*(f-entry_fx)
                q-=use; lots[0][0]-=use
                if lots[0][0]<1e-9: lots.pop(0)
    if abs(sum(q for q,p,f in lots)-quantity)>1e-7: raise ValueError('CAD FIFO quantity mismatch')
    unrealized=0.
    for q,cost,entry_fx in lots:
        unrealized+=q*(last*final_fx-cost*entry_fx)
        price_effect+=q*(last-cost)*entry_fx
        fx_effect+=q*last*(final_fx-entry_fx)
    if not np.isclose(price_effect+fx_effect,realized+unrealized): raise ValueError('Price and FX attribution does not reconcile')
    return realized,unrealized,price_effect,fx_effect

def load_inputs(cfg,out):
    raw=ROOT/cfg['input_dir']
    manifest=[]
    for name in ('positions.csv','transactions.csv','nav.csv','summary.csv'):
        file=raw/name
        if not file.exists(): raise ValueError(f'Missing {file}')
        manifest.append(dict(file=name,sha256=hashlib.sha256(file.read_bytes()).hexdigest(),bytes=file.stat().st_size))
    jsave(out/'input_manifest.json',manifest)
    p=pd.read_csv(raw/'positions.csv')
    required={'Symbol','Quantity','Currency','LastPrice','PricePaid','MarketValue'}
    if not required<=set(p): raise ValueError(f'Missing position fields:  {required-set(p)}')
    if p[list(required)].isna().any().any() or p.Symbol.duplicated().any(): raise ValueError('Positions contain missing values or duplicate symbols')
    for col in ['Quantity','LastPrice','PricePaid','MarketValue']: p[col]=p[col].map(money)
    if not np.isfinite(p[['Quantity','LastPrice','PricePaid','MarketValue']].to_numpy()).all() or (p[['Quantity','LastPrice','PricePaid','MarketValue']]<=0).any().any(): raise ValueError('Only long positions with finite positive quantities and prices are supported')
    p=p.set_index('Symbol')
    for s,row in p.iterrows():
        if s not in cfg['assets'] or row.Currency!=cfg['assets'][s]['currency']: raise ValueError(f'{s} missing symbol mapping or currency mismatch')
        if row.Currency=='CAD' and abs(row.Quantity*row.LastPrice-row.MarketValue)>.02: raise ValueError(f'{s} CAD market value mismatch')
    t=pd.read_csv(raw/'transactions.csv',keep_default_na=False)
    required={'Symbol','TransactionType','Exchange','Quantity','Currency','Price','Amount','CreateDate'}
    if not required<=set(t): raise ValueError(f'Missing transaction fields:  {required-set(t)}')
    if t[list(required)].eq('').any().any(): raise ValueError('Required transaction fields are blank')
    t['source_row']=np.arange(len(t))+2
    t['date']=pd.to_datetime(t.CreateDate,format='%m/%d/%Y - %H:%M',errors='raise')
    t['kind']=t.TransactionType.map(cfg['transaction_types'])
    if t.kind.isna().any(): raise ValueError(f'Unknown transaction types:  {t.loc[t.kind.isna(),"TransactionType"].tolist()}')
    t['Quantity']=pd.to_numeric(t.Quantity); t['price_numeric']=t.Price.map(money); t['amount_numeric']=t.Amount.map(money)
    if not np.isfinite(t[['Quantity','price_numeric','amount_numeric']].to_numpy()).all() or (t.Quantity<=0).any() or (t.price_numeric<0).any(): raise ValueError('Invalid transaction values; quantities must be positive, prices nonnegative, and direction determined by type')
    if t.duplicated(subset=list(required),keep=False).any(): issue('warning','similar_trades','Identical-looking transactions are retained; verify unique execution IDs. No automatic deduplication.')
    for _,row in t.iterrows():
        a=cfg['assets'].get(row.Symbol)
        if not a or row.Currency!=a['currency'] or row.Exchange!=a['stocktrak_exchange']: raise ValueError(f'Transaction row {row.source_row}symbol, exchange, or currency mismatch')
        if row.kind=='dividend':
            if not str(row.Amount).startswith('C$'): issue('warning','dividend_currency',f'{row.Symbol}dividend settlement currency needs verification')
            elif abs(row.Quantity*row.price_numeric-row.amount_numeric)>.01: raise ValueError('CAD dividend quantity times per-unit amount does not match the total')
    n=pd.read_csv(raw/'nav.csv'); n['date']=pd.to_datetime(n.Date,format='%m/%d/%Y %I:%M:%S %p'); n['Value']=pd.to_numeric(n.Value)
    if n.date.duplicated().any() or not np.isfinite(n.Value).all() or (n.Value<=0).any(): raise ValueError('NAV contains duplicate dates, missing values, or nonpositive values')
    nav=n.set_index('date').Value.sort_index(); summary={}
    with (raw/'summary.csv').open(encoding='utf-8-sig',newline='') as file:
        for row in csv.reader(file):
            for i in range(0,len(row)-1,2):
                if row[i].strip(): summary[row[i].strip().rstrip(':')]=row[i+1].strip()
    asof=pd.to_datetime(summary['Date'],format='%m/%d/%Y')
    if asof!=pd.Timestamp(cfg['positions_asof']): raise ValueError('Configured position date differs from Summary date')
    if asof<nav.index.max(): raise ValueError('Summary predates the latest historical NAV')
    if asof not in nav.index:
        nav.loc[asof]=money(summary['Portfolio Value'])
        issue('info','nav_append',f'Historical NAV ends on {n.date.max().date()}; appended {asof.date()} Summary valuation; original files unchanged')
    elif abs(nav.loc[asof]-money(summary['Portfolio Value']))>.02: issue('warning','nav_conflict','Same-date NAV differs from Summary; historical NAV is retained and the discrepancy flagged')
    nav=nav.sort_index()
    if t.date.min().normalize()<nav.index.min() or t.date.max().normalize()>asof: raise ValueError('Transaction dates fall outside the supplied NAV period')
    issue('info','timezone',f'Timezone-free dates are interpreted in {cfg["timezone"]}; midnight labels do not establish beginning-of-day valuation')
    cash,loan=money(summary['Cash Balance']),money(summary['Loan Balance'])
    long=money(summary['Market Value of Long Positions']); total=money(summary['Portfolio Value'])
    recon=pd.DataFrame({'CAD':{'Detailed position market value':p.MarketValue.sum(),'Summary long-position market value':long,'Cash':cash,'Loan':loan,'Summary account value':total,
        'Detailed positions + cash - loan - account value':p.MarketValue.sum()+cash-loan-total,'Summary positions + cash - loan - account value':long+cash-loan-total,'Detailed positions - Summary positions':p.MarketValue.sum()-long}})
    csave(recon,out/'reconciliation.csv')
    if abs(p.MarketValue.sum()+cash-loan-total)>.02 or abs(long+cash-loan-total)>.02:
        issue('warning','reconciliation','Account summary discrepancies remain; see reconciliation.csv. No cash adjustments or invented fees. Simulation weights use detailed positions plus cash, not unverified summary totals.')
    if loan!=0 or money(summary['Market Value of Short Positions'])!=0: raise ValueError('Borrowing and short-position risk models are not supported')
    p['weight']=p.MarketValue/(p.MarketValue.sum()+cash)
    p['implied_snapshot_fx']=p.MarketValue/(p.Quantity*p.LastPrice)
    if cfg['external_flows']=='unknown': issue('warning','flows_unknown','External flows are unconfirmed; the account curve shows unadjusted NAV change')
    if not cfg['official_benchmark']: issue('info','benchmark','SPY CAD total return is only an additional unofficial reference with a different allocation')
    clean=ROOT/cfg['clean_dir']; clean.mkdir(parents=True,exist_ok=True)
    csave(p,clean/'positions.csv'); csave(t,clean/'transactions.csv'); csave(nav.to_frame('nav_CAD'),clean/'nav.csv'); jsave(clean/'summary.json',summary)
    return p,t,nav,summary,cash,asof

def fetch_market(cfg,asof,offline,refresh,out):
    import yfinance as yf
    cache=ROOT/cfg['cache_dir']; cache.mkdir(parents=True,exist_ok=True)
    yf.set_tz_cache_location(str(cache/'yfinance_internal'))
    start=asof-pd.DateOffset(years=cfg['history_years']); end=asof+pd.Timedelta(days=1)
    histories,provenance={},[]
    for symbol,a in (cfg['assets']|cfg.get('benchmark_assets',{})).items():
        ticker=a['ticker']; key=f'{ticker}_{start.date()}_{asof.date()}'
        file,meta=cache/f'{key}.csv',cache/f'{key}.json'
        try:
            if (refresh or not file.exists()) and not offline:
                print(f'Yahoo Finance: {ticker}',flush=True)
                obj=yf.Ticker(ticker)
                h=obj.history(start=str(start.date()),end=str(end.date()),auto_adjust=False,back_adjust=False,actions=True,repair=False,keepna=True,timeout=20)
                metadata=obj.get_history_metadata()
                if h.empty: raise ValueError('No price history returned')
                exchange=metadata.get('exchangeName',metadata.get('exchange',''))
                if metadata.get('symbol')!=ticker or metadata.get('currency')!=a['currency'] or exchange not in a['yahoo_exchanges']:
                    raise ValueError(f'Market-data mapping mismatch:currency={metadata.get("currency")},exchange={exchange}')
                h.index=h.index.tz_localize(None).normalize(); csave(h,file)
                info=dict(source=f'https://finance.yahoo.com/quote/{ticker}/history/',ticker=ticker,currency=metadata.get('currency'),exchange=exchange,
                    fetched_at_utc=datetime.now(timezone.utc).isoformat(),requested_start=str(start.date()),requested_end=str(asof.date()),
                    first=str(h.index.min().date()),last=str(h.index.max().date()),rows=len(h),sha256=hashlib.sha256(file.read_bytes()).hexdigest())
                jsave(meta,info)
            if not file.exists() or not meta.exists(): raise ValueError('No cache matches the requested window')
            info=json.loads(meta.read_text(encoding='utf-8'))
            if info['currency']!=a['currency'] or info['exchange'] not in a['yahoo_exchanges']: raise ValueError('Cached currency or exchange mismatch')
            if info['sha256']!=hashlib.sha256(file.read_bytes()).hexdigest(): raise ValueError('Cache checksum mismatch')
            h=pd.read_csv(file,index_col=0,parse_dates=True)
            if 'Adj Close' not in h or h.index.duplicated().any(): raise ValueError('Adjusted prices are missing or dates are duplicated')
            if (h['Adj Close'].dropna()<=0).any(): raise ValueError('Nonpositive adjusted price')
            if h.index.max()>asof or h.index.min()<start: raise ValueError('Price dates fall outside the requested window')
            if h['Adj Close'].isna().any(): issue('warning','missing_price',f'{ticker}missing prices are excluded from the common window, not filled')
            if h.index.min()>start+pd.Timedelta(days=10): issue('warning','short_history',f'{ticker}starts on {h.index.min().date()}; less than the requested three years')
            if (asof-h.index.max()).days>7: issue('warning','stale_market',f'{ticker}latest available date is {h.index.max().date()}; do not present this as current through the valuation date')
            histories[symbol]=h; provenance.append(info)
        except Exception as exc: issue('error','market_download',f'{ticker}: {type(exc).__name__}: {exc}')
    boc={}
    for name in ('FXUSDCAD','TB.CDN.90D.MID'):
        file=cache/f'BOC_{name}_{start.date()}_{asof.date()}.json'
        url=f'https://www.bankofcanada.ca/valet/observations/{name}/json?start_date={(start-pd.Timedelta(days=10)).date()}&end_date={asof.date()}'
        try:
            if (refresh or not file.exists()) and not offline:
                print(f'Bank of Canada: {name}',flush=True)
                response=requests.get(url,timeout=30); response.raise_for_status(); payload=response.json()
                if not payload.get('observations'): raise ValueError('No observations returned')
                jsave(file,dict(source=url,fetched_at_utc=datetime.now(timezone.utc).isoformat(),payload=payload))
            obj=json.loads(file.read_text(encoding='utf-8'))
            if obj['payload'].get('seriesDetail',{}).get(name,{}).get('dateFormat') in ('monthly','quarterly','annual'):
                raise ValueError('Daily data were requested but the source returned a lower-frequency series; rejected')
            data=[(pd.Timestamp(o['d']),float(o[name]['v'])) for o in obj['payload']['observations'] if name in o]
            s=pd.Series(dict(data),name=name).sort_index()
            if s.empty: raise ValueError('Empty observations')
            boc[name]=s; provenance.append({k:v for k,v in obj.items() if k!='payload'}|dict(series=name,first=str(s.index.min().date()),last=str(s.index.max().date()),rows=len(s)))
        except Exception as exc: issue('error','boc_download',f'{name}: {type(exc).__name__}: {exc}')
    jsave(out/'sources.json',provenance)
    return histories,boc,start

def asof_series(source,dates,max_days,label):
    left=pd.DataFrame({'date':pd.DatetimeIndex(dates)}).sort_values('date')
    right=source.rename('value').rename_axis('source_date').reset_index()
    merged=pd.merge_asof(left,right,left_on='date',right_on='source_date',direction='backward')
    age=(merged.date-merged.source_date).dt.days; valid=age.between(0,max_days)
    if (~valid).any(): issue('warning','asof_missing',f'{label}: {(~valid).sum()} dates have no observation within the previous {max_days} days; left unavailable')
    if (valid&age.gt(0)).any(): issue('info','explicit_carry',f'{label}: {(valid&age.gt(0)).sum()} dates explicitly use the latest previously published observation; maximum age {age[valid].max()} days; see alignment_audit.csv')
    merged['label']=label; merged['age_days']=age; merged['accepted']=valid; ALIGNMENTS.append(merged)
    return pd.Series(merged.value.where(valid).to_numpy(),index=pd.DatetimeIndex(merged.date)).reindex(dates)

def cad_prices(cfg,histories,boc):
    prices={}
    assets=cfg['assets']|cfg.get('benchmark_assets',{})
    for s,h in histories.items():
        if assets[s]['currency']=='CAD': prices[s]=h['Adj Close']
        elif 'FXUSDCAD' in boc: prices[s]=h['Adj Close']*asof_series(boc['FXUSDCAD'],h.index,4,f'{s} USD/CAD')
    return prices

def lines(frame,path,title,ylabel):
    fig,ax=plt.subplots(figsize=(10,4.8)); frame.plot(ax=ax,lw=1.6)
    ax.set(title=title,ylabel=ylabel,xlabel='Date'); ax.grid(alpha=.22); ax.yaxis.set_major_formatter(PercentFormatter(1))
    fig.tight_layout(); fig.savefig(path,dpi=150); plt.close(fig)

def actual_analysis(cfg,nav,prices,out):
    file=ROOT/cfg['flows_file']; flows=pd.read_csv(file) if file.exists() else pd.DataFrame()
    r,label=nav_returns(nav,flows,cfg['external_flows']); idx=(1+r.fillna(0)).cumprod()
    daily=pd.DataFrame({'nav_CAD':nav,'daily_return':r,'cumulative_change':idx-1,'drawdown':idx/idx.cummax()-1})
    csave(daily,out/'actual_daily.csv')
    result=dict(label=label,start=str(nav.index.min().date()),end=str(nav.index.max().date()),calendar_observations=len(nav),
                cumulative_change=float(idx.iloc[-1]-1),max_drawdown=mdd(idx),annual_volatility=None,sharpe=None)
    issue('info','actual_sampling',f'Retained all {len(nav)} calendar-day valuations including weekends. Full-period returns/drawdowns use actual NAV. Competition Sharpe is a separate common-market-day short-sample estimate; calendar-day returns are not annualized using 252.')
    if nav.index.to_series().diff().dt.days.dropna().gt(1).any(): issue('warning','nav_gap','NAV dates have gaps; daily_return at a gap is an interval return, not a one-day return')
    benchmark=cfg['comparison_benchmark']
    if not cfg.get('benchmark') and benchmark in prices:
        b=prices[benchmark].dropna(); dates=nav.index.intersection(b.index)
        if len(dates)>=2:
            compare=pd.DataFrame({'account':idx.reindex(dates),'benchmark':b.reindex(dates)})
            compare=compare/compare.iloc[0]-1; csave(compare,out/'actual_benchmark_common_dates.csv')
            lines(compare.rename(columns={'account':'Account NAV change','benchmark':benchmark+' CAD total return'}),out/'actual_benchmark.png',f'Actual account vs. {benchmark} CAD total return ('+('official' if cfg['official_benchmark'] else 'additional reference')+')\nCommon quote dates, rebased start; see cash-flow status in report','Cumulative change')
            result['comparison']=dict(start=str(dates.min().date()),end=str(dates.max().date()),account=float(compare.account.iloc[-1]),benchmark=float(compare.benchmark.iloc[-1]))
        else: issue('error','benchmark_overlap','Benchmark and account have fewer than two common dates; other periods are not spliced in')
    elif not cfg.get('benchmark'): issue('error','benchmark_missing','Benchmark CAD data unavailable; comparison chart shows the account only')
    if not (out/'actual_benchmark.png').exists(): lines((idx-1).to_frame(label),out/'actual_benchmark.png','Actual account NAV change; benchmark unavailable','Cumulative change')
    lines(daily[['drawdown']],out/'actual_drawdown.png','Actual account drawdown (calendar-day NAV; see cash-flow status)','Drawdown')
    jsave(out/'actual_summary.json',result)
    return result

def pnl_analysis(cfg,p,t,out):
    book=p.copy()
    for s in sorted(set(t.Symbol)-set(book.index)):
        book.loc[s,'Currency']=cfg['assets'][s]['currency']
        book.loc[s,['Quantity','LastPrice','implied_snapshot_fx']]=[0.,0.,1.]
    settlements_file=ROOT/cfg['settlements_file']
    settlements=None
    if settlements_file.exists():
        settlements=pd.read_csv(settlements_file)
        req={'transactions_sha256','source_row','fx_CAD_per_local','fee_raw','fee_currency','fee_cash_effect_CAD','source','note'}
        if not req<=set(settlements) or settlements.source_row.duplicated().any(): raise ValueError('Settlement table has missing fields or duplicate source_row values')
        digest=hashlib.sha256((ROOT/cfg['input_dir']/'transactions.csv').read_bytes()).hexdigest()
        if len(settlements) and (not settlements.transactions_sha256.eq(digest).all() or settlements[list(req)].isna().any().any()): raise ValueError('Settlement input transaction checksum mismatch or missing fields')
        for col in ['fx_CAD_per_local','fee_raw','fee_cash_effect_CAD']: settlements[col]=pd.to_numeric(settlements[col])
        if (settlements.fx_CAD_per_local<=0).any() or not settlements.source_row.isin(t.source_row).all(): raise ValueError('Nonpositive settlement FX or unknown transaction row')
        csave(settlements,out/'verified_settlements.csv')
    rows=[]
    for s,pos in book.iterrows():
        trades=t[t.Symbol.eq(s)]
        try:
            realized,unrealized,cost=fifo(trades,pos.Quantity,pos.LastPrice); status='FIFO gross P/L; fees unverified'
        except ValueError as exc:
            realized=unrealized=cost=np.nan; status=str(exc); issue('error','quantity_reconciliation',f'{s}: {exc}')
        div=trades[trades.kind.eq('dividend')]
        cad_div=div.amount_numeric.sum() if div.Amount.astype(str).str.startswith('C$').all() else np.nan
        fx=pos.implied_snapshot_fx
        rows.append(dict(symbol=s,currency=pos.Currency,quantity=pos.Quantity,cost_local=cost,realized_gross_local=realized,
            unrealized_gross_local=unrealized,price_effect_at_snapshot_fx_CAD=unrealized*fx,
            realized_CAD=realized if pos.Currency=='CAD' else (0. if realized==0 else np.nan),
            unrealized_total_CAD=unrealized if pos.Currency=='CAD' else np.nan,dividends_confirmed_CAD=cad_div,
            fees_CAD=np.nan,fx_effect_CAD=np.nan,net_contribution_CAD=np.nan,implied_snapshot_fx=fx,status=status))
    frame=pd.DataFrame(rows).set_index('symbol'); csave(frame,out/'asset_pnl.csv')
    if settlements is not None and len(settlements):
        for s,pos in book.iterrows():
            trades=t[t.Symbol.eq(s)&t.kind.isin(['buy','sell'])]
            if not trades.source_row.isin(settlements.source_row).all():
                issue('warning','settlement_incomplete',f'{s}settlement inputs incomplete; net CAD P/L unavailable'); continue
            merged=trades.merge(settlements,on='source_row',how='left',validate='one_to_one')
            if pos.Currency=='CAD' and not np.isclose(merged.fx_CAD_per_local,1).all(): raise ValueError('CAD trade settlement FX must equal 1')
            realized,unrealized,pe,fe=fifo_cad(merged,pos.Quantity,pos.LastPrice,pos.implied_snapshot_fx)
            frame.loc[s,'realized_CAD']=realized; frame.loc[s,'unrealized_total_CAD']=unrealized
            frame.loc[s,'price_effect_verified_CAD']=pe; frame.loc[s,'fx_effect_CAD']=fe
            frame.loc[s,'fees_CAD']=merged.fee_cash_effect_CAD.sum()
            frame.loc[s,'net_contribution_CAD']=realized+unrealized+frame.loc[s,'dividends_confirmed_CAD']+merged.fee_cash_effect_CAD.sum()
            frame.loc[s,'status']='FIFO using verified settlement inputs; fees booked separately as cash effects; snapshot market value in CAD'
        csave(frame,out/'asset_pnl.csv')
    if frame.net_contribution_CAD.isna().any():
        issue('warning','pnl_currency','For unverified settlement, full CAD P/L, FX attribution and net contribution are unavailable. Local-currency gross P/L and snapshot-FX price effects are diagnostics, not total CAD P/L.')
        issue('warning','fees_missing','No separate fee field; do not assume zero fees or infer them from Amount differences. Fees already in NAV are not deducted again.')
    ledger_file=ROOT/cfg['ledger_file']
    if ledger_file.exists():
        ledger=pd.read_csv(ledger_file)
        req={'event_id','date','symbol','kind','amount_raw','currency','cad_cash_effect','included_in_nav','source','note'}
        if not req<=set(ledger) or ledger.event_id.duplicated().any(): raise ValueError('Ledger has missing fields or duplicate event IDs')
        if len(ledger):
            if not ledger.currency.eq('CAD').all() or not ledger.kind.isin(['fee','interest','dividend','tax','fx_adjustment']).all(): raise ValueError('Ledger requires verified CAD cash events')
            ledger['amount_raw']=pd.to_numeric(ledger.amount_raw); ledger['cad_cash_effect']=pd.to_numeric(ledger.cad_cash_effect)
            if ledger[['source','note','included_in_nav']].isna().any().any(): raise ValueError('Ledger lacks source, sign explanation, or included-in-NAV flag')
            csave(ledger,out/'verified_ledger.csv'); issue('info','ledger','Verified cash events are listed separately without changing NAV or duplicating transaction dividends')
    return frame

def concentration(cfg,p,cash,out):
    weights=pd.concat([p.weight,pd.Series({'CAD cash':cash/(p.MarketValue.sum()+cash)})])
    for field in ['asset_class','region','sector','currency']:
        labels=pd.Series({s:cfg['assets'][s].get(field,'Unknown') for s in p.index})
        group=p.weight.groupby(labels).sum(); group.loc['CAD cash' if field=='currency' else 'Cash']=weights['CAD cash']
        csave(group.to_frame('weight'),out/f'concentration_{field}.csv')
    fig,ax=plt.subplots(figsize=(9,4.5)); weights.sort_values().plot.barh(ax=ax,color='#3876a8')
    ax.set(title='Identifiable asset weights (detailed positions + cash)',xlabel='Weight'); ax.xaxis.set_major_formatter(PercentFormatter(1)); fig.tight_layout(); fig.savefig(out/'weights.png',dpi=150); plt.close(fig)
    return weights

def risk_analysis(cfg,p,weights,prices,boc,start,asof,out):
    if not set(p.index)<=set(prices):
        issue('error','risk_incomplete',f'Missing CAD history for {sorted(set(p.index)-set(prices))}; do not drop assets and renormalize as a full portfolio')
        return None
    panel=pd.concat({s:prices[s] for s in p.index},axis=1).sort_index(); common=panel.dropna()
    csave(panel.loc[~panel.index.isin(common.index)],out/'excluded_market_dates.csv')
    issue('info','common_dates',f'Common valid dates: {len(common)}; excluded {len(panel)-len(common)} incomplete dates. Security prices are not filled. All asset metrics use the same window; returns across holiday gaps span retained observations, so 252 annualization is approximate.')
    if len(common)<60: issue('error','risk_short',f'Only {len(common)} price dates; fewer than 60, so risk statistics are unavailable'); return None
    if common.index.min()>start+pd.Timedelta(days=10) or (asof-common.index.max()).days>7: issue('warning','risk_window',f'Actual common window: {common.index.min().date()}—{common.index.max().date()}; not a complete three-year window through the current valuation')
    r=common.pct_change(fill_method=None).iloc[1:]; rf=None
    if 'TB.CDN.90D.MID' in boc:
        yields=asof_series(boc['TB.CDN.90D.MID'],common.index[:-1]-pd.Timedelta(days=1),7,'CAD T-bill yield published before the return interval')
        gaps=np.diff(common.index.values)/np.timedelta64(1,'D')
        rf=pd.Series((1+yields.to_numpy()/100)**(gaps/365)-1,index=r.index)
    if rf is None or rf.isna().any(): issue('warning','rf_missing','Risk-free data incomplete; Sharpe unavailable rather than assuming a zero rate')
    portfolio=r.mul(p.weight,axis=1).sum(axis=1)
    stats=pd.DataFrame({s:metrics(r[s],rf) for s in r}|{'Hypothetical portfolio':metrics(portfolio,rf)}).T
    stats['start']=str(common.index.min().date()); stats['end']=str(common.index.max().date())
    csave(stats,out/'historical_risk.csv'); csave(common,out/'historical_CAD_total_return_prices.csv'); csave(r,out/'historical_returns.csv')
    if rf is not None: csave(rf.to_frame('risk_free_interval_return'),out/'risk_free.csv')
    cov=r.cov()*252; w=p.weight.reindex(cov.index).to_numpy(); sigma=np.sqrt(float(w@cov.to_numpy()@w))
    rc=w*(cov.to_numpy()@w)/sigma if sigma>0 else w*np.nan
    risk=pd.DataFrame({'weight':p.weight,'volatility_contribution':rc,'risk_share':rc/sigma if sigma>0 else np.nan})
    risk.loc['CAD cash']=[weights['CAD cash'],0,0]; csave(risk,out/'risk_contribution.csv')
    if not np.isclose(risk.volatility_contribution.sum(),sigma): raise ValueError('Risk contributions do not sum to portfolio volatility')
    corr=r.corr(); csave(corr,out/'correlation.csv')
    fig,ax=plt.subplots(figsize=(8,7)); im=ax.imshow(corr,vmin=-1,vmax=1,cmap='RdBu_r')
    ax.set_xticks(range(len(corr)),corr.columns,rotation=45,ha='right'); ax.set_yticks(range(len(corr)),corr.index)
    for i in range(len(corr)):
        for j in range(len(corr)): ax.text(j,i,f'{corr.iloc[i,j]:.2f}',ha='center',va='center',fontsize=8)
    ax.set_title('CAD daily total-return correlations (common window)'); fig.colorbar(im,ax=ax); fig.tight_layout(); fig.savefig(out/'correlation.png',dpi=150); plt.close(fig)
    fig,ax=plt.subplots(figsize=(9,4.5)); risk[['weight','risk_share']].rename(columns={'weight':'Asset weight','risk_share':'Risk share'}).plot.bar(ax=ax)
    ax.set(title='Current weights and historical volatility risk contributions (may be negative)',ylabel='Share',xlabel='Asset'); ax.yaxis.set_major_formatter(PercentFormatter(1)); fig.tight_layout(); fig.savefig(out/'risk_contribution.png',dpi=150); plt.close(fig)
    sim=pd.concat([pd.Series([1.],index=common.index[:1]),(1+portfolio).cumprod()])
    lines((sim-1).to_frame('Hypothetical portfolio'),out/'simulated_return.png','Hypothetical history of current holdings\nDaily fixed-weight rebalancing; zero CAD cash return; no trading costs','Cumulative return')
    lines((sim/sim.cummax()-1).to_frame('Hypothetical portfolio'),out/'simulated_drawdown.png','Hypothetical historical simulation: drawdown','Drawdown')
    issue('info','simulation','Daily rebalancing to current weights; zero CAD cash return; no fees, tax or impact. Adjusted total returns already reinvest distributions. Not actual competition returns or a forecast.')
    return stats,risk

def unavailable(path,title):
    if path.exists(): return
    fig,ax=plt.subplots(figsize=(9,4)); ax.axis('off'); ax.text(.5,.6,title,ha='center',fontsize=17)
    ax.text(.5,.35,'Data unavailable; see missing inputs and failures in the report.\nNo fabricated data were used.',ha='center'); fig.tight_layout(); fig.savefig(path,dpi=120); plt.close(fig)

def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--config',default='config.json')
    parser.add_argument('--offline',action='store_true'); parser.add_argument('--refresh',action='store_true'); parser.add_argument('--validate-only',action='store_true')
    args=parser.parse_args(); cfg=json.loads((ROOT/args.config).read_text(encoding='utf-8-sig'))
    out=ROOT/cfg['output_dir']/datetime.now().strftime('%Y%m%d_%H%M%S_%f'); out.mkdir(parents=True,exist_ok=False); jsave(out/'run_config.json',cfg)
    try:
        ISSUES.clear(); ALIGNMENTS.clear()
        p,t,nav,summary,cash,asof=load_inputs(cfg,out); pnl=pnl_analysis(cfg,p,t,out); weights=concentration(cfg,p,cash,out)
        histories,boc,start=({}, {},asof-pd.DateOffset(years=cfg['history_years'])) if args.validate_only else fetch_market(cfg,asof,args.offline,args.refresh,out)
        prices=cad_prices(cfg,histories,boc); actual=actual_analysis(cfg,nav,prices,out); risk=risk_analysis(cfg,p,weights,prices,boc,start,asof,out)
        context=sys.modules[__name__]
        estimate=enrichment.accounting_estimate(context,cfg,p,t,nav,summary,cash,boc,out)
        # Superseded broad warnings are replaced by specific remaining limitations below.
        solved={'pnl_currency','fees_missing','lookthrough','etf_overlap'}
        ISSUES[:]=[x for x in ISSUES if x['code'] not in solved]
        coverage=enrichment.official_holdings(context,cfg,p,cash,out,args.offline,args.refresh)
        try:
            actual['comparison']=enrichment.benchmark(context,cfg,prices,nav,boc,out)
        except Exception as exc: issue('error','rpmc_benchmark',str(exc))
        jsave(out/'actual_summary.json',actual)
        enrichment.render_report(context,cfg,p,weights,actual,estimate,risk,coverage,out)
        from export_report import export_run
        portable,bundle=export_run(out)
        (ROOT/cfg['output_dir']/'LATEST.txt').write_text(str(out),encoding='utf-8')
        print(f'Standalone English report: {portable}\nDownload bundle: {bundle}')
        print(f'Report: {out/"report.html"}'); print(f'Unavailable items: {sum(x["level"]=="error" for x in ISSUES)}; outstanding checks: {sum(x["level"]=="warning" for x in ISSUES)}')
        return 0
    except Exception as exc:
        issue('fatal','input_or_calculation',f'{type(exc).__name__}: {exc}'); jsave(out/'failure.json',ISSUES)
        print(f'Stopped: {exc}; see {out/"failure.json"}',file=sys.stderr); return 2

if __name__=='__main__': sys.exit(main())
