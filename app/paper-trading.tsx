"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

const DEFAULT_SYMBOLS =
  "159611,002317,600183,603738,600367,000811,002714,300308,300502,688498,300394,002371,688008";

type PlanItem = {
  symbol: string;
  name: string;
  sector: string;
  action: "BUY" | "SELL" | "CLOSE";
  target_weight: number;
  reason: string;
  signal_price: number;
  score: number;
  signal_date: string;
  strategy_version: string;
};

type DailyJournal = {
  trade_date: string;
  strategy_id: string;
  strategy_name: string;
  review: {
    scheduled_count: number;
    executed_count: number;
    scheduled_actions: Array<{
      symbol: string;
      action: string;
      reason: string;
    }>;
    executions: Array<{
      symbol: string;
      action: string;
      price: number;
      quantity: number;
      reason: string;
    }>;
    unfilled_symbols: string[];
    daily_return: number;
    drawdown: number;
  };
  analysis: {
    market_regime: string;
    breadth: number;
    data_quality: number;
    top_sectors: Array<{ name: string }>;
    selected_symbols: string[];
    position_count: number;
    equity: number;
    cash: number;
  };
  decision: {
    action_count: number;
    actions: PlanItem[];
    summary: string;
    execution_timing: string;
  };
  reflection: {
    category: string;
    conclusion: string;
    evidence: string[];
    next_focus: string;
  };
};

type PaperDashboard = {
  account: {
    account_id: string;
    initial_cash: number;
    cash: number;
    peak_equity: number;
    current_version: string;
    last_date: string | null;
    pending_plan: PlanItem[];
    universe: string[];
    configuration: {
      strategy_id?: "moving_average" | "momentum" | "breakout";
      strategy_name?: string;
      risk_profile?: "balanced" | "aggressive";
      frequency?: "1d";
      backtest_start_date?: string;
      backtest_end_date?: string;
      simulation_start_date?: string;
      simulation_end_date?: string;
    };
  };
  latest: {
    trade_date: string;
    equity: number;
    cash: number;
    market_value: number;
    daily_return: number;
    drawdown: number;
    breadth: number;
    market_regime: string;
    data_quality: number;
    top_sectors: Array<{
      name: string;
      score: number;
      breadth: number;
      members: number;
    }>;
    selected_symbols: string[];
    strategy_version: string;
  } | null;
  positions: Array<{
    symbol: string;
    name: string;
    sector: string;
    shares: number;
    avg_price: number;
    entry_date: string;
  }>;
  executions: Array<{
    id: number;
    trade_date: string;
    symbol: string;
    name: string;
    sector: string;
    action: "BUY" | "SELL" | "CLOSE";
    quantity: number;
    price: number;
    commission: number;
    tax: number;
    slippage: number;
    reason: string;
    strategy_version: string;
  }>;
  reviews: Array<{
    id: number;
    trade_date: string;
    category: string;
    severity: string;
    diagnosis: string;
    evidence: string;
    recommendation: string;
  }>;
  daily_journals: DailyJournal[];
  journal_count: number;
  versions: Array<{
    version: string;
    status: "champion" | "challenger";
    reason: string;
    metrics: Record<string, number>;
  }>;
  upgrade_events: Array<{
    id: number;
    trade_date: string;
    from_version: string;
    to_version: string;
    decision: "PROMOTED" | "REJECTED";
    reason: string;
  }>;
  equity_curve: Array<{
    trade_date: string;
    equity: number;
    drawdown: number;
  }>;
  holding_summary?: {
    invested_days: number;
    first_holding_date: string | null;
    last_holding_date: string | null;
    last_exit_date: string | null;
    last_holding_positions: Array<{
      symbol: string;
      name: string;
      sector: string;
      shares: number;
      avg_price: number;
      entry_date: string;
    }>;
    last_exits: Array<{
      id: number;
      symbol: string;
      reason: string;
    }>;
  };
  run?: {
    mode: string;
    processed_days: number;
    backtest_days?: number;
    simulation_days?: number;
    data_errors: Array<{ symbol: string; message: string }>;
    message: string;
  };
};

type PaperForm = {
  account_id: string;
  strategy_id: "moving_average" | "momentum" | "breakout";
  risk_profile: "balanced" | "aggressive";
  symbols: string;
  backtest_start_date: string;
  backtest_end_date: string;
  simulation_start_date: string;
  simulation_end_date: string;
  initial_cash: number;
};

