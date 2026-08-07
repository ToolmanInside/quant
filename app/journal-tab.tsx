"use client";

import type { DailyJournal, PlanItem, PaperDashboard } from "./types";
import { SectionLabel, ratio, actionLabel } from "./shared";

type Props = {
  dashboard: PaperDashboard | null;
  journals: DailyJournal[];
  selectedJournal: DailyJournal | undefined;
  onSelectDate: (date: string) => void;
};

type ReviewItem = PaperDashboard["reviews"][number];

// 按 (category, diagnosis) 合并连续相同条目为一条，显示日期范围
function groupReviews(reviews: ReviewItem[]): Array<{
  category: string;
  severity: string;
  diagnosis: string;
  evidence: string;
  recommendation: string;
  firstDate: string;
  lastDate: string;
  count: number;
}> {
  if (!reviews.length) return [];
  const groups: Array<{
    category: string;
    severity: string;
    diagnosis: string;
    evidence: string;
    recommendation: string;
    firstDate: string;
    lastDate: string;
    count: number;
  }> = [];

  for (const r of reviews) {
    const last = groups[groups.length - 1];
    if (
      last &&
      last.category === r.category &&
      last.diagnosis === r.diagnosis
    ) {
      // 扩展到更远日期（reviews 按日期倒序）
      if (r.trade_date < last.lastDate) last.lastDate = r.trade_date;
      if (r.trade_date > last.firstDate) last.firstDate = r.trade_date;
      last.count += 1;
    } else {
      groups.push({
        category: r.category,
        severity: r.severity,
        diagnosis: r.diagnosis,
        evidence: typeof r.evidence === "string" ? r.evidence : JSON.stringify(r.evidence),
        recommendation: r.recommendation,
        firstDate: r.trade_date,
        lastDate: r.trade_date,
        count: 1,
      });
    }
  }
  return groups;
}

