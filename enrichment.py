"""Official-source holdings, RPMC benchmark and explicitly labelled accounting estimates."""
from __future__ import annotations
import csv
import hashlib
import io
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

RPMC_URL='https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/finance-research-and-trading-lab/events/rpmc/'
URLS={
 'XEF_CSV':('csv','https://www.blackrock.com/ca/investors/en/products/251421/ishares-msci-eafe-imi-index-etf/1464253357814.ajax?fileType=csv&fileName=XEF_holdings&dataType=fund'),
 'XBB_CSV':('csv','https://www.blackrock.com/ca/investors/en/products/239493/ishares-canadian-universe-bond-index-etf/1464253357814.ajax?fileType=csv&fileName=XBB_holdings&dataType=fund'),
 'SGOV_CSV':('csv','https://www.ishares.com/us/products/314116/ishares-0-3-month-treasury-bond-etf/latest-holdings.csv'),
 'IEFA_CSV':('csv','https://www.ishares.com/us/products/244049/ishares-core-msci-eafe-etf/latest-holdings.csv'),
 'VEE':('html','https://www.vanguard.ca/en/product/etf/equity/9556/vanguard-ftse-emerging-markets-all-cap-index-etf'),
 'VEE_FACT_PDF':('pdf','https://fund-docs.vanguard.com/VEE_FTSE_Emerging_Markets_All_Cap_Index_ETF_9556_FS_EN_CA.pdf'),
 'ZCN_PDF':('pdf','https://df.bmogam.com/assets/static/etf-profiles-pdfs/BMO%20S%26PTSX%20Capped%20Composite%20Index%20ETF_Factsheet_EN_ZCN.pdf'),
 'SPY':('html','https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy'),
 'SPY_XLSX':('xlsx','https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx'),
 'GLD':('html','https://www.ssga.com/us/en/individual/etfs/spdr-gold-shares-gld'),
 'GLD_ARCHIVE_XLSX':('xlsx','https://api.spdrgoldshares.com/api/v1/historical-archive?exchange=NYSE&lang=en&product=gld'),
 'RPMC':('html',RPMC_URL),
 'ST_FX':('html','https://www.stocktrak.com/faq/want-trade-foreign-markets/'),
 'ST_FEES':('html','https://www.stocktrak.com/faq/brokerage-commission-determined/'),
}

class Sources:
    def __init__(self,a,cfg,offline,refresh):
        self.a=a; self.base=a.ROOT/cfg['cache_dir']/'issuers'; self.base.mkdir(parents=True,exist_ok=True)
        self.offline=offline; self.refresh=refresh; self.used=[]
    def get(self,key):
        ext,url=URLS[key]; file=self.base/f'{key}.{ext}'; meta=self.base/f'{key}.meta.json'
        if (self.refresh or not file.exists()) and not self.offline:
            print(f'发行商/规则: {key}',flush=True)
            r=requests.get(url,timeout=25,headers={'User-Agent':'Mozilla/5.0'}); r.raise_for_status()
            file.write_bytes(r.content)
            self.a.jsave(meta,dict(source=url,final_url=r.url,fetched_at_utc=datetime.now(timezone.utc).isoformat(),sha256=hashlib.sha256(r.content).hexdigest()))
        if not file.exists() or not meta.exists(): raise ValueError(f'{key}没有官方来源缓存')
        info=json.loads(meta.read_text(encoding='utf-8')); digest=hashlib.sha256(file.read_bytes()).hexdigest()
        if info.get('sha256',digest)!=digest: raise ValueError(f'{key}来源缓存校验和不符')
        info.update(key=key,sha256=digest); self.used.append(info)
        return file,info

def clean_text(v):
    return '' if pd.isna(v) else str(v).strip()

def name_id(name):
    normalized=re.sub(r'[^A-Z0-9]','',str(name).upper())
    if normalized in ('CISCOSYSTEMSINC','CISCOSYSTEMS'): return 'US:CSCO'
    if normalized.startswith('CREDOTECHNOLOGYGROUP'): return 'US:CRDO'
    return 'NAME:'+normalized

def parse_blackrock(path):
    text=path.read_text(encoding='utf-8-sig'); lines=text.splitlines()
    date_line=next(x for x in lines[:12] if 'Fund Holdings as of' in x)
    date=pd.to_datetime(next(csv.reader([date_line]))[1]).strftime('%Y-%m-%d')
    header=next(i for i,x in enumerate(lines) if 'Name,Sector,Asset Class' in x)
    frame=pd.read_csv(io.StringIO('\n'.join(lines[header:])),keep_default_na=False)
    frame['weight']=pd.to_numeric(frame['Weight (%)'],errors='coerce')/100
    return frame[frame.weight.notna()].copy(),date

def frame_from_table(table):
    return pd.read_html(io.StringIO(str(table)))[0]

