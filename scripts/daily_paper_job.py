from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any
from zoneinfo import ZoneInfo

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.data.providers import TushareDataProvider
from backend.models import PaperAdvanceRequest, PaperSimulationRequest
from backend.paper_store import PaperStore
from backend.paper_trading import advance_paper_simulation, replay_paper_simulation


LOGGER = logging.getLogger("daily-paper-job")
ACTION_NAMES = {
    "BUY": "买入",
    "SELL": "卖出 / 减仓",
    "CLOSE": "平仓",
}


@dataclass(frozen=True)
class JobConfig:
    account_id: str
    strategy_id: str
    frequency: str
    symbols: list[str]
    backtest_start_date: date
    backtest_end_date: date
    simulation_start_date: date
    initial_cash: float


@dataclass(frozen=True)
class UnifiedConfig:
    paper_account: JobConfig
    tushare_token: str
    timezone: str
    position_report_at: str
    daily_close_at: str
    run_every_day: bool
    state_directory: Path
    midday_report_path: Path
    report_path: Path
    wechat_enabled: bool
    wechat_webhook_url: str
    notification_timeout: int
    notification_retries: int
    report_title: str
    midday_report_title: str
    max_holdings: int
    max_plans: int
    include_reflection: bool


def _resolve_environment_placeholder(
    value: object,
    field_name: str,
    *,
    required: bool = True,
) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"\$\{([A-Z][A-Z0-9_]*)\}", text)
    if match:
        text = os.getenv(match.group(1), "").strip()
    if not text and required:
        raise ValueError(f"配置项 {field_name} 为空")
    return text


def load_unified_config(path: Path) -> UnifiedConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    account_payload = payload.get("paper_account") or {}
    required = {
        "account_id",
        "strategy_id",
        "symbols",
        "backtest_start_date",
        "backtest_end_date",
        "simulation_start_date",
        "initial_cash",
    }
    missing = sorted(required.difference(account_payload))
    if missing:
        raise ValueError(f"日终任务配置缺少字段：{', '.join(missing)}")
    frequency = str(account_payload.get("frequency", "1d"))
    if frequency != "1d":
        raise ValueError("当前模拟盘只支持日频 frequency=1d")

    market = payload.get("market_data") or {}
    if market.get("provider", "tushare") != "tushare":
        raise ValueError("当前系统只支持 Tushare 数据源")
    schedule = payload.get("schedule") or {}
    storage = payload.get("storage") or {}
    notification = payload.get("notification") or {}
    report = payload.get("report") or {}
    timezone = str(schedule.get("timezone", "Asia/Shanghai"))
    ZoneInfo(timezone)
    position_report_at = str(schedule.get("position_report_at", "12:00"))
    daily_close_at = str(schedule.get("daily_close_at", "18:00"))
    for field_name, field_value in (
        ("schedule.position_report_at", position_report_at),
        ("schedule.daily_close_at", daily_close_at),
    ):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", field_value):
            raise ValueError(f"{field_name} 必须使用 HH:MM 24 小时格式")

    wechat_enabled = bool(notification.get("wechat_enabled", True))
    raw_webhook = notification.get("wechat_webhook_url", "")
    webhook = (
        _resolve_environment_placeholder(
            raw_webhook,
            "notification.wechat_webhook_url",
            required=False,
        )
        if wechat_enabled
        else ""
    )
    return UnifiedConfig(
        paper_account=JobConfig(
            account_id=str(account_payload["account_id"]),
            strategy_id=str(account_payload["strategy_id"]),
            frequency=frequency,
            symbols=[str(symbol) for symbol in account_payload["symbols"]],
            backtest_start_date=date.fromisoformat(
                account_payload["backtest_start_date"]
            ),
            backtest_end_date=date.fromisoformat(account_payload["backtest_end_date"]),
            simulation_start_date=date.fromisoformat(
                account_payload["simulation_start_date"]
            ),
            initial_cash=float(account_payload["initial_cash"]),
        ),
        tushare_token=_resolve_environment_placeholder(
            market.get("tushare_token"),
            "market_data.tushare_token",
        ),
        timezone=timezone,
        position_report_at=position_report_at,
        daily_close_at=daily_close_at,
        run_every_day=bool(schedule.get("run_every_day", True)),
        state_directory=Path(
            storage.get("state_directory", ".quant-state/accounts")
        ),
        midday_report_path=Path(
            storage.get(
                "midday_report_path",
                "outputs/midday-position-report.md",
            )
        ),
        report_path=Path(
            storage.get("report_path", "outputs/daily-paper-report.md")
        ),
        wechat_enabled=wechat_enabled,
        wechat_webhook_url=webhook,
        notification_timeout=max(
            1,
            int(notification.get("timeout_seconds", 20)),
        ),
        notification_retries=max(0, int(notification.get("retry_count", 2))),
        report_title=str(report.get("title", "Quant Lab 模拟盘日终报告")),
        midday_report_title=str(
            report.get("midday_title", "Quant Lab 午间盘位报告")
        ),
        max_holdings=max(1, int(report.get("max_holdings", 5))),
        max_plans=max(1, int(report.get("max_plans", 5))),
        include_reflection=bool(report.get("include_reflection", True)),
    )


