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
  assert.match(html, /中短期趋势组合驾驶舱/);
  assert.match(html, /不会发送真实订单/);
  // Tab 导航按钮必须出现在 SSR HTML 中
  assert.match(html, /驾驶舱/);
  assert.match(html, /决策日志/);
  assert.match(html, /配置/);
  // 驾驶舱 Tab（默认激活）的静态内容
  assert.match(html, /当前持仓/);
  assert.match(html, /下一交易日执行计划/);
  // config tab 与 journal tab 是客户端渲染，不出现在 SSR HTML
  assert.doesNotMatch(html, /策略 × 交易频率评分矩阵|第一阶段/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

test("keeps the dashboard connected to the local API", async () => {
  // API_BASE 常量已提取到 shared.tsx
  const shared = await readFile(
    new URL("../app/shared.tsx", import.meta.url),
    "utf8",
  );
  assert.match(shared, /127\.0\.0\.1:8000/);
  assert.match(shared, /NEXT_PUBLIC_API_BASE_URL/);

  // quant-dashboard 仍然引用 PaperTrading
  const dashboard = await readFile(
    new URL("../app/quant-dashboard.tsx", import.meta.url),
    "utf8",
  );
  assert.match(dashboard, /PaperTrading/);

  // paper-trading 仍调用 replay/advance API
  const paperTrading = await readFile(
    new URL("../app/paper-trading.tsx", import.meta.url),
    "utf8",
  );
  assert.match(paperTrading, /\/api\/paper\/replay/);
  assert.match(paperTrading, /\/api\/paper\/advance/);

  // actionLabel 已提取到 shared.tsx
  const shared2 = await readFile(
    new URL("../app/shared.tsx", import.meta.url),
    "utf8",
  );
  assert.match(shared2, /BUY.*SELL.*CLOSE/s);
});
