"use strict";

const $ = id => document.getElementById(id);
const model = { state: {}, personas: [], providers: [], targets: [], jobs: [], sessions: [], history: [], llms: [], astrbotPersonas: [], diagnostics: {} };
const selection = { persona: null, provider: null, target: null, reference: "", references: [], referencePreview: "", previewJob: "" };
const dirty = new Set();
let bridge = null, token = "", loading = false, polling = false, connected = false;
const pageNames = {
  overview: ["总览", "让对话、人设状态与画面保持一致。"], persona: ["人设工作台", "维护角色外观、判断规则与参考图。"],
  providers: ["模型接口", "管理绘画接口与动作判断模型。"], studio: ["生图工作台", "构图、预演与生成任务，集中在这里。"],
  active: ["主动消息", "安排自然的破冰与每日早安。"], history: ["生成历史", "回看画面与每次请求的执行结果。"], settings: ["运行设置", "控制对话集成、配额与默认生成参数。"]
};
const statusNames = { queued: "等待中", pending: "等待中", generating: "生成中", running: "生成中", processing: "生成中", sending: "发送中", sent: "已发送", uncertain: "发送结果待确认", succeeded: "已完成", success: "已完成", done: "已完成", completed: "已完成", failed: "失败", error: "失败", interrupted: "已中断", cancelled: "已取消" };
const finished = job => ["succeeded", "success", "done", "completed", "sent"].includes(job.status);
const failed = job => ["failed", "error", "interrupted", "cancelled", "uncertain"].includes(job.status);
const fullImages = new Map(), imageRequests = new Set();
const referenceImages = new Map();
const decisionNames = { photo: "同意拍摄", scene: "场景绘图", edit: "图片编辑", state: "更新状态", ask: "等待确认", refuse: "角色拒绝", chat: "文字回复", skip: "暂不联系", no_tool: "未调用拍照工具", blocked: "请求被拦截" };
const stageNames = { request: "请求检查", role: "角色判断", tool: "工具调用", conditions: "拍摄条件", reference: "参考图", quota: "额度", provider: "接口请求", generation: "生成结果", delivery: "平台发送", cancel: "撤回", schedule: "主动机会" };
const conditionNames = { outfit: "服装", camera: "镜头", pose: "姿势", expression: "表情", scene: "场景", avoid: "避免", notes: "约定" };
const negativeModes = { disabled: "不发送负面词", natural_language: "自然语言约束", field: "独立负面字段" };
const referenceSources = { persona: "角色参考图库", persona_gallery: "角色参考图库", explicit: "指定参考图", explicit_reference: "指定参考图", attachment: "聊天附件", message_attachment: "聊天附件", last_image: "上一张图片", none: "无参考图" };
const activeJob = job => !finished(job) && !failed(job);
const jobId = job => String(job.id || job.job_id || "");
const value = id => $(id).value.trim();
const num = id => Number($(id).value);
const check = id => $(id).checked;
const set = (id, val) => { $(id).value = val ?? ""; };
const setCheck = (id, val) => { $(id).checked = Boolean(val); };
const setText = (id, val) => { $(id).textContent = val ?? ""; };
const available = (id, enabled) => { $(id).disabled = !enabled; $(id).dataset.unavailable = enabled ? "false" : "true"; };
const date = timestamp => {
  if (!timestamp) return "—";
  const d = new Date(typeof timestamp === "number" ? timestamp * 1000 : timestamp);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString("zh-CN", { hour12: false });
};
function node(tag, className, text) { const el = document.createElement(tag); if (className) el.className = className; if (text != null) el.textContent = String(text); return el; }
function button(label, action, className = "button outline small") { const el = node("button", className, label); el.type = "button"; el.addEventListener("click", () => run(el, action)); return el; }
function empty(container, message) { container.replaceChildren(node("div", "empty", message)); }
function notice(message, error = false) { setText("notice", message); $("notice").hidden = !message; $("notice").className = `notice${error ? " error" : ""}`; }
function connection(ok, message) { connected = ok; setText("status-label", message); $("status-dot").className = `dot ${ok ? "online" : "error"}`; }
function markDirty(form, enabled = true) { enabled ? dirty.add(form) : dirty.delete(form); const count = Array.from(dirty).filter(id => !["generate-form", "simulate-form"].includes(id)).length; setText("dirty-label", count ? `${count} 个表单有未保存修改` : ""); }
async function run(el, action) {
  if (el.disabled) return;
  el.disabled = true;
  const label = el.textContent;
  el.textContent = "处理中…";
  try { return await action(); } catch (error) { notice(error.message || "操作失败", true); } finally { el.disabled = el.dataset.unavailable === "true"; el.textContent = label; }
}
function unwrap(result) {
  if (result?.status === "error" || result?.error) throw new Error(result.message || result.error || "请求失败");
  return result?.status === "ok" && Object.hasOwn(result, "data") ? result.data : result;
}
async function api(path, method = "GET", body) {
  if (bridge) return unwrap(method === "POST" ? await bridge.apiPost(`page/${path}`, body || {}) : await bridge.apiGet(`page/${path}`));
  if (!token) throw new Error("请先输入控制台访问令牌");
  const response = await fetch(`/api/page/${path}`, { method, headers: { Authorization: `Bearer ${token}`, ...(method === "POST" ? { "Content-Type": "application/json" } : {}) }, ...(method === "POST" ? { body: JSON.stringify(body || {}) } : {}), cache: "no-store" });
  let result;
  try { result = await response.json(); } catch { throw new Error(`服务器没有返回有效 JSON（${response.status}）`); }
  if (response.status === 401) { connection(false, "令牌已失效"); $("token-dialog").showModal(); }
  if (!response.ok) throw new Error(result.message || result.error || `请求失败（${response.status}）`);
  return unwrap(result);
}
async function confirmAction(title, message) {
  const dialog = $("confirm-dialog");
  setText("confirm-title", title); setText("confirm-message", message);
  return new Promise(resolve => {
    const finish = answer => { dialog.close(); $("confirm-ok").onclick = null; $("confirm-cancel").onclick = null; dialog.oncancel = null; resolve(answer); };
    $("confirm-ok").onclick = () => finish(true); $("confirm-cancel").onclick = () => finish(false);
    dialog.oncancel = event => { event.preventDefault(); finish(false); }; dialog.showModal();
  });
}
async function canReplace(form) { return !dirty.has(form) || await confirmAction("放弃未保存的修改？", "切换后，这个表单尚未保存的内容会被已保存的数据替换。"); }
function setPage(page, updateHash = true) {
  if (!Object.hasOwn(pageNames, page)) page = "overview";
  document.querySelectorAll("[data-panel]").forEach(el => { el.hidden = el.dataset.panel !== page; });
  document.querySelectorAll("[data-page]").forEach(el => { el.classList.toggle("active", el.dataset.page === page); el.setAttribute("aria-current", el.dataset.page === page ? "page" : "false"); });
  setText("page-title", pageNames[page][0]); setText("page-subtitle", pageNames[page][1]);
  if (updateHash && location.hash !== `#${page}`) history.replaceState(null, "", `#${page}`);
  window.scrollTo({ top: 0, behavior: "instant" });
}
function fillSelect(id, items, selected, emptyLabel = "") {
  const select = $(id), old = selected ?? select.value;
  select.replaceChildren();
  if (emptyLabel) { const option = node("option", "", emptyLabel); option.value = ""; select.append(option); }
  for (const item of items) { const option = node("option", "", item.label); option.value = item.value; select.append(option); }
  if (Array.from(select.options).some(option => option.value === String(old))) select.value = String(old);
}
function renderSelectors() {
  const personas = model.personas.map(p => ({ value: p.id, label: p.name || p.id }));
  for (const id of ["g-persona", "s-persona", "t-persona"]) fillSelect(id, personas, null, id === "t-persona" ? "默认视觉人设" : "");
  fillSelect("g-provider", model.providers.map(p => ({ value: p.name, label: `${p.name} · ${p.model || "未设模型"}` })));
  const astrbot = model.astrbotPersonas.map(p => ({ value: p.persona_id || p.id, label: p.name || p.persona_id || p.id }));
  fillSelect("p-astrbot", astrbot, null, "不绑定，使用默认视觉人设");
  $("session-options").replaceChildren(...model.sessions.map(s => { const el = node("option"); el.value = s.umo || s.unified_msg_origin || ""; return el; }));
}
function renderPersonas() {
  const container = $("persona-list"); container.replaceChildren();
  if (!model.personas.length) return empty(container, "尚无人设，点击新建开始。");
  for (const p of model.personas) {
    const el = button("", async () => { if (await canReplace("persona-form")) fillPersona(p); }, `select-item${p.id === selection.persona?.id ? " active" : ""}`);
    el.replaceChildren(node("strong", "", p.name || p.id), node("small", "", `${p.id === model.state.settings?.current_persona ? "默认 · " : ""}${p.astrbot_persona_id ? `绑定 ${p.astrbot_persona_id}` : "独立视觉人设"}`)); container.append(el);
  }
}
function fillPersona(p) {
  selection.persona = structuredClone(p); selection.references = [...new Set([p.reference_asset, ...(p.reference_assets || [])].filter(Boolean))]; if (p.reference_image && p.reference_asset) referenceImages.set(p.reference_asset, safeImage(p.reference_image));
  for (const [id, key] of [["p-name", "name"], ["p-description", "description"], ["p-consent", "consent_prompt"], ["p-positive", "positive_prompt"], ["p-negative", "negative_prompt"], ["p-style", "style_prompt"]]) set(id, p[key]);
  for (const key of ["outfit", "pose", "expression", "scene"]) set(`p-${key}`, p.state?.[key]);
  fillSelect("p-astrbot", model.astrbotPersonas.map(x => ({ value: x.persona_id || x.id, label: x.name || x.persona_id || x.id })), p.astrbot_persona_id, "不绑定，使用默认视觉人设");
  if (p.astrbot_persona_id && !Array.from($("p-astrbot").options).some(x => x.value === p.astrbot_persona_id)) { const el = node("option", "", p.astrbot_persona_id); el.value = p.astrbot_persona_id; $("p-astrbot").append(el); $("p-astrbot").value = p.astrbot_persona_id; }
  set("p-pool", (p.outfit_pool || []).join("\n")); setCheck("p-reference-enabled", p.reference_enabled); setCheck("p-set-current", p.id === model.state.settings?.current_persona);
  setText("persona-current", p.id === model.state.settings?.current_persona ? "当前默认" : "视觉档案"); available("delete-persona", p.id !== "default" && model.personas.some(x => x.id === p.id));
  renderReference(); renderPrompt(); markDirty("persona-form", false); renderPersonas();
}
function readPersona() {
  return { id: selection.persona?.id || `persona-${Date.now()}`, name: value("p-name"), description: value("p-description"), astrbot_persona_id: value("p-astrbot"), consent_prompt: value("p-consent"), positive_prompt: value("p-positive"), negative_prompt: value("p-negative"), style_prompt: value("p-style"), state: Object.fromEntries(["outfit", "pose", "expression", "scene"].map(key => [key, value(`p-${key}`)])), outfit_pool: $("p-pool").value.split(/\r?\n/).map(x => x.trim()).filter(Boolean), reference_enabled: check("p-reference-enabled"), reference_asset: selection.reference, reference_assets: selection.references, set_current: check("p-set-current") };
}
function renderPrompt() { const p = readPersona(); setText("prompt-preview", `正面\n${[p.style_prompt, p.positive_prompt, ...Object.values(p.state)].filter(Boolean).join(", ")}\n\n负面\n${p.negative_prompt || "未设置"}`); }
function renderReference() {
  selection.reference = selection.references[0] || "";
  selection.referencePreview = referenceImages.get(selection.reference) || "";
  setText("p-reference-name", selection.reference || "尚未上传"); const img = $("p-reference-preview");
  img.hidden = !selection.referencePreview; if (selection.referencePreview) img.src = selection.referencePreview; else img.removeAttribute("src");
  if (selection.reference && !selection.referencePreview && (bridge || token)) {
    const name = selection.reference;
    api(`reference/preview/${encodeURIComponent(name)}`).then(result => { referenceImages.set(name, safeImage(result.image)); if (selection.reference === name) { selection.referencePreview = safeImage(result.image); img.src = selection.referencePreview; img.hidden = !selection.referencePreview; } }).catch(() => {});
  }
  const gallery = $("reference-gallery"); gallery.replaceChildren();
  selection.references.forEach((name, index) => {
    const card = node("div", "reference-item"), image = node("img"); image.alt = `参考图 ${index + 1}`;
    if (referenceImages.get(name)) image.src = referenceImages.get(name);
    else api(`reference/preview/${encodeURIComponent(name)}`).then(result => { referenceImages.set(name, safeImage(result.image)); image.src = safeImage(result.image); }).catch(() => { image.hidden = true; });
    card.append(image, node("small", "", index === 0 ? "主参考图" : `辅助参考图 ${index + 1}`));
    if (index > 0) card.append(button("设为主图", async () => { selection.references = [name, ...selection.references.filter(x => x !== name)]; markDirty("persona-form"); renderReference(); }));
    card.append(button("移除", async () => { selection.references = selection.references.filter(x => x !== name); if (!selection.references.length) setCheck("p-reference-enabled", false); markDirty("persona-form"); renderReference(); }, "link-button danger-text")); gallery.append(card);
  });
}

