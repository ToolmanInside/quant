# GitHub Actions 日终模拟任务

仓库内的 `.github/workflows/daily-paper-trading.yml` 包含两个北京时间任务：

- **12:00 午间盘位报告**：只读取最近一次成功保存的账户 TXT，推送当前持仓、
  仓位、最近权益、当前/历史最高收益、回撤和已生成的下一步计划。它不请求新的
  日线、不推进策略，也不产生模拟成交。
- **18:00 日终任务**：执行以下完整流程：

1. 恢复上一次运行的账户文本和 Tushare 行情缓存。
2. 获取截至北京时间当天的最新日线数据。
3. 按现有日频策略执行逐日推进、复盘、市场分析和策略检查。
4. 生成下一交易日的买入、卖出、减仓或平仓计划。
5. 将当前持仓、当前收益、历史最高收益、回撤和下一步计划推送到企业微信。
6. 将 Markdown 日报和账户 TXT 备份为 GitHub Actions Artifact，保留 90 天。

午间报告中的权益和收益来自最近一次完成的日线模拟快照，不是盘中实时价格或实时
盈亏，消息中会明确标注账本交易日。周末或节假日仍会执行工作流，但日终任务会识别
“没有新的交易日”，不会重复成交。

## 一、统一配置文件

所有运行配置只读取 `config/quant-config.json`，不存在示例文件回退逻辑。

统一配置包括：

- Tushare 数据源和 API Token。
- 企业微信机器人开关、Webhook、超时和重试次数。
- 账户标识、初始资金、策略和固定交易频率。
- 股票池、回测起止日期、模拟盘起点。
- 北京时区、午间报告时间和日终运行时间。
- 每账户文本账本目录、日报输出路径。
- 推送标题、最大持仓/计划展示数量和是否包含反思。

为了避免泄露密钥，已提交的配置保留 `${TUSHARE_TOKEN}`、
`${WECHAT_WEBHOOK_URL}` 占位符，程序从本地环境变量或 GitHub Repository Secrets
读取真实值。GitHub Actions 与本地任务读取的是同一个配置文件。

> GitHub 的 `schedule` 在检出仓库之前触发，不能动态读取 JSON。因此统一配置中的
> `schedule.position_report_at`、`schedule.daily_close_at` 是记录值，工作流中的
> cron 仍需与它们保持一致。当前配置为 `Asia/Shanghai 12:00` 和 `18:00`。

## 二、添加 GitHub Secrets

进入 `ToolmanInside/quant`：

`Settings → Secrets and variables → Actions → New repository secret`

添加两个 Repository secrets：

- `TUSHARE_TOKEN`：Tushare Pro Token。
- `WECHAT_WEBHOOK_URL`：企业微信群机器人完整 Webhook 地址。

不要将真实密钥写进已提交的示例配置或工作流 YAML。

## 三、账户文本如何跨天保存

每个模拟账户只有一个文件：

```text
.quant-state/accounts/<account_id>.txt
```

它是缩进格式的 UTF-8 JSON，可以直接打开阅读，包含：

- 现金、累计权益、历史最高权益和当前策略版本。
- 当前持仓和完整模拟成交。
- 每日权益快照。
- 每日回顾、分析、决策和反思。
- 下一交易日待执行计划。
- 策略版本和升级记录。

每次任务先在内存中完成计算，结束时生成临时文件再原子替换，避免历史回放反复重写，
也避免任务中断留下半个文件。Actions 通过 Cache 恢复最近一次成功运行的文本账本，
第二天读取后继续处理新交易日；每次成功运行还会上传一份账户 TXT Artifact 作为
人工可下载的备份。

## 四、修改策略或账户配置

`paper_account.reinitialize_on_config_change` 默认为 `true`。修改策略、股票池、
初始资金或回测/模拟日期并提交到默认分支后，下一次日终任务会识别配置变化，自动
重建账户 TXT 并按新配置完成历史推演。日报会显示本次实际生效的初始资金、回测区间
和模拟盘起点。

如将该开关设为 `false`，配置不一致时任务会失败而不重置。此时可在 Actions 页面
手动运行 `Daily paper trading` 并勾选 `force_reinitialize`。

## 五、首次运行

工作流合入默认分支后，可在 Actions 页面手动执行一次：

1. 打开 `Actions → Daily paper trading → Run workflow`。
2. 任务类型选择 `daily-close`，保持“推送企业微信”开启。
3. 首次运行会自动完成历史回放并创建账户文本；账户初始化后才能运行
   `noon-position` 午间报告。

## 六、本地试运行

不推送企业微信：

```powershell
$env:PAPER_PUSH_WECHAT = "false"
python scripts/daily_paper_job.py --config config/quant-config.json
```

发送企业微信：

```powershell
python scripts/daily_paper_job.py --config config/quant-config.json
```

本地预览午间盘位报告：

```powershell
python scripts/daily_paper_job.py `
  --config config/quant-config.json `
  --mode noon-position
```

所有成功步骤、数据异常和完整错误堆栈都会直接显示在命令行或 GitHub Actions 日志中。
