from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
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
from backend.market_research import (
    DEFAULT_FACTOR_WEIGHTS,
    MarketUniverseConfig,
    research_full_market,
)
from backend.models import (
    PaperAdvanceRequest,
    PaperSimulationRequest,
    normalize_ts_code,
)
from backend.news_research import NewsResearchConfig, enrich_with_bocha_news
from backend.paper_store import PaperStore
from backend.paper_trading import advance_paper_simulation, replay_paper_simulation


LOGGER = logging.getLogger("daily-paper-job")
ACTION_NAMES = {
    "BUY": "买入",
    "SELL": "卖出 / 减仓",
    "CLOSE": "平仓",
}
RISK_PROFILE_NAMES = {
    "balanced": "均衡型",
    "aggressive": "进取型",
}
CONSTRAINT_NAMES = {
    "limit_up_locked": "一字涨停，买入无法成交",
    "limit_down_locked": "一字跌停，卖出无法成交",
    "suspended_or_no_volume": "停牌或无成交量",
    "invalid_open_quote": "开盘报价无效",
    "price_limit_unavailable": "缺少当日涨跌停价",
    "insufficient_cash": "可用现金不足",
    "insufficient_cash_partial_fill": "现金不足，仅部分成交",
    "board_lot_constraint": "不足一手，无法部分减仓",
    "board_lot_or_already_at_target": "不足一手或已接近目标",
    "missing_market_frame": "缺少行情序列",
    "no_open_quote": "当日没有开盘报价",
    "no_position_or_already_at_target": "无持仓或已达到目标",
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
    universe_mode: str = "fixed"
    risk_profile: str = "balanced"
    minimum_invested_ratio: float = 0.0


@dataclass(frozen=True)
class UnifiedConfig:
    paper_account: JobConfig
    tushare_token: str
    bocha_api_key: str
    market_universe: MarketUniverseConfig
    news_research: NewsResearchConfig
    timezone: str
    position_report_at: str
    daily_close_at: str
    run_every_day: bool
    state_directory: Path
    midday_report_path: Path
    report_path: Path
    wechat_enabled: bool
    wechat_webhook_url: str
    notification_provider: str
    feishu_webhook_url: str
    feishu_webhook_secret: str
    feishu_webhook_keyword: str
    notification_timeout: int
    notification_retries: int
    report_title: str
    midday_report_title: str
    max_holdings: int
    include_reflection: bool
    reinitialize_on_config_change: bool


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


def _strict_bool(value: object, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(
        f"配置项 {field_name} 必须使用 JSON 布尔值 true 或 false，"
        "不能使用字符串"
    )


def _normalized_symbol_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"配置项 {field_name} 必须是证券代码数组")
    try:
        return list(dict.fromkeys(normalize_ts_code(symbol) for symbol in value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"配置项 {field_name} 包含无效证券代码：{exc}") from exc


def load_unified_config(path: Path) -> UnifiedConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"配置文件 {path} 不是合法 JSON：第 {exc.lineno} 行、"
            f"第 {exc.colno} 列，{exc.msg}。"
            "JSON 不支持 // 或 # 注释；空列表请写成 []。"
        ) from exc
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
    risk_profile = str(account_payload.get("risk_profile", "balanced"))
    if risk_profile not in {"balanced", "aggressive"}:
        raise ValueError(
            "paper_account.risk_profile 只能是 balanced 或 aggressive"
        )
    minimum_invested_ratio = float(
        account_payload.get("minimum_invested_ratio", 0.0)
    )
    if not 0.0 <= minimum_invested_ratio <= 0.95:
        raise ValueError(
            "paper_account.minimum_invested_ratio 必须在 0 到 0.95 之间"
        )

    market = payload.get("market_data") or {}
    if market.get("provider", "tushare") != "tushare":
        raise ValueError("当前系统只支持 Tushare 数据源")
    universe_payload = payload.get("market_universe") or {}
    universe_mode = str(universe_payload.get("mode", "fixed"))
    if universe_mode not in {"fixed", "full_market"}:
        raise ValueError("market_universe.mode 只能是 fixed 或 full_market")
    factor_weights = universe_payload.get("factor_weights") or DEFAULT_FACTOR_WEIGHTS
    if not isinstance(factor_weights, dict):
        raise ValueError("market_universe.factor_weights 必须是对象")
    market_universe = MarketUniverseConfig(
        mode=universe_mode,
        minimum_listing_days=max(
            0,
            int(universe_payload.get("minimum_listing_days", 180)),
        ),
        minimum_daily_amount=max(
            0.0,
            float(universe_payload.get("minimum_daily_amount", 50_000_000)),
        ),
        detailed_candidate_count=max(
            5,
            min(int(universe_payload.get("detailed_candidate_count", 40)), 100),
        ),
        top_sector_count=max(
            1,
            min(int(universe_payload.get("top_sector_count", 8)), 20),
        ),
        max_candidates_per_sector=max(
            1,
            min(int(universe_payload.get("max_candidates_per_sector", 6)), 20),
        ),
        always_include_symbols=_normalized_symbol_list(
            universe_payload.get("always_include_symbols", []),
            "market_universe.always_include_symbols",
        ),
        factor_weights={
            str(name): float(value) for name, value in factor_weights.items()
        },
    )
    news_payload = payload.get("news_research") or {}
    news_enabled = _strict_bool(
        news_payload.get("enabled"),
        "news_research.enabled",
        False,
    )
    news_research = NewsResearchConfig(
        enabled=news_enabled,
        freshness=str(news_payload.get("freshness", "oneWeek")),
        results_per_query=max(
            1,
            min(int(news_payload.get("results_per_query", 5)), 10),
        ),
        max_sectors=max(0, min(int(news_payload.get("max_sectors", 3)), 10)),
        max_stocks=max(0, min(int(news_payload.get("max_stocks", 8)), 20)),
        factor_weight=max(
            0.0,
            min(float(news_payload.get("factor_weight", 0.05)), 0.10),
        ),
        timeout_seconds=max(1, int(news_payload.get("timeout_seconds", 20))),
    )
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

    wechat_enabled = _strict_bool(
        notification.get("wechat_enabled"),
        "notification.wechat_enabled",
        True,
    )
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
    notification_provider = str(
        notification.get("provider", "wechat")
    ).strip().lower()
    if notification_provider not in ("wechat", "feishu"):
        raise ValueError(
            "notification.provider 只能是 wechat 或 feishu"
        )
    feishu_enabled = notification_provider == "feishu"
    feishu_webhook_url = (
        _resolve_environment_placeholder(
            notification.get("feishu_webhook_url", ""),
            "notification.feishu_webhook_url",
            required=feishu_enabled,
        )
        if feishu_enabled
        else ""
    )
    feishu_webhook_secret = (
        _resolve_environment_placeholder(
            notification.get("feishu_webhook_secret", ""),
            "notification.feishu_webhook_secret",
            required=False,
        )
        if feishu_enabled
        else ""
    )
    feishu_webhook_keyword = (
        _resolve_environment_placeholder(
            notification.get("feishu_webhook_keyword", ""),
            "notification.feishu_webhook_keyword",
            required=False,
        )
        if feishu_enabled
        else ""
    )
    return UnifiedConfig(
        paper_account=JobConfig(
            account_id=str(account_payload["account_id"]),
            strategy_id=str(account_payload["strategy_id"]),
            frequency=frequency,
            symbols=_normalized_symbol_list(
                account_payload["symbols"],
                "paper_account.symbols",
            ),
            backtest_start_date=date.fromisoformat(
                account_payload["backtest_start_date"]
            ),
            backtest_end_date=date.fromisoformat(account_payload["backtest_end_date"]),
            simulation_start_date=date.fromisoformat(
                account_payload["simulation_start_date"]
            ),
            initial_cash=float(account_payload["initial_cash"]),
            universe_mode=universe_mode,
            risk_profile=risk_profile,
            minimum_invested_ratio=minimum_invested_ratio,
        ),
        tushare_token=_resolve_environment_placeholder(
            market.get("tushare_token"),
            "market_data.tushare_token",
        ),
        bocha_api_key=_resolve_environment_placeholder(
            news_payload.get("api_key", ""),
            "news_research.api_key",
            required=news_enabled,
        ),
        market_universe=market_universe,
        news_research=news_research,
        timezone=timezone,
        position_report_at=position_report_at,
        daily_close_at=daily_close_at,
        run_every_day=_strict_bool(
            schedule.get("run_every_day"),
            "schedule.run_every_day",
            True,
        ),
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
        notification_provider=notification_provider,
        feishu_webhook_url=feishu_webhook_url,
        feishu_webhook_secret=feishu_webhook_secret,
        feishu_webhook_keyword=feishu_webhook_keyword,
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
        include_reflection=_strict_bool(
            report.get("include_reflection"),
            "report.include_reflection",
            True,
        ),
        reinitialize_on_config_change=_strict_bool(
            account_payload.get("reinitialize_on_config_change"),
            "paper_account.reinitialize_on_config_change",
            True,
        ),
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
) -> list[str]:
    plan = dashboard["account"].get("pending_plan") or []
    if not plan:
        return ["- **无新增交易指令**：下一交易日维持当前持仓/空仓，等待新信号。"]
    latest = dashboard.get("latest") or {}
    target_exposure = float(
        latest.get("allocated_exposure")
        or sum(
            float(item.get("target_weight", 0))
            for item in plan
            if item.get("action") != "CLOSE"
        )
    )
    signal_date = str(plan[0].get("signal_date") or "最近信号日")
    lines = [
        (
            f"- **待执行目标总仓位 {target_exposure:.1%}**；"
            f"{signal_date} 收盘确认，下一交易日开盘模拟成交。"
        )
    ]
    # 交易计划属于必须完整披露的信息。企业微信超长内容由发送层分段处理。
    for item in plan:
        action = ACTION_NAMES.get(item["action"], item["action"])
        lines.append(
            (
                f"- **{action} {item['name']}** `{item['symbol']}`："
                f"目标仓位 {float(item['target_weight']):.1%}；"
                f"{_trim(item['reason'])}"
            )
        )
    return lines


def _execution_reconciliation_lines(dashboard: dict[str, Any]) -> list[str]:
    journals = dashboard.get("daily_journals") or []
    reconciliation = (
        journals[0].get("review", {}).get("execution_reconciliation", [])
        if journals
        else []
    )
    if not reconciliation:
        return ["- 今日没有到期交易计划。"]
    lines: list[str] = []
    for item in reconciliation:
        action = ACTION_NAMES.get(item.get("action"), item.get("action", "交易"))
        status = (
            "完成"
            if float(item.get("fill_ratio", 0)) >= 1
            else "部分成交"
            if float(item.get("fill_ratio", 0)) > 0
            else "未成交"
        )
        raw_constraint = item.get("constraint_reason")
        constraint = CONSTRAINT_NAMES.get(raw_constraint, raw_constraint or "无")
        warning_key = item.get("tradability_warning")
        warning = CONSTRAINT_NAMES.get(warning_key, warning_key or "")
        lines.append(
            f"- **{action}** `{item['symbol']}` {status}："
            f"计划 {int(item.get('planned_quantity', 0)):,} 股，"
            f"实际 {int(item.get('actual_quantity', 0)):,} 股；"
            f"实际仓位 {float(item.get('actual_weight', 0)):.1%}；约束 {constraint}"
            f"{'；提示 ' + warning if warning else ''}。"
        )
    return lines


def _market_research_lines(dashboard: dict[str, Any]) -> list[str]:
    research = (
        dashboard.get("market_research")
        or dashboard.get("account", {}).get("market_research")
    )
    if not research:
        return ["- 未运行全市场横截面研究（当前为固定股票池模式）。"]
    if research.get("mode") != "full_market":
        warnings = research.get("warnings") or ["本次已降级到固定候选池"]
        return [f"- ⚠️ {_trim(str(message), 150)}" for message in warnings]

    coverage = research.get("factor_coverage") or {}
    lines = [
        (
            f"- 截面日期 **{research.get('trade_date', '—')}**；扫描 "
            f"**{int(research.get('market_count', 0)):,}** 只，基础过滤后 "
            f"**{int(research.get('eligible_count', 0)):,}** 只，进入详细趋势池 "
            f"**{int(research.get('detailed_candidate_count', 0))}** 只。"
        ),
        (
            "- 因子覆盖率："
            f"估值 {float(coverage.get('valuation', 0)):.0%}；"
            f"盈利质量 {float(coverage.get('quality', 0)):.0%}；"
            f"换手 {float(coverage.get('turnover', 0)):.0%}；"
            f"资金流 {float(coverage.get('fund_flow', 0)):.0%}。"
        ),
        (
            f"- 全市场过滤样本上涨宽度："
            f"**{float(research.get('market_breadth', 0)):.1%}**。"
        ),
    ]
    technical = research.get("technical_breadth") or {}
    if technical:
        lines.append(
            "- 全市场中期趋势宽度："
            f"20日 **{float(technical.get('above_ma20', 0)):.1%}**；"
            f"60日 **{float(technical.get('above_ma60', 0)):.1%}**；"
            f"组合 **{float(technical.get('composite', 0)):.1%}**；"
            f"覆盖 {float(technical.get('coverage', 0)):.1%}。"
        )
    sectors = research.get("top_sectors") or []
    if sectors:
        lines.append(
            "- 多因子领先板块："
            + "；".join(
                (
                    f"{item['name']} {float(item['score']):.2f}"
                    f"（上涨宽度 {float(item['breadth']):.0%}）"
                )
                for item in sectors[:5]
            )
        )
    candidates = research.get("candidates") or []
    visible_candidates = [
        item for item in candidates if not item.get("excluded_by_news")
    ][:5]
    if visible_candidates:
        lines.append(
            "- 结构化因子候选："
            + "；".join(
                (
                    f"{item['name']} `{item['symbol']}` "
                    f"{float(item['factor_score']):.2f}"
                )
                for item in visible_candidates
            )
        )
    lines.extend(
        f"- ⚠️ {_trim(str(message), 150)}"
        for message in (research.get("warnings") or [])[:3]
    )
    return lines


def _news_research_lines(dashboard: dict[str, Any]) -> list[str]:
    research = (
        dashboard.get("market_research")
        or dashboard.get("account", {}).get("market_research")
        or {}
    )
    news = research.get("news") or {}
    if not news.get("enabled"):
        return ["- Bocha 新闻研究未启用；结构化因子与技术策略仍可独立运行。"]

    lines = [
        (
            "- 新闻仅作低权重校验与重大风险否决，"
            f"当前权重 **{float(news.get('factor_weight', 0)):.0%}**，"
            "不直接触发买卖。"
        )
    ]
    sector_news = news.get("sectors") or []
    if sector_news:
        lines.append(
            "- 板块新闻评分："
            + "；".join(
                f"{item['name']} {float(item['sentiment_score']):.2f}"
                f"（风险 {item['risk_level']}）"
                for item in sector_news[:3]
            )
        )
    risky = [
        item
        for item in news.get("stocks", [])
        if item.get("risk_level") in {"medium", "high"}
    ]
    if risky:
        for item in risky[:3]:
            keywords = (
                item.get("severe_risk_keywords")
                or item.get("negative_keywords")
                or []
            )
            lines.append(
                f"- {item['name']} `{item['symbol']}` 新闻风险 "
                f"**{item['risk_level']}**：{', '.join(keywords) or '需人工核验'}"
            )
    else:
        lines.append("- 已检索的重点候选中未命中规则库里的重大负面关键词。")

    linked = []
    for item in news.get("stocks", []):
        for article in item.get("items", []):
            linked.append(article)
            if len(linked) >= 3:
                break
        if len(linked) >= 3:
            break
    for article in linked:
        source = f"（{article['source']}）" if article.get("source") else ""
        lines.append(
            f"- [{_trim(article['title'], 45)}]({article['url']}){source}"
        )
    lines.extend(
        f"- ⚠️ {_trim(str(message), 150)}"
        for message in (news.get("errors") or [])[:2]
    )
    return lines


def build_markdown_report(
    dashboard: dict[str, Any],
    *,
    generated_at: datetime,
    title: str = "Quant Lab 模拟盘日终报告",
    max_holdings: int = 5,
    include_reflection: bool = True,
) -> str:
    account = dashboard["account"]
    latest = dashboard.get("latest")
    if not latest:
        raise ValueError("模拟账户还没有权益快照，无法生成日报")

    initial_cash = float(account["initial_cash"])
    current_equity = float(latest["equity"])
    current_return = current_equity / initial_cash - 1
    configuration = account.get("configuration") or {}
    actual_exposure = (
        float(latest.get("market_value", 0)) / current_equity
        if current_equity > 0
        else 0.0
    )
    # 生效下限：防守状态下最低仓位被挂起，展示实际生效值并说明原因。
    minimum_exposure = float(latest.get("minimum_exposure", 0.0))
    minimum_suspended_reason = latest.get("minimum_suspended_reason")
    configured_minimum = float(configuration.get("minimum_invested_ratio", 0.0))
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
        "#### 本次生效配置",
        f"- 配置初始资金：**{_money(initial_cash)}**",
        (
            "- 回测区间："
            f"**{configuration.get('backtest_start_date', '—')}** 至 "
            f"**{configuration.get('backtest_end_date', '—')}**"
        ),
        (
            "- 模拟盘起点："
            f"**{configuration.get('simulation_start_date', '—')}**"
        ),
        (
            "- 策略："
            f"**{configuration.get('strategy_name', configuration.get('strategy_id', '—'))}**；"
            f"风险档 **{RISK_PROFILE_NAMES.get(configuration.get('risk_profile', 'balanced'), configuration.get('risk_profile', 'balanced'))}**；"
            f"最低仓位 **{configured_minimum:.0%}**；"
            f"频率 **{configuration.get('frequency', '1d')}**"
        ),
        "",
        f"#### 当前持仓（{len(dashboard.get('positions') or [])} 个）",
        *_holding_lines(dashboard, max_holdings),
        "",
        "#### 今日计划执行核对",
        *_execution_reconciliation_lines(dashboard),
        "",
        "#### 今日分析",
        (
            f"- 市场状态：**{latest['market_regime']}**；"
            f"趋势宽度 {float(latest['breadth']):.1%}；"
            f"数据完整度 {float(latest['data_quality']):.1%}；"
            f"宽度口径 {latest.get('breadth_source', 'candidate_pool')}"
        ),
        (
            f"- 风险目标仓位 {float(latest.get('requested_exposure', 0)):.1%}；"
            f"已分配 {float(latest.get('allocated_exposure', 0)):.1%}；"
            f"约束后现金缓冲 {float(latest.get('unallocated_exposure', 0)):.1%}。"
        ),
        (
            f"- 实际仓位 {actual_exposure:.1%}；"
            + (
                f"配置下限 {minimum_exposure:.1%}（防守状态不强制）。"
                if minimum_suspended_reason
                else f"配置下限 {minimum_exposure:.1%}。"
            )
            + (
                " ⚠️ 当前低于下限，详见执行约束并将在下一交易日继续补足。"
                if actual_exposure + 1e-6 < minimum_exposure
                else ""
            )
        ),
        *(
            [
                "- 宽基ETF兜底：个股优选不足，已用 "
                + "、".join(str(item) for item in latest.get("etf_fallback_used") or [])
                + " 承接市场beta补足仓位。"
            ]
            if latest.get("etf_fallback_used")
            else []
        ),
        *(
            [
                "- 整手可执行性：已跳过 "
                f"{len(latest.get('unaffordable_symbols') or [])} 个在当前资金规模下"
                "无法按单票上限买入一手的高价标的，并将预算重新分配。"
            ]
            if latest.get("unaffordable_symbols")
            else []
        ),
        f"- 优选板块：{sectors}",
        *(
            [f"- 复盘结论：{_trim(reflection, 120)}"]
            if include_reflection
            else []
        ),
        "",
        "#### 全市场板块与选股研究",
        *_market_research_lines(dashboard),
        "",
        "#### 新闻与事件校验",
        *_news_research_lines(dashboard),
        "",
        "#### 下一交易日计划",
        *_plan_lines(dashboard),
        "",
        "> 本报告来自模拟账户，不连接券商，不构成投资建议。",
    ]
    return "\n".join(lines)


