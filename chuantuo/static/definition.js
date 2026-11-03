/* Scenario definition UI. Uses the existing Scenario contract and engine. */
(() => {
  "use strict";
  const copy = value => JSON.parse(JSON.stringify(value));
  const list = value => Array.isArray(value) ? value : [];
  const userSource = () => ({ kind: "user", source_ids: [], explanation: "用户显式配置" });
  const identifier = value => /^[\p{L}\p{N}_.-]+$/u.test(value);
  const uniqueId = (items, prefix) => {
    let n = 1;
    while (items.some(item => item.id === `${prefix}_${n}`)) n++;
    return `${prefix}_${n}`;
  };
  function normalize(scene) {
    const result = copy(scene);
    for (const key of ["domains", "entities", "subsystems", "influences", "sources", "rules", "assumptions", "missing_information"]) result[key] ??= [];
    for (const actor of result.entities) { actor.fields ??= []; actor.materials ??= []; }
    return result;
  }
  function depths(scene) {
    const actors = new Map(scene.entities.map(actor => [actor.id, actor]));
    const systems = new Map(scene.subsystems.map(system => [system.id, system]));
    const result = Object.create(null);
    function visit(id, path = new Set()) {
      if (Object.hasOwn(result, id)) return result[id];
      if (path.has(id)) throw new Error("此归属会形成包含循环，请选择其他子系统。");
      const actor = actors.get(id);
      if (!actor) throw new Error("包含结构引用了不存在的主体。");
      if (actor.subsystem_id == null) return result[id] = 0;
      const system = systems.get(actor.subsystem_id);
      if (!system) throw new Error(`找不到主体 ${actor.name || id} 所属的子系统。`);
      return result[id] = visit(system.owner_id, new Set([...path, id])) + 1;
    }
    for (const actor of actors.values()) visit(actor.id);
    return result;
  }
  function renameActor(scene, oldId, newId) {
    if (!identifier(newId)) throw new Error("主体 ID 仅支持字母、数字、汉字、下划线、点与短横线。");
    if (scene.entities.some(actor => actor.id === newId && actor.id !== oldId)) throw new Error("主体 ID 已存在。");
    const next = normalize(scene);
    next.entities.find(actor => actor.id === oldId).id = newId;
    for (const system of next.subsystems) if (system.owner_id === oldId) system.owner_id = newId;
    for (const edge of next.influences) {
      if (edge.source === oldId) edge.source = newId;
      if (edge.target === oldId) edge.target = newId;
    }
    for (const actor of next.entities) for (const item of [...actor.fields, ...actor.materials]) {
      if (item.visibility?.readers) item.visibility.readers = item.visibility.readers.map(id => id === oldId ? newId : id);
    }
    return next;
  }
  function renameDomain(scene, oldName, newName) {
    newName = newName.trim();
    if (!newName || (oldName !== newName && scene.domains.includes(newName))) throw new Error("领域名称不能为空或重复。");
    const next = normalize(scene);
    next.domains = next.domains.map(name => name === oldName ? newName : name);
    for (const actor of next.entities) for (const item of [...actor.fields, ...actor.materials]) if (item.domain === oldName) item.domain = newName;
    return next;
  }
  function checks(scene, config) {
    const items = [];
    let layerDepths = {};
    const add = (kind, text, step) => items.push({kind, text, step});
    if (!scene.title?.trim() || !scene.background?.trim()) add("warning", "补充场景名称、背景与推演目标。", "materials");
    if (!scene.entities.length) add("error", "至少定义一个显式主体。", "actors");
    if (new Set(scene.entities.map(a => a.id)).size !== scene.entities.length) add("error", "主体 ID 重复。", "actors");
    try { layerDepths = depths(scene); }
    catch (error) { add("error", error.message, "actors"); }
    const actorIds = new Set(scene.entities.map(a => a.id));
    const sourceIds = new Set(scene.sources.map(s => s.id));
    const provenanceObjects = [...scene.entities, ...scene.influences, ...scene.rules];
    for (const actor of scene.entities) {
      if (!actor.name?.trim() || !actor.identity?.trim()) add("warning", `${actor.name || actor.id}：补充名称与身份。`, "actors");
      if (!identifier(actor.id)) add("error", `无效主体 ID：${actor.id}`, "actors");
      if (new Set(actor.fields.map(f => f.id)).size !== actor.fields.length) add("error", `${actor.name}：字段 ID 重复。`, "actors");
      for (const item of [...actor.fields, ...actor.materials]) {
        provenanceObjects.push(item);
        if (!scene.domains.includes(item.domain)) add("error", `${actor.name}：领域 ${item.domain} 未定义。`, "actors");
        if (list(item.visibility?.readers).some(id => !actorIds.has(id))) add("error", `${actor.name}：可见范围引用未知主体。`, "actors");
      }
    }
    for (const object of provenanceObjects) if (list(object.provenance?.source_ids).some(id => !sourceIds.has(id))) add("error", "有定义引用了不存在的来源材料。", "materials");
    const pairs = new Set();
    for (const edge of scene.influences) {
      const key = JSON.stringify([edge.source, edge.target]);
      if (pairs.has(key)) add("error", `重复关系：${edge.source} → ${edge.target}`, "relations");
      pairs.add(key);
      if (!actorIds.has(edge.source) || !actorIds.has(edge.target)) add("error", "关系引用了不存在的主体。", "relations");
      if (edge.weight == null) add("error", `${edge.source} → ${edge.target}：填写权重后才能启动。`, "relations");
      else if (!Number.isFinite(edge.weight) || edge.weight < 0 || edge.weight > 1) add("error", "影响权重必须位于 0–1。", "relations");
    }
    for (const depth of new Set(Object.values(layerDepths))) if (!config?.models?.layers?.[depth]) add("error", `L${depth} 尚未指定主体模型。`, "review");
    if (scene.assumptions.length) add("warning", `${scene.assumptions.length} 条假设需要审阅。`, "review");
    if (scene.missing_information.length) add("warning", `${scene.missing_information.length} 条缺失信息需要审阅。`, "review");
    return items;
  }

  function create(bridge) {
    const $ = id => document.getElementById(id);
    const steps = ["materials", "actors", "relations", "review"];
    const labels = ["材料与目标", "主体与层级", "影响关系", "开局检查"];
    const draftStorageSlot = "chuantuo.definition.v1";
    let scene = null, selectedActor = null, selectedEdge = 0, step = "materials", ownUpdate = false, locked = false, zoom = 1, formId = 0;
    let initialDraft, previousDraft;
    try { initialDraft = JSON.parse(localStorage.getItem(draftStorageSlot) || "null"); } catch { initialDraft = null; }
    try { previousDraft = JSON.parse(localStorage.getItem(draftStorageSlot + ".previous") || "null"); } catch { previousDraft = null; }
    const el = (tag, cls, text) => {
      const node = document.createElement(tag);
      if (cls) node.className = cls;
      if (text != null) node.textContent = text;
      return node;
    };
    const help = text => el("p", "field-help", text);
    function button(text, action, cls = "text-button") {
      const node = el("button", cls, text); node.type = "button";
      node.addEventListener("click", () => attempt(action));
      return node;
    }
    function attempt(action) { try { action(); } catch (error) { bridge.error(error); } }
    function input(label, value, change, options = {}) {
      const wrapper = el("div", "editor-field");
      const control = el(options.rows ? "textarea" : "input");
      control.id = `definition-control-${++formId}`;
      const caption = el("label", "", label); caption.htmlFor = control.id;
      control.value = value ?? "";
      if (options.rows) control.rows = options.rows;
      else control.type = options.type || "text";
      for (const key of ["min", "max", "step", "placeholder", "readOnly"]) if (options[key] !== undefined) control[key] = options[key];
      control.addEventListener("change", () => attempt(() => change(control.value)));
      wrapper.append(caption, control);
      return wrapper;
    }
    function select(label, value, choices, change) {
      const wrapper = el("div", "editor-field");
      const control = el("select"); control.id = `definition-control-${++formId}`;
      const caption = el("label", "", label); caption.htmlFor = control.id;
      for (const [id, name] of choices) control.append(new Option(name, id));
      control.value = value ?? "";
      control.addEventListener("change", () => attempt(() => change(control.value)));
      wrapper.append(caption, control); return wrapper;
    }
    function checkbox(label, value, change) {
      const wrapper = el("label", "checkbox-label"); const control = el("input");
      control.type = "checkbox"; control.checked = Boolean(value);
      control.addEventListener("change", () => attempt(() => change(control.checked)));
      wrapper.append(control, document.createTextNode(label)); return wrapper;
    }
    const row = (...children) => { const node = el("div", "editor-row"); node.append(...children); return node; };
    const actorChoices = () => scene.entities.map(actor => [actor.id, actor.name || actor.id]);
    const actorName = id => scene.entities.find(actor => actor.id === id)?.name || id;
    const actor = () => scene.entities.find(item => item.id === selectedActor);
    function persist() {
      if (!scene) return;
      try {
        localStorage.setItem(draftStorageSlot, JSON.stringify(bridge.bundle()));
        $("definition-save-status").textContent = "草稿已保存在当前浏览器 · 保存场景可下载完整定义";
      } catch {
        $("definition-save-status").textContent = "浏览器未能保存草稿，请用“保存场景”下载文件。";
      }
    }
    function archive() {
      if (!scene) return;
      previousDraft = bridge.bundle();
      $("definition-undo").hidden = false;
      try { localStorage.setItem(draftStorageSlot + ".previous", JSON.stringify(previousDraft)); } catch { /* The in-session recovery remains available. */ }
    }
    function change(update, redraw = false) {
      if (locked) return;
      const before = copy(scene);
      try {
        update(scene); depths(scene);
        ownUpdate = true; bridge.write(scene); ownUpdate = false;
        bridge.clearError();
        persist(); renderSummary();
        if (redraw) render();
      } catch (error) { ownUpdate = false; scene = before; render(); throw error; }
    }
    function provenance(item) {
      const box = el("details", "definition-provenance");
      const names = {user:"用户设定",material:"材料依据",ai_assumption:"AI 假设",missing:"待补充"};
      const current = item.provenance || userSource();
      box.append(el("summary", "", `来源 · ${names[current.kind]}`));
      const edit = fn => change(() => { item.provenance ??= userSource(); fn(item.provenance); });
      box.append(select("依据类型", current.kind, Object.entries(names), value => { edit(p => p.kind = value); render(); }));
      if (scene.sources.length) {
        box.append(help("关联原始材料"));
        for (const source of scene.sources) box.append(checkbox(source.name, list(current.source_ids).includes(source.id), checked => edit(p => {
          p.source_ids = checked ? [...new Set([...p.source_ids, source.id])] : p.source_ids.filter(id => id !== source.id);
          if (!checked && p.quotes) p.quotes = p.quotes.filter(q => q.source_id !== source.id);
        })));
      }
      for (const quote of list(current.quotes)) {
        const source = scene.sources.find(s => s.id === quote.source_id);
        if (source) box.append(button("核对引文 · " + source.name + (quote.page ? " 第 " + quote.page + " 页" : ""), () => bridge.showSource(source,quote)));
      }
      box.append(input("依据说明", current.explanation, value => edit(p => p.explanation = value), {rows:2}));
      return box;
    }
    function constraints(field) {
      const box=el("details","definition-provenance"), current=field.constraints||{};
      box.append(el("summary","","取值与修改约束"));
      const update=(key,value)=>change(()=>{field.constraints??={};field.constraints[key]=value;});
      if(field.kind==="number"){
        for(const [key,label] of [["minimum","下界"],["maximum","上界"]]) box.append(input(label+"（可空）",current[key]??"",value=>{
          if(!value.trim())return update(key,null);
          const bound=Number(value); if(!Number.isFinite(bound))throw new Error("请输入有限数值。");update(key,bound);
        },{type:"number",step:"any"}));
      }
      if(field.kind==="string")box.append(input("允许的文字值（每行一个，留空表示自由文本）",list(current.choices).join("\n"),
        value=>update("choices",value.split(/\r?\n/).filter(v=>v.length)),{rows:3}));
      box.append(checkbox("允许取未知值",current.allow_unknown!==false,v=>update("allow_unknown",v)),
        checkbox("运行中固定不变",Boolean(current.fixed),v=>update("fixed",v)));
      return box;
    }
    function visibility(item, ownerId) {
      const box = el("details", "definition-provenance");
      const current = item.visibility || {public:false,readers:[]};
      box.append(el("summary", "", current.public ? "可见范围 · 公开" : `可见范围 · ${current.readers.length ? `本主体及 ${current.readers.length} 个指定主体` : "仅本主体"}`));
      const edit = fn => change(() => { item.visibility ??= {public:false,readers:[]}; fn(item.visibility); });
      box.append(checkbox("所有主体可见", current.public, checked => { edit(v => v.public = checked); render(); }));
      for (const entity of scene.entities.filter(a => a.id !== ownerId)) box.append(checkbox(entity.name, current.readers.includes(entity.id), checked => edit(v => {
        v.readers = checked ? [...new Set([...v.readers, entity.id])] : v.readers.filter(id => id !== entity.id);
      })));
      box.append(help("所属主体始终可见；上级不会自动取得权限。"));
      return box;
    }
    function renderSummary() {
      $("definition-title").textContent = scene.title || "未命名场景";
      let d = {};
      try { d = depths(scene); } catch { /* Invalid imports remain editable. */ }
      $("definition-summary").textContent = `${scene.sources.length} 份材料 · ${scene.entities.length} 个主体 · ${new Set(Object.values(d)).size} 个层级 · ${scene.influences.length} 条影响关系`;
      let config; try { config = bridge.config(); } catch { /* Editable invalid config. */ }
      const issues = checks(scene, config);
      for (const node of $("definition-steps").querySelectorAll("button")) {
        node.classList.toggle("has-issue", issues.some(item => item.kind === "error" && item.step === node.dataset.step));
      }
    }
    function renderBasics() {
      const root = $("definition-basics"); root.replaceChildren(el("h3", "", "场景与问题"), help("先说明要观察的变化，再补充世界的初始条件。"));
      root.append(input("场景名称", scene.title, value => change(s => s.title = value)), input("背景与推演目标", scene.background, value => change(s => s.background = value), {rows:6, placeholder:"谁处于什么环境？你希望观察什么变化？"}));
      root.append(el("h3", "subsection-title", "自定义领域"), help("领域组织资料与状态字段，由你按场景命名。"));
      for (const domain of scene.domains) root.append(row(input("领域名称", domain, value => change(() => { scene = renameDomain(scene, domain, value); }, true)), button("移除", () => {
        if (scene.entities.some(a => [...a.fields, ...a.materials].some(item => item.domain === domain))) throw new Error("该领域仍有状态字段或资料，请先调整其所属领域。");
        change(s => s.domains = s.domains.filter(name => name !== domain), true);
      })));
      let domainName = "";
      root.append(row(input("新增领域", "", value => domainName = value, {placeholder:"例如：供应链"}), button("添加", () => {
        const name = domainName.trim();
        if (!name || scene.domains.includes(name)) throw new Error("请输入一个不重复的领域名称。");
        change(s => s.domains.push(name), true);
      })));
      root.append(el("h3", "subsection-title", "世界规则与约束"));
      scene.rules.forEach((rule, index) => {
        const card = el("div", "editor-entry");
        card.append(input(`规则 ${index + 1}`, rule.text, value => change(() => rule.text = value), {rows:2}), provenance(rule), button("移除规则", () => change(s => s.rules.splice(index, 1), true)));
        root.append(card);
      });
      root.append(button("＋ 添加规则", () => change(s => s.rules.push({text:"",provenance:userSource()}), true)));
    }
    function renderSources() {
      const root = $("definition-sources"); root.replaceChildren(el("h3", "", "原始材料"), help("支持多份 TXT / Markdown。导入保留全文；解析后逐项审阅引用与假设。"));
      const zone = el("label", "source-dropzone", "拖入材料，或点击选择文件");
      const upload = el("input"); upload.type = "file"; upload.multiple = true; upload.accept = ".txt,.md,text/plain,text/markdown"; upload.setAttribute("aria-label", "上传来源材料");
      upload.addEventListener("change", () => readFiles(upload.files)); zone.append(upload);
      zone.addEventListener("dragover", event => { event.preventDefault(); zone.classList.add("drag-over"); });
      zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
      zone.addEventListener("drop", event => { event.preventDefault(); zone.classList.remove("drag-over"); if (!locked) readFiles(event.dataTransfer.files); });
      root.append(zone);
      scene.sources.forEach((source, index) => {
        const details = el("details", "source-entry"); details.append(el("summary", "", `${source.name} · ${source.text.length.toLocaleString()} 字符`));
        details.append(input("材料名称", source.name, value => change(() => source.name = value)),button("查看材料原文",()=>bridge.showSource(source)));
        if (!source.asset_id) details.append(input("材料全文",source.text,value=>change(()=>source.text=value),{rows:5}));
        else details.append(help("原件和提取文字已存档。勘误可作为新的补充材料添加。"));
        details.append(button("移除材料", () => {
          const refs = [...scene.entities, ...scene.rules, ...scene.influences, ...scene.entities.flatMap(a => [...a.fields,...a.materials])];
          if (refs.some(item => list(item.provenance?.source_ids).includes(source.id))) throw new Error("仍有定义引用这份材料，请先调整来源引用。");
          change(s => s.sources.splice(index, 1), true);
        })); root.append(details);
      });
      root.append(button("＋ 粘贴一份材料", () => change(s => s.sources.push({id:uniqueId(s.sources,"source"),name:"粘贴材料",text:""}), true)));
    }
    async function readFiles(files) {
      try {
        if (locked) return;
        const selected = Array.from(files || []);
        if (selected.some(file => !/\.(txt|md)$/i.test(file.name))) throw new Error("当前支持 TXT 和 Markdown 材料，请将其他格式先转为文本。");
        const materials = await Promise.all(selected.map(async file => ({name:file.name,text:await file.text()})));
        change(s => { for (const material of materials) s.sources.push({id:uniqueId(s.sources,"source"),...material}); }, true);
        bridge.notice(`已保留 ${materials.length} 份原始材料。尚未调用解析模型。`);
      } catch (error) { bridge.error(error); }
    }
    function addActor(subsystemId = null) {
      change(s => {
        const id = uniqueId(s.entities,"actor"); selectedActor = id;
        s.entities.push({id,name:"新主体",identity:"",preferences:"",subsystem_id:subsystemId,fields:[],materials:[],provenance:userSource()});
      }, true);
      showStep("actors");
    }
    function renderTree() {
      const root = $("definition-tree"); root.replaceChildren();
      const query = $("definition-search").value.toLowerCase(); const visited = new Set();
      let d = {}; try { d = depths(scene); } catch { /* Validation below shows malformed drafts. */ }
      function actorNode(entity, depth = 0) {
        if (visited.has(entity.id)) return; visited.add(entity.id);
        if (!query || `${entity.name} ${entity.id}`.toLowerCase().includes(query)) {
          const node = button("", () => { selectedActor = entity.id; renderTree(); renderActor(); }, "definition-actor-row");
          node.style.paddingLeft = `${12 + Math.min(depth, 8) * 16}px`;
          node.setAttribute("aria-pressed", String(entity.id === selectedActor));
          node.append(el("span", "level-tag", d[entity.id] == null ? "L?" : `L${d[entity.id]}`), el("span", "", entity.name || entity.id), el("small", "", `${entity.fields.length} 字段`)); root.append(node);
        }
        for (const system of scene.subsystems.filter(item => item.owner_id === entity.id)) {
          if (!query) {
            const group = el("div", "definition-system-row"); group.style.paddingLeft = `${18 + Math.min(depth, 8) * 16}px`;
            group.append(el("span", "", system.name), button("＋", () => addActor(system.id)));
            group.lastChild.setAttribute("aria-label", `在 ${system.name} 中添加主体`); root.append(group);
          }
          for (const child of scene.entities.filter(item => item.subsystem_id === system.id)) actorNode(child, depth + 1);
        }
      }
      for (const entity of scene.entities.filter(a => a.subsystem_id == null)) actorNode(entity);
      for (const entity of scene.entities) actorNode(entity);
      if (!scene.entities.length) root.append(help("添加第一个主体，或先从材料解析。"));
    }
    function renderSubsystems() {
      const root = $("definition-subsystems"); root.replaceChildren(el("h3", "subsection-title", "内部子系统"));
      for (const system of scene.subsystems) {
        const details = el("details", "source-entry"); details.append(el("summary", "", system.name));
        details.append(input("子系统名称", system.name, value => change(() => system.name = value, true)), select("所属主体", system.owner_id, actorChoices(), value => change(() => system.owner_id = value, true)), help(`ID：${system.id}`));
        details.append(button("移除子系统", () => {
          if (scene.entities.some(a => a.subsystem_id === system.id)) throw new Error("请先把子系统中的主体移到其他位置。");
          change(s => s.subsystems = s.subsystems.filter(item => item.id !== system.id), true);
        })); root.append(details);
      }
      root.append(button("＋ 为选中主体添加子系统", () => {
        if (!actor()) throw new Error("请先添加并选中一个主体。");
        change(s => s.subsystems.push({id:uniqueId(s.subsystems,"system"),name:`${actor().name}内部`,owner_id:selectedActor}), true);
      }), help("只有显式加入的主体建立 Agent；未展开部分可保留为上级状态。"));
    }
    function renderActor() {
      const root = $("definition-actor-detail"); root.replaceChildren();
      const entity = actor();
      if (!entity) { root.append(el("h3", "", "选择或添加主体"), help("每个显式主体都有独立上下文与决策权。")); return; }
      const header = el("div", "editor-title");
      header.append(el("h3", "", entity.name || entity.id), el("span", "tag", "独立 Agent")); root.append(header);
      root.append(row(input("主体名称", entity.name, value => change(() => entity.name = value, true)), input("主体 ID", entity.id, value => change(() => { scene = renameActor(scene, selectedActor, value.trim()); selectedActor = value.trim(); }, true))));
      root.append(select("所在子系统", entity.subsystem_id || "", [["","世界顶层"],...scene.subsystems.map(s => [s.id,`${actorName(s.owner_id)} / ${s.name}`])], value => change(() => entity.subsystem_id = value || null, true)));
      root.append(input("身份与职责", entity.identity, value => change(() => entity.identity = value), {rows:3}), input("决策偏好与目标", entity.preferences, value => change(() => entity.preferences = value), {rows:3}), provenance(entity));
      const fieldsHeader = el("div", "editor-title subsection-title"); fieldsHeader.append(el("h3", "", "已定义状态"), button("＋ 添加字段", () => {
        if (!scene.domains.length) throw new Error("请先在“材料与目标”中定义一个领域。");
        change(() => entity.fields.push({id:uniqueId(entity.fields,"field"),domain:scene.domains[0],kind:"number",value:null,unit:null,visibility:{public:false,readers:[]},provenance:userSource()}), true);
      })); root.append(fieldsHeader, help("空缺值保留为未知；没有添加的字段不会进入结算。"));
      entity.fields.forEach((field, index) => {
        const card = el("section", "editor-entry");
        card.append(row(input("字段 ID", field.id, value => change(() => {
          if (!identifier(value) || entity.fields.some((f, i) => i !== index && f.id === value)) throw new Error("字段 ID 无效或重复。"); field.id = value;
        })), select("所属领域", field.domain, scene.domains.map(name => [name,name]), value => change(() => field.domain = value))));
        card.append(row(select("字段类型", field.kind, [["number","数值"],["string","文字"],["boolean","是 / 否"]], value => change(() => {field.kind = value; field.value = null; if(field.constraints){delete field.constraints.minimum;delete field.constraints.maximum;delete field.constraints.choices;}}, true)), input("单位（可选）", field.unit, value => change(() => field.unit = value || null))));
        card.append(checkbox("初始值未知（null）", field.value == null, checked => change(() => field.value = checked ? null : field.kind === "number" ? 0 : field.kind === "boolean" ? false : "", true)));
        if (field.value != null) {
          if (field.kind === "boolean") card.append(select("初始值", String(field.value), [["true","是 / true"],["false","否 / false"]], value => change(() => field.value = value === "true")));
          else card.append(input("初始值", field.value, value => change(() => {
            if (field.kind === "number") {
              if (!value.trim()) field.value = null;
              else { const number = Number(value); if (!Number.isFinite(number)) throw new Error("请输入有效数字。"); field.value = number; }
            } else field.value = value;
          }, true), {type:field.kind === "number" ? "number" : "text",step:"any"}));
        }
        card.append(constraints(field),visibility(field, entity.id), provenance(field), button("移除字段", () => change(() => entity.fields.splice(index,1), true))); root.append(card);
      });
      const materialsHeader = el("div", "editor-title subsection-title"); materialsHeader.append(el("h3", "", "主体领域资料"), button("＋ 添加资料", () => {
        if (!scene.domains.length) throw new Error("请先在“材料与目标”中定义一个领域。");
        change(() => entity.materials.push({domain:scene.domains[0],text:"",visibility:{public:false,readers:[]},provenance:userSource()}), true);
      })); root.append(materialsHeader);
      entity.materials.forEach((material,index) => {
        const card = el("section","editor-entry");
        card.append(select("资料领域",material.domain,scene.domains.map(d => [d,d]),value => change(() => material.domain = value)), input("资料内容",material.text,value => change(() => material.text = value),{rows:3}),visibility(material,entity.id),provenance(material),button("移除资料",() => change(() => entity.materials.splice(index,1),true))); root.append(card);
      });
      root.append(button("移除此主体", () => {
        if (scene.subsystems.some(s => s.owner_id === entity.id) || scene.influences.some(e => e.source === entity.id || e.target === entity.id) || scene.entities.some(a => [...a.fields,...a.materials].some(f => list(f.visibility?.readers).includes(entity.id)))) throw new Error("此主体仍被子系统、影响关系或可见范围引用。请先调整这些引用。");
        change(s => {s.entities = s.entities.filter(a => a.id !== entity.id); selectedActor = s.entities[0]?.id;},true);
      }, "danger-button"));
    }
    function renderRelations() {
      const root = $("definition-edge-list"); root.replaceChildren();
      scene.influences.forEach((edge,index) => {
        const node = button("",() => {selectedEdge=index;renderRelations();},"definition-edge-row");
        node.setAttribute("aria-pressed",String(index === selectedEdge));
        node.append(el("span","",`${actorName(edge.source)} → ${actorName(edge.target)}`),el("strong",edge.weight == null ? "pending-weight" : "",edge.weight == null ? "待赋权" : String(edge.weight))); root.append(node);
      });
      const detail = $("definition-edge-detail"); detail.replaceChildren();
      const edge = scene.influences[selectedEdge];
      if (edge) {
        const updateEndpoint = (key,value) => change(() => {
          const source = key === "source" ? value : edge.source, target = key === "target" ? value : edge.target;
          if (scene.influences.some((e,i) => i !== selectedEdge && e.source === source && e.target === target)) throw new Error("这条有向边已存在；反向边可以独立添加。"); edge[key] = value;
        },true);
        detail.append(select("源主体",edge.source,actorChoices(),value => updateEndpoint("source",value)),select("目标主体",edge.target,actorChoices(),value => updateEndpoint("target",value)),input("影响强度（0–1，留空待估）",edge.weight,value => change(() => {
          const weight = value.trim() ? Number(value) : null;
          if (weight != null && (!Number.isFinite(weight) || weight < 0 || weight > 1)) throw new Error("权重必须位于 0–1。");
          edge.weight = weight; edge.provenance = userSource();
        },true),{type:"number",min:0,max:1,step:0.05}),provenance(edge),help("每个方向独立赋权；强度不表示服从概率，目标仍自主决策。"),button("移除此关系",() => change(s => {s.influences.splice(selectedEdge,1);selectedEdge=Math.max(0,selectedEdge-1);},true),"danger-button"));
      } else detail.append(help("添加第一条关系，连接任意两个显式主体。"));
      renderGraph();
    }
    function renderGraph() {
      const root = $("definition-graph"); root.replaceChildren();
      let d; try {d = depths(scene);} catch (error) {root.append(help(error.message));return;}
      const layers = [...new Set(Object.values(d))].sort((a,b) => a-b);
      const coords = new Map();
      const height = Math.max(380,...layers.map(depth => scene.entities.filter(a => d[a.id] === depth).length*120+110));
      const width = Math.max(720,layers.length*270+100);
      layers.forEach((depth,column) => {
        const actors = scene.entities.filter(a => d[a.id] === depth);
        actors.forEach((a,index) => coords.set(a.id,{x:140+column*270,y:100+index*((height-160)/Math.max(1,actors.length-1))}));
      });
      const svg = (tag,attrs,text) => {const node=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const [k,v] of Object.entries(attrs||{})) node.setAttribute(k,String(v));if(text != null) node.textContent=text;return node;};
      const canvas = svg("svg",{viewBox:`0 0 ${width} ${height}`,role:"group","aria-label":"按包含深度排列的有向影响图"});
      canvas.style.width=`${zoom*100}%`;canvas.style.minWidth=`${Math.min(width,720)*zoom}px`;
      const defs=svg("defs");const marker=svg("marker",{id:"influence-arrow",viewBox:"0 0 10 10",refX:9,refY:5,markerWidth:7,markerHeight:7,orient:"auto-start-reverse"});marker.append(svg("path",{d:"M 0 0 L 10 5 L 0 10 z",fill:"#507d76"}));defs.append(marker);canvas.append(defs);
      layers.forEach((depth,column) => {canvas.append(svg("text",{x:140+column*270,y:32,class:"graph-layer","text-anchor":"middle"},`L${depth}`));});
      scene.influences.forEach((edge,index) => {
        const a=coords.get(edge.source),b=coords.get(edge.target);if(!a||!b)return;
        let path,tx,ty;
        if(edge.source===edge.target) {path=`M ${a.x-25} ${a.y-24} C ${a.x-95} ${a.y-105} ${a.x+95} ${a.y-105} ${a.x+25} ${a.y-24}`;tx=a.x;ty=a.y-80;}
        else {
          const dx=b.x-a.x,dy=b.y-a.y,len=Math.hypot(dx,dy),ux=dx/len,uy=dy/len;
          const sx=a.x+ux*62,sy=a.y+uy*30,ex=b.x-ux*69,ey=b.y-uy*34;
          const bend=Math.abs(d[edge.source]-d[edge.target])>1 ? 110 : scene.influences.some(e=>e.source===edge.target&&e.target===edge.source)?42:24;
          const cx=(sx+ex)/2-uy*bend,cy=(sy+ey)/2+ux*bend;
          path=`M ${sx} ${sy} Q ${cx} ${cy} ${ex} ${ey}`;tx=(sx+2*cx+ex)/4;ty=(sy+2*cy+ey)/4-8;
        }
        const group=svg("g",{class:`graph-edge ${index===selectedEdge?"selected":""}`,tabindex:0,role:"button","aria-label":`${actorName(edge.source)} 影响 ${actorName(edge.target)}，权重 ${edge.weight??"待估"}`});
        group.append(svg("path",{d:path,class:"edge-hit"}),svg("path",{d:path,class:"edge-line","marker-end":"url(#influence-arrow)","stroke-dasharray":edge.weight==null?"6 5":"none"}),svg("text",{x:tx,y:ty,"text-anchor":"middle",class:"graph-weight"},edge.weight??"?"));
        const choose=()=>{selectedEdge=index;renderRelations();};group.addEventListener("click",choose);group.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();choose();}});canvas.append(group);
      });
      for(const entity of scene.entities){const p=coords.get(entity.id);const group=svg("g",{class:"graph-node",tabindex:0,role:"button","aria-label":`编辑主体 ${entity.name}`});group.append(svg("rect",{x:p.x-74,y:p.y-25,width:148,height:50,rx:8}),svg("text",{x:p.x,y:p.y+5,"text-anchor":"middle"},entity.name.length>10?entity.name.slice(0,9)+"…":entity.name),svg("title",{},`${entity.name} (${entity.id})`));const choose=()=>{selectedActor=entity.id;showStep("actors");};group.addEventListener("click",choose);group.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();choose();}});canvas.append(group);}
      root.append(canvas);$("graph-caption").textContent=`${scene.influences.length} 条有向边 · 实线为已设权重，虚线为待赋权 · 不要求入边归一化`;
    }
    function renderReview() {
      const root=$("definition-checks");root.replaceChildren();let config;try{config=bridge.config();}catch{/* config editor may be incomplete */}
      const items=checks(scene,config);
      root.append(el("p","review-headline",items.some(i=>i.kind==="error")?`${items.filter(i=>i.kind==="error").length} 项需要修正`:"基础结构已就绪"));
      if(!items.length)root.append(help("可继续核对模型与运行安排，再验证配置。"));
      for(const item of items){const node=button(item.text,()=>showStep(item.step),`definition-check ${item.kind}`);root.append(node);}
      root.append(help("这些检查在本地进行；启动前仍由引擎验证字段、来源和模型配置。"));
      const notes=$("definition-assumptions");notes.replaceChildren();
      for(const [key,label] of [["assumptions","假设清单"],["missing_information","缺失信息"]]){
        notes.append(el("h3","subsection-title",label));
        scene[key].forEach((text,index)=>notes.append(row(input(`${label} ${index+1}`,text,value=>change(s=>s[key][index]=value),{rows:2}),button("移除",()=>change(s=>s[key].splice(index,1),true)))));
        notes.append(button(`＋ 添加${label==="假设清单"?"假设":"缺失项"}`,()=>change(s=>s[key].push(""),true)));
      }
    }
    function render() {
      if(!scene)return;selectedActor=scene.entities.some(a=>a.id===selectedActor)?selectedActor:scene.entities[0]?.id;
      renderSummary();
      if(step==="materials"){renderBasics();renderSources();}
      if(step==="actors"){renderTree();renderSubsystems();renderActor();}
      if(step==="relations")renderRelations();
      if(step==="review")renderReview();
    }
    function showStep(name) {
      step=name;for(const id of steps)$("definition-"+id).hidden=id!==step;
      for(const node of $("definition-steps").querySelectorAll("button")){if(node.dataset.step===step)node.setAttribute("aria-current","step");else node.removeAttribute("aria-current");}
      const index=steps.indexOf(step);$("definition-back").disabled=index===0;
      $("definition-next").hidden=index===3;$("definition-next").textContent=`下一步：${labels[index+1]||""}`;render();
    }
    function view(name) {
      for (const id of ["library", "definition", "observer"]) {
        $(id+"-workspace").hidden = name !== id;
        $("show-"+id).setAttribute("aria-pressed", String(name === id));
      }
    }
    // Reuse the existing parse, model, validation and launch actions; one source
    // of truth for JSON and the graphical editor, no parallel run configuration.
    $("definition-settings").append(document.querySelector(".configuration"));
    const reviewActions=el("div","button-row");reviewActions.append($("validate-draft"),$("estimate-weights"));
    document.querySelector(".launch-area").before(reviewActions);
    $("definition-parser").append($("material-section"));$("material-section").open=true;
    $("material-file").hidden=true;document.querySelector('label[for="material-file"]').hidden=true;
    document.querySelector('label[for="material-text"]').textContent="补充解析要求（可选）";
    $("material-text").placeholder="例如：从材料中提取组织和内部部门，标记无法确定的关系。";
    $("material-text").rows=3;
    const advanced=el("details","scenario-advanced");advanced.append(el("summary","","高级：编辑场景 JSON"));
    const scenarioSection=document.querySelector(".scenario-section");scenarioSection.before(advanced);advanced.append(scenarioSection);
    $("show-definition").addEventListener("click",()=>view("definition"));$("show-observer").addEventListener("click",()=>view("observer"));
    for(const node of $("definition-steps").querySelectorAll("button"))node.addEventListener("click",()=>showStep(node.dataset.step));
    $("definition-back").addEventListener("click",()=>showStep(steps[Math.max(0,steps.indexOf(step)-1)]));
    $("definition-next").addEventListener("click",()=>showStep(steps[Math.min(3,steps.indexOf(step)+1)]));
    $("definition-search").addEventListener("input",renderTree);
    $("definition-add-actor").addEventListener("click",()=>attempt(()=>addActor()));
    $("definition-add-edge").addEventListener("click",()=>attempt(()=>{
      const pairs=scene.entities.flatMap(a=>scene.entities.filter(b=>b.id!==a.id).map(b=>[a.id,b.id]));
      const pair=pairs.find(([source,target])=>!scene.influences.some(e=>e.source===source&&e.target===target));
      if(!pair)throw new Error("请先添加两个主体，或检查是否已建立所有有向关系。");
      change(s=>{s.influences.push({source:pair[0],target:pair[1],weight:null,provenance:userSource()});selectedEdge=s.influences.length-1;},true);
    }));
    $("graph-zoom-in").addEventListener("click",()=>{zoom=Math.min(3,zoom+.25);renderGraph();});$("graph-zoom-out").addEventListener("click",()=>{zoom=Math.max(.5,zoom-.25);renderGraph();});$("graph-reset").addEventListener("click",()=>{zoom=1;renderGraph();});
    $("definition-export").addEventListener("click",()=>attempt(()=>bridge.download(bridge.bundle())));
    $("definition-undo").hidden = !previousDraft;
    $("definition-undo").addEventListener("click",()=>attempt(()=>{
      if(locked||!previousDraft)return;const prior=previousDraft;archive();bridge.importDraft(prior);showStep("materials");bridge.notice("已恢复上次替换前的场景、配置与准备记录。");
    }));
    $("definition-new").addEventListener("click",()=>attempt(()=>{
      if(locked)return;archive();bridge.importDraft({scenario:{title:"未命名场景",background:"",domains:[],entities:[],subsystems:[],influences:[],sources:[],rules:[],assumptions:[],missing_information:[]}});showStep("materials");bridge.notice("已创建空白草稿。可手动定义主体，也可导入材料解析；上一个草稿可恢复。");
    }));
    $("definition-import").addEventListener("click",()=>$("definition-file").click());
    $("definition-file").addEventListener("change",async()=>{
      try{const file=$("definition-file").files?.[0];if(!file||locked)return;const data=JSON.parse(await file.text());const value=data.scenario||data;
        if(!Array.isArray(value.entities)||!Array.isArray(value.domains)||typeof value.title!=="string")throw new Error("请选择川陀场景 JSON 或保存的定义文件。");
        const draft=normalize(value);depths(draft);archive();bridge.importDraft({...data,scenario:draft});showStep("materials");bridge.notice("已导入场景，可继续编辑与验证。尚未启动推演。");
      }catch(error){bridge.error(error);}finally{$("definition-file").value="";}
    });
    return {
      initialDraft,
      archive,
      update(value){if(ownUpdate)scene=value;else{scene=normalize(value);render();}persist();},
      configChanged(){persist();if(scene){renderSummary();if(step==="review")renderReview();}},
      lock(value){locked=value;$("definition-controls").disabled=value;for(const id of ["definition-import","definition-new","definition-undo"])$(id).disabled=value;$("definition-lock").hidden=!value;},
      parseInput(){return {text:[`场景名称：${scene.title||"未命名"}`,scene.background,$("material-text").value].filter(Boolean).join("\n\n"),sources:copy(scene.sources)};},
      show: view,
      showStep,
    };
  }
  const api={normalize,depths,renameActor,renameDomain,checks,create};
  if(typeof module!=="undefined"&&module.exports)module.exports=api;
  else window.ChuantuoDefinition=api;
})();