def rows_from_blackrock(frame,fund,asof,info,path=None):
    rows=[]
    for n,r in frame.iterrows():
        name=clean_text(r['Name']); ticker=clean_text(r.get('Ticker',''))
        rawclass=clean_text(r.get('Asset Class','')); sector=clean_text(r.get('Sector',''))
        asset='股票' if rawclass=='Equity' else ('债券' if rawclass=='Fixed Income' else '现金/其他')
        if name=='TREASURY BILL': asset='国库券'; sector='不适用：国库券'
        elif asset=='债券': sector='债券发行人类型：'+sector
        elif rawclass in ('Cash','Money Market'): sector='不适用：现金/货币工具'
        elif rawclass=='Futures': asset='衍生品'; sector='不适用：衍生品'
        currency=clean_text(r.get('Market Currency',r.get('Currency','未知')))
        location=clean_text(r.get('Location','未知'))
        isin=clean_text(r.get('ISIN',''))
        identifier=isin if isin not in ('','-') else name_id(name)
        if asset in ('债券','国库券') and not isin:
            identifier='BOND:'+name+'|'+clean_text(r.get('Maturity',''))+'|'+clean_text(r.get('Coupon (%)',''))
        if asset=='现金/其他': identifier='CASHLIKE:'+name+'|'+currency
        nested=ticker=='IEFA' or ('ISHARES' in name and name.endswith(' ETF') and asset=='股票')
        if nested: asset='未穿透基金'; sector='未知：嵌套基金'
        rows.append(dict(fund=fund,underlying_id=identifier,ticker=ticker,name=name,weight=float(r.weight),
            sector=sector if sector not in ('','-') else '未知',country=location if location not in ('','-') else '未知',
            underlying_currency=currency if currency not in ('','-') else '未知',asset_class=asset,
            asof=asof,source=info['source'],fetched_at_utc=info['fetched_at_utc'],path=path or fund,
            detail='发行商完整公开列表；权重有四舍五入，现金/衍生品可为负；币种为底层报价代理，不是收入或净套保敞口'))
    return rows

def percent_lines(text):
    return [(m.group(1).strip(),float(m.group(2))/100) for m in re.finditer(r'^\s*([^\n%]+?)\s+([\d.]+)%\s*$',text,re.M)]

