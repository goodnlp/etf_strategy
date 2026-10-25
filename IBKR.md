# 接入 IBKR 自动执行

实现使用 `ib_async` 连接 TWS / IB Gateway 的原生 Socket API，复用 `strategy.target_weights`，没有引入之前讨论但尚未验证的策略优化。代码、离线信号预览和模拟接口测试已完成；本项目尚未连接真实 IBKR 会话，也未提交任何真实或模拟账户订单。

## 账户与运行条件

- 在运行机器上安装并登录 IB Gateway 或 Trader Workstation（TWS）。登录和双因素验证由用户完成；程序不保存或代填 IBKR 密码，不处理自动登录。
- 在 TWS/Gateway 的 API 设置中启用 Socket 客户端，确认端口、允许的客户端地址。只做预览时可以启用 Read-Only API；发送模拟盘订单时需要关闭该选项。
- 本实现支持标准 DU 前缀模拟账户，以及显式启用的实盘账户。机构／顾问账户分配、组合账户和其他模拟账户命名形式没有适配。
- 使用 USD 基础货币的独立账户；非零持仓只能是八只策略 ETF，不能有空头、期权或碎股。不能与人工交易、其他机器人共享账户。仅设置资金上限不会把共享账户中的原有 ETF 持仓排除在管理之外。
- 需要八只 ETF 的交易权限、有效的实时 bid/ask 行情订阅及 API 行情权限。延迟／冻结行情不能用于下单。报价时间采用 API 更新接收时间，并非独立的交易所时间戳。Yahoo 行情只计算历史信号，不作为实际订单报价。
- 交易资金只取 USD 总现金、已结算现金和可用资金三者的最小值。不会自动兑换外汇，也不依赖 BuyingPower 借钱。账户为保证金账户，也仍执行这个资金约束。
- 必须有持续可用、与 Gateway 网络连通的机器及持久化磁盘。云环境中 localhost 指云机器本身，不能直接连接你电脑上的 TWS。Socket API 不经过普通 HTTPS 代理；应在 Gateway 所在机器部署，或使用已配置且可达的私有网络。不要把 TWS API 端口暴露到公网。

## 安装和离线预览

Python 3.11+；仓库当前环境使用 Python 3.12。

```bash
cd /workspace/etf_strategy
.venv/bin/python -m pip install -r requirements-live.txt
cp ibkr.example.toml ibkr.local.toml
chmod 600 ibkr.local.toml
.venv/bin/python -m pytest -q

# 不连接 IBKR，使用已有行情计算历史信号和假设全现金账户的交易计划
.venv/bin/python -m ibkr_bot preview --config ibkr.local.toml \
  --signal-date 2026-09-30 --prices-csv data/prices.csv --nav 10000
```

`preview` 明确使用信号日收盘价，不是实时可执行报价；它不会建立 IBKR 连接、提交订单或消耗当月执行记录。`signal` 命令只输出分数和权重：

```bash
.venv/bin/python -m ibkr_bot signal --config ibkr.local.toml \
  --signal-date 2026-09-30 --prices-csv data/prices.csv
```

省略 `--prices-csv` 时下载行情。历史数据必须覆盖完整的十三个月交易日日历，并包括精确的月末收盘；缺失行情会中止。`run` 不允许从命令行覆盖信号日期，避免把历史信号误用于当前执行。

## 连接模拟盘

编辑 `ibkr.local.toml` 中的 `account` 为你的 DU 模拟账户，或在运行环境中设置 `IBKR_ACCOUNT`。无需 GitHub Token 或 IBKR API Key：认证来自已登录的 TWS/Gateway 会话。配置文件和 runtime 目录均被 Git 忽略。

端口通常为：TWS 模拟 7497、实盘 7496；Gateway 模拟 4002、实盘 4001。以实际 API 设置为准，端口本身不能用来判断是否为模拟盘。程序还会核对账户及模式。

```bash
# 默认不会下单；只在当月执行窗口内连接、核对实时持仓和行情并生成计划
.venv/bin/python -m ibkr_bot run --config ibkr.local.toml

# 同一窗口内允许提交模拟盘订单
.venv/bin/python -m ibkr_bot run --config ibkr.local.toml --execute

# 查看本地记录，或连接 IBKR 核对当前持仓、活动订单、近期成交
.venv/bin/python -m ibkr_bot status --config ibkr.local.toml
.venv/bin/python -m ibkr_bot reconcile --config ibkr.local.toml
```

不在窗口内时 `run` 返回 `not_due`，不会下载数据、连接账户或下单。因此在周末运行只会得到排期信息，不能当作连接验证；可用 `reconcile` 验证实际连接。

## 执行时点及与回测的差异

