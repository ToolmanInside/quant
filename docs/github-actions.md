# GitHub Actions 日终模拟任务

> 午间报告只反映最近一次完成的日线账本，不包含当天尚未由18:00日终任务回填的
> 开盘成交；报告会列出待执行目标总仓位和执行时点。

仓库内的 `.github/workflows/daily-paper-trading.yml` 包含两个北京时间任务：

- **12:00 午间盘位报告**：只读取最近一次成功保存的账户 TXT，推送当前持仓、
  仓位、最近权益、当前/历史最高收益、回撤和已生成的下一步计划。它不请求新的
  日线、不推进策略，也不产生模拟成交。
- **18:00 日终任务**：执行以下完整流程：

1. 恢复上一次运行的账户文本和 Tushare 行情缓存。
2. 获取截至北京时间当天的全市场日线、估值、财务质量、换手率和资金流快照。
3. 扫描全部上市 A 股，形成板块排名和详细候选池，再运行日频趋势策略。
4. 使用 Bocha 检索领先板块和重点候选的近期新闻，进行低权重校验和重大风险否决。
5. 生成下一交易日的买入、卖出、减仓或平仓计划。
6. 将当前持仓、当前收益、历史最高收益、回撤、全市场研究和下一步计划推送到飞书群机器人。
7. 将 Markdown 日报和账户 TXT 备份为 GitHub Actions Artifact，保留 90 天。

午间报告中的权益和收益来自最近一次完成的日线模拟快照，不是盘中实时价格或实时
盈亏，消息中会明确标注账本交易日。周末或节假日仍会执行工作流，但日终任务会识别
“没有新的交易日”，不会重复成交。

## 一、统一配置文件

`paper_account.risk_profile` 支持 `balanced`（均衡型）和 `aggressive`（进取型）。
修改风险档会被视为账户配置变化，并按模拟起点重新回放账本。
`paper_account.minimum_invested_ratio` 设置最低目标仓位，当前为 `0.70`。没有合格趋势
信号或订单因涨跌停、停牌、整手约束无法成交时，系统不会强买，但会记录仓位缺口并在
后续交易日继续尝试补足。

所有运行配置只读取 `config/quant-config.json`，不存在示例文件回退逻辑。

统一配置包括：

- Tushare 数据源和 API Token。
- 飞书群机器人开关、Webhook、签名密钥、关键词、超时和重试次数。
- 账户标识、初始资金、策略和固定交易频率。
- 全市场/固定股票池模式、因子权重、候选池规模、回测起止日期、模拟盘起点。
- Bocha 新闻开关、检索范围、最大查询数和新闻因子权重。
- 北京时区、午间报告时间和日终运行时间。
- 每账户文本账本目录、日报输出路径。
- 推送标题、最大持仓展示数量和是否包含反思；交易计划始终完整展示，超长时分段推送。

为了避免泄露密钥，已提交的配置保留 `${TUSHARE_TOKEN}`、`${BOCHA_API_KEY}`、
`${FEISHU_WEBHOOK_URL}` 占位符，程序从本地环境变量或 GitHub Repository Secrets
读取真实值。GitHub Actions 与本地任务读取的是同一个配置文件。

> GitHub 的 `schedule` 在检出仓库之前触发，不能动态读取 JSON。因此统一配置中的
> `schedule.position_report_at`、`schedule.daily_close_at` 是记录值，工作流中的
> cron 仍需与它们保持一致。当前配置为 `Asia/Shanghai 12:00` 和 `18:00`。

## 二、添加 GitHub Secrets

进入 `ToolmanInside/quant`：

`Settings → Secrets and variables → Actions → New repository secret`

添加以下 Repository secrets：

- `TUSHARE_TOKEN`：Tushare Pro Token。
- `BOCHA_API_KEY`：Bocha Web Search API Key。曾粘贴到聊天、日志或提交历史中的
  Key 必须先撤销再重建。
- `FEISHU_WEBHOOK_URL`：飞书群机器人完整 Webhook 地址。
- `FEISHU_WEBHOOK_SECRET`：飞书机器人签名密钥（如启用签名校验）。
- `FEISHU_WEBHOOK_KEYWORD`：飞书机器人自定义关键词（如启用关键词校验）。