def official_holdings(a,cfg,p,cash,out,offline=False,refresh=False):
    store=Sources(a,cfg,offline,refresh); allrows=[]; summaries=[]; status=[]
    asof=pd.Timestamp(cfg['positions_asof'])
    for key in ['RPMC','ST_FX','ST_FEES']:
        try: store.get(key)
        except Exception as exc: a.issue('warning','official_rule_source',f'{key}: {exc}；保留已核实配置与来源链接')
    for fund in ['SGOV','XEF','XBB']:
        if fund not in p.index: continue
        try:
            file,info=store.get(fund+'_CSV'); frame,date=parse_blackrock(file)
            if pd.Timestamp(date)>asof: raise ValueError('最新持仓日期晚于组合估值，不能用于该日当前敞口')
            rows=rows_from_blackrock(frame,fund,date,info)
            if fund=='XEF' and any(r['ticker']=='IEFA' for r in rows):
                nested=[r for r in rows if r['ticker']=='IEFA']; rows=[r for r in rows if r['ticker']!='IEFA']
                try:
                    nf,ni=store.get('IEFA_CSV'); nh,nd=parse_blackrock(nf)
                    if pd.Timestamp(nd)>asof: raise ValueError('IEFA资料晚于组合估值')
                    for parent in nested:
                        nr=rows_from_blackrock(nh,fund,nd,ni,'XEF > IEFA')
                        for r in nr: r['weight']*=parent['weight']; r['detail']+='；乘以XEF内IEFA权重，父子日期独立记录'
                        rows.extend(nr)
                except Exception as exc:
                    rows.extend(nested); a.issue('warning','nested_fund',f'XEF内IEFA未能继续穿透：{exc}')
            allrows.extend(rows)
            status.append(dict(fund=fund,holdings_asof=date,security_weight_coverage=sum(r['weight'] for r in rows),
                list_status='发行商完整公开列表（舍入后覆盖不一定100%）',source=info['source'],fetched_at_utc=info['fetched_at_utc']))
        except Exception as exc: a.issue('warning','issuer_holdings',f'{fund}: {exc}')
    if 'SPY' in p.index:
        try:
            file,info=store.get('SPY_XLSX'); raw=pd.read_excel(file,header=None)
            date=pd.to_datetime(str(raw.iloc[2,1]).replace('As of ','')).strftime('%Y-%m-%d')
            if pd.Timestamp(date)>asof: raise ValueError('SPY持仓晚于组合估值')
            h=pd.read_excel(file,skiprows=4); h['weight']=pd.to_numeric(h.Weight,errors='coerce')/100; h=h[h.weight.notna()]
            for _,r in h.iterrows():
                ticker=clean_text(r.Ticker); name=clean_text(r.Name)
                identifier='US:'+ticker if ticker in ('CSCO','CRDO') else 'CUSIP:'+clean_text(r.Identifier)
                allrows.append(dict(fund='SPY',underlying_id=identifier,ticker=ticker,name=name,weight=float(r.weight),sector='未知：完整表未提供行业',
                    country='未知：完整表未提供国家',underlying_currency=clean_text(r['Local Currency']),asset_class='股票',asof=date,
                    source=info['source'],fetched_at_utc=info['fetched_at_utc'],path='SPY',detail='发行商完整公开列表；行业汇总另表，不从挂牌地猜测公司经济国家'))
            status.append(dict(fund='SPY',holdings_asof=date,security_weight_coverage=h.weight.sum(),list_status='发行商完整公开列表',source=info['source'],fetched_at_utc=info['fetched_at_utc']))
            page,pi=store.get('SPY'); soup=BeautifulSoup(page.read_text(encoding='utf-8'),'html.parser')
            for table in soup.find_all('table'):
                f=frame_from_table(table)
                if list(f.columns)==['Sector','Weight']:
                    context=table.find_previous(string=re.compile('Sector Breakdown'))
                    # Date comes from the nearby heading, not the acquisition timestamp.
                    header=table.find_previous(['h2','h3','h4']); surrounding=header.parent.get_text(' ',strip=True) if header else ''
                    dates=re.findall(r'(?:as of|As of)\s+([A-Z][a-z]{2} \d{1,2} \d{4})',surrounding)
                    sd=pd.to_datetime(dates[0]).strftime('%Y-%m-%d') if dates else date
                    for _,r in f.iterrows(): summaries.append(dict(fund='SPY',dimension='sector',label=r.Sector,weight=float(str(r.Weight).strip('%'))/100,asof=sd,source=pi['source'],basis='发行商行业汇总，可能与完整列表日期不同',fetched_at_utc=pi['fetched_at_utc']))
                    break
        except Exception as exc: a.issue('warning','issuer_holdings',f'SPY: {exc}')
    if 'VEE' in p.index:
        try:
            file,info=store.get('VEE'); soup=BeautifulSoup(file.read_text(encoding='utf-8'),'html.parser')
            text=soup.get_text(' ',strip=True)
            date=pd.to_datetime(re.search(r'Market allocation As at ([A-Z][a-z]{2} \d{1,2} \d{4})',text).group(1)).strftime('%Y-%m-%d')
            if pd.Timestamp(date)>asof: raise ValueError('VEE资料晚于组合估值')
            top=[]
            for table in soup.find_all('table'):
                f=frame_from_table(table)
                if 'Country' in f and 'Fund' in f:
                    for _,r in f.iterrows(): summaries.append(dict(fund='VEE',dimension='country',label=r.Country,weight=float(str(r.Fund).strip('%'))/100,asof=date,source=info['source'],basis='官方底层股票分布代理；排除临时现金，未取得VEE/VWO精确份额比例',fetched_at_utc=info['fetched_at_utc']))
                elif 'Holding name' in f:
                    for _,r in f.iterrows():
                        row=dict(fund='VEE',underlying_id=name_id(r['Holding name']),ticker='',name=r['Holding name'],weight=float(str(r['% of market value']).strip('%'))/100,
                            sector=r.Sector,country='未知：Region是上市市场代码',underlying_currency='未知',asset_class='股票',asof=date,source=info['source'],fetched_at_utc=info['fetched_at_utc'],
                            path='VEE > 官方汇总底层（仅前10）',detail='官方页面部分底层代理，不是完整VWO/VEE清单；保留未知，不能凭前10排除其他重叠')
                        top.append(row)
            if not top: raise ValueError('未找到官方前十大底层表')
            allrows.extend(top); status.append(dict(fund='VEE',holdings_asof=date,security_weight_coverage=sum(r['weight'] for r in top),list_status='仅前10底层代理；未取得VEE/VWO完整份额链',source=info['source'],fetched_at_utc=info['fetched_at_utc']))
            pdf,pi=store.get('VEE_FACT_PDF'); text='\n'.join(p.extract_text() for p in PdfReader(pdf).pages)
            fd=pd.to_datetime(re.search(r'Factsheet \| ([A-Za-z]+ \d{1,2}, \d{4})',text).group(1)).strftime('%Y-%m-%d')
            sector=text.split('Sector weighting',1)[1].split('Market allocation',1)[0]
            for m in re.finditer(r'^([^\n]+?)\s+([\d.]+)%\s+[\d.]+%\s*$',sector,re.M):
                if m.group(1).strip()=='Total': continue
                summaries.append(dict(fund='VEE',dimension='sector',label=m.group(1).strip(),weight=float(m.group(2))/100,asof=fd,source=pi['source'],basis='官方VWO底层ICB行业代理，排除临时现金；不可当作精确VEE资产负债穿透',fetched_at_utc=pi['fetched_at_utc']))
            a.issue('info','vee_basis','VEE官网前10与事实表前10数值口径有差异；证券清单只用官网表，行业只用事实表各自日期/口径，不拼成完整持仓；基金套基金精确份额链仍未取得')
        except Exception as exc: a.issue('warning','issuer_holdings',f'VEE: {exc}')
    if 'ZCN' in p.index:
        try:
            file,info=store.get('ZCN_PDF'); text='\n'.join(pg.extract_text() for pg in PdfReader(file).pages)
            date=pd.to_datetime(re.search(r'DATA AS OF ([A-Z]{3} \d{1,2}, \d{4})',text).group(1)).strftime('%Y-%m-%d')
            if pd.Timestamp(date)>asof: raise ValueError('ZCN资料晚于组合估值')
            sec=text.split('Sector Allocation pie chart',1)[1].split('Geographic Allocation',1)[0]
            geo=text.split('Geographic allocation pie chart',1)[1].split('Top Holdings',1)[0]
            for dim,section in [('sector',sec),('country',geo)]:
                for label,w in percent_lines(section): summaries.append(dict(fund='ZCN',dimension=dim,label=label,weight=w,asof=date,source=info['source'],basis='官方基金汇总；不由前10推断完整分布',fetched_at_utc=info['fetched_at_utc']))
            top=text.split('Top Holdings',1)[1].split('The portfolio holdings',1)[0]; rows=[]
            for name,w in percent_lines(top): rows.append(dict(fund='ZCN',underlying_id=name_id(name),ticker='',name=name,weight=w,sector='未知：前10表未给行业',country='未知：前10表未给国家',underlying_currency='未知',asset_class='股票',asof=date,source=info['source'],fetched_at_utc=info['fetched_at_utc'],path='ZCN > 前10',detail='官方事实表只有前10证券；不据此排除其他证券重叠'))
            allrows.extend(rows); status.append(dict(fund='ZCN',holdings_asof=date,security_weight_coverage=sum(r['weight'] for r in rows),list_status='产品页访问失败；使用官方事实表前10及行业/国家汇总',source=info['source'],fetched_at_utc=info['fetched_at_utc']))
        except Exception as exc: a.issue('warning','issuer_holdings',f'ZCN: {exc}')
    if 'GLD' in p.index:
        try:
            file,info=store.get('GLD_ARCHIVE_XLSX')
            # Keep raw archive; schema varies. Inspection in parser below is strict.
            sheet=pd.read_excel(file,sheet_name='US GLD Historical Archive')
            sheet['date']=pd.to_datetime(sheet.Date,format='%d-%b-%Y',errors='coerce')
            sheet['gold_ounces']=pd.to_numeric(sheet['Total Ounces of Gold in the Trust'],errors='coerce')
            sheet['trust_net_assets_USD']=pd.to_numeric(sheet['Total Net Asset Value in the Trust'],errors='coerce')
            valid=sheet[sheet.date.le(asof)&sheet.gold_ounces.notna()&sheet.trust_net_assets_USD.notna()].sort_values('date')
            if valid.empty: raise ValueError('黄金档案没有估值日前有效持仓')
            latest=valid.iloc[-1]; date=latest.date.strftime('%Y-%m-%d')
            a.csave(valid,out/'gld_official_archive.csv'); a.csave(latest.to_frame('value'),out/'gld_current_official_assets.csv')
            status.append(dict(fund='GLD',holdings_asof=date,security_weight_coverage=0.,list_status=f'实物黄金{latest.gold_ounces:,.2f}盎司、信托净资产USD {latest.trust_net_assets_USD:,.2f}；精确黄金/现金/负债比例未量化，结构归为黄金',source=info['source'],fetched_at_utc=info['fetched_at_utc']))
        except Exception as exc:
            a.issue('warning','gld_archive',f'GLD实物黄金明细暂不可用：{exc}；官方产品结构可用于黄金类别，但精确黄金/现金/负债比例未知')
            try:
                _,info=store.get('GLD'); status.append(dict(fund='GLD',holdings_asof='结构性分类，非逐日持仓',security_weight_coverage=0.,list_status='黄金信托结构已核实；精确资产负债比例未知',source=info['source'],fetched_at_utc=info['fetched_at_utc']))
            except Exception: pass
    h=pd.DataFrame(allrows); summary=pd.DataFrame(summaries); coverage=pd.DataFrame(status)
    if len(h):
        h['portfolio_exposure']=h.fund.map(p.weight)*h.weight
        a.csave(h,out/'etf_lookthrough.csv')
        # Aggregate exact identifiers only; name matches are explicitly lower-confidence keys.
        agg=h.groupby(['underlying_id','name'],dropna=False).portfolio_exposure.sum().reset_index()
        for symbol in p.index:
            if cfg['assets'][symbol]['asset_class']=='股票':
                agg.loc[len(agg)]=['US:'+symbol,p.loc[symbol].get('Description',symbol),p.loc[symbol,'weight']]
        merged=agg.groupby('underlying_id').agg(name=('name','first'),portfolio_exposure=('portfolio_exposure','sum')).sort_values('portfolio_exposure',ascending=False)
        a.csave(merged,out/'underlying_exposure.csv')
        overlaps=[]
        for symbol in ['CSCO','CRDO']:
            if symbol not in p.index: continue
            indirect=h[h.underlying_id.eq('US:'+symbol)]
            overlaps.append(dict(symbol=symbol,direct_weight=p.loc[symbol,'weight'],identified_indirect_weight=indirect.portfolio_exposure.sum(),
                total_identified_weight=p.loc[symbol,'weight']+indirect.portfolio_exposure.sum(),funds=';'.join(indirect.fund.unique()),
                note='已识别暴露下限；VEE/ZCN只有部分证券，不能据缺席断言其他ETF中为零'))
        a.csave(pd.DataFrame(overlaps).set_index('symbol'),out/'direct_etf_overlap.csv')
    if len(summary): a.csave(summary,out/'issuer_allocations.csv')
    a.csave(coverage,out/'etf_coverage.csv'); a.jsave(out/'issuer_sources.json',store.used)
    exposures=[]
    for dim,col in [('sector','sector'),('country','country'),('underlying_currency','underlying_currency')]:
        for fund,pos in p.iterrows():
            if fund in ['CSCO','CRDO']:
                exposures.append(dict(dimension=dim,label='未知：直接股票'+('行业' if dim=='sector' else '国家') if dim!='underlying_currency' else 'USD',portfolio_weight=pos.weight,basis='直接股票；USD是报价币种，不代表全部经济敞口')); continue
            if fund=='GLD':
                exposures.append(dict(dimension=dim,label={'sector':'不适用：黄金','country':'不适用：实物黄金','underlying_currency':'黄金（USD报价，不等同美元现金）'}[dim],portfolio_weight=pos.weight,basis='官方黄金信托结构；精确资产负债组成未量化')); continue
            use=summary[summary.fund.eq(fund)&summary.dimension.eq(dim)] if len(summary) else pd.DataFrame()
            if len(use):
                for _,r in use.iterrows(): exposures.append(dict(dimension=dim,label=r.label,portfolio_weight=pos.weight*r.weight,basis=r.basis))
                total=use.weight.sum()
            else:
                sub=h[h.fund.eq(fund)] if len(h) else pd.DataFrame(); total=0.
                for _,r in sub.iterrows():
                    exposures.append(dict(dimension=dim,label=r[col],portfolio_weight=r.portfolio_exposure,basis=r.detail)); total+=r.weight
            residual=1-total
            if abs(residual)>1e-10:
                exposures.append(dict(dimension=dim,label='未知/未穿透部分' if residual>0 else '发行商权重舍入差（未强制归一）',portfolio_weight=pos.weight*residual,basis='保留原权重覆盖差额'))
        exposures.append(dict(dimension=dim,label='CAD现金' if dim!='sector' else '不适用：现金',portfolio_weight=cash/(p.MarketValue.sum()+cash),basis='实际账户现金'))
    e=pd.DataFrame(exposures); a.csave(e,out/'lookthrough_allocation_detail.csv')
    for dim in e.dimension.unique():
        g=e[e.dimension.eq(dim)].groupby('label').portfolio_weight.sum().sort_values(ascending=False)
        if not np.isclose(g.sum(),1): raise ValueError(f'{dim}穿透含未知部分权重未合计100%')
        a.csave(g.to_frame(),out/f'lookthrough_{dim}.csv')
    a.issue('info','issuer_scope','发行商持仓仅用于当前敞口，不代表过去三年历史持仓；国家是发行商分类/所在地代理，底层币种是报价代理，均不等于收入来源或净套保后经济敞口')
    a.issue('warning','partial_lookthrough','VEE/ZCN只取得部分证券清单，使用官方行业/国家汇总补充；未取得的证券、精确基金套基金份额链及净套保敞口继续保留未知。完整公开列表的舍入残差也保留，不归一填平')
    return coverage