function actionDetail(item) {
  const detail = node("details", "action-detail"); detail.append(node("summary", "", "查看拍摄条件与执行过程"));
  const conditions = Object.entries(item.requirements || {}).filter(([, val]) => val).map(([key, val]) => `${conditionNames[key] || key}：${val}`);
  if (conditions.length) detail.append(node("p", "conditions", conditions.join("\n")));
  if (item.reference_warning) detail.append(node("p", "danger-text", item.reference_warning));
  for (const step of item.trace || []) { const row = node("div", "trace-row"); row.append(node("strong", "", stageNames[step.stage] || step.stage), node("span", "", `${decisionNames[step.status] || statusNames[step.status] || (step.status === "ok" ? "通过" : step.status || "")} ${step.detail || ""}`)); detail.append(row); }
  if (!conditions.length && !item.trace?.length) detail.append(node("p", "muted", "这条早期记录没有保存执行过程。"));
  return detail;
}
function requestDetail(item) {
  const request = item.request_summary;
  if (!request || typeof request !== "object" || Array.isArray(request) || !Object.keys(request).length) return null;
  const preparedOnly = Boolean(request.notes);
  const detail = node("details", "request-detail"); detail.append(node("summary", "", preparedOnly ? "查看请求准备摘要" : "查看最终请求摘要"));
  if (request.notes) detail.append(node("p", "request-note", Array.isArray(request.notes) ? request.notes.join("\n") : String(request.notes)));
  const mode = request.negative_mode || "disabled";
  const rows = [["绘图模型", request.model || "接口默认"], ["负面处理", negativeModes[mode] || mode], ["负面字段", request.negative_prompt_field || "未使用独立字段"], ["参考图", `${request.reference_count ?? 0} 张 · ${referenceSources[request.reference_source] || request.reference_source || "未记录来源"}`], ["接口超时", request.provider_timeout_sec != null ? `${request.provider_timeout_sec} 秒` : "未记录"], ["任务超时", request.task_timeout_sec != null ? `${request.task_timeout_sec} 秒` : "未记录"], ["有效生成超时", request.effective_timeout_sec != null ? `${request.effective_timeout_sec} 秒（两者取较短）` : "未记录"]];
  const info = node("div", "info-list request-info");
  for (const [label, content] of rows) { const row = node("div", "info-row"); row.append(node("span", "", label), node("span", "", content)); info.append(row); }
  detail.append(info, node("h4", "", preparedOnly ? "准备阶段正面提示词" : "最终正面提示词"), node("pre", "request-prompt", request.prompt || "未提交"), node("h4", "", preparedOnly ? "准备阶段独立负面提示词" : "实际独立负面提示词"), node("pre", "request-prompt", request.negative_prompt || (mode === "natural_language" ? preparedOnly ? "尚未提交；自然语言约束将在适配器处理时加入正面描述。" : "未使用独立字段；约束已写入最终正面提示词。" : "未发送独立负面词。")), node("h4", "", preparedOnly ? "准备阶段生成参数" : "实际生成参数"), node("pre", "request-options", JSON.stringify(request.options || {}, null, 2)));
  if (mode === "disabled") detail.append(node("p", "muted", "此接口未发送负面词。可在模型接口设置自然语言约束，或确认接口支持后启用独立字段。"));
  if (item.failure_stage || item.error_stage) detail.append(node("p", "danger-text", `失败阶段：${stageNames[item.failure_stage || item.error_stage] || item.failure_stage || item.error_stage}`));
  return detail;
}
function renderDiagnostics() {
  const data = model.diagnostics || {}, setup = $("setup-list"); setup.replaceChildren();
  for (const step of data.setup || []) { const row = node("div", "setup-row"), body = node("div"); body.append(node("strong", "", `${step.ok === true ? "✓" : step.ok === false ? "○" : "◇"} ${step.name}`), node("small", "", step.message)); row.append(body, button("前往设置", async () => setPage(step.page))); setup.append(row); }
  const budget = data.budget || {};
  $("budget-status").replaceChildren(...[["messages", "主动消息"], ["photos", "主动照片"], ["llm", "角色判断"]].map(([key, label]) => node("span", "", `${label} ${budget[key]?.used ?? 0} / ${budget[key]?.limit ?? "—"}`)), node("small", "", `${budget.day || ""} · ${budget.timezone || ""}`));
  const container = $("diagnostic-list"), query = value("diagnostic-filter").toLowerCase(); container.replaceChildren();
  const items = (data.items || []).filter(item => !query || JSON.stringify(item).toLowerCase().includes(query)).slice(0, 40);
  if (!items.length) return empty(container, "暂无匹配的动作记录。可先预演角色反应，再发起一次请求。");
  for (const item of items) { const row = node("article", "diagnostic-row"); row.append(node("span", `badge ${item.status}`, decisionNames[item.status] || statusNames[item.status] || item.status), node("p", "", item.summary || item.request || item.reply || "主动联系机会"), node("small", "muted", `${item.umo || ""} · ${date(item.updated_at || item.at)}`)); if (item.error) row.append(node("p", "danger-text", item.error)); if (item.reply && item.reply !== item.request) row.append(node("p", "", item.reply)); const request = requestDetail(item); if (request) row.append(request); row.append(actionDetail(item)); container.append(row); }
}
function renderProviders() {
  const container = $("provider-list"); container.replaceChildren();
  if (!model.providers.length) return empty(container, "请新增绘画接口。");
  for (const p of model.providers) { const el = button("", async () => { if (await canReplace("provider-form")) fillProvider(p); }, `select-item${p.name === selection.provider?.name ? " active" : ""}`); el.replaceChildren(node("strong", "", p.name), node("small", "", `${p.name === model.state.settings?.default_provider ? "默认 · " : ""}${p.kind} / ${p.model || "未设模型"}`)); if (p.error) el.append(node("small", "danger-text", p.error)); container.append(el); }
}
function authDefaults(kind) { return kind === "gemini" ? { header: "x-goog-api-key", prefix: "" } : { header: "Authorization", prefix: "Bearer " }; }
let providerKind = "openai";
function defaultProviderAuth() {
  const defaults = authDefaults(value("v-kind")); set("v-header", defaults.header); set("v-prefix", defaults.prefix); markDirty("provider-form");
}
function changeProviderKind() {
  const before = authDefaults(providerKind), kind = value("v-kind"), next = authDefaults(kind);
  if (value("v-header") === before.header && $("v-prefix").value === before.prefix) { set("v-header", next.header); set("v-prefix", next.prefix); }
  const mode = value("v-negative-mode");
  if (providerKind === "novelai" && kind !== "novelai") set("v-negative-mode", "disabled");
  renderProviderFields(providerKind !== "novelai" ? mode : "disabled");
  providerKind = kind;
}
function providerNegativeMode(p) {
  if (p.kind === "novelai") return "field";
  return p.negative_mode || p.capabilities?.negative_mode || (p.kind === "custom" && p.negative_prompt === true ? "field" : "disabled");
}
function renderProviderFields(selectedMode = value("v-negative-mode")) {
  const kind = value("v-kind");
  const modes = kind === "novelai" ? ["field"] : kind === "gemini" ? ["disabled", "natural_language"] : ["disabled", "natural_language", "field"];
  fillSelect("v-negative-mode", modes.map(mode => ({ value: mode, label: negativeModes[mode] })), modes.includes(selectedMode) ? selectedMode : modes[0]);
  $("v-negative-mode").disabled = kind === "novelai";
  const field = value("v-negative-mode") === "field" && kind !== "novelai";
  $("v-negative-field-row").hidden = !field; $("v-negative-field").disabled = !field;
  setText("v-negative-help", kind === "novelai" ? "NovelAI 固定使用原生独立负面通道；负面词不会拼入正面标签。" : value("v-negative-mode") === "field" ? (kind === "openai" ? "仅适用于明确支持独立负面字段的中转接口。OpenAI 原生 Images 没有通用负面字段，请先核对接口文档。" : "按指定字段路径发送负面词，不再拼入正面提示词。") : value("v-negative-mode") === "natural_language" ? "将负面词转为自然语言约束加入正面描述。仅适用于理解自然语言指令的模型；标签模型应使用独立负面字段。" : "负面词不发送，也不会追加到正面提示词；可避免标签模型把禁止内容画进图片。");
  document.querySelectorAll("[data-provider-kinds]").forEach(el => { const enabled = el.dataset.providerKinds.split(" ").includes(kind); el.hidden = !enabled; el.querySelectorAll("input,select,textarea").forEach(input => { input.disabled = !enabled; }); });
  renderProviderTimeout();
}
function renderProviderTimeout() {
  const providerTimeout = num("v-timeout") || 180, taskTimeout = model.state.settings?.generation?.timeout_sec || 180;
  setText("v-timeout-summary", `接口超时 ${providerTimeout} 秒；已保存的任务生成超时 ${taskTimeout} 秒。有效生成超时为 ${Math.min(providerTimeout, taskTimeout)} 秒，聊天、工作台与测试图共用此限制。`);
}
function fillProvider(p) {
  selection.provider = structuredClone(p);
  for (const [id, key] of [["v-name", "name"], ["v-kind", "kind"], ["v-endpoint", "endpoint"], ["v-model", "model"], ["v-response", "response_path"]]) set(id, p[key] || (key === "kind" ? "openai" : ""));
  providerKind = p.kind || "openai"; const auth = authDefaults(providerKind);
  set("v-key", ""); set("v-header", p.auth_header ?? auth.header); set("v-prefix", p.auth_prefix ?? auth.prefix); set("v-timeout", p.timeout || p.timeout_sec || 180);
  set("v-extra", JSON.stringify(p.extra_body || {}, null, 2)); setCheck("v-edit", p.supports_image_edit); setCheck("v-set-default", p.name === model.state.settings?.default_provider); set("v-negative-field", p.negative_prompt_field ?? "negative_prompt");
  setText("provider-default", p.name === model.state.settings?.default_provider ? "默认绘画接口" : "独立接口"); setText("v-key-state", p.has_api_key || p.api_key_set ? "已保存密钥；留空保留，密钥不回显。" : "密钥不回显，填写后保存。");
  for (const [id, key] of [["v-generation-path", "generation_path"], ["v-edit-path", "edit_path"], ["v-models-path", "models_path"]]) set(id, p[key]);
  for (const [id, key] of [["v-prompt-field", "prompt_field"], ["v-model-field", "model_field"], ["v-reference-field", "reference_field"], ["v-reference-mime", "reference_mime_field"], ["v-models-response", "models_response_path"]]) set(id, p[key]);
  set("v-reference-format", p.reference_format || "data_url"); set("v-supported-sizes", Array.isArray(p.supported_sizes) ? p.supported_sizes.join("\n") : p.supported_sizes || "");
  setCheck("v-seed", p.supports_seed); setCheck("v-sampler", p.supports_sampler); set("v-options", p.option_fields ? JSON.stringify(p.option_fields, null, 2) : ""); setCheck("v-allow-urls", p.allow_image_urls); setCheck("v-clear-key", false); set("v-image-hosts", Array.isArray(p.allowed_image_hosts) ? p.allowed_image_hosts.join("\n") : p.allowed_image_hosts || "");
  renderProviderFields(providerNegativeMode(p));
  $("v-name").readOnly = model.providers.some(x => x.name === p.name); available("delete-provider", model.providers.length > 1 && model.providers.some(x => x.name === p.name));
  $("image-model-options").replaceChildren(); $("provider-test-result").replaceChildren(); if (p.error) $("provider-test-result").append(node("p", "danger-text", `当前配置无法使用，请修正后保存：${p.error}`)); markDirty("provider-form", false); renderProviders();
}
function readProvider() {
  let extra;
  try { extra = JSON.parse(value("v-extra") || "{}"); } catch { throw new Error("附加请求体需要是有效 JSON"); }
  if (!extra || typeof extra !== "object" || Array.isArray(extra)) throw new Error("附加请求体需要是 JSON 对象");
  const kind = value("v-kind"), mode = kind === "novelai" ? "field" : value("v-negative-mode");
  let fields;
  if (kind === "custom" && value("v-options")) { try { fields = JSON.parse(value("v-options")); } catch { throw new Error("参数字段映射需要是有效 JSON"); } if (!fields || typeof fields !== "object" || Array.isArray(fields)) throw new Error("参数字段映射需要是 JSON 对象"); }
  const data = { name: value("v-name"), kind, endpoint: value("v-endpoint"), model: value("v-model"), api_key: value("v-key"), auth_header: value("v-header"), auth_prefix: $("v-prefix").value, timeout: num("v-timeout"), extra_body: extra, response_path: value("v-response"), generation_path: value("v-generation-path"), edit_path: value("v-edit-path"), models_path: value("v-models-path"), allow_image_urls: check("v-allow-urls"), allowed_image_hosts: $("v-image-hosts").value.split(/\r?\n/).map(x => x.trim()).filter(Boolean), clear_api_key: check("v-clear-key"), supports_image_edit: check("v-edit"), negative_mode: mode, negative_prompt: mode === "field", set_default: check("v-set-default") };
  if (mode === "field" && kind !== "novelai") data.negative_prompt_field = value("v-negative-field") || "negative_prompt";
  if (kind === "custom") {
    data.supports_seed = check("v-seed"); data.supports_sampler = check("v-sampler");
    if (fields) data.option_fields = fields;
    for (const [id, key] of [["v-prompt-field", "prompt_field"], ["v-model-field", "model_field"], ["v-reference-field", "reference_field"], ["v-reference-mime", "reference_mime_field"], ["v-models-response", "models_response_path"]]) if (value(id) || Object.hasOwn(selection.provider || {}, key)) data[key] = value(id);
    data.reference_format = value("v-reference-format");
  }
  if (kind === "openai" || kind === "custom") { const sizes = $("v-supported-sizes").value.split(/\r?\n/).map(x => x.trim()).filter(Boolean); if (sizes.length) data.supported_sizes = sizes; else if (Object.hasOwn(selection.provider || {}, "supported_sizes")) data.supported_sizes = null; }
  return data;
}
function renderLlmOptions(selected) {
  fillSelect("l-provider", model.llms.map(p => ({ value: p.id, label: `${p.id} · ${p.model || p.type || "聊天模型"}` })), selected ?? null, "沿用当前会话模型");
  renderLlmModels();
}
function renderLlmModels() { const llm = model.llms.find(p => p.id === value("l-provider")); $("llm-model-options").replaceChildren(...(llm?.models || []).map(m => { const el = node("option"); el.value = typeof m === "string" ? m : m.id || m.name; return el; })); }
function fillLlm() { const s = model.state.settings?.llm || {}; renderLlmOptions(s.provider_id || ""); set("l-model", s.model); set("l-timeout", s.timeout_sec || 60); setCheck("l-fallback", s.fallback_to_current !== false); markDirty("llm-form", false); }
async function loadLlm() {
  const result = await api("llm/providers"); model.llms = result.items || []; model.astrbotPersonas = result.personas || [];
  renderSelectors(); if (!dirty.has("llm-form")) fillLlm(); else renderLlmOptions();
  if (!dirty.has("persona-form") && selection.persona) fillPersona(model.personas.find(p => p.id === selection.persona.id) || selection.persona);
}
function fillActive() {
  const a = model.state.settings?.active || {}, m = model.state.settings?.good_morning || {};
  setCheck("a-enabled", a.enabled); set("a-timezone", a.timezone || "Asia/Shanghai"); set("a-start", a.default_start || "09:00"); set("a-end", a.default_end || "22:00"); set("a-interval", a.check_interval_sec ?? 30); set("a-gap", a.min_gap_sec ?? 3600); set("a-idle", a.min_idle_sec ?? 1800); set("a-silence", a.silence_after ?? 3); set("a-silence-hours", a.silence_hours ?? 24);
  setCheck("m-enabled", m.enabled); set("m-timezone", m.timezone || "Asia/Shanghai"); set("m-start", m.start || "07:00"); set("m-end", m.end || "10:00"); set("a-jitter", a.jitter_percent ?? 15); const b = model.state.settings?.proactive_budget || {}; for (const [key, fallback] of [["messages", 10], ["photos", 2], ["llm", 24]]) set(`b-${key}`, b[key] ?? fallback); set("b-timezone", b.timezone || "Asia/Shanghai"); markDirty("active-form", false);
}
function renderTargets() {
  const container = $("target-list"); container.replaceChildren();
  if (!model.targets.length) return empty(container, "尚未发现私聊目标。");
  for (const t of model.targets) { const el = button("", async () => { if (await canReplace("target-form")) fillTarget(t); }, `select-item${t.umo === selection.target?.umo ? " active" : ""}`); el.replaceChildren(node("strong", "", t.name || t.sender_id || t.umo), node("small", "", `${t.enabled ? "已启用" : "已停用"} · 未回复 ${t.unanswered || 0} 次`)); container.append(el); }
}
function fillTarget(t) {
  selection.target = structuredClone(t); const a = model.state.settings?.active || {}, m = model.state.settings?.good_morning || {};
  set("t-umo", t.umo); $("t-umo").readOnly = true;
  fillSelect("t-persona", model.personas.map(p => ({ value: p.id, label: p.name || p.id })), t.persona_id || "", "默认视觉人设");
  set("t-timezone", t.timezone || a.timezone || "Asia/Shanghai"); set("t-start", t.start || a.default_start || "09:00"); set("t-end", t.end || a.default_end || "22:00"); set("t-gap", t.min_gap_sec ?? a.min_gap_sec ?? 3600); set("t-idle", t.min_idle_sec ?? a.min_idle_sec ?? 1800); set("t-silence", t.silence_after ?? a.silence_after ?? 3);
  setCheck("t-enabled", t.enabled); setCheck("t-image", t.with_image); setCheck("t-morning", t.morning_enabled !== false); set("t-morning-start", t.morning_start || m.start || "07:00"); set("t-morning-end", t.morning_end || m.end || "10:00");
  setText("target-status", !t.umo ? "等待私聊会话" : t.silent_until && t.silent_until > Date.now() / 1000 ? `静默至 ${date(t.silent_until)}` : t.enabled ? "已启用" : "未启用");
  const exists = model.targets.some(x => x.umo === t.umo); available("delete-target", exists); available("test-target", exists); const save = $("target-form").querySelector('[type="submit"]'); save.disabled = !exists; save.dataset.unavailable = exists ? "false" : "true";
  $("target-test-result").replaceChildren(); markDirty("target-form", false); renderTargets();
}
function readTarget() { return { enabled: check("t-enabled"), persona_id: value("t-persona"), timezone: value("t-timezone"), start: value("t-start"), end: value("t-end"), min_gap_sec: num("t-gap"), min_idle_sec: num("t-idle"), silence_after: num("t-silence"), with_image: check("t-image"), morning_enabled: check("t-morning"), morning_start: value("t-morning-start"), morning_end: value("t-morning-end") }; }
function fillSettings() {
  const s = model.state.settings || {}, c = s.integration || {}, d = s.dialogue || {}, q = s.moderation || {}, g = s.generation || {};
  setCheck("c-enabled", c.enabled !== false); set("c-mode", c.mode || "native_tools"); setCheck("c-strict", true); set("c-timeout", d.timeout_sec ?? 60); set("c-context", d.context_turns ?? 12); set("c-confirmation", d.confirmation_ttl_sec ?? 1800);
  setCheck("q-enabled", q.enabled !== false); set("q-daily", q.daily_limit ?? 5); set("q-interval", q.min_interval_sec ?? 20); set("q-concurrency", q.max_concurrency ?? 1); set("q-timezone", q.timezone || "Asia/Shanghai"); set("d-concurrency", g.max_concurrency ?? 2); set("d-timeout", g.timeout_sec ?? 180);
  for (const [id, key, fallback] of [["d-width", "width", 832], ["d-height", "height", 1216], ["d-steps", "steps", 28], ["d-scale", "scale", 5], ["d-sampler", "sampler", "k_euler_ancestral"], ["d-seed", "seed", -1], ["d-history", "max_history", 100]]) set(id, g[key] ?? fallback);
  const lifetimes = s.state_lifetimes || {}; for (const [key, fallback] of [["outfit", 0], ["pose", 1800], ["expression", 1800], ["scene", 14400]]) set(`s-${key}-life`, lifetimes[key + "_sec"] ?? fallback);
  markDirty("settings-form", false);
}
function fillGenerationDefaults() {
  if (dirty.has("generate-form")) return;
  const g = model.state.settings?.generation || {};
  for (const [id, key, fallback] of [["g-width", "width", 832], ["g-height", "height", 1216], ["g-steps", "steps", 28], ["g-scale", "scale", 5], ["g-seed", "seed", -1]]) set(id, g[key] ?? fallback);
  set("g-aspect-ratio", g.aspect_ratio || ""); set("g-image-size", g.image_size || "");
  if (!selection.previewJob) { set("g-persona", model.state.settings?.current_persona); set("s-persona", model.state.settings?.current_persona); set("g-provider", model.state.settings?.default_provider); }
  renderGenerationCapabilities();
}
function renderGenerationCapabilities() {
  const provider = model.providers.find(p => p.name === value("g-provider")), cap = provider?.capabilities || {};
  const override = check("g-override"), dimensions = provider?.kind !== "gemini";
  const mapped = key => provider?.kind !== "custom" || !provider.option_fields || Boolean(provider.option_fields[key]);
  for (const id of ["g-width", "g-height"]) { $(id).disabled = !override || !dimensions || !mapped(id.slice(2)); $(id).step = provider?.kind === "openai" ? "16" : "64"; $(id).closest("label").hidden = !dimensions; }
  for (const id of ["g-steps", "g-scale"]) $(id).disabled = !override || !cap.sampler || !mapped(id.slice(2));
  $("g-seed").disabled = !override || !cap.seed || !mapped("seed");
  for (const id of ["g-aspect-ratio", "g-image-size"]) { $(id).disabled = !override || provider?.kind !== "gemini"; $(id).closest("label").hidden = provider?.kind !== "gemini"; }
  const submit = $("generate-form").querySelector('[type="submit"]'); submit.disabled = cap.text_to_image === false; submit.dataset.unavailable = cap.text_to_image === false ? "true" : "false";
  const providerTimeout = provider?.timeout || provider?.timeout_sec || 180, taskTimeout = model.state.settings?.generation?.timeout_sec || 180;
  setText("g-capabilities", provider?.error ? `接口配置错误：${provider.error}。请到模型接口修正后保存。` : `${cap.dimensions || "尺寸取决于模型"}。${override ? "只提交接口支持的参数。" : "使用已保存默认参数，由适配器按模型能力处理。"}${cap.identity_reference ? ` ${cap.identity_reference}` : ""} 参考图上限：${cap.max_reference_images || 1} 张。负面处理：${cap.negative_prompt_mode || negativeModes[providerNegativeMode(provider || {})]}。有效生成超时：${Math.min(providerTimeout, taskTimeout)} 秒（接口 ${providerTimeout} / 任务 ${taskTimeout} 秒）。`);
}
function readGenerationOptions() {
  if (!check("g-override")) return {};
  const options = {};
  for (const [id, key] of [["g-width", "width"], ["g-height", "height"], ["g-steps", "steps"], ["g-scale", "scale"], ["g-seed", "seed"]]) if (!$(id).disabled) options[key] = num(id);
  for (const [id, key] of [["g-aspect-ratio", "aspect_ratio"], ["g-image-size", "image_size"]]) if (!$(id).disabled && value(id)) options[key] = value(id);
  return options;
}
function renderOverview() {
  const state = model.state, p = state.persona || {}, s = state.settings || {}, inflight = model.jobs.filter(activeJob), total = model.jobs.length;
  setText("overview-name", p.name || "默认人设"); setText("overview-description", p.description || "从人设工作台填写固定外观与参考图，再开始第一张画面。");
  $("overview-chips").replaceChildren(...[`绘画 ${s.default_provider || "未配置"}`, `参考图 ${p.reference_asset ? "已保存" : "未设置"}`, s.integration?.mode === "compatibility" ? "兼容动作模式" : "原生工具模式"].map(x => node("span", "", x)));
  setText("overview-state", Object.values(p.state || {}).filter(Boolean).join(" · ") || "尚未设置"); setText("overview-jobs", inflight.length); setText("overview-job-detail", `等待 / 进行中 · 共 ${total} 条任务`); setText("overview-targets", model.targets.filter(t => t.enabled).length); setText("overview-target-detail", `已启用 · 共 ${model.targets.length} 个目标`);
  const integration = state.integration || {}, capabilities = state.capabilities || {};
  const rows = [["对话动作", s.integration?.enabled === false ? "已关闭" : "已开启"], ["集成方式", s.integration?.mode === "compatibility" ? "兼容消息解析" : "AstrBot 原生工具"], ["当前会话模型", integration.provider_id || integration.chat_provider || "沿用 AstrBot 配置"], ["页面连接", bridge ? "AstrBot 内嵌管理页" : "兼容控制台"], ["能力状态", integration.message || capabilities.message || "以实际 Provider 与平台支持为准"]];
  if (integration.provider_error) rows.push(["绘画配置错误", integration.provider_error]);
  $("integration-summary").replaceChildren(...rows.map(([key, val]) => { const el = node("div", "info-row"); el.append(node("span", "", key), node("span", key === "绘画配置错误" ? "danger-text" : "", val)); return el; }));
  const tasks = $("overview-task-list"); tasks.replaceChildren();
  if (!model.jobs.length) empty(tasks, "暂无任务，去工作台提交一次生成。");
  for (const job of model.jobs.slice(0, 4)) { const row = node("div", "compact-row"), body = node("div"); body.append(node("span", "", String(job.caption || job.raw || job.request?.text || job.text || jobId(job)).slice(0, 55)), node("small", "", date(job.updated_at || job.created_at || job.at))); row.append(body, node("span", `badge ${job.status}`, statusNames[job.status] || job.status)); tasks.append(row); }
  setText("capability-detail", JSON.stringify({ capabilities, integration }, null, 2)); renderSessions();
}
function renderSessions() {
  const container = $("session-list"); container.replaceChildren();
  if (!model.sessions.length) return empty(container, "对话产生视觉动作后，会话状态会显示在这里。");
  for (const s of model.sessions.slice(0, 30)) { const row = node("div", "session-row"), identity = node("div"); identity.append(node("strong", "", s.umo || s.unified_msg_origin || "会话"), node("small", "", `${s.persona_id || "默认人设"} · ${date(s.updated_at)}`)); const state = s.state || s.visual_state || {}; row.append(identity, node("div", "", Object.values(state).filter(x => typeof x === "string" && x).join(" · ") || "尚无动态状态"), button("工作台使用", async () => { set("g-umo", s.umo || s.unified_msg_origin); if (s.persona_id) set("g-persona", s.persona_id); setPage("studio"); })); container.append(row); }
}
function safeImage(source) { return typeof source === "string" && /^data:image\/(png|jpeg|webp|gif|avif);base64,/i.test(source) ? source : ""; }
function jobImage(job) { return safeImage(job.image || job.image_url || job.result?.image || model.history.find(h => h.job_id === jobId(job) || h.id === jobId(job))?.image); }
function renderJobs() {
  const container = $("job-list"); container.replaceChildren();
  if (!model.jobs.length) empty(container, "暂无生成任务。");
  for (const job of model.jobs.slice(0, 40)) {
    const row = node("article", "task-row"), body = node("div"), actions = node("div", "actions");
    body.append(node("span", `badge ${job.status}`, statusNames[job.status] || job.status), node("p", "", job.caption || job.request?.text || job.text || "生成任务"), node("small", "", `${jobId(job)} · ${job.provider || job.request?.provider || "默认接口"} · ${date(job.created_at || job.at)}`));
    if (job.error) body.append(node("pre", "", job.error));
    if (finished(job) || jobImage(job)) actions.append(button("查看画面", async () => { selection.previewJob = jobId(job); renderPreview(job); }));
    if (["failed", "cancelled"].includes(job.status) && !job.cancel_requested) actions.append(button("重试", async () => { const result = await api("jobs/retry", "POST", { id: jobId(job) }); selection.previewJob = result.job_id; notice("已提交重试任务"); await refreshJobs(); }));
    if (job.status === "uncertain") body.append(node("p", "danger-text", "请先检查目标会话，确认是否已发送。发送结果不确定的任务不能直接重试。"));
    if (["queued", "deciding", "generating", "succeeded"].includes(job.status) && !job.cancel_requested) actions.append(button("取消拍摄", async () => { await api("jobs/cancel", "POST", { id: jobId(job) }); notice("已停止等待与发送；上游已受理的任务可能仍计费。"); await refreshJobs(); }));
    const request = requestDetail(job); if (request) body.append(request); body.append(actionDetail(job)); row.append(body, actions); container.append(row);
  }
  const current = model.jobs.find(j => jobId(j) === selection.previewJob) || model.jobs.find(finished) || model.jobs[0]; if (current) renderPreview(current);
}
function renderPreview(job) {
  setText("preview-status", statusNames[job.status] || job.status); $("preview-status").className = `badge ${job.status}`; setText("preview-caption", job.caption || job.request?.text || job.text || "");
  const image = fullImages.get(jobId(job)) || jobImage(job), stage = $("studio-preview");
  if (image) { const img = node("img"); img.src = image; img.alt = job.caption || "生成结果"; stage.replaceChildren(img); }
  else { const el = node("div", "empty"); el.append(node("span", "", failed(job) ? "×" : "✧"), node("p", "", failed(job) ? job.error || "生成失败，可在任务列表重试" : finished(job) ? "任务已完成，刷新历史查看画面" : "任务正在处理，完成后自动显示")); stage.replaceChildren(el); }
  if (finished(job) && selection.previewJob === jobId(job) && !fullImages.has(jobId(job)) && !imageRequests.has(jobId(job))) {
    const id = jobId(job); imageRequests.add(id);
    api(`jobs/${encodeURIComponent(id)}`).then(result => { if (safeImage(result.image)) { fullImages.set(id, result.image); if (fullImages.size > 3) fullImages.delete(fullImages.keys().next().value); if (selection.previewJob === id) renderPreview(result); } }).catch(error => notice(`原图读取失败：${error.message}`, true)).finally(() => imageRequests.delete(id));
  }
}
function renderHistory() {
  const filter = value("history-filter"), container = $("history-list"); container.replaceChildren();
  const list = model.history.filter(h => filter === "all" || (filter === "success" ? h.ok === true || finished(h) : h.ok === false || failed(h)));
  if (!list.length) return empty(container, "这个筛选条件下暂无记录。");
  for (const h of list) {
    const el = node("article", "history-item"), media = node("div", "history-image"), content = node("div", "history-content"), ok = h.ok === true || finished(h);
    if (safeImage(h.image)) { const img = node("img"); img.src = h.image; img.alt = h.caption || "生成结果"; img.loading = "lazy"; media.append(img); } else media.append(node("span", "", ok ? "图片不可用" : "生成未完成"));
    content.append(node("span", `badge ${ok ? "success" : "failed"}`, ok ? "生成成功" : "生成失败"), node("h4", "", h.caption || h.raw || h.request?.text || "生成请求"), node("small", "", `${h.provider || "默认接口"} / ${h.model || "默认模型"} · ${date(h.at || h.created_at)}`));
    if (h.error) content.append(node("p", "danger-text", h.error));
    const request = requestDetail(h); if (request) content.append(request);
    const detail = node("details"); detail.append(node("summary", "", request ? "查看状态修改与任务信息" : "查看历史提示词与任务信息"), node("pre", "", JSON.stringify({ ...(request ? {} : { prompt: h.prompt || "", negative_prompt: h.negative_prompt || h.negative || "", note: "早期记录未保存最终请求摘要，以下提示词不代表适配器实际提交内容。" }), mode: h.mode, state_patch: h.state_patch, job_id: h.job_id }, null, 2))); content.append(detail, actionDetail(h));
    const actions = node("div", "actions"); if (!ok && h.job_id && ["failed", "cancelled"].includes(model.jobs.find(j => jobId(j) === h.job_id)?.status || h.status) && !model.jobs.find(j => jobId(j) === h.job_id)?.cancel_requested) actions.append(button("重试任务", async () => { const result = await api("jobs/retry", "POST", { id: h.job_id }); selection.previewJob = result.job_id; setPage("studio"); await refreshJobs(); }));
    if (safeImage(h.image) && h.job_id) actions.append(button("保存原图", async () => { const job = await api(`jobs/${encodeURIComponent(h.job_id)}`); if (!safeImage(job.image)) throw new Error("原图不可用"); const a = node("a"); a.href = job.image; a.download = `persona-canvas-${h.job_id}.${job.image.startsWith("data:image/jpeg") ? "jpg" : job.image.startsWith("data:image/webp") ? "webp" : "png"}`; document.body.append(a); a.click(); a.remove(); }));
    content.append(actions); el.append(media, content); container.append(el);
  }
}
async function reload() {
  if (loading) return; loading = true;
  try {
    const [state, personas, providers, targets, jobs, sessions, historyResult, diagnostics] = await Promise.all([api("state"), api("personas"), api("providers"), api("targets"), api("jobs"), api("sessions"), api("history"), api("diagnostics")]);
    model.diagnostics = diagnostics; model.state = state; model.personas = personas.items || []; model.providers = providers.items || []; model.targets = targets.items || []; model.jobs = (jobs.items || []).slice().sort((a, b) => (b.created_at || b.at || 0) - (a.created_at || a.at || 0)); model.sessions = sessions.items || []; model.history = (historyResult.items || []).slice().sort((a, b) => (b.at || b.created_at || 0) - (a.at || a.created_at || 0));
    renderSelectors();
    if (!dirty.has("persona-form")) fillPersona(model.personas.find(p => p.id === selection.persona?.id) || model.personas.find(p => p.id === state.settings?.current_persona) || model.personas[0] || { id: `persona-${Date.now()}`, state: {} });
    if (!dirty.has("provider-form")) fillProvider(model.providers.find(p => p.name === selection.provider?.name) || model.providers.find(p => p.name === state.settings?.default_provider) || model.providers[0] || { name: "default", kind: "openai" });
    if (!dirty.has("target-form")) fillTarget(model.targets.find(t => t.umo === selection.target?.umo) || model.targets[0] || {});
    if (!dirty.has("active-form")) fillActive(); if (!dirty.has("settings-form")) fillSettings(); if (!dirty.has("llm-form")) fillLlm(); fillGenerationDefaults();
    renderPersonas(); renderProviders(); renderTargets(); renderOverview(); renderJobs(); renderHistory(); renderDiagnostics(); connection(true, bridge ? "AstrBot 已连接" : "兼容控制台已连接");
  } finally { loading = false; }
}
async function refreshJobs() {
  const [jobs, historyResult, sessions, diagnostics] = await Promise.all([api("jobs"), api("history"), api("sessions"), api("diagnostics")]); model.diagnostics = diagnostics;
  model.jobs = (jobs.items || []).slice().sort((a, b) => (b.created_at || b.at || 0) - (a.created_at || a.at || 0)); model.history = (historyResult.items || []).slice().sort((a, b) => (b.at || b.created_at || 0) - (a.at || a.created_at || 0)); model.sessions = sessions.items || [];
  renderOverview(); renderJobs(); renderHistory(); renderDiagnostics();
}
function bindForm(id, handler) { $(id).addEventListener("submit", event => { event.preventDefault(); const el = $(id).querySelector('[type="submit"]'); run(el, handler); }); $(id).addEventListener("input", () => markDirty(id)); $(id).addEventListener("change", () => markDirty(id)); }
function bind(id, action) { $(id).addEventListener("click", () => run($(id), action)); }
function testResult(id, result) {
  const container = $(id); container.replaceChildren(node("p", result.ok === false ? "danger-text" : "", result.message || `${result.ok === false ? "失败" : "成功"}${result.elapsed_ms != null ? ` · ${result.elapsed_ms} ms` : ""}${result.model ? ` · ${result.model}` : ""}${result.text ? ` · ${result.text}` : ""}`));
  if (safeImage(result.image)) { const img = node("img"); img.src = result.image; img.alt = "接口测试图片"; container.append(img); }
  const request = requestDetail(result); if (request) container.append(request);
  if (!request && result.effective_timeout_sec != null) container.append(node("p", "muted", `有效生成超时：${result.effective_timeout_sec} 秒。`));
  if (result.trace?.length) container.append(actionDetail(result));
}
async function savedProvider() { const name = value("v-name"); if (!model.providers.some(p => p.name === name)) throw new Error("请先保存这个绘画接口"); if (dirty.has("provider-form")) throw new Error("接口有未保存修改，请先保存后测试"); return name; }