def build_position_report(
    dashboard: dict[str, Any],
    *,
    generated_at: datetime,
    title: str = "Quant Lab 午间盘位报告",
    max_holdings: int = 20,
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
    configuration = account.get("configuration") or {}
    minimum_exposure = float(
        configuration.get("minimum_invested_ratio", 0.0)
    )
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
            (
                "- 账户配置：初始资金 "
                f"**{_money(initial_cash)}**；模拟起点 "
                f"**{configuration.get('simulation_start_date', '—')}**；风险档 "
                f"**{RISK_PROFILE_NAMES.get(configuration.get('risk_profile', 'balanced'), configuration.get('risk_profile', 'balanced'))}**；"
                f"最低仓位 **{minimum_exposure:.0%}**"
            ),
            *(
                [
                    f"- ⚠️ 当前仓位 {position_ratio:.1%} 低于配置下限 "
                    f"{minimum_exposure:.1%}；午间任务不会提前回填开盘成交。"
                ]
                if position_ratio + 1e-6 < minimum_exposure
                else []
            ),
            "",
            f"#### 持仓明细（{len(dashboard.get('positions') or [])} 个）",
            *_holding_lines(dashboard, max_holdings),
            "",
            "#### 已生成的下一步计划",
            *_plan_lines(dashboard),
            "",
            (
                "> 午间任务只读上一份已完成的日线账本，不推进策略、不产生模拟成交；"
                "上方计划要到下一次日终任务取得完整日线后，才会按下一交易日开盘价回填成交。"
            ),
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


def _split_markdown_messages(
    content: str,
    maximum_bytes: int = 3500,
) -> list[str]:
    """Split a report without dropping lines or breaking UTF-8 characters."""
    if len(content.encode("utf-8")) <= maximum_bytes:
        return [content]

    chunks: list[str] = []
    current = ""
    for line in content.splitlines(keepends=True):
        if len((current + line).encode("utf-8")) <= maximum_bytes:
            current += line
            continue
        if current:
            chunks.append(current.rstrip("\n"))
            current = ""
        while len(line.encode("utf-8")) > maximum_bytes:
            piece = ""
            for character in line:
                if len((piece + character).encode("utf-8")) > maximum_bytes:
                    break
                piece += character
            chunks.append(piece.rstrip("\n"))
            line = line[len(piece) :]
        current = line
    if current:
        chunks.append(current.rstrip("\n"))

    total = len(chunks)
    return [
        f"### 日报分段 {index}/{total}\n{chunk}"
        for index, chunk in enumerate(chunks, start=1)
    ]


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
    messages = _split_markdown_messages(content)
    owns_client = client is None
    active_client = client or httpx.Client(timeout=timeout_seconds)
    try:
        for message_index, message in enumerate(messages, start=1):
            payload = {"msgtype": "markdown", "markdown": {"content": message}}
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
                    last_error_message = ""
                    break
                except httpx.HTTPStatusError as exc:
                    last_error_message = f"HTTP {exc.response.status_code}"
                except httpx.HTTPError as exc:
                    last_error_message = type(exc).__name__
                except (RuntimeError, ValueError) as exc:
                    last_error_message = str(exc)
                LOGGER.warning(
                    "企业微信第 %s/%s 段推送第 %s/%s 次失败：%s",
                    message_index,
                    len(messages),
                    attempt + 1,
                    retry_count + 1,
                    last_error_message,
                )
            if last_error_message:
                raise RuntimeError(
                    f"企业微信第 {message_index}/{len(messages)} 段推送失败，"
                    f"已重试 {retry_count} 次：{last_error_message}"
                )
    finally:
        if owns_client:
            active_client.close()


FEISHU_WEBHOOK_URL_PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"
# 飞书interactive卡片（lark_md）单条消息体上限约30KB，保守按字节分段。
FEISHU_CARD_CHUNK_LIMIT = 12_000


def _feishu_sign(timestamp: str, secret: str) -> str:
    """飞书自定义机器人签名校验：HMAC-SHA256(timestamp\nsecret) 后 base64。"""
    import base64
    import hashlib
    import hmac

    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _split_feishu_cards(
    content: str,
    limit: int = FEISHU_CARD_CHUNK_LIMIT,
) -> list[str]:
    """按字节分段，避免lark_md内容超限发送失败。"""
    chunks: list[str] = []
    current = ""
    for line in content.splitlines(keepends=True):
        if len((current + line).encode("utf-8")) > limit and current:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks or [""]


def send_feishu_markdown(
    webhook_url: str,
    content: str,
    *,
    secret: str = "",
    keyword: str = "",
    client: httpx.Client | None = None,
    timeout_seconds: int = 20,
    retry_count: int = 2,
) -> None:
    """向飞书群推送日报（interactive卡片 + lark_md渲染markdown）。

    支持机器人安全设置：关键词（每条消息前置关键词）与签名校验（timestamp+sign）。
    """
    import time

    if not webhook_url.startswith(FEISHU_WEBHOOK_URL_PREFIX):
        raise ValueError("FEISHU_WEBHOOK_URL 不是有效的飞书自定义机器人 Webhook")
    messages = _split_feishu_cards(content)
    owns_client = client is None
    active_client = client or httpx.Client(timeout=timeout_seconds)
    try:
        for message_index, chunk in enumerate(messages, start=1):
            body = chunk if not keyword else f"{keyword}\n{chunk}"
            payload: dict[str, Any] = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": "Quant Lab 模拟盘报告",
                        },
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {"tag": "lark_md", "content": body},
                        }
                    ],
                },
            }
            if secret:
                timestamp = str(int(time.time()))
                payload["timestamp"] = timestamp
                payload["sign"] = _feishu_sign(timestamp, secret)
            last_error_message = ""
            for attempt in range(retry_count + 1):
                try:
                    response = active_client.post(webhook_url, json=payload)
                    response.raise_for_status()
                    result = response.json()
                    code = result.get("code", result.get("StatusCode", 0))
                    if code not in (None, 0):
                        raise RuntimeError(
                            "飞书机器人拒绝消息："
                            f"{result.get('msg') or result.get('StatusMessage') or result}"
                        )
                    last_error_message = ""
                    break
                except httpx.HTTPStatusError as exc:
                    detail = ""
                    try:
                        body = exc.response.json()
                        detail = (
                            f" code={body.get('code')} "
                            f"{body.get('msg', '')}"
                        )
                    except Exception:
                        detail = f" {exc.response.text[:200]}"
                    last_error_message = (
                        f"HTTP {exc.response.status_code}{detail}"
                    )
                except httpx.HTTPError as exc:
                    last_error_message = type(exc).__name__
                except (RuntimeError, ValueError) as exc:
                    last_error_message = str(exc)
                LOGGER.warning(
                    "飞书第 %s/%s 段推送第 %s/%s 次失败：%s",
                    message_index,
                    len(messages),
                    attempt + 1,
                    retry_count + 1,
                    last_error_message,
                )
            if last_error_message:
                raise RuntimeError(
                    f"飞书第 {message_index}/{len(messages)} 段推送失败，"
                    f"已重试 {retry_count} 次：{last_error_message}"
                )
    finally:
        if owns_client:
            active_client.close()
