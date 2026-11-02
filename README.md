# Portfolio Return and Risk Analysis

A repeatable Python analysis tool for a StockTrak stock, ETF and cash portfolio, with CAD as the reporting currency. Actual account performance is kept separate from a hypothetical historical simulation of current holdings. Reports are generated in Chinese.

**历史结果不是未来收益预测。仓库不包含真实账户数据、账户配置、缓存或已生成报告。**

## 功能

- 从 StockTrak 持仓、完整交易、每日净值及账户汇总 CSV 读取数据；保留源文件，另存清洗结果。
- 用真实净值计算每日/累计收益及最大回撤；外部出入金状态需明确确认，支持已确认日末资金流调整。
- 区分买卖与分红，FIFO 核算，并检查交易数量、持仓市值、现金、借款和净值对账。
- CAD 成本/汇率/贡献估算明确标记，费用未知不填零；费用证据须绑定原始交易文件 SHA256。不得把自动换汇当作已知实际成交汇率。
- 自动下载约三年的拆股/分红调整行情、USD/CAD、加拿大三个月国库券收益率；缓存来源、获取时间、实际窗口及对齐审计，失败不编造。
- 当前权重历史模拟：波动率、回撤、Sharpe、相关性、资产权重和 Euler 风险贡献。
- 从 BlackRock、Vanguard、BMO、State Street 官方获取 ETF 穿透资料；部分名单、日期、未知暴露及嵌套基金均注明。
- 输出中文 HTML、收益/基准对比、回撤、相关性热力图、权重及风险贡献图，以及可审计 CSV/JSON。

## 安装（Python 3.10 或更高版本）

```powershell
git clone https://github.com/wangzhuxirou-svg/Portfolio-Return-and-Risk-Analysis.git
cd Portfolio-Return-and-Risk-Analysis
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
New-Item -ItemType Directory -Force data/raw | Out-Null
```

macOS / Linux 使用 `python3 -m venv .venv`，随后通过 `.venv/bin/python` 运行同一命令。Windows 默认包含中文字体；其他系统如图中中文缺字，请安装支持中文的字体并修改 main.py 的字体设置。

## 放入自己的输入文件

从 StockTrak 重新导出比赛开始至今的完整资料，保存为：

| 文件 | 必要字段 / 口径 |
|---|---|
| `data/raw/positions.csv` | Symbol, Quantity, Currency, LastPrice, PricePaid, MarketValue；价格为交易币种，MarketValue已为CAD |
| `data/raw/transactions.csv` | Symbol, TransactionType, Exchange, Quantity, Currency, Price, Amount, CreateDate；时间格式 `MM/DD/YYYY - HH:MM` |
| `data/raw/nav.csv` | Date, Value；日期格式 `MM/DD/YYYY hh:mm:ss AM/PM`，Value为CAD账户净值 |
| `data/raw/summary.csv` | StockTrak原生多列键值格式，包含Date、Cash Balance、Loan Balance、Market Value of Long Positions、Market Value of Short Positions、Portfolio Value |

templates 提供空表头。原文件不应提交到GitHub；每次替换前可自行归档备份。不要将新导出的完整交易表追加到旧表，否则会重复计算。

## 配置与再次运行

在本地 `config.json` 中确认：

- `positions_asof`：真实持仓估值日期，尽量与Summary同日。
- `initial_capital_CAD`：实际初始资金。
- `external_flows`：默认 `unknown`；确认没有额外出入金后改 `none`；有额外出入金则 `provided`，并填写 `data/external_flows.csv`（实际CAD金额、明确日末时点）。证券买卖、分红和利息不是外部出入金。
- `assets`：每项资产的Yahoo代码、币种、StockTrak交易所及Yahoo交易所白名单。示例证券仅用于展示映射，不代表仓库附带真实持仓。
- `accounting_evidence`：示例默认没有已核实佣金证据。只有取得本场比赛相关证据时才填写总额、每笔金额及对应交易CSV的SHA256；不能套用其他比赛默认规则。

