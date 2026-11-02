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
            print(f'Issuer / rules: {key}',flush=True)
            r=requests.get(url,timeout=25,headers={'User-Agent':'Mozilla/5.0'}); r.raise_for_status()
            file.write_bytes(r.content)
            self.a.jsave(meta,dict(source=url,final_url=r.url,fetched_at_utc=datetime.now(timezone.utc).isoformat(),sha256=hashlib.sha256(r.content).hexdigest()))
        if not file.exists() or not meta.exists(): raise ValueError(f'{key} has no official-source cache')
        info=json.loads(meta.read_text(encoding='utf-8')); digest=hashlib.sha256(file.read_bytes()).hexdigest()
        if info.get('sha256',digest)!=digest: raise ValueError(f'{key} source-cache checksum mismatch')
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
        asset='Equity' if rawclass=='Equity' else ('Bond' if rawclass=='Fixed Income' else 'Cash / other')
        if name=='TREASURY BILL': asset='Treasury bill'; sector='Not applicable: Treasury bills'
        elif asset=='Bond': sector='Bond issuer category: '+sector
        elif rawclass in ('Cash','Money Market'): sector='Not applicable: cash / money-market instruments'
        elif rawclass=='Futures': asset='Derivative'; sector='Not applicable: derivatives'
        currency=clean_text(r.get('Market Currency',r.get('Currency','Unknown')))
        location=clean_text(r.get('Location','Unknown'))
        isin=clean_text(r.get('ISIN',''))
        identifier=isin if isin not in ('','-') else name_id(name)
        if asset in ('Bond','Treasury bill') and not isin:
            identifier='BOND:'+name+'|'+clean_text(r.get('Maturity',''))+'|'+clean_text(r.get('Coupon (%)',''))
        if asset=='Cash / other': identifier='CASHLIKE:'+name+'|'+currency
        nested=ticker=='IEFA' or ('ISHARES' in name and name.endswith(' ETF') and asset=='Equity')
        if nested: asset='Unresolved underlying fund'; sector='Unknown: nested fund'
        rows.append(dict(fund=fund,underlying_id=identifier,ticker=ticker,name=name,weight=float(r.weight),
            sector=sector if sector not in ('','-') else 'Unknown',country=location if location not in ('','-') else 'Unknown',
            underlying_currency=currency if currency not in ('','-') else 'Unknown',asset_class=asset,
            asof=asof,source=info['source'],fetched_at_utc=info['fetched_at_utc'],path=path or fund,
            detail='Full public issuer list; weights are rounded and cash/derivatives may be negative. Currency is an underlying quote proxy, not revenue or net hedged exposure.'))
    return rows

def percent_lines(text):
    return [(m.group(1).strip(),float(m.group(2))/100) for m in re.finditer(r'^\s*([^\n%]+?)\s+([\d.]+)%\s*$',text,re.M)]