const currency = new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: "CNY",
  maximumFractionDigits: 0,
});

const ratio = new Intl.NumberFormat("zh-CN", {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

const PAPER_STRATEGIES = {
  moving_average: {
    name: "双均线趋势",
    description: "快均线高于慢均线时参与，反向交叉退出。",
  },
  momentum: {
    name: "价格动量",
    description: "选择20/60日涨幅领先且中短期趋势为正的股票。",
  },
  breakout: {
    name: "通道突破",
    description: "突破前20日高点入场，跌破10日退出通道平仓。",
  },
} as const;

function localDate(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

function parseSymbols(value: string): string[] {
  return value
    .split(/[\s,，;；]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  const body: unknown = await response
    .json()
    .catch(() => ({ detail: `服务返回 HTTP ${response.status}` }));
  if (!response.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? body.detail
        : "请求失败，请查看命令行";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body as T;
}

function actionLabel(action: PlanItem["action"]): string {
  return { BUY: "买入", SELL: "卖出 / 减仓", CLOSE: "平仓" }[action];
}

function EquitySparkline({
  points,
}: {
  points: PaperDashboard["equity_curve"];
}) {
  const path = useMemo(() => {
    if (points.length < 2) return "";
    const values = points.map((point) => point.equity);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    return points
      .map((point, index) => {
        const x = (index / (points.length - 1)) * 800;
        const y = 170 - ((point.equity - min) / span) * 150;
        return `${index ? "L" : "M"} ${x.toFixed(1)} ${y.toFixed(1)}`;
      })
      .join(" ");
  }, [points]);

  return (
    <div className="paper-equity-chart">
      {path ? (
        <svg viewBox="0 0 800 190" role="img" aria-label="模拟账户权益曲线">
          <line x1="0" x2="800" y1="170" y2="170" />
          <path d={path} />
        </svg>
      ) : (
        <span>初始化历史回放后显示权益曲线</span>
      )}
    </div>
  );
}

export function PaperTrading() {
  const [form, setForm] = useState<PaperForm>({
    account_id: "default",
    strategy_id: "moving_average",
    risk_profile: "balanced",
    symbols: DEFAULT_SYMBOLS,
    backtest_start_date: "2024-01-01",
    backtest_end_date: "2025-12-31",
    simulation_start_date: "2026-01-01",
    simulation_end_date: localDate(),
    initial_cash: 500_000,
  });
  const [dashboard, setDashboard] = useState<PaperDashboard | null>(null);
  const [journalDate, setJournalDate] = useState<string | null>(null);
  const [busy, setBusy] = useState<"replay" | "advance" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<PaperDashboard>("/api/paper/dashboard?account_id=default")
      .then((value) => {
        setDashboard(value);
        setForm((current) => ({
          ...current,
          symbols: value.account.universe.join(","),
          initial_cash: value.account.initial_cash,
          strategy_id:
            value.account.configuration.strategy_id ?? current.strategy_id,
          risk_profile:
            value.account.configuration.risk_profile ?? current.risk_profile,
          backtest_start_date:
            value.account.configuration.backtest_start_date ??
            current.backtest_start_date,
          backtest_end_date:
            value.account.configuration.backtest_end_date ??
            current.backtest_end_date,
          simulation_start_date:
            value.account.configuration.simulation_start_date ??
            current.simulation_start_date,
          simulation_end_date:
            value.account.configuration.simulation_end_date ??
            current.simulation_end_date,
        }));
      })
      .catch(() => undefined);
  }, []);

  async function replay(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy("replay");
    setError(null);
    try {
      const result = await api<PaperDashboard>("/api/paper/replay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...form, symbols: parseSymbols(form.symbols) }),
      });
      setDashboard(result);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "历史回放失败");
    } finally {
      setBusy(null);
    }
  }

  async function advance() {
    setBusy("advance");
    setError(null);
    try {
      const result = await api<PaperDashboard>("/api/paper/advance", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_id: form.account_id,
          symbols: parseSymbols(form.symbols),
          as_of_date: localDate(),
        }),
      });
      setDashboard(result);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "今日模拟失败");
    } finally {
      setBusy(null);
    }
  }

  const latest = dashboard?.latest;
  const pnl = latest
    ? latest.equity / dashboard.account.initial_cash - 1
    : 0;
  const plan = dashboard?.account.pending_plan ?? [];
  const journals = dashboard?.daily_journals ?? [];
  const selectedJournal =
    journals.find((journal) => journal.trade_date === journalDate) ??
    journals[0];

  return (
    <section className="paper-panel" id="paper-trading">
      <div className="paper-heading">
        <div>
          <p className="section-label">每日模拟交易 / PAPER ACCOUNT</p>
          <h2>中短期趋势组合驾驶舱</h2>
          <p>
            回测期只验证策略，不动资金；模拟期才从初始资金开始，每个交易日收盘后决定买入、减仓或平仓，并在下一交易日开盘模拟成交。
          </p>
        </div>
        <div className="paper-mode">
          <span>安全边界</span>
          <strong>模拟账户</strong>
          <small>未连接券商 · 不会发送真实订单</small>
        </div>
      </div>

      {error && (
        <div className="error-banner paper-error" role="alert">
          <strong>模拟任务失败</strong>
          <span>{error}</span>
        </div>
      )}
      {dashboard?.run && (
        <div className="paper-run-note">
          {dashboard.run.message}
          {Object.keys(dashboard.run.data_errors).length > 0 &&
            ` · ${Object.keys(dashboard.run.data_errors).length} 个标的数据异常`}
        </div>
      )}

      <div className="paper-layout">
        <form className="paper-form" onSubmit={replay}>
          <div className="paper-form-title">
            <div>
              <span>模拟配置</span>
              <strong>账户、策略与时间范围</strong>
            </div>
            <span className="paper-config-badge">日频</span>
          </div>
          <label>
            账户标识
            <input
              value={form.account_id}
              onChange={(event) =>
                setForm({ ...form, account_id: event.target.value })
              }
              pattern="[a-zA-Z0-9_-]{1,32}"
              required
            />
          </label>
          <label>
            候选标的（至少 5 个）
            <textarea
              rows={4}
              value={form.symbols}
              onChange={(event) =>
                setForm({ ...form, symbols: event.target.value })
              }
              required
            />
          </label>
          <label>
            日频模拟策略
            <select
              value={form.strategy_id}
              onChange={(event) =>
                setForm({
                  ...form,
                  strategy_id: event.target.value as PaperForm["strategy_id"],
                })
              }
            >
              {Object.entries(PAPER_STRATEGIES).map(([id, strategy]) => (
                <option key={id} value={id}>
                  {strategy.name}
                </option>
              ))}
            </select>
            <small className="paper-strategy-description">
              {PAPER_STRATEGIES[form.strategy_id].description} 固定为每日收盘计算；
              修改后需重新初始化账户。
            </small>
          </label>
          <label>
            投资风险档
            <select
              value={form.risk_profile}
              onChange={(event) =>
                setForm({
                  ...form,
                  risk_profile: event.target.value as PaperForm["risk_profile"],
                })
              }
            >
              <option value="balanced">均衡型（90% / 45% / 15%）</option>
              <option value="aggressive">进取型（95% / 75% / 35%）</option>
            </select>
            <small className="paper-strategy-description">
              依次对应进攻、谨慎和防守市场；进取型仍保留单票25%和最多5只的上限。
            </small>
          </label>
          <p className="paper-stage-label">① 回测期 · 只评估策略</p>
          <div className="field-row">
            <label>
              回测起点
              <input
                type="date"
                value={form.backtest_start_date}
                onChange={(event) =>
                  setForm({
                    ...form,
                    backtest_start_date: event.target.value,
                  })
                }
              />
            </label>
            <label>
              回测终点
              <input
                type="date"
                value={form.backtest_end_date}
                onChange={(event) =>
                  setForm({
                    ...form,
                    backtest_end_date: event.target.value,
                  })
                }
              />
            </label>
          </div>
          <p className="paper-stage-label">② 模拟期 · 启用虚拟资金</p>
          <div className="field-row">
            <label>
              模拟盘起点
              <input
                type="date"
                value={form.simulation_start_date}
                min={form.backtest_end_date}
                onChange={(event) =>
                  setForm({
                    ...form,
                    simulation_start_date: event.target.value,
                  })
                }
              />
            </label>
            <label>
              模拟截至
              <input
                type="date"
                value={form.simulation_end_date}
                min={form.simulation_start_date}
                onChange={(event) =>
                  setForm({
                    ...form,
                    simulation_end_date: event.target.value,
                  })
                }
              />
            </label>
          </div>
          <label>
            初始虚拟资金
            <input
              type="number"
              min={50_000}
              step={10_000}
              value={form.initial_cash}
              onChange={(event) =>
                setForm({ ...form, initial_cash: Number(event.target.value) })
              }
            />
          </label>
          <button className="run-button" type="submit" disabled={busy !== null}>
            {busy === "replay"
              ? "正在逐日推演…"
              : "初始化并逐日历史推演"}
          </button>
          <button
            className="paper-daily-button"
            type="button"
            disabled={busy !== null || !dashboard}
            onClick={() => void advance()}
          >
            {busy === "advance" ? "正在更新今日数据…" : "更新今日数据"}
          </button>
          <p className="paper-reset-warning">
            初始化会重置同名虚拟账户。回测终点必须早于模拟起点；模拟起点当天收盘生成首批信号，下一交易日才可能成交。
          </p>
        </form>

        <div className="paper-main-column">
          <div className="paper-dashboard">
            <div className="paper-metrics">
            <article>
              <span>账户权益</span>
              <strong>{latest ? currency.format(latest.equity) : "—"}</strong>
              <small className={pnl >= 0 ? "positive" : "negative"}>
                累计 {latest ? ratio.format(pnl) : "—"}
              </small>
            </article>
            <article>
              <span>可用现金</span>
              <strong>
                {dashboard ? currency.format(dashboard.account.cash) : "—"}
              </strong>
              <small>{latest ? `仓位 ${ratio.format(latest.market_value / latest.equity)}` : "—"}</small>
            </article>
            <article>
              <span>市场状态</span>
              <strong>{latest?.market_regime ?? "—"}</strong>
              <small>{latest ? `趋势宽度 ${ratio.format(latest.breadth)}` : "—"}</small>
            </article>
            <article>
              <span>当前策略</span>
              <strong>
                {dashboard?.account.configuration.strategy_name ??
                  PAPER_STRATEGIES[form.strategy_id].name}
              </strong>
              <small>
                日频 · {dashboard?.account.current_version ?? "待初始化"}
              </small>
            </article>
            </div>

            <EquitySparkline points={dashboard?.equity_curve ?? []} />

            <div className="paper-context">
              <div>
                <span>信号日期</span>
                <strong>{dashboard?.account.last_date ?? "尚未初始化"}</strong>
              </div>
              <div>
                <span>数据完整度</span>
                <strong>{latest ? ratio.format(latest.data_quality) : "—"}</strong>
              </div>
              <div>
                <span>优选板块</span>
                <strong>
                  {latest?.top_sectors.map((sector) => sector.name).join(" · ") ||
                    "—"}
                </strong>
              </div>
            </div>
          </div>

          <article className="paper-card daily-journal-card">
        <div className="paper-card-heading">
          <div>
            <p className="section-label">DAILY DECISION LOOP</p>
            <h3>每日决策 · 回顾 · 分析</h3>
          </div>
          <div className="journal-date-control">
            <span>已记录 {dashboard?.journal_count ?? 0} 个交易日</span>
            <select
              value={selectedJournal?.trade_date ?? ""}
              onChange={(event) => setJournalDate(event.target.value)}
              disabled={!journals.length}
              aria-label="选择决策日志日期"
            >
              {!journals.length && (
                <option value="">暂无日志</option>
              )}
              {journals.map((journal) => (
                <option key={journal.trade_date} value={journal.trade_date}>
                  {journal.trade_date}
                </option>
              ))}
            </select>
          </div>
        </div>
        {selectedJournal ? (
          <div className="daily-cycle-grid">
            <section>
              <span className="daily-cycle-index">01 · 回顾</span>
              <strong>
                到期计划 {selectedJournal.review.scheduled_count} 条，成交{" "}
                {selectedJournal.review.executed_count} 条
              </strong>
              <p>
                当日收益 {ratio.format(selectedJournal.review.daily_return)} ·
                回撤 {ratio.format(selectedJournal.review.drawdown)}
              </p>
              <small>
                {selectedJournal.review.executions.length
                  ? selectedJournal.review.executions
                      .map(
                        (item) =>
                          `${actionLabel(item.action as PlanItem["action"])} ${item.symbol}`,
                      )
                      .join("；")
                  : "当日没有到期成交，仍完成账户状态检查。"}
              </small>
            </section>
            <section>
              <span className="daily-cycle-index">02 · 分析</span>
              <strong>
                {selectedJournal.analysis.market_regime} · 趋势宽度{" "}
                {ratio.format(selectedJournal.analysis.breadth)}
              </strong>
              <p>
                数据完整度 {ratio.format(selectedJournal.analysis.data_quality)}
                {" · "}持仓 {selectedJournal.analysis.position_count} 只
              </p>
              <small>
                优选板块：
                {selectedJournal.analysis.top_sectors
                  .map((sector) => sector.name)
                  .join("、") || "无"}
              </small>
            </section>
            <section>
              <span className="daily-cycle-index">03 · 决策</span>
              <strong>
                {selectedJournal.decision.action_count
                  ? `${selectedJournal.decision.action_count} 条下一日指令`
                  : "明确无操作"}
              </strong>
              <p>{selectedJournal.decision.summary}</p>
              <small>{selectedJournal.decision.execution_timing}</small>
            </section>
            <section>
              <span className="daily-cycle-index">04 · 复盘</span>
              <strong>
                {selectedJournal.reflection.category} ·{" "}
                {selectedJournal.reflection.conclusion}
              </strong>
              <p>{selectedJournal.reflection.evidence.join("；")}</p>
              <small>下一日关注：{selectedJournal.reflection.next_focus}</small>
            </section>
          </div>
        ) : (
          <p className="paper-empty">
            重新初始化模拟账户后，每个模拟交易日都会在这里生成完整日志。
          </p>
        )}
          </article>

          <div className="paper-grid">
        <article className="paper-card paper-orders">
          <div className="paper-card-heading">
            <div>
              <p className="section-label">NEXT SESSION</p>
              <h3>下一交易日执行计划</h3>
            </div>
            <span>
              {plan.length ? `${plan.length} 条指令` : "1 项无操作决策"}
            </span>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>动作</th>
                  <th>标的 / 板块</th>
                  <th>目标仓位</th>
                  <th>信号价</th>
                  <th>原因</th>
                </tr>
              </thead>
              <tbody>
                {plan.map((item) => (
                  <tr key={`${item.action}-${item.symbol}`}>
                    <td>
                      <span className={`paper-action ${item.action.toLowerCase()}`}>
                        {actionLabel(item.action)}
                      </span>
                    </td>
                    <td>
                      <strong>{item.symbol}</strong>
                      <small>{item.name} · {item.sector}</small>
                    </td>
                    <td>{ratio.format(item.target_weight)}</td>
                    <td>¥{item.signal_price.toFixed(2)}</td>
                    <td className="paper-reason">{item.reason}</td>
                  </tr>
                ))}
                {!plan.length && (
                  <tr>
                    <td colSpan={5} className="plan-no-action">
                      <span className="paper-action no-action">无操作</span>
                      <strong>
                        {selectedJournal?.decision.summary ??
                          "尚未完成今日分析，请先初始化或更新今日数据。"}
                      </strong>
                      <small>
                        {selectedJournal
                          ? `${selectedJournal.strategy_name} · ${selectedJournal.analysis.market_regime} · 趋势宽度 ${ratio.format(selectedJournal.analysis.breadth)}`
                          : "系统不会为了填满计划而伪造买卖信号。"}
                      </small>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </article>

        <article className="paper-card">
          <div className="paper-card-heading">
            <div>
              <p className="section-label">PORTFOLIO</p>
              <h3>当前持仓</h3>
            </div>
            <span>{dashboard?.positions.length ?? 0} / 5</span>
          </div>
          <div className="paper-list">
            {dashboard?.positions.map((position) => (
              <div key={position.symbol}>
                <div>
                  <strong>{position.symbol}</strong>
                  <span>{position.name} · {position.sector}</span>
                </div>
                <div>
                  <strong>{position.shares.toLocaleString("zh-CN")} 股</strong>
                  <span>成本 ¥{position.avg_price.toFixed(2)}</span>
                </div>
              </div>
            ))}
            {!dashboard?.positions.length && (
              <div className="paper-empty-position">
                <strong>当前确实为空仓</strong>
                <p>
                  {dashboard?.holding_summary?.last_exit_date
                    ? `${dashboard.holding_summary.last_exit_date} 执行了最近一次平仓`
                    : "尚未发生模拟买入"}
                </p>
                {dashboard?.holding_summary?.last_exits
                  .slice(0, 3)
                  .map((execution) => (
                    <small key={execution.id}>
                      {execution.symbol} · {execution.reason}
                    </small>
                  ))}
              </div>
            )}
            {!dashboard?.positions.length &&
              Boolean(
                dashboard?.holding_summary?.last_holding_positions.length,
              ) && (
                <div className="paper-history-position">
                  <div className="paper-history-title">
                    <strong>
                      最近持仓 ·{" "}
                      {dashboard?.holding_summary?.last_holding_date}
                    </strong>
                    <span>
                      历史持仓{" "}
                      {dashboard?.holding_summary?.invested_days ?? 0} 天
                    </span>
                  </div>
                  {dashboard?.holding_summary?.last_holding_positions.map(
                    (position) => (
                      <div
                        className="paper-history-row"
                        key={`history-${position.symbol}`}
                      >
                        <div>
                          <strong>{position.symbol}</strong>
                          <span>
                            {position.name} · {position.sector}
                          </span>
                        </div>
                        <div>
                          <strong>
                            {position.shares.toLocaleString("zh-CN")} 股
                          </strong>
                          <span>成本 ¥{position.avg_price.toFixed(2)}</span>
                        </div>
                      </div>
                    ),
                  )}
                </div>
              )}
          </div>
        </article>
          </div>

          <div className="paper-grid">
        <article className="paper-card">
          <div className="paper-card-heading">
            <div>
              <p className="section-label">DIAGNOSIS</p>
              <h3>异常反思</h3>
            </div>
            <span>数据 / 执行 / 策略 / 市场</span>
          </div>
          <div className="review-list">
            {dashboard?.reviews.slice(0, 5).map((review) => (
              <div key={review.id}>
                <span className={`review-severity ${review.severity.toLowerCase()}`}>
                  {review.category}
                </span>
                <strong>{review.diagnosis}</strong>
                <p>{review.evidence}</p>
                <small>下一步：{review.recommendation}</small>
              </div>
            ))}
            {!dashboard?.reviews.length && (
              <p className="paper-empty">暂无达到阈值的异常；这不等于策略没有风险。</p>
            )}
          </div>
        </article>

        <article className="paper-card">
          <div className="paper-card-heading">
            <div>
              <p className="section-label">CHAMPION / CHALLENGER</p>
              <h3>自动升级实验室</h3>
            </div>
            <span>仅模拟晋级</span>
          </div>
          <div className="version-list">
            {dashboard?.versions.map((version) => (
              <div key={version.version}>
                <div>
                  <strong>{version.version}</strong>
                  <span className={version.status}>{version.status}</span>
                </div>
                <small>
                  样本外夏普{" "}
                  {version.metrics.oos_sharpe === undefined
                    ? "待评估"
                    : version.metrics.oos_sharpe.toFixed(2)}
                  {" · "}样本外回撤{" "}
                  {version.metrics.oos_max_drawdown === undefined
                    ? "待评估"
                    : ratio.format(version.metrics.oos_max_drawdown)}
                </small>
              </div>
            ))}
          </div>
          {dashboard?.upgrade_events[0] && (
            <div className="upgrade-note">
              <strong>
                {dashboard.upgrade_events[0].decision === "PROMOTED"
                  ? "已晋级"
                  : "未晋级"}
                ：{dashboard.upgrade_events[0].to_version}
              </strong>
              <p>{dashboard.upgrade_events[0].reason}</p>
            </div>
          )}
          <p className="paper-guardrail">
            挑战者必须在样本外夏普领先至少 0.15、回撤不明显恶化且交易样本充足；系统不会生成任意代码，也不会接入真实账户。
          </p>
        </article>
          </div>
        </div>
      </div>
    </section>
  );
}
