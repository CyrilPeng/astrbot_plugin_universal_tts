/**
 * 通用 TTS · 引擎管理视图
 *
 * 运行在 AstrBot WebUI 的受限 iframe 中，所有后端调用都经由
 * window.AstrBotPluginView bridge 转发到插件注册的 Web API。
 */

const bridge = window.AstrBotPluginView;

const state = {
  engines: [],
  active_engine: "",
  disabled_engine: "__disabled__",
  templates: {},
  samples: [],
  settings: null,
  bindings: [],
  bindingEngines: [],
  history: [],
  status: null,
  token: "",
  plugin_enabled: null,
  global_tts_enabled: null,
};

/** @type {{index: number|null, templateKey: string, draft: object, secrets: Set<string>}|null} */
let editing = null;

let fileInput = null;
let confirmResolve = null;

const els = {};

function t(key, fallback, vars) {
  let text = fallback || key;
  if (bridge && typeof bridge.t === "function") {
    const translated = bridge.t(`views.engines.${key}`, fallback);
    if (translated) text = translated;
  }
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.split(`{${name}}`).join(String(value));
    }
  }
  return text;
}

function byId(id) {
  return document.getElementById(id);
}

function setBanner(message, kind) {
  const banner = els.banner;
  if (!message) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  banner.hidden = false;
  banner.textContent = message;
  banner.dataset.kind = kind || "error";
}

function setEditorError(message) {
  const box = els.editorError;
  if (!message) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.textContent = message;
}

/**
 * 视图 iframe 的 sandbox 是 allow-scripts allow-forms allow-downloads，
 * 不含 allow-modals：window.confirm() / alert() / prompt() 会被浏览器静默
 * 忽略并直接返回 false。所有确认一律走页面内的 <dialog>。
 */
function askConfirm(message, okLabel) {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    els.confirmMessage.textContent = message;
    els.confirmOk.textContent = okLabel || t("confirm", "确定");
    els.confirmCancel.textContent = t("cancel", "取消");
    els.confirm.showModal();
  });
}

function settleConfirm(value) {
  if (!confirmResolve) return;
  const resolve = confirmResolve;
  confirmResolve = null;
  if (els.confirm.open) els.confirm.close();
  resolve(value);
}

function templateLabel(templateKey) {
  const template = state.templates[templateKey];
  if (template && template.name) return template.name;
  return templateKey || t("unknown_template", "未知类型");
}

function engineName(engine) {
  return String((engine && engine.instance_name) || "").trim();
}

function activeName() {
  const active = String(state.active_engine || "").trim();
  if (active === state.disabled_engine) return null;
  if (active) return active;
  const first = state.engines[0];
  return first ? engineName(first) : null;
}

function isActive(engine) {
  const name = engineName(engine);
  return Boolean(name) && name === activeName();
}

function defaultEngineFor(templateKey) {
  const items = (state.templates[templateKey] || {}).items || {};
  const draft = { __template_key: templateKey };
  for (const [key, spec] of Object.entries(items)) {
    if (!spec || typeof spec !== "object") continue;
    draft[key] = spec.default !== undefined && spec.default !== null ? spec.default : "";
  }
  return draft;
}

/** 生成一个当前未被占用的实例名，避免新建时直接撞名。 */
function uniqueEngineName(base) {
  const used = new Set(state.engines.map(engineName));
  const clean = String(base || "").trim() || "engine";
  if (!used.has(clean)) return clean;
  let seq = 2;
  while (used.has(`${clean}${seq}`)) seq += 1;
  return `${clean}${seq}`;
}

function applyPayload(data) {
  if (!data || typeof data !== "object") return;
  state.engines = Array.isArray(data.engines) ? data.engines : [];
  state.active_engine = String(data.active_engine || "");
  state.disabled_engine = String(data.disabled_engine || "__disabled__");
  state.plugin_enabled =
    data.plugin_enabled === undefined || data.plugin_enabled === null
      ? null
      : Boolean(data.plugin_enabled);
  state.global_tts_enabled =
    data.global_tts_enabled === undefined || data.global_tts_enabled === null
      ? null
      : Boolean(data.global_tts_enabled);
  if (data.templates && typeof data.templates === "object") {
    state.templates = data.templates;
  }
  if (data.token) state.token = String(data.token);
  if (data.status && typeof data.status === "object") state.status = data.status;
  renderList();
  renderSummary();
}

/* ------------------------------------------------------------------ */
/* 配置指纹与错误处理                                                  */
/* ------------------------------------------------------------------ */

const STALE_HINTS = ["配置已被其他方式修改", "请刷新页面后重试", "请刷新后重试"];

function isStaleError(message) {
  const text = String(message || "");
  return STALE_HINTS.some((hint) => text.includes(hint));
}

/** 给写操作带上配置指纹，供后端做双入口冲突检测。 */
function withToken(payload) {
  const body = Object.assign({}, payload || {});
  if (state.token) body.token = state.token;
  return body;
}

async function reportError(error, scope) {
  const message = error && error.message ? error.message : String(error);
  if (isStaleError(message) && scope === "inline") {
    setEditorError(message);
  } else {
    setEditorError("");
    setBanner(message, "error");
  }
  if (isStaleError(message)) {
    // 配置已被别处改过，重新拉一遍保证界面与真实状态一致
    try {
      await loadEngines();
    } catch (reloadError) {
      /* 忽略：已经把错误提示给用户了 */
    }
  }
}

/* ------------------------------------------------------------------ */
/* 状态总览                                                            */
/* ------------------------------------------------------------------ */

function summaryItem(label, value, kind) {
  const wrap = document.createElement("div");
  wrap.className = "summary-item" + (kind ? ` summary-${kind}` : "");
  const name = document.createElement("span");
  name.className = "summary-label";
  name.textContent = label;
  const val = document.createElement("span");
  val.className = "summary-value";
  val.textContent = String(value);
  wrap.appendChild(name);
  wrap.appendChild(val);
  return wrap;
}

