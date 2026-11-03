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
};

/** @type {{index: number|null, templateKey: string, draft: object, secrets: Set<string>}|null} */
let editing = null;

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
  if (data.templates && typeof data.templates === "object") {
    state.templates = data.templates;
  }
  renderList();
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

  const globalDisabled = String(state.active_engine || "").trim() === state.disabled_engine;
  els.notice.hidden = !globalDisabled;
  if (globalDisabled) {
    els.notice.textContent = t(
      "notice_disabled",
      "全局默认 TTS 当前处于关闭状态（等同于 /tts_switch 0）。把任意引擎设为默认即可恢复。",
    );
  }

  state.engines.forEach((engine, index) => {
    els.list.appendChild(renderRow(engine, index));
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
  nameText.textContent = engineName(engine) || t("unnamed", "（未命名）");
  nameText.style.overflow = "hidden";
  nameText.style.textOverflow = "ellipsis";
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
  meta.textContent = templateLabel(engine.__template_key);
  main.appendChild(meta);

  row.appendChild(main);

  const actions = document.createElement("div");
  actions.className = "row-actions";
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
  if (!window.confirm(t("confirm_delete", "确定删除引擎「{name}」？此操作不可撤销。", { name }))) {
    return;
  }
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
  if (!String(engine.instance_name || "").trim()) {
    setEditorError(t("name_required", "实例名称不能为空"));
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
  });

  bindEvents();

  await bridge.ready();

  const title = t("title", "TTS 引擎管理");
  byId("page-title").textContent = title;
  byId("page-desc").textContent = t(
    "description",
    "在这里增删改查引擎实例，改动保存后立即生效，无需重启 AstrBot。",
  );
  els.btnNew.textContent = t("new", "新建引擎");
  els.btnSave.textContent = t("save", "保存");
  els.btnCancel.textContent = t("cancel", "取消");
  document.title = title;

  await loadEngines();

  if (bridge.onContext) {
    bridge.onContext(() => {
      document.title = t("title", "TTS 引擎管理");
      byId("page-title").textContent = t("title", "TTS 引擎管理");
    });
  }
}

main();