def _configuration_changed(account: dict[str, Any], config: JobConfig) -> bool:
    stored = account.get("configuration") or {}
    return (
        stored.get("strategy_id") != config.strategy_id
        or stored.get("risk_profile", "balanced") != config.risk_profile
        or float(stored.get("minimum_invested_ratio", 0.0))
        != config.minimum_invested_ratio
        or stored.get("frequency", "1d") != config.frequency
        or stored.get("universe_mode", config.universe_mode) != config.universe_mode
        or (
            config.universe_mode == "fixed"
            and account.get("universe") != config.symbols
        )
        or float(account.get("initial_cash", 0)) != config.initial_cash
        or stored.get("backtest_start_date")
        != config.backtest_start_date.isoformat()
        or stored.get("backtest_end_date") != config.backtest_end_date.isoformat()
        or stored.get("simulation_start_date")
        != config.simulation_start_date.isoformat()
    )


def _active_market_universe(
    provider: TushareDataProvider,
    store: PaperStore,
    config: JobConfig,
    as_of_date: date,
    universe_config: MarketUniverseConfig,
    news_config: NewsResearchConfig,
    bocha_api_key: str,
    include_news: bool = True,
) -> tuple[list[str], dict[str, Any] | None]:
    if universe_config.mode != "full_market":
        return list(config.symbols), None

    account = store.account(config.account_id)
    required_symbols = [
        item["symbol"] for item in store.positions(config.account_id)
    ] if account else []
    if account:
        required_symbols.extend(
            item["symbol"] for item in account.get("pending_plan", [])
        )
    try:
        result = research_full_market(provider, as_of_date, universe_config)
        if news_config.enabled and include_news:
            result = enrich_with_bocha_news(
                result,
                bocha_api_key,
                news_config,
            )
        elif news_config.enabled:
            result.summary["news"] = {
                "enabled": False,
                "provider": "bocha",
                "sectors": [],
                "stocks": [],
                "errors": ["历史补算日不回看后来新闻，避免未来信息泄漏"],
            }
        selected = [
            item["symbol"]
            for item in result.summary["candidates"]
            if not item.get("excluded_by_news", False)
        ][: universe_config.detailed_candidate_count]
        active = list(
            dict.fromkeys(
                selected
                + universe_config.always_include_symbols
                + required_symbols
            )
        )
        if len(active) < 5:
            active = list(dict.fromkeys(active + config.symbols))
        result.summary["active_symbols"] = active
        result.summary["required_position_symbols"] = list(
            dict.fromkeys(required_symbols)
        )
        return active, result.summary
    except Exception as exc:
        LOGGER.exception("全市场研究失败，降级使用配置中的固定候选池")
        return list(dict.fromkeys(config.symbols + required_symbols)), {
            "mode": "degraded_fixed_fallback",
            "trade_date": as_of_date.isoformat(),
            "market_count": 0,
            "eligible_count": 0,
            "detailed_candidate_count": len(config.symbols),
            "top_sectors": [],
            "candidates": [],
            "factor_coverage": {},
            "warnings": [
                "全市场研究失败，本次仅使用配置中的固定候选池："
                f"{type(exc).__name__}: {exc}"
            ],
            "news": {
                "enabled": news_config.enabled,
                "provider": "bocha",
                "sectors": [],
                "stocks": [],
                "errors": ["因全市场结构化数据失败，未执行新闻研究"],
            },
        }