- 月末最后一个 XNYS 交易日收盘形成信号；下个月第一个交易日执行。日历处理节假日、夏令时和提前收盘。
- 默认在收盘前 25 分钟至前 5 分钟之间启动和提交订单。常规交易日为纽约时间 15:35–15:55，夏令时／标准时自动处理。
- 采用可成交限价单：买入限价为 ask 加 10 bps，卖出为 bid 减 10 bps，按合约最小价格单位取整。仅正常交易时段、DAY 有效。不使用市场单、MOC、杠杆或碎股。
- 所有卖单先执行，再买入风险 ETF，最后补 SGOV。每次只发一单，要求完整成交，刷新行情及持仓后再继续。
- 实际执行使用限价、完整股数、资金上限和现金缓冲，不等于回测的精确收盘价、满额投资、碎股和立即再投资。下单期间价格变化可能导致实际权重偏离目标；没有保证收益或成交的机制。

默认最多管理 **10,000 美元**，并预留该金额的 **1% 现金**。不是自动投入全部账户资金。改变 `capital_limit`、`max_order_usd` 和现金缓冲可调整规模。现金缓冲独立于策略中的 SGOV：即使策略目标 SGOV 为零，也保留这部分真实现金。小于 10 美元的差额不交易。

**结算约束：** 现金账户卖出后资金可能直到 T+1 才结算。同日买入可能只执行一部分或完全跳过；程序保留现金并记录 `completed_with_shortfall`，退出码 2，不会借款或第二天自动补单。默认当月不自动重试这种结果。不要把模拟盘资金表现视为实盘结算的保证；如需严格实现满额同日换仓，应先在目标账户验证结算行为，再单独设计资金安排。

## 自动调度

`deploy/` 提供 systemd **用户级** service/timer，每分钟检查一次窗口。示例启动参数为模拟盘 `--execute`，不会自动开启实盘。安装前确认路径、账户、端口、资金上限和模拟盘连接已验证：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/etf-ibkr.service deploy/etf-ibkr.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now etf-ibkr.timer
journalctl --user -u etf-ibkr.service -f
```

若需要环境变量，可把 `IBKR_ACCOUNT=DU...` 放在 `runtime/ibkr.env` 并限制文件权限。示例所有绝对路径为 `/workspace/etf_strategy`，在其他机器上安装前应一起修改；不用系统级 root 服务。需确保用户服务在退出登录后仍可运行，且 Gateway 持续登录。不会在这次代码编写中自动安装或启用 timer。

成功月份由 SQLite 标记，之后的分钟任务不再发单。相同账户只允许一个机器人实例，所有实例必须共用同一个持久化 `state_dir` 和唯一 `client_id`；本地文件锁不能协调多台机器。不要删除 runtime 或通过更换目录来绕过重复执行保护。Broker API、网络或计划异常时命令退出码 1，需要检查日志，程序不发送邮件或即时消息。

## 订单中断与恢复

`runtime/ibkr/journal.sqlite` 在提交订单**之前**保存意图，之后记录 broker order ID、状态和成交量。部分成交、拒单、超时、无法确认取消或断线都会停止本轮；未解决的失败记录也会阻止之后月份自动执行。

```bash
# 停止继续提交新订单；不会自动撤销已经发出的活动订单
mkdir -p runtime/ibkr
touch runtime/ibkr/STOP
systemctl --user stop etf-ibkr.timer

.venv/bin/python -m ibkr_bot status --config ibkr.local.toml
.venv/bin/python -m ibkr_bot reconcile --config ibkr.local.toml
```

订单超时会请求取消并等待确认。断线或进程被杀时不能保证撤单成功。应在 TWS 中核对活动单、成交和持仓，必要时人工取消。不能因为 API 没返回某笔订单就认定它没提交：历史查询可能只覆盖近期订单／成交。

人工审核完毕、确认不存在工作订单后，才使用：

```bash
.venv/bin/python -m ibkr_bot resolve --config ibkr.local.toml \
  --run-id <失败记录ID> --confirm-broker-audited
rm runtime/ibkr/STOP
```

`resolve` 不下单，只标记人工核对完成并保存核对快照。之后仍须在原执行窗口内运行 `run --execute`，从实际持仓重新计算剩余交易；已错过窗口则不自动追单，需人工处理并等待下月。`completed_with_shortfall` 属于已结束月份，不能通过此命令重开。恢复后如需自动调度，重新启动 timer。

## 实盘配置

代码支持实盘，但必须同时满足：私有配置 `mode="live"`、`allow_live=true`、正确实盘账户／端口，以及命令 `--execute --live`。仅修改端口不会启用实盘。实盘的 reconcile/resolve 也要求 `--live`。

```bash
.venv/bin/python -m ibkr_bot run --config ibkr.local.toml --execute --live
```

示例 timer 没有 `--live`，不会因为配置切到实盘就直接下单。启用前应先在模拟盘验证一次完整的月度调仓、订单中断恢复、行情和资金结算，再修改自己的部署配置。本次尚无 IBKR 会话可供这些账户级验证。