export function JournalTab({ dashboard, journals, selectedJournal, onSelectDate }: Props) {
  const groups = groupReviews(dashboard?.reviews ?? []);
  const dataErrors = dashboard?.data_errors ?? [];

  return (
    <div className="journal-tab">
      {/* 每日决策日志 */}
      <div className="card">
        <div className="card-heading">
          <div>
            <SectionLabel>DAILY DECISION LOOP</SectionLabel>
            <h3>每日决策 · 回顾 · 分析</h3>
          </div>
          <div className="journal-date-control">
            <span>已记录 {dashboard?.journal_count ?? 0} 个交易日</span>
            <select
              value={selectedJournal?.trade_date ?? ""}
              onChange={(e) => onSelectDate(e.target.value)}
              disabled={!journals.length}
              aria-label="选择决策日志日期"
              className="journal-date-select"
            >
              {!journals.length && <option value="">暂无日志</option>}
              {journals.map((j) => (
                <option key={j.trade_date} value={j.trade_date}>
                  {j.trade_date}
                </option>
              ))}
            </select>
          </div>
        </div>

        {selectedJournal ? (
          <JournalDetail journal={selectedJournal} />
        ) : (
          <p className="empty-card">
            重新初始化模拟账户后，每个模拟交易日都会在这里生成完整日志。
          </p>
        )}
      </div>

      {/* 数据异常明细 */}
      {dataErrors.length > 0 && (
        <div className="card">
          <div className="card-heading">
            <div>
              <SectionLabel>DATA ERRORS</SectionLabel>
              <h3>数据缺失明细</h3>
            </div>
            <span>{dataErrors.length} 个标的异常</span>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>标的</th>
                  <th>错误信息</th>
                </tr>
              </thead>
              <tbody>
                {dataErrors.map((e) => (
                  <tr key={e.symbol}>
                    <td><strong>{e.symbol}</strong></td>
                    <td className="reason-cell">{e.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 异常反思（合并后） */}
      <div className="card">
        <div className="card-heading">
          <div>
            <SectionLabel>DIAGNOSIS</SectionLabel>
            <h3>异常反思</h3>
          </div>
          <span>
            {dashboard?.reviews.length ?? 0} 条记录
            {groups.length !== dashboard?.reviews.length && `（合并为 ${groups.length} 类）`}
          </span>
        </div>
        {groups.length > 0 ? (
          groups.map((g, idx) => (
            <div key={idx} className="review-item">
              <div className="review-item-header">
                <span className={`review-badge ${g.severity.toLowerCase()}`}>
                  {g.category}
                </span>
                <small className="review-date">
                  {g.count === 1 ? g.firstDate : `${g.lastDate} → ${g.firstDate}（${g.count} 天）`}
                </small>
              </div>
              <strong>{g.diagnosis}</strong>
              <p>{g.evidence}</p>
              <small>下一步：{g.recommendation}</small>
            </div>
          ))
        ) : (
          <p className="empty-card">暂无达到阈值的异常；这不等于策略没有风险。</p>
        )}
      </div>
    </div>
  );
}

// ---------- JournalDetail ----------

function JournalDetail({ journal }: { journal: DailyJournal }) {
  return (
    <div className="daily-cycle-grid">
      <ReviewSection journal={journal} />
      <AnalysisSection journal={journal} />
      <DecisionSection journal={journal} />
      <ReflectionSection journal={journal} />
    </div>
  );
}

function ReviewSection({ journal }: { journal: DailyJournal }) {
  const { review } = journal;
  const hasExecutions = review.executions.length > 0;
  return (
    <section>
      <span className="cycle-index">01 · 回顾</span>

      <strong>到期计划 {review.scheduled_count} 条，成交 {review.executed_count} 条</strong>

      {/* 到期指令清单 */}
      {review.scheduled_actions.length > 0 && (
        <div className="cycle-sub-list">
          {review.scheduled_actions.map((a, idx) => (
            <div key={idx}>
              <small>{a.action} {a.symbol}</small>
              <small>{a.reason}</small>
            </div>
          ))}
        </div>
      )}

      {/* 成交记录 */}
      {hasExecutions && (
        <p>
          {review.executions
            .map((e) => `${actionLabel(e.action as PlanItem["action"])} ${e.symbol}`)
            .join("；")}
        </p>
      )}
      {!hasExecutions && (
        <p>当日没有到期成交，仍完成账户状态检查。</p>
      )}

      {/* 未成交 */}
      {review.unfilled_symbols.length > 0 && (
        <p>
          未成交：{review.unfilled_symbols.join(", ")}
        </p>
      )}

      <small>
        当日收益 {ratio.format(review.daily_return)} · 回撤 {ratio.format(review.drawdown)}
      </small>
    </section>
  );
}

function AnalysisSection({ journal }: { journal: DailyJournal }) {
  const { analysis } = journal;
  return (
    <section>
      <span className="cycle-index">02 · 分析</span>

      <strong>
        {analysis.market_regime} · 趋势宽度 {ratio.format(analysis.breadth)}
      </strong>

      <p>
        数据完整度 {ratio.format(analysis.data_quality)} · 持仓 {analysis.position_count} 只 · 权益 {analysis.equity.toLocaleString("zh-CN")} · 现金 {analysis.cash ? analysis.cash.toLocaleString("zh-CN") : "—"}
      </p>

      {/* 优选板块 */}
      {analysis.top_sectors.length > 0 && (
        <small>优选板块：{analysis.top_sectors.map((s) => s.name).join("、")}</small>
      )}

      {/* 当日入选标的 */}
      {analysis.selected_symbols.length > 0 && (
        <small>入选标的：{analysis.selected_symbols.join("、")}</small>
      )}
    </section>
  );
}

function DecisionSection({ journal }: { journal: DailyJournal }) {
  const { decision } = journal;
  return (
    <section>
      <span className="cycle-index">03 · 决策</span>

      <strong>
        {decision.action_count ? `${decision.action_count} 条下一日指令` : "明确无操作"}
      </strong>

      <p>{decision.summary}</p>

      {/* 每条指令明细 */}
      {decision.actions.length > 0 ? (
        <div className="cycle-sub-list">
          {decision.actions.map((item) => (
            <div key={`${item.action}-${item.symbol}`}>
              <small>
                <span className={`action-pill ${item.action.toLowerCase()}`}>
                  {actionLabel(item.action)}
                </span>
                {" "}{item.symbol}
              </small>
              <small>{item.reason}</small>
            </div>
          ))}
        </div>
      ) : (
        <small>无待执行指令</small>
      )}

      <small>{decision.execution_timing}</small>
    </section>
  );
}

function ReflectionSection({ journal }: { journal: DailyJournal }) {
  const { reflection } = journal;
  return (
    <section>
      <span className="cycle-index">04 · 复盘</span>

      <strong>{reflection.category} · {reflection.conclusion}</strong>

      {reflection.evidence.length > 0 && (
        <p>{reflection.evidence.join("；")}</p>
      )}

      <small>下一日关注：{reflection.next_focus}</small>
    </section>
  );
}