document.querySelectorAll("[data-page]").forEach(el => el.addEventListener("click", () => setPage(el.dataset.page)));
document.querySelectorAll("[data-goto]").forEach(el => el.addEventListener("click", () => setPage(el.dataset.goto)));
window.addEventListener("hashchange", () => setPage(location.hash.slice(1), false)); setPage(location.hash.slice(1) || "overview", false);
bind("refresh", async () => { await reload(); notice(dirty.size ? "数据已刷新；未保存的表单内容已保留。" : "数据已刷新"); });
bind("new-persona", async () => { if (await canReplace("persona-form")) fillPersona({ id: `persona-${Date.now()}`, name: "新的人设", state: {}, outfit_pool: [] }); });
bind("restore-persona", async () => fillPersona(model.personas.find(p => p.id === selection.persona?.id) || selection.persona || { state: {} }));
bind("reset-state", async () => { for (const key of ["outfit", "pose", "expression", "scene"]) set(`p-${key}`, ""); markDirty("persona-form"); renderPrompt(); });
bind("clear-reference", async () => { selection.references = []; selection.reference = ""; selection.referencePreview = ""; setCheck("p-reference-enabled", false); markDirty("persona-form"); renderReference(); });
$("persona-form").addEventListener("input", renderPrompt);
bindForm("persona-form", async () => { const saved = await api("personas", "POST", readPersona()); selection.persona = saved; markDirty("persona-form", false); await reload(); notice("人设已保存"); });
bind("delete-persona", async () => { if (await confirmAction("删除这个人设？", "这会移除视觉档案；历史图片保留。正在使用这个人设的会话会回退到默认人设。")) { await api("personas/delete", "POST", { id: selection.persona.id }); selection.persona = null; markDirty("persona-form", false); await reload(); notice("人设已删除"); } });
$("p-reference-file").addEventListener("change", async event => {
  const files = Array.from(event.target.files || []); if (!files.length) return;
  const personaId = selection.persona?.id;
  try {
    if (selection.references.length + files.length > 8) throw new Error("参考图库最多 8 张，请先移除多余图片");
    for (const file of files) {
      if (!/^image\/(png|jpeg|webp)$/.test(file.type)) throw new Error("请选择 PNG、JPEG 或 WebP 图片");
      if (file.size > 16 * 1024 * 1024) throw new Error("每张参考图不能超过 16 MB");
    }
    notice("正在上传参考图…");
    for (const file of files) {
      const preview = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
      const result = bridge?.upload ? unwrap(await bridge.upload("page/reference/upload", file)) : await api("reference/upload", "POST", { data: preview.split(",")[1], name: file.name });
      if (selection.persona?.id !== personaId) throw new Error("上传期间切换了人设，请在所需人设重新上传");
      const name = result.asset || result.reference_asset;
      if (!name) throw new Error("上传成功但没有返回图片资产标识");
      referenceImages.set(name, preview); if (!selection.references.includes(name)) selection.references.push(name);
      setCheck("p-reference-enabled", true); renderReference(); markDirty("persona-form");
    }
    notice("参考图已上传，请保存人设使其生效。");
  } catch (error) { notice(error.message || "上传失败", true); } finally { event.target.value = ""; }
});
bind("new-provider", async () => { if (await canReplace("provider-form")) fillProvider({ name: "", kind: "openai", extra_body: {} }); });
bind("restore-provider", async () => fillProvider(model.providers.find(p => p.name === selection.provider?.name) || selection.provider || { kind: "openai" }));
bindForm("provider-form", async () => { const saved = await api("providers", "POST", readProvider()); selection.provider = saved; set("v-key", ""); markDirty("provider-form", false); await reload(); notice("绘画接口已保存"); });
bind("delete-provider", async () => { if (await confirmAction("删除绘画接口？", "已保存密钥也会移除。请确保保留至少一个可用接口。")) { await api("providers/delete", "POST", { name: selection.provider.name }); selection.provider = null; markDirty("provider-form", false); await reload(); notice("绘画接口已删除"); } });
bind("list-image-models", async () => { const result = await api("image/models", "POST", { name: await savedProvider() }); const models = result.models || result.items || []; $("image-model-options").replaceChildren(...models.map(m => { const el = node("option"); el.value = typeof m === "string" ? m : m.id || m.name; return el; })); testResult("provider-test-result", { ok: true, message: result.note || result.message || `已读取 ${models.length} 个模型${result.automatic === false ? "；这些是建议项，以测试结果为准" : ""}。可从模型输入框选择或手动填写。` }); });
bind("test-image-connection", async () => { const result = await api("image/test", "POST", { name: await savedProvider(), generate_image: false }); testResult("provider-test-result", result); });
bind("test-image-generation", async () => { const name = await savedProvider(); setText("provider-test-result", "正在生成小图，接口可能需要一段时间…"); const result = await api("image/test", "POST", { name, generate_image: true }); testResult("provider-test-result", result); });
bind("refresh-llm", async () => { await loadLlm(); notice("已读取 AstrBot 聊天 Provider 和人格"); }); $("l-provider").addEventListener("change", renderLlmModels);
bindForm("llm-form", async () => { await api("settings", "POST", { llm: { provider_id: value("l-provider"), model: value("l-model"), fallback_to_current: check("l-fallback"), timeout_sec: num("l-timeout") } }); markDirty("llm-form", false); await reload(); notice("动作判断模型设置已保存"); });
bind("restore-llm", async () => fillLlm()); bind("test-llm", async () => { setText("llm-test-result", "正在测试…"); testResult("llm-test-result", await api("llm/test", "POST", { provider_id: value("l-provider"), model: value("l-model") })); });
bindForm("generate-form", async () => { const result = await api("generate", "POST", { text: value("g-prompt"), mode: value("g-mode"), persona_id: value("g-persona"), provider: value("g-provider"), umo: value("g-umo"), reference_asset: value("g-reference"), ...readGenerationOptions() }); if (!result.job_id) { setText("generate-result", result.reply || "角色未安排生成动作，请查看预演或补充描述。"); await refreshJobs(); notice("角色判断已完成"); return; } selection.previewJob = result.job_id; setText("generate-result", `任务 ${result.job_id} 已提交，完成后自动显示结果。${result.caption ? `\n${result.caption}` : ""}`); await refreshJobs(); notice("生成任务已提交"); });
bind("use-persona-reference", async () => { const p = model.personas.find(x => x.id === value("g-persona")); if (!p?.reference_asset) throw new Error("所选人设没有保存参考图，请在人设工作台上传并保存"); set("g-reference", ""); markDirty("generate-form"); notice(`已使用人设参考图库，共 ${p.reference_assets?.length || 1} 张；实际使用数量取决于接口。`); });
bind("refresh-jobs", refreshJobs);
bindForm("simulate-form", async () => { setText("simulate-result", "正在读取角色判断…"); const result = await api("simulate", "POST", { text: value("s-text"), umo: value("s-umo"), persona_id: value("s-persona"), execute: false }); setText("simulate-result", JSON.stringify(result, null, 2)); });
bindForm("active-form", async () => { await api("settings", "POST", { active: { enabled: check("a-enabled"), timezone: value("a-timezone"), default_start: value("a-start"), default_end: value("a-end"), check_interval_sec: num("a-interval"), min_gap_sec: num("a-gap"), min_idle_sec: num("a-idle"), silence_after: num("a-silence"), silence_hours: num("a-silence-hours"), jitter_percent: num("a-jitter") }, proactive_budget: { messages: num("b-messages"), photos: num("b-photos"), llm: num("b-llm"), timezone: value("b-timezone") }, good_morning: { enabled: check("m-enabled"), timezone: value("m-timezone"), start: value("m-start"), end: value("m-end") } }); markDirty("active-form", false); await reload(); notice("主动消息策略已保存"); });
bind("restore-active", async () => fillActive()); bind("restore-target", async () => fillTarget(model.targets.find(t => t.umo === selection.target?.umo) || selection.target || {}));
bindForm("target-form", async () => { const umo = value("t-umo"), patch = readTarget(); await api("targets", "POST", { action: "save", umo, patch, ...patch }); selection.target = { umo }; markDirty("target-form", false); await reload(); notice("会话目标已保存"); });
bind("test-target", async () => { const umo = value("t-umo"); if (!model.targets.some(t => t.umo === umo)) throw new Error("请先保存目标会话"); if (await confirmAction("发送测试消息？", `这会向会话 ${umo} 立即发送一条测试消息。`)) { await api("targets", "POST", { action: "test", umo }); setText("target-test-result", "测试消息已发送，请在目标会话中查看。"); } });
bind("delete-target", async () => { if (await confirmAction("移除这个目标？", "移除后不会再向该会话发送主动消息；再次收到私聊消息时可重新发现它。")) { await api("targets", "POST", { action: "remove", umo: value("t-umo") }); selection.target = null; markDirty("target-form", false); await reload(); notice("主动目标已移除"); } });
bind("refresh-diagnostics", async () => { model.diagnostics = await api("diagnostics"); renderDiagnostics(); }); $("diagnostic-filter").addEventListener("input", renderDiagnostics);
bind("reload-history", async () => { model.history = (await api("history")).items || []; model.history.sort((a, b) => (b.at || 0) - (a.at || 0)); renderHistory(); }); $("history-filter").addEventListener("change", renderHistory);
bindForm("settings-form", async () => { await api("settings", "POST", { integration: { enabled: check("c-enabled"), mode: value("c-mode"), strict_trigger: true }, dialogue: { timeout_sec: num("c-timeout"), context_turns: num("c-context"), confirmation_ttl_sec: num("c-confirmation") }, state_lifetimes: Object.fromEntries(["outfit", "pose", "expression", "scene"].map(key => [key + "_sec", num(`s-${key}-life`)])), moderation: { enabled: check("q-enabled"), daily_limit: num("q-daily"), min_interval_sec: num("q-interval"), max_concurrency: num("q-concurrency"), timezone: value("q-timezone") }, generation: { width: num("d-width"), height: num("d-height"), steps: num("d-steps"), scale: num("d-scale"), sampler: value("d-sampler"), seed: num("d-seed"), max_history: num("d-history"), max_concurrency: num("d-concurrency"), timeout_sec: num("d-timeout") } }); markDirty("settings-form", false); await reload(); notice("运行设置已保存"); });
bind("restore-settings", async () => fillSettings());
bind("export-data", async () => { const data = await api("export"), blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }), url = URL.createObjectURL(blob), a = node("a"); a.href = url; a.download = `persona-canvas-backup-${new Date().toISOString().slice(0, 10)}.json`; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); notice("安全备份已导出，密钥不包含在其中。"); });
bind("theme-toggle", async () => { const html = document.documentElement; html.dataset.theme = html.dataset.theme === "dark" ? "light" : "dark"; });
$("g-provider").addEventListener("change", renderGenerationCapabilities); $("g-override").addEventListener("change", renderGenerationCapabilities);
$("v-kind").addEventListener("change", changeProviderKind); bind("default-provider-auth", async () => defaultProviderAuth());
$("v-negative-mode").addEventListener("change", () => renderProviderFields()); $("v-timeout").addEventListener("input", renderProviderTimeout);
$("token-form").addEventListener("submit", event => { event.preventDefault(); run($("token-form").querySelector('[type="submit"]'), async () => { token = value("access-token"); try { await reload(); $("token-dialog").close(); set("access-token", ""); setText("token-error", ""); notice(""); try { await loadLlm(); } catch (error) { notice(`主体已连接，模型列表读取失败：${error.message}`, true); } } catch (error) { token = ""; setText("token-error", error.message); throw error; } }); });
$("token-dialog").addEventListener("cancel", event => event.preventDefault()); bind("change-token", async () => { token = ""; connection(false, "等待连接"); $("token-dialog").showModal(); });
window.addEventListener("beforeunload", event => { if (Array.from(dirty).some(id => !["generate-form", "simulate-form"].includes(id))) { event.preventDefault(); event.returnValue = ""; } });

async function boot() {
  bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;
  if (!bridge) { $("change-token").hidden = false; $("token-dialog").showModal(); return; }
  try { const context = await bridge.ready(); if (context?.isDark != null) document.documentElement.dataset.theme = context.isDark ? "dark" : "light"; bridge.onContext?.(context => { if (context?.isDark != null) document.documentElement.dataset.theme = context.isDark ? "dark" : "light"; }); await reload(); try { await loadLlm(); } catch (error) { notice(`主体已连接，AstrBot 模型列表读取失败：${error.message}`, true); } }
  catch (error) { connection(false, "连接失败"); notice(`控制台连接失败：${error.message}。可点击刷新重试。`, true); }
}
await boot();
setInterval(async () => {
  if (!connected || polling || loading || document.hidden || !model.jobs.some(activeJob)) return;
  polling = true;
  try { await refreshJobs(); } catch (error) { notice(`任务状态刷新失败：${error.message}`, true); } finally { polling = false; }
}, 4000);