def benchmark(a,cfg,prices,nav,boc,out):
    start=pd.Timestamp(cfg['benchmark']['inception']); dates=nav.index[nav.index>=start]
    platform=a.ROOT/cfg['benchmark']['nav_file']; label='官方配置的模拟基准'
    if platform.exists():
        official=pd.read_csv(platform); official['date']=pd.to_datetime(official.date,format='%Y-%m-%d')
        if official.date.duplicated().any() or not official.currency.eq('CAD').all() or (official.value<=0).any(): raise ValueError('官方基准净值重复/币种不符/非正')
        s=official.set_index('date').value
        dates=dates.intersection(s.index); b=s.reindex(dates); label='RPMC Benchmark官方净值'
    else:
        components=cfg['benchmark']['weights']
        if not np.isclose(sum(components.values()),1): raise ValueError('基准初始权重不合计100%')
        if not set(components)<=set(prices): raise ValueError('官方配置模拟基准缺组件CAD价格')
        if any(start not in prices[s].dropna().index for s in components): raise ValueError('缺2026-10-09建仓日组件价格；不能用其他日期替代')
        panel=pd.DataFrame({s:a.asof_series(prices[s].dropna(),dates,4,f'基准{s}休市沿用最近收盘（显式）')/prices[s].loc[start] for s in components})
        if panel.isna().any().any(): raise ValueError('基准组件存在超过4日的缺价，不能补造基准')
        b=panel.mul(pd.Series(components)).sum(axis=1)
        a.csave(panel,out/'benchmark_component_indices.csv')
        a.issue('info','benchmark_assumptions','未取得RPMC Benchmark平台净值；按官方2026-10-09收盘CAD权重重建，买入持有、各基金内部总回报分红再投资、不做跨基金再平衡、不另计交易佣金/税/换汇点差。以上是分析假设，不是官方已确认规则；周末/休市显式沿用最近4日内收盘和对应汇率')
    if len(dates)<2 or dates.min()!=start: raise ValueError('官方基准与账户缺共同起始估值')
    compare=pd.DataFrame({'账户真实收益':nav.reindex(dates)/nav.loc[start]-1,label:b/b.iloc[0]-1})
    if 'SPY' in prices:
        spy=a.asof_series(prices['SPY'].dropna(),dates,4,'SPY额外参考休市沿用最近收盘')
        compare['SPY额外参考']=spy/spy.iloc[0]-1
    a.csave(compare,out/'actual_benchmark_same_period.csv')
    a.lines(compare,out/'actual_benchmark.png','真实账户与RPMC基准（CAD，同起止日期）\n模拟基准采用买入持有，不是每日固定权重再平衡','累计收益')
    # Sharpe estimate on common market observation dates only, not calendar-day NAV.
    quote_dates=dates
    for s in cfg['benchmark']['weights']: quote_dates=quote_dates.intersection(prices[s].dropna().index)
    q=compare.reindex(quote_dates); summary=[]
    rf=None
    if 'TB.CDN.90D.MID' in boc and len(q)>2:
        y=a.asof_series(boc['TB.CDN.90D.MID'],q.index[:-1]-pd.Timedelta(days=1),7,'比赛Sharpe区间前已发布CAD无风险利率')
        days=np.diff(q.index.values)/np.timedelta64(1,'D'); rf=pd.Series((1+y.to_numpy()/100)**(days/365)-1,index=q.index[1:])
    for col in compare:
        r=(1+q[col]).pct_change(fill_method=None).iloc[1:]
        m=a.metrics(r,rf) if len(r)>=2 else {}
        summary.append(dict(series=col,start=str(dates.min().date()),end=str(dates.max().date()),cumulative_return=compare[col].iloc[-1],
            max_drawdown=a.mdd(1+compare[col]),sharpe_estimate=m.get('sharpe',np.nan),market_return_observations=len(r),
            sharpe_sample_start=str(q.index.min().date()),sharpe_sample_end=str(q.index.max().date()),annual_volatility_estimate=m.get('annual_volatility',np.nan)))
    a.csave(pd.DataFrame(summary).set_index('series'),out/'actual_comparison_summary.csv')
    a.issue('info','short_sample_sharpe',f'比赛Sharpe仅为CAD国库券代理及共同市场日采样的短样本估计（{max(len(q)-1,0)}个收益），不等同平台官方评分。官方页面未给出完整评分采样/无风险参数；周末净值仍保留用于累计收益与回撤')
    return dict(label=label,start=str(dates.min().date()),end=str(dates.max().date()),account=float(compare.iloc[-1,0]),benchmark=float(compare.iloc[-1,1]))