def _percent(value: float) -> str:
    return f"{value * 100:+.2f}%"


def _money(value: float) -> str:
    return f"¥{value:,.2f}"


def _trim(value: str, maximum: int = 70) -> str:
    normalized = " ".join(str(value).split())
    return normalized if len(normalized) <= maximum else f"{normalized[: maximum - 1]}…"


def _holding_lines(
    dashboard: dict[str, Any],
    maximum: int,
) -> list[str]:
    positions = dashboard.get("positions") or []
    if not positions:
        return ["- 当前空仓"]
    lines = []
    for position in positions[:maximum]:
        lines.append(
            (
                f"- **{position['name']}** `{position['symbol']}`："
                f"{int(position['shares']):,} 股，成本 {_money(float(position['avg_price']))}，"
                f"建仓日 {position['entry_date']}"
            )
        )
    if len(positions) > maximum:
        lines.append(
            f"- 另有 {len(positions) - maximum} 个持仓，请在模拟盘页面查看"
        )
    return lines


def _plan_lines(
    dashboard: dict[str, Any],
    maximum: int,
) -> list[str]:
    plan = dashboard["account"].get("pending_plan") or []
    if not plan:
        return ["- **无新增交易指令**：下一交易日维持当前持仓/空仓，等待新信号。"]
    lines = []
    for item in plan[:maximum]:
        action = ACTION_NAMES.get(item["action"], item["action"])
        lines.append(
            (
                f"- **{action} {item['name']}** `{item['symbol']}`："
                f"目标仓位 {float(item['target_weight']):.1%}；"
                f"{_trim(item['reason'])}"
            )
        )
    if len(plan) > maximum:
        lines.append(
            f"- 另有 {len(plan) - maximum} 条计划，请在模拟盘页面查看"
        )
    return lines


