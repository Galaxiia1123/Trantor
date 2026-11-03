(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const list = (value) => Array.isArray(value) ? value : [];
  const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const pretty = (value) => JSON.stringify(value, null, 2);
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const PAGE_SIZE = 40;
  const ACTIVE = new Set(["ready", "running", "paused"]);
  const REPORTABLE = new Set(["paused", "completed", "stopped", "failed", "interrupted"]);
  const TECHNICAL = new Set(["model_input", "model_output", "report_input", "report_output", "analysis_input", "analysis_output", "report_batch_input", "report_batch_output"]);
  const STATUS = { ready: "准备中", running: "运行中", paused: "已暂停", stopped: "已提前结束", completed: "已完成", failed: "运行失败", interrupted: "等待恢复" };
  const KINDS = { run_created: "创建运行", run_started: "开始运行", observation: "主体观察", expression: "表达原文", message_delivered: "消息投递", window_committed: "窗口结算", pause_requested: "请求暂停", stop_requested: "请求结束", run_status: "运行状态", run_error: "运行错误", model_failure: "模型调用失败", scene_parse: "场景解析", weight_estimation: "影响估权", model_input: "模型输入", model_output: "模型输出", report_input: "报告输入", report_output: "报告输出", report_created: "报告生成", project_linked:"关联项目", run_recovered:"恢复运行", analysis_answer:"访谈与问答", analysis_input:"分析输入", analysis_output:"分析输出", provider_attempt:"提供商回执", settlement_rejected:"结算未通过", report_batch_input:"分批报告输入", report_batch_output:"分批摘要" };
  const state = {
    draft: null, preparation: [], runId: null, runScenario: null, runConfig: null,
    snapshot: null, events: [], eventIds: new Set(), discardedIds:new Set(), offset: 0, eventPage: 0,
    busy: false, polling: null, pollTimer: null, settledPolls: 0, stream:null, streamErrors:0, streamFailed:false, hasMore:false,
    stateSignature: null, report: null, reportWindow: null, urls: new Map(),
  };
  let definition, assistantDefinition, studio;

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = String(content);
    return node;
  }

  function notice(message) {
    $("notice").textContent = message;
    $("notice").hidden = !message;
  }

  function showError(error) {
    $("error-notice").textContent = error instanceof Error ? error.message : String(error);
    $("error-notice").hidden = false;
  }

  function clearError() {
    $("error-notice").textContent = "";
    $("error-notice").hidden = true;
  }

  async function api(path, body, asBlob = false) {
    let response;
    try {
      response = await fetch(path, {
        method: body === undefined ? "GET" : "POST",
        headers: body === undefined || body instanceof FormData ? { Accept: "application/json" } : { "Content-Type": "application/json", Accept: "application/json" },
        body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
        cache: "no-store",
      });
    } catch {
      throw new Error("无法连接本地服务。请确认川陀服务仍在运行，然后重试。");
    }
    if (!response.ok) {
      let detail = `请求失败（HTTP ${response.status}）`;
      try {
        const data = await response.json();
        if (typeof data.detail === "string") detail = data.detail;
        else if (data.detail) detail = pretty(data.detail);
      } catch { /* A non-JSON error still has an actionable HTTP status. */ }
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    if (asBlob) return response.blob();
    try { return await response.json(); }
    catch { throw new Error("本地服务返回了无法读取的 JSON。请检查服务日志。"); }
  }

  function readJson(id, label) {
    let value;
    try { value = JSON.parse($(id).value); }
    catch { throw new Error(`${label}不是有效 JSON，请检查引号、逗号和括号。`); }
    if (!isObject(value)) throw new Error(`${label}必须是 JSON 对象。`);
    return value;
  }

  function readScenario() {
    const scene = readJson("scenario-json", "场景");
    if (!Array.isArray(scene.entities) || !scene.entities.length) throw new Error("场景至少需要一个主体（entities）。");
    if (scene.entities.some((actor) => !isObject(actor) || typeof actor.id !== "string")) throw new Error("每个主体都需要字符串 id。");
    return scene;
  }

  function readConfig() {
    const config = readJson("config-json", "运行配置");
    if (!isObject(config.models) || !isObject(config.models.configs) || !isObject(config.models.layers)) {
      throw new Error("运行配置需要 models.configs 和 models.layers，以及 world、parser、report 的独立选择。");
    }
    return config;
  }

  function usedModels(config) {
    return [config.models.world, config.models.parser, config.models.report, ...Object.values(config.models.layers)];
  }

  function checkScheduleInputs() {
    for (const id of ["window-count", "round-count"]) {
      const value = Number($(id).value);
      if (!$(id).value || !Number.isInteger(value) || value < 1) throw new Error("窗口数和每窗口轮次必须为正整数，请修正当前表单后重试。");
    }
    if (!$("window-duration").value.trim()) throw new Error("请输入统一窗口长度后重试。");
  }

  function checkModelConfig(config, ids = usedModels(config)) {
    for (const [name, model] of Object.entries(config.models.configs)) {
      if (!isObject(model)) throw new Error(`模型配置 ${name} 必须是对象。`);
      if (Object.keys(model).some((key) => /^(api_?key|key|token|secret|authorization|password|headers)$/i.test(key))) {
        throw new Error(`模型配置 ${name} 含有不支持的凭据字段。请删除真实密钥，只填写 api_key_env 环境变量名。`);
      }
      if (model.api_key_env != null && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(model.api_key_env)) {
        throw new Error(`模型配置 ${name} 的 api_key_env 必须是环境变量名，不能填写真实密钥。`);
      }
    }
    for (const id of new Set(ids)) {
      if (typeof id !== "string" || !Object.hasOwn(config.models.configs, id)) throw new Error("有角色尚未选择有效模型。请检查世界、各层主体、解析和报告的独立配置。");
      const model = config.models.configs[id];
      if (!model.model || model.model === "REPLACE_WITH_MODEL_ID") throw new Error(`请先将 ${id} 的 model 改成你实际选择的模型 ID。`);
      if (model.api_key_env === "REPLACE_WITH_API_KEY_ENV") throw new Error(`请先将 ${id} 的 api_key_env 改成已在服务环境中设置的变量名。`);
      if (model.base_url === "https://api.example.com/v1") throw new Error(`请先将 ${id} 的 base_url 改成实际接口地址。`);
    }
  }

  function roleModel(config, role) {
    checkModelConfig(config, [config.models[role]]);
    return config.models.configs[config.models[role]];
  }

  function depthsFor(scene) {
    const actors = new Map(list(scene?.entities).map((actor) => [actor.id, actor]));
    const systems = new Map(list(scene?.subsystems).map((system) => [system.id, system]));
    const depths = new Map();
    function visit(id, path = new Set()) {
      if (depths.has(id)) return depths.get(id);
      if (path.has(id) || !actors.has(id)) return null;
      const actor = actors.get(id);
      let depth = 0;
      if (actor.subsystem_id != null) {
        const owner = systems.get(actor.subsystem_id)?.owner_id;
        const parentDepth = owner == null ? null : visit(owner, new Set([...path, id]));
        depth = parentDepth == null ? null : parentDepth + 1;
      }
      depths.set(id, depth);
      return depth;
    }
    for (const id of actors.keys()) visit(id);
    return Object.fromEntries(depths);
  }

  function modeFor(config) {
    if (!config?.models) return { label: "尚未配置", kind: "unknown" };
    const providers = usedModels(config).map((id) => config.models.configs[id]?.provider).filter(Boolean);
    if (!providers.length) return { label: "尚未配置", kind: "unknown" };
    const demoCount = providers.filter((provider) => provider === "demo").length;
    if (demoCount === providers.length) return { label: "离线演示", kind: "demo" };
    if (demoCount) return { label: "演示 / API 混合", kind: "mixed" };
    return { label: "真实 API", kind: "api" };
  }

  function modelDescription(config, role) {
    const id = config?.models?.[role];
    const model = config?.models?.configs?.[id];
    return model ? `${id} · ${model.provider} / ${model.model}` : "尚未指定";
  }

  function writeConfig(config) {
    $("config-json").value = pretty(config);
    syncConfigForm(config);
    definition?.configChanged();
  }

  function syncConfigForm(config) {
    $("window-count").value = config.windows ?? "";
    $("round-count").value = config.rounds_per_window ?? "";
    $("window-duration").value = config.window_duration ?? "";
    $("memory-policy").value = config.memory_policy || "full";
    $("world-sources").value = config.world_source_policy || "full";
    $("settlement-retries").value = String(config.settlement_retries || 0);
    $("roster-policy").value = config.roster_policy || "all";
    $("call-limit").value = config.model_call_limit ?? "";
    const modelKeys = Object.keys(config.models.configs);
    const depths = depthsFor(state.draft);
    const layers = [...new Set([...Object.keys(config.models.layers), ...Object.values(depths).filter((value) => value != null).map(String)])].sort((a, b) => Number(a) - Number(b));
    const roles = [
      { label: "世界模型", key: "world" },
      ...layers.map((depth) => ({ label: `L${depth} 主体模型`, key: depth, layer: true })),
      { label: "材料解析模型", key: "parser" },
      { label: "报告模型", key: "report" },
    ];
    const fragment = document.createDocumentFragment();
    roles.forEach((role, index) => {
      const wrapper = element("div");
      const label = element("label", "", role.label);
      const select = element("select");
      select.id = `model-role-${index}`;
      label.htmlFor = select.id;
      select.append(new Option("请选择模型", ""));
      for (const key of modelKeys) select.append(new Option(key, key));
      select.value = (role.layer ? config.models.layers[role.key] : config.models[role.key]) ?? "";
      select.addEventListener("change", () => {
        try {
          const updated = readConfig();
          if (role.layer) updated.models.layers[role.key] = select.value;
          else updated.models[role.key] = select.value;
          $("config-json").value = pretty(updated);
          updateModelNotes(updated);
          markDraftChanged();
          definition?.configChanged();
          clearError();
        } catch (error) { showError(error); }
      });
      wrapper.append(label, select);
      fragment.append(wrapper);
    });
    $("model-assignments").replaceChildren(fragment);
    $("model-count").textContent = `${modelKeys.length} 个配置`;
    updateModelNotes(config);
  }

  function updateModelNotes(config) {
    assistantDefinition?.syncConfig(config);
    const actors = state.draft?.entities?.length || 0, agentCalls = actors * config.rounds_per_window * config.windows;
    $("call-estimate").textContent = "按 " + actors + " 个主体计算，基础调度为主体 " + agentCalls + " 次、世界结算 " + config.windows + " 次。";
    const mode = modeFor(config);
    $("mode-badge").textContent = mode.label;
    $("mode-badge").dataset.mode = mode.kind;
    $("parser-note").textContent = config.models.configs[config.models.parser]?.provider === "demo"
      ? "当前为离线解析：返回固定样例，不根据输入材料作真实语义解析。"
      : `解析模型：${modelDescription(config, "parser")}。只生成草稿，不自动启动。`;
    $("report-model").textContent = `报告模型：${modelDescription(state.runConfig || config, "report")}`;
    $("launch-note").textContent = mode.kind === "demo" ? "当前为离线脚本演示，仅验证流程。点击启动后运行。" : "启动将使用上述显式模型配置；真实 API 可能产生调用费用。";
    if (!state.runId) $("runtime-mode").textContent = `${mode.label} · 尚未运行`;
  }

  function markDraftChanged() { $("draft-status").textContent = "草稿已修改"; }

  function writeScenario(scene, message = "草稿待审阅") {
    $("scenario-json").value = pretty(scene);
    state.draft = clone(scene);
    $("draft-status").textContent = message;
    renderDraftInfo();
    try { syncConfigForm(readConfig()); } catch { /* Configuration may still be loading or being edited. */ }
    if (!state.runId) renderOverview();
    updateControls();
    definition?.update(scene);
  }

  function renderDraftInfo() {
    const scene = state.draft;
    const missing = list(scene?.influences).filter((edge) => edge.weight == null).length;
    const facts = [`${list(scene?.entities).length} 个主体`, `${list(scene?.subsystems).length} 个子系统`, `${list(scene?.influences).length} 条影响边`];
    if (missing) facts.push(`${missing} 条待赋权`);
    $("draft-facts").replaceChildren(...facts.map((text) => element("span", "", text)));
    const assumptions = list(scene?.assumptions);
    const gaps = list(scene?.missing_information);
    $("review-notes").hidden = !(assumptions.length || gaps.length);
    $("review-summary").textContent = `${assumptions.length} 条假设 · ${gaps.length} 条缺失信息`;
    const items = element("ul");
    for (const item of assumptions) items.append(element("li", "", `假设：${item}`));
    for (const item of gaps) items.append(element("li", "", `待补充：${item}`));
    $("review-content").replaceChildren(items);
    $("preparation-note").textContent = state.preparation.length ? `已保留 ${state.preparation.length} 条解析 / 估权记录，启动时写入运行档案。` : "尚无解析或估权记录。";
    if (!state.runId) $("run-title").textContent = scene?.title || "未命名场景";
  }

  function activeScene() { return state.runScenario || state.draft; }
  function actorName(id) { return list(activeScene()?.entities).find((actor) => actor.id === id)?.name || id || "未知主体"; }
  function depthLabel(depth) { return depth == null ? "L?" : `L${depth}`; }
  function valueText(value) { return value === null ? "未提供" : value === undefined ? "未定义" : typeof value === "boolean" ? (value ? "true" : "false") : String(value); }
  function visibilityText(visibility) {
    if (visibility?.public) return "公开";
    const readers = list(visibility?.readers).length;
    return readers ? `本主体及 ${readers} 个指定主体` : "仅本主体";
  }

  function renderOverview() {
    const scene = activeScene();
    if (!scene) return;
    const depths = state.snapshot?.depths || depthsFor(scene);
    $("actor-count").textContent = list(scene.entities).length;
    $("layer-count").textContent = new Set(Object.values(depths).filter((depth) => depth != null)).size;
    $("edge-count").textContent = list(scene.influences).length;
    renderTree(scene, depths);
    renderEdges(scene);
    state.stateSignature = null;
    renderStates();
    syncEventFilters();
  }

  function renderTree(scene, depths) {
    const visited = new Set();
    const entities = list(scene.entities);
    const systems = list(scene.subsystems);
    function actorItem(actor) {
      const item = element("li");
      const row = element("div", "tree-actor");
      row.append(element("span", "level-tag", depthLabel(depths[actor.id])));
      const button = element("button", "text-button", actor.name || actor.id);
      button.type = "button";
      button.title = `查看 ${actor.name || actor.id} 的事件`;
      button.addEventListener("click", () => { $("actor-filter").value = actor.id; state.eventPage = 0; selectTab("events"); renderEvents(); });
      row.append(button);
      item.append(row);
      if (visited.has(actor.id)) { row.append(element("span", "muted small", "包含关系循环")); return item; }
      visited.add(actor.id);
      for (const system of systems.filter((entry) => entry.owner_id === actor.id)) {
        const group = element("ul");
        const branch = element("li");
        branch.append(element("div", "subsystem-label", system.name || system.id));
        const children = element("ul");
        for (const child of entities.filter((entry) => entry.subsystem_id === system.id)) children.append(actorItem(child));
        if (!children.childNodes.length) children.append(element("li", "muted small", "暂无显式主体"));
        branch.append(children);
        group.append(branch);
        item.append(group);
      }
      return item;
    }
    const root = element("ul", "tree-list");
    for (const actor of entities.filter((entry) => entry.subsystem_id == null)) root.append(actorItem(actor));
    for (const actor of entities.filter((entry) => !visited.has(entry.id))) {
      if (!visited.has(actor.id)) root.append(actorItem(actor));
    }
    $("containment-tree").replaceChildren(root);
  }

  function renderEdges(scene) {
    const fragment = document.createDocumentFragment();
    const provenanceLabels = { user: "用户设定", material: "材料依据", ai_assumption: "AI 假设", missing: "待补充" };
    for (const edge of list(scene.influences)) {
      const row = element("tr");
      const relation = element("td");
      relation.append(element("span", "edge-source", actorName(edge.source)), element("span", "edge-target", `→ ${actorName(edge.target)}`));
      const source = element("td", "", provenanceLabels[edge.provenance?.kind || "user"] || "未注明");
      source.title = edge.provenance?.explanation || "用户显式配置";
      row.append(relation, element("td", "", edge.weight == null ? "待估" : String(edge.weight)), source);
      fragment.append(row);
    }
    $("influence-edges").replaceChildren(fragment);
    $("edges-empty").hidden = list(scene.influences).length > 0;
  }

  function renderStates() {
    const scene = activeScene();
    if (!scene) return;
    const depths = state.snapshot?.depths || depthsFor(scene);
    const current = state.snapshot?.state || Object.fromEntries(list(scene.entities).map((actor) => [actor.id, Object.fromEntries(list(actor.fields).map((field) => [field.id, field.value]))]));
    const signature = JSON.stringify([current, depths]);
    $("state-caption").textContent = state.snapshot ? `已提交状态 · 窗口 ${state.snapshot.window}` : "草稿初始值";
    if (signature === state.stateSignature) return;
    state.stateSignature = signature;
    const fragment = document.createDocumentFragment();
    for (const actor of list(scene.entities)) {
      const card = element("article", "actor-card");
      const header = element("header");
      const identity = element("div");
      identity.append(element("h4", "", actor.name || actor.id), element("span", "actor-id", actor.id));
      header.append(identity, element("span", "level-tag", depthLabel(depths[actor.id])));
      card.append(header);
      const fields = element("dl", "field-list");
      for (const field of list(actor.fields)) {
        const row = element("div", "state-field");
        const term = element("dt", "", field.id);
        term.append(element("span", "field-metadata", `${field.domain || ""} · ${visibilityText(field.visibility)}`));
        const value = element("dd", "", valueText(current[actor.id]?.[field.id]));
        if (field.unit) value.append(element("span", "unit", field.unit));
        row.append(term, value);
        fields.append(row);
      }
      card.append(fields);
      if (!list(actor.fields).length) card.append(element("p", "muted small", "未定义状态字段"));
      fragment.append(card);
    }
    $("actor-states").replaceChildren(fragment);
  }

  function applySnapshot(snapshot) {
    const priorWindow = state.snapshot?.window;
    state.snapshot = snapshot;
    if (priorWindow !== snapshot.window && !$("panel-replay").hidden) studio?.loadReplay().catch(showError);
    const mode = modeFor(state.runConfig);
    $("run-status").textContent = STATUS[snapshot.status] || snapshot.status;
    $("run-status").dataset.state = snapshot.status;
    $("run-title").textContent = state.runScenario?.title || "未命名场景";
    $("run-id").textContent = `运行 ID：${snapshot.run_id}`;
    $("runtime-mode").textContent = `${mode.label} · 每窗口 ${snapshot.window_duration} · 主体与世界调用 ${snapshot.model_calls ?? "未知"}${snapshot.model_call_limit ? " / "+snapshot.model_call_limit : ""} 次`;
    $("progress-text").textContent = `已提交 ${snapshot.window} / ${snapshot.windows} 个窗口`;
    $("run-progress").max = Math.max(1, Number(snapshot.windows) || 1);
    $("run-progress").value = Number(snapshot.window) || 0;
    const currentWindow = Math.min((Number(snapshot.window) || 0) + 1, Number(snapshot.windows) || 1);
    $("round-text").textContent = snapshot.status === "running" ? `窗口 ${currentWindow} · 第 ${snapshot.round || 1} 轮` : snapshot.status === "ready" ? "等待调度" : STATUS[snapshot.status] || snapshot.status;
    $("boundary-note").textContent = snapshot.status === "running" && snapshot.stop_requested ? "结束请求已提交，等待当前窗口结算。" : snapshot.status === "running" && snapshot.pause_requested ? "暂停请求已提交，等待当前窗口结算。" : "暂停和提前结束在完整窗口边界生效。";
    $("run-error").hidden = !snapshot.error;
    $("run-error").textContent = snapshot.error || "";
    renderStates();
    updateControls();
  }

  function updateControls() {
    const status = state.snapshot?.status;
    const active = ACTIVE.has(status);
    $("draft-controls").disabled = state.busy || active;
    $("load-demo").disabled = state.busy || active;
    $("start-run").disabled = state.busy || active || !state.draft;
    $("pause-run").disabled = state.busy || status !== "running" || state.snapshot?.pause_requested || state.snapshot?.stop_requested;
    $("resume-run").disabled = state.busy || !state.snapshot?.can_resume || state.snapshot?.stop_requested;
    $("resume-run").textContent = ["failed","interrupted"].includes(status) ? "从已提交窗口重试" : "继续";
    $("stop-run").disabled = state.busy || !(active || ["interrupted","failed"].includes(status)) || state.snapshot?.stop_requested;
    $("refresh-run").disabled = state.busy || !state.runId;
    $("export-records").disabled = state.busy || !state.runId;
    $("save-project").disabled = $("project-copy").disabled = state.busy || !state.draft;
    $("ask-question").disabled = state.busy || !REPORTABLE.has(status);
    $("generate-report").disabled = $("report-estimate").disabled = state.busy || !REPORTABLE.has(status);
    $("download-report").disabled = state.busy || !state.report;
    definition?.lock(state.busy || active);
    assistantDefinition?.lock(state.busy || active);
  }

  async function perform(buttonId, action) {
    if (state.busy) return;
    const button = $(buttonId);
    const originalText = button.textContent;
    state.busy = true;
    button.textContent = "处理中…";
    clearError();
    updateControls();
    try { await action(); }
    catch (error) { showError(error); }
    finally { state.busy = false; button.textContent = originalText; updateControls(); }
  }

  function syncEventFilters() {
    const actorSelection = $("actor-filter").value;
    $("actor-filter").replaceChildren(new Option("全部主体", ""));
    for (const actor of list(activeScene()?.entities)) $("actor-filter").append(new Option(actor.name || actor.id, actor.id));
    $("actor-filter").value = actorSelection;
    if ($("actor-filter").selectedIndex < 0) $("actor-filter").value = "";
    const windowSelection = $("window-filter").value;
    const windows = new Set([0, ...state.events.map((event) => event.window)]);
    if (state.snapshot?.window) windows.add(state.snapshot.window);
    $("window-filter").replaceChildren(new Option("全部窗口", ""));
    for (const number of [...windows].filter(Number.isFinite).sort((a, b) => a - b)) $("window-filter").append(new Option(number === 0 ? "准备阶段 / 窗口 0" : `窗口 ${number}`, String(number)));
    $("window-filter").value = windowSelection;
    if ($("window-filter").selectedIndex < 0) $("window-filter").value = "";
  }

  function matchesActor(event, actorId) {
    if (!actorId) return true;
    const data = event.data || {};
    return data.actor_id === actorId || data.from === actorId || data.to === actorId || list(data.reviewed_actors).includes(actorId) || list(data.reviews).some((review) => review.entity_id === actorId);
  }

  function eventBody(event, card) {
    const data = event.data || {};
    const addText = (text) => { if (text) card.append(element("p", "event-body", text)); };
    if (TECHNICAL.has(event.kind)) {
      addText(`配置：${data.config_id || "详见记录"}。完整输入或输出仅在展开后显示。`);
    } else if (event.kind === "expression") {
      addText(data.text);
      card.append(element("p", "field-help", `第 ${data.round} 轮 · 主体私有表达 · ${data.message_count} 条定向消息`));
    } else if (event.kind === "message_delivered") {
      addText(data.text);
      card.append(element("p", "field-help", `仅投递给指定收件人 · 行动 ${data.action_id || ""}`));
    } else if (event.kind === "observation") {
      addText(`第 ${data.round || "?"} 轮：可见 ${data.field_count} 个字段、${data.material_count} 条材料；收到 ${data.message_count} 条消息。`);
    } else if (event.kind === "window_committed") {
      const actor = $("actor-filter").value;
      const changes = list(data.changes).filter((change) => !actor || change.entity_id === actor);
      addText(`本窗口已结算，${actor ? "所选主体有" : "共"} ${changes.length} 项字段变化。`);
      for (const change of changes.slice(0, 30)) {
        const item = element("div", "event-change");
        item.append(element("p", "", `${actorName(change.entity_id)} · ${change.field_id}：${valueText(change.before)} → ${valueText(change.after)}`), element("p", "", change.reason));
        if (list(change.action_ids).length) item.append(element("p", "", `依据：${change.action_ids.join("、")}`));
        card.append(item);
      }
      if (changes.length > 30) addText(`其余 ${changes.length - 30} 项变化请展开完整记录查看。`);
      for (const consequence of list(data.consequences)) addText(consequence.text);
      if (list(data.suggested_fields).length) addText(`建议补充字段（尚未新增）：${data.suggested_fields.join("；")}`);
    } else if (event.kind === "run_created") addText(`已创建场景「${data.scenario?.title || "未命名"}」。`);
    else if (event.kind === "run_started") addText("所有显式主体按轮次参与，完整窗口结束后统一结算。");
    else if (event.kind === "run_status") addText(`${STATUS[data.status] || data.status} · 已提交 ${data.window} 个窗口。`);
    else if (event.kind === "pause_requested") addText("已请求暂停，将在完整窗口边界生效。");
    else if (event.kind === "stop_requested") addText("已请求提前结束，将在完整窗口边界生效。");
    else if (event.kind === "scene_parse") addText("材料解析记录。模型输入与原始输出保留在详情中，解析结果需人工审阅。");
    else if (event.kind === "weight_estimation") addText(`缺失权重估算记录，${data.estimate_count} 条估计值。估计属于 AI 假设。`);
    else if (event.kind === "report_created") addText(data.report?.title || "已生成分析报告。");
    else if (event.kind === "run_error" || event.kind === "model_failure") addText(data.message || data.error || "模型调用失败，请查看详情及服务日志。");
    else addText("记录已保留，可展开查看详情。");
  }

  function renderEvents() {
    const actor = $("actor-filter").value;
    const windowValue = $("window-filter").value;
    const kind = $("kind-filter").value;
    const includeTechnical = $("include-technical").checked;
    const lifecycleKinds = new Set(["run_created", "run_started", "run_status", "pause_requested", "stop_requested", "run_error", "model_failure", "scene_parse", "weight_estimation", "report_created"]);
    const events = state.events.filter((event) => {
      if (!includeTechnical && TECHNICAL.has(event.kind)) return false;
      if (windowValue !== "" && String(event.window) !== windowValue) return false;
      if (kind === "lifecycle" && !lifecycleKinds.has(event.kind)) return false;
      if (kind && kind !== "lifecycle" && event.kind !== kind) return false;
      return matchesActor(event, actor);
    }).reverse();
    const pageCount = Math.max(1, Math.ceil(events.length / PAGE_SIZE));
    state.eventPage = Math.min(state.eventPage, pageCount - 1);
    const visible = events.slice(state.eventPage * PAGE_SIZE, (state.eventPage + 1) * PAGE_SIZE);
    const openIds = new Set([...$("event-list").querySelectorAll("details[open]")].map((details) => details.dataset.eventId));
    const fragment = document.createDocumentFragment();
    for (const event of visible) {
      const card = element("article", "event-card");
      card.id = `event-${event.id}`;
      const header = element("header");
      header.append(element("span", "event-kind", KINDS[event.kind] || event.kind));
      if (state.discardedIds.has(event.id) || event.commit_status==="discarded") header.append(element("span","tag attempt-tag","重试前的作废尝试"));
      else if (event.commit_status==="uncommitted" && event.window > (state.snapshot?.window || 0)) header.append(element("span","tag attempt-tag","窗口尚未提交"));
      if (event.data?.actor_id) header.append(element("span", "event-actor", actorName(event.data.actor_id)));
      if (event.kind === "message_delivered") header.append(element("span", "event-actor", `${actorName(event.data.from)} → ${actorName(event.data.to)}`));
      const time = new Date(event.recorded_at);
      const timeText = Number.isNaN(time.getTime()) ? "" : time.toLocaleTimeString("zh-CN", { hour12: false });
      header.append(element("span", "event-meta", `${event.id} · 窗口 ${event.window} · ${timeText}`));
      card.append(header);
      eventBody(event, card);
      const details = element("details", "event-details");
      details.dataset.eventId = event.id;
      details.append(element("summary", "", TECHNICAL.has(event.kind) ? "展开完整模型输入 / 输出" : "查看完整记录 JSON"));
      let loaded = false;
      const populate = async () => {
        if (loaded || !details.open) return;
        loaded = true;
        try {
          const full = await api("/api/runs/" + state.runId + "/events/" + event.id);
          details.append(element("pre", "", pretty(full)));
        } catch (error) { loaded = false; showError(error); }
      };
      details.addEventListener("toggle", populate);
      if (openIds.has(event.id)) { details.open = true; populate(); }
      card.append(details);
      fragment.append(card);
    }
    if (!visible.length) fragment.append(element("div", "empty-state spacious", state.events.length ? "没有匹配的事件，请调整主体、窗口或类型筛选。" : "启动推演后，表达、消息和状态变化会在这里出现。"));
    $("event-list").replaceChildren(fragment);
    $("event-total").textContent = state.events.length;
    $("event-summary").textContent = `${events.length} 条符合筛选 · 已读取 ${state.events.length} 条 · 最新在前`;
    $("event-pagination").hidden = pageCount <= 1;
    $("event-page-label").textContent = `第 ${state.eventPage + 1} / ${pageCount} 页`;
    $("events-newer").disabled = state.eventPage === 0;
    $("events-older").disabled = state.eventPage >= pageCount - 1;
  }

  function consumeBatch(snapshot,batch,channel) {
    if (batch.next_offset < state.offset) return;
    applySnapshot({...state.snapshot,...snapshot});
    let added=false;
    for(const event of list(batch.events)){
      if(state.eventIds.has(event.id))continue;
      if(event.kind==="run_recovered")for(const id of list(event.data.discarded_event_ids))state.discardedIds.add(id);
      state.eventIds.add(event.id); state.events.push(event); added=true;
    }
    state.offset=batch.next_offset; state.hasMore=Boolean(batch.has_more);
    if(added){
      $("event-total").textContent=state.events.length;
      if(!$("panel-events").hidden){syncEventFilters();renderEvents();}
    }
    const active=["ready","running"].includes(snapshot.status);
    state.settledPolls=state.hasMore||active?0:state.settledPolls+1;
    $("connection-status").textContent=channel+" · 已读取 "+state.offset+" / "+batch.total+" 条记录";
  }

  function closeStream() {
    state.stream?.close();state.stream=null;
  }

  function listen(runId) {
    const source=new EventSource("/api/runs/"+encodeURIComponent(runId)+"/stream?after="+state.offset);
    state.stream=source;
    const current=()=>source===state.stream && runId===state.runId;
    source.addEventListener("batch", event=>{
      if(!current())return;
      try{
        const batch=JSON.parse(event.data);consumeBatch(batch.snapshot,batch,"实时同步");state.streamErrors=0;
      }catch(error){closeStream();state.streamFailed=true;queuePoll();showError(error);}
    });
    source.addEventListener("idle",()=>{
      if(!current())return;
      closeStream();state.settledPolls=2;
      if (["ready","running"].includes(state.snapshot?.status)) queuePoll();
    });
    source.onerror=()=>{
      if(!current())return;
      state.streamErrors++;
      $("connection-status").textContent="实时连接中断，正在从已读取的记录继续。";
      if(state.streamErrors>=2){closeStream();state.streamFailed=true;state.settledPolls=0;queuePoll();}
    };
  }

  async function pollOnce() {
    if (state.polling) return state.polling;
    if (!state.runId) return;
    const runId = state.runId;
    const runPath = `/api/runs/${encodeURIComponent(runId)}`;
    state.polling = (async () => {
      const [snapshot, batch] = await Promise.all([api(runPath), api(`${runPath}/events?preview=true&after=${state.offset}`)]);
      if (state.runId !== runId) return;
      consumeBatch(snapshot,batch,"增量同步");
    })();
    try { await state.polling; }
    finally { state.polling = null; }
  }

  function queuePoll() {
    clearTimeout(state.pollTimer);
    if (!state.runId) return;
    if ("EventSource" in window && !state.streamFailed && (state.hasMore || ["ready","running"].includes(state.snapshot?.status))) {
      if(!state.stream)listen(state.runId);
      return;
    }
    state.pollTimer = setTimeout(async () => {
      try { await pollOnce(); }
      catch (error) { $("connection-status").textContent = `同步失败，将自动重试。${error.message}`; }
      if (["ready", "running"].includes(state.snapshot?.status) || state.settledPolls < 2) queuePoll();
    }, 900);
  }

  function selectTab(name, focus = false) {
    $("inquiry").hidden = name !== "report" || !state.runId;
    if (state.runId) {
      const url=new URL(location.href);url.searchParams.set("run",state.runId);url.searchParams.set("tab",name);
      if (name!=="replay"){url.searchParams.delete("window");url.searchParams.delete("actor");}
      url.searchParams.delete("event");history.replaceState(null,"",url);
    }
    if (name === "replay") studio?.loadReplay().catch(showError);
    if (name === "events") {syncEventFilters();renderEvents();}
    for (const tab of document.querySelectorAll('[role="tab"]')) {
      const selected = tab.id === `tab-${name}`;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $(tab.getAttribute("aria-controls")).hidden = !selected;
      if (selected && focus) tab.focus();
    }
  }

  function releaseUrl(key) {
    if (state.urls.has(key)) { URL.revokeObjectURL(state.urls.get(key)); state.urls.delete(key); }
  }

  function download(blob, filename, key) {
    releaseUrl(key);
    const url = URL.createObjectURL(blob);
    state.urls.set(key, url);
    const link = element("a");
    link.href = url;
    link.download = filename;
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
  }

  function bindAction(id, action) { $(id).addEventListener("click", () => perform(id, action)); }

  async function loadDemo(name="harbor") {
    const data = await api("/api/examples/"+name);
    definition?.archive();
    studio?.setProject(null);
    state.preparation = [];
    writeConfig(data.config);
    writeScenario(data.scenario, "演示草稿");
    $("review-notes").open = false;
    notice("已加载离线演示草稿。可直接点击“启动推演”验证流程，或编辑场景与模型配置。尚未启动运行。");
  }

  async function chooseExample(name) {
    if (ACTIVE.has(state.snapshot?.status)) throw new Error("请先暂停或结束当前推演，或从项目资料库打开另一份草稿。");
    await loadDemo(name); definition.show("definition"); assistantDefinition.show("expert"); definition.showStep("review");
    notice("已载入「"+state.draft.title+"」。核对窗口数后即可启动离线推演。");
  }
  bindAction("load-demo", () => chooseExample("harbor"));
  for (const name of ["harbor","release"]) bindAction("example-"+name,()=>chooseExample(name));
  bindAction("format-scenario", async () => { writeScenario(readScenario()); notice("场景 JSON 已格式化。"); });
  bindAction("format-config", async () => { writeConfig(readConfig()); markDraftChanged(); notice("运行配置 JSON 已格式化，表单已同步。"); });
  bindAction("validate-draft", async () => {
    const scenario = readScenario();
    const config = readConfig();
    checkScheduleInputs();
    checkModelConfig(config);
    const result = await api("/api/validate", { scenario, config });
    if (!result.valid) throw new Error("配置未通过校验。");
    state.draft = scenario;
    renderDraftInfo();
    if (!state.runId) renderOverview();
    $("draft-status").textContent = "验证通过";
    notice(`配置验证通过。${list(result.warnings).length ? `\n${result.warnings.join("\n")}` : "可审阅后启动推演。"}`);
  });
  bindAction("parse-material", async () => {
    const input = definition?.parseInput() || { text: $("material-text").value };
    if (!input.text.trim()) throw new Error("请先补充场景背景或导入材料。");
    const config = readConfig();
    const result = await api("/api/parse", { ...input, model: roleModel(config, "parser") });
    definition?.archive();
    if (result.record) state.preparation.push(result.record);
    writeScenario(result.scenario, "解析草稿待审阅");
    $("review-notes").open = true;
    definition?.showStep("actors");
    notice("解析完成，已更新场景草稿。请审阅假设、缺失信息、主体字段和影响权重，并为所有层级选择模型；确认后点击启动。尚未运行。");
  });
  bindAction("estimate-weights", async () => {
    const scenario = readScenario();
    const config = readConfig();
    if (!list(scenario.influences).some((edge) => edge.weight == null)) { notice("所有影响边均已有权重，无需估算。"); return; }
    const result = await api("/api/estimate", { scenario, model: roleModel(config, "world") });
    if (result.record) state.preparation.push(result.record);
    writeScenario(result.scenario, "估权结果待审阅");
    definition?.showStep("relations");
    notice("缺失权重已由当前世界模型估算。请在影响关系中审阅 AI 假设与逐边理由。尚未启动运行。");
  });
  bindAction("add-api-example", async () => {
    const config = readConfig();
    const provider = $("api-provider").value;
    const bases = { openai: "https://api.openai.com/v1", anthropic: "https://api.anthropic.com/v1", deepseek: "https://api.deepseek.com", custom: "https://api.example.com/v1" };
    let id = `api_${provider}`;
    let suffix = 2;
    while (Object.hasOwn(config.models.configs, id)) id = `api_${provider}_${suffix++}`;
    config.models.configs[id] = { provider, model: "REPLACE_WITH_MODEL_ID", base_url: bases[provider], api_key_env: "REPLACE_WITH_API_KEY_ENV", timeout_seconds: 60, max_tokens: 4096, context_limit_chars: 120000 };
    if (provider === "deepseek") Object.assign(config.models.configs[id], { json_output: true, max_retries: 3 });
    writeConfig(config);
    markDraftChanged();
    notice(`已添加 ${id}，保留所有现有模型选择。请在 JSON 中修改 model 和 api_key_env${provider === "custom" ? "、base_url" : ""}，再在所需角色的下拉框中选择此配置。`);
  });
  bindAction("start-run", async () => {
    const scenario = readScenario();
    const config = readConfig();
    checkScheduleInputs();
    checkModelConfig(config);
    const validation = await api("/api/validate", { scenario, config });
    if (!validation.valid) throw new Error("配置未通过校验，尚未创建运行。");
    if (validation.preflight?.missing_credentials.length || validation.preflight?.blocking_context.length) throw new Error(validation.warnings.join("\n"));
    const link = await studio.saveProject();
    const snapshot = await studio.job("launch", id => "/api/jobs/"+id+"/launch",
      { scenario, config, preparation_records: clone(state.preparation), ...link });
    const created = await api("/api/runs/"+snapshot.run_id+"/events/e000001");
    await displayRun(snapshot,created.data.scenario,created.data.config);
    studio.clearJob("launch");
    notice("推演已创建 " + snapshot.run_id + "。可通过项目与历史重新打开。");
  });

  async function displayRun(snapshot,scenario,config) {
    clearTimeout(state.pollTimer);
    closeStream();state.streamErrors=0;state.streamFailed=false;state.hasMore=false;
    if (state.polling) await state.polling.catch(() => {});
    state.runId = snapshot.run_id;
    state.runScenario = clone(scenario); state.runConfig = clone(config);
    state.events = []; state.eventIds = new Set(); state.discardedIds = new Set(); state.offset = 0; state.eventPage = 0;
    state.settledPolls = 0; state.stateSignature = null;
    state.report = null; state.reportWindow = null; releaseUrl("report");
    $("report-markdown").textContent = ""; $("report-record").textContent = ""; $("report-empty").hidden = false;
    $("report-note").textContent = "报告保留生成时的窗口范围。长记录会分批调用报告模型整理，点击证据编号可核对原文。";
    $("actor-filter").value = $("window-filter").value = "";
    studio.resetRun(scenario);
    applySnapshot(snapshot); renderOverview(); renderEvents(); definition.show("observer");
    updateModelNotes(config); selectTab("overview");
    const url = new URL(location.href); url.searchParams.set("run",snapshot.run_id); history.replaceState(null,"",url);
    await Promise.all([pollOnce(), studio.reportList(snapshot.run_id,true), studio.answers(snapshot.run_id)]);
    queuePoll();
  }
  async function openRun(id) {
    const params = new URL(location.href).searchParams;
    const restoreView = params.get("run") === id;
    const path = "/api/runs/" + encodeURIComponent(id);
    const [snapshot,created] = await Promise.all([api(path),api(path+"/events/e000001")]);
    await displayRun(snapshot,created.data.scenario,created.data.config);
    if (restoreView && ["overview","replay","events","report"].includes(params.get("tab"))) {
      selectTab(params.get("tab"));
      if (params.get("tab")==="replay") await studio.loadReplay({window:params.get("window"),actor:params.get("actor")});
    }
    if (restoreView && params.has("event")) await studio.showEvidence(params.get("event"));
    await restoreAnalysisTasks();
  }
  for (const command of ["pause", "resume", "stop"]) {
    bindAction(`${command}-run`, async () => {
      const recovery = command === "resume" && ["failed","interrupted"].includes(state.snapshot?.status);
      const snapshot = await api(`/api/runs/${encodeURIComponent(state.runId)}/${recovery ? "recover" : command}`, {});
      applySnapshot(snapshot);
      state.settledPolls = 0;
      if (command === "resume" && state.report) $("report-note").textContent = `下方报告对应窗口 ${state.reportWindow} 的已有记录；继续运行后可再次生成报告。`;
      notice(command === "pause" ? "暂停请求已提交，将在完整窗口边界生效。" : command === "stop" ? "提前结束请求已提交，将在完整窗口边界生效。" : "已继续推演。");
      queuePoll();
      await pollOnce();
    });
  }
  bindAction("refresh-run", async () => { await pollOnce(); queuePoll(); notice("当前状态与事件记录已同步。"); });
  async function estimateReport() {
    const plan = await api("/api/runs/"+state.runId+"/report-plan?reuse="+$("report-reuse").checked);
    $("report-plan").textContent = "截至窗口 "+plan.cutoff_window+"，"+plan.evidence_count+" 条证据，最低需新调用 "+plan.minimum_new_calls+" 次。首层 "+plan.first_level_batches+" 批，可复用 "+plan.reusable_first_level+" 批。进一步归并、格式修正和传输重试可能增加用量。";
    return plan;
  }
  bindAction("report-estimate",estimateReport);
  bindAction("generate-report", async () => {
    const id = state.runId, scope = "report." + id;
    const capText=$("report-budget").value, cap=capText===""?null:Number(capText);
    if(cap!==null && (!Number.isInteger(cap)||cap<1))throw new Error("报告调用上限须为正整数，留空表示不设上限。");
    const plan=await estimateReport();
    if(!studio.pending(scope) && cap!==null && plan.minimum_new_calls>cap){
      $("report-budget").closest("details").open=true;
      throw new Error("本次报告至少需新调用 "+plan.minimum_new_calls+" 次，高于上限 "+cap+" 次，请核对预算。");
    }
    const result = await studio.job(scope, jobId => "/api/runs/" + id + "/jobs/" + jobId + "/report",
      {max_calls:cap,reuse:$("report-reuse").checked,expected_window:plan.cutoff_window},{},message=>{$("report-note").textContent=message;});
    if (id === state.runId) { displayReport(result); await studio.reportList(id,false); selectTab("report"); }
    studio.clearJob(scope);
    notice("报告已保存，可点击引用或下载 Markdown。");
    await pollOnce();
  });
  function displayReport(result) {
    state.report = result.markdown; state.reportWindow = result.cutoff_window;
    releaseUrl("report");
    $("report-markdown").textContent = result.markdown;
    $("report-markdown").hidden = false; $("report-empty").hidden = true;
    $("report-record").textContent = "记录 " + result.record_id + " · 窗口 " + result.cutoff_window;
    if(result.coverage) $("report-note").textContent="证据 "+result.coverage.evidence_count+" 条 · 本次新调用 "+(result.coverage.new_model_calls??"未记录")+" 次 · 复用 "+(result.coverage.reused_batches||0)+" 批。";
    studio.renderReport(result); updateControls();
  }
  bindAction("ask-question", async () => {
    const question = $("ask-text").value.trim();
    if (!question) throw new Error("请输入要向记录或主体提出的问题。");
    const id = state.runId, scope = "question." + id;
    await studio.job(scope, jobId => "/api/runs/" + id + "/jobs/" + jobId + "/question",
      {text:question, actor_id:$("ask-actor").value || null});
    if (id === state.runId) { await studio.answers(id); $("ask-text").value = ""; }
    studio.clearJob(scope); await pollOnce();
  });
  async function restoreAnalysisTasks() {
    const id = state.runId;
    for (const kind of ["report","question"]) {
      const scope = kind+"."+id;
      if (!studio.pending(scope)) continue;
      const result = await studio.resumeJob(scope);
      if (id !== state.runId) return;
      if (kind === "report") { displayReport(result); await studio.reportList(id,false); }
      else await studio.answers(id);
      studio.clearJob(scope); selectTab("report");
    }
  }
  bindAction("download-report", async () => {
    download(new Blob([state.report], { type: "text/markdown;charset=utf-8" }), `chuantuo-${state.runId}-report.md`, "report");
  });
  bindAction("export-records", async () => {
    const blob = await api(`/api/runs/${encodeURIComponent(state.runId)}/export`, undefined, true);
    download(blob, `chuantuo-${state.runId}-events.jsonl`, "records");
    notice("已触发完整记录下载，包含模型输入输出与操作者可回看的私有信息。");
  });

  $("material-file").addEventListener("change", async () => {
    const file = $("material-file").files?.[0];
    if (!file) return;
    await perform("parse-material", async () => {
      if (!/\.(txt|md)$/i.test(file.name)) throw new Error("请选择 .txt 或 .md 文件。");
      $("material-text").value = await file.text();
      $("material-section").open = true;
      notice(`已读取 ${file.name}，可编辑材料后点击解析。尚未调用模型。`);
    });
    $("material-file").value = "";
  });

  for (const [id, key, numeric] of [["window-count", "windows", true], ["round-count", "rounds_per_window", true], ["window-duration", "window_duration", false]]) {
    $(id).addEventListener("change", () => {
      try {
        const config = readConfig();
        const value = numeric ? Number($(id).value) : $(id).value.trim();
        if (numeric ? !Number.isInteger(value) || value < 1 : !value) throw new Error(numeric ? "窗口数和每窗口轮次必须为正整数。" : "请输入统一窗口长度。");
        config[key] = value;
        $("config-json").value = pretty(config);
        markDraftChanged();
        updateModelNotes(config);
        definition?.configChanged();
        clearError();
      } catch (error) { showError(error); }
    });
  }

  for (const id of ["memory-policy","call-limit","world-sources","settlement-retries","roster-policy"]) $(id).addEventListener("change", () => {
    try {
      const config = readConfig(), limit = $("call-limit").value;
      if (limit && (!Number.isInteger(Number(limit)) || Number(limit) < 1)) throw new Error("调用上限须为正整数或留空。");
      config.model_call_limit = limit ? Number(limit) : null; config.memory_policy = $("memory-policy").value; config.world_source_policy = $("world-sources").value;
      config.settlement_retries = Number($("settlement-retries").value);
      config.roster_policy = $("roster-policy").value;
      writeConfig(config); markDraftChanged();
    } catch (error) { showError(error); }
  });

  let scenarioEditTimer;
  $("scenario-json").addEventListener("input", () => {
    markDraftChanged();
    clearTimeout(scenarioEditTimer);
    scenarioEditTimer = setTimeout(() => {
      try {
        state.draft = readScenario();
        renderDraftInfo();
        syncConfigForm(readConfig());
        if (!state.runId) renderOverview();
        definition?.update(state.draft);
      } catch { $("draft-status").textContent = "草稿编辑中，待验证"; }
    }, 350);
  });
  let configEditTimer;
  $("config-json").addEventListener("input", () => {
    markDraftChanged();
    clearTimeout(configEditTimer);
    configEditTimer = setTimeout(() => {
      try { syncConfigForm(readConfig()); definition?.configChanged(); }
      catch { $("draft-status").textContent = "配置编辑中，待验证"; }
    }, 350);
  });

  for (const id of ["actor-filter", "window-filter", "kind-filter", "include-technical"]) $(id).addEventListener("change", () => { state.eventPage = 0; renderEvents(); });
  $("events-newer").addEventListener("click", () => { state.eventPage = Math.max(0, state.eventPage - 1); renderEvents(); });
  $("events-older").addEventListener("click", () => { state.eventPage += 1; renderEvents(); });
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(tab.id.slice(4)));
    tab.addEventListener("keydown", (event) => {
      let target;
      if (event.key === "ArrowRight") target = (index + 1) % tabs.length;
      if (event.key === "ArrowLeft") target = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") target = 0;
      if (event.key === "End") target = tabs.length - 1;
      if (target !== undefined) { event.preventDefault(); selectTab(tabs[target].id.slice(4), true); }
    });
  });
  window.addEventListener("beforeunload", () => { closeStream(); clearTimeout(state.pollTimer); for (const url of state.urls.values()) URL.revokeObjectURL(url); });
  function importDefinition(data) {
    if (data.config) {
      checkModelConfig(data.config, []);
      writeConfig(data.config);
    }
    state.preparation = list(data.preparation_records).filter(record => ["scene_parse", "weight_estimation"].includes(record.kind));
    writeScenario(data.scenario, "草稿待审阅");
  }
  definition = window.ChuantuoDefinition.create({
    write: writeScenario, showSource:(source,quote)=>studio.showSource(source,quote), config: readConfig, error: showError, clearError, notice,
    bundle: () => {
      const config = readConfig();
      checkModelConfig(config, []);
      return { version: 1, scenario: readJson("scenario-json", "场景"), config, preparation_records: clone(state.preparation) };
    },
    importDraft: importDefinition,
    download: data => download(new Blob([pretty(data)], { type: "application/json;charset=utf-8" }), "chuantuo-scenario.json", "definition"),
  });
  assistantDefinition = window.ChuantuoAssistantDefinition.create({
    api, perform, notice, showSource:(source,quote)=>studio.showSource(source,quote), error: showError, clearError, config: readConfig, writeConfig, model: roleModel,
    bundle: () => ({scenario: readJson("scenario-json", "场景"), preparation_records: clone(state.preparation)}),
    accept: (scenario, records) => { definition.archive(); importDefinition({scenario, preparation_records: records}); },
    review: () => definition.showStep("review"),
    definitionJob: (payload,refine,progress) => studio.job("definition", id => "/api/jobs/"+id+"/definition", payload,{refine},progress),
    jobApplied: () => studio.clearJob("definition"),
    materialJob:(form,progress)=>studio.job("material", id=>"/api/jobs/"+id+"/material",form,{},progress),
    materialApplied:()=>studio.clearJob("material"),
  });
  studio = window.ChuantuoStudio.create({
    api, notice, onError:showError, runId:()=>state.runId, sourceOf:id=>state.runScenario?.sources?.find(s=>s.id===id), scene:()=>state.runScenario, actorName, show:definition.show, openRun, displayReport,
    bundle:()=>({scenario:readScenario(), config:readConfig(), preparation_records:clone(state.preparation), assistant:assistantDefinition.snapshot()}),
    restoreTask:(kind,result,context)=>{
      if (ACTIVE.has(state.snapshot?.status)) throw new Error("当前推演正在占用编辑区，请先打开其他草稿或结束观察。");
      if (kind==="material") assistantDefinition.applyMaterial(result,false);
      else {
        const sources=result.scenario.sources.filter(s=>s.name!=="用户目标与补充回答");
        assistantDefinition.restore({goal:context.goal || result.scenario.title,...result.request_context,sources,proposal:result,records:result.records});
      }
      definition.show("definition");assistantDefinition.show("guided");notice("已找回任务结果，请审阅当前草案。");
    },
    importProject: payload => {
      clearTimeout(state.pollTimer); closeStream();state.runId=null; state.snapshot=null; state.runScenario=null; state.runConfig=null;
      state.events=[]; state.eventIds=new Set(); state.report=null; state.stateSignature=null; state.settledPolls=2;
      const url = new URL(location.href); url.searchParams.delete("run"); history.replaceState(null,"",url);
      definition.archive(); importDefinition(payload); assistantDefinition.restore(payload.assistant);
      definition.show("definition"); assistantDefinition.show("expert"); definition.showStep("review"); updateControls();
    },
  });
  bindAction("save-project", () => studio.saveProject());
  bindAction("project-copy", async () => { await studio.saveProject(true); await studio.library(); });
  perform("load-demo", async () => {
    const saved = definition.initialDraft;
    if (saved?.scenario?.entities && saved?.config?.models) {
      try {
        importDefinition(saved);
        notice("已恢复当前浏览器保存的场景草稿。尚未启动推演。");
      } catch { await loadDemo(); }
    } else await loadDemo();
    const launch = studio.pending("launch");
    const requestedProject = new URL(location.href).searchParams.get("project");
    if (requestedProject && !launch) await studio.openProject(requestedProject);
    const run = launch ? (await studio.resumeJob("launch")).run_id : new URL(location.href).searchParams.get("run");
    if (run) await openRun(run);
    if (launch) studio.clearJob("launch");
    if (studio.pending("material")) {
      const progress = $("assistant-progress"); progress.hidden=false;
      try { assistantDefinition.applyMaterial(await studio.resumeJob("material", msg=>{progress.textContent=msg;})); }
      finally { progress.hidden=true; }
    }
    const pending = studio.pending("definition");
    if (pending) {
      const progress = $("assistant-progress"); progress.hidden=false;
      try {
        const result=await studio.resumeJob("definition", stage => {progress.textContent=stage;});
        if(pending.fromHistory){studio.restoreTask("definition",result,pending);studio.clearJob("definition");}
        else assistantDefinition.applyResult(result,pending.refine);
      } finally { progress.hidden=true; }
    }
  });
})();