function renderSummary() {
  const box = els.summary;
  if (!box) return;
  box.innerHTML = "";
  const status = state.status || {};
  const active = String(status.active_engine || state.active_engine || "");
  let activeLabel;
  if (active === state.disabled_engine) {
    activeLabel = t("summary_active_off", "已关闭");
  } else if (active) {
    activeLabel = active;
  } else {
    activeLabel = t("summary_active_none", "未设置");
  }
  const missing = status.active_engine_exists === false;
  box.appendChild(
    summaryItem(t("summary_active", "当前默认引擎"), activeLabel, missing ? "warn" : ""),
  );
  box.appendChild(
    summaryItem(t("summary_engines", "引擎"), status.engine_count ?? state.engines.length),
  );
  box.appendChild(
    summaryItem(t("summary_bindings", "会话绑定"), status.binding_count ?? "—"),
  );
  box.appendChild(
    summaryItem(t("summary_samples", "音色样本"), status.sample_count ?? "—"),
  );
}

/* ------------------------------------------------------------------ */
/* 音频预览                                                            */
/* ------------------------------------------------------------------ */

const PREVIEW_POLL_INTERVAL = 800;
const PREVIEW_POLL_MAX = 90;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * 轮询试听任务结果。
 *
 * 后端把合成放到后台任务里，这里只发短请求 —— 因为视图经由反向代理访问，
 * 单个请求挂太久会被中间层以 524 掐断（实测不到 30 秒就会发生）。
 */
async function waitForPreview(jobId) {
  let lastError = null;
  for (let attempt = 0; attempt < PREVIEW_POLL_MAX; attempt += 1) {
    await sleep(attempt === 0 ? 300 : PREVIEW_POLL_INTERVAL);
    let data = null;
    try {
      data = await bridge.apiGet("engines/test/result", { job_id: jobId });
    } catch (error) {
      // 单次查询抖动不该让整次试听失败，继续轮询
      lastError = error;
      continue;
    }
    lastError = null;
    if (!data) continue;
    if (data.status === "done") return data;
    if (data.status === "error") {
      throw new Error(data.error || t("preview_failed", "试听失败"));
    }
  }
  if (lastError) throw lastError;
  throw new Error(t("preview_timeout", "试听超时，请稍后重试"));
}

