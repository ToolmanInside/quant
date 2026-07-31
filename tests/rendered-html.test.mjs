import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the Quant Lab workspace", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>Quant Lab｜本地策略工作台<\/title>/i);
  assert.match(html, /Quant Lab/);
  assert.match(html, /双均线趋势/);
  assert.match(html, /中短期趋势组合驾驶舱/);
  assert.match(html, /不会发送真实订单/);
  assert.match(html, /价格动量/);
  assert.match(html, /通道突破/);
  assert.match(html, /每日决策 · 回顾 · 分析/);
  assert.match(html, /初始化并逐日历史推演/);
  assert.match(html, /更新今日数据/);
  assert.doesNotMatch(html, /策略 × 交易频率评分矩阵|第一阶段/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

test("keeps the dashboard connected to the local API", async () => {
  const dashboard = await readFile(
    new URL("../app/quant-dashboard.tsx", import.meta.url),
    "utf8",
  );
  assert.match(dashboard, /127\.0\.0\.1:8000/);
  assert.match(dashboard, /NEXT_PUBLIC_API_BASE_URL/);
  assert.match(dashboard, /PaperTrading/);

  const paperTrading = await readFile(
    new URL("../app/paper-trading.tsx", import.meta.url),
    "utf8",
  );
  assert.match(paperTrading, /\/api\/paper\/replay/);
  assert.match(paperTrading, /\/api\/paper\/advance/);
  assert.match(paperTrading, /BUY.*SELL.*CLOSE/s);
});
