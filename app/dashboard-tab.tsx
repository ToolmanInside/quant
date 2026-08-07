"use client";

import type { PaperDashboard, PlanItem } from "./types";
import { MetricsRow, EquitySparkline, SectionLabel, RunNote, ratio, actionLabel } from "./shared";

type Props = {
  dashboard: PaperDashboard | null;
  plan: PlanItem[];
  pnl: number;
  busy: "replay" | "advance" | null;
  onAdvance: () => void;
};

export function DashboardTab({ dashboard, plan, pnl, busy, onAdvance }: Props) {
  const latest = dashboard?.latest;

  const investedRatio = latest
    ? ratio.format(latest.market_value / latest.equity)
    : "—";
  const breadth = latest ? ratio.format(latest.breadth) : "—";

  return (
    <div className="dashboard-tab">
      <RunNote run={dashboard?.run} />

      {/* 指标卡片 */}
      <MetricsRow
        equity={latest?.equity ?? null}
        pnl={pnl}
        cash={dashboard?.account.cash ?? null}
        investedRatio={investedRatio}
        regime={latest?.market_regime ?? null}
        breadth={breadth}
        strategyName={
          dashboard?.account.configuration.strategy_name ?? "双均线趋势"
        }
        version={dashboard?.account.current_version ?? "待初始化"}
      />

      {/* 权益曲线 */}
      <EquitySparkline points={dashboard?.equity_curve ?? []} />

      {/* 上下文条 */}
      <div className="paper-context">
        <div>
          <span>信号日期</span>
          <strong>{dashboard?.account.last_date ?? "尚未初始化"}</strong>
        </div>
        <div>
          <span>数据完整度</span>
          <strong>
            {latest ? ratio.format(latest.data_quality) : "—"}
          </strong>
        </div>
        <div>
          <span>优选板块</span>
          <strong>
            {latest?.top_sectors.map((s) => s.name).join(" · ") || "—"}
          </strong>
        </div>
      </div>

      {/* 下一交易日执行计划 */}
      <div className="card">
        <div className="card-heading">
          <div>
            <SectionLabel>NEXT SESSION</SectionLabel>
            <h3>下一交易日执行计划</h3>
          </div>
          <span>{plan.length ? `${plan.length} 条指令` : "暂无待执行计划"}</span>
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
                    <span className={`action-pill ${item.action.toLowerCase()}`}>
                      {actionLabel(item.action)}
                    </span>
                  </td>
                  <td>
                    <strong>{item.symbol}</strong>
                    <small>{item.name} · {item.sector}</small>
                  </td>
                  <td>{ratio.format(item.target_weight)}</td>
                  <td>¥{item.signal_price.toFixed(2)}</td>
                  <td className="reason-cell">{item.reason}</td>
                </tr>
              ))}
              {!plan.length && (
                <tr>
                  <td colSpan={5} className="no-action-cell">
                    <span className="action-pill no-action">无操作</span>
                    <strong>
                      {dashboard?.daily_journals[0]?.decision.summary ??
                        "尚未完成今日分析，请先初始化或更新今日数据。"}
                    </strong>
                    <small>
                      系统不会为了填满计划而伪造买卖信号。
                    </small>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* 当前持仓 */}
      <div className="card">
        <div className="card-heading">
          <div>
            <SectionLabel>PORTFOLIO</SectionLabel>
            <h3>当前持仓</h3>
          </div>
          <span>{dashboard?.positions.length ?? 0} / 5</span>
        </div>
        <div className="position-list">
          {dashboard?.positions.map((pos) => (
            <div key={pos.symbol} className="position-row">
              <div>
                <strong>{pos.symbol}</strong>
                <span>{pos.name} · {pos.sector}</span>
              </div>
              <div>
                <strong>{pos.shares.toLocaleString("zh-CN")} 股</strong>
                <span>成本 ¥{pos.avg_price.toFixed(2)}</span>
              </div>
            </div>
          ))}
          {!dashboard?.positions.length && (
            <div className="empty-position">
              <strong>当前确实为空仓</strong>
              <p>
                {dashboard?.holding_summary?.last_exit_date
                  ? `${dashboard.holding_summary.last_exit_date} 执行了最近一次平仓`
                  : "尚未发生模拟买入"}
              </p>
              {dashboard?.holding_summary?.last_exits?.slice(0, 3).map((e) => (
                <small key={e.id}>{e.symbol} · {e.reason}</small>
              ))}
            </div>
          )}
          {/* 历史持仓（当前空仓但有历史） */}
          {!dashboard?.positions.length &&
            Boolean(dashboard?.holding_summary?.last_holding_positions.length) && (
              <div className="history-position">
                <div className="history-pos-title">
                  <strong>
                    最近持仓 · {dashboard?.holding_summary?.last_holding_date}
                  </strong>
                  <span>
                    历史持仓 {dashboard?.holding_summary?.invested_days ?? 0} 天
                  </span>
                </div>
                {dashboard?.holding_summary?.last_holding_positions.map((pos) => (
                  <div className="position-row" key={`history-${pos.symbol}`}>
                    <div>
                      <strong>{pos.symbol}</strong>
                      <span>{pos.name} · {pos.sector}</span>
                    </div>
                    <div>
                      <strong>{pos.shares.toLocaleString("zh-CN")} 股</strong>
                      <span>成本 ¥{pos.avg_price.toFixed(2)}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
        </div>
      </div>

      {/* 快速操作 —— 只有一行，放底部不抢眼 */}
      <div className="quick-actions">
        <button
          className="advance-btn"
          type="button"
          disabled={busy !== null || !dashboard}
          onClick={onAdvance}
        >
          {busy === "advance" ? "正在更新今日数据…" : "更新今日数据"}
        </button>
      </div>
    </div>
  );
}