def official_holdings(a,cfg,p,cash,out,offline=False,refresh=False):
    store=Sources(a,cfg,offline,refresh); allrows=[]; summaries=[]; status=[]
    asof=pd.Timestamp(cfg['positions_asof'])
    for key in ['RPMC','ST_FX','ST_FEES']:
        try: store.get(key)
        except Exception as exc: a.issue('warning','official_rule_source',f'{key}: {exc}; retained verified configuration and source links')
    for fund in ['SGOV','XEF','XBB']:
        if fund not in p.index: continue
        try:
            file,info=store.get(fund+'_CSV'); frame,date=parse_blackrock(file)
            if pd.Timestamp(date)>asof: raise ValueError('Latest holdings postdate the portfolio valuation and cannot represent exposures on that date')
            rows=rows_from_blackrock(frame,fund,date,info)
            if fund=='XEF' and any(r['ticker']=='IEFA' for r in rows):
                nested=[r for r in rows if r['ticker']=='IEFA']; rows=[r for r in rows if r['ticker']!='IEFA']
                try:
                    nf,ni=store.get('IEFA_CSV'); nh,nd=parse_blackrock(nf)
                    if pd.Timestamp(nd)>asof: raise ValueError('IEFA holdings postdate the portfolio valuation')
                    for parent in nested:
                        nr=rows_from_blackrock(nh,fund,nd,ni,'XEF > IEFA')
                        for r in nr: r['weight']*=parent['weight']; r['detail']+='; multiplied by IEFA weight within XEF; parent and child dates recorded separately'
                        rows.extend(nr)
                except Exception as exc:
                    rows.extend(nested); a.issue('warning','nested_fund',f'Unable to look through XEF into IEFA: {exc}')
            allrows.extend(rows)
            status.append(dict(fund=fund,holdings_asof=date,security_weight_coverage=sum(r['weight'] for r in rows),
                list_status='Full public issuer list (rounded coverage may differ from 100%)',source=info['source'],fetched_at_utc=info['fetched_at_utc']))
        except Exception as exc: a.issue('warning','issuer_holdings',f'{fund}: {exc}')
    if 'SPY' in p.index:
        try:
            file,info=store.get('SPY_XLSX'); raw=pd.read_excel(file,header=None)
            date=pd.to_datetime(str(raw.iloc[2,1]).replace('As of ','')).strftime('%Y-%m-%d')
            if pd.Timestamp(date)>asof: raise ValueError('SPY holdings postdate the portfolio valuation')
            h=pd.read_excel(file,skiprows=4); h['weight']=pd.to_numeric(h.Weight,errors='coerce')/100; h=h[h.weight.notna()]
            for _,r in h.iterrows():
                ticker=clean_text(r.Ticker); name=clean_text(r.Name)
                identifier='US:'+ticker if ticker in ('CSCO','CRDO') else 'CUSIP:'+clean_text(r.Identifier)
                allrows.append(dict(fund='SPY',underlying_id=identifier,ticker=ticker,name=name,weight=float(r.weight),sector='Unknown: full list provides no sector',
                    country='Unknown: full list provides no country',underlying_currency=clean_text(r['Local Currency']),asset_class='Equity',asof=date,
                    source=info['source'],fetched_at_utc=info['fetched_at_utc'],path='SPY',detail='Full public issuer list; sector aggregates are separate. Economic country is not inferred from listing venue.'))
            status.append(dict(fund='SPY',holdings_asof=date,security_weight_coverage=h.weight.sum(),list_status='Full public issuer list',source=info['source'],fetched_at_utc=info['fetched_at_utc']))
            page,pi=store.get('SPY'); soup=BeautifulSoup(page.read_text(encoding='utf-8'),'html.parser')
            for table in soup.find_all('table'):
                f=frame_from_table(table)
                if list(f.columns)==['Sector','Weight']:
                    context=table.find_previous(string=re.compile('Sector Breakdown'))
                    # Date comes from the nearby heading, not the acquisition timestamp.
                    header=table.find_previous(['h2','h3','h4']); surrounding=header.parent.get_text(' ',strip=True) if header else ''
                    dates=re.findall(r'(?:as of|As of)\s+([A-Z][a-z]{2} \d{1,2} \d{4})',surrounding)
                    sd=pd.to_datetime(dates[0]).strftime('%Y-%m-%d') if dates else date
                    for _,r in f.iterrows(): summaries.append(dict(fund='SPY',dimension='sector',label=r.Sector,weight=float(str(r.Weight).strip('%'))/100,asof=sd,source=pi['source'],basis='Issuer sector aggregate; date may differ from the full holdings list',fetched_at_utc=pi['fetched_at_utc']))
                    break
        except Exception as exc: a.issue('warning','issuer_holdings',f'SPY: {exc}')
    if 'VEE' in p.index:
        try:
            file,info=store.get('VEE'); soup=BeautifulSoup(file.read_text(encoding='utf-8'),'html.parser')
            text=soup.get_text(' ',strip=True)
            date=pd.to_datetime(re.search(r'Market allocation As at ([A-Z][a-z]{2} \d{1,2} \d{4})',text).group(1)).strftime('%Y-%m-%d')
            if pd.Timestamp(date)>asof: raise ValueError('VEE data postdate the portfolio valuation')
            top=[]
            for table in soup.find_all('table'):
                f=frame_from_table(table)
                if 'Country' in f and 'Fund' in f:
                    for _,r in f.iterrows(): summaries.append(dict(fund='VEE',dimension='country',label=r.Country,weight=float(str(r.Fund).strip('%'))/100,asof=date,source=info['source'],basis='Official underlying equity-allocation proxy; temporary cash excluded; exact VEE/VWO ownership weight unavailable',fetched_at_utc=info['fetched_at_utc']))
                elif 'Holding name' in f:
                    for _,r in f.iterrows():
                        row=dict(fund='VEE',underlying_id=name_id(r['Holding name']),ticker='',name=r['Holding name'],weight=float(str(r['% of market value']).strip('%'))/100,
                            sector=r.Sector,country='Unknown: Region is a listing-market code',underlying_currency='Unknown',asset_class='Equity',asof=date,source=info['source'],fetched_at_utc=info['fetched_at_utc'],
                            path='VEE > issuer underlying proxy (top ten only)',detail='Partial issuer underlying proxy, not a complete VWO/VEE list. Unknown portions remain; the top ten cannot exclude other overlaps.')
                        top.append(row)
            if not top: raise ValueError('Official top-ten underlying holdings table not found')
            allrows.extend(top); status.append(dict(fund='VEE',holdings_asof=date,security_weight_coverage=sum(r['weight'] for r in top),list_status='Top-ten underlying proxy only; full VEE/VWO ownership chain unavailable',source=info['source'],fetched_at_utc=info['fetched_at_utc']))
            pdf,pi=store.get('VEE_FACT_PDF'); text='\n'.join(p.extract_text() for p in PdfReader(pdf).pages)
            fd=pd.to_datetime(re.search(r'Factsheet \| ([A-Za-z]+ \d{1,2}, \d{4})',text).group(1)).strftime('%Y-%m-%d')
            sector=text.split('Sector weighting',1)[1].split('Market allocation',1)[0]
            for m in re.finditer(r'^([^\n]+?)\s+([\d.]+)%\s+[\d.]+%\s*$',sector,re.M):
                if m.group(1).strip()=='Total': continue
                summaries.append(dict(fund='VEE',dimension='sector',label=m.group(1).strip(),weight=float(m.group(2))/100,asof=fd,source=pi['source'],basis='Official VWO underlying ICB sector proxy excluding temporary cash; not an exact VEE balance-sheet look-through',fetched_at_utc=pi['fetched_at_utc']))
            a.issue('info','vee_basis','VEE website and factsheet top-ten weights differ in basis. Securities use the website, sectors use the factsheet, each with its own date/basis. They are not combined into a complete list; exact nested ownership weights remain unavailable.')
        except Exception as exc: a.issue('warning','issuer_holdings',f'VEE: {exc}')
    if 'ZCN' in p.index:
        try:
            file,info=store.get('ZCN_PDF'); text='\n'.join(pg.extract_text() for pg in PdfReader(file).pages)
            date=pd.to_datetime(re.search(r'DATA AS OF ([A-Z]{3} \d{1,2}, \d{4})',text).group(1)).strftime('%Y-%m-%d')
            if pd.Timestamp(date)>asof: raise ValueError('ZCN data postdate the portfolio valuation')
            sec=text.split('Sector Allocation pie chart',1)[1].split('Geographic Allocation',1)[0]
            geo=text.split('Geographic allocation pie chart',1)[1].split('Top Holdings',1)[0]
            for dim,section in [('sector',sec),('country',geo)]:
                for label,w in percent_lines(section): summaries.append(dict(fund='ZCN',dimension=dim,label=label,weight=w,asof=date,source=info['source'],basis='Official fund aggregate; full allocation is not inferred from the top ten',fetched_at_utc=info['fetched_at_utc']))
            top=text.split('Top Holdings',1)[1].split('The portfolio holdings',1)[0]; rows=[]
            for name,w in percent_lines(top): rows.append(dict(fund='ZCN',underlying_id=name_id(name),ticker='',name=name,weight=w,sector='Unknown: top-ten table provides no sector',country='Unknown: top-ten table provides no country',underlying_currency='Unknown',asset_class='Equity',asof=date,source=info['source'],fetched_at_utc=info['fetched_at_utc'],path='ZCN > top ten',detail='Official factsheet contains only the top ten; other security overlaps cannot be excluded'))
            allrows.extend(rows); status.append(dict(fund='ZCN',holdings_asof=date,security_weight_coverage=sum(r['weight'] for r in rows),list_status='Product-page access failed; official factsheet top ten and sector/country aggregates used',source=info['source'],fetched_at_utc=info['fetched_at_utc']))
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
            if valid.empty: raise ValueError('Gold archive has no valid holdings on or before the valuation date')
            latest=valid.iloc[-1]; date=latest.date.strftime('%Y-%m-%d')
            a.csave(valid,out/'gld_official_archive.csv'); a.csave(latest.to_frame('value'),out/'gld_current_official_assets.csv')
            status.append(dict(fund='GLD',holdings_asof=date,security_weight_coverage=0.,list_status=f'Physical gold: {latest.gold_ounces:,.2f} oz; trust net assets USD {latest.trust_net_assets_USD:,.2f}; exact gold/cash/liability split unquantified; structurally classified as gold',source=info['source'],fetched_at_utc=info['fetched_at_utc']))
        except Exception as exc:
            a.issue('warning','gld_archive',f'GLD physical gold details unavailable: {exc}; official structure supports the gold category but the exact gold/cash/liability split is unknown')
            try:
                _,info=store.get('GLD'); status.append(dict(fund='GLD',holdings_asof='Structural classification, not daily holdings',security_weight_coverage=0.,list_status='Gold-trust structure verified; exact asset/liability proportions unknown',source=info['source'],fetched_at_utc=info['fetched_at_utc']))
            except Exception: pass
    h=pd.DataFrame(allrows); summary=pd.DataFrame(summaries); coverage=pd.DataFrame(status)
    if len(h):
        h['portfolio_exposure']=h.fund.map(p.weight)*h.weight
        a.csave(h,out/'etf_lookthrough.csv')
        # Aggregate exact identifiers only; name matches are explicitly lower-confidence keys.
        agg=h.groupby(['underlying_id','name'],dropna=False).portfolio_exposure.sum().reset_index()
        for symbol in p.index:
            if cfg['assets'][symbol]['asset_class']=='Equity':
                agg.loc[len(agg)]=['US:'+symbol,p.loc[symbol].get('Description',symbol),p.loc[symbol,'weight']]
        merged=agg.groupby('underlying_id').agg(name=('name','first'),portfolio_exposure=('portfolio_exposure','sum')).sort_values('portfolio_exposure',ascending=False)
        a.csave(merged,out/'underlying_exposure.csv')
        overlaps=[]
        for symbol in ['CSCO','CRDO']:
            if symbol not in p.index: continue
            indirect=h[h.underlying_id.eq('US:'+symbol)]
            overlaps.append(dict(symbol=symbol,direct_weight=p.loc[symbol,'weight'],identified_indirect_weight=indirect.portfolio_exposure.sum(),
                total_identified_weight=p.loc[symbol,'weight']+indirect.portfolio_exposure.sum(),funds=';'.join(indirect.fund.unique()),
                note='Lower bound of identified exposure; VEE/ZCN security lists are partial and absence does not prove zero exposure elsewhere'))
        a.csave(pd.DataFrame(overlaps).set_index('symbol'),out/'direct_etf_overlap.csv')
    if len(summary): a.csave(summary,out/'issuer_allocations.csv')
    a.csave(coverage,out/'etf_coverage.csv'); a.jsave(out/'issuer_sources.json',store.used)
    exposures=[]
    for dim,col in [('sector','sector'),('country','country'),('underlying_currency','underlying_currency')]:
        for fund,pos in p.iterrows():
            if fund in ['CSCO','CRDO']:
                exposures.append(dict(dimension=dim,label='Unknown: direct-stock '+('sector' if dim=='sector' else 'country') if dim!='underlying_currency' else 'USD',portfolio_weight=pos.weight,basis='Direct stock; USD quotation does not establish its entire economic currency exposure')); continue
            if fund=='GLD':
                exposures.append(dict(dimension=dim,label={'sector':'Not applicable: gold','country':'Not applicable: physical gold','underlying_currency':'Gold (USD-quoted; not equivalent to USD cash)'}[dim],portfolio_weight=pos.weight,basis='Official gold-trust structure; exact balance-sheet composition unquantified')); continue
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
                exposures.append(dict(dimension=dim,label='Unknown / not looked through' if residual>0 else 'Issuer weight-rounding residual (not normalized)',portfolio_weight=pos.weight*residual,basis='Original weight-coverage residual retained'))
        exposures.append(dict(dimension=dim,label='CAD cash' if dim!='sector' else 'Not applicable: cash',portfolio_weight=cash/(p.MarketValue.sum()+cash),basis='Actual account cash'))
    e=pd.DataFrame(exposures); a.csave(e,out/'lookthrough_allocation_detail.csv')
    for dim in e.dimension.unique():
        g=e[e.dimension.eq(dim)].groupby('label').portfolio_weight.sum().sort_values(ascending=False)
        if not np.isclose(g.sum(),1): raise ValueError(f'{dim} look-through weights, including unknown portions, do not sum to 100%')
        a.csave(g.to_frame(),out/f'lookthrough_{dim}.csv')
    a.issue('info','issuer_scope','Issuer holdings describe current exposures, not holdings over the preceding three years. Country is an issuer classification/location proxy, and currency a quotation proxy; neither establishes revenue sources or net hedged economic exposure.')
    a.issue('warning','partial_lookthrough','VEE/ZCN security lists are partial and supplemented by official sector/country aggregates. Undisclosed securities, exact nested ownership and net hedged exposure remain unknown. Rounded full-list residuals are retained rather than normalized.')
    return coverage