def accounting_estimate(a,cfg,p,t,nav,summary,cash,boc,out):
    evidence=cfg['accounting_evidence']; digest=hashlib.sha256((a.ROOT/cfg['input_dir']/'transactions.csv').read_bytes()).hexdigest()
    buys=t[t.kind.eq('buy')]; sells=t[t.kind.eq('sell')]
    per_trade=float(evidence['commission_per_trade_CAD_supported'])
    use_export=(digest==evidence['transactions_sha256'] and len(buys)>0 and sells.empty and np.isclose(per_trade*len(buys),evidence['commission_total_CAD']))
    rows=[]; initial=cfg['initial_capital_CAD']; interest=a.money(summary['Interest Earn Rate'])
    amounts=buys.amount_numeric.sum(); dividends=t.loc[t.kind.eq('dividend'),'amount_numeric'].sum()
    commission=evidence['commission_total_CAD'] if use_export else np.nan
    cash_residual=cash-(initial+amounts+dividends+interest-commission) if use_export else np.nan
    if use_export and abs(cash_residual)>.02:
        use_export=False; a.issue('warning','export_cash_test','当前输入未通过金额/利息/佣金现金恒等式，不沿用Amount为CAD的推断')
    for s,pos in p.iterrows():
        sub=t[t.Symbol.eq(s)].copy(); trade=sub[sub.kind.isin(['buy','sell'])].copy()
        fee=-per_trade*len(trade) if use_export else np.nan
        method='导出金额与现金对账支持的估算（尚非逐单平台确认）' if use_export else '公开央行日度参考汇率估算'
        if use_export:
            trade['fx_CAD_per_local']=trade.amount_numeric.abs()/(trade.Quantity*trade.price_numeric)
        elif pos.Currency=='CAD': trade['fx_CAD_per_local']=1.
        elif 'FXUSDCAD' in boc:
            trade['fx_CAD_per_local']=a.asof_series(boc['FXUSDCAD'],pd.DatetimeIndex(trade.date.dt.normalize()),4,'实际账本公开汇率估算').to_numpy()
        else:
            a.issue('warning','accounting_fx',f'{s}缺汇率估算，保留NA'); continue
        if trade.fx_CAD_per_local.isna().any(): continue
        realized,unrealized,pe,fe=a.fifo_cad(trade,pos.Quantity,pos.LastPrice,pos.implied_snapshot_fx)
        div=sub[sub.kind.eq('dividend')]; confirmed_div=div.amount_numeric.sum() if div.Amount.astype(str).str.startswith('C$').all() else np.nan
        rows.append(dict(symbol=s,method=method,remaining_gross_cost_CAD_estimate=sum(trade.loc[trade.kind.eq('buy'),'Quantity']*trade.loc[trade.kind.eq('buy'),'price_numeric']*trade.loc[trade.kind.eq('buy'),'fx_CAD_per_local']) if not trade.kind.eq('sell').any() else np.nan,
            realized_gross_CAD_estimate=realized,unrealized_gross_CAD_estimate=unrealized,price_effect_CAD_estimate=pe,fx_effect_CAD_estimate=fe,
            dividends_confirmed_CAD=confirmed_div,commission_cash_effect_CAD_supported=fee,net_contribution_CAD_estimate=realized+unrealized+confirmed_div+fee,
            weight_of_initial_capital_estimate=(realized+unrealized+confirmed_div+fee)/initial))
    frame=pd.DataFrame(rows).set_index('symbol'); a.csave(frame,out/'asset_pnl_CAD_estimate.csv')
    details=[]
    for _,r in buys.iterrows():
        implied=abs(r.amount_numeric)/(r.Quantity*r.price_numeric)
        details.append(dict(source_row=r.source_row,symbol=r.Symbol,raw_amount=r.Amount,price_currency=r.Currency,raw_FXRate=r.get('FXRate',''),
            implied_Amount_CAD_per_price_currency=implied,commission_raw_reported=-per_trade if use_export else np.nan,
            commission_currency_supported='CAD' if use_export else 'unknown',status='全账户现金对账支持，仍需Session Rules/逐笔成交CAD字段确认' if use_export else '未确认'))
    a.csave(pd.DataFrame(details),out/'transaction_settlement_diagnostics.csv')
    reconciliation=pd.DataFrame({'CAD':{'初始资金':initial,'买入导出Amount合计（保留符号）':amounts,'确认分红':dividends,'累计佣金（用户截图叙述支持）':-commission,
        'Summary利息金额（累计口径由现金对账支持）':interest,'实际现金':cash,'现金重建残差':cash_residual,
        '资产净贡献估算合计':frame.net_contribution_CAD_estimate.sum(min_count=len(frame)),
        '账户真实净盈亏':nav.iloc[-1]-initial,
        '净贡献估算+利息-真实盈亏':frame.net_contribution_CAD_estimate.sum(min_count=len(frame))+interest-(nav.iloc[-1]-initial)}})
    a.csave(reconciliation,out/'cash_and_pnl_bridge.csv')
    if use_export:
        a.issue('info','cash_reconstruction',f'输入金额按佣金前CAD解释通过现金对账；利息{interest:.2f}、佣金{commission:.2f}、残差{cash_residual:.6f} CAD。属于对账支持估算，不是独立确认的逐单结算。')
        a.issue('warning','session_rules',f'本次{len(buys)}笔交易每笔佣金估计{per_trade:.2f} CAD仅绑定已配置交易文件哈希；实际规则以本场Session Rules为准，不推广至未来交易。')
    else: a.issue('warning','estimate_costs','CAD成本/价格和汇率归因使用公开日度汇率估算，非平台精确结果；费用未知则净贡献留空，不阻止官方账户净值整体收益')
    return frame