def _persist_research_context(
    store: PaperStore,
    config: JobConfig,
    active_symbols: list[str],
    market_research: dict[str, Any] | None,
) -> None:
    account = store.account(config.account_id)
    if account is None:
        return
    account["universe"] = active_symbols
    configuration = dict(account.get("configuration") or {})
    configuration["universe_mode"] = config.universe_mode
    account["configuration"] = configuration
    if market_research is not None:
        account["market_research"] = market_research
        research_date = str(market_research.get("trade_date") or "")
        if research_date:
            store.attach_market_research(
                config.account_id,
                research_date,
                market_research,
            )
        candidate_map = {
            item["symbol"]: item
            for item in market_research.get("candidates", [])
        }
        enriched_plan = []
        for plan in account.get("pending_plan", []):
            item = dict(plan)
            candidate = candidate_map.get(item["symbol"])
            if candidate and "全市场多因子" not in str(item.get("reason", "")):
                factors = candidate.get("factors") or {}
                factor_detail = "/".join(
                    f"{label}{float(factors.get(key, 0)):.2f}"
                    for key, label in (
                        ("valuation", "估"),
                        ("quality", "质"),
                        ("turnover", "换"),
                        ("fund_flow", "流"),
                    )
                )
                item["reason"] = (
                    f"{item['reason']}；全市场多因子 "
                    f"{float(candidate['factor_score']):.2f}"
                    f"（{factor_detail}）"
                )
                if candidate.get("news_risk_level") not in (None, "unknown"):
                    item["reason"] += (
                        f"；新闻风险 {candidate['news_risk_level']}"
                    )
            enriched_plan.append(item)
        account["pending_plan"] = enriched_plan
    store.save_account(account)