def build_markdown_report(
    dashboard: dict[str, Any],
    *,
    generated_at: datetime,
    title: str = "Quant Lab 模拟盘日终报告",
    max_holdings: int = 5,
    max_plans: int = 5,
    include_reflection: bool = True,
) -> str:
    account = dashboard["account"]
    latest = dashboard.get("latest")
    if not latest:
        raise ValueError("模拟账户还没有权益快照，无法生成日报")

    initial_cash = float(account["initial_cash"])
    current_equity = float(latest["equity"])
    current_return = current_equity / initial_cash - 1
    peak_return = float(account["peak_equity"]) / initial_cash - 1
    run = dashboard.get("run") or {}
    processed_days = int(run.get("processed_days", 0))
    run_status = (
        f"已处理 {processed_days} 个新交易日"
        if processed_days
        else "今日无新增交易日，未重复执行"
    )
    sectors = "、".join(
        sector["name"] for sector in (latest.get("top_sectors") or [])[:3]
    ) or "暂无"
    latest_journals = dashboard.get("daily_journals") or []
    reflection = (
        latest_journals[0].get("reflection", {}).get("conclusion")
        if latest_journals
        else None
    ) or "暂无异常结论"

    lines = [
        f"### {title}",
        (
            f"> 信号日：**{latest['trade_date']}**　"
            f"生成：{generated_at:%Y-%m-%d %H:%M}（北京时间）"
        ),
        f"> 运行结果：{run_status}",
        "",
        "#### 账户概览",
        f"- 当前权益：**{_money(current_equity)}**",
        f"- 当前收益：**{_percent(current_return)}**",
        f"- 历史最高收益：**{_percent(peak_return)}**",
        f"- 今日收益：**{_percent(float(latest['daily_return']))}**",
        f"- 当前回撤：**{_percent(float(latest['drawdown']))}**",
        (
            f"- 可用现金：{_money(float(account['cash']))}；"
            f"持仓市值：{_money(float(latest['market_value']))}"
        ),
        "",
        f"#### 当前持仓（{len(dashboard.get('positions') or [])} 个）",
        *_holding_lines(dashboard, max_holdings),
        "",
        "#### 今日分析",
        (
            f"- 市场状态：**{latest['market_regime']}**；"
            f"趋势宽度 {float(latest['breadth']):.1%}；"
            f"数据完整度 {float(latest['data_quality']):.1%}"
        ),
        f"- 优选板块：{sectors}",
        *(
            [f"- 复盘结论：{_trim(reflection, 120)}"]
            if include_reflection
            else []
        ),
        "",
        "#### 下一交易日计划",
        *_plan_lines(dashboard, max_plans),
        "",
        "> 本报告来自模拟账户，不连接券商，不构成投资建议。",
    ]
    return "\n".join(lines)


def build_position_report(
    dashboard: dict[str, Any],
    *,
    generated_at: datetime,
    title: str = "Quant Lab 午间盘位报告",
    max_holdings: int = 5,
    max_plans: int = 5,
) -> str:
    account = dashboard["account"]
    latest = dashboard.get("latest")
    if not latest:
        raise ValueError("模拟账户还没有权益快照，无法生成午间盘位报告")

    initial_cash = float(account["initial_cash"])
    equity = float(latest["equity"])
    market_value = float(latest["market_value"])
    position_ratio = market_value / equity if equity > 0 else 0.0
    current_return = equity / initial_cash - 1
    peak_return = float(account["peak_equity"]) / initial_cash - 1
    return "\n".join(
        [
            f"### {title}",
            (
                f"> 账本时点：**{latest['trade_date']}**　"
                f"报告生成：{generated_at:%Y-%m-%d %H:%M}（北京时间）"
            ),
            "> 数据口径：最近一次完成的日线模拟快照，不是盘中实时价格或实时盈亏。",
            "",
            "#### 当前盘位",
            f"- 当前权益：**{_money(equity)}**",
            f"- 总仓位：**{position_ratio:.1%}**",
            f"- 当前收益：**{_percent(current_return)}**",
            f"- 历史最高收益：**{_percent(peak_return)}**",
            f"- 当前回撤：**{_percent(float(latest['drawdown']))}**",
            (
                f"- 可用现金：{_money(float(account['cash']))}；"
                f"持仓市值：{_money(market_value)}"
            ),
            "",
            f"#### 持仓明细（{len(dashboard.get('positions') or [])} 个）",
            *_holding_lines(dashboard, max_holdings),
            "",
            "#### 已生成的下一步计划",
            *_plan_lines(dashboard, max_plans),
            "",
            "> 午间任务只读账户文本，不推进策略、不产生模拟成交。",
        ]
    )


