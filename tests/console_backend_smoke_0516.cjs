"use strict";
// Requests use real Python routes and SQLite. All external services are doubles.
const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const [base, token] = process.argv.slice(2);
async function main() {
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
    const requests = [], errors = [];
    page.on("request", request => requests.push(request.url()));
    page.on("pageerror", error => errors.push(error.message));
    const call = async (path, body) => {
      const response = await page.request.post(`${base}/api/page/${path}`, { headers: { Authorization: `Bearer ${token}` }, data: body });
      const result = await response.json(); assert.equal(response.ok(), true, JSON.stringify(result)); return result;
    };
    const refreshJobs = async () => { await page.locator("#refresh-jobs").click(); await page.waitForFunction(() => !document.getElementById("refresh-jobs").disabled); };
    const connect = async () => {
      await page.goto("about:blank");
      await page.goto(base + "/#providers");
      await page.locator("#access-token").fill(token);
      await page.locator("#token-form button").click();
      await page.waitForFunction(() => document.getElementById("status-label").textContent.includes("已连接"));
    };
    await connect();
    await page.locator("#provider-form summary").filter({ hasText: "高级接口配置" }).click();
    await page.locator("#v-extra").fill(JSON.stringify({ request: { token: "fixture-cross-layer-secret", quality: "high" } }));
    const save = async () => { await page.locator("#provider-form").getByRole("button", { name: "保存接口", exact: true }).click(); await page.waitForFunction(() => !document.querySelector("#provider-form button[type='submit']").disabled && !document.getElementById("dirty-label").textContent); };
    await save();
    const redacted = JSON.parse(await page.locator("#v-extra").inputValue());
    assert.equal(redacted.request.has_token, true); assert.equal(redacted.request.token, undefined);
    await page.locator("#v-timeout").fill("60"); await save();
    assert.equal((await call("qa/control", { action: "snapshot" })).secret_preserved, true);
    await call("qa/control", { action: "restart" });
    await page.waitForTimeout(500);
    for (let n = 0; n < 30; n++) {
      try { if ((await call("qa/control", { action: "snapshot" })).secret_preserved) break; } catch {}
      await page.waitForTimeout(100);
      if (n === 29) throw new Error("backend restart timeout");
    }
    await connect();
    assert.equal((await call("qa/control", { action: "snapshot" })).secret_preserved, true);
    await page.locator("[data-page='active']").click();
    const before = await call("qa/control", { action: "snapshot" });
    await page.locator("#t-gap").fill("7777"); await page.locator("#refresh-target-diagnostics").click();
    assert.match(await page.locator("#target-diagnostics").innerText(), /当前不触发/);
    assert.equal(await page.locator("#t-gap").inputValue(), "7777");
    assert.deepEqual(await call("qa/control", { action: "snapshot" }), before);
    await page.locator("#restore-target").click();
    await page.locator("[data-page='history']").click();
    await page.locator("#load-more-history").click();
    await page.waitForFunction(() => document.querySelectorAll("#history-list .history-item").length === 25);
    assert.equal(await page.locator("#history-list .history-item").count(), 25);
    await page.locator("[data-page='studio']").click();
    await page.waitForLoadState("networkidle");
    const previews = requests.filter(url => url.includes("reference/preview/")).length;
    const detail = page.locator("#job-list details").first(); await detail.evaluate(el => { el.open = true; el.dataset.retained = "yes"; });
    await refreshJobs(); await refreshJobs();
    assert.equal(await detail.evaluate(el => el.open && el.dataset.retained === "yes"), true);
    assert.equal(requests.filter(url => url.includes("reference/preview/")).length, previews);
    const changed = await call("qa/control", { action: "replace_conditions" });
    assert.equal(changed.ok, true); assert.equal(changed.status, "cancelled"); assert.equal(changed.pending, "coat");
    await refreshJobs();
    assert.match(await page.locator("#job-list").innerText(), /已取消/);
    assert.equal((await call("qa/control", { action: "snapshot" })).sent, 0);
    // Clear pending agreement through the real tool path before the next fixture.
    const saved = await call("qa/control", { action: "seed_uncertain" });
    await refreshJobs();
    const row = page.locator("#job-list .task-row").filter({ hasText: saved.job_id });
    await row.getByRole("button", { name: "核对后补发原图", exact: true }).click();
    assert.equal(await page.locator("#confirm-ok").isDisabled(), true);
    await page.locator("#confirm-uncertain").check(); await page.locator("#confirm-ok").click();
    await page.waitForFunction(() => document.getElementById("notice").textContent.includes("补发"));
    const final = await call("qa/control", { action: "snapshot" });
    assert.equal(final.sent, 1); assert.equal(final.generations, 1, "resend reuses original image");
    const embedded = await browser.newPage();
    await embedded.addInitScript(({ token }) => {
      const request = async (route, method, body) => {
        const response = await fetch(`/api/${route}`, { method, headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }, ...(body ? { body: JSON.stringify(body) } : {}) });
        const result = await response.json(); if (!response.ok) throw new Error(result.message || "backend request failed"); return result;
      };
      window.AstrBotPluginView = { ready: async () => ({}), apiGet: route => request(route, "GET"), apiPost: (route, body) => request(route, "POST", body) };
    }, { token });
    await embedded.goto(base + "/#active");
    await embedded.waitForFunction(() => document.getElementById("status-label").textContent === "AstrBot 已连接");
    await embedded.locator("#r-max-wait").fill("45");
    await embedded.locator("#reply-images-form").getByRole("button", { name: "保存配图设置", exact: true }).click();
    await embedded.waitForFunction(() => !document.querySelector("#reply-images-form button[type='submit']").disabled);
    assert.equal((await call("qa/control", { action: "snapshot" })).max_wait_sec, 45);
    await embedded.close();
    assert.deepEqual(errors, []);
    console.log("PASS: Chrome -> real Python backend -> temporary SQLite (redacted save/restart, read-only diagnosis, cursor history, cached thumbnails, retained details, replaced agreement cancellation, original-image resend, bridge-shaped real route calls).");
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
