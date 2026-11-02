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
plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans']
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
    if len(r)<2: raise ValueError('至少需要两个收益观测')
    wealth=pd.concat([pd.Series([1.],index=[r.index[0]-pd.Timedelta(days=1)]),(1+r).cumprod()])
    sharpe=np.nan
    if rf is not None and rf.reindex(r.index).notna().all():
        excess=r-rf.reindex(r.index)
        if excess.std(ddof=1)>1e-14: sharpe=excess.mean()/excess.std(ddof=1)*np.sqrt(252)
    return dict(observations=len(r),cumulative_return=float((1+r).prod()-1),
                annual_volatility=float(r.std(ddof=1)*np.sqrt(252)),max_drawdown=mdd(wealth),sharpe=sharpe)

def nav_returns(nav, flows, status):
    """Only confirmed end-of-day external flows are supported."""
    if status=='unknown': return nav.pct_change(fill_method=None),'未调整资金流的净值变化（出入金待确认）'
    if status not in ('none','provided'): raise ValueError('external_flows 应为 unknown/none/provided')
    cash=pd.Series(0.,index=nav.index)
    if status=='provided':
        if flows.empty or not flows.timing.eq('end_of_day').all():
            raise ValueError('需要已确认日末资金流；日内资金流需要流入前后估值')
        if not flows.currency.eq('CAD').all(): raise ValueError('外部资金流必须提供实际CAD结算金额')
        f=flows.copy(); f['date']=pd.to_datetime(f.date,format='%Y-%m-%d')
        if not f.date.isin(nav.index).all() or f.date.min()<=nav.index.min():
            raise ValueError('资金流必须位于两个估值之间且有对应日期估值')
        cash=f.groupby('date').amount.sum().reindex(nav.index,fill_value=0.)
    r=(nav-cash)/nav.shift(1)-1
    if (r.dropna()<=-1).any(): raise ValueError('资金流调整后收益<=-100%，请检查金额和时点')
    return r,'日末外部资金流调整的时间加权收益' if status=='provided' else '真实净值收益（已确认无外部出入金）'

def fifo(trades, quantity, last):
    lots,realized=[],0.
    for _,t in trades.sort_values(['date','source_row'],kind='stable').iterrows():
        q,p=float(t.Quantity),float(t.price_numeric)
        if t.kind=='buy': lots.append([q,p])
        elif t.kind=='sell':
            while q>1e-9:
                if not lots: raise ValueError('卖出超过已知持仓；缺少期初持仓或不支持做空')
                use=min(q,lots[0][0]); realized+=use*(p-lots[0][1]); q-=use; lots[0][0]-=use
                if lots[0][0]<1e-9: lots.pop(0)
    derived=sum(q for q,p in lots)
    if abs(derived-quantity)>1e-7: raise ValueError(f'交易推导数量{derived}，当前数量{quantity}')
    return realized,sum(q*(last-p) for q,p in lots),sum(q*p for q,p in lots)

def fifo_cad(trades, quantity, last, final_fx):
    """Verified CAD lots. FX cross-term belongs to FX effect; fees are separate."""
    lots=[]; realized=price_effect=fx_effect=0.
    for _,t in trades.sort_values(['date','source_row'],kind='stable').iterrows():
        q,p,f=float(t.Quantity),float(t.price_numeric),float(t.fx_CAD_per_local)
        if t.kind=='buy': lots.append([q,p,f])
        elif t.kind=='sell':
            while q>1e-9:
                if not lots: raise ValueError('CAD FIFO卖出超过持仓')
                use=min(q,lots[0][0]); _,cost,entry_fx=lots[0]
                realized+=use*(p*f-cost*entry_fx)
                price_effect+=use*(p-cost)*entry_fx
                fx_effect+=use*p*(f-entry_fx)
                q-=use; lots[0][0]-=use
                if lots[0][0]<1e-9: lots.pop(0)
    if abs(sum(q for q,p,f in lots)-quantity)>1e-7: raise ValueError('CAD FIFO数量不符')
    unrealized=0.
    for q,cost,entry_fx in lots:
        unrealized+=q*(last*final_fx-cost*entry_fx)
        price_effect+=q*(last-cost)*entry_fx
        fx_effect+=q*last*(final_fx-entry_fx)
    if not np.isclose(price_effect+fx_effect,realized+unrealized): raise ValueError('价格/汇率拆分未对平')
    return realized,unrealized,price_effect,fx_effect

