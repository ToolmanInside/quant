"use client";

import { useEffect, useState } from "react";
import { PaperTrading } from "./paper-trading";
import { SystemBar, API_BASE } from "./shared";

type SystemStatus = {
  tushare_configured: boolean;
};

export function QuantDashboard() {
  const [connected, setConnected] = useState(false);
  const [tushareOk, setTushareOk] = useState(false);

  useEffect(() => {
    fetch(`${API_BASE}/api/system/status`)
      .then((r) => r.json())
      .then((body: SystemStatus) => {
        setConnected(true);
        setTushareOk(body.tushare_configured);
      })
      .catch(() => {
        setConnected(false);
        setTushareOk(false);
      });
  }, []);

  return (
    <main className="app-shell">
      <SystemBar connected={connected} tushareOk={tushareOk} />
      <PaperTrading />
      <footer>
        <span>Quant Lab v0.1</span>
        <p>本地模拟环境 · 不连接券商 · 历史表现不代表未来收益</p>
      </footer>
    </main>
  );
}