def run_daily_job(
    config: JobConfig,
    *,
    state_path: Path,
    as_of_date: date,
    tushare_token: str,
    force_reinitialize: bool = False,
    reinitialize_on_config_change: bool = True,
    market_universe: MarketUniverseConfig | None = None,
    news_research: NewsResearchConfig | None = None,
    bocha_api_key: str = "",
) -> dict[str, Any]:
    if as_of_date < config.simulation_start_date:
        raise ValueError(
            "任务日期早于模拟盘起点，请修改统一配置文件中的 simulation_start_date"
        )

    universe_config = market_universe or MarketUniverseConfig(
        mode=config.universe_mode
    )
    news_config = news_research or NewsResearchConfig(enabled=False)
    provider = TushareDataProvider(tushare_token)
    store = PaperStore(state_path)
    try:
        account = store.account(config.account_id)
        configuration_changed = (
            account is not None and _configuration_changed(account, config)
        )
        state_upgrade_required = bool(
            account and account.get("requires_reinitialize_reason")
        )
        should_reinitialize = (
            account is None
            or force_reinitialize
            or (configuration_changed and reinitialize_on_config_change)
            or (state_upgrade_required and reinitialize_on_config_change)
        )
        if should_reinitialize or config.universe_mode != "full_market":
            active_symbols, market_research_summary = _active_market_universe(
                provider,
                store,
                config,
                as_of_date,
                universe_config,
                news_config,
                bocha_api_key,
            )
        else:
            # Existing full-market accounts rebuild each missing date below.
            # Avoid querying today's snapshot/news before that chronological loop.
            active_symbols = list(config.symbols)
            market_research_summary = None
        effective_config = replace(config, symbols=active_symbols)
        if should_reinitialize:
            if (configuration_changed or state_upgrade_required) and not force_reinitialize:
                LOGGER.warning(
                    "检测到账户配置或账本引擎升级，自动重建账户 %s",
                    config.account_id,
                )
            LOGGER.info(
                "初始化模拟账户 %s，并历史回放到 %s",
                config.account_id,
                as_of_date,
            )
            research_date = (
                date.fromisoformat(str(market_research_summary["trade_date"]))
                if market_research_summary
                and market_research_summary.get("mode") == "full_market"
                else as_of_date
            )
            transition_end = research_date - timedelta(days=1)
            use_fixed_history_transition = (
                config.universe_mode == "full_market"
                and transition_end >= config.simulation_start_date
            )
            replay_symbols = (
                config.symbols
                if use_fixed_history_transition
                else effective_config.symbols
            )
            replay_end = (
                transition_end
                if use_fixed_history_transition
                else as_of_date
            )
            result = replay_paper_simulation(
                PaperSimulationRequest(
                    account_id=config.account_id,
                    strategy_id=config.strategy_id,
                    universe_mode=config.universe_mode,
                    symbols=replay_symbols,
                    backtest_start_date=config.backtest_start_date,
                    backtest_end_date=config.backtest_end_date,
                    simulation_start_date=config.simulation_start_date,
                    simulation_end_date=replay_end,
                    initial_cash=config.initial_cash,
                    risk_profile=config.risk_profile,
                    minimum_invested_ratio=config.minimum_invested_ratio,
                ),
                provider,
                store,
            )
            if "account" not in result:
                return result
            if use_fixed_history_transition:
                assert market_research_summary is not None
                transition_account = store.account(config.account_id) or {}
                transition_required = [
                    item["symbol"] for item in store.positions(config.account_id)
                ] + [
                    item["symbol"]
                    for item in transition_account.get("pending_plan", [])
                ]
                active_symbols = list(
                    dict.fromkeys(active_symbols + transition_required)
                )
                effective_config = replace(config, symbols=active_symbols)
                market_research_summary["active_symbols"] = active_symbols
                market_research_summary["required_position_symbols"] = list(
                    dict.fromkeys(transition_required)
                )
                market_research_summary["warnings"].append(
                    "全市场模式首次启用：历史模拟沿用配置中的固定池，"
                    f"自 {research_date} 起才按每日全市场截面动态选股，"
                    "避免用今天候选回看过去造成事后选股偏差。"
                )
                _persist_research_context(
                    store,
                    effective_config,
                    active_symbols,
                    market_research_summary,
                )
                result = advance_paper_simulation(
                    PaperAdvanceRequest(
                        account_id=config.account_id,
                        symbols=effective_config.symbols,
                        as_of_date=as_of_date,
                    ),
                    provider,
                    store,
                )
        else:
            if configuration_changed or state_upgrade_required:
                raise ValueError(
                    "仓库配置、账本引擎与持久化模拟账户不一致。"
                    "请手动运行工作流并启用 force_reinitialize 以重建账户。"
                )

            account = store.account(config.account_id) or {}
            last_date_text = account.get("last_date")
            open_dates: list[date] = []
            if config.universe_mode == "full_market" and last_date_text:
                open_dates = provider.fetch_open_dates(
                    date.fromisoformat(last_date_text) + timedelta(days=1),
                    as_of_date,
                )

            if open_dates:
                total_processed = 0
                researched_days = 0
                combined_errors: list[dict[str, str]] = []
                result = {}
                for index, replay_date in enumerate(open_dates):
                    active_symbols, market_research_summary = (
                        _active_market_universe(
                            provider,
                            store,
                            config,
                            replay_date,
                            universe_config,
                            news_config,
                            bocha_api_key,
                            include_news=index == len(open_dates) - 1,
                        )
                    )
                    if (
                        market_research_summary
                        and market_research_summary.get("mode") == "full_market"
                        and market_research_summary.get("trade_date")
                        != replay_date.isoformat()
                    ):
                        market_research_summary["warnings"].append(
                            f"{replay_date} 收盘数据尚未完整发布，逐日补算在此停止。"
                        )
                        break
                    effective_config = replace(config, symbols=active_symbols)
                    _persist_research_context(
                        store,
                        effective_config,
                        active_symbols,
                        market_research_summary,
                    )
                    LOGGER.info(
                        "逐日重建 %s 的全市场截面；详细候选池 %s 只",
                        replay_date,
                        len(active_symbols),
                    )
                    result = advance_paper_simulation(
                        PaperAdvanceRequest(
                            account_id=config.account_id,
                            symbols=effective_config.symbols,
                            as_of_date=replay_date,
                        ),
                        provider,
                        store,
                    )
                    metadata = result.get("run") or {}
                    total_processed += int(metadata.get("processed_days", 0))
                    researched_days += 1
                    combined_errors.extend(metadata.get("data_errors") or [])
                result["run"] = {
                    **(result.get("run") or {}),
                    "processed_days": total_processed,
                    "data_errors": combined_errors,
                    "point_in_time_research_days": researched_days,
                    "message": f"已逐日重建并处理 {total_processed} 个新交易日。",
                }
            else:
                if config.universe_mode == "full_market":
                    active_symbols, market_research_summary = (
                        _active_market_universe(
                            provider,
                            store,
                            config,
                            as_of_date,
                            universe_config,
                            news_config,
                            bocha_api_key,
                        )
                    )
                    effective_config = replace(config, symbols=active_symbols)
                _persist_research_context(
                    store,
                    effective_config,
                    active_symbols,
                    market_research_summary,
                )
                LOGGER.info(
                    "更新模拟账户 %s 到 %s；详细候选池 %s 只",
                    config.account_id,
                    as_of_date,
                    len(active_symbols),
                )
                result = advance_paper_simulation(
                    PaperAdvanceRequest(
                        account_id=config.account_id,
                        symbols=effective_config.symbols,
                        as_of_date=as_of_date,
                    ),
                    provider,
                    store,
                )

        run_metadata = result.get("run") or {}
        _persist_research_context(
            store,
            effective_config,
            active_symbols,
            market_research_summary,
        )
        dashboard = store.dashboard(config.account_id)
        dashboard["run"] = run_metadata
        if market_research_summary is not None:
            dashboard["market_research"] = market_research_summary
        return dashboard
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
        default=Path("config/quant-config.json"),
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
            )
            report_path = args.report or unified.midday_report_path
        else:
            dashboard = run_daily_job(
                config,
                state_path=state_path,
                as_of_date=as_of_date,
                tushare_token=unified.tushare_token,
                force_reinitialize=args.force_reinitialize,
                reinitialize_on_config_change=(
                    unified.reinitialize_on_config_change
                ),
                market_universe=unified.market_universe,
                news_research=unified.news_research,
                bocha_api_key=unified.bocha_api_key,
            )
            report = build_markdown_report(
                dashboard,
                generated_at=generated_at,
                title=unified.report_title,
                max_holdings=unified.max_holdings,
                include_reflection=unified.include_reflection,
            )
            report_path = args.report or unified.report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        print(report)

        if args.skip_wechat or not unified.wechat_enabled:
            LOGGER.warning("已跳过消息推送")
        elif unified.notification_provider == "feishu":
            if not unified.feishu_webhook_url:
                raise ValueError(
                    "飞书推送已启用，但 feishu_webhook_url 为空；"
                    "请填写配置文件或添加 FEISHU_WEBHOOK_URL Secret"
                )
            send_feishu_markdown(
                unified.feishu_webhook_url,
                report,
                secret=unified.feishu_webhook_secret,
                keyword=unified.feishu_webhook_keyword,
                timeout_seconds=unified.notification_timeout,
                retry_count=unified.notification_retries,
            )
            LOGGER.info(
                "飞书%s推送成功",
                (
                    unified.midday_report_title
                    if args.mode == "noon-position"
                    else unified.report_title
                ),
            )
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
            LOGGER.info(
                "企业微信%s推送成功",
                (
                    unified.midday_report_title
                    if args.mode == "noon-position"
                    else unified.report_title
                ),
            )
        return 0
    except Exception:
        LOGGER.exception("模拟盘定时任务失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
