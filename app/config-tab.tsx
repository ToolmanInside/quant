"use client";

import type { PaperForm, PaperDashboard } from "./types";
import { PAPER_STRATEGIES, ratio } from "./shared";

type Props = {
  form: PaperForm;
  dashboard: PaperDashboard | null;
  busy: "replay" | "advance" | null;
  onChangeForm: (next: PaperForm) => void;
  onSubmit: (event: React.FormEvent<HTMLFormElement>) => void;
};

export function ConfigTab({ form, dashboard, busy, onChangeForm, onSubmit }: Props) {
  return (
    <div className="config-layout">
      <form className="config-form" onSubmit={onSubmit}>
        <div className="config-section">
          <div className="config-section-head">
            <span>账户与策略</span>
            <span className="config-badge">日频</span>
          </div>

          <div className="config-grid-2">
            <label>
              账户标识
              <input
                value={form.account_id}
                onChange={(e) => onChangeForm({ ...form, account_id: e.target.value })}
                pattern="[a-zA-Z0-9_-]{1,32}"
                required
              />
            </label>

            <label>
              初始虚拟资金
              <input
                type="number"
                min={50_000}
                step={10_000}
                value={form.initial_cash}
                onChange={(e) => onChangeForm({ ...form, initial_cash: Number(e.target.value) })}
              />
            </label>
          </div>

          <div className="config-grid-3">
            <label>
              日频模拟策略
              <select
                value={form.strategy_id}
                onChange={(e) =>
                  onChangeForm({ ...form, strategy_id: e.target.value as PaperForm["strategy_id"] })
                }
              >
                {Object.entries(PAPER_STRATEGIES).map(([id, s]) => (
                  <option key={id} value={id}>{s.name}</option>
                ))}
              </select>
              <small className="field-hint">{PAPER_STRATEGIES[form.strategy_id].description}</small>
            </label>

            <label>
              选股范围
              <select
                value={form.universe_mode}
                onChange={(e) =>
                  onChangeForm({ ...form, universe_mode: e.target.value as PaperForm["universe_mode"] })
                }
              >
                <option value="full_market">全市场扫描</option>
                <option value="fixed">固定股票池</option>
              </select>
              <small className="field-hint">全市场模式逐日重建候选池</small>
            </label>

            <label>
              投资风险档
              <select
                value={form.risk_profile}
                onChange={(e) =>
                  onChangeForm({ ...form, risk_profile: e.target.value as PaperForm["risk_profile"] })
                }
              >
                <option value="balanced">均衡型</option>
                <option value="aggressive">进取型</option>
              </select>
              <small className="field-hint">
                {form.risk_profile === "aggressive" ? "95% / 75% / 35%" : "90% / 45% / 15%"}
                {" · "}单票 {form.risk_profile === "aggressive" ? "25%" : "22%"}
              </small>
            </label>
          </div>

          <label>
            候选标的（全市场模式作为固定池回退；至少 5 个）
            <textarea
              rows={3}
              value={form.symbols}
              onChange={(e) => onChangeForm({ ...form, symbols: e.target.value })}
              required
            />
          </label>

          <label>
            最低持仓比例{" "}
            <span className="config-inline-value">
              {ratio.format(form.minimum_invested_ratio)}
            </span>
            <input
              type="range"
              min={0}
              max={95}
              step={5}
              value={Math.round(form.minimum_invested_ratio * 100)}
              onChange={(e) =>
                onChangeForm({ ...form, minimum_invested_ratio: Number(e.target.value) / 100 })
              }
              className="config-range"
            />
            <small className="field-hint">
              无合格信号或无法成交时不强买，记录仓位缺口后续补足
            </small>
          </label>
        </div>

        <div className="config-section">
          <div className="config-section-head">
            <span>时间区间</span>
          </div>

          <div className="config-grid-2">
            <div className="config-date-group">
              <span className="config-phase-label">回测期 · 仅评估策略版本</span>
              <div className="config-grid-2">
                <label>
                  起点
                  <input
                    type="date"
                    value={form.backtest_start_date}
                    onChange={(e) => onChangeForm({ ...form, backtest_start_date: e.target.value })}
                  />
                </label>
                <label>
                  终点
                  <input
                    type="date"
                    value={form.backtest_end_date}
                    onChange={(e) => onChangeForm({ ...form, backtest_end_date: e.target.value })}
                  />
                </label>
              </div>
            </div>

            <div className="config-date-group">
              <span className="config-phase-label">模拟期 · 启用虚拟资金</span>
              <div className="config-grid-2">
                <label>
                  起点
                  <input
                    type="date"
                    value={form.simulation_start_date}
                    min={form.backtest_end_date}
                    onChange={(e) => onChangeForm({ ...form, simulation_start_date: e.target.value })}
                  />
                </label>
                <label>
                  截至
                  <input
                    type="date"
                    value={form.simulation_end_date}
                    min={form.simulation_start_date}
                    onChange={(e) => onChangeForm({ ...form, simulation_end_date: e.target.value })}
                  />
                </label>
              </div>
            </div>
          </div>
        </div>

        <div className="config-actions">
          <button className="primary-btn" type="submit" disabled={busy !== null}>
            {busy === "replay" ? "正在逐日推演…" : "初始化并逐日历史推演"}
          </button>
          <p className="config-warning">
            初始化会重置同名虚拟账户。修改策略、模式或时间区间后需重新推演。
          </p>
        </div>
      </form>

      {/* 策略版本实验室 */}
      <div className="card config-lab">
        <div className="card-heading">
          <div>
            <p className="section-label">CHAMPION / CHALLENGER</p>
            <h3>策略版本实验室</h3>
          </div>
        </div>
        {dashboard?.versions.length ? (
          <div className="version-list">
            {dashboard.versions.map((v) => (
              <div key={v.version} className="version-row">
                <div>
                  <strong>{v.version}</strong>
                  <span className={`version-badge ${v.status}`}>{v.status}</span>
                </div>
                <small>
                  样本外夏普{" "}
                  {v.metrics.oos_sharpe === undefined
                    ? "待评估"
                    : v.metrics.oos_sharpe.toFixed(2)}
                  {" · "}样本外回撤{" "}
                  {v.metrics.oos_max_drawdown === undefined
                    ? "待评估"
                    : ratio.format(v.metrics.oos_max_drawdown)}
                </small>
              </div>
            ))}
          </div>
        ) : (
          <p className="empty-card">初始化模拟账户后显示策略版本评估结果。</p>
        )}
        {dashboard?.upgrade_events[0] && (
          <div className="upgrade-note">
            <strong>
              {dashboard.upgrade_events[0].decision === "PROMOTED" ? "已晋级" : "未晋级"}
              ：{dashboard.upgrade_events[0].to_version}
            </strong>
            <p>{dashboard.upgrade_events[0].reason}</p>
          </div>
        )}
      </div>
    </div>
  );
}
