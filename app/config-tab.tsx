"use client";

import type { PaperForm, PaperDashboard } from "./types";
import { PAPER_STRATEGIES, ratio } from "./shared";

type Props = {
  form: PaperForm;
  dashboard: PaperDashboard | null;
  busy: "replay" | "advance" | null;
  onChangeForm: (next: PaperForm) => void;
  onSubmit: (event: React.FormEvent<HTMLFormElement>) => void;
  onAdvance: () => void;
};

export function ConfigTab({ form, dashboard, busy, onChangeForm, onSubmit, onAdvance }: Props) {
  return (
    <div className="config-tab">
      <form className="config-form" onSubmit={onSubmit}>
        <div className="config-form-title">
          <div>
            <span>模拟配置</span>
            <strong>账户、策略与时间范围</strong>
          </div>
          <span className="config-badge">日频</span>
        </div>

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
          候选标的（至少 5 个）
          <textarea
            rows={4}
            value={form.symbols}
            onChange={(e) => onChangeForm({ ...form, symbols: e.target.value })}
            required
          />
        </label>

        <label>
          日频模拟策略
          <select
            value={form.strategy_id}
            onChange={(e) =>
              onChangeForm({ ...form, strategy_id: e.target.value as PaperForm["strategy_id"] })
            }
          >
            {Object.entries(PAPER_STRATEGIES).map(([id, s]) => (
              <option key={id} value={id}>
                {s.name}
              </option>
            ))}
          </select>
          <small className="field-hint">
            {PAPER_STRATEGIES[form.strategy_id].description} 固定为每日收盘计算；
            修改后需重新初始化账户。
          </small>
        </label>

        <label>
          投资风险档
          <select
            value={form.risk_profile}
            onChange={(e) =>
              onChangeForm({ ...form, risk_profile: e.target.value as PaperForm["risk_profile"] })
            }
          >
            <option value="balanced">均衡型（90% / 45% / 15%）</option>
            <option value="aggressive">进取型（95% / 75% / 35%）</option>
          </select>
          <small className="field-hint">
            依次对应进攻、谨慎和防守市场；进取型仍保留单票25%和最多5只的上限。
          </small>
        </label>

        <label>
          最低持仓比例
          <input
            type="number"
            min={0}
            max={95}
            step={5}
            value={Math.round(form.minimum_invested_ratio * 100)}
            onChange={(e) =>
              onChangeForm({ ...form, minimum_invested_ratio: Number(e.target.value) / 100 })
            }
          />
          <small className="field-hint">
            当前设置为 {ratio.format(form.minimum_invested_ratio)}；无合格信号或无法成交时会记录仓位缺口，不会强买。
          </small>
        </label>

        <p className="stage-label">① 回测期 · 只评估策略</p>
        <div className="field-row">
          <label>
            回测起点
            <input
              type="date"
              value={form.backtest_start_date}
              onChange={(e) => onChangeForm({ ...form, backtest_start_date: e.target.value })}
            />
          </label>
          <label>
            回测终点
            <input
              type="date"
              value={form.backtest_end_date}
              onChange={(e) => onChangeForm({ ...form, backtest_end_date: e.target.value })}
            />
          </label>
        </div>

        <p className="stage-label">② 模拟期 · 启用虚拟资金</p>
        <div className="field-row">
          <label>
            模拟盘起点
            <input
              type="date"
              value={form.simulation_start_date}
              min={form.backtest_end_date}
              onChange={(e) =>
                onChangeForm({ ...form, simulation_start_date: e.target.value })
              }
            />
          </label>
          <label>
            模拟截至
            <input
              type="date"
              value={form.simulation_end_date}
              min={form.simulation_start_date}
              onChange={(e) =>
                onChangeForm({ ...form, simulation_end_date: e.target.value })
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
            onChange={(e) =>
              onChangeForm({ ...form, initial_cash: Number(e.target.value) })
            }
          />
        </label>

        <button className="primary-btn" type="submit" disabled={busy !== null}>
          {busy === "replay" ? "正在逐日推演…" : "初始化并逐日历史推演"}
        </button>

        <button
          className="secondary-btn"
          type="button"
          disabled={busy !== null || !dashboard}
          onClick={onAdvance}
        >
          {busy === "advance" ? "正在更新今日数据…" : "更新今日数据"}
        </button>

        <p className="config-warning">
          初始化会重置同名虚拟账户。回测终点必须早于模拟起点；模拟起点当天收盘生成首批信号，下一交易日才可能成交。
        </p>
      </form>

      {/* 自动升级实验室 */}
      <div className="card config-upgrade">
        <div className="card-heading">
          <div>
            <p className="section-label">CHAMPION / CHALLENGER</p>
            <h3>自动升级实验室</h3>
          </div>
          <span>仅模拟晋级</span>
        </div>
        <div className="version-list">
          {dashboard?.versions.map((v) => (
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
        {dashboard?.upgrade_events[0] && (
          <div className="upgrade-note">
            <strong>
              {dashboard.upgrade_events[0].decision === "PROMOTED" ? "已晋级" : "未晋级"}
              ：{dashboard.upgrade_events[0].to_version}
            </strong>
            <p>{dashboard.upgrade_events[0].reason}</p>
          </div>
        )}
        <p className="config-warning">
          挑战者必须在样本外夏普领先至少 0.15、回撤不明显恶化且交易样本充足；系统不会生成任意代码，也不会接入真实账户。
        </p>
      </div>
    </div>
  );
}