def load_account_dashboard(
    config: JobConfig,
    *,
    state_path: Path,
) -> dict[str, Any]:
    if not state_path.exists():
        raise ValueError(
            f"模拟账户文本不存在：{state_path}；请先运行一次日终初始化任务"
        )
    store = PaperStore(state_path)
    try:
        return store.dashboard(config.account_id)
    finally:
        store.close()


def send_wechat_markdown(
    webhook_url: str,
    content: str,
    *,
    client: httpx.Client | None = None,
    timeout_seconds: int = 20,
    retry_count: int = 2,
) -> None:
    if not webhook_url.startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"):
        raise ValueError("WECHAT_WEBHOOK_URL 不是有效的企业微信机器人 Webhook")
    suffix = "\n\n> 消息过长，完整内容请查看 GitHub Actions 日报。"
    encoded = content.encode("utf-8")
    if len(encoded) > 3900:
        budget = 3900 - len(suffix.encode("utf-8"))
        truncated = encoded[:budget]
        while True:
            try:
                content = truncated.decode("utf-8") + suffix
                break
            except UnicodeDecodeError:
                truncated = truncated[:-1]
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    owns_client = client is None
    active_client = client or httpx.Client(timeout=timeout_seconds)
    try:
        last_error_message = ""
        for attempt in range(retry_count + 1):
            try:
                response = active_client.post(webhook_url, json=payload)
                response.raise_for_status()
                result = response.json()
                if result.get("errcode") != 0:
                    raise RuntimeError(
                        "企业微信机器人拒绝消息："
                        f"{result.get('errmsg', '未知错误')}"
                    )
                return
            except httpx.HTTPStatusError as exc:
                last_error_message = f"HTTP {exc.response.status_code}"
            except httpx.HTTPError as exc:
                last_error_message = type(exc).__name__
            except (RuntimeError, ValueError) as exc:
                last_error_message = str(exc)
            if last_error_message:
                LOGGER.warning(
                    "企业微信推送第 %s/%s 次失败：%s",
                    attempt + 1,
                    retry_count + 1,
                    last_error_message,
                )
        raise RuntimeError(
            f"企业微信推送失败，已重试 {retry_count} 次：{last_error_message}"
        )
    finally:
        if owns_client:
            active_client.close()


def _configuration_changed(account: dict[str, Any], config: JobConfig) -> bool:
    stored = account.get("configuration") or {}
    return (
        stored.get("strategy_id") != config.strategy_id
        or stored.get("frequency", "1d") != config.frequency
        or account.get("universe") != config.symbols
        or float(account.get("initial_cash", 0)) != config.initial_cash
        or stored.get("backtest_start_date")
        != config.backtest_start_date.isoformat()
        or stored.get("backtest_end_date") != config.backtest_end_date.isoformat()
        or stored.get("simulation_start_date")
        != config.simulation_start_date.isoformat()
    )


def run_daily_job(
    config: JobConfig,
    *,
    state_path: Path,
    as_of_date: date,
    tushare_token: str,
    force_reinitialize: bool = False,
) -> dict[str, Any]:
    if as_of_date < config.simulation_start_date:
        raise ValueError(
            "任务日期早于模拟盘起点，请修改统一配置文件中的 simulation_start_date"
        )

    provider = TushareDataProvider(tushare_token)
    store = PaperStore(state_path)
    try:
        account = store.account(config.account_id)
        if account is None or force_reinitialize:
            LOGGER.info(
                "初始化模拟账户 %s，并历史回放到 %s",
                config.account_id,
                as_of_date,
            )
            return replay_paper_simulation(
                PaperSimulationRequest(
                    account_id=config.account_id,
                    strategy_id=config.strategy_id,
                    symbols=config.symbols,
                    backtest_start_date=config.backtest_start_date,
                    backtest_end_date=config.backtest_end_date,
                    simulation_start_date=config.simulation_start_date,
                    simulation_end_date=as_of_date,
                    initial_cash=config.initial_cash,
                ),
                provider,
                store,
            )

        if _configuration_changed(account, config):
            raise ValueError(
                "仓库配置与持久化模拟账户不一致。"
                "请手动运行工作流并启用 force_reinitialize 以重建账户。"
            )

        LOGGER.info("更新模拟账户 %s 到 %s", config.account_id, as_of_date)
        return advance_paper_simulation(
            PaperAdvanceRequest(
                account_id=config.account_id,
                symbols=config.symbols,
                as_of_date=as_of_date,
            ),
            provider,
            store,
        )
    finally:
        store.close()