def load_inputs(cfg,out):
    raw=ROOT/cfg['input_dir']
    manifest=[]
    for name in ('positions.csv','transactions.csv','nav.csv','summary.csv'):
        file=raw/name
        if not file.exists(): raise ValueError(f'缺少 {file}')
        manifest.append(dict(file=name,sha256=hashlib.sha256(file.read_bytes()).hexdigest(),bytes=file.stat().st_size))
    jsave(out/'input_manifest.json',manifest)
    p=pd.read_csv(raw/'positions.csv')
    required={'Symbol','Quantity','Currency','LastPrice','PricePaid','MarketValue'}
    if not required<=set(p): raise ValueError(f'持仓缺字段 {required-set(p)}')
    if p[list(required)].isna().any().any() or p.Symbol.duplicated().any(): raise ValueError('持仓有缺值或重复代码')
    for col in ['Quantity','LastPrice','PricePaid','MarketValue']: p[col]=p[col].map(money)
    if not np.isfinite(p[['Quantity','LastPrice','PricePaid','MarketValue']].to_numpy()).all() or (p[['Quantity','LastPrice','PricePaid','MarketValue']]<=0).any().any(): raise ValueError('第一版仅支持有限正数量正价格的多头')
    p=p.set_index('Symbol')
    for s,row in p.iterrows():
        if s not in cfg['assets'] or row.Currency!=cfg['assets'][s]['currency']: raise ValueError(f'{s} 缺映射或币种不符')
        if row.Currency=='CAD' and abs(row.Quantity*row.LastPrice-row.MarketValue)>.02: raise ValueError(f'{s} CAD市值不符')
    t=pd.read_csv(raw/'transactions.csv',keep_default_na=False)
    required={'Symbol','TransactionType','Exchange','Quantity','Currency','Price','Amount','CreateDate'}
    if not required<=set(t): raise ValueError(f'交易缺字段 {required-set(t)}')
    if t[list(required)].eq('').any().any(): raise ValueError('交易必要字段为空')
    t['source_row']=np.arange(len(t))+2
    t['date']=pd.to_datetime(t.CreateDate,format='%m/%d/%Y - %H:%M',errors='raise')
    t['kind']=t.TransactionType.map(cfg['transaction_types'])
    if t.kind.isna().any(): raise ValueError(f'未知交易类型 {t.loc[t.kind.isna(),"TransactionType"].tolist()}')
    t['Quantity']=pd.to_numeric(t.Quantity); t['price_numeric']=t.Price.map(money); t['amount_numeric']=t.Amount.map(money)
    if not np.isfinite(t[['Quantity','price_numeric','amount_numeric']].to_numpy()).all() or (t.Quantity<=0).any() or (t.price_numeric<0).any(): raise ValueError('交易数值非法；数量为正，价格非负，方向由类型指定')
    if t.duplicated(subset=list(required),keep=False).any(): issue('warning','similar_trades','相同内容交易全部保留，请用唯一成交ID核实；未自动删除')
    for _,row in t.iterrows():
        a=cfg['assets'].get(row.Symbol)
        if not a or row.Currency!=a['currency'] or row.Exchange!=a['stocktrak_exchange']: raise ValueError(f'交易行{row.source_row}代码/交易所/币种不符')
        if row.kind=='dividend':
            if not str(row.Amount).startswith('C$'): issue('warning','dividend_currency',f'{row.Symbol}分红结算币种待核实')
            elif abs(row.Quantity*row.price_numeric-row.amount_numeric)>.01: raise ValueError('CAD分红数量×每份金额不符')
    n=pd.read_csv(raw/'nav.csv'); n['date']=pd.to_datetime(n.Date,format='%m/%d/%Y %I:%M:%S %p'); n['Value']=pd.to_numeric(n.Value)
    if n.date.duplicated().any() or not np.isfinite(n.Value).all() or (n.Value<=0).any(): raise ValueError('净值日期重复/缺值/非正数')
    nav=n.set_index('date').Value.sort_index(); summary={}
    with (raw/'summary.csv').open(encoding='utf-8-sig',newline='') as file:
        for row in csv.reader(file):
            for i in range(0,len(row)-1,2):
                if row[i].strip(): summary[row[i].strip().rstrip(':')]=row[i+1].strip()
    asof=pd.to_datetime(summary['Date'],format='%m/%d/%Y')
    if asof!=pd.Timestamp(cfg['positions_asof']): raise ValueError('持仓日期配置与Summary不同')
    if asof<nav.index.max(): raise ValueError('Summary早于历史净值')
    if asof not in nav.index:
        nav.loc[asof]=money(summary['Portfolio Value'])
        issue('info','nav_append',f'历史净值止于{n.date.max().date()}，追加{asof.date()} Summary估值，原文件不变')
    elif abs(nav.loc[asof]-money(summary['Portfolio Value']))>.02: issue('warning','nav_conflict','同日净值与Summary不一致，保留历史净值并另行核对')
    nav=nav.sort_index()
    if t.date.min().normalize()<nav.index.min() or t.date.max().normalize()>asof: raise ValueError('交易日期超出提供的比赛净值窗口')
    issue('info','timezone',f'无时区标签的日期按配置{cfg["timezone"]}解释；午夜标签不认定为日初估值')
    cash,loan=money(summary['Cash Balance']),money(summary['Loan Balance'])
    long=money(summary['Market Value of Long Positions']); total=money(summary['Portfolio Value'])
    recon=pd.DataFrame({'CAD':{'持仓明细市值':p.MarketValue.sum(),'Summary多头市值':long,'现金':cash,'借款':loan,'Summary净值':total,
        '明细持仓+现金-借款-净值':p.MarketValue.sum()+cash-loan-total,'Summary持仓+现金-借款-净值':long+cash-loan-total,'明细持仓-Summary持仓':p.MarketValue.sum()-long}})
    csave(recon,out/'reconciliation.csv')
    if abs(p.MarketValue.sum()+cash-loan-total)>.02 or abs(long+cash-loan-total)>.02:
        issue('warning','reconciliation','账户未对平，差异见reconciliation.csv；不修改现金或虚构费用。持仓模拟以明细持仓+现金作分母，不当作已核实净值权重/真实贡献')
    if loan!=0 or money(summary['Market Value of Short Positions'])!=0: raise ValueError('第一版不支持借款/空头风险模型')
    p['weight']=p.MarketValue/(p.MarketValue.sum()+cash)
    p['implied_snapshot_fx']=p.MarketValue/(p.Quantity*p.LastPrice)
    if cfg['external_flows']=='unknown': issue('warning','flows_unknown','外部出入金尚未确认，账户曲线是未调整资金流净值变化')
    if not cfg['official_benchmark']: issue('info','benchmark','SPY CAD总回报仅为非官方参考，配置与本组合不同')
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
                if h.empty: raise ValueError('行情为空')
                exchange=metadata.get('exchangeName',metadata.get('exchange',''))
                if metadata.get('symbol')!=ticker or metadata.get('currency')!=a['currency'] or exchange not in a['yahoo_exchanges']:
                    raise ValueError(f'行情映射不符:currency={metadata.get("currency")},exchange={exchange}')
                h.index=h.index.tz_localize(None).normalize(); csave(h,file)
                info=dict(source=f'https://finance.yahoo.com/quote/{ticker}/history/',ticker=ticker,currency=metadata.get('currency'),exchange=exchange,
                    fetched_at_utc=datetime.now(timezone.utc).isoformat(),requested_start=str(start.date()),requested_end=str(asof.date()),
                    first=str(h.index.min().date()),last=str(h.index.max().date()),rows=len(h),sha256=hashlib.sha256(file.read_bytes()).hexdigest())
                jsave(meta,info)
            if not file.exists() or not meta.exists(): raise ValueError('没有匹配窗口的缓存')
            info=json.loads(meta.read_text(encoding='utf-8'))
            if info['currency']!=a['currency'] or info['exchange'] not in a['yahoo_exchanges']: raise ValueError('缓存币种/交易所不符')
            if info['sha256']!=hashlib.sha256(file.read_bytes()).hexdigest(): raise ValueError('缓存校验和不符')
            h=pd.read_csv(file,index_col=0,parse_dates=True)
            if 'Adj Close' not in h or h.index.duplicated().any(): raise ValueError('缺少复权价格/日期重复')
            if (h['Adj Close'].dropna()<=0).any(): raise ValueError('非正复权价格')
            if h.index.max()>asof or h.index.min()<start: raise ValueError('行情超出请求日期窗口')
            if h['Adj Close'].isna().any(): issue('warning','missing_price',f'{ticker}空价格在共同窗口排除，不填充')
            if h.index.min()>start+pd.Timedelta(days=10): issue('warning','short_history',f'{ticker}起始{h.index.min().date()}，不足三年')
            if (asof-h.index.max()).days>7: issue('warning','stale_market',f'{ticker}最新仅{h.index.max().date()}；不能冒充截至估值日数据')
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
                if not payload.get('observations'): raise ValueError('没有观测')
                jsave(file,dict(source=url,fetched_at_utc=datetime.now(timezone.utc).isoformat(),payload=payload))
            obj=json.loads(file.read_text(encoding='utf-8'))
            if obj['payload'].get('seriesDetail',{}).get(name,{}).get('dateFormat') in ('monthly','quarterly','annual'):
                raise ValueError('请求日度数据但数据源返回低频序列，拒绝使用')
            data=[(pd.Timestamp(o['d']),float(o[name]['v'])) for o in obj['payload']['observations'] if name in o]
            s=pd.Series(dict(data),name=name).sort_index()
            if s.empty: raise ValueError('观测为空')
            boc[name]=s; provenance.append({k:v for k,v in obj.items() if k!='payload'}|dict(series=name,first=str(s.index.min().date()),last=str(s.index.max().date()),rows=len(s)))
        except Exception as exc: issue('error','boc_download',f'{name}: {type(exc).__name__}: {exc}')
    jsave(out/'sources.json',provenance)
    return histories,boc,start