def benchmark(a,cfg,prices,nav,boc,out):
    start=pd.Timestamp(cfg['benchmark']['inception']); dates=nav.index[nav.index>=start]
    platform=a.ROOT/cfg['benchmark']['nav_file']; label='Simulated benchmark using official allocation'
    if platform.exists():
        official=pd.read_csv(platform); official['date']=pd.to_datetime(official.date,format='%Y-%m-%d')
        if official.date.duplicated().any() or not official.currency.eq('CAD').all() or (official.value<=0).any(): raise ValueError('Official benchmark NAV has duplicate dates, wrong currency, or nonpositive values')
        s=official.set_index('date').value
        dates=dates.intersection(s.index); b=s.reindex(dates); label='RPMC Benchmark official NAV'
    else:
        components=cfg['benchmark']['weights']
        if not np.isclose(sum(components.values()),1): raise ValueError('Initial benchmark weights do not sum to 100%')
        if not set(components)<=set(prices): raise ValueError('Simulated official-allocation benchmark lacks component CAD prices')
        if any(start not in prices[s].dropna().index for s in components): raise ValueError('Missing benchmark inception-day component prices; another date cannot be substituted')
        panel=pd.DataFrame({s:a.asof_series(prices[s].dropna(),dates,4,f'Benchmark {s} holiday closing-value carry (explicit)')/prices[s].loc[start] for s in components})
        if panel.isna().any().any(): raise ValueError('Benchmark component prices are missing for over four days; benchmark values cannot be fabricated')
        b=panel.mul(pd.Series(components)).sum(axis=1)
        a.csave(panel,out/'benchmark_component_indices.csv')
        a.issue('info','benchmark_assumptions','RPMC Benchmark platform NAV unavailable. Reconstructed from official inception closing CAD weights: buy and hold across ETFs, reinvest distributions within each fund, no cross-fund rebalancing, no separate commissions/tax/FX spreads. These are analytical assumptions, not confirmed official rules. Holiday/weekend closes and corresponding FX explicitly carry for up to four days.')
    if len(dates)<2 or dates.min()!=start: raise ValueError('Benchmark and account lack a common starting valuation')
    compare=pd.DataFrame({'Actual account return':nav.reindex(dates)/nav.loc[start]-1,label:b/b.iloc[0]-1})
    if 'SPY' in prices:
        spy=a.asof_series(prices['SPY'].dropna(),dates,4,'SPY reference holiday closing-value carry')
        compare['SPY additional reference']=spy/spy.iloc[0]-1
    a.csave(compare,out/'actual_benchmark_same_period.csv')
    a.lines(compare,out/'actual_benchmark.png','Actual account vs. RPMC benchmark (CAD, identical dates)\nSimulated benchmark: buy and hold across ETFs, not daily rebalancing','Cumulative return')
    # Sharpe estimate on common market observation dates only, not calendar-day NAV.
    quote_dates=dates
    for s in cfg['benchmark']['weights']: quote_dates=quote_dates.intersection(prices[s].dropna().index)
    q=compare.reindex(quote_dates); summary=[]
    rf=None
    if 'TB.CDN.90D.MID' in boc and len(q)>2:
        y=a.asof_series(boc['TB.CDN.90D.MID'],q.index[:-1]-pd.Timedelta(days=1),7,'CAD risk-free yield published before the competition Sharpe interval')
        days=np.diff(q.index.values)/np.timedelta64(1,'D'); rf=pd.Series((1+y.to_numpy()/100)**(days/365)-1,index=q.index[1:])
    for col in compare:
        r=(1+q[col]).pct_change(fill_method=None).iloc[1:]
        m=a.metrics(r,rf) if len(r)>=2 else {}
        summary.append(dict(series=col,start=str(dates.min().date()),end=str(dates.max().date()),cumulative_return=compare[col].iloc[-1],
            max_drawdown=a.mdd(1+compare[col]),sharpe_estimate=m.get('sharpe',np.nan),market_return_observations=len(r),
            sharpe_sample_start=str(q.index.min().date()),sharpe_sample_end=str(q.index.max().date()),annual_volatility_estimate=m.get('annual_volatility',np.nan)))
    a.csave(pd.DataFrame(summary).set_index('series'),out/'actual_comparison_summary.csv')
    a.issue('info','short_sample_sharpe',f'Competition Sharpe is a short-sample estimate using a CAD T-bill proxy and common market dates ({max(len(q)-1,0)} returns), not the official platform score. Full official scoring sampling/risk-free inputs are undisclosed; weekend NAV remains included in cumulative return and drawdown.')
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
        use_export=False; a.issue('warning','export_cash_test','Inputs failed the amount/interest/commission cash identity; the CAD interpretation of Amount is not carried forward')
    for s,pos in p.iterrows():
        sub=t[t.Symbol.eq(s)].copy(); trade=sub[sub.kind.isin(['buy','sell'])].copy()
        fee=-per_trade*len(trade) if use_export else np.nan
        method='Export/cash-reconciliation-supported estimate (not independently confirmed settlement)' if use_export else 'Public Bank of Canada daily FX estimate'
        if use_export:
            trade['fx_CAD_per_local']=trade.amount_numeric.abs()/(trade.Quantity*trade.price_numeric)
        elif pos.Currency=='CAD': trade['fx_CAD_per_local']=1.
        elif 'FXUSDCAD' in boc:
            trade['fx_CAD_per_local']=a.asof_series(boc['FXUSDCAD'],pd.DatetimeIndex(trade.date.dt.normalize()),4,'Actual ledger public-FX estimate').to_numpy()
        else:
            a.issue('warning','accounting_fx',f'{s}FX estimate unavailable; left NA'); continue
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
            commission_currency_supported='CAD' if use_export else 'unknown',status='Supported by whole-account cash reconciliation; Session Rules and trade-level CAD fields still need verification' if use_export else 'Unconfirmed'))
    a.csave(pd.DataFrame(details),out/'transaction_settlement_diagnostics.csv')
    reconciliation=pd.DataFrame({'CAD':{'Initial capital':initial,'Total exported purchase Amount (original signs)':amounts,'Confirmed dividends':dividends,'Total commission (user-provided evidence)':-commission,
        'Summary interest amount (cumulative interpretation requires cash-reconciliation support)':interest,'Actual cash':cash,'Cash reconstruction residual':cash_residual,
        'Total estimated net asset contribution':frame.net_contribution_CAD_estimate.sum(min_count=len(frame)),
        'Actual account net P/L':nav.iloc[-1]-initial,
        'Estimated net contribution + interest - actual P/L':frame.net_contribution_CAD_estimate.sum(min_count=len(frame))+interest-(nav.iloc[-1]-initial)}})
    a.csave(reconciliation,out/'cash_and_pnl_bridge.csv')
    if use_export:
        a.issue('info','cash_reconstruction',f'Pre-commission CAD Amount interpretation passes cash reconciliation; interest {interest:.2f}, commissions {commission:.2f}, residual {cash_residual:.6f} CAD. Reconciliation-supported estimate, not independently confirmed trade-level settlement.')
        a.issue('warning','session_rules',f'This run: {len(buys)} trades; estimated commission per trade {per_trade:.2f} CAD bound only to the configured transaction-file checksum; actual rules require this competition Session Rules and are not assumed for future trades.')
    else: a.issue('warning','estimate_costs','CAD costs and price/FX attribution use public daily FX estimates, not exact platform settlement. Net contribution remains unavailable if fees are unknown; actual NAV performance remains computable.')
    return frame

# Presentation is separate from accounting and source acquisition.
from reporting import render_report