不要将真实密钥写进配置、工作流 YAML、账户 TXT 或日志。

## 三、全市场研究口径

`market_universe.mode` 设为 `full_market` 时，每个日终任务采用两阶段流程：

1. 一次性读取当日全部上市 A 股横截面，排除 ST、上市时间过短和成交额不足的股票。
2. 横截面计算估值、盈利质量、换手活跃度、资金流、流动性和当日强弱分。
3. 先排名板块，再限制每个板块的候选数量，防止一个热点行业占满股票池。
4. 只对 `detailed_candidate_count` 只候选、当前持仓和待执行计划拉取多年复权行情，
   运行双均线、动量或通道突破策略。
5. Bocha 只检索前几个板块和重点候选，默认权重 5%，命中重大风险关键词时从新买
   候选中排除；已有持仓仍交给技术退出和止损规则处理。

这是真正的“每日全市场横截面扫描”，但不是严格的历史全市场回测。严格回测还需要
保存每个交易日当时的上市/退市、ST、行业成分、财务公告时点和全市场截面，避免幸存者
偏差。当前模拟盘适合从启用之日起向前运行；不要把“用今天候选回放过去”的结果当成
全市场历史业绩。

## 四、账户文本如何跨天保存

每个模拟账户只有一个文件：

```text
.quant-state/accounts/<account_id>.txt
```

它是缩进格式的 UTF-8 JSON，可以直接打开阅读，包含：

- 现金、累计权益、历史最高权益和当前策略版本。
- 当前持仓和完整模拟成交。
- 每日权益快照。
- 每日回顾、分析、决策和反思。
- 当日全市场因子覆盖率、板块/个股排名、新闻风险和来源链接。
- 下一交易日待执行计划。
- 策略版本和升级记录。

每次任务先在内存中完成计算，结束时生成临时文件再原子替换，避免历史回放反复重写，
也避免任务中断留下半个文件。Actions 通过 Cache 恢复最近一次成功运行的文本账本，
第二天读取后继续处理新交易日；每次成功运行还会上传一份账户 TXT Artifact 作为
人工可下载的备份。

## 五、修改策略或账户配置

`paper_account.reinitialize_on_config_change` 默认为 `true`。修改策略、股票池、
初始资金或回测/模拟日期并提交到默认分支后，下一次日终任务会识别配置变化，自动
重建账户 TXT 并按新配置完成历史推演。日报会显示本次实际生效的初始资金、回测区间
和模拟盘起点。

如将该开关设为 `false`，配置不一致时任务会失败而不重置。此时可在 Actions 页面
手动运行 `Daily paper trading` 并勾选 `force_reinitialize`。

## 六、首次运行

工作流合入默认分支后，可在 Actions 页面手动执行一次：

1. 打开 `Actions → Daily paper trading → Run workflow`。
2. 任务类型选择 `daily-close`，保持“推送飞书”开启。
3. 如需从指定日期起重建模拟盘，在 `模拟盘起点` 填入 `YYYY-MM-DD`
   （留空则沿用配置文件中的 `simulation_start_date`）；修改起点会触发
   配置变更检测并自动重建账户。
4. 首次运行会自动完成历史回放并创建账户文本；账户初始化后才能运行
   `noon-position` 午间报告。

## 七、本地试运行

不推送飞书：

```powershell
$env:PAPER_PUSH_FEISHU = "false"
python scripts/daily_paper_job.py --config config/quant-config.json
```

发送飞书：

```powershell
python scripts/daily_paper_job.py --config config/quant-config.json
```

从指定日期重建模拟盘：

```powershell
python scripts/daily_paper_job.py `
  --config config/quant-config.json `
  --simulation-start-date 2026-07-30
```

本地预览午间盘位报告：

```powershell
python scripts/daily_paper_job.py `
  --config config/quant-config.json `
  --mode noon-position
```

所有成功步骤、数据异常和完整错误堆栈都会直接显示在命令行或 GitHub Actions 日志中。