def _environment_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="更新 Quant Lab 模拟账户、生成日终报告并推送企业微信。"
    )
    parser.add_argument(
        "--mode",
        choices=("daily-close", "noon-position"),
        default=os.getenv("PAPER_JOB_MODE") or "daily-close",
        help="daily-close 更新日线并分析；noon-position 只读并报告盘位。",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=(
            Path("config/quant-config.json")
            if Path("config/quant-config.json").exists()
            else Path("config/quant-config.example.json")
        ),
    )
    parser.add_argument(
        "--state-directory",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--as-of-date",
        default=os.getenv("PAPER_AS_OF_DATE") or None,
        help="按 YYYY-MM-DD 指定任务日期；默认使用北京时间当天。",
    )
    parser.add_argument(
        "--force-reinitialize",
        action="store_true",
        default=_environment_flag("PAPER_FORCE_REINITIALIZE", False),
    )
    parser.add_argument(
        "--skip-wechat",
        action="store_true",
        default=not _environment_flag("PAPER_PUSH_WECHAT", True),
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    args = parse_args()
    try:
        unified = load_unified_config(args.config)
        config = unified.paper_account
        timezone = ZoneInfo(unified.timezone)
        as_of_date = (
            date.fromisoformat(args.as_of_date)
            if args.as_of_date
            else datetime.now(timezone).date()
        )
        if not unified.run_every_day and as_of_date.weekday() >= 5:
            LOGGER.info("统一配置已关闭周末运行，本次任务无需处理")
            return 0
        state_directory = args.state_directory or unified.state_directory
        state_path = state_directory / f"{config.account_id}.txt"
        generated_at = datetime.now(timezone)
        if args.mode == "noon-position":
            dashboard = load_account_dashboard(config, state_path=state_path)
            report = build_position_report(
                dashboard,
                generated_at=generated_at,
                title=unified.midday_report_title,
                max_holdings=unified.max_holdings,
                max_plans=unified.max_plans,
            )
            report_path = args.report or unified.midday_report_path
        else:
            dashboard = run_daily_job(
                config,
                state_path=state_path,
                as_of_date=as_of_date,
                tushare_token=unified.tushare_token,
                force_reinitialize=args.force_reinitialize,
            )
            report = build_markdown_report(
                dashboard,
                generated_at=generated_at,
                title=unified.report_title,
                max_holdings=unified.max_holdings,
                max_plans=unified.max_plans,
                include_reflection=unified.include_reflection,
            )
            report_path = args.report or unified.report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        print(report)

        if args.skip_wechat or not unified.wechat_enabled:
            LOGGER.warning("已跳过企业微信推送")
        else:
            if not unified.wechat_webhook_url:
                raise ValueError(
                    "企业微信推送已启用，但 wechat_webhook_url 为空；"
                    "请填写配置文件或添加 WECHAT_WEBHOOK_URL Secret"
                )
            send_wechat_markdown(
                unified.wechat_webhook_url,
                report,
                timeout_seconds=unified.notification_timeout,
                retry_count=unified.notification_retries,
            )
            LOGGER.info("企业微信%s推送成功", unified.midday_report_title if args.mode == "noon-position" else unified.report_title)
        return 0
    except Exception:
        LOGGER.exception("模拟盘定时任务失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
