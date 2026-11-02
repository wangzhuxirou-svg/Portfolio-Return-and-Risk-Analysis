import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import main as a
import enrichment as e

def test_cli_runs_offline_with_synthetic_inputs(tmp_path):
    """A clean clone must produce an honest partial report without private caches."""
    import subprocess
    raw=tmp_path/'raw'; raw.mkdir()
    pd.DataFrame([dict(Symbol='ZCN',Quantity=10,Currency='CAD',LastPrice=21,PricePaid=20,MarketValue=210)]).to_csv(raw/'positions.csv',index=False)
    pd.DataFrame([dict(Symbol='ZCN',TransactionType='Market - Buy',Exchange='T',Quantity=10,Currency='CAD',Price='C$20',Amount='-C$200',CreateDate='10/22/2026 - 12:00')]).to_csv(raw/'transactions.csv',index=False)
    pd.DataFrame([{'Date':'10/8/2026 12:00:00 AM','Value':1000},{'Date':'11/1/2026 12:00:00 AM','Value':1000}]).to_csv(raw/'nav.csv',index=False)
    (raw/'summary.csv').write_text('Date:,11/1/2026\nCash Balance:,C$790,Market Value of Long Positions:,C$210,Portfolio Value:,C$1000\nLoan Balance:,C$0,Market Value of Short Positions:,C$0\nInterest Earn Rate:,C$0\n',encoding='utf-8')
    cfg=json.loads((a.ROOT/'config.example.json').read_text(encoding='utf-8'))
    cfg.update(input_dir=str(raw),clean_dir=str(tmp_path/'clean'),cache_dir=str(tmp_path/'cache'),output_dir=str(tmp_path/'reports'),external_flows='none',initial_capital_CAD=1000)
    cfg['assets']={'ZCN':cfg['assets']['ZCN']}
    config=tmp_path/'config.json'; config.write_text(json.dumps(cfg),encoding='utf-8')
    result=subprocess.run([sys.executable,str(a.ROOT/'main.py'),'--config',str(config),'--offline'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr+result.stdout
    latest=Path((tmp_path/'reports/LATEST.txt').read_text(encoding='utf-8'))
    assert (latest/'report.html').exists()
    actual=json.loads((latest/'actual_summary.json').read_text(encoding='utf-8'))
    assert actual['cumulative_change']==0
    assert 'flows_unknown' not in (latest/'validation.csv').read_text(encoding='utf-8-sig')

def test_currency_parser_preserves_sign():
    assert a.money('-C$1,234.50')==-1234.5
    assert a.money('($12.50)')==-12.5
    assert a.money('C$451.0000')==451

def test_dividend_not_a_purchase_and_fifo():
    t=pd.DataFrame({'date':pd.to_datetime(['2026-01-01','2026-01-02','2026-01-03','2026-01-04']),
        'source_row':[2,3,4,5],'Quantity':[100,50,150,120],'price_numeric':[10,12,.5,15],
        'kind':['buy','buy','dividend','sell']})
    realized,unrealized,cost=a.fifo(t,30,14)
    assert realized==560  # 100*5 + 20*3
    assert unrealized==60 and cost==360

def test_external_deposit_is_not_profit():
    dates=pd.date_range('2026-01-01',periods=3)
    nav=pd.Series([100,160,176],index=dates)
    f=pd.DataFrame([{'date':'2026-01-02','amount':50,'currency':'CAD','timing':'end_of_day'}])
    r,_=a.nav_returns(nav,f,'provided')
    np.testing.assert_allclose(r.iloc[1:],[.1,.1])
    assert (1+r.dropna()).prod()-1==pytest.approx(.21)

def test_flow_timing_cannot_be_guessed():
    nav=pd.Series([100,150],index=pd.date_range('2026-01-01',periods=2))
    f=pd.DataFrame([{'date':'2026-01-02','amount':50,'currency':'CAD','timing':'intraday'}])
    with pytest.raises(ValueError): a.nav_returns(nav,f,'provided')

def test_drawdown_includes_initial_capital():
    r=pd.Series([-.2,.1],index=pd.date_range('2026-01-01',periods=2))
    m=a.metrics(r)
    assert m['max_drawdown']==pytest.approx(-.2)
    assert m['cumulative_return']==pytest.approx(-.12)
    assert np.isnan(m['sharpe'])

def test_fx_uses_each_date_and_no_future_fill():
    d=pd.date_range('2026-01-01',periods=3)
    s=pd.Series([1.3,1.4],index=d[1:])
    fx=a.asof_series(s,d,4,'test')
    assert np.isnan(fx.iloc[0])
    assert (100*fx.iloc[2])/(100*fx.iloc[1])-1==pytest.approx(1.4/1.3-1)


def test_risk_contribution_identity():
    cov=np.array([[.04,.01],[.01,.09]])
    w=np.array([.4,.5]); sigma=np.sqrt(w@cov@w)
    rc=w*(cov@w)/sigma
    assert sum(rc)==pytest.approx(sigma)

def test_verified_fx_decomposition_and_fees():
    t=pd.DataFrame({'date':pd.to_datetime(['2026-01-01','2026-01-02']), 'source_row':[2,3],
        'Quantity':[10,4],'price_numeric':[100,110],'kind':['buy','sell'],'fx_CAD_per_local':[1.3,1.4]})
    realized,unrealized,price,fx=a.fifo_cad(t,6,120,1.5)
    assert realized==pytest.approx(96)
    assert unrealized==pytest.approx(300)
    assert price+fx==pytest.approx(396)
    # Fees are signed cash effects, applied once outside gross lot P&L.
    assert realized+unrealized+(-5)+(-3)==pytest.approx(388)

def test_no_external_flows_actual_return():
    nav=pd.Series([1000,1001,990],index=pd.date_range('2026-10-08',periods=3))
    r,label=a.nav_returns(nav,pd.DataFrame(),'none')
    assert (1+r.dropna()).prod()-1==pytest.approx(-.01)
    assert '已确认无外部出入金' in label

def test_benchmark_is_buy_and_hold_and_same_dates(tmp_path):
    from types import SimpleNamespace
    dates=pd.to_datetime(['2026-10-09','2026-10-12','2026-10-13'])
    cfg={'benchmark':{'inception':'2026-10-09','weights':{'A':.5,'B':.5},'nav_file':'missing.csv'}}
    prices={'A':pd.Series([100.,200.,100.],index=dates),'B':pd.Series([100.,100.,200.],index=dates)}
    ctx=SimpleNamespace(ROOT=tmp_path,asof_series=a.asof_series,csave=a.csave,lines=a.lines,metrics=a.metrics,mdd=a.mdd,issue=a.issue)
    result=e.benchmark(ctx,cfg,prices,pd.Series([100.,100.,100.],index=dates),{},tmp_path)
    assert result['benchmark']==pytest.approx(.5)  # Daily fixed-weight rebalancing would give .875.
    compare=pd.read_csv(tmp_path/'actual_benchmark_same_period.csv',index_col=0)
    assert len(compare)==3 and compare.iloc[0].eq(0).all()
