"use client";

import { useMemo } from "react";

import type { PlanAction, PaperDashboard } from "./types";

// --------------- 格式化工具 ---------------

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

export const currency = new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: "CNY",
  maximumFractionDigits: 0,
});

export const ratio = new Intl.NumberFormat("zh-CN", {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

export const PAPER_STRATEGIES = {
  moving_average: { name: "双均线趋势", description: "快均线高于慢均线时参与，反向交叉退出。" },
  momentum: { name: "价格动量", description: "选择20/60日涨幅领先且中短期趋势为正的股票。" },
  breakout: { name: "通道突破", description: "突破前20日高点入场，跌破10日退出通道平仓。" },
} as const;

export function localDate(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

export function parseSymbols(value: string): string[] {
  return value
    .split(/[\s,，;；]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  const body: unknown = await response
    .json()
    .catch(() => ({ detail: `服务返回 HTTP ${response.status}` }));
  if (!response.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? (body as Record<string, unknown>).detail
        : "请求失败，请查看命令行";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body as T;
}

export function actionLabel(action: PlanAction): string {
  return { BUY: "买入", SELL: "卖出 / 减仓", CLOSE: "平仓" }[action];
}

// --------------- 共享组件 ---------------

/** 权益曲线 Sparkline */
export function EquitySparkline({ points }: { points: PaperDashboard["equity_curve"] }) {
  const path = useMemo(() => {
    if (points.length < 2) return "";
    const values = points.map((p) => p.equity);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    return points
      .map((p, i) => {
        const x = (i / (points.length - 1)) * 800;
        const y = 170 - ((p.equity - min) / span) * 150;
        return `${i ? "L" : "M"} ${x.toFixed(1)} ${y.toFixed(1)}`;
      })
      .join(" ");
  }, [points]);

  return (
    <div className="equity-chart">
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

/** 模拟盘指标卡片（4 列） */
export function MetricsRow({
  equity,
  pnl,
  cash,
  investedRatio,
  regime,
  breadth,
  strategyName,
  version,
}: {
  equity: number | null;
  pnl: number;
  cash: number | null;
  investedRatio: string;
  regime: string | null;
  breadth: string;
  strategyName: string;
  version: string;
}) {
  return (
    <div className="metrics-row">
      <article>
        <span className="metric-label">账户权益</span>
        <strong>{equity != null ? currency.format(equity) : "—"}</strong>
        <small className={pnl >= 0 ? "metric-positive" : "metric-negative"}>
          累计 {ratio.format(pnl)}
        </small>
      </article>
      <article>
        <span className="metric-label">可用现金</span>
        <strong>{cash != null ? currency.format(cash) : "—"}</strong>
        <small>仓位 {investedRatio}</small>
      </article>
      <article>
        <span className="metric-label">市场状态</span>
        <strong>{regime ?? "—"}</strong>
        <small>趋势宽度 {breadth}</small>
      </article>
      <article>
        <span className="metric-label">当前策略</span>
        <strong>{strategyName}</strong>
        <small>日频 · {version}</small>
      </article>
    </div>
  );
}

/** 顶部系统状态栏 */
export function SystemBar({ connected, tushareOk }: { connected: boolean; tushareOk: boolean }) {
  return (
    <header className="topbar">
      <div>
        <p className="eyebrow">LOCAL PAPER TRADING DESK</p>
        <h1>Quant Lab</h1>
      </div>
      <div className="system-state" aria-label="系统状态">
        <span className={`status-dot ${connected ? "" : "offline"}`} />
        <div>
          <strong>{connected ? "模拟交易服务已连接" : "等待本地服务"}</strong>
          <small>
            {connected
              ? `日频模拟 · ${tushareOk ? "Tushare 已配置" : "Tushare 未配置"}`
              : "请重新启动 start-quant-lab.cmd"}
          </small>
        </div>
      </div>
    </header>
  );
}

/** 错误横幅 */
export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="error-banner" role="alert">
      <strong>模拟任务失败</strong>
      <span>{message}</span>
    </div>
  );
}

/** 运行备注 */
export function RunNote({ run }: { run: PaperDashboard["run"] | undefined }) {
  if (!run) return null;
  return (
    <div className="run-note">
      {run.message}
      {run.data_errors.length > 0 && ` · ${run.data_errors.length} 个标的数据异常`}
    </div>
  );
}

/** 安全边界章 */
export function SafetyBadge() {
  return (
    <div className="safety-badge">
      <span>安全边界</span>
      <strong>模拟账户</strong>
      <small>未连接券商 · 不会发送真实订单</small>
    </div>
  );
}

/** section label (英文大写标记) */
export function SectionLabel({ children }: { children: string }) {
  return <p className="section-label">{children}</p>;
}
