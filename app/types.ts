// 与 paper-trading.tsx 按 dashboard API 返回数据保持一致的类型定义。

export type PlanAction = "BUY" | "SELL" | "CLOSE";

export type PlanItem = {
  symbol: string;
  name: string;
  sector: string;
  action: PlanAction;
  target_weight: number;
  reason: string;
  signal_price: number;
  score: number;
  signal_date: string;
  strategy_version: string;
};

export type DailyJournal = {
  trade_date: string;
  strategy_id: string;
  strategy_name: string;
  review: {
    scheduled_count: number;
    executed_count: number;
    scheduled_actions: Array<{ symbol: string; action: string; reason: string }>;
    executions: Array<{ symbol: string; action: string; price: number; quantity: number; reason: string }>;
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

export type PaperDashboard = {
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
      universe_mode?: "fixed" | "full_market";
      risk_profile?: "balanced" | "aggressive";
      minimum_invested_ratio?: number;
      adx_window?: number;
      adx_min?: number;
      volume_confirm_ratio?: number;
      cross_valid_days?: number;
      death_cross_confirm_days?: number;
      death_cross_buffer?: number;
      reentry_cooldown_days?: number;
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
    top_sectors: Array<{ name: string; score: number; breadth: number; members: number }>;
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
    action: PlanAction;
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
    status: "champion" | "challenger" | "retired";
    metrics: {
      oos_sharpe?: number;
      oos_max_drawdown?: number;
      turnover?: number;
      annualized_turnover?: number;
      estimated_transaction_cost?: number;
      cost_to_initial_capital?: number;
    };
  }>;
  upgrade_events: Array<{
    id: number;
    trade_date: string;
    from_version: string;
    to_version: string;
    decision: "PROMOTED" | "REJECTED";
    reason: string;
  }>;
  equity_curve: Array<{ trade_date: string; equity: number; drawdown: number }>;
  data_errors: Array<{ symbol: string; message: string }>;
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
    last_exits: Array<{ id: number; symbol: string; reason: string }>;
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

export type PaperForm = {
  account_id: string;
  strategy_id: "moving_average" | "momentum" | "breakout";
  universe_mode: "fixed" | "full_market";
  risk_profile: "balanced" | "aggressive";
  minimum_invested_ratio: number;
  adx_window: number;
  adx_min: number;
  volume_confirm_ratio: number;
  cross_valid_days: number;
  death_cross_confirm_days: number;
  death_cross_buffer: number;
  reentry_cooldown_days: number;
  symbols: string;
  backtest_start_date: string;
  backtest_end_date: string;
  simulation_start_date: string;
  simulation_end_date: string;
  initial_cash: number;
};