def asof_series(source,dates,max_days,label):
    left=pd.DataFrame({'date':pd.DatetimeIndex(dates)}).sort_values('date')
    right=source.rename('value').rename_axis('source_date').reset_index()
    merged=pd.merge_asof(left,right,left_on='date',right_on='source_date',direction='backward')
    age=(merged.date-merged.source_date).dt.days; valid=age.between(0,max_days)
    if (~valid).any(): issue('warning','asof_missing',f'{label}: {(~valid).sum()}日期无{max_days}日内既往观测，保留NA')
    if (valid&age.gt(0)).any(): issue('info','explicit_carry',f'{label}: {(valid&age.gt(0)).sum()}日期显式使用最近既往发布值，最长{age[valid].max()}天；见alignment_audit.csv')
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
    ax.set(title=title,ylabel=ylabel,xlabel='日期'); ax.grid(alpha=.22); ax.yaxis.set_major_formatter(PercentFormatter(1))
    fig.tight_layout(); fig.savefig(path,dpi=150); plt.close(fig)

def actual_analysis(cfg,nav,prices,out):
    file=ROOT/cfg['flows_file']; flows=pd.read_csv(file) if file.exists() else pd.DataFrame()
    r,label=nav_returns(nav,flows,cfg['external_flows']); idx=(1+r.fillna(0)).cumprod()
    daily=pd.DataFrame({'nav_CAD':nav,'daily_return':r,'cumulative_change':idx-1,'drawdown':idx/idx.cummax()-1})
    csave(daily,out/'actual_daily.csv')
    result=dict(label=label,start=str(nav.index.min().date()),end=str(nav.index.max().date()),calendar_observations=len(nav),
                cumulative_change=float(idx.iloc[-1]-1),max_drawdown=mdd(idx),annual_volatility=None,sharpe=None)
    issue('info','actual_sampling',f'保留全部{len(nav)}个自然日估值含周末；全期累计收益与回撤以实际净值计算。比赛Sharpe另外使用共同市场日估值，报告短样本估计，不对自然日收益套252')
    if nav.index.to_series().diff().dt.days.dropna().gt(1).any(): issue('warning','nav_gap','净值缺日，缺口处daily_return为跨期收益，不是单日收益')
    benchmark=cfg['comparison_benchmark']
    if not cfg.get('benchmark') and benchmark in prices:
        b=prices[benchmark].dropna(); dates=nav.index.intersection(b.index)
        if len(dates)>=2:
            compare=pd.DataFrame({'account':idx.reindex(dates),'benchmark':b.reindex(dates)})
            compare=compare/compare.iloc[0]-1; csave(compare,out/'actual_benchmark_common_dates.csv')
            lines(compare.rename(columns={'account':'账户净值变化','benchmark':benchmark+' CAD总回报'}),out/'actual_benchmark.png',f'真实账户与{benchmark} CAD总回报（'+('官方' if cfg['official_benchmark'] else '非官方参考')+'）\n共同报价日、起点归一；资金流状态见报告','累计变化')
            result['comparison']=dict(start=str(dates.min().date()),end=str(dates.max().date()),account=float(compare.account.iloc[-1]),benchmark=float(compare.benchmark.iloc[-1]))
        else: issue('error','benchmark_overlap','基准与比赛没有至少两个共同日期；不拼接其他时期价格')
    elif not cfg.get('benchmark'): issue('error','benchmark_missing','基准CAD数据不可用，真实对比图只展示账户')
    if not (out/'actual_benchmark.png').exists(): lines((idx-1).to_frame(label),out/'actual_benchmark.png','真实账户净值变化；基准暂不可用','累计变化')
    lines(daily[['drawdown']],out/'actual_drawdown.png','真实账户回撤（自然日；资金流状态见报告）','回撤')
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
        if not req<=set(settlements) or settlements.source_row.duplicated().any(): raise ValueError('结算表缺字段或source_row重复')
        digest=hashlib.sha256((ROOT/cfg['input_dir']/'transactions.csv').read_bytes()).hexdigest()
        if len(settlements) and (not settlements.transactions_sha256.eq(digest).all() or settlements[list(req)].isna().any().any()): raise ValueError('结算表对应的交易文件哈希不符或有缺值')
        for col in ['fx_CAD_per_local','fee_raw','fee_cash_effect_CAD']: settlements[col]=pd.to_numeric(settlements[col])
        if (settlements.fx_CAD_per_local<=0).any() or not settlements.source_row.isin(t.source_row).all(): raise ValueError('结算汇率非正或交易行未知')
        csave(settlements,out/'verified_settlements.csv')
    rows=[]
    for s,pos in book.iterrows():
        trades=t[t.Symbol.eq(s)]
        try:
            realized,unrealized,cost=fifo(trades,pos.Quantity,pos.LastPrice); status='FIFO毛盈亏，费用待核实'
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
                issue('warning','settlement_incomplete',f'{s}结算不完整，CAD净盈亏留空'); continue
            merged=trades.merge(settlements,on='source_row',how='left',validate='one_to_one')
            if pos.Currency=='CAD' and not np.isclose(merged.fx_CAD_per_local,1).all(): raise ValueError('CAD成交结算汇率必须为1')
            realized,unrealized,pe,fe=fifo_cad(merged,pos.Quantity,pos.LastPrice,pos.implied_snapshot_fx)
            frame.loc[s,'realized_CAD']=realized; frame.loc[s,'unrealized_total_CAD']=unrealized
            frame.loc[s,'price_effect_verified_CAD']=pe; frame.loc[s,'fx_effect_CAD']=fe
            frame.loc[s,'fees_CAD']=merged.fee_cash_effect_CAD.sum()
            frame.loc[s,'net_contribution_CAD']=realized+unrealized+frame.loc[s,'dividends_confirmed_CAD']+merged.fee_cash_effect_CAD.sum()
            frame.loc[s,'status']='已核实结算FIFO；费用独立现金记账；快照市值为CAD'
        csave(frame,out/'asset_pnl.csv')
    if frame.net_contribution_CAD.isna().any():
        issue('warning','pnl_currency','未确认结算的资产完整CAD盈亏/汇率效应/净贡献留空。本币毛盈亏与快照汇率折算价格效应仅辅助对账，不是CAD总盈亏')
        issue('warning','fees_missing','交易表无独立费用字段，不假设费用为0，不用Amount差值反推费用；净值已含费用不再扣')
    ledger_file=ROOT/cfg['ledger_file']
    if ledger_file.exists():
        ledger=pd.read_csv(ledger_file)
        req={'event_id','date','symbol','kind','amount_raw','currency','cad_cash_effect','included_in_nav','source','note'}
        if not req<=set(ledger) or ledger.event_id.duplicated().any(): raise ValueError('ledger缺字段/事件ID重复')
        if len(ledger):
            if not ledger.currency.eq('CAD').all() or not ledger.kind.isin(['fee','interest','dividend','tax','fx_adjustment']).all(): raise ValueError('ledger需要已核实CAD现金事件')
            ledger['amount_raw']=pd.to_numeric(ledger.amount_raw); ledger['cad_cash_effect']=pd.to_numeric(ledger.cad_cash_effect)
            if ledger[['source','note','included_in_nav']].isna().any().any(): raise ValueError('流水缺来源/符号解释/净值包含标记')
            csave(ledger,out/'verified_ledger.csv'); issue('info','ledger','已核实现金事件另表列示，不再次改变净值，不与交易分红重复汇总')
    return frame