async function runPreview(payload, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = t("testing", "合成中…");
  setEditorError("");
  setBanner("");
  try {
    const started = await bridge.apiPost("engines/test", payload);
    const jobId = started && started.job_id;
    if (!jobId) {
      throw new Error(t("preview_empty", "试听任务提交失败"));
    }
    const data = await waitForPreview(jobId);
    if (!data.audio) {
      throw new Error(t("preview_empty", "引擎没有返回音频"));
    }
    if (data.playable === false) {
      setBanner(
        t("preview_unplayable", "合成成功，但格式 {fmt} 无法在浏览器中播放。", {
          fmt: data.format || "?",
        }),
        "error",
      );
      return;
    }
    els.player.src = data.audio;
    els.player.hidden = false;
    try {
      els.player.currentTime = 0;
      await els.player.play();
    } catch (playError) {
      // 合成耗时较长时浏览器可能已丢失用户手势而拒绝自动播放，
      // 播放器就在页面上，用户可以直接点播放键。
    }
    setBanner(
      t("preview_ok", "试听成功（{fmt} · {ms} ms · {kb} KB）", {
        fmt: data.format || "?",
        ms: data.took_ms || data.elapsed_ms || 0,
        kb: Math.max(1, Math.round((data.bytes || 0) / 1024)),
      }),
      "ok",
    );
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    if (editing) setEditorError(message);
    else setBanner(message, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/* ------------------------------------------------------------------ */
/* 渲染                                                                */
/* ------------------------------------------------------------------ */

function makeButton(label, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function renderList() {
  els.list.innerHTML = "";
  const hasEngines = state.engines.length > 0;

  els.empty.hidden = hasEngines;
  if (!hasEngines) {
    els.empty.textContent = t("empty", "还没有配置任何引擎，点击右上角「新建引擎」开始。");
  }

  renderNotice();

  state.engines.forEach((engine, index) => {
    els.list.appendChild(renderRow(engine, index));
  });
}

/** 汇总所有"插件不会生效"的状态，一次性提示给用户。 */
function renderNotice() {
  const lines = [];
  let severe = false;

  if (state.plugin_enabled === false) {
    severe = true;
    lines.push(t("notice_plugin_off", "插件总开关（enable）已关闭，语音合成不会生效。"));
  }
  if (state.global_tts_enabled === false) {
    severe = true;
    lines.push(
      t(
        "notice_global_tts_off",
        "AstrBot 全局 TTS 开关已关闭，本插件不会接管语音合成。请在「配置 → 服务提供商 → TTS」中开启。",
      ),
    );
  }
  if (String(state.active_engine || "").trim() === state.disabled_engine) {
    lines.push(
      t(
        "notice_disabled",
        "全局默认 TTS 当前处于关闭状态（等同于 /tts_switch 0）。把任意引擎设为默认即可恢复。",
      ),
    );
  }

  els.notice.hidden = lines.length === 0;
  els.notice.innerHTML = "";
  if (!lines.length) return;
  els.notice.dataset.kind = severe ? "warn" : "info";
  lines.forEach((text) => {
    const line = document.createElement("div");
    line.className = "notice-line";
    line.textContent = text;
    els.notice.appendChild(line);
  });
}

function renderRow(engine, index) {
  const row = document.createElement("div");
  row.className = "row" + (isActive(engine) ? " row-active" : "");
  row.setAttribute("role", "listitem");

  const main = document.createElement("div");
  main.className = "row-main";

  const name = document.createElement("div");
  name.className = "row-name";
  const nameText = document.createElement("span");
  nameText.className = "row-name-text";
  nameText.textContent = engineName(engine) || t("unnamed", "（未命名）");
  name.appendChild(nameText);
  if (isActive(engine)) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = t("badge_default", "默认");
    name.appendChild(badge);
  }
  main.appendChild(name);

  const meta = document.createElement("div");
  meta.className = "row-meta";
  const bits = [templateLabel(engine.__template_key)];
  if (engine.voice_sample_name) bits.push(String(engine.voice_sample_name));
  meta.textContent = bits.join(" · ");
  main.appendChild(meta);

  row.appendChild(main);

  const actions = document.createElement("div");
  actions.className = "row-actions";

  const upBtn = makeButton(t("move_up", "↑"), "btn btn-icon", () => moveEngine(index, -1));
  upBtn.title = t("move_up_title", "上移");
  upBtn.disabled = index === 0;
  actions.appendChild(upBtn);

  const downBtn = makeButton(t("move_down", "↓"), "btn btn-icon", () =>
    moveEngine(index, 1),
  );
  downBtn.title = t("move_down_title", "下移");
  downBtn.disabled = index >= state.engines.length - 1;
  actions.appendChild(downBtn);

  const previewBtn = makeButton(t("preview", "试听"), "btn", () => {
    runPreview({ index }, previewBtn);
  });
  actions.appendChild(previewBtn);

  if (!isActive(engine)) {
    actions.appendChild(
      makeButton(t("activate", "设为默认"), "btn", () => activateEngine(index)),
    );
  }
  actions.appendChild(makeButton(t("clone", "复制"), "btn", () => cloneEngine(index)));
  actions.appendChild(makeButton(t("edit", "编辑"), "btn", () => openEditor(index)));
  actions.appendChild(
    makeButton(t("delete", "删除"), "btn btn-danger", () => deleteEngine(index)),
  );
  row.appendChild(actions);

  return row;
}

/* ------------------------------------------------------------------ */
/* 编辑器                                                              */
/* ------------------------------------------------------------------ */

function openEditor(index) {
  const templateKeys = Object.keys(state.templates);
  if (!templateKeys.length) {
    setBanner(t("no_templates", "未读取到引擎模板定义，请重载插件后重试。"), "error");
    return;
  }

  if (index === null || index === undefined) {
    const templateKey = templateKeys[0];
    const draft = defaultEngineFor(templateKey);
    if ("instance_name" in draft) {
      draft.instance_name = uniqueEngineName(draft.instance_name);
    }
    editing = {
      index: null,
      templateKey,
      draft,
      secrets: new Set(),
    };
  } else {
    const engine = state.engines[index] || {};
    const draft = JSON.parse(JSON.stringify(engine));
    delete draft._secrets;
    editing = {
      index,
      templateKey: engine.__template_key || templateKeys[0],
      draft,
      secrets: new Set(Array.isArray(engine._secrets) ? engine._secrets : []),
    };
  }

  setEditorError("");
  renderEditor();
  els.editor.showModal();
  loadSamples();
}

async function loadSamples() {
  try {
    const data = await bridge.apiGet("samples");
    state.samples = Array.isArray(data.samples) ? data.samples : [];
  } catch (error) {
    state.samples = [];
  }
  if (editing) renderEditor();
}

function renderEditor() {
  if (!editing) return;
  const { index, templateKey, draft } = editing;

  els.editorTitle.textContent =
    index === null ? t("new_title", "新建引擎") : t("edit_title", "编辑引擎");

  const templateKeys = Object.keys(state.templates);
  els.templateField.hidden = index !== null || templateKeys.length <= 1;
  els.templateSelect.innerHTML = "";
  templateKeys.forEach((key) => {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = templateLabel(key);
    option.selected = key === templateKey;
    els.templateSelect.appendChild(option);
  });

  const items = (state.templates[templateKey] || {}).items || {};
  els.fields.innerHTML = "";
  Object.entries(items).forEach(([key, spec]) => {
    if (!spec || typeof spec !== "object") return;
    els.fields.appendChild(renderField(key, spec, draft[key], editing.secrets.has(key)));
  });
}

function renderField(key, spec, value, secretIsSet) {
  if (key === "voice_sample_name") {
    return renderSampleField(key, spec, value);
  }

  const wrap = document.createElement("label");
  wrap.className = "field";

  const label = document.createElement("span");
  label.className = "field-label";
  label.textContent = spec.description || key;
  wrap.appendChild(label);

  const type = String(spec.type || "string");
  let input;

  if (type === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
  } else if (Array.isArray(spec.options) && spec.options.length) {
    input = document.createElement("select");
    const options = spec.options.map((item) => String(item));
    const current = value === undefined || value === null ? "" : String(value);
    if (current && !options.includes(current)) options.push(current);
    options.forEach((item) => {
      const option = document.createElement("option");
      option.value = item;
      option.textContent = item;
      input.appendChild(option);
    });
    if (current) input.value = current;
  } else if (type === "text") {
    input = document.createElement("textarea");
    input.rows = 3;
    input.value = value === undefined || value === null ? "" : String(value);
  } else if (type === "int" || type === "float") {
    input = document.createElement("input");
    input.type = "number";
    if (type === "float") input.step = "any";
    input.value = value === undefined || value === null ? "" : String(value);
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.value = value === undefined || value === null ? "" : String(value);
  }

  input.name = key;
  if (secretIsSet && type !== "bool") {
    input.placeholder = t("secret_set", "已设置，留空表示不修改");
  }
  wrap.appendChild(input);

  if (spec.hint) {
    const hint = document.createElement("span");
    hint.className = "field-hint";
    hint.textContent = String(spec.hint);
    wrap.appendChild(hint);
  }

  return wrap;
}

/** 音色样本字段：下拉选择 + 上传 / 删除。
 *  这里用 div 而不是 label：label 内的按钮点击会被转发给控件，导致双触发。
 */
function renderSampleField(key, spec, value) {
  const wrap = document.createElement("div");
  wrap.className = "field";

  const label = document.createElement("span");
  label.className = "field-label";
  label.textContent = spec.description || key;
  wrap.appendChild(label);

  const row = document.createElement("div");
  row.className = "sample-row";

  const select = document.createElement("select");
  select.name = key;

  const emptyOption = document.createElement("option");
  emptyOption.value = "";
  emptyOption.textContent = t("sample_default", "（自动使用第一个样本）");
  select.appendChild(emptyOption);

  const current = String(value || "");
  let matched = false;
  state.samples.forEach((sample) => {
    const option = document.createElement("option");
    option.value = sample.name;
    option.textContent = sample.exists
      ? sample.name
      : `${sample.name} ${t("sample_missing", "（文件缺失）")}`;
    if (sample.name === current) {
      option.selected = true;
      matched = true;
    }
    select.appendChild(option);
  });
  if (current && !matched) {
    const option = document.createElement("option");
    option.value = current;
    option.textContent = `${current} ${t("sample_missing", "（文件缺失）")}`;
    option.selected = true;
    select.appendChild(option);
  }
  row.appendChild(select);
  wrap.appendChild(row);

  const hint = document.createElement("span");
  hint.className = "field-hint";
  hint.textContent =
    spec.hint || t("sample_hint", "样本文件请在「音色样本」页上传、下载、重命名或删除。");
  wrap.appendChild(hint);

  return wrap;
}

function pickFile(onPicked, accept) {
  if (!fileInput) {
    fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.style.display = "none";
    document.body.appendChild(fileInput);
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = "";
      if (!file || typeof fileInput._onPicked !== "function") return;
      await fileInput._onPicked(file);
    });
  }
  fileInput.accept = accept || ".mp3,.wav,audio/mpeg,audio/wav";
  fileInput._onPicked = onPicked;
  fileInput.click();
}

async function uploadSampleFile(file) {
  if (!/\.(mp3|wav)$/i.test(file.name || "")) {
    setBanner(t("sample_bad_ext", "仅支持 mp3 / wav 文件"), "error");
    return;
  }
  els.btnUploadSample.disabled = true;
  setBanner(t("uploading", "上传中…"), "info");
  try {
    const data = await bridge.upload("samples/upload", file);
    state.samples = Array.isArray(data.samples) ? data.samples : state.samples;
    renderSamples();
    setBanner(t("sample_uploaded", "已上传样本 {name}", { name: data.name }), "ok");
  } catch (error) {
    setBanner(error && error.message ? error.message : String(error), "error");
  } finally {
    els.btnUploadSample.disabled = false;
  }
}

async function downloadSample(sample) {
  try {
    await bridge.download("samples/download", { path: sample.path }, sample.name);
  } catch (error) {
    setBanner(error && error.message ? error.message : String(error), "error");
  }
}

async function renameSample(sample, rawName) {
  const next = String(rawName || "").trim();
  if (!next || next === sample.name) {
    renamingPath = null;
    renderSamples();
    return;
  }
  try {
    const data = await bridge.apiPost(
      "samples/rename",
      withToken({ path: sample.path, new_name: next }),
    );
    state.samples = Array.isArray(data.samples) ? data.samples : state.samples;
    renamingPath = null;
    renderSamples();
    const refs = Number(data.renamed_refs || 0);
    setBanner(
      refs > 0
        ? t("sample_renamed_refs", "已重命名为 {name}，并同步更新 {count} 个引擎", {
            name: data.name,
            count: refs,
          })
        : t("sample_renamed", "已重命名为 {name}", { name: data.name }),
      "ok",
    );
    await loadEngines();
  } catch (error) {
    setBanner(error && error.message ? error.message : String(error), "error");
  }
}

async function deleteSampleEntry(sample) {
  const ok = await askConfirm(
    t("sample_confirm_delete", "确定删除样本「{name}」？", { name: sample.name }),
    t("delete", "删除"),
  );
  if (!ok) return;
  try {
    const data = await bridge.apiPost(
      "samples/delete",
      withToken({ path: sample.path }),
    );
    state.samples = Array.isArray(data.samples) ? data.samples : [];
    renderSamples();
    setBanner(t("sample_deleted", "已删除样本 {name}", { name: sample.name }), "ok");
    await loadEngines();
  } catch (error) {
    setBanner(error && error.message ? error.message : String(error), "error");
  }
}

function collectDraft() {
  if (!editing) return {};
  const items = (state.templates[editing.templateKey] || {}).items || {};
  const draft = { __template_key: editing.templateKey };
  for (const [key, spec] of Object.entries(items)) {
    if (!spec || typeof spec !== "object") continue;
    const input = els.fields.querySelector(`[name="${CSS.escape(key)}"]`);
    if (!input) continue;
    draft[key] = String(spec.type || "string") === "bool" ? input.checked : input.value;
  }
  return draft;
}

/** 提交前本地校验：重名、空名、非法数值。文案可走 i18n。 */
function validateDraft(engine) {
  const name = String(engine.instance_name || "").trim();
  if (!name) {
    return t("name_required", "实例名称不能为空");
  }
  const duplicate = state.engines.some(
    (item, idx) => idx !== editing.index && engineName(item) === name,
  );
  if (duplicate) {
    return t("name_duplicate", "已存在同名引擎「{name}」", { name });
  }

  const items = (state.templates[editing.templateKey] || {}).items || {};
  for (const [key, spec] of Object.entries(items)) {
    if (!spec || typeof spec !== "object") continue;
    const type = String(spec.type || "string");
    if (type !== "int" && type !== "float") continue;
    const raw = String(engine[key] === undefined ? "" : engine[key]).trim();
    if (raw === "") continue;
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) {
      return t("invalid_number", "「{field}」需要填写数字", {
        field: spec.description || key,
      });
    }
    if (type === "int" && !Number.isInteger(parsed)) {
      return t("invalid_integer", "「{field}」需要填写整数", {
        field: spec.description || key,
      });
    }
  }
  return "";
}

