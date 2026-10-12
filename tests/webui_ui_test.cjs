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
  submitted: true,
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
    { name: "local-comfy", kind: "comfyui", endpoint: "http://127.0.0.1:8188", model: "角色本地工作流", timeout: 180, workflow: { "6": { class_type: "CLIPTextEncode", inputs: { text: "default" } } }, workflow_fields: { prompt: "6.inputs.text" }, capabilities: { ...capabilities, image_to_image: false, negative_mode: "disabled" } },
  ] },
  targets: { items: [{ umo: "offline:FriendMessage:friend", name: "私聊测试", scope: "private", enabled: false, morning_enabled: false, reply_image_enabled: false }, { umo: "offline:GroupMessage:group", name: "群聊测试", scope: "group", enabled: false, morning_enabled: false, reply_image_enabled: false }] }, sessions: { items: [] },
  jobs: { items: [{ id: "job-failed", status: "failed", caption: "离线失败测试", provider: "default", created_at: 1728000000, error: "绘图接口超时", failure_stage: "provider", request_summary: requestSummary, trace: [{ stage: "provider", status: "failed", detail: "60 秒限制已生效" }] }, { id: "job-prepared", status: "failed", caption: "离线参考图错误", provider: "default", created_at: 1727000000, error: "参考图不可用", request_summary: { ...requestSummary, submitted: false, negative_mode: "natural_language", negative_prompt: "", reference_source: "message_attachment", notes: "请求尚未提交，显示编排输入；接口实际参数以提交后摘要为准" }, trace: [{ stage: "reference", status: "failed", detail: "参考图不可用" }] },
    { id: "job-delivery-failed", status: "delivery_failed", caption: "已生成平台拒收", asset: "original.png", umo: "offline:FriendMessage:test", created_at: 1726000000 },
    { id: "job-uncertain", status: "uncertain", caption: "已生成发送待确认", asset: "original-uncertain.png", umo: "offline:FriendMessage:test", created_at: 1725000000 },
    { id: "job-sending", status: "sending", caption: "原图正在发送", asset: "sending.png", umo: "offline:FriendMessage:test", created_at: 1724000000 },
  ] },
  history: { items: [{ job_id: "job-failed", ok: false, caption: "离线失败测试", provider: "default", model: "offline-image", error: "绘图接口超时", request_summary: requestSummary, state_patch: { outfit: "white dress" }, trace: [{ stage: "provider", status: "failed", detail: "60 秒限制已生效" }] },
    { job_id: "job-delivery-failed", ok: true, caption: "已生成平台拒收" }, { job_id: "job-uncertain", ok: true, caption: "已生成发送待确认" },
  ] },
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
            if (saved.kind === "comfyui") saved.capabilities = { ...saved.capabilities, seed: Boolean(saved.workflow_fields?.seed), sampler: ["steps", "scale", "sampler"].some(key => saved.workflow_fields?.[key]), image_to_image: Boolean(saved.workflow_fields?.reference), negative_mode: saved.workflow_fields?.negative_prompt ? "field" : "disabled" };
            if (body.reset_option_fields === true) delete saved.option_fields;
            delete saved.reset_option_fields;
            if (current) { if (body.reset_option_fields === true) delete current.option_fields; Object.assign(current, saved); } else data.providers.items.push(saved);
            return structuredClone(saved);
          }
          if (route === "image/test") {
            if (window.__testTransportFailure) throw new Error("离线桥接传输失败");
            return window.__testResponse ? structuredClone(window.__testResponse) : { ok: true, message: "离线测试，无真实接口调用", request_summary: data.jobs.items[0].request_summary, effective_timeout_sec: 60 };
          }
          if (route === "jobs/resend") {
            const job = data.jobs.items.find(item => item.id === body.id);
            job.status = "sending";
            await new Promise(resolve => { window.__releaseResend = resolve; });
            job.status = window.__resendFailure ? "delivery_failed" : "sent";
            return window.__resendFailure ? { ok: false, error: "离线平台明确拒收" } : { ok: true, message: "原图已补发", job_id: job.id };
          }
          if (route === "settings") { for (const [key, patch] of Object.entries(body)) data.state.settings[key] = { ...data.state.settings[key], ...patch }; return { ok: true }; }
          if (route === "targets") { const current = data.targets.items.find(item => item.umo === body.umo); if (body.action === "save") Object.assign(current, body.patch); return structuredClone(current || {}); }
          if (route === "image/models") return { models: [data.providers.items.find(item => item.name === body.name)?.model], note: "显示当前工作流名称，不请求云端模型列表" };
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
      try { await page.waitForFunction(() => !document.getElementById("dirty-label").textContent); }
      catch (error) { console.error(await page.evaluate(() => ({ notice: document.getElementById("notice").textContent, dirty: document.getElementById("dirty-label").textContent, invalid: Array.from(document.querySelectorAll("#provider-form :invalid")).map(el => ({ id: el.id, value: el.value, message: el.validationMessage })), call: window.__canvasCalls.at(-1) }))); throw error; }
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
    saved = await lastPost("providers"); assert.equal(Object.hasOwn(saved, "option_fields"), false); assert.equal(saved.reset_option_fields, true, "清空映射应明确恢复默认映射");
    assert.equal(await get("v-options").inputValue(), "", "恢复默认映射后不能回填旧字段");
    await get("v-options").fill("{}"); await saveProvider();
    assert.deepEqual((await lastPost("providers")).option_fields, {}, "显式空对象应保留禁用参数映射的含义");
    assert.equal(await get("v-options").inputValue(), "{}");
    await get("reset-provider-options").click(); await saveProvider();
    assert.equal((await lastPost("providers")).reset_option_fields, true); assert.equal(await get("v-options").inputValue(), "");
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
    await page.evaluate(() => { window.__testResponse = { status: "ok", data: { ok: false, error: "离线接口超时", request_summary: { ...window.__canvasFixtures.jobs.items[0].request_summary, notes: ["同时发送 v4_negative_prompt.caption.base_caption"] } } }; });
    await get("test-image-generation").click();
    await page.waitForFunction(() => !document.getElementById("test-image-generation").disabled);
    assert.match(await get("provider-test-result").innerText(), /离线接口超时/);
    assert.doesNotMatch(await get("provider-test-result").innerText(), /正在生成测试图/);
    let testDetail = get("provider-test-result").locator(".request-detail"); await testDetail.locator("summary").click();
    assert.match(await testDetail.innerText(), /查看最终请求摘要/); assert.doesNotMatch(await testDetail.innerText(), /查看请求准备摘要/);
    assert.match(await testDetail.innerText(), /v4_negative_prompt/); assert.match(await testDetail.innerText(), /最终正面提示词/);
    await page.evaluate(() => { window.__testResponse = { ok: false, error: "离线手动负面字段错误", request_summary: { ...window.__canvasFixtures.jobs.items[0].request_summary, negative_mode: "disabled", negative_source: "extra_body", negative_prompt: "manual negative", negative_prompt_field: "parameters.negative", notes: ["手动附加请求体"] } }; });
    await get("test-image-generation").click(); await page.waitForFunction(() => !document.getElementById("test-image-generation").disabled);
    testDetail = get("provider-test-result").locator(".request-detail"); await testDetail.locator("summary").click();
    assert.match(await testDetail.innerText(), /实际独立负面提示词/); assert.match(await testDetail.innerText(), /manual negative/); assert.match(await testDetail.innerText(), /手动附加请求体/);
    await page.evaluate(() => { window.__testTransportFailure = true; });
    await get("test-image-generation").click(); await page.waitForFunction(() => !document.getElementById("test-image-generation").disabled);
    assert.match(await get("provider-test-result").innerText(), /离线桥接传输失败/); assert.doesNotMatch(await get("provider-test-result").innerText(), /正在生成测试图/);
    await page.evaluate(() => { window.__testTransportFailure = false; window.__testResponse = null; });

    await page.locator("[data-page='active']").click();
    assert.equal(await get("a-concurrency").inputValue(), "3"); assert.equal(await get("a-concurrency").getAttribute("max"), "16");
    await get("a-concurrency").fill("5"); await get("active-form").getByRole("button", { name: "保存主动策略", exact: true }).click();
    await page.waitForFunction(() => !document.getElementById("dirty-label").textContent);
    assert.equal((await lastPost("settings")).active.max_concurrent_targets, 5); assert.equal(await get("a-concurrency").inputValue(), "5");

    assert.equal(await get("r-enabled").isChecked(), false, "缺省全局配图关闭");
    assert.equal(await get("r-probability").inputValue(), "20"); assert.equal(await get("r-interval").inputValue(), "300");
    assert.equal(await get("t-reply-enabled").isChecked(), false, "发现的聊天缺省配图关闭");
    await get("target-scope-filter").selectOption("group");
    assert.equal(await get("target-list").getByRole("button").count(), 1);
    await get("target-list").getByRole("button", { name: /群聊测试/ }).click();
    assert.equal(await get("t-enabled").isDisabled(), true); assert.equal(await get("t-morning").isDisabled(), true); assert.equal(await get("test-target").isDisabled(), true);
    assert.match(await get("t-scope-help").innerText(), /不会主动联系或发送早安/);
    await get("t-reply-enabled").check(); await get("t-reply-probability").fill("0");
    await get("target-form").getByRole("button", { name: "保存目标", exact: true }).click();
    await page.waitForFunction(() => !document.getElementById("dirty-label").textContent);
    let targetSaved = await lastPost("targets");
    assert.equal(targetSaved.umo, "offline:GroupMessage:group"); assert.equal(targetSaved.patch.reply_image_enabled, true); assert.equal(targetSaved.patch.reply_image_probability, 0);
    assert.equal(targetSaved.patch.enabled, false); assert.equal(targetSaved.patch.morning_enabled, false, "群聊保存不得开启主动或早安");
    await get("t-reply-probability").fill("100"); await get("target-form").getByRole("button", { name: "保存目标", exact: true }).click();
    await page.waitForFunction(() => !document.getElementById("dirty-label").textContent);
    assert.equal((await lastPost("targets")).patch.reply_image_probability, 100);
    await get("target-scope-filter").selectOption("private"); await get("target-list").getByRole("button", { name: /私聊测试/ }).click();
    assert.equal(await get("t-reply-enabled").isChecked(), false, "群聊设置不能改变私聊开关");
    assert.equal(await get("t-enabled").isDisabled(), false); assert.equal(await get("test-target").isDisabled(), false);
    await get("t-reply-enabled").check(); await get("t-reply-probability").fill("35");
    await get("target-form").getByRole("button", { name: "保存目标", exact: true }).click();
    await page.waitForFunction(() => !document.getElementById("dirty-label").textContent);
    assert.equal((await lastPost("targets")).patch.reply_image_probability, 35);
    await get("r-enabled").check(); await get("r-probability").fill("40"); await get("r-interval").fill("120"); await get("r-provider").selectOption("local-comfy");
    await get("reply-images-form").getByRole("button", { name: "保存配图设置", exact: true }).click();
    await page.waitForFunction(() => !document.getElementById("dirty-label").textContent);
    assert.deepEqual((await lastPost("settings")).reply_images, { enabled: true, default_probability: 40, min_interval_sec: 120, provider: "local-comfy" });
    assert.equal(await get("r-provider").inputValue(), "local-comfy"); assert.equal(await get("t-reply-probability").inputValue(), "35", "修改全局默认不得覆盖聊天概率");
    const settingsCount = await page.evaluate(() => window.__canvasCalls.filter(call => call.route === "settings").length);
    await get("r-probability").fill("101"); await get("reply-images-form").getByRole("button", { name: "保存配图设置", exact: true }).click();
    assert.equal(await page.evaluate(() => window.__canvasCalls.filter(call => call.route === "settings").length), settingsCount, "HTML范围校验阻止超过100%的概率");
    await get("restore-reply-images").click();

    await page.locator("[data-page='providers']").click(); await get("provider-list").getByRole("button", { name: /local-comfy/ }).click();
    assert.equal(await get("comfy-workflow").isVisible(), true); assert.match(await get("v-model-label").innerText(), /工作流显示名称/);
    assert.equal(await get("v-negative-mode").inputValue(), "disabled"); assert.equal(await get("v-negative-mode").isDisabled(), true);
    assert.match(await get("v-negative-help").innerText(), /保留工作流原有负面内容/);
    const apiWorkflow = { "6": { class_type: "CLIPTextEncode", inputs: { text: "positive" } }, "7": { class_type: "CLIPTextEncode", inputs: { text: "negative" } }, "3": { class_type: "KSampler", inputs: { seed: 9, steps: 20 } }, "9": { class_type: "SaveImage", inputs: { filename_prefix: "offline" } } };
    await get("v-workflow-file").setInputFiles({ name: "canvas-format.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify({ nodes: [], links: [] })) });
    await page.waitForFunction(() => document.getElementById("v-workflow-import-status").textContent.includes("前端画布格式"));
    assert.equal(JSON.parse(await get("v-workflow").inputValue())["6"].inputs.text, "default", "无效导入不能覆盖已保存工作流");
    await get("v-workflow-file").setInputFiles({ name: "local-api.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(apiWorkflow)) });
    await page.waitForFunction(() => document.getElementById("v-workflow-import-status").textContent.includes("共 4 个节点"));
    assert.deepEqual(JSON.parse(await get("v-workflow").inputValue()), apiWorkflow);
    const workflowFields = { prompt: "6.inputs.text", negative_prompt: "7.inputs.text", seed: "3.inputs.seed", steps: "3.inputs.steps" };
    await get("v-workflow-fields").fill(JSON.stringify(workflowFields)); await get("v-output-node").fill("9");
    assert.equal(await get("v-negative-mode").inputValue(), "field"); assert.equal(await get("v-negative-field-row").isVisible(), false);
    await saveProvider(); saved = await lastPost("providers");
    assert.deepEqual(saved.workflow, apiWorkflow); assert.deepEqual(saved.workflow_fields, workflowFields); assert.equal(saved.output_node, "9"); assert.equal(saved.negative_mode, "field");
    await get("list-image-models").click(); assert.equal(await get("image-model-options").locator("option").first().getAttribute("value"), "角色本地工作流");
    await get("test-image-connection").click(); assert.equal((await lastPost("image/test")).generate_image, false);
    await get("v-kind").selectOption("openai"); assert.equal(await get("comfy-workflow").isVisible(), false);
    await get("v-kind").selectOption("comfyui"); assert.deepEqual(JSON.parse(await get("v-workflow").inputValue()), apiWorkflow, "未保存的类型切换保留工作流编辑内容");
    await get("restore-provider").click();
    await page.locator("[data-page='studio']").click(); await get("g-provider").selectOption("local-comfy"); await get("g-override").check();
    assert.equal(await get("g-width").isDisabled(), true); assert.equal(await get("g-height").isDisabled(), true); assert.equal(await get("g-scale").isDisabled(), true);
    assert.equal(await get("g-steps").isDisabled(), false); assert.equal(await get("g-seed").isDisabled(), false, "仅允许覆盖工作流实际映射的参数");

    await page.locator("[data-page='studio']").click();
    const deliveryRow = get("job-list").locator("article").filter({ hasText: "已生成平台拒收" });
    const uncertainRow = get("job-list").locator("article").filter({ hasText: "已生成发送待确认" });
    const sendingRow = get("job-list").locator("article").filter({ hasText: "原图正在发送" });
    assert.equal(await deliveryRow.getByRole("button", { name: "重试", exact: true }).count(), 0, "发送失败不能安排重新绘图");
    assert.equal(await sendingRow.getByRole("button", { name: "正在发送原图…", exact: true }).isDisabled(), true);
    await deliveryRow.getByRole("button", { name: "补发原图", exact: true }).click();
    await page.waitForFunction(() => Boolean(window.__releaseResend));
    assert.equal(await deliveryRow.getByRole("button", { name: "正在补发原图…", exact: true }).isDisabled(), true);
    await page.locator("[data-page='history']").click();
    const deliveryHistory = get("history-list").locator("article").filter({ hasText: "已生成平台拒收" });
    assert.equal(await deliveryHistory.getByRole("button", { name: "正在补发原图…", exact: true }).isDisabled(), true, "任务与历史共用补发中的状态");
    assert.equal(await page.evaluate(() => window.__canvasCalls.filter(call => call.route === "jobs/resend").length), 1);
    await page.evaluate(() => { window.__releaseResend(); window.__releaseResend = null; });
    await page.waitForFunction(() => window.__canvasFixtures.jobs.items.find(job => job.id === "job-delivery-failed").status === "sent");
    await page.waitForFunction(() => !Array.from(document.querySelectorAll("#history-list button")).some(button => button.textContent === "正在补发原图…"));
    assert.equal(await deliveryHistory.getByRole("button", { name: /补发/ }).count(), 0, "已发送任务不保留重复补发入口");
    await page.locator("[data-page='studio']").click();
    await uncertainRow.getByRole("button", { name: "核对后补发原图", exact: true }).click();
    assert.equal(await get("confirm-ok").isDisabled(), true); assert.equal(await get("confirm-uncertain-row").isVisible(), true);
    await get("confirm-cancel").click();
    assert.equal(await page.evaluate(() => window.__canvasCalls.filter(call => call.route === "jobs/resend").length), 1, "未明确核对不能补发结果不明的任务");
    await uncertainRow.getByRole("button", { name: "核对后补发原图", exact: true }).click();
    await get("confirm-uncertain").check(); assert.equal(await get("confirm-ok").isDisabled(), false); await get("confirm-ok").click();
    await page.waitForFunction(() => Boolean(window.__releaseResend));
    assert.deepEqual(await lastPost("jobs/resend"), { id: "job-uncertain", confirm_uncertain: true });
    await page.evaluate(() => { window.__resendFailure = true; window.__releaseResend(); window.__releaseResend = null; });
    await page.waitForFunction(() => document.getElementById("notice").textContent === "离线平台明确拒收");
    assert.match(await get("notice").getAttribute("class"), /error/);
    assert.equal(await page.evaluate(() => window.__canvasCalls.filter(call => call.route === "generate").length), 1, "补发不得调用绘图入口");
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
    await page.locator("[data-page='active']").click();
    assert.equal(await get("a-concurrency").isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "移动主动策略页面不应横向溢出");
    if (process.env.CANVAS_UI_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.CANVAS_UI_SCREENSHOT_DIR, "webui-active-mobile.png"), fullPage: true });
    assert.equal(await get("r-enabled").isChecked(), true); assert.equal(await get("r-provider").inputValue(), "local-comfy");
    await get("target-scope-filter").selectOption("group"); await get("target-list").getByRole("button", { name: /群聊测试/ }).click();
    assert.equal(await get("t-reply-probability").inputValue(), "100"); assert.equal(await get("t-enabled").isDisabled(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "移动群聊配图页面不应横向溢出");
    await page.locator("[data-page='providers']").click(); await get("provider-list").getByRole("button", { name: /local-comfy/ }).click();
    assert.equal(await get("v-workflow").isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "移动工作流编辑器不应横向溢出");
    if (process.env.CANVAS_UI_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.CANVAS_UI_SCREENSHOT_DIR, "webui-comfy-mobile.png"), fullPage: true });
    await page.evaluate(() => { window.__canvasFixtures.jobs.items.find(job => job.id === "job-uncertain").status = "uncertain"; });
    await page.locator("[data-page='studio']").click(); await get("refresh-jobs").click();
    await uncertainRow.getByRole("button", { name: "核对后补发原图", exact: true }).click();
    assert.equal(await get("confirm-ok").isDisabled(), true);
    assert.equal(await page.evaluate(() => { const box = document.getElementById("confirm-dialog").getBoundingClientRect(); return box.left >= 0 && box.right <= innerWidth; }), true, "移动补发确认框应完整显示");
    if (process.env.CANVAS_UI_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.CANVAS_UI_SCREENSHOT_DIR, "webui-resend-mobile.png"), fullPage: true });
    await get("confirm-cancel").click();
    assert.deepEqual(browserErrors, [], "页面运行不应有未捕获的 JavaScript 异常");
    console.log("PASS: WebUI offline Chrome checks (ComfyUI workflow import and mapping, per-chat reply images and probability, private/group scopes, negative modes, mapping reset, Gemini options, failed test summaries, resend confirmation, target concurrency, escaping, mobile layout).");
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
