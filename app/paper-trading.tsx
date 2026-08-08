"use client";

import { useEffect, useState, FormEvent } from "react";

import type { PaperDashboard, PaperForm } from "./types";
import {
  api,
  localDate,
  parseSymbols,
  ErrorBanner,
  SafetyBadge,
} from "./shared";
import { DashboardTab } from "./dashboard-tab";
import { JournalTab } from "./journal-tab";
import { ConfigTab } from "./config-tab";

// --------------- 默认值 ---------------

const DEFAULT_SYMBOLS =
  "159611,002317,600183,603738,600367,000811,002714,300308,300502,688498,300394,002371,688008";

const DEFAULT_FORM: PaperForm = {
  account_id: "default",
  strategy_id: "moving_average",
  risk_profile: "balanced",
  minimum_invested_ratio: 0,
  symbols: DEFAULT_SYMBOLS,
  backtest_start_date: "2024-01-01",
  backtest_end_date: "2025-12-31",
  simulation_start_date: "2026-01-01",
  simulation_end_date: localDate(),
  initial_cash: 500_000,
};

type Tab = "dashboard" | "journal" | "config";

// --------------- 组件 ---------------

export function PaperTrading() {
  const [form, setForm] = useState<PaperForm>(DEFAULT_FORM);
  const [dashboard, setDashboard] = useState<PaperDashboard | null>(null);
  const [journalDate, setJournalDate] = useState<string | null>(null);
  const [busy, setBusy] = useState<"replay" | "advance" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("dashboard");

  // 启动时拉取已有账户状态
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
          minimum_invested_ratio:
            value.account.configuration.minimum_invested_ratio ??
            current.minimum_invested_ratio,
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

  // 派生值
  const latest = dashboard?.latest;
  const pnl = latest
    ? latest.equity / dashboard.account.initial_cash - 1
    : 0;
  const plan = dashboard?.account.pending_plan ?? [];
  const journals = dashboard?.daily_journals ?? [];
  const selectedJournal =
    journals.find((j) => j.trade_date === journalDate) ?? journals[0];

  const tabs: Array<{ key: Tab; label: string }> = [
    { key: "dashboard", label: "📊 驾驶舱" },
    { key: "journal", label: "📋 决策日志" },
    { key: "config", label: "⚙️ 配置" },
  ];

  return (
    <section className="paper-panel" id="paper-trading">
      {/* 页头 */}
      <div className="paper-heading">
        <div>
          <p className="section-label">每日模拟交易 / PAPER ACCOUNT</p>
          <h2>中短期趋势组合驾驶舱</h2>
          <p>
            回测期只验证策略，不动资金；模拟期才从初始资金开始，每个交易日收盘后决定买入、减仓或平仓，并在下一交易日开盘模拟成交。
          </p>
        </div>
        <SafetyBadge />
      </div>

      {error && <ErrorBanner message={error} />}

      {/* Tab 导航 */}
      <nav className="tab-nav">
        {tabs.map(({ key, label }) => (
          <button
            key={key}
            type="button"
            className={`tab-btn ${tab === key ? "active" : ""}`}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </nav>

      {/* Tab 内容 */}
      <div className="tab-content">
        {tab === "dashboard" && (
          <DashboardTab
            dashboard={dashboard}
            plan={plan}
            pnl={pnl}
            busy={busy}
            onAdvance={advance}
          />
        )}
        {tab === "journal" && (
          <JournalTab
            dashboard={dashboard}
            journals={journals}
            selectedJournal={selectedJournal}
            onSelectDate={setJournalDate}
          />
        )}
        {tab === "config" && (
          <ConfigTab
            form={form}
            dashboard={dashboard}
            busy={busy}
            onChangeForm={setForm}
            onSubmit={replay}
          />
        )}
      </div>
    </section>
  );
}