def concentration(cfg,p,cash,out):
    weights=pd.concat([p.weight,pd.Series({'CAD现金':cash/(p.MarketValue.sum()+cash)})])
    for field in ['asset_class','region','sector','currency']:
        labels=pd.Series({s:cfg['assets'][s].get(field,'未知') for s in p.index})
        group=p.weight.groupby(labels).sum(); group.loc['CAD现金' if field=='currency' else '现金']=weights['CAD现金']
        csave(group.to_frame('weight'),out/f'concentration_{field}.csv')
    fig,ax=plt.subplots(figsize=(9,4.5)); weights.sort_values().plot.barh(ax=ax,color='#3876a8')
    ax.set(title='可识别资产权重（持仓明细+现金为分母）',xlabel='权重'); ax.xaxis.set_major_formatter(PercentFormatter(1)); fig.tight_layout(); fig.savefig(out/'weights.png',dpi=150); plt.close(fig)
    return weights

def risk_analysis(cfg,p,weights,prices,boc,start,asof,out):
    if not set(p.index)<=set(prices):
        issue('error','risk_incomplete',f'缺少{sorted(set(p.index)-set(prices))}的CAD历史；不删除后归一假冒完整组合')
        return None
    panel=pd.concat({s:prices[s] for s in p.index},axis=1).sort_index(); common=panel.dropna()
    csave(panel.loc[~panel.index.isin(common.index)],out/'excluded_market_dates.csv')
    issue('info','common_dates',f'共同有效日期{len(common)}，排除{len(panel)-len(common)}个不完整日期；证券价格不填充，资产指标使用同一窗口，跨休市缺口收益按保留观测间收益计算，252年化为近似')
    if len(common)<60: issue('error','risk_short',f'仅{len(common)}价格日，少于60，停止统计'); return None
    if common.index.min()>start+pd.Timedelta(days=10) or (asof-common.index.max()).days>7: issue('warning','risk_window',f'实际共同窗口{common.index.min().date()}—{common.index.max().date()}，不能称完整截至当前三年')
    r=common.pct_change(fill_method=None).iloc[1:]; rf=None
    if 'TB.CDN.90D.MID' in boc:
        yields=asof_series(boc['TB.CDN.90D.MID'],common.index[:-1]-pd.Timedelta(days=1),7,'CAD国库券区间开始前已发布利率')
        gaps=np.diff(common.index.values)/np.timedelta64(1,'D')
        rf=pd.Series((1+yields.to_numpy()/100)**(gaps/365)-1,index=r.index)
    if rf is None or rf.isna().any(): issue('warning','rf_missing','无风险数据不完整，Sharpe留空，不假设利率为零')
    portfolio=r.mul(p.weight,axis=1).sum(axis=1)
    stats=pd.DataFrame({s:metrics(r[s],rf) for s in r}|{'假设性组合':metrics(portfolio,rf)}).T
    stats['start']=str(common.index.min().date()); stats['end']=str(common.index.max().date())
    csave(stats,out/'historical_risk.csv'); csave(common,out/'historical_CAD_total_return_prices.csv'); csave(r,out/'historical_returns.csv')
    if rf is not None: csave(rf.to_frame('risk_free_interval_return'),out/'risk_free.csv')
    cov=r.cov()*252; w=p.weight.reindex(cov.index).to_numpy(); sigma=np.sqrt(float(w@cov.to_numpy()@w))
    rc=w*(cov.to_numpy()@w)/sigma if sigma>0 else w*np.nan
    risk=pd.DataFrame({'weight':p.weight,'volatility_contribution':rc,'risk_share':rc/sigma if sigma>0 else np.nan})
    risk.loc['CAD现金']=[weights['CAD现金'],0,0]; csave(risk,out/'risk_contribution.csv')
    if not np.isclose(risk.volatility_contribution.sum(),sigma): raise ValueError('风险贡献不等于组合波动率')
    corr=r.corr(); csave(corr,out/'correlation.csv')
    fig,ax=plt.subplots(figsize=(8,7)); im=ax.imshow(corr,vmin=-1,vmax=1,cmap='RdBu_r')
    ax.set_xticks(range(len(corr)),corr.columns,rotation=45,ha='right'); ax.set_yticks(range(len(corr)),corr.index)
    for i in range(len(corr)):
        for j in range(len(corr)): ax.text(j,i,f'{corr.iloc[i,j]:.2f}',ha='center',va='center',fontsize=8)
    ax.set_title('CAD总回报日收益相关性（共同窗口）'); fig.colorbar(im,ax=ax); fig.tight_layout(); fig.savefig(out/'correlation.png',dpi=150); plt.close(fig)
    fig,ax=plt.subplots(figsize=(9,4.5)); risk[['weight','risk_share']].rename(columns={'weight':'资产权重','risk_share':'风险占比'}).plot.bar(ax=ax)
    ax.set(title='当前权重与历史波动风险贡献（可为负）',ylabel='占比',xlabel='资产'); ax.yaxis.set_major_formatter(PercentFormatter(1)); fig.tight_layout(); fig.savefig(out/'risk_contribution.png',dpi=150); plt.close(fig)
    sim=pd.concat([pd.Series([1.],index=common.index[:1]),(1+portfolio).cumprod()])
    lines((sim-1).to_frame('假设性组合'),out/'simulated_return.png','当前持仓的假设性历史模拟\n每日固定权重再平衡、CAD现金零收益、无交易成本','累计收益')
    lines((sim/sim.cummax()-1).to_frame('假设性组合'),out/'simulated_drawdown.png','当前持仓的假设性历史模拟：回撤','回撤')
    issue('info','simulation','每日当前权重再平衡、CAD现金零收益、无费用税及冲击，复权总回报已含分红再投资，不再加入分红。不是比赛真实收益或未来预测')
    return stats,risk

def unavailable(path,title):
    if path.exists(): return
    fig,ax=plt.subplots(figsize=(9,4)); ax.axis('off'); ax.text(.5,.6,title,ha='center',fontsize=17)
    ax.text(.5,.35,'数据不可用；请查看报告中的失败和缺失项目。\n没有使用虚构数据生成图形。',ha='center'); fig.tight_layout(); fig.savefig(path,dpi=120); plt.close(fig)

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
        (ROOT/cfg['output_dir']/'LATEST.txt').write_text(str(out),encoding='utf-8')
        print(f'报告：{out/"report.html"}'); print(f'不可用项目{sum(x["level"]=="error" for x in ISSUES)}，待核对{sum(x["level"]=="warning" for x in ISSUES)}')
        return 0
    except Exception as exc:
        issue('fatal','input_or_calculation',f'{type(exc).__name__}: {exc}'); jsave(out/'failure.json',ISSUES)
        print(f'停止：{exc}；详见{out/"failure.json"}',file=sys.stderr); return 2

if __name__=='__main__': sys.exit(main())