```powershell
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe main.py --offline
.\.venv\Scripts\python.exe main.py --refresh
.\.venv\Scripts\python.exe -m pytest -q
```

默认联网并复用缓存；`--offline`只读缓存；`--refresh`重新下载。当前发行商资料日期晚于持仓日时会拒绝用于该历史日敞口；分析旧日期请保留当时缓存。新结果写入 `outputs/<运行时间>/report.html`，`outputs/LATEST.txt`指向最新目录。先查看校验记录；退出码0不表示所有市场数据或指标均已齐全，致命输入错误返回2。

## 基准

示例包含 [RPMC官方配置](https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/finance-research-and-trading-lab/events/rpmc/)：2026-10-09收盘，SPY 35%、XIC.TO 15%、IEMG.K 20%、XBB.TO 20%、GLD 10%。IEMG.K按发行商基金名称映射为行情代码IEMG。

优先读取 `data/official_benchmark_nav.csv`（date,value,currency，CAD）。没有平台RPMC Benchmark净值时，标为“官方配置的模拟基准”：跨基金买入持有、不再平衡、基金内部总回报分红再投资，不另计交易佣金/税/换汇点差。这些是分析假设，不是已确认官方规则。SPY为额外参考，不能单独冒充官方基准。账户与基准使用相同起止日期。

## 方法与限制

- 真实账户：用实际净值，保留周末；分红、利息和费用已在净值中时不再加减。资金流未知时只称净值变化，不称资金流调整后的投资收益。
- 当前权重历史模拟：每日固定权重再平衡、CAD现金零收益、无交易费用/税/冲击；不是买入持有，也不是实际比赛历史收益。
- Yahoo Adj Close作为拆股及分红调整总回报代理，分红不另加。USD历史价格按对应日期央行FXUSDCAD转换；公开日均FX不冒充平台成交FX。证券价格不填充，FX最多显式沿用4日内既往值并留审计。
- 所有资产使用共同历史窗口；美加休市差异导致剔除日期，252年化为近似。波动率=样本标准差×√252；Sharpe=超额区间收益均值/样本标准差×√252。无风险代理TB.CDN.90D.MID取区间开始前已发布报价收益率并按实际天数/365折算。
- 比赛期间Sharpe只是短样本估计，不能声称等于平台官方评分。历史最大回撤和相关性不能预测未来。
- 当前ETF持仓只用于当前敞口，不能假装代表过去三年持仓。完整公开CSV/XLSX仍有舍入残差；VEE/ZCN可能只有部分证券清单，辅以官方行业/国家汇总。保留未知，不把前10当全部持仓。
- 底层报价币种不等于收入币种或净套保后的经济敞口；CAD上市国际ETF仍可能有外汇风险。黄金、债券、国库券和现金不强行归入股票行业。
- 第一版只支持多头股票/ETF和现金，不支持杠杆/空头；拆股、转仓及公司行动需要完整账本，数量不符时不猜测修复。

之前针对特定账户日期制作的固定英文译本及其导出脚本未包含在仓库，避免发布账户快照。这里提供可重复运行的中文分析程序；报告由使用者本地生成。

## 项目结构

```text
main.py                 输入校验、行情缓存、收益及风险计算
enrichment.py           官方ETF资料、RPMC基准、CAD估算与中文报告
config.example.json     无账户结算证据的示例配置
templates/              输入CSV模板
tests/                  不依赖真实账户数据的计算验证
requirements.txt        依赖范围
requirements-lock.txt   本次验证的精确环境版本
```

主要官方参考：[StockTrak自动换汇](https://www.stocktrak.com/faq/want-trade-foreign-markets/)、[佣金由管理员设定](https://www.stocktrak.com/faq/brokerage-commission-determined/)、[IEMG发行商](https://www.ishares.com/us/products/244050/ishares-msci-emerging-markets-etf)、[Bank of Canada Valet](https://www.bankofcanada.ca/valet/docs)。每次下载的实际URL、日期和UTC获取时间另存于报告目录，下载失败会显式说明。
