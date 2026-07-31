"use client";

import { useEffect, useState } from "react";
import { PaperTrading } from "./paper-trading";

type SystemStatus = {
  mode: string;
  tushare_configured: boolean;
  data_provider: string;
};

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

export function QuantDashboard() {
  const [status, setStatus] = useState<SystemStatus | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/api/system/status`)
      .then((response) => response.json())
      .then((body: SystemStatus) => setStatus(body))
      .catch(() => setStatus(null));
  }, []);

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">LOCAL PAPER TRADING DESK</p>
          <h1>Quant Lab</h1>
        </div>
        <div className="system-state" aria-label="系统状态">
          <span className={`status-dot ${status ? "" : "offline"}`} />
          <div>
            <strong>{status ? "模拟交易服务已连接" : "等待本地服务"}</strong>
            <small>
              {status
                ? `日频模拟 · ${
                    status.tushare_configured
                      ? "Tushare 已配置"
                      : "Tushare 未配置"
                  }`
                : "请重新启动 start-quant-lab.cmd"}
            </small>
          </div>
        </div>
      </header>

      <PaperTrading />

      <footer>
        <span>Quant Lab v0.1</span>
        <p>本地模拟环境 · 不连接券商 · 历史表现不代表未来收益</p>
      </footer>
    </main>
  );
}
