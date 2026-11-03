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
  renderList();
}

/* ------------------------------------------------------------------ */
/* 音频预览                                                            */
/* ------------------------------------------------------------------ */

async function runPreview(payload, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = t("testing", "合成中…");
  setEditorError("");
  setBanner("");
  try {
    const data = await bridge.apiPost("engines/test", payload);
    if (!data || !data.audio) {
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
        ms: data.elapsed_ms || 0,
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

  const uploadBtn = makeButton(t("sample_upload", "上传"), "btn", () => {
    pickFile(async (file) => {
      await uploadSample(file, uploadBtn);
    });
  });
  row.appendChild(uploadBtn);

  const deleteBtn = makeButton(t("sample_delete", "删除"), "btn btn-danger", () => {
    deleteSample(select.value);
  });
  row.appendChild(deleteBtn);

  wrap.appendChild(row);

  const hint = document.createElement("span");
  hint.className = "field-hint";
  hint.textContent =
    spec.hint || t("sample_hint", "可上传 mp3 / wav 样本；选择后该引擎将使用它克隆音色。");
  wrap.appendChild(hint);

  return wrap;
}

function pickFile(onPicked) {
  if (!fileInput) {
    fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = ".mp3,.wav,audio/mpeg,audio/wav";
    fileInput.style.display = "none";
    document.body.appendChild(fileInput);
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = "";
      if (!file || typeof fileInput._onPicked !== "function") return;
      await fileInput._onPicked(file);
    });
  }
  fileInput._onPicked = onPicked;
  fileInput.click();
}

async function uploadSample(file, button) {
  if (!/\.(mp3|wav)$/i.test(file.name || "")) {
    setEditorError(t("sample_bad_ext", "仅支持 mp3 / wav 文件"));
    return;
  }
  const original = button.textContent;
  button.disabled = true;
  button.textContent = t("uploading", "上传中…");
  setEditorError("");
  try {
    const data = await bridge.upload("samples/upload", file);
    state.samples = Array.isArray(data.samples) ? data.samples : state.samples;
    editing.draft = Object.assign(collectDraft(), {
      voice_sample_name: data.name || "",
    });
    renderEditor();
    setBanner(t("sample_uploaded", "已上传样本 {name}", { name: data.name }), "ok");
  } catch (error) {
    setEditorError(error && error.message ? error.message : String(error));
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function deleteSample(name) {
  if (!name) {
    setEditorError(t("sample_pick_first", "请先选择一个样本"));
    return;
  }
  const sample = state.samples.find((item) => item.name === name);
  if (!sample) {
    setEditorError(t("sample_pick_first", "请先选择一个样本"));
    return;
  }
  const ok = await askConfirm(
    t("sample_confirm_delete", "确定删除样本「{name}」？", { name }),
    t("delete", "删除"),
  );
  if (!ok) return;
  setEditorError("");
  try {
    const data = await bridge.apiPost("samples/delete", { path: sample.path });
    state.samples = Array.isArray(data.samples) ? data.samples : [];
    editing.draft = Object.assign(collectDraft(), { voice_sample_name: "" });
    renderEditor();
    setBanner(t("sample_deleted", "已删除样本 {name}", { name }), "ok");
  } catch (error) {
    setEditorError(error && error.message ? error.message : String(error));
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

async function activateEngine(index) {
  const name = engineName(state.engines[index]);
  if (!name) return;
  setBanner("");
  try {
    applyPayload(await bridge.apiPost("engines/activate", { name }));
  } catch (error) {
    setBanner(error && error.message ? error.message : String(error), "error");
  }
}

async function cloneEngine(index) {
  const source = engineName(state.engines[index]);
  setBanner("");
  try {
    applyPayload(await bridge.apiPost("engines/clone", { index }));
    setBanner(
      t("cloned", "已复制「{name}」，记得修改实例名称。", { name: source }),
      "ok",
    );
  } catch (error) {
    setBanner(error && error.message ? error.message : String(error), "error");
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
    applyPayload(await bridge.apiPost("engines/delete", { index }));
  } catch (error) {
    setBanner(error && error.message ? error.message : String(error), "error");
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
    const data = await bridge.apiPost("engines/save", payload);
    applyPayload(data);
    els.editor.close();
    setBanner(t("saved", "已保存"), "ok");
  } catch (error) {
    setEditorError(error && error.message ? error.message : String(error));
  } finally {
    els.btnSave.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* 初始化                                                              */
/* ------------------------------------------------------------------ */

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
  });

  bindEvents();

  await bridge.ready();

  const title = t("title", "通用 TTS 管理");
  byId("page-title").textContent = title;
  byId("page-desc").textContent = t(
    "description",
    "在这里增删改查引擎实例，改动保存后立即生效，无需重启 AstrBot。",
  );
  els.btnNew.textContent = t("new", "新建引擎");
  els.btnSave.textContent = t("save", "保存");
  els.btnCancel.textContent = t("cancel", "取消");
  els.btnPreview.textContent = t("preview", "试听");
  document.title = title;

  await loadEngines();

  if (bridge.onContext) {
    bridge.onContext(() => {
      document.title = t("title", "通用 TTS 管理");
      byId("page-title").textContent = t("title", "通用 TTS 管理");
    });
  }
}

main();