/* ------------------------------------------------------------------ */
/* 后端操作                                                            */
/* ------------------------------------------------------------------ */

async function loadEngines() {
  setBanner("");
  try {
    const data = await bridge.apiGet("engines");
    applyPayload(data);
  } catch (error) {
    setBanner(error && error.message ? error.message : String(error), "error");
  }
}

async function moveEngine(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= state.engines.length) return;
  const order = state.engines.map((_, i) => i);
  const [moved] = order.splice(index, 1);
  order.splice(target, 0, moved);
  setBanner("");
  try {
    applyPayload(await bridge.apiPost("engines/reorder", withToken({ order })));
  } catch (error) {
    await reportError(error);
  }
}

async function activateEngine(index) {
  const name = engineName(state.engines[index]);
  if (!name) return;
  setBanner("");
  try {
    applyPayload(await bridge.apiPost("engines/activate", withToken({ name })));
  } catch (error) {
    await reportError(error);
  }
}

async function cloneEngine(index) {
  const source = engineName(state.engines[index]);
  setBanner("");
  try {
    applyPayload(await bridge.apiPost("engines/clone", withToken({ index })));
    setBanner(
      t("cloned", "已复制「{name}」，记得修改实例名称。", { name: source }),
      "ok",
    );
  } catch (error) {
    await reportError(error);
  }
}

