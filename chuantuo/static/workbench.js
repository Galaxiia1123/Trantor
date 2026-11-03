"use strict";
const $ = id => document.getElementById(id);
const h = (tag, value, cls) => {
  const item = document.createElement(tag);
  if (value !== undefined && value !== null) item.textContent = String(value);
  if (cls) item.className = cls;
  return item;
};
const json = value => JSON.stringify(value, null, 2);
function sameValue(a,b) {
  if (a === b) return true;
  if (!a || !b || typeof a !== "object" || typeof b !== "object" || Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(k => Object.hasOwn(b,k) && sameValue(a[k],b[k]));
}
const uid = () => crypto.randomUUID().replaceAll("-", "");
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const safe = fn => async (...args) => { try { await fn(...args); } catch (err) { notice(err.message, true); } };
function button(label, fn, cls) {
  const item = h("button", label, cls); item.type = "button";
  item.addEventListener("click", safe(fn)); return item;
}
function disclosure(label, ...nodes) {
  const box = h("details", null, "subtle"); box.append(h("summary", label), ...nodes); return box;
}
function bullets(items) {
  const ul = h("ul", null, "compact-list"); items.forEach(s => ul.append(h("li", s))); return ul;
}
function notice(message = "", error = false) {
  $("notice").hidden = !message; $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
}
function local(key) { try { return JSON.parse(sessionStorage.getItem("ct11." + key)); } catch { return null; } }
function keep(key, value) {
  if (value === null) sessionStorage.removeItem("ct11." + key);
  else sessionStorage.setItem("ct11." + key, JSON.stringify(value));
}
async function api(path, method = "GET", body) {
  const init = {method, cache:"no-store"};
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.body = JSON.stringify(body); init.headers = {"Content-Type":"application/json"}; }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.detail;
    throw new Error(Array.isArray(detail) ? detail.map(d => d.loc.slice(1).join(".") + " " + d.msg).join(" / ") : detail || "请求未完成（" + response.status + "）");
  }
  return data;
}
const statuses = {ready:"待启动",running:"推演中",paused:"已暂停",failed:"运行失败",interrupted:"运行中断",stopped:"已提前结束",completed:"已完成"};
let profile, sources = [], draft, project, activeRun, example, timer, routeGeneration = 0, busy = false;
let selectedWindow = null, eventLimit = 100, lastView = "", reportsSignature = "";
let actorLimit = 8, watchRequest = 0;
const playbacks = new Map();
let sampleCatalog=[{id:"tidal",title:"潮汐港 · 跨层协商 · 9 窗",replay:"case"}];
const loadedSamples=new Map();
const playback = () => ["case","recording"].includes(activeRun);
let extraAnswers = "", overrides = {}, currentStateWindow = 0;
let requestedWindows = 40, historyThrough = -1;
let historyRequest = 0;
let savedLibrary = null;
let libraryLimits = {projects:20,runs:20,studies:20};
const readingTrail = [];
let readingRequest = 0;
function persistSeed() {
  const refs = sources.map(s => s.asset_id ? {asset_id:s.asset_id} : loadedSamples.get(s.id)?.text===s.text ? {sample:loadedSamples.get(s.id).key} : s);
  try { keep("seed", {background:$("brief").value, sources:refs, allow_search:$("allow-search").checked}); }
  catch { notice("本次输入超过浏览器暂存空间，整理并保存项目后即可重新打开。"); }
}
function showFiles() {
  $("files").replaceChildren(...sources.map((s, i) => {
    const chip = h("span", s.name, "file-chip"); chip.append(button("×", () => {
      sources.splice(i, 1); showFiles(); persistSeed();
    })); chip.lastChild.setAttribute("aria-label", "移除" + s.name); return chip;
  }));
}
function showProfile() {
  const m = profile?.model;
  $("open-model").classList.toggle("ready", Boolean(profile?.ready));
  $("open-model").querySelector("span").textContent = !m ? "配置模型" : profile.ready ? m.model : "补充凭据";
}
function modelDialog() {
  const m = profile?.model || {};
  $("provider").value = m.provider || "custom"; $("model-name").value = m.model || "";
  $("base-url").value = m.base_url || ""; $("protocol").value = m.protocol || "";
  $("thinking").value = m.thinking || "default"; $("thinking-label").hidden = $("provider").value !== "deepseek";
  $("context-limit").value = m.context_limit_chars || 120000; $("output-limit").value = m.max_tokens || 4096;
  $("model-key").value = ""; $("model-error").textContent = "";
  $("key-hint").textContent = profile?.key_storage === "windows_user" ? "填写的密钥由当前 Windows 账户加密保存。官方接口也可读取对应环境变量。" : "填写的密钥只在本次服务运行期间有效。可用提供商环境变量跨重启配置。";
  $("model-dialog").showModal();
}
$("open-model").onclick = modelDialog;
$("provider").onchange = () => {
  const deepseek = $("provider").value === "deepseek";
  $("thinking-label").hidden = !deepseek;
  if (deepseek && profile?.model?.provider !== "deepseek") $("thinking").value = "disabled";
};
document.querySelectorAll("dialog .close").forEach(b => b.onclick = () => b.closest("dialog").close());
$("model-dialog").addEventListener("close", () => { $("model-key").value = ""; });
$("model-form").onsubmit = async event => {
  event.preventDefault(); const old = profile?.model || {};
  const provider = $("provider").value, base_url = $("base-url").value.trim() || null;
  const same = old.provider === provider && old.base_url === base_url;
  const model = {...(same ? old : {}), provider, base_url, model:$("model-name").value.trim(),
    protocol:$("protocol").value || null, thinking:provider === "deepseek" ? $("thinking").value : "default",
    context_limit_chars:Number($("context-limit").value), max_tokens:Number($("output-limit").value)};
  try {
    profile = await api("/api/model", "PUT", {model, key:$("model-key").value || null});
    $("model-dialog").close(); showProfile(); if (draft) renderDraft(); notice(profile.ready ? "模型配置已保存。" : "模型名称已保存，请补充凭据。");
  } catch (err) { $("model-error").textContent = err.message; }
};
function reading(title, ...content) {
  readingRequest++;
  if ($("reading").open) {
    readingTrail.push({title:$("reading-title").textContent,nodes:[...$("reading-content").childNodes],
      scroll:$("reading").scrollTop,focus:document.activeElement});
    const back = button("← 返回" + $("reading-title").textContent, () => {
      readingRequest++;const prior = readingTrail.pop(); $("reading-title").textContent = prior.title;
      $("reading-content").replaceChildren(...prior.nodes);
      if(prior.focus?.isConnected)prior.focus.focus({preventScroll:true});
      $("reading").scrollTop=prior.scroll;
    }, "link");
    content.unshift(back);
  }
  $("reading-title").textContent = title; $("reading-content").replaceChildren(...content);
  $("reading").scrollTop=0;
  if (!$("reading").open) $("reading").showModal();
}
$("reading").addEventListener("close", () => { if (!$("reading").open) { readingTrail.length = 0; readingRequest++; } });
window.addEventListener("keydown", event => { if (event.key === "Escape") readingRequest++; });
$("brief").addEventListener("input", persistSeed); $("allow-search").onchange = persistSeed;
$("material").onchange = safe(async () => {
  const files = Array.from($("material").files); $("material").value = ""; $("prepare").disabled = true;
  try {
    for (const file of files) {
      notice("正在读取 " + file.name); const form = new FormData(); form.append("file", file);
      const data = await api("/api/materials", "POST", form);
      if (!sources.some(s => s.id === data.source.id)) sources.push(data.source);
      showFiles(); persistSeed();
    }
    notice(files.length ? "文稿已就绪，可以直接整理场景。" : "");
  } finally { $("prepare").disabled = busy; }
});
$("use-case").onclick = safe(async () => {
  const key=$("sample-material").value, source=await api("/api/case/materials/"+encodeURIComponent(key));
  sources=sources.filter(s=>!(loadedSamples.get(s.id)?.text===s.text || s.id===source.id));sources.push(source);
  loadedSamples.set(source.id,{key,text:source.text});showFiles();persistSeed();notice("已加入案例文稿，可直接整理。");
});
$("sample-material").onchange=()=>{const sample=sampleCatalog.find(s=>s.id===$("sample-material").value);$("read-case").textContent=sample?.replay ? "看案例回放" : "预览文稿";};
$("read-case").onclick = safe(async()=>{
  const sample=sampleCatalog.find(s=>s.id===$("sample-material").value);
  if(sample?.replay){location.hash=sample.replay;return;}
  const material=await api("/api/case/materials/"+encodeURIComponent($("sample-material").value));
  reading("案例文稿",h("pre",material.text));
});
$("try-letter").onclick = openLetterLab;
function pickCase(post) {
  $("letter-case").setAttribute("aria-pressed",String(!post));$("post-case").setAttribute("aria-pressed",String(post));
  $("sample-heading").textContent=post ? "封存单赶得上交接吗" : "一封签字函，两种结果";
  $("sample-intro").textContent=post ? "缩短山路邮程，或只寄一封说明。看看收货站何时拿到可以核对的副本。" : "签字函按时送达，申请就会进入复核吗？试着改变谁知道这件事。";
  $("try-letter").onclick=post ? openPostLab : openLetterLab;
}
$("letter-case").onclick=()=>pickCase(false);$("post-case").onclick=()=>pickCase(true);
$("import-bundle").onchange = safe(async () => {
  const file = $("import-bundle").files[0]; if (!file) return;
  const form = new FormData(); form.append("file", file); $("import-bundle").value = "";
  const imported = await api("/api/project-import","POST",form);
  location.hash = "project/" + imported.project.id;
});
async function awaitJob(id) {
  for (;;) {
    const receipt = await api("/api/jobs/" + id);
    if (receipt.status === "completed") return receipt.result;
    if (["failed","interrupted"].includes(receipt.status)) throw new Error(receipt.error || "任务中断");
    notice(receipt.message || "正在处理"); await pause(720);
  }
}
async function job(kind, path, payload, follow, sourceRun = activeRun) {
  if (busy) return;
  busy = true; $("prepare").disabled = true; const id = uid();
  keep("job", {id, kind, project:project?.id, run:sourceRun});
  try {
    await api(path.replace("{id}", id), "POST", payload);
    const result = await awaitJob(id);
    await follow(result); keep("job", null);
    if (kind === "prepare") { keep("failed-prepare",null); $("retry-panel").hidden = true; }
  } catch (err) {
    // Keep the receipt after a lost connection so a refresh can recover it.
    if (!(err instanceof TypeError)) keep("job", null);
    if (kind === "prepare" && !(err instanceof TypeError)) {
      const receipt = await api("/api/jobs/" + id).catch(() => null);
      if (["failed","interrupted"].includes(receipt?.status)) {
        keep("failed-prepare",{id,project:project?.id || null}); $("retry-panel").hidden = false;
      }
    }
    throw err;
  } finally { busy = false; $("prepare").disabled = false; }
}
async function saveProject(reason = "", preparedJob = null) {
  const assistant = {summary:draft.summary, goal:$("brief").value, extra_answers:extraAnswers,
    source_ids:sources.map(s => s.id), overrides, plan_reason:draft.plan_reason, revision_reason:reason};
  if (draft.observation_questions || draft.preparation_records.at(-1)?.observation_questions)
    assistant.observation_questions=applicableQuestions(draft.scenario,draft.observation_questions ?? draft.preparation_records.at(-1).observation_questions);
  const payload = {scenario:draft.scenario, config:draft.config, preparation_records:draft.preparation_records,
    assistant, expected_version:project?.version || 0};
  const endpoint = "/api/projects" + (project ? "/" + project.id : "") + (preparedJob ? "?prepared_job=" + encodeURIComponent(preparedJob) : "");
  const saved = await api(endpoint, "POST", payload);
  project = saved; draft.scenario = saved.payload.scenario; draft.config = saved.payload.config;
  draft.preparation_records = saved.payload.preparation_records;
  draft.questions = draft.preparation_records.at(-1)?.blocking_questions || [];
  return saved;
}
async function acceptedDraft(result) {
  const origin = result.preparation_records[0]?.preparation_trace?.job_id;
  if (origin && project?.payload?.assistant?.adopted_prepare_job === origin) {
    openProject(project); notice("已恢复此前保存的整理结果。"); return;
  }
  draft = result; renderDraft();
  await saveProject(extraAnswers || (project ? "依据重新提交的场景文稿整理" : ""), origin);
  history.replaceState(null, "", "#project/" + project.id);
  notice("场景已保存。"); renderDraft(); $("draft").scrollIntoView({block:"start"});
}
async function prepareScene() {
  if (!$("brief").value.trim() && !sources.length && !draft) { $("brief").focus(); throw new Error("请输入背景或导入文稿。"); }
  if (!profile?.ready) { modelDialog(); return; }
  const seed = {background:[$("brief").value, extraAnswers].filter(Boolean).join("\n\n"), sources,
    overrides, allow_search:$("allow-search").checked, current_scenario:draft?.scenario || null};
  await job("prepare", "/api/jobs/{id}/prepare", seed, acceptedDraft);
}
$("prepare").onclick = safe(prepareScene);
$("retry-prepare").onclick = safe(async () => {
  const failed = local("failed-prepare");
  if (!failed || failed.project !== (project?.id || null)) throw new Error("请打开原项目后重试，或重新整理当前文稿。");
  await job("prepare","/api/jobs/" + failed.id + "/retry/{id}",undefined,acceptedDraft);
});
function sourceReview(record) {
  const list = h("div");
  draft.scenario.sources.forEach(s => {
    const box = h("div", null, "line"); box.append(h("strong", s.name));
    const ref = s.public_reference;
    if (ref) {
      const link = h("a", "打开来源 ↗"); link.href = ref.url; link.target = "_blank"; link.rel = "noopener noreferrer";
      box.append(h("p", [ref.service, ref.published, ref.retrieved_at].filter(Boolean).join(" · "), "hint"), link);
    }
    box.append(disclosure("阅读保留的原文", h("pre", s.text))); list.append(box);
  });
  for (const f of record.findings || []) list.append(h("p", f.quote.quote + " — " + f.relevance + "。 " + f.time_and_authority));
  if (record.research?.length) list.append(disclosure("查询与候选来源", h("pre", json(record.research))));
  const names = Object.fromEntries(draft.scenario.entities.map(a => [a.id,a.name]));
  const deferred = record.visibility_review?.pending_grants || [];
  if (deferred.length) list.append(disclosure("开局尚未获知的资料", bullets(deferred.map(g =>
    (names[g.owner] || g.owner) + "的资料尚未向" + g.readers.map(r => names[r] || r).join("、") + "开放。"))));
  const sharing = draft.scenario.entities.flatMap(a => a.materials.filter(m => m.sharing).map(m => {
    const scope = m.sharing;
    const allowed = scope.allowed_recipients === null ? "依已声明的排除名单" : "限" + [...new Set([a.id,...scope.allowed_recipients])].map(id => names[id] || id).join("、");
    const denied = scope.excluded_recipients.length ? "，禁止发给" + scope.excluded_recipients.map(id => names[id] || id).join("、") : "";
    return a.name + "的保密资料原文" + allowed + denied + "。";
  }));
  if (sharing.length) list.append(disclosure("保密资料的通信范围",bullets(sharing)));
  if (record.checklist) list.append(disclosure("信息检查清单", h("pre", json(record.checklist))));
  return list;
}
function relationDiagram(actor, edges, actors, systems, selectActor) {
  const wrap = h("div",null,"relation-graph"), space = "http://www.w3.org/2000/svg";
  const element = (tag, attributes) => {
    const node = document.createElementNS(space,tag);
    Object.entries(attributes || {}).forEach(([key,value]) => node.setAttribute(key,String(value)));
    return node;
  };
  const incoming = edges.filter(e => e.target === actor.id && e.source !== actor.id).slice(0,6);
  const outgoing = edges.filter(e => e.source === actor.id && e.target !== actor.id).slice(0,6);
  const height = 62 * Math.max(1,incoming.length,outgoing.length) + 54, center = height / 2 + 10;
  const svg = element("svg",{viewBox:"0 0 630 " + height,role:"group","aria-label":actor.name + "的直接影响关系图"});
  const defs = element("defs"), arrowId = "rel-" + uid(), marker = element("marker",{id:arrowId,viewBox:"0 0 10 10",refX:9,refY:5,markerWidth:7,markerHeight:7,orient:"auto"});
  marker.append(element("path",{d:"M0 0 L10 5 L0 10 z",fill:"#736384"})); defs.append(marker); svg.append(defs);
  function label(x,y,text,css) {
    const n = element("text",{x,y,"text-anchor":"middle",class:css}); n.textContent = text; return n;
  }
  svg.append(label(96,21,"影响该主体","lane"),label(315,21,"所选主体","lane"),label(534,21,"受该主体影响","lane"));
  function level(a) {
    let depth = 0;
    while (a.parent_id || a.subsystem_id) {
      a = actors.get(a.parent_id || systems.get(a.subsystem_id).owner_id); depth++;
    }
    return depth + 1;
  }
  function node(id,x,y,selected) {
    const a = actors.get(id), g = element("g",{class:selected ? "focus-node selected" : "focus-node",tabindex:0,role:"button","aria-label":"查看" + a.name + "的关系"});
    const title = element("title"); title.textContent = a.name + " · " + a.identity;
    g.append(title,element("rect",{x:x-72,y:y-20,width:144,height:40,rx:8}),label(x,y-1,a.name.length > 10 ? a.name.slice(0,9)+"…" : a.name,"node-name"),label(x,y+13,"包含层级 " + level(a),"node-depth"));
    g.addEventListener("click",() => selectActor(id)); g.addEventListener("keydown",e => { if (["Enter"," "].includes(e.key)) {e.preventDefault();selectActor(id);} });
    svg.append(g);
  }
  for (const [left,items] of [[true,incoming],[false,outgoing]]) {
    items.forEach((edge,i) => {
      const y = center + (i - (items.length-1)/2)*62, id = left ? edge.source : edge.target;
      const path = element("path",{d:left ? "M168 "+y+" C211 "+y+" 202 "+center+" 241 "+center : "M389 "+center+" C431 "+center+" 424 "+y+" 460 "+y,
        fill:"none",stroke:"#736384","stroke-width":1.7,"marker-end":"url(#"+arrowId+")"});
      const title = element("title"); title.textContent = actors.get(edge.source).name+" → "+actors.get(edge.target).name+"："+edge.description; path.append(title);svg.append(path);
      node(id,left ? 96 : 534,y,false);
    });
  }
  node(actor.id,315,center,true); const viewport = h("div",null,"map-viewport"); viewport.append(svg); wrap.append(viewport);
  if (edges.length > incoming.length + outgoing.length) wrap.append(h("small","图上每侧最多展示六条直接关系，完整描述列于下方。","hint"));
  return wrap;
}
function relationExplorer(scene) {
  const box = h("div", null, "relations"), picker = h("select"), panel = h("div");
  const actors = new Map(scene.entities.map(a => [a.id,a])), systems = new Map(scene.subsystems.map(s => [s.id,s]));
  for (const actor of actors.values()) { const o = h("option",actor.name); o.value = actor.id; picker.append(o); }
  picker.setAttribute("aria-label","查看主体关系"); box.append(picker, panel);
  function draw() {
    const actor = actors.get(picker.value), owner = actor.parent_id || systems.get(actor.subsystem_id)?.owner_id;
    panel.replaceChildren(h("p",actor.identity + (owner ? " · 直属" + actors.get(owner).name : "")));
    const edges = scene.influences.filter(e => e.source === actor.id || e.target === actor.id);
    if (!edges.length) { panel.append(h("p","未设定该主体的直接影响关系。","hint")); return; }
    panel.append(relationDiagram(actor,edges,actors,systems,id => {picker.value=id; draw(); picker.focus();}));
    for (const e of edges) {
      const row = h("div",null,"relation"), start = e.source === actor.id ? actor.name : actors.get(e.source).name;
      row.append(h("strong",start), h("span"," → ","arrow"), h("strong",actors.get(e.target).name), h("p",e.description));
      const other = e.source === actor.id ? e.target : e.source;
      const p = e.provenance || {}, quotes = (p.quotes || []).map(q => {
        const source = scene.sources.find(s => s.id === q.source_id);
        return h("p","“" + q.quote + "”　" + (source?.name || q.source_id) + (q.page ? " · 第 " + q.page + " 页" : ""),"hint");
      });
      row.append(disclosure("关系依据",h("p",p.explanation || (p.kind === "ai_assumption" ? "模拟假设" : "用户设定"),"hint"),...quotes));
      if (other !== actor.id) row.append(button("查看" + actors.get(other).name + "的关系", () => {picker.value = other; draw();}, "link"));
      panel.append(row);
    }
  }
  picker.onchange = draw; draw(); return box;
}
function markManualEdits(prior, scene, reason) {
  const content = (item, exclusions) => Object.fromEntries(Object.entries(item || {}).filter(([key]) => !["provenance",...exclusions].includes(key)));
  function mark(item, original, excluded = []) {
    if (!sameValue(content(original,excluded),content(item,excluded)) && (!original || sameValue(original.provenance,item.provenance))) {
      item.provenance = {kind:"user",source_ids:[],quotes:[],explanation:reason || "用户在高级编辑中明确设定"};
    }
  }
  for (const a of scene.entities) {
    const old = prior.entities.find(v => v.id === a.id);
    mark(a, old, ["fields","materials"]);
    for (const f of a.fields || []) mark(f, old?.fields.find(v => v.id === f.id));
    for (const m of a.materials || []) mark(m, old?.materials.find(v => v.domain === m.domain && v.text === m.text));
  }
  for (const e of scene.influences || []) mark(e,prior.influences.find(v => v.source === e.source && v.target === e.target));
  for (const r of scene.rules || []) mark(r,prior.rules.find(v => v.text === r.text));
}
function runPlanEditor(settings, specified) {
  const root = h("div"), timing = h("div",null,"plan-fields"), tuning = h("div",null,"plan-fields");
  const controls = [];
  function add(group,key,title,min,max,options) {
    const input = h(options ? "select" : "input"), label = h("label",title);
    if (options) options.forEach(([value,name]) => {const o=h("option",name);o.value=value;input.append(o);});
    else {
      input.type = key === "window_duration" ? "text" : "number";
      if (min !== undefined) { input.min = min; input.step = "1"; }
      if (max !== undefined) input.max = max;
    }
    input.setAttribute("aria-label",title); input.required = key !== "model_call_limit";
    input.value = settings[key] ?? (key==="max_review_interval" ? 0 : ""); if (key === "model_call_limit") input.placeholder = "未设置";
    label.append(input); label.append(h("small", key in specified ? "已有设定" : "当前方案", "quiet"));
    group.append(label); controls.push({key,title,input});
  }
  add(timing,"windows","运行窗口数",1); add(timing,"window_duration","每窗时长"); add(timing,"rounds_per_window","每窗互动轮数",1);
  add(tuning,"recent_records","保留近期原文条数",1,32); add(tuning,"retrieval_count","每次检索历史条数",0,8);
  add(tuning,"history_windows","世界参照近期窗口数",1,24);
  add(tuning,"max_review_interval","主体自主等待上限（窗，0 表示每轮判断）",0,4);
  add(tuning,"roster_policy","主体通讯录",undefined,undefined,[["neighbors","相关主体"],["all","全部主体"]]);
  add(tuning,"max_parallel","同时请求上限",1,64); add(tuning,"model_call_limit","主体与世界总调用上限",1);
  add(tuning,"settlement_retries","每窗结算纠正次数",0,2); add(tuning,"output_retries","每次表达格式纠正次数",0,2);
  root.append(timing,disclosure("记忆、并发与失败处理",tuning,h("p","超过校验或调用上限后停在提交边界，已记录的原文可回看。","hint")));
  return {node:root,read() {
    const result = {...settings};
    for (const {key,title,input} of controls) {
      if (!input.checkValidity() || (key === "window_duration" && !input.value.trim())) {
        input.focus(); throw new Error("请核对“" + title + "”的取值。");
      }
      result[key] = input.type === "number" ? (input.value === "" ? null : Number(input.value)) : input.value.trim();
    }
    return result;
  }};
}
function renderDraft() {
  $("draft").hidden = !draft; if (!draft) return;
  const scene = draft.scenario, config = draft.config, record = draft.preparation_records.at(-1) || {};
  const root = $("draft"); root.replaceChildren();
  const top = h("div", null, "card-top"); top.append(h("div", "开局摘要", "eyebrow"), h("span", project ? "已存 · 第 " + project.version + " 版" : "待保存", "quiet"));
  root.append(top, h("h2", scene.title), h("p", draft.summary, "summary"), h("p", scene.entities.length + " 个主体　·　" + config.windows + " 个窗口　·　每窗 " + config.window_duration + "　·　" + config.model.model, "badge"));
  root.append(h("p", "每窗互动 " + config.rounds_per_window + " 轮，若逐轮判断共需 " + (config.windows * (scene.entities.length * config.rounds_per_window + 1)) + " 次基础调用。"+
    (config.max_review_interval ? "主体可自行等待，最迟 "+config.max_review_interval+" 窗后重新判断；来信和可见变化会提前唤醒。" : "")+"纠正或重试另计。", "hint"));
  const proposed=applicableQuestions(scene,record.observation_questions || []);
  if (proposed.length) {
    const selected=draft.observation_questions ?? proposed, panel=h("div");
    panel.append(h("p","整理模型根据文稿建议了这些观察问题。勾选的项目会在启动后显示，可随时增删。","hint"));
    for (const question of proposed) {
      const label=h("label",null,"check"), checkbox=h("input");checkbox.type="checkbox";
      checkbox.checked=selected.some(s=>sameValue(s,question));
      const actor=scene.entities.find(a=>a.id===question.actor_id), field=actor.fields.find(f=>f.id===question.field_id);
      label.append(checkbox,document.createTextNode(question.title || actor.name+" · "+(field.label || field.id)));panel.append(label);
      checkbox.onchange=safe(async()=>{
        const old=draft.observation_questions;
        draft.observation_questions=proposed.filter((q,i)=>panel.querySelectorAll("input")[i].checked);
        panel.querySelectorAll("input").forEach(input=>input.disabled=true);
        try {await saveProject("调整开局关注的问题");}
        catch(error){draft.observation_questions=old;throw error;}
        finally{renderDraft();}
      });
    }
    root.append(disclosure("准备追踪的问题（"+selected.length+"）",panel));
  }
  root.append(disclosure("参与者与有向关系", relationExplorer(scene),draftMailRoutes(scene)),draftBrief(scene,config));
  if(record.comparison_ideas?.length)root.append(disclosure("可以怎样对照（"+record.comparison_ideas.length+"）",
    h("p","这些备选条件不会改变开局。原路线跑到比较终点后，可在观察页选择运行。","hint"),
    ...record.comparison_ideas.map(ideaPreview)));
  root.append(disclosure("外部事件（"+(scene.scheduled_events || []).length+"）",draftCalendar(scene)));
  if (scene.assumptions.length || scene.missing_information.length) root.append(disclosure("假设与缺项（" + (scene.assumptions.length + scene.missing_information.length) + "）", bullets([...scene.assumptions,...scene.missing_information])));
  const check = record.source_reconciliation || {};
  const suggestions = (check.suggested_fields || []).filter(s => !s.adopted);
  if (suggestions.length) {
    const panel = h("div"); panel.append(h("p","文稿已列出结算字段。以下资料保留在主体档案中，可按需要加入结算。","hint"));
    for (const s of suggestions) {
      const actor = scene.entities.find(a => a.id === s.actor_id);
      if (!actor) continue;
      panel.append(recordRow(actor.name + " · " + (s.field.label || s.field.id),s.field.provenance.explanation,async () => {
        const previous = structuredClone(draft);
        if (!actor.fields.some(f => f.id === s.field.id)) actor.fields.push({...s.field,provenance:{...s.field.provenance,kind:"user",explanation:"用户从资料建议中明确采用此结算字段"}});
        if (s.dossier_text) actor.materials = actor.materials.filter(m => m.text !== s.dossier_text);
        s.adopted = true;
        try { await saveProject("明确采用建议字段"); } catch (err) { draft = previous; renderDraft(); throw err; }
        renderDraft(); notice("已将字段纳入新版本的结算范围。");
      },"采用字段"));
    }
    root.append(disclosure("待采纳的字段（" + suggestions.length + "）", panel));
  }
  root.append(disclosure("资料与依据（" + scene.sources.length + "）", sourceReview(record), draftDocuments(scene,record.document_review)));
  const qs = draft.questions || record.blocking_questions || [];
  if (qs.length) {
    root.append(h("h3", "开局前需要确认"), bullets(qs));
    const reply = h("textarea", null, "editor"); reply.setAttribute("aria-label", "补充回答"); reply.placeholder = "集中补充这些问题的答案";
    root.append(reply, button("按回答重新整理", async () => { if (!reply.value.trim()) throw new Error("请填写补充回答"); extraAnswers += "\n" + reply.value.trim(); await prepareScene(); }));
  }
  const adv = h("div", null, "advanced"), sceneEdit = h("textarea", null, "editor code");
  const {model, format_version, ...settings} = config;
  const plan = runPlanEditor(settings,{...record.explicit_settings,...overrides}); sceneEdit.value = json(scene);
  const labeled = (text, control) => { const l = h("label", text); l.append(control); return l; };
  const reason = h("input"); reason.placeholder = "变更关系或解决开局冲突时填写";
  adv.append(h("h3","运行方案"),h("p", draft.plan_reason || record.plan_reason, "hint"),plan.node,
    draftEntryWindows(scene),draftMeanings(scene),
    disclosure("编辑完整场景定义", labeled("场景 JSON", sceneEdit)), labeled("修改依据", reason),
    button("校验并保存修改", async () => {
      const editedPlan = plan.read(), editedScene = JSON.parse(sceneEdit.value);
      markManualEdits(scene, editedScene, reason.value.trim());
      const configNext = {...editedPlan, model, format_version}, records = structuredClone(draft.preparation_records);
      const latest = records.at(-1);
      for (const [key, question] of Object.entries(latest?.plan_conflicts || {})) {
        if (reason.value.trim() && key in editedPlan) {
          latest.blocking_questions = latest.blocking_questions.filter(q => q !== question);
          latest.explicit_settings[key] = editedPlan[key]; delete latest.plan_conflicts[key];
          latest.override_reason = reason.value.trim();
        }
      }
      const candidate = {scenario:editedScene, config:configNext, preparation_records:records};
      await api("/api/validate", "POST", candidate);
      const prior = draft, previousOverrides = overrides;
      draft = {...draft, ...candidate, questions:latest?.blocking_questions || []};
      overrides = Object.fromEntries(Object.entries(editedPlan).filter(([k,v]) => v !== settings[k] || k in overrides));
      try { await saveProject(reason.value.trim()); } catch (err) { draft = prior; overrides = previousOverrides; throw err; }
      renderDraft(); notice("已保存项目第 " + project.version + " 版。");
    }));
  if (profile?.ready && project && !sameValue(profile.model,config.model)) {
    const same = ["provider","model","base_url","protocol"].every(k => profile.model[k] === config.model[k]);
    if (same) adv.append(button("采用此接口的本机配置", async () => {
      const old = draft.config;
      draft.config = {...old,model:profile.model};
      try { await saveProject("为相同接口绑定本机凭据"); } catch (err) { draft.config = old; throw err; }
      renderDraft(); notice("已采用当前接口的本机配置。");
    }));
    else adv.append(button("用当前模型重新整理",prepareScene));
  }
  root.append(disclosure("高级编辑", adv));
  if (project) {
    const history = h("div"), versions = disclosure("已保存版本",history);
    versions.ontoggle = safe(async () => {
      if (!versions.open || history.childElementCount) return;
      const id = project.id, list = await api("/api/projects/" + id + "/versions");
      history.replaceChildren(...list.map(v => recordRow("第 " + v.version + " 版",v.relation_change_reason || new Date(v.updated_at).toLocaleString("zh-CN"), async () => {
        const saved = await api("/api/projects/" + id + "?version=" + v.version);
        const diff = saved.relation_revision;
        const rows = (diff?.changes || []).map(c => h("p",c.source + " → " + c.target + "\n原描述：" + (c.before?.description || "此前未设定") + "\n新描述：" + (c.after?.description || "已移除")));
        reading("第 " + v.version + " 版",h("p",diff?.reason || saved.payload.scenario.title),...rows,
          disclosure("该版本的场景与方案",h("pre",json({scenario:saved.payload.scenario,plan:saved.payload.config}))));
      },"查看")));
    });
    root.append(versions);
  }
  const controls = h("div", null, "actions right");
  if (project) { const exp = h("a", "导出项目包"); exp.href = "/api/projects/" + project.id + "/package"; controls.append(exp); }
  const launch = button("启动推演 →", async () => {
    if (!project) await saveProject();
    await job("launch", "/api/jobs/{id}/launch", {scenario:draft.scenario, config:draft.config, preparation_records:draft.preparation_records, project_id:project.id,project_version:project.version},
      async result => { location.hash = "run/" + result.run_id; });
  }, "primary"); launch.disabled = Boolean(qs.length); controls.append(launch); root.append(controls);
}
function openProject(p) {
  project = p; const data = p.payload, a = data.assistant || {}, record = data.preparation_records.at(-1) || {};
  if (data.scenario.format_version !== "1.1") { location.href = "/archive?project=" + p.id; return; }
  draft = {scenario:data.scenario, config:data.config,preparation_records:data.preparation_records, summary:a.summary || record.summary || data.scenario.background,
    plan_reason:a.plan_reason || record.plan_reason, questions:record.blocking_questions || []};
  draft.observation_questions=a.observation_questions ?? record.observation_questions;
  sources = data.scenario.sources.filter(s => (a.source_ids || []).includes(s.id)); $("brief").value = a.goal || "";
  extraAnswers = a.extra_answers || ""; overrides = {...(record.explicit_settings || {}), ...(a.overrides || {})};
  showFiles(); renderDraft();
}
function recordRow(title, sub, action, label = "打开") {
  const row = h("div", null, "record-row"), meta = h("div"); meta.append(h("strong", title), h("small", sub));
  row.append(meta, button(label, action)); return row;
}
async function library(gen) {
  const [projects,catalog,studies] = await Promise.all([api("/api/projects"),api("/api/runs"),api("/api/studies")]); if (gen !== routeGeneration) return;
  savedLibrary = {projects,catalog,studies}; drawLibrary();
}
function drawLibrary() {
  if (!savedLibrary) return;
  const {projects,catalog,studies} = savedLibrary, query = $("library-search").value.trim().toLocaleLowerCase();
  const matches = (...parts) => parts.some(x => String(x || "").toLocaleLowerCase().includes(query));
  const selections = {projects:projects.filter(p => matches(p.title,p.id,p.goal)),runs:catalog.runs.filter(r => matches(r.title,r.run_id,statuses[r.status],r.status,r.experiment?.condition,r.experiment && experimentKind(r.experiment))),
    studies:studies.filter(s=>matches(s.title,s.condition,s.id,studyStatus[s.status],"重复实验"))};
  $("studies").parentElement.hidden=!selections.studies.length;
  $("studies").replaceChildren(...selections.studies.slice(0,libraryLimits.studies).map(s=>recordRow(s.condition || s.title+" · 原条件重跑",
    (s.levels ? s.levels+" 档条件 · " : s.paired ? "双条件对照 · " : "")+studyStatus[s.status]+" · "+s.completed+"/"+s.count+" 条完成",()=>{location.hash="study/"+s.id;},"比较结果")));
  $("projects").replaceChildren(...selections.projects.slice(0,libraryLimits.projects).map(p => recordRow(p.title, p.actors + " 个主体 · 第 " + p.version + " 版", () => {location.hash = "project/" + p.id;})));
  $("runs").replaceChildren(...selections.runs.slice(0,libraryLimits.runs).map(r => recordRow(r.experiment ? experimentKind(r.experiment)+" · "+(r.experiment.condition || r.title) : r.title, (statuses[r.status] || r.status) + " · " + r.window + "/" + r.windows + " 窗 · " + (r.mode === "demo" ? "脚本" : "模型接口"), () => {location.hash = "run/" + r.run_id;}, "回看")));
  if (!selections.projects.length) $("projects").append(h("p",query ? "没有匹配的场景。" : "整理后的场景会保存在这里。","empty"));
  if (!selections.runs.length) $("runs").append(h("p",query ? "没有匹配的推演。" : "启动后可以在这里回看每次推演。","empty"));
  for (const [key,items] of Object.entries(selections)) {
    if (items.length > libraryLimits[key]) $(key).append(button("再显示 " + Math.min(20,items.length-libraryLimits[key]) + " 条",() => {libraryLimits[key]+=20; drawLibrary();}));
  }
  catalog.warnings.forEach(w => $("runs").append(h("p",w.message,"error")));
}
$("library-search").oninput = () => {libraryLimits={projects:20,runs:20,studies:20};drawLibrary();};
async function evidence(id, sourceRun = activeRun) {
  const ticket=++readingRequest, run=activeRun;
  const item = playback() && sourceRun === run ? example.evidence[id] : await api("/api/runs/" + sourceRun + "/events/" + encodeURIComponent(id));
  if (ticket !== readingRequest || run !== activeRun) return;
  if (!item) throw new Error("这份回放未收录该记录。");
  const body = h("div");
  const data=item.data || {}, labels={expression:"主体决定",message_delivered:"消息送达",message_dispatched:"消息寄出",window_committed:"窗口结算",observation:"当时的观察",run_created:"开局材料",external_event:"预定外部事件",decision_deferred:"延续等待"};
  body.append(h("small",(item.window ? "第 "+item.window+" 窗" : "开局")+" · "+(labels[item.kind] || item.kind),"quiet"));
  if (data.text) body.append(h("p",data.text));
  if (item.kind==="message_delivered") body.append(h("p",data.from+" → "+data.to),h("small","对应行动","quiet"),evidenceLinks([data.action_id],sourceRun));
  if (item.kind==="message_dispatched") body.append(h("p","预计第 "+data.due_window+" 窗开始时到达。"),evidenceLinks([data.action_id],sourceRun));
  if(["message_delivered","message_dispatched"].includes(item.kind))for(const d of data.enclosures || [])
    body.append(disclosure("随信副本 · "+d.title,h("p",d.text),h("small","内容指纹 "+d.sha256,"quiet")));
  if (data.dispatch_id)body.append(h("small","原始发件","quiet"),evidenceLinks([data.dispatch_id],sourceRun));
  if (item.kind==="expression" && !playback() && item.commit_status==="committed")
    body.append(button("查看作出决定时的可见信息",()=>openActorContext({id:data.actor_id,name:data.actor_id},item.window,data.round,false,sourceRun),"link"));
  if (item.kind==="window_committed") {
    for (const c of data.consequences || []) body.append(h("p",c.text),evidenceLinks([...(c.event_ids||[]),...(c.action_ids||[])],sourceRun));
    for (const r of data.reviews || []) body.append(h("p",r.entity_id+" · "+r.field_id+"　"+(r.before??"未知")+" → "+(r.after??"未知")),h("small",r.reason,"quiet"),evidenceLinks([...(r.event_ids||[]),...(r.action_ids||[])],sourceRun));
  }
  if (item.kind==="run_created" && data.scenario) body.append(h("h3",data.scenario.title),h("p",data.scenario.background));
  body.append(disclosure("完整记录数据",h("pre",json(item.data || item))));
  reading("记录 " + id, body);
}
function evidenceLinks(ids, sourceRun = activeRun) { const line = h("div"); for (const id of new Set(ids || [])) line.append(button(id, () => evidence(id, sourceRun), "link")); return line; }
function groupDeliveries(events) {
  const rows = [];
  for (const e of events) {
    const prior = rows.at(-1);
    const action = prior?.kind === "expression" ? prior.id : prior?.data.action_id;
    if (prior && ["expression","message_delivered"].includes(prior.kind) && e.kind === "message_delivered" && action === e.data.action_id &&
        !e.data.enclosures?.length && !prior.data.enclosures?.length && e.data.text === prior.data.text && prior.commit_status === e.commit_status) {
      prior.to.push(e.data.to); prior.receipts.push(e.id);
    } else rows.push({...e, to:e.kind === "message_delivered" ? [e.data.to] : [], receipts:[]});
  }
  return rows;
}
function fieldDetails(actor, stateRecord, view) {
  const fields = h("dl",null,"field-facts");
  const origins = {material:"原始材料",ai_assumption:"模拟假设",missing:"资料缺失",user:"用户设定"};
  for (const [id,field] of Object.entries(actor.fields)) {
    const definition = field.provenance || {};
    fields.append(h("dt",field.label || id));
    const row = h("dd"), value = field.value === null ? "未知" : typeof field.value === "boolean" ? (field.value ? "是" : "否") : String(field.value) + (field.unit || "");
    row.append(h("strong",value),h("small",field.domain + " · 定义来自" + (origins[definition.kind || "user"] || "已保存设定")));
    if(field.constraints?.choices?.length)row.append(h("small","可用的记录值　"+field.constraints.choices.join("、")));
    for(const m of field.constraints?.meanings || [])row.append(h("small","“"+m.value+"”指"+m.text));
    for(const p of field.constraints?.entry_windows || [])row.append(h("small","“"+p.value+"”"+entryText(p)));
    if (!playback()) row.append(button("查阅这项状态的依据",()=>openFieldReason(view,actor.id,id),"link"),
      button("追踪这个字段",()=>openFieldWatch(view,{actor_id:actor.id,field_id:id}),"link"));
    if (definition.quotes?.length) row.append(disclosure("查看定义原句",...definition.quotes.map(q => h("p","“" + q.quote + "”" + (q.page ? " · 第 " + q.page + " 页" : ""),"hint"))));
    fields.append(row);
  }
  return disclosure("数值、字段与来源",fields,h("small","当前数值的提交记录","quiet"),evidenceLinks([stateRecord]),
    disclosure("完整字段数据",h("pre",json(actor.fields))));
}
async function openActorContext(actor, window, turn, replace = false, run = activeRun) {
  const current=activeRun, ticket=++readingRequest;
  const result=await api("/api/runs/"+run+"/actors/"+encodeURIComponent(actor.id)+"/context?window="+window+(turn ? "&turn="+turn : ""));
  if (ticket!==readingRequest || current!==activeRun) return;
  const view=result.observation, memory=result.memory, body=h("div");
  const round=h("select"); round.setAttribute("aria-label","查看主体决策轮次");
  result.rounds.forEach(n=>{const o=h("option","第 "+n+" 轮");o.value=n;round.append(o);}); round.value=result.round;
  round.onchange=safe(()=>openActorContext(actor,window,Number(round.value),true,run));
  body.append(h("p",actor.name+" · 第 "+window+" 窗 · "+(result.deferred ? "引擎照常形成了这一轮的观察，主体按此前安排等待。" : "这次作决定时实际收到的内容。"),"hint"),round);
  if(result.decision) {
    const statement=h("section",null,"decision-statement");
    statement.append(h("small","当时的决定","quiet"),h("p",result.decision));
    if(result.outgoing?.length) statement.append(disclosure("这次发出的消息（"+result.outgoing.length+"）",
      ...result.outgoing.map(m=>h("p",m.recipients.map(id=>view.roster?.find(a=>a.id===id)?.name || id).join("、")+" · "+m.text))));
    body.append(statement);
  }
  body.append(button("就这一轮追问",()=>openDecisionQuestion(actor,result,run),"secondary"));
  if(result.deferred) body.append(h("p","本轮没有新的模型请求或对外行动。若无来信和可见变化，最迟第 "+result.review_at+" 窗重新判断。"),evidenceLinks([result.plan_record,result.deferred_id],run));
  if(result.resumed_plan) body.append(h("p",wakeText(result.resumed_plan.reason)),evidenceLinks([result.resumed_plan.plan_record],run));
  if(view.state){
    const names=Object.fromEntries(view.roster.map(a=>[a.id,a.name]));
    body.append(h("h3","可见处境"),bullets(Object.entries(view.state).map(([id,text])=>(names[id]||id)+" · "+text)));
  } else body.append(disclosure("可见字段",h("pre",json(view.visible_state))));
  if(view.field_definitions?.length)body.append(disclosure("当时可见的候选值和判定条件",...view.field_definitions.map(f=>{
    const card=h("div");card.append(h("strong",f.label),h("p",(f.choices || []).join("、"),"hint"));
    for(const m of f.meanings || [])card.append(h("p","“"+m.value+"”指"+m.text));
    for(const p of f.entry_windows || [])card.append(h("p","“"+p.value+"”"+entryText(p)));
    return card;
  })));
  if(view.messages?.length) {
    body.append(h("h3","本轮收到的消息"));
    view.messages.forEach(m=>{
      body.append(h("p",m.from+" → "+m.to+"　"+m.text),evidenceLinks([m.receipt_id || result.observation_id || result.input_id],run));
      for(const doc of m.enclosures || [])body.append(disclosure("收到附件 · "+doc.title,h("p",doc.text),evidenceLinks([m.receipt_id].filter(Boolean),run)));
    });
  }
  if(view.documents?.length)body.append(disclosure("当时持有的原件或副本",...view.documents.map(d=>{
    const original=view.visible_materials?.find(m=>m.document?.id===d.id),line=h("div");
    line.append(h("strong",d.title),h("small",d.first_delivery ? " · 第 "+d.received_window+" 窗取得副本" : " · 开局已持有","quiet"));
    if(original)line.append(h("p",original.text));
    if(d.first_delivery)line.append(button("查阅签收的副本",paperReader(run,actor.id,d,{window,round:result.round}),"link"),evidenceLinks([d.first_delivery],run));
    return line;
  })));
  if(view.mail_routes?.length) {
    const names=Object.fromEntries(view.roster.map(a=>[a.id,a.name]));
    body.append(disclosure("当时已知的通信时效",...view.mail_routes.map(r=>h("p",(names[r.source]||r.source)+" → "+(names[r.target]||r.target)+"　"+transitText(r.transit_windows)))));
  }
  if(view.external_events?.length) {
    body.append(h("h3","本轮收到的外部通知"));
    for(const event of view.external_events)body.append(h("p",event.text),...(event.at_settlement||[]).map(s=>h("small","窗末预定值："+s.entity_id+" · "+s.field_id+" → "+(s.value??"未知"),"quiet")));
  }
  if(memory?.open_matters) {
    body.append(h("h3","持续事项")); const items=Object.values(memory.open_matters);
    if(!items.length) body.append(h("p","当时没有登记的未完成事项。","hint"));
    for(const m of items) body.append(h("p",m.text),evidenceLinks([m.first_record,m.record].filter(Boolean),run));
    for(const [key,label] of [["recent","近期原文"],["earlier","历史摘录"],["retrieved","本轮检索"]]) {
      if(memory[key]?.length) body.append(disclosure(label+"（"+memory[key].length+"）",h("pre",json(memory[key]))));
    }
  } else if (!result.deferred) body.append(disclosure("当时的记忆输入",h("pre",json(memory))));
  if (view.rules?.length) body.append(disclosure("当时已知的规则和条件",bullets(view.rules.map(r=>r.text))));
  body.append(disclosure("完整可见信息",h("pre",json(view))),h("small",result.deferred ? "本轮观察" : "原始输入与对应决定","quiet"),
    evidenceLinks(result.deferred ? [result.observation_id] : [result.input_id,result.action_id],run));
  if(replace && $("reading").open) {
    const back=readingTrail.length ? $("reading-content").firstElementChild : null;
    $("reading-title").textContent=actor.name+"的决策现场"; $("reading-content").replaceChildren(...(back ? [back,body] : [body]));
  }
  else reading(actor.name+"的决策现场",body);
}
function adoptSuggestedField(view, event, suggestion, index) {
  const run = activeRun, form=h("form",null,"advanced"), owner=h("select"), domain=h("select"), kind=h("select");
  const name=h("input"), unit=h("input"), initial=h("input"), flag=h("select"), error=h("p",null,"error");
  const choose = (control,value,text) => {const o=h("option",text);o.value=value;control.append(o);};
  view.actors.forEach(a => choose(owner,a.id,a.name)); (view.domains || []).forEach(d => choose(domain,d,d));
  [["number","数值"],["string","文字"],["boolean","是 / 否"]].forEach(([v,t]) => choose(kind,v,t));
  [["","未知"],["true","是"],["false","否"]].forEach(([v,t]) => choose(flag,v,t));
  function field(label,control) {const l=h("label",label);control.setAttribute("aria-label",label);l.append(control);form.append(l);return l;}
  name.required=true; field("所属主体",owner); field("领域",domain); field("字段名称",name); field("类型",kind);
  const unitLabel=field("单位",unit), valueLabel=field("初始值（留空表示未知）",initial), boolLabel=field("初始值",flag);
  function typeChanged() {
    unitLabel.hidden=kind.value!=="number"; valueLabel.hidden=kind.value==="boolean"; boolLabel.hidden=kind.value!=="boolean";
    initial.type=kind.value==="number"?"number":"text"; initial.step="any";
  }
  kind.onchange=typeChanged; typeChanged();
  const consent=h("input");consent.type="checkbox";const share=field("向所有主体公开",consent);share.className="check";
  const submit=h("button","采纳并建立新场景","primary"); submit.type="submit"; form.append(submit,error);
  form.onsubmit=async e => {
    e.preventDefault();submit.disabled=true;
    try {
      const label=name.value.trim();if(!label)throw new Error("请填写字段名称。");
      const id=label.replace(/[^\p{L}\p{N}_.-]+/gu,"_");
      let value=null;
      if(kind.value==="boolean") value=flag.value===""?null:flag.value==="true";
      else if(initial.value!=="") value=kind.value==="number"?Number(initial.value):initial.value;
      const result=await api("/api/runs/"+run+"/adopt-field","POST",{window:event.window,suggestion_index:index,entity_id:owner.value,
        field:{id,label,domain:domain.value,kind:kind.value,value,unit:kind.value==="number"?(unit.value.trim()||null):null,visibility:{public:consent.checked}}});
      $("reading").close();location.hash="project/"+result.id;
    } catch(err) {error.textContent=err.message;} finally {submit.disabled=false;}
  };
  reading("采纳字段建议",h("p",suggestion),h("p","新场景从这一窗口已提交的状态开始。默认只有所属主体可见。","hint"),form);
}
function renderWindow(view) {
  const s = view.status, isCase = playback(), names = Object.fromEntries(view.actors.map(a => [a.id,a.name]));
  currentStateWindow = view.settled_through;
  $("open-interactions").hidden=isCase;
  $("open-interactions").onclick=safe(()=>openInteractions(view));
  const experimentSlot=$(s.experiment ? "comparison-lead" : "experiment-follow");
  if($("experiment-tools").parentElement!==experimentSlot)experimentSlot.append($("experiment-tools"));
  safe(()=>showWindowStrip(view))();
  showExperimentTools(view);
  showFieldWatches(view);
  if ($("all-windows").open && historyThrough !== s.window) safe(drawWindowHistory)();
  $("followup").hidden = isCase; $("ask").disabled = ["ready","running"].includes(s.status);
  const scope = $("question-scope"), priorScope = scope.value;
  const everyone = h("option","全局推演记录"); everyone.value = "";
  scope.replaceChildren(everyone, ...view.actors.map(a => {const o = h("option",a.name + "当时可见的记录"); o.value = a.id; return o;}));
  if (names[priorScope]) scope.value = priorScope;
  $("watch-title").textContent = view.title;
  $("watch-meta").textContent = (view.mode === "recorded_api" ? "真实接口实录 · 只读回放" : isCase || view.mode === "sample" ? "脚本案例 · 未调用真实模型" : "模型 " + view.model) + "　·　每窗 " + s.window_duration + "　·　" + (statuses[s.status] || s.status);
  const select = $("window"); select.disabled=false;select.replaceChildren();
  const live = h("option", isCase ? "最后一个窗口" : "跟随最新"); live.value = ""; select.append(live);
  const near=new Set([0,view.latest]);
  const first=view.latest<=150 ? 0 : Math.max(0,view.selected-3);
  const last=view.latest<=150 ? view.latest : Math.min(view.latest,view.selected+3);
  for(let i=first;i<=last;i++)near.add(i);
  for(const i of [...near].sort((a,b)=>a-b))select.append(new Option(windowName(i),String(i)));
  if(view.latest>150)select.append(new Option("跳到指定窗口…","jump"));
  select.dataset.latest=view.latest;
  select.value = selectedWindow === null ? "" : String(selectedWindow);
  $("previous-window").disabled=view.selected===0; $("next-window").disabled=view.selected>=view.latest;
  $("previous-window").onclick=safe(()=>pinWindow(view.selected-1)); $("next-window").onclick=safe(()=>pinWindow(view.selected+1));
  $("window-status").textContent = "第 " + view.selected + " 窗 / 共 " + s.windows + " 窗";
  const controls = $("run-actions"); controls.replaceChildren();
  const control = (label, action) => button(label, async () => {
    await api("/api/runs/" + activeRun + "/" + action, "POST"); lastView = ""; await refreshWatch(routeGeneration);
  });
  if(s.experiment?.study) {
    const link=h("a","返回实验组 · 路线 "+s.experiment.study.index);link.href="#study/"+s.experiment.study.id;controls.append(link);
  } else if (!isCase) {
    if (s.status === "running" && !s.pause_requested && !s.stop_requested) controls.append(control("暂停","pause"));
    if (s.status === "paused" && s.can_resume) controls.append(control("继续","resume"));
    if (["failed","interrupted"].includes(s.status) && s.can_resume) controls.append(control("从上次提交重试","recover"));
    if (["ready","running","paused"].includes(s.status) && !s.stop_requested) controls.append(control("提前结束","stop"));
    if (s.pause_requested && s.status === "running") controls.append(h("small","将在当前窗口结束后暂停"));
    if (s.stop_requested && s.status === "running") controls.append(h("small","正在结束"));
  }
  if(!isCase && view.format_version==="1.1" && s.window>0 && ["completed","stopped"].includes(s.status))
    controls.append(button("延长观察",()=>extendRun(view),"secondary"));
  const feed = $("events"); feed.replaceChildren();
  const actorSelect=$("feed-actor"), focus=actorSelect.value;
  if (actorSelect.options.length!==view.actors.length+1 || view.actors.some((a,i)=>actorSelect.options[i+1]?.value!==a.id)) {
    actorSelect.replaceChildren(new Option("全部主体",""),...view.actors.map(a=>new Option(a.name,a.id)));
    actorSelect.value=names[focus] ? focus : "";
  }
  if (s.error) feed.append(h("p", s.error, "error"));
  const settlement = view.settlement || view.events.find(e => e.kind === "window_committed");
  const cited = new Set((settlement?.data.consequences || []).concat(settlement?.data.changes || []).flatMap(c => c.action_ids || []));
  const outcome=$("window-outcome"); outcome.hidden=!settlement; outcome.replaceChildren();
  if (settlement) {
    const highlights=settlement.data.consequences || [], changes=settlement.data.changes || [];
    outcome.append(h("strong","本窗结果 · "+changes.length+" 项字段变化"),h("p",highlights[0]?.text || changes[0]?.reason || "本窗已结算，已定义字段保持原值。"),
      button(changes.length ? "为什么发生变化" : "查看结算依据",()=>openFieldReason(view),"link"));
  }
  let group = null, grouped = 0;
  for (const e of groupDeliveries(view.events)) {
    const data = e.data, row = h("div",null,e.kind === "window_committed" ? "event settled" : "event");
    const phase = e.commit_status === "discarded" ? " · 已作废" : e.commit_status === "uncommitted" ? " · 尚未结算" : "";
    if (e.to.length) {
      const sender = data.from || data.actor_id;
      row.append(h("span",(names[sender] || sender) + " → " + e.to.map(id => names[id] || id).join("、") + phase,"type"),h("p",data.text));
      if(data.dispatch_id)row.append(h("small","第 "+data.sent_window+" 窗寄出，本窗已送达。","quiet"));
    } else if(e.kind==="message_dispatched") {
      row.append(h("span",(names[data.from]||data.from)+" → "+(names[data.to]||data.to)+" · 已寄出"+phase,"type"),h("p",data.text),
        h("small","预计第 "+data.due_window+" 窗首送达。","quiet"),evidenceLinks([data.action_id]));
    } else if (e.kind === "expression") {
      row.append(h("span",(names[data.actor_id] || data.actor_id) + " · 决定" + phase,"type"), h("p",data.text));
      if(data.resumed_plan)row.append(h("small",wakeText(data.resumed_plan.reason),"quiet"));
      if(data.next_review_window)row.append(h("small","已安排最迟第 "+data.next_review_window+" 窗重新判断。","quiet"));
    } else if(e.kind==="decision_deferred") {
      row.append(h("span",(names[data.actor_id]||data.actor_id)+" · 延续等待"+phase,"type"),
        h("p","沿用此前决定，本轮没有新发言或投递。"),evidenceLinks([data.plan_record]));
    }
    else if(e.kind==="external_event") {
      row.append(h("span","外部事件"+phase,"type"),h("p",data.text),h("small",eventAudience(data,view.actors),"quiet"));
    } else if (e.kind === "window_committed") {
      row.append(h("strong","世界结算"));
      for (const c of data.consequences || []) row.append(h("p", c.text), evidenceLinks([...(c.event_ids||[]),...c.action_ids]));
      for (const c of data.changes || []) {
        const field = view.actors.find(a => a.id === c.entity_id)?.fields[c.field_id];
        const value = v => v === null ? "未知" : String(v) + (field?.unit || "");
        row.append(h("p", (names[c.entity_id] || c.entity_id) + "的" + (field?.label || c.field_id) + "从“" + value(c.before) + "”变为“" + value(c.after) + "”。"));
        if (!(data.consequences || []).some(item => item.text === c.reason)) row.append(h("p",c.reason), evidenceLinks([...(c.event_ids||[]),...c.action_ids]));
      }
      if (!data.consequences?.length && !data.changes?.length) row.append(h("p","本窗口已结算，已定义字段保持原值。"));
      if (data.suggested_fields?.length) {
        const offered = h("div");
        data.suggested_fields.forEach((s,i) => {
          offered.append(h("p",s));
          if (!isCase && view.domains?.length) offered.append(button("在新场景中采纳",()=>adoptSuggestedField(view,e,s,i),"link"));
        });
        row.append(disclosure("世界提出的字段建议（" + data.suggested_fields.length + "）",offered));
      }
    } else row.append(h("span","运行异常","type"), h("p",data.reason || "本窗口未完成，查看记录了解原因。"));
    row.append(button("记录 " + e.id, () => evidence(e.id), "link"));
    if(!isCase && e.commit_status==="committed")for(const doc of data.enclosures || [])
      row.append(button("随信副本 · "+doc.title,()=>openDocuments(view,doc.id),"link"));
    if (!isCase && ["expression","decision_deferred"].includes(e.kind) && e.commit_status==="committed")
      row.append(button("当时知道什么",()=>openActorContext({id:data.actor_id,name:names[data.actor_id]},e.window,data.round),"link"));
    if (e.receipts.length) row.append(disclosure("送达记录（" + e.receipts.length + "）",evidenceLinks(e.receipts)));
    if (settlement && e.kind === "expression" && !data.message_count && !cited.has(e.id)) {
      if (!group) { group = disclosure(""); group.classList.add("minor-actions"); feed.append(group); grouped = 0; }
      group.append(row); group.firstChild.textContent = "查看同期主体决定（" + (++grouped) + "）";
    } else { group = null; feed.append(row); }
  }
  if (!view.events.length) feed.append(h("p", view.selected === 0 ? "这是开局时的状态。" : (focus || $("feed-kind").value) ? "这个窗口没有符合筛选的记录。" : "正在等待本窗口的行动记录。", "empty"));
  if (view.next < view.total) feed.append(button("继续读取本窗口",async () => { eventLimit += 100; await refreshWatch(routeGeneration); }));
  const open = new Set(Array.from($("actors").querySelectorAll("details[open]"), el => el.dataset.actor));
  $("actors").replaceChildren(); $("state-time").textContent = view.settled_through ? "来自第 " + view.settled_through + " 窗的已提交状态" : "来自开局配置";
  const displayActors=focus ? view.actors.filter(a=>a.id===focus) : view.actors;
  for (const a of displayActors.slice(0,actorLimit)) {
    const card = h("div",null,"actor");
    const who=!isCase && view.settled_through ? button(a.name+" ↗",()=>enterPerspective(a.id,view.settled_through),"perspective-name") : h("strong",a.name);
    if(who.tagName==="BUTTON")who.setAttribute("aria-label","沿"+a.name+"的视角回看");
    card.append(who,h("p",a.profile || "未定义结算字段。"));
    if (Object.keys(a.fields).length) { const d = fieldDetails(a,view.state_record,view); d.dataset.actor = a.id; d.open = open.has(a.id); card.append(d); }
    if (!isCase && view.settled_through) card.append(button("当时可见的信息",()=>openActorContext(a,view.settled_through),"link"));
    $("actors").append(card);
  }
  if (displayActors.length>actorLimit) $("actors").append(button("查看更多主体（共 "+displayActors.length+" 位）",()=>{actorLimit+=12; renderWindow(view);},"link"));
  if (!isCase && view.usage) {
    const rows = Object.entries(view.usage).filter(([,u]) => u.calls).map(([stage,u]) => {
      const tokenText = u.with_usage ? "接口回报输入 " + u.input_tokens + "、输出 " + u.output_tokens + " token（" + u.with_usage + "/" + u.attempts + " 次有用量回执）" : "接口未回报 token";
      return h("p", stage + " · " + u.calls + " 次调用 · " + tokenText);
    });
    if(s.max_review_interval)rows.push(h("p","已提交窗口中有 "+s.deferred_reviews+" 个主体轮次沿用原等待安排，没有发起新的主体请求。"));
    const cost = disclosure("模型用量", ...rows, h("p","持续事项随主体决定更新，历史摘录和检索在本机执行。关联场景的准备用量按原任务列示。","hint"));
    cost.dataset.actor = "_usage"; cost.open = open.has("_usage"); $("actors").append(cost);
  }
  $("write-report").hidden = isCase; $("write-report").disabled = ["ready","running"].includes(s.status);
}
function reportBody(markdown) {
  const body = h("div",null,"paper"); let region = body, list = null;
  for (const line of markdown.split("\n")) {
    if (!line.trim() || /^\s*<a id="event-e\d+"><\/a>\s*$/.test(line)) continue;
    if (/^##\s+(证据索引|提交状态核对)\s*$/.test(line)) {
      region = h("div"); body.append(disclosure("查看" + line.replace(/^##\s*/,""),region)); list = null; continue;
    }
    let text = line, node;
    if (/^#{1,6}\s/.test(line)) { list = null; node = h(line.startsWith("# ") ? "h2" : "h3"); text = line.replace(/^#{1,6}\s*/,""); }
    else if (/^>\s?/.test(line)) { list = null; node = h("blockquote"); text = line.replace(/^>\s?/,""); }
    else if (/^[-*]\s/.test(line)) {
      if (!list) { list=h("ul",null,"compact-list"); region.append(list); }
      node=h("li"); text=line.slice(2);
    } else { list=null; node=h("p"); }
    for (const part of text.replace(/\[(e\d{6,12})\]\(#event-\1\)/g,"$1").split(/(e\d{6,12}|\*\*[^*]+\*\*|`[^`]+`)/g)) {
      if (/^e\d{6,12}$/.test(part)) node.append(button(part, () => evidence(part), "link"));
      else if (part.startsWith("**") && part.endsWith("**")) node.append(h("strong",part.slice(2,-2)));
      else if (part.charCodeAt(0) === 96 && part.charCodeAt(part.length-1) === 96) node.append(h("code",part.slice(1,-1)));
      else node.append(document.createTextNode(part));
    }
    (list || region).append(node);
  }
  return body;
}
async function showReport(id) {
  const ticket=++readingRequest, run=activeRun;
  const report = playback() ? example.report : await api("/api/runs/" + activeRun + "/reports/" + encodeURIComponent(id));
  if (ticket !== readingRequest || run !== activeRun) return;
  const download = button("下载 Markdown", () => {
    const url = URL.createObjectURL(new Blob([report.markdown], {type:"text/markdown;charset=utf-8"}));
    const a = h("a"); a.href = url; a.download = "川陀报告-" + (report.record_id || "潮汐港案例") + ".md"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  reading("推演报告", download, reportBody(report.markdown));
}
async function refreshReports(gen) {
  const list = playback() ? [{id:"sample",title:example.report.title || "潮汐港扩建听证 · 案例回顾",window:example.windows.length - 1}] : await api("/api/runs/" + activeRun + "/reports");
  if (gen !== routeGeneration) return;
  const signature = json(list);
  if (signature === reportsSignature) return;
  reportsSignature = signature;
  $("reports").replaceChildren(...list.map(r => recordRow(r.title, "截至第 " + r.window + " 窗", () => showReport(r.id), "阅读")));
  if (!list.length) $("reports").append(h("p","暂停或结束后，可根据已提交的记录生成带引用的报告。","empty"));
}
async function refreshWatch(gen) {
  clearTimeout(timer);
  const pinned = selectedWindow, run = activeRun, ticket=++watchRequest;
  const actor=$("feed-actor").value, kind=$("feed-kind").value;
  let view;
  if (playback()) {
    const data = playbacks.get(run) || await api(run === "recording" ? "/api/case/recording" : "/api/case/replay");
    if (gen !== routeGeneration || run !== activeRun) return;
    playbacks.set(run,data); example = data; view = data.windows[pinned ?? data.windows.length - 1];
    const settlement=view.events.find(e=>e.kind==="window_committed");
    const events=view.events.filter(e=>(!kind || e.kind===kind) && (!actor || [e.data.actor_id,e.data.from,e.data.to].includes(actor) ||
      (e.kind==="window_committed" && e.data.reviewed_actors?.includes(actor))));
    view={...view,settlement,events,total:events.length,next:events.length};
  } else {
    const filters=(actor ? "actor="+encodeURIComponent(actor)+"&" : "")+(kind ? "kind="+encodeURIComponent(kind)+"&" : "");
    const url = "/api/runs/" + run + "/watch?" + filters + (pinned === null ? "" : "window=" + pinned + "&");
    view = await api(url + "limit=100");
    const target = view.selected;
    for (let offset = view.next; offset < Math.min(eventLimit, view.total);) {
      const page = await api("/api/runs/" + run + "/watch?"+filters+"window=" + target + "&offset=" + offset + "&limit=100");
      view.events.push(...page.events); view.next = page.next; offset = page.next;
    }
  }
  if (gen !== routeGeneration || pinned !== selectedWindow || run !== activeRun || ticket!==watchRequest) return;
  const signature = json(view);
  if (signature !== lastView) { lastView = signature; renderWindow(view); }
  await refreshReports(gen);
  if (gen === routeGeneration && !["completed","stopped"].includes(view.status.status) && !playback())
    timer = setTimeout(safe(() => refreshWatch(gen)), view.status.status === "running" ? 1100 : 3500);
}
async function pinWindow(n) {
  selectedWindow=n;eventLimit=100;lastView="";
  const base=playback() ? activeRun : "run/"+activeRun;
  history.replaceState(null,"","#"+base+(n===null ? "" : "/"+n));
  await refreshWatch(routeGeneration);
}
$("window").onchange = safe(() => {
  const select=$("window");
  if(select.value!=="jump")return pinWindow(select.value==="" ? null : Number(select.value));
  select.value=selectedWindow===null ? "" : String(selectedWindow);
  const form=h("form",null,"experiment-form"),label=h("label","跳到第几窗"),n=h("input");
  n.type="number";n.min="0";n.max=select.dataset.latest;n.step="1";n.required=true;n.value=String(selectedWindow ?? currentStateWindow);
  const submit=h("button","回看这一窗","primary");submit.type="submit";label.append(n);form.append(label,submit);
  form.onsubmit=safe(async e=>{e.preventDefault();$("reading").close();await pinWindow(Number(n.value));});
  reading("按编号回看",form);n.select();
});
$("feed-actor").onchange=$("feed-kind").onchange=safe(async()=>{eventLimit=100; lastView=""; await refreshWatch(routeGeneration);});
async function drawWindowHistory() {
  const run = activeRun, root = $("window-list"), ticket=++historyRequest;
  const changes=$("changed-windows").checked, newest=$("window-order").value==="new";
  let items, total, committed;
  if (playback()) {
    if (!example) return;
    items = example.windows.slice(1).map(view => {
      const commit = view.events.find(e => e.kind === "window_committed");
      return {window:view.selected, changed_fields:commit?.data.changes?.length || 0,
        summary:(commit?.data.consequences || []).map(c => c.text).join(" ") || "本窗已结算，已定义字段保持原值。"};
    }); committed=items.length; if(changes)items=items.filter(v=>v.changed_fields); if(newest)items.reverse();
    total=items.length;items=items.slice(0,requestedWindows);
  } else {
    items = []; total = 0;
    do {
      const page = await api("/api/runs/" + run + "/windows?offset=" + items.length + "&limit=40&changed_only="+changes+"&newest_first="+newest);
      items.push(...page.windows); total = page.total; committed=page.committed;
      if (!page.windows.length) break;
    } while (items.length < Math.min(total,requestedWindows));
  }
  if (ticket!==historyRequest || run !== activeRun || !$("all-windows").open) return;
  historyThrough = committed;
  root.replaceChildren(...items.map(v => recordRow("第 " + v.window + " 窗 · " + v.changed_fields + " 项状态变化",v.summary,async () => {
    $("all-windows").open = false; await pinWindow(v.window);
  },"回看")));
  if (!items.length) root.append(h("p",committed && changes ? "已提交的窗口均保持原有字段值。清除筛选可回看互动。" : "首个窗口结算后可在这里回顾。","hint"));
  if (items.length < total) root.append(button("继续读取窗口",async () => {requestedWindows += 40; await drawWindowHistory();}));
}
$("all-windows").ontoggle = safe(async () => { if ($("all-windows").open) await drawWindowHistory(); });
$("changed-windows").onchange=$("window-order").onchange=safe(async()=>{requestedWindows=40;await drawWindowHistory();});
function openDecisionQuestion(actor, context, run) {
  const form=h("form",null,"advanced"), input=h("textarea"), label=h("label","想向"+actor.name+"确认什么？");
  input.className="editor"; input.rows=3; input.maxLength=10000; input.required=true;
  input.value=context.deferred ? "当时在等什么？哪些新消息会使你提前行动？" : "作出这个决定时，你掌握了哪些信息？还有什么无法确认？";
  label.append(input);
  const here="第 "+context.window+" 窗 · 第 "+context.round+" 轮", status=h("p","","hint");
  status.setAttribute("aria-live","polite");
  const submit=h("button","用这一轮的记录回答"); submit.type="submit";
  form.append(h("p",actor.name+" · "+here,"eyebrow"),label,
    h("p","回答只取用这轮的决定及其可见信息。"+(context.mode==="sample" ? "这条路线使用本地示例脚本。" : "通常调用 1 次模型，引用或格式出错时最多再试 1 次。"),"hint"),submit,status);
  form.onsubmit=async event=>{
    event.preventDefault();if(busy || submit.disabled)return;
    const question=input.value.trim();if(!question){input.focus();return;}
    const ticket=readingRequest;submit.disabled=true;status.textContent="正在查阅这轮的原始记录…";
    try {
      await job("question","/api/runs/"+run+"/jobs/{id}/question",
        {text:question,actor_id:actor.id,anchor:context.action_id || context.deferred_id,max_calls:2},async answer=>{
          if(activeRun===run && $("followup").open)await loadAnswers();
          status.textContent="回答已保存在这条路线的追问记录。";
          if(ticket===readingRequest && $("reading").open)reading(actor.name+"的现场回答",
            h("p",here+" · 回顾性访谈","eyebrow"),h("h3",question),h("p",answer.text),
            evidenceLinks(answer.evidence_ids,run),h("p",answer.limitations,"hint"),
            disclosure("本次提供的片段",h("p",answer.selection,"hint"),
              ...answer.evidence.map(item=>disclosure(item.locator.section+" · "+item.id,h("p",item.excerpt)))));
        },run);
    } catch(err){status.textContent=err.message;} finally{submit.disabled=false;}
  };
  reading("追问这轮决定",form);
}
async function loadAnswers() {
  const run = activeRun;
  if (!run || playback()) return;
  const list = await api("/api/runs/" + run + "/answers"); if (run !== activeRun) return;
  $("answers").replaceChildren(...list.map(a => {
    const row=disclosure(a.question, h("p",a.text), evidenceLinks(a.evidence_ids,run),h("p",a.limitations || "", "hint"));
    if(a.anchor_record) {
      const name=Array.from($("question-scope").options).find(option=>option.value===a.actor_id)?.textContent;
      row.append(button("回到第 "+a.cutoff_window+" 窗第 "+a.cutoff_round+" 轮的决定",
        ()=>openActorContext({id:a.actor_id,name:name?.replace(/当时可见的记录$/,"") || a.actor_id},a.cutoff_window,a.cutoff_round,false,run),"link"));
    }
    return row;
  }));
}
$("followup").ontoggle = safe(async () => { if ($("followup").open) await loadAnswers(); });
$("ask").onclick = safe(async () => {
  const question = $("question-text").value.trim(), run = activeRun;
  if (!question) throw new Error("请填写问题。");
  await job("question", "/api/runs/" + run + "/jobs/{id}/question", {text:question,actor_id:$("question-scope").value || null},
    async answer => { if (activeRun === run) { await loadAnswers(); reading("记录回答",h("p",answer.text),evidenceLinks(answer.evidence_ids),h("p",answer.limitations,"hint")); } notice("回答已保存。"); });
});
$("branch").onclick = safe(async () => {
  const p = await api("/api/runs/" + activeRun + "/branch", "POST", {window:currentStateWindow});
  location.hash = "project/" + p.id;
});
$("write-report").onclick = safe(async () => {
  if (busy) return; const run = activeRun;
  const plan = await api("/api/runs/" + run + "/report-plan");
  const max = h("input"); max.type = "number"; max.min = "1"; max.value = String(Math.max(4, plan.minimum_new_calls + 3));
  const label = h("label","本次报告调用上限"); label.append(max);
  reading("生成推演报告", h("p","范围截至第 " + plan.cutoff_window + " 窗，共 " + plan.evidence_count + " 条证据。基础预计至少需要 " + plan.minimum_new_calls + " 次新调用。"), label,
    button("开始生成", async () => {
      const cap = Number(max.value); if (!Number.isInteger(cap) || cap < 1) throw new Error("请输入有效调用上限");
      $("reading").close(); await job("report", "/api/runs/" + run + "/jobs/{id}/report", {max_calls:cap, expected_window:plan.cutoff_window},
        async res => { if (run === activeRun) { await refreshReports(routeGeneration); await showReport(res.record_id); } notice("报告已保存。"); });
    },"primary"));
});
async function route() {
  readingRequest++; if ($("reading").open) $("reading").close();
  const gen = ++routeGeneration; clearTimeout(timer); notice(""); lastView = reportsSignature = "";
  const [kind = "new", id, pinned] = location.hash.slice(1).split("/"); activeRun = null; selectedWindow = null; eventLimit = 100;
  const showWatch = ["run","case","recording"].includes(kind), showLibrary = kind === "library", showStudy=kind==="study";
  const showPerspective=kind==="actor";
  const showSaved=showWatch || showLibrary || showStudy || showPerspective;
  $("perspective-page").hidden=!showPerspective;
  $("watch-page").hidden = !showWatch; $("library-page").hidden = !showLibrary; $("study-page").hidden=!showStudy; $("prepare-page").hidden = showSaved;
  $("nav-new").classList.toggle("active", !showSaved); $("nav-library").classList.toggle("active", showSaved);
  if (showWatch) {
    activeRun = kind === "run" ? id : kind; $("watch-title").textContent = "正在打开记录";
    const at=kind==="run" ? pinned : id;
    if (/^\d{1,9}$/.test(at || "")) selectedWindow = Number(at);
    $("experiment-tools").hidden = true; $("experiment-tools").replaceChildren();
    stripState=null;$("window-strip").hidden=true;$("window-strip").replaceChildren();delete $("window-strip").dataset.stamp;
    $("field-watches").hidden=true; $("field-watches").replaceChildren(); delete $("field-watches").dataset.stamp;
    $("window-outcome").hidden=true; $("feed-actor").replaceChildren(new Option("全部主体","")); $("feed-kind").value=""; actorLimit=8;
    $("all-windows").open = false; historyThrough = -1; requestedWindows = 40; $("window-list").replaceChildren();
    $("changed-windows").checked=false; $("window-order").value="new";
    $("followup").open = false; $("answers").replaceChildren(); $("question-text").value = "";
    $("watch-meta").textContent = ""; ["events","actors","reports","run-actions","window"].forEach(k => $(k).replaceChildren());
    $("open-interactions").hidden=true;$("window").disabled=$("next-window").disabled=$("previous-window").disabled=true;
    $("events").append(h("p","正在读取该窗口的推演记录…","hint"));
    await refreshWatch(gen);
    if(gen===routeGeneration && perspectiveOrigin?.run===id && perspectiveOrigin.url===location.hash) {
      const saved=perspectiveOrigin;perspectiveOrigin=null;
      requestAnimationFrame(()=>{if(gen===routeGeneration)scrollTo(0,saved.scroll);});
    }
  }
  else if (showPerspective) {
    activeRun=id;$("perspective-page").replaceChildren(h("p","正在打开这个主体的手记…","hint"));scrollTo(0,0);
    await perspectivePage(gen);
  }
  else if (showStudy) await studyPage(id,gen);
  else if (showLibrary) await library(gen);
  else if (kind === "project" && id) {
    const p = await api("/api/projects/" + encodeURIComponent(id));
    if (gen === routeGeneration) { openProject(p); $("draft").scrollIntoView({block:"start"}); }
  }
  else {
    project = draft = null; extraAnswers = ""; overrides = {}; const seed = local("seed") || {};
    $("brief").value = seed.background || ""; $("allow-search").checked = seed.allow_search !== false;
    const loaded = await Promise.allSettled((seed.sources || []).map(async s => {
      if (s.asset_id) return (await api("/api/materials/" + encodeURIComponent(s.asset_id))).source;
      if (s.sample) {const item=await api("/api/case/materials/"+encodeURIComponent(s.sample));loadedSamples.set(item.id,{key:s.sample,text:item.text});return item;}
      if (s.case) { const v = await api("/api/case"); return {id:"tidal_brief",name:v.name,text:v.text}; }
      return s;
    }));
    if (gen !== routeGeneration) return;
    sources = loaded.filter(r => r.status === "fulfilled").map(r => r.value);
    showFiles(); renderDraft();
    if (sources.length !== loaded.length) notice("部分暂存文稿已不在本机资料库，可重新导入。", true);
  }
}
window.addEventListener("hashchange", safe(route));
safe(async () => {
  const catalog=await api("/api/case/materials");sampleCatalog=catalog;
  $("sample-material").replaceChildren(...catalog.map(s=>new Option(s.title,s.id)));
  profile = await api("/api/model"); showProfile(); await route();
  $("retry-panel").hidden = !local("failed-prepare");
  const pending = local("job");
  if (!pending) return;
  busy = true; $("prepare").disabled = true;
  try {
    const result = await awaitJob(pending.id);
    if (["launch","experiment"].includes(pending.kind)) location.hash = "run/" + result.run_id;
    else if (pending.kind === "prepare") {
      if (pending.project && project?.id !== pending.project) openProject(await api("/api/projects/" + pending.project));
      await acceptedDraft(result);
    } else if (["report","question","field_reading"].includes(pending.kind)) { location.hash = "run/" + pending.run; notice("结果已保存。"); }
    keep("job", null);
  } catch (err) { if (!(err instanceof TypeError)) keep("job", null); throw err; }
  finally { busy = false; $("prepare").disabled = false; }
})();
