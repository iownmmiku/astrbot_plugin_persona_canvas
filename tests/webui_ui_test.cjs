/* Offline browser regression tests. Requires Node.js + Playwright + Chrome.
 * Run: NODE_PATH=<playwright module directory> node tests/webui_ui_test.cjs
 * All AstrBot bridge calls are fixtures; no model, QQ or drawing API is used.
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require("playwright");

const pageRoot = path.resolve(__dirname, "../pages/canvas");
const requestSummary = {
  prompt: "adult character, white dress, soft light, <img src=x onerror=alert(1)>",
  negative_prompt: "abs, muscular female, extra toes", negative_mode: "field",
  negative_prompt_field: "parameters.negative_prompt", model: "offline-image",
  options: { width: 832, height: 1216, seed: 42 }, reference_count: 2,
  reference_source: "persona", provider_timeout_sec: 300,
  task_timeout_sec: 60, effective_timeout_sec: 60,
  api_key: "must-not-display", raw_reference: "must-not-display-reference",
};
const capabilities = { text_to_image: true, image_to_image: true, sampler: false, seed: false, negative_mode: "disabled", negative_prompt_mode: "不发送负面词", max_reference_images: 1 };
const fixtures = {
  state: { settings: { current_persona: "default", default_provider: "default", generation: { width: 832, height: 1216, timeout_sec: 60 }, integration: { mode: "native_tools" } }, persona: { name: "离线测试角色", state: {} } },
  personas: { items: [{ id: "default", name: "离线测试角色", state: {}, reference_assets: [] }] },
  providers: { items: [
    { name: "default", kind: "openai", endpoint: "https://offline.invalid/v1", model: "offline-image", timeout: 300, capabilities },
    { name: "custom-nested", kind: "custom", endpoint: "https://offline.invalid/generate", model: "custom-model", timeout: 120, negative_prompt: true, negative_prompt_field: "parameters.negative", prompt_field: "input.positive", model_field: "input.model", reference_field: "input.image", reference_format: "base64", reference_mime_field: "input.mime", models_response_path: "models.items", supported_sizes: ["832x1216"], option_fields: { width: "parameters.width", height: "parameters.height", seed: "parameters.seed" }, supports_seed: true, capabilities: { ...capabilities, seed: true, negative_prompt: true, negative_mode: "field", negative_prompt_mode: "自定义独立字段" } },
    { name: "gemini", kind: "gemini", endpoint: "https://offline.invalid", model: "offline-gemini", timeout: 90, capabilities },
    { name: "novelai", kind: "novelai", endpoint: "https://offline.invalid", model: "offline-novelai", timeout: 90, capabilities: { ...capabilities, seed: true, sampler: true, negative_prompt: true, negative_mode: "field", negative_prompt_mode: "原生负面通道" } },
  ] },
  targets: { items: [] }, sessions: { items: [] },
  jobs: { items: [{ id: "job-failed", status: "failed", caption: "离线失败测试", provider: "default", created_at: 1728000000, error: "绘图接口超时", failure_stage: "provider", request_summary: requestSummary, trace: [{ stage: "provider", status: "failed", detail: "60 秒限制已生效" }] }, { id: "job-prepared", status: "failed", caption: "离线参考图错误", provider: "default", created_at: 1727000000, error: "参考图不可用", request_summary: { ...requestSummary, negative_mode: "natural_language", negative_prompt: "", reference_source: "message_attachment", notes: "请求尚未提交，显示编排输入；接口实际参数以提交后摘要为准" }, trace: [{ stage: "reference", status: "failed", detail: "参考图不可用" }] }] },
  history: { items: [{ job_id: "job-failed", ok: false, caption: "离线失败测试", provider: "default", model: "offline-image", error: "绘图接口超时", request_summary: requestSummary, state_patch: { outfit: "white dress" }, trace: [{ stage: "provider", status: "failed", detail: "60 秒限制已生效" }] }] },
  diagnostics: { setup: [], budget: {}, items: [{ status: "failed", summary: "离线失败测试", request_summary: requestSummary, trace: [{ stage: "provider", status: "failed", detail: "60 秒限制已生效" }] }] },
  "llm/providers": { items: [], personas: [] },
};

async function main() {
  const server = http.createServer(async (req, res) => {
    const filename = new URL(req.url, "http://localhost").pathname;
    const allowed = { "/": ["index.html", "text/html"], "/app.js": ["app.js", "text/javascript"], "/app.css": ["app.css", "text/css"] };
    if (!allowed[filename]) { res.writeHead(404); res.end(); return; }
    try { const [name, contentType] = allowed[filename]; res.writeHead(200, { "Content-Type": `${contentType};charset=utf-8` }); res.end(await fs.readFile(path.join(pageRoot, name))); }
    catch (error) { res.writeHead(500); res.end(error.message); }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({ headless: true, channel: "chrome" });
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
    const browserErrors = [];
    page.on("pageerror", error => browserErrors.push(error.message));
    await page.addInitScript(data => {
      window.__canvasFixtures = data; window.__canvasCalls = [];
      window.AstrBotPluginView = {
        ready: async () => ({ isDark: false }),
        apiGet: async route => structuredClone(data[route.replace(/^page\//, "")] || { items: [] }),
        apiPost: async (route, body) => {
          route = route.replace(/^page\//, ""); window.__canvasCalls.push({ route, body: structuredClone(body) });
          if (route === "providers") {
            const current = data.providers.items.find(item => item.name === body.name);
            const saved = { ...current, ...body, capabilities: current?.capabilities || { text_to_image: true } };
            if (current) Object.assign(current, saved); else data.providers.items.push(saved);
            return structuredClone(saved);
          }
          if (route === "image/test") return { ok: true, message: "离线测试，无真实接口调用", request_summary: data.jobs.items[0].request_summary, effective_timeout_sec: 60 };
          if (route === "generate") return { job_id: "offline-generated-job" };
          return {};
        },
      };
    }, fixtures);
    await page.goto(`http://127.0.0.1:${server.address().port}/#providers`);
    await page.waitForFunction(() => document.getElementById("status-label").textContent === "AstrBot 已连接");
    const get = id => page.locator(`#${id}`);
    const lastPost = async route => page.evaluate(route => window.__canvasCalls.filter(call => call.route === route).at(-1)?.body, route);
    const saveProvider = async () => {
      await get("provider-form").getByRole("button", { name: "保存接口", exact: true }).click();
      await page.waitForFunction(() => !document.getElementById("dirty-label").textContent);
    };

    assert.equal(await get("v-negative-mode").inputValue(), "disabled", "OpenAI 缺省不得伪装成支持负面字段");
    assert.match(await get("v-timeout-summary").innerText(), /有效生成超时为 60 秒/);
    await get("v-negative-mode").selectOption("field");
    assert.equal(await get("v-negative-field").isVisible(), true);
    await get("v-negative-field").fill("parameters.negative_prompt");
    await saveProvider();
    let saved = await lastPost("providers");
    assert.equal(saved.negative_mode, "field"); assert.equal(saved.negative_prompt_field, "parameters.negative_prompt");
    assert.equal(saved.negative_prompt, true); assert.equal(Object.hasOwn(saved, "option_fields"), false, "非 custom 保存不得覆盖旧自定义映射");

    await get("provider-list").getByRole("button", { name: /custom-nested/ }).click();
    await get("provider-form").getByText("高级接口配置", { exact: true }).click();
    assert.equal(await get("v-negative-mode").inputValue(), "field", "旧 custom 独立负面字段保持兼容");
    assert.equal(await get("v-prompt-field").inputValue(), "input.positive");
    assert.equal(await get("v-reference-format").inputValue(), "base64");
    await saveProvider();
    saved = await lastPost("providers");
    for (const key of ["prompt_field", "model_field", "reference_field", "reference_mime_field", "reference_format", "models_response_path", "supported_sizes", "option_fields"]) assert.deepEqual(saved[key], fixtures.providers.items[1][key], `高级字段 ${key} 应完整往返保存`);
    await get("v-options").fill(""); await get("v-model").fill("changed-model"); await saveProvider();
    saved = await lastPost("providers"); assert.equal(Object.hasOwn(saved, "option_fields"), false, "空映射输入不应改写已有字段");
    assert.equal(JSON.parse(await get("v-options").inputValue()).width, "parameters.width");
    await get("v-supported-sizes").fill(""); await saveProvider();
    assert.equal((await lastPost("providers")).supported_sizes, null, "清空原自定义尺寸列表恢复模型默认");
    await get("v-kind").selectOption("gemini");
    assert.equal(await get("v-negative-mode").inputValue(), "disabled", "改为 Gemini 不应携带无效独立字段模式");
    await get("restore-provider").click();

    await get("provider-list").getByRole("button", { name: /^gemini/ }).click();
    assert.equal(await get("v-negative-mode").locator("option[value='field']").count(), 0, "Gemini 不应暴露独立负面字段");
    assert.equal(await get("v-prompt-field").isVisible(), false);
    await get("provider-list").getByRole("button", { name: /^novelai/ }).click();
    assert.equal(await get("v-negative-mode").inputValue(), "field"); assert.equal(await get("v-negative-mode").isDisabled(), true);
    assert.equal(await get("v-negative-field-row").isVisible(), false, "NovelAI 原生字段不允许误改成中转路径");

    await page.locator("[data-page='studio']").click();
    await get("g-provider").selectOption("custom-nested"); await get("g-override").check();
    assert.equal(await get("g-width").isDisabled(), false); assert.equal(await get("g-seed").isDisabled(), false);
    await get("g-provider").selectOption("gemini"); await get("g-override").check();
    assert.equal(await get("g-width").isDisabled(), true); assert.equal(await get("g-width").isVisible(), false);
    assert.equal(await get("g-aspect-ratio").isVisible(), true);
    await get("g-aspect-ratio").selectOption("9:16"); await get("g-image-size").selectOption("2K"); await get("g-prompt").fill("离线图像参数测试");
    await get("generate-form").getByRole("button", { name: "提交生成任务", exact: true }).click();
    await page.waitForFunction(() => window.__canvasCalls.some(call => call.route === "generate"));
    const generated = await lastPost("generate"); assert.equal(generated.aspect_ratio, "9:16"); assert.equal(generated.image_size, "2K");
    for (const key of ["width", "height", "steps", "scale", "seed"]) assert.equal(Object.hasOwn(generated, key), false, "Gemini 不应提交禁用参数");

    const jobDetail = get("job-list").locator(".request-detail").first(); await jobDetail.locator("summary").click();
    assert.match(await jobDetail.innerText(), /adult character/); assert.match(await jobDetail.innerText(), /abs, muscular female, extra toes/);
    assert.match(await jobDetail.innerText(), /角色参考图库/); assert.match(await jobDetail.innerText(), /60 秒（两者取较短）/);
    assert.equal(await jobDetail.locator("img").count(), 0, "提示词必须作为文本显示，避免 HTML 执行");
    assert.doesNotMatch(await page.locator("body").innerText(), /must-not-display/);
    const prepared = get("job-list").locator(".request-detail").nth(1); await prepared.locator("summary").click();
    assert.match(await prepared.innerText(), /查看请求准备摘要/); assert.match(await prepared.innerText(), /请求尚未提交/);
    assert.match(await prepared.innerText(), /聊天附件/); assert.doesNotMatch(await prepared.innerText(), /实际生成参数|最终正面提示词/);
    if (process.env.CANVAS_UI_SCREENSHOT_DIR) { await fs.mkdir(process.env.CANVAS_UI_SCREENSHOT_DIR, { recursive: true }); await page.evaluate(() => window.scrollTo(0, 0)); await page.screenshot({ path: path.join(process.env.CANVAS_UI_SCREENSHOT_DIR, "webui-request-diagnostic.png"), fullPage: true }); }

    await page.locator("[data-page='history']").click();
    const historyDetail = get("history-list").locator(".request-detail"); await historyDetail.locator("summary").click();
    assert.match(await historyDetail.innerText(), /最终正面提示词/); assert.match(await historyDetail.innerText(), /parameters.negative_prompt/);
    assert.equal(await get("diagnostic-list").locator(".request-detail").count(), 1, "失败动作诊断保留最终请求摘要");

    await page.locator("[data-page='providers']").click();
    await get("provider-list").getByRole("button", { name: /^default/ }).click();
    await get("test-image-generation").click();
    assert.equal(await get("provider-test-result").locator(".request-detail").count(), 1, "测试图应展示同一格式的最终摘要");
    await page.locator("[data-page='settings']").click();
    assert.equal(await get("d-timeout").getAttribute("max"), "600");
    assert.match(await get("settings-form").innerText(), /persona_canvas_control/);

    await page.locator("[data-page='providers']").click();
    await get("provider-list").getByRole("button", { name: /custom-nested/ }).click();
    if (process.env.CANVAS_UI_SCREENSHOT_DIR) {
      await fs.mkdir(process.env.CANVAS_UI_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.CANVAS_UI_SCREENSHOT_DIR, "webui-providers-desktop.png"), fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.screenshot({ path: path.join(process.env.CANVAS_UI_SCREENSHOT_DIR, "webui-providers-mobile.png"), fullPage: true });
    } else await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "移动页面不应横向溢出");
    assert.deepEqual(browserErrors, [], "页面运行不应有未捕获的 JavaScript 异常");
    console.log("PASS: WebUI offline Chrome checks (negative modes, custom fields, Gemini options, job/history/test diagnostics, escaping, mobile layout).");
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