async function deleteEngine(index) {
  const name = engineName(state.engines[index]) || t("unnamed", "（未命名）");
  const ok = await askConfirm(
    t("confirm_delete", "确定删除引擎「{name}」？此操作不可撤销。", { name }),
    t("delete", "删除"),
  );
  if (!ok) return;
  setBanner("");
  try {
    applyPayload(await bridge.apiPost("engines/delete", withToken({ index })));
  } catch (error) {
    await reportError(error);
  }
}

async function saveEditor() {
  if (!editing) return;
  const engine = collectDraft();

  const problem = validateDraft(engine);
  if (problem) {
    setEditorError(problem);
    return;
  }

  const payload = { engine };
  if (editing.index !== null) payload.index = editing.index;

  setEditorError("");
  els.btnSave.disabled = true;
  try {
    const data = await bridge.apiPost("engines/save", withToken(payload));
    applyPayload(data);
    els.editor.close();
    setBanner(t("saved", "已保存"), "ok");
  } catch (error) {
    await reportError(error, "inline");
  } finally {
    els.btnSave.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* 初始化                                                              */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* 标签页                                                              */
/* ------------------------------------------------------------------ */

const TABS = ["engines", "samples", "bindings", "settings"];
let currentTab = "engines";
let renamingPath = null;

function formatSize(bytes) {
  const size = Number(bytes || 0);
  if (!size || size <= 0) return "0 B";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(ts) {
  const value = Number(ts || 0) * 1000;
  if (!value) return "";
  const date = new Date(value);
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

async function switchTab(name) {
  if (!TABS.includes(name)) return;
  currentTab = name;
  const map = {
    engines: [els.tabEnginesBtn, els.tabEngines],
    samples: [els.tabSamplesBtn, els.tabSamples],
    bindings: [els.tabBindingsBtn, els.tabBindings],
    settings: [els.tabSettingsBtn, els.tabSettings],
  };
  for (const [key, pair] of Object.entries(map)) {
    const active = key === name;
    pair[0].classList.toggle("tab-active", active);
    pair[0].setAttribute("aria-selected", active ? "true" : "false");
    pair[1].hidden = !active;
  }
  setBanner("");
  if (name === "samples") {
    await loadSamples();
    renderSamples();
  } else if (name === "bindings") {
    await loadBindings();
  } else if (name === "settings") {
    await loadSettings();
    await loadHistory();
  }
}

/* ------------------------------------------------------------------ */
/* 会话绑定                                                            */
/* ------------------------------------------------------------------ */

async function loadBindings() {
  els.bindingsList.innerHTML = "";
  try {
    const data = await bridge.apiGet("bindings");
    state.bindings = Array.isArray(data.bindings) ? data.bindings : [];
    state.bindingEngines = Array.isArray(data.engines) ? data.engines : [];
    if (data.token) state.token = String(data.token);
    renderBindings();
  } catch (error) {
    await reportError(error);
  }
}

function renderBindings() {
  const list = els.bindingsList;
  list.innerHTML = "";
  if (!state.bindings.length) {
    els.bindingsEmpty.hidden = false;
    els.bindingsEmpty.textContent = t(
      "bindings_empty",
      "当前没有会话绑定，所有会话都跟随全局引擎。",
    );
    return;
  }
  els.bindingsEmpty.hidden = true;
  state.bindings.forEach((binding) => list.appendChild(renderBindingRow(binding)));
}

function renderBindingRow(binding) {
  const row = document.createElement("div");
  row.className = "row" + (binding.exists ? "" : " row-warn");
  row.setAttribute("role", "listitem");

  const main = document.createElement("div");
  main.className = "row-main";

  const name = document.createElement("div");
  name.className = "row-name";
  const nameText = document.createElement("span");
  nameText.className = "row-name-text";
  nameText.textContent = binding.display || binding.session_id;
  name.appendChild(nameText);
  if (!binding.exists) {
    const badge = document.createElement("span");
    badge.className = "badge badge-danger";
    badge.textContent = t("binding_missing", "引擎已删除");
    name.appendChild(badge);
  }
  main.appendChild(name);

  const meta = document.createElement("div");
  meta.className = "row-meta";
  meta.textContent = binding.session_id;
  main.appendChild(meta);
  row.appendChild(main);

  const actions = document.createElement("div");
  actions.className = "row-actions";

  const select = document.createElement("select");
  const followOption = document.createElement("option");
  followOption.value = "";
  followOption.textContent = t("binding_follow", "跟随全局引擎");
  select.appendChild(followOption);

  const offOption = document.createElement("option");
  offOption.value = state.disabled_engine;
  offOption.textContent = t("binding_off", "不合成语音");
  select.appendChild(offOption);

  (state.bindingEngines || []).forEach((engineNameValue) => {
    const option = document.createElement("option");
    option.value = engineNameValue;
    option.textContent = engineNameValue;
    select.appendChild(option);
  });

  const current = String(binding.target || "");
  select.value = current;
  if (current && select.value !== current) {
    // 指向已被删除的引擎，补一个占位项，避免下拉自动跳到第一项
    const option = document.createElement("option");
    option.value = current;
    option.textContent = `${current} ${t("binding_missing", "引擎已删除")}`;
    select.appendChild(option);
    select.value = current;
  }
  select.addEventListener("change", () => setBinding(binding.session_id, select.value));
  actions.appendChild(select);

  actions.appendChild(
    makeButton(t("binding_unbind", "解除绑定"), "btn btn-danger", () =>
      deleteBinding(binding),
    ),
  );
  row.appendChild(actions);
  return row;
}

function applyBindingsPayload(data) {
  state.bindings = Array.isArray(data.bindings) ? data.bindings : [];
  if (Array.isArray(data.engines)) state.bindingEngines = data.engines;
  if (data.token) state.token = String(data.token);
  renderBindings();
}

async function setBinding(sessionId, target) {
  setBanner("");
  try {
    const data = await bridge.apiPost("bindings/set", {
      session_id: sessionId,
      target,
    });
    applyBindingsPayload(data);
    setBanner(
      target
        ? t("binding_saved", "已更新会话绑定")
        : t("binding_cleared", "已恢复为跟随全局引擎"),
      "ok",
    );
    await loadEngines();
  } catch (error) {
    await reportError(error);
    await loadBindings();
  }
}

async function deleteBinding(binding) {
  const ok = await askConfirm(
    t("binding_confirm_delete", "解除「{name}」的绑定？", {
      name: binding.display || binding.session_id,
    }),
    t("binding_unbind", "解除绑定"),
  );
  if (!ok) return;
  try {
    const data = await bridge.apiPost("bindings/delete", {
      session_id: binding.session_id,
    });
    applyBindingsPayload(data);
    setBanner(t("binding_removed", "已解除绑定"), "ok");
    await loadEngines();
  } catch (error) {
    await reportError(error);
    await loadBindings();
  }
}

async function clearBindings() {
  if (!state.bindings.length) return;
  const ok = await askConfirm(
    t("binding_confirm_clear", "确定清空全部 {count} 条会话绑定？", {
      count: state.bindings.length,
    }),
    t("binding_clear_all", "全部解绑"),
  );
  if (!ok) return;
  try {
    const data = await bridge.apiPost("bindings/clear", {});
    applyBindingsPayload(data);
    setBanner(t("binding_all_removed", "已清空全部会话绑定"), "ok");
    await loadEngines();
  } catch (error) {
    await reportError(error);
  }
}

/* ------------------------------------------------------------------ */
/* 配置导入 / 导出                                                     */
/* ------------------------------------------------------------------ */

function downloadText(filename, text) {
  // 视图 iframe 带 allow-downloads，Blob + <a download> 可用
  const blob = new Blob([text], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

async function exportConfig() {
  const includeSecrets = Boolean(els.exportSecrets && els.exportSecrets.checked);
  if (includeSecrets) {
    const ok = await askConfirm(
      t(
        "export_confirm_secrets",
        "导出的配置将包含明文 API Key 等密钥，请确认保存位置安全。继续？",
      ),
      t("export_action", "导出"),
    );
    if (!ok) return;
  }
  try {
    const data = await bridge.apiGet("config/export", {
      include_secrets: includeSecrets ? 1 : 0,
    });
    const stamp = formatTime(Math.floor(Date.now() / 1000))
      .replace(/[:\s]/g, "-")
      .replace(/-+/g, "-");
    downloadText(`universal-tts-config-${stamp}.json`, JSON.stringify(data, null, 2));
    setBanner(
      includeSecrets
        ? t("export_ok_secrets", "配置已导出（含密钥），请妥善保管")
        : t("export_ok", "配置已导出（已脱敏）"),
      "ok",
    );
    await loadHistory();
  } catch (error) {
    await reportError(error);
  }
}

async function importConfig(file) {
  if (!file) return;
  let parsed;
  try {
    parsed = JSON.parse(await file.text());
  } catch (error) {
    setBanner(t("import_bad_json", "文件不是合法的 JSON"), "error");
    return;
  }
  const config =
    parsed && typeof parsed.config === "object" && parsed.config !== null
      ? parsed.config
      : parsed;
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    setBanner(t("import_bad_format", "文件格式不正确，需为插件配置对象"), "error");
    return;
  }
  const count = Object.keys(config).length;
  const ok = await askConfirm(
    t("import_confirm", "将导入 {count} 个配置项，现有同名配置会被覆盖。确定继续？", {
      count,
    }),
    t("import_action", "导入"),
  );
  if (!ok) return;
  try {
    const data = await bridge.apiPost("config/import", { config });
    state.settings = data;
    if (data.token) state.token = String(data.token);
    renderSettings();
    const applied = Array.isArray(data.imported) ? data.imported.length : 0;
    setBanner(t("import_ok", "已导入 {count} 项配置", { count: applied }), "ok");
    await loadEngines();
    await loadHistory();
  } catch (error) {
    await reportError(error);
  }
}

/* ------------------------------------------------------------------ */
/* 变更历史                                                            */
/* ------------------------------------------------------------------ */

async function loadHistory() {
  if (!els.historyList) return;
  try {
    const data = await bridge.apiGet("history", { limit: 30 });
    state.history = Array.isArray(data.entries) ? data.entries : [];
    renderHistory();
  } catch (error) {
    els.historyList.textContent = error && error.message ? error.message : String(error);
  }
}

function renderHistory() {
  const box = els.historyList;
  if (!box) return;
  box.innerHTML = "";
  if (!state.history.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = t("history_empty", "还没有变更记录。");
    box.appendChild(empty);
    return;
  }
  state.history.forEach((entry) => {
    const line = document.createElement("div");
    line.className = "history-row";
    const when = document.createElement("span");
    when.className = "history-time";
    when.textContent = entry.time || formatTime(entry.ts);
    const what = document.createElement("span");
    what.className = "history-detail";
    what.textContent = entry.detail || entry.action || "";
    line.appendChild(when);
    line.appendChild(what);
    box.appendChild(line);
  });
}

/* ------------------------------------------------------------------ */
/* 音色样本                                                            */
/* ------------------------------------------------------------------ */

function renderSamples() {
  const list = els.samplesList;
  list.innerHTML = "";
  if (!state.samples.length) {
    els.samplesEmpty.hidden = false;
    els.samplesEmpty.textContent = t(
      "samples_empty",
      "还没有样本文件，先上传一个 mp3 或 wav。",
    );
    return;
  }
  els.samplesEmpty.hidden = true;
  state.samples.forEach((sample) => list.appendChild(renderSampleRow(sample)));
}

function renderSampleRow(sample) {
  const row = document.createElement("div");
  row.className = "row";
  row.setAttribute("role", "listitem");

  const main = document.createElement("div");
  main.className = "row-main";

  if (renamingPath === sample.path) {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "row-name-input";
    input.value = sample.name;
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        renameSample(sample, input.value);
      } else if (event.key === "Escape") {
        renamingPath = null;
        renderSamples();
      }
    });
    main.appendChild(input);

    const actions = document.createElement("div");
    actions.className = "row-actions";
    actions.appendChild(
      makeButton(t("save", "保存"), "btn btn-primary", () => renameSample(sample, input.value)),
    );
    actions.appendChild(
      makeButton(t("cancel", "取消"), "btn", () => {
        renamingPath = null;
        renderSamples();
      }),
    );
    row.appendChild(main);
    row.appendChild(actions);
    setTimeout(() => input.focus(), 0);
    return row;
  }

  const name = document.createElement("div");
  name.className = "row-name";
  const nameText = document.createElement("span");
  nameText.className = "row-name-text";
  nameText.textContent = sample.name;
  name.appendChild(nameText);
  if (!sample.exists) {
    const badge = document.createElement("span");
    badge.className = "badge badge-danger";
    badge.textContent = t("sample_missing", "文件缺失");
    name.appendChild(badge);
  }
  main.appendChild(name);

  const meta = document.createElement("div");
  meta.className = "row-meta";
  const refs = Array.isArray(sample.referenced_by) ? sample.referenced_by : [];
  const bits = [formatSize(sample.size)];
  if (refs.length) {
    bits.push(
      t("sample_used_by", "被 {count} 个引擎引用：{names}", {
        count: refs.length,
        names: refs.join("、"),
      }),
    );
  } else {
    bits.push(t("sample_unused", "未被任何引擎引用"));
  }
  meta.textContent = bits.join(" · ");
  main.appendChild(meta);

  row.appendChild(main);

  const actions = document.createElement("div");
  actions.className = "row-actions";
  actions.appendChild(makeButton(t("download", "下载"), "btn", () => downloadSample(sample)));
  actions.appendChild(
    makeButton(t("rename", "重命名"), "btn", () => {
      renamingPath = sample.path;
      renderSamples();
    }),
  );
  actions.appendChild(
    makeButton(t("delete", "删除"), "btn btn-danger", () => deleteSampleEntry(sample)),
  );
  row.appendChild(actions);
  return row;
}

/* ------------------------------------------------------------------ */
/* 全局设置                                                            */
/* ------------------------------------------------------------------ */

async function loadSettings() {
  els.settingsFields.innerHTML = "";
  try {
    const data = await bridge.apiGet("settings");
    state.settings = data;
    if (data && data.token) state.token = String(data.token);
    renderSettings();
  } catch (error) {
    setBanner(error && error.message ? error.message : String(error), "error");
  }
}

function renderSettings() {
  const container = els.settingsFields;
  container.innerHTML = "";
  const data = state.settings;
  if (!data || !Array.isArray(data.fields)) return;
  const secrets = new Set(Array.isArray(data.secrets) ? data.secrets : []);
  data.fields.forEach((field) => {
    container.appendChild(renderSettingField(field, secrets.has(field.key)));
  });
}

function renderSettingField(field, secretIsSet) {
  const wrap = document.createElement("div");
  wrap.className = "field";

  const label = document.createElement("span");
  label.className = "field-label";
  label.textContent = field.description || field.key;
  wrap.appendChild(label);

  const type = String(field.type || "string");
  const options = Array.isArray(field.options) ? field.options : [];
  let input;

  if (type === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(field.value);
  } else if (type === "int" || type === "float") {
    input = document.createElement("input");
    input.type = "number";
    input.step = type === "int" ? "1" : "any";
    input.value =
      field.value === null || field.value === undefined ? "" : String(field.value);
  } else if (type === "list") {
    input = document.createElement("textarea");
    input.rows = 4;
    input.value = Array.isArray(field.value)
      ? field.value.join("\n")
      : String(field.value || "");
  } else if (options.length) {
    input = document.createElement("select");
    options.forEach((option) => {
      const opt = document.createElement("option");
      opt.value = String(option);
      opt.textContent = String(option);
      if (String(field.value) === String(option)) opt.selected = true;
      input.appendChild(opt);
    });
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.value =
      field.value === null || field.value === undefined ? "" : String(field.value);
  }

  input.name = field.key;
  wrap.appendChild(input);

  const hintText = secretIsSet
    ? t("settings_secret_set", "已设置；留空表示保持不变。")
    : field.hint || "";
  if (hintText) {
    const hint = document.createElement("span");
    hint.className = "field-hint";
    hint.textContent = hintText;
    wrap.appendChild(hint);
  }
  return wrap;
}

async function saveSettings() {
  const values = {};
  const fields = (state.settings && state.settings.fields) || [];
  for (const field of fields) {
    const input = els.settingsFields.querySelector(`[name="${CSS.escape(field.key)}"]`);
    if (!input) continue;
    values[field.key] = field.type === "bool" ? Boolean(input.checked) : input.value;
  }

  els.btnSaveSettings.disabled = true;
  const original = els.btnSaveSettings.textContent;
  els.btnSaveSettings.textContent = t("saving", "保存中…");
  try {
    const data = await bridge.apiPost("settings/save", withToken({ values }));
    state.settings = data;
    if (data.token) state.token = String(data.token);
    renderSettings();
    setBanner(t("settings_saved", "设置已保存并即时生效"), "ok");
    await loadEngines();
    await loadHistory();
  } catch (error) {
    await reportError(error);
  } finally {
    els.btnSaveSettings.disabled = false;
    els.btnSaveSettings.textContent = original;
  }
}

function bindEvents() {
  els.btnNew.addEventListener("click", () => openEditor(null));
  els.btnCancel.addEventListener("click", () => els.editor.close());
  els.btnClose.addEventListener("click", () => els.editor.close());
  els.btnSave.addEventListener("click", saveEditor);
  els.btnPreview.addEventListener("click", () => {
    const payload = { engine: collectDraft() };
    if (editing && editing.index !== null) payload.index = editing.index;
    runPreview(payload, els.btnPreview);
  });
  els.templateSelect.addEventListener("change", () => {
    if (!editing) return;
    editing.templateKey = els.templateSelect.value;
    const draft = defaultEngineFor(editing.templateKey);
    if ("instance_name" in draft) {
      draft.instance_name = uniqueEngineName(draft.instance_name);
    }
    editing.draft = draft;
    editing.secrets = new Set();
    renderEditor();
  });
  els.editor.addEventListener("click", (event) => {
    if (event.target === els.editor) els.editor.close();
  });

  els.confirmOk.addEventListener("click", () => settleConfirm(true));
  els.confirmCancel.addEventListener("click", () => settleConfirm(false));
  els.confirm.addEventListener("cancel", (event) => {
    event.preventDefault();
    settleConfirm(false);
  });
  els.confirm.addEventListener("click", (event) => {
    if (event.target === els.confirm) settleConfirm(false);
  });

  els.tabEnginesBtn.addEventListener("click", () => switchTab("engines"));
  els.tabSamplesBtn.addEventListener("click", () => switchTab("samples"));
  els.tabBindingsBtn.addEventListener("click", () => switchTab("bindings"));
  els.tabSettingsBtn.addEventListener("click", () => switchTab("settings"));

  els.btnClearBindings.addEventListener("click", () => clearBindings());
  els.btnExport.addEventListener("click", () => exportConfig());
  els.btnImport.addEventListener("click", () =>
    pickFile((file) => importConfig(file), ".json,application/json"),
  );
  els.btnRefreshHistory.addEventListener("click", () => loadHistory());

  els.btnUploadSample.addEventListener("click", () => {
    pickFile((file) => uploadSampleFile(file));
  });
  els.btnSaveSettings.addEventListener("click", () => saveSettings());
}

async function main() {
  if (!bridge) {
    document.body.innerHTML =
      '<p style="padding:24px;font-family:sans-serif">此页面需要在 AstrBot WebUI 中打开。</p>';
    return;
  }

  Object.assign(els, {
    banner: byId("banner"),
    notice: byId("notice"),
    list: byId("list"),
    empty: byId("empty"),
    player: byId("player"),
    editor: byId("editor"),
    editorTitle: byId("editor-title"),
    editorError: byId("editor-error"),
    templateField: byId("template-field"),
    templateSelect: byId("template-select"),
    fields: byId("fields"),
    btnNew: byId("btn-new"),
    btnSave: byId("btn-save"),
    btnCancel: byId("btn-cancel"),
    btnClose: byId("btn-close"),
    btnPreview: byId("btn-preview"),
    confirm: byId("confirm"),
    confirmMessage: byId("confirm-message"),
    confirmOk: byId("confirm-ok"),
    confirmCancel: byId("confirm-cancel"),
    tabEnginesBtn: byId("tab-engines-btn"),
    tabSamplesBtn: byId("tab-samples-btn"),
    tabBindingsBtn: byId("tab-bindings-btn"),
    tabSettingsBtn: byId("tab-settings-btn"),
    tabEngines: byId("tab-engines"),
    tabSamples: byId("tab-samples"),
    tabBindings: byId("tab-bindings"),
    tabSettings: byId("tab-settings"),
    summary: byId("summary"),
    enginesHint: byId("engines-hint"),
    samplesList: byId("samples-list"),
    samplesEmpty: byId("samples-empty"),
    samplesHint: byId("samples-hint"),
    btnUploadSample: byId("btn-upload-sample"),
    bindingsList: byId("bindings-list"),
    bindingsEmpty: byId("bindings-empty"),
    bindingsHint: byId("bindings-hint"),
    btnClearBindings: byId("btn-clear-bindings"),
    settingsFields: byId("settings-fields"),
    settingsHint: byId("settings-hint"),
    btnSaveSettings: byId("btn-save-settings"),
    btnExport: byId("btn-export"),
    btnImport: byId("btn-import"),
    exportSecrets: byId("export-secrets"),
    exportSecretsLabel: byId("export-secrets-label"),
    historyList: byId("history-list"),
    historyHint: byId("history-hint"),
    btnRefreshHistory: byId("btn-refresh-history"),
  });

  bindEvents();

  await bridge.ready();

  const title = t("title", "通用 TTS 管理");
  byId("page-title").textContent = title;
  byId("page-desc").textContent = t(
    "description",
    "引擎、音色样本与全局设置的统一管理界面，改动保存后立即生效。",
  );
  els.btnNew.textContent = t("new", "新建引擎");
  els.btnSave.textContent = t("save", "保存");
  els.btnCancel.textContent = t("cancel", "取消");
  els.btnPreview.textContent = t("preview", "试听");
  els.tabEnginesBtn.textContent = t("tab_engines", "引擎");
  els.tabSamplesBtn.textContent = t("tab_samples", "音色样本");
  els.tabBindingsBtn.textContent = t("tab_bindings", "会话绑定");
  els.tabSettingsBtn.textContent = t("tab_settings", "全局设置");
  els.btnUploadSample.textContent = t("sample_upload", "上传样本");
  els.btnSaveSettings.textContent = t("settings_save", "保存设置");
  els.btnClearBindings.textContent = t("binding_clear_all", "全部解绑");
  els.btnExport.textContent = t("export", "导出");
  els.btnImport.textContent = t("import", "导入");
  els.exportSecretsLabel.textContent = t("export_secrets", "含密钥");
  els.btnRefreshHistory.textContent = t("history_refresh", "刷新");
  els.historyHint.textContent = t("history_hint", "最近 30 条配置改动记录");
  els.bindingsHint.textContent = t(
    "bindings_hint",
    "会话绑定优先于全局引擎，改动立即生效。",
  );
  els.enginesHint.textContent = t(
    "engines_hint",
    "把任意引擎设为默认，它就会接管全部会话。",
  );
  els.samplesHint.textContent = t(
    "samples_hint",
    "音色克隆用的参考音频，支持 mp3 / wav。",
  );
  els.settingsHint.textContent = t(
    "settings_hint",
    "这里的改动直接写入插件配置，保存后立即生效。",
  );
  document.title = title;

  await loadEngines();
  await loadSamples();

  if (bridge.onContext) {
    bridge.onContext(() => {
      document.title = t("title", "通用 TTS 管理");
      byId("page-title").textContent = t("title", "通用 TTS 管理");
    });
  }
}

main();
