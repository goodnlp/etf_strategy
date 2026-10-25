# ETF 月度动量轮动

标的：QQQ、VOO、VTV、VXUS、GLDM、XLE、IEF、SGOV。

## 规则与约定

- 月末最后一个实际交易日收盘计算信号，下一交易日收盘成交。月末当日收盘成交会使用尚不可得的收盘信号，本实现延迟一个交易日。
- 动量使用 Yahoo Finance 的 Adj Close（分红及拆股调整），计算截至信号日的 3、6、12 个日历月收益，权重 30%、40%、30%。历史日期非交易日时取之前最近一个收盘。
- 风险候选不包括 SGOV。综合分数严格高于 SGOV，且未进行分红调整的 Close 严格高于其最近 200 个交易日均线，才可入选。
- 取分数前三名；同分按标的列表顺序。先各分配 35%，总和超过 100% 时按比例缩减，再将股票类总仓位超过 70% 的部分按比例缩减。余款配置 SGOV，不再递补其他资产。
- 股票类为 QQQ、VOO、VTV、VXUS、XLE。单只 35% 上限仅适用于风险资产；SGOV 作为剩余现金替代品可达 100%。上限在调仓时约束目标权重，月中允许随市值漂移。
- 初始资金在 SGOV，第一笔可执行信号出现后调仓；首日以收盘净值为起点，此前不计收益。无杠杆、允许碎股、不计税。
- 默认按买卖成交金额各收 10 bps，SGOV 交易也计费；100% 换仓成本约为净值 20 bps。作为实验成本假设，不代表实际券商收费。
- VOO 和 SGOV 买入持有为对照，分红再投资。Sharpe 使用零无风险利率，非超额于 SGOV 的 Sharpe。
- 默认截至运行当天，向前五个日历年，另取 13 个月预热。不完整的最后一个月不产生月末信号，不补齐缺失行情。

## 运行

```bash
cd /workspace/etf_strategy
python -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python backtest.py --start 2021-10-25 --end 2026-10-25
```

需允许 HTTPS 访问 `query1.finance.yahoo.com`、`query2.finance.yahoo.com`、`fc.yahoo.com`、`finance.yahoo.com`。成功下载后可用 `--offline` 重放本地缓存，用 `--cost-bps 0` 做零成本对照。缓存路径固定，离线运行前应确认日期覆盖所需区间。Yahoo 免费数据可能有修订，实际研究应核对权威行情；metadata.json 记录缓存 SHA256 及实际起止日期。

输出：`results/summary.csv`（收益、年化、波动、最大回撤、Sharpe）、`annual_returns.csv`（首尾年度可能不足全年）、`equity.csv`、`rebalances.csv`、`signals.csv`、`metadata.json`、`equity.png`。

## 当前实验结果

真实行情已下载并完成回测。数据缓存为 `data/prices.csv`，覆盖八只 ETF，含 Adj Close、Close 和分红事件。正式评估区间为 2021-10-25 至 2026-10-23；10 月 25 日为非交易日。结果见 [回测报告](results/REPORT.md)。

可使用 `.venv/bin/python backtest.py --offline --start 2021-10-25 --end 2026-10-25` 复现本地缓存结果。最新下载可能因数据源修订而变化。当前结果只验证了本实例，尚未验证新任务中的快照恢复。