def render_report(a,cfg,p,weights,actual,estimate,risk,coverage,out):
    """Render exclusively from the current run; never embed a previous account snapshot."""
    def table(df,percent=()):
        df=df.copy()
        for col in percent:
            if col in df: df[col]=df[col].map(lambda x:'未知/不可用' if pd.isna(x) else f'{x:.2%}')
        return df.to_html(border=0,escape=True,float_format=lambda x:f'{x:,.4f}',na_rep='未知/不可用')
    def from_file(name,percent=()):
        path=out/name
        if not path.exists(): return '<p>本次未生成该表。</p>'
        try: return table(pd.read_csv(path,index_col=0),percent)
        except pd.errors.EmptyDataError: return '<p>资料不足，保留未知。</p>'
    parts=['''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Portfolio Return and Risk Analysis</title><style>body{font-family:Arial,Microsoft YaHei,sans-serif;max-width:1200px;margin:32px auto;padding:0 20px;color:#24364b}p{line-height:1.7}table{display:block;overflow:auto;border-collapse:collapse;font-size:13px;max-width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:right}th{background:#eaf2f8}img{max-width:100%}.notice{background:#fff2d5;padding:16px}</style><body><h1>组合收益与风险分析（CAD）</h1>''']
    parts.append(f'<p>持仓日期：{html.escape(str(cfg["positions_asof"]))}；配置初始资金：{cfg["initial_capital_CAD"]:,.2f} CAD。</p>')
    parts.append('<p class="notice">真实净值、官方配置的模拟基准、当前持仓假设性历史模拟分别呈现。历史结果不是未来预测；估算不等同平台精确结算。空值表示未知，不代表零。</p>')
    parts.append('<h2>一、实际账户净值</h2>')
    parts.append(f'<p>{html.escape(actual["label"])}；{actual["start"]}—{actual["end"]}，{actual["calendar_observations"]}个自然日估值；累计变化{actual["cumulative_change"]:.3%}，最大回撤{actual["max_drawdown"]:.3%}。仅在已确认无外部出入金时将净值变化解释为投资收益。分红、利息、佣金已含净值时不重复加减。</p>')
    parts.append(from_file('actual_comparison_summary.csv',['cumulative_return','max_drawdown']))
    parts.append('<p>官方基准优先使用RPMC Benchmark平台净值。无官方净值时，按官网配置重建并标注模拟：建仓日收盘CAD价值配置、跨基金买入持有、不再平衡、各基金总回报分红再投资，不另计佣金/税/换汇点差；这些是分析假设，不是官方已确认规则。SPY为额外参考。对比使用相同起止日期；休市显式沿用最近4日内收盘和相应FX，审计另存。比赛Sharpe仅为共同市场日和CAD国库券代理下的短样本估计，不等于平台官方评分。</p>')
    parts.append('<img src="actual_benchmark.png"><img src="actual_drawdown.png">')
    parts.append('<h3>CAD盈亏估算及现金桥接</h3>'+table(estimate,['weight_of_initial_capital_estimate']))
    parts.append('<p>FIFO核算，分红不新增股数。结算证据必须绑定本次交易文件SHA256并通过现金对账，否则使用公开日度FX估算；未知费用不填零。价格效应用购入FX、汇率效应用终点价格，交叉项归汇率效应。两者合计CAD毛盈亏；费用独立列示一次。未知Session Rules不能以其他比赛默认佣金替代。</p>')
    parts.append(from_file('cash_and_pnl_bridge.csv')+from_file('reconciliation.csv'))
    parts.append('<p>持仓MarketValue已CAD，不重复换汇；Buying Power不是现金。对账差异原样保留，不改现金或虚构费用。Summary利息金额的累计口径仍须以字段说明或对账证据核实，不直接当作年利率。</p>')
    parts.append('<h2>二、当前持仓的假设性历史风险</h2>')
    if risk is not None: parts.append(table(risk[0],['cumulative_return','annual_volatility','max_drawdown']))
    else: parts.append('<p>缺少完整历史行情，组合风险不可用。</p>')
    parts.append('<p>约三年共同有效窗口；Yahoo Adj Close为拆股及分红调整总回报代理，分红不再次加入。USD历史价格按相应日期央行FX转换，4日内显式沿用既往发布FX并留审计；证券价格不填充。假设每日固定当前权重再平衡、CAD现金零收益、无费用税及冲击；不是实际账户历史收益。波动为样本标准差×√252，Sharpe为区间超额收益均值/样本标准差×√252；CAD三月国库券代理取区间开始前已发布利率并按实际天数/365折算。共同交易日删减使252年化成为近似。风险贡献用Euler协方差分解，合计等于组合波动，可为负。</p>')
    for name in ['simulated_return','simulated_drawdown','correlation','weights','risk_contribution']:
        a.unavailable(out/f'{name}.png',name); parts.append(f'<img src="{name}.png">')
    parts.append('<h2>三、发行商ETF穿透：当前敞口</h2>'+table(coverage,['security_weight_coverage']))
    parts.append('<p>仅使用发行商官方资料，保存来源、持仓日期、获取UTC时间。当前ETF持仓不代表过去三年历史持仓。敞口=基金组合权重×基金内部权重；XEF中的IEFA继续穿透。VEE/ZCN部分证券清单不能当完整持仓，官方行业/国家汇总与原始覆盖率分别列示。股票、债券、国库券、黄金和现金分类独立，舍入残差和负现金保留，不强制归一。GLD按实物黄金信托结构分类。</p>')
    for name in ['direct_etf_overlap.csv','lookthrough_sector.csv','lookthrough_country.csv','lookthrough_underlying_currency.csv']:
        parts.append('<h3>'+html.escape(name)+'</h3>'+from_file(name,['portfolio_weight','direct_weight','identified_indirect_weight','total_identified_weight']))
    parts.append('<p>CAD上市不代表无外汇风险。底层Market Currency/Local Currency只是报价币种代理，不是收入来源或净套保后的经济货币风险。发行商行业标准不同，保留原标签。部分名单未出现某股票，不能证明所有基金间接敞口为零。ISIN/CUSIP优先，名称匹配仅为代理。</p>')
    a.csave(pd.DataFrame(a.ISSUES),out/'validation.csv')
    if a.ALIGNMENTS: a.csave(pd.concat(a.ALIGNMENTS,ignore_index=True),out/'alignment_audit.csv')
    parts.append('<h2>校验、来源和限制</h2>'+table(pd.DataFrame(a.ISSUES)))
    parts.append('<p>来源与审计：sources.json、issuer_sources.json、etf_coverage.csv、issuer_allocations.csv、etf_lookthrough.csv、input_manifest.json、run_config.json、alignment_audit.csv。数据源：<a href="https://finance.yahoo.com/">Yahoo Finance</a>、<a href="https://www.bankofcanada.ca/valet/docs">Bank of Canada</a>、BlackRock、Vanguard、BMO、State Street。官方配置：<a href="'+RPMC_URL+'">Rotman RPMC</a>。</p></body></html>')
    (out/'report.html').write_text('\n'.join(parts),encoding='utf-8')
