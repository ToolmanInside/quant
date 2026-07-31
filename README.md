# Quant Lab

GitHub Actions 日终模拟与企业微信推送配置见
[docs/github-actions.md](docs/github-actions.md)。

面向 Windows 本地运行的量化研究、回测与模拟交易系统：

- Tushare Pro 日线与复权因子适配
- Tushare Pro 全市场行情、估值、财务质量、换手率和资金流
- Bocha 新闻检索作为低权重事件校验与重大风险否决
- 双均线趋势策略
- 策略 × 交易频率二维评测（双均线、价格动量、通道突破、RSI均值回归）
- 日频、5交易日、20交易日统一成本回测与相对评分
- 最后 30% 时间样本外评分、标的覆盖率和失败明细
- 次日开盘成交，避免未来函数
- A 股整手、佣金最低收费、印花税与滑点模型
- 一字涨跌停、停牌/零成交量拒单及合法成交价约束
- 现金分红、送转股与除权日经济成本基准处理
- 计划/实际成交、部分成交、整手与现金约束审计
- 每日中短期趋势组合、板块与个股优选
- 每个账户一个 UTF-8 文本账本和下一交易日买入/卖出/平仓计划
- 每天 12:00 午间盘位报告与 18:00 日终分析/计划推送
- 数据、执行、策略和市场环境异常归因
- 冠军—挑战者样本外评估与受约束自动晋级
- FastAPI 本地服务
- 可交互的本地网页工作台

当前版本只做研究、回测和模拟交易，不包含 MiniQMT 连接，也不会发送真实订单。

## 环境

- Windows 10/11
- Node.js：`C:\Program Files\nodejs`
- 系统 Python 3.14（项目会创建独立的 `.venv`）

## 首次安装

在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

所有设置只读取 `config/quant-config.json`。在这里调整策略、全市场/固定股票池、
因子权重、资金、日期和其他运行参数；Tushare Token、Bocha API Key 与企业微信
Webhook 保留环境变量占位符，由本地 `.env` 或 GitHub Secrets 注入。密钥只由
Python 任务读取，不会返回给浏览器，也不会写入日志和账户文本。

## 启动

可以双击：

```text
start-quant-lab.cmd
```

也可以运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

打开：

- 工作台：http://127.0.0.1:3000
- API 文档：http://127.0.0.1:8000/docs

后端和网页日志会直接显示在当前窗口中。按 `Ctrl+C` 同时停止两个服务。

已有服务不会自动加载新代码。更新项目文件后，请在前台窗口按 `Ctrl+C`，
再重新双击 `start-quant-lab.cmd`。

## 每日模拟交易

网页的“中短期趋势组合驾驶舱”支持：

- 首次初始化并逐日历史推演（会重置同名虚拟账户，并为模拟期每天生成日志）
- 分别设置回测起点/终点和模拟盘起点；回测期不动模拟资金
- 可选择双均线趋势、价格动量或通道突破，三者均为日频
- 前台服务运行时，每天 18:00 后自动尝试一次，也支持手动运行
- 下一交易日开盘模拟执行买入、卖出/减仓、平仓
- 同一交易日重复运行不重复成交
- 每个模拟交易日都记录回顾、市场分析、下一日决策和复盘结论
- 查看持仓、权益、市场宽度、优选板块、异常反思和版本评估
- 日终先扫描全部上市 A 股，再对少量候选拉取历史行情运行日频策略
- 漏跑多个交易日时逐日重建当时的全市场截面，不复用今天的候选池
- 日报展示估值、盈利质量、换手、资金流因子覆盖率和可追溯新闻链接

每个本地账户分别保存在
`backend/data/simulation/accounts/<account_id>.txt`，均已加入
`.gitignore`。文件是可直接阅读的 JSON 文本，包含账户、持仓、公司行为、成交、每日复盘和
下一交易日计划。完整策略口径和升级门槛见
[docs/paper-trading.md](docs/paper-trading.md)。

每日调度属于当前前台启动进程；关闭窗口或按 `Ctrl+C` 后不会遗留后台任务。
自动运行和手动点击共用交易日幂等保护，失败的完整异常会打印到命令行。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests_py -q
& "C:\Program Files\nodejs\npm.cmd" run lint
& "C:\Program Files\nodejs\npm.cmd" run build
& "C:\Program Files\nodejs\node.exe" --test tests\rendered-html.test.mjs
```

## 目录

```text
backend/                 Python API、Tushare数据源、策略与回测
app/                     本地网页界面
tests_py/                Python 单元测试
tests/                   前端渲染测试
scripts/                 Windows 安装、启动与停止脚本
```

## 下一阶段

1. 将逐日全市场截面落地到 Parquet，支持无幸存者偏差的历史全市场回测。
2. 补充涨跌停、停牌拒单后的跨交易日订单排队与撤单策略。
3. 补充逐日 ST 状态、历史行业成分和财务公告时点修订。
4. 对现金分红税率与到账日、送转上市日做更严格的券商结算级模拟。
5. 最后再接入 MiniQMT `xttrader` 仿真交易；历史行情仍由 Tushare 提供。
