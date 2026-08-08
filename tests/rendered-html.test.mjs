import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import test from "node:test";

const PORT = 3947;
const BASE = `http://127.0.0.1:${PORT}`;

async function waitReady(timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(BASE, { headers: { accept: "text/html" } });
      if (response.ok) return response;
    } catch {
      // server not up yet
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`next start did not become ready on ${BASE}`);
}

test("server-renders the Quant Lab workspace", async (t) => {
  const server = spawn(
    process.platform === "win32" ? "npx.cmd" : "npx",
    ["next", "start", "-p", String(PORT)],
    { stdio: "ignore", shell: process.platform === "win32" },
  );
  t.after(async () => {
    server.kill("SIGTERM");
    await Promise.race([
      once(server, "exit"),
      new Promise((resolve) => setTimeout(resolve, 3000)),
    ]);
    if (server.exitCode === null) server.kill("SIGKILL");
  });

  const response = await waitReady();
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  // 页面骨架
  assert.match(html, /Quant Lab｜本地策略工作台/);
  assert.match(html, /中短期趋势组合驾驶舱/);
  assert.match(html, /不会发送真实订单/);
  // Tab 导航（SSR 输出）
  assert.match(html, /驾驶舱/);
  assert.match(html, /决策日志/);
  assert.match(html, /配置/);
  // 驾驶舱 Tab（默认激活）静态内容
  assert.match(html, /当前持仓/);
  assert.match(html, /下一交易日执行计划/);
  // 旧版遗留不应再出现
  assert.doesNotMatch(html, /策略 × 交易频率评分矩阵/);
});
