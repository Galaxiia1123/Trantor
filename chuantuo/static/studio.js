(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const make = (tag, cls, value) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (value != null) node.textContent = value;
    return node;
  };
  const describeValue = value => value === null ? "未知" : String(value);
  const labels = {completed:"已完成", failed:"失败", interrupted:"中断", paused:"已暂停", stopped:"已结束", running:"运行中", ready:"准备中"};
  const modeNames = {api:"API 配置", mixed:"混合配置", demo:"离线演示"};
  const key = scope => "chuantuo.task." + scope;

  function create(bridge) {
    const {api, notice, onError} = bridge;
    let projects = [], runs = [], project = null, replay = null, replayRun = null;
    let observationQuery = 0, loadEpoch = 0, refreshEpoch = 0, evidenceQuery = 0;
    const runPath = () => "/api/runs/" + encodeURIComponent(bridge.runId());
    const action = (label, callback, cls) => {
      const node = make("button", cls, label);
      node.type = "button";
      node.addEventListener("click", async () => {
        node.disabled = true;
        try { await callback(); } catch (error) { onError(error); } finally { node.disabled = false; }
      });
      return node;
    };
    const reference = (id, otherRun) => action(id, () => showEvidence(id,otherRun), "evidence-link");
    function projectLabel() {
      $("current-project").textContent = project ? project.title + " · v" + project.version : "独立草稿";
    }
    function setProject(value) {
      project = value;
      try {
        if (value) sessionStorage.setItem("chuantuo.project", JSON.stringify(value));
        else sessionStorage.removeItem("chuantuo.project");
      } catch { /* The draft can still be saved manually. */ }
      projectLabel();
    }
    try { project = JSON.parse(sessionStorage.getItem("chuantuo.project") || "null"); } catch { /* No saved tab. */ }
    projectLabel();
    async function saveProject(asCopy = false) {
      const bundle = bridge.bundle();
      const prior = asCopy ? null : project;
      const saved = await api(prior ? "/api/projects/" + prior.id : "/api/projects",
        {...bundle, expected_version:prior?.version || 0});
      setProject({id:saved.id, version:saved.version, title:saved.payload.scenario.title});
      notice("已保存「" + project.title + "」第 " + project.version + " 版。可从项目与历史重新打开。");
      return {project_id:saved.id, project_version:saved.version};
    }
    async function openProject(id, version) {
      const saved = await api("/api/projects/" + id + (version ? "?version=" + version : ""));
      if (saved.payload.scenario.format_version === "1.1") { location.assign("/#project/" + saved.id); return; }
      bridge.importProject(saved.payload);
      setProject({id:saved.id, version:saved.version, title:saved.payload.scenario.title});
      notice("已打开「" + project.title + "」第 " + project.version + " 版。");
    }
    function renderLibrary() {
      const needle = $("library-search").value.trim().toLowerCase();
      const matches = (...terms) => terms.join(" ").toLowerCase().includes(needle);
      const left = $("project-list"), right = $("run-list");
      left.replaceChildren(); right.replaceChildren();
      for (const item of projects.filter(p => matches(p.title,p.id,p.goal))) {
        const card = make("article","library-card");
        card.append(make("span","tag","v" + item.version), make("h4","",item.title),
          make("p","field-help",item.actors + " 个主体 · " + item.sources + " 份材料"));
        const buttons = make("div","button-row");
        buttons.append(action("打开项目", () => openProject(item.id,item.version)));
        const exportLink=make("a","source-original","下载项目与原件"); exportLink.href="/api/projects/"+item.id+"/package?version="+item.version;exportLink.download="";
        buttons.append(exportLink);
        const versions = make("div","version-list");
        buttons.append(action("查看版本", async () => {
          const history = await api("/api/projects/" + item.id + "/versions");
          versions.replaceChildren(...history.map(v => action("v" + v.version + " · " + new Date(v.updated_at).toLocaleString("zh-CN"), () => openProject(v.id,v.version), "text-button")));
        }, "text-button"));
        card.append(buttons, versions); left.append(card);
      }
      for (const item of runs.filter(r => matches(r.title,r.run_id,r.project_id || "",labels[r.status]))) {
        const card = make("article","library-card run-card");
        const badge = make("span","tag",labels[item.status] || item.status);
        card.append(badge, make("h4","",item.title), make("p","field-help",
          [modeNames[item.mode],item.window + " / " + item.windows + " 窗口", item.reports + " 份报告",item.run_id].join(" · ")),
          action("打开推演", () => bridge.openRun(item.run_id)));
        right.append(card);
      }
      if (!left.childElementCount) left.append(make("p","empty-state",needle ? "没有匹配的项目。" : "保存第一个项目版本后，可在这里继续编辑。"));
      if (!right.childElementCount) right.append(make("p","empty-state",needle ? "没有匹配的运行。" : "完成启动后，运行记录会出现在这里。"));
    }
    async function library() {
      bridge.show("library");
      const epoch = ++refreshEpoch;
      const [p, r, jobs] = await Promise.all([api("/api/projects"), api("/api/runs"),api("/api/jobs")]);
      if (epoch !== refreshEpoch) return;
      projects = p; runs = r.runs; renderJobs(jobs);
      const warnings=$("catalog-warning"); warnings.replaceChildren();
      for(const w of r.warnings) {
        warnings.append(make("span","",w.directory+" · "+w.message+" "));
        if(w.can_salvage) warnings.append(action("救回完整记录为新副本",async()=>{
          const result=await api("/api/archive/"+encodeURIComponent(w.directory)+"/salvage",{});
          await bridge.openRun(result.run_id);notice("已救回至窗口 "+result.committed_window+"，原档案保留在 "+w.directory+"。");
        }));
      }
      renderLibrary();
    }

    function renderJobs(items) {
      const root=$("job-list");root.replaceChildren();
      const verbs={definition:"场景整理",material:"材料识别",report:"生成报告",question:"访谈与分析",launch:"启动推演"};
      for(const job of items) {
        const c=job.context||{},row=make("article","job-row");
        row.append(make("strong","",verbs[job.kind]||job.kind), make("span","tag",labels[job.status]||({queued:"排队中",running:"处理中"}[job.status])||job.status));
        row.append(make("p","field-help",c.goal || c.name || c.title || c.run_id || job.id));
        if(job.has_trace){
          const trace=make("a","source-original","下载准备记录");trace.href="/api/jobs/"+job.id+"/trace";trace.download="";row.append(trace);
        }
        if(job.can_retry)row.append(action("用原请求重试，复用已核验步骤",async()=>{
          if(pending("definition"))throw new Error("已有待找回的场景整理任务，请先完成该任务。");
          const result=await jobRequestFromHistory(job,c);
          bridge.restoreTask("definition",result,c);clearJob("definition");
        }));
        if(job.error) row.append(make("p","field-help",job.error));
        if(job.status==="completed" && ["definition","material"].includes(job.kind)) row.append(action(job.kind==="definition"?"找回本次草案":"将原件加入材料草稿",async()=>{
          const receipt=await api("/api/jobs/"+job.id);
          bridge.restoreTask(job.kind,receipt.result,c);
        }));
        else if(c.run_id) row.append(action("打开关联推演",()=>bridge.openRun(c.run_id)));
        root.append(row);
      }
      if(!items.length)root.append(make("p","field-help","尚无已提交的任务。"));
    }
    async function jobRequestFromHistory(prior,context) {
      return job("definition",id=>"/api/jobs/"+prior.id+"/retry/"+id,{},
        {fromHistory:true,goal:context.goal || ""},message=>{$("catalog-warning").textContent=message;});
    }

    function pending(scope) {
      try { return JSON.parse(sessionStorage.getItem(key(scope)) || "null"); } catch { return null; }
    }
    function clearJob(scope) { sessionStorage.removeItem(key(scope)); }
    async function awaitJob(scope, receipt, progress) {
      for (;;) {
        progress?.(receipt.message || "等待本地任务");
        if (receipt.status === "completed") return receipt.result;
        if (["interrupted","failed"].includes(receipt.status)) {
          clearJob(scope);
          throw new Error(receipt.error || "任务已中断，可重新提交。");
        }
        await new Promise(resolve => setTimeout(resolve, 650));
        receipt = await api("/api/jobs/" + receipt.id);
      }
    }
    async function job(scope, pathForId, payload, meta = {}, progress) {
      const existing = pending(scope);
      if (existing) return resumeJob(scope, progress);
      const id = crypto.randomUUID().replaceAll("-", "");
      sessionStorage.setItem(key(scope), JSON.stringify({id,...meta}));
      let receipt;
      try { receipt = await api(pathForId(id), payload); }
      catch (error) {
        // A lost response can still have started the model. Keep its ID until checked.
        if (error.status && error.status < 500) clearJob(scope);
        throw error;
      }
      return awaitJob(scope, receipt, progress);
    }
    async function resumeJob(scope, progress) {
      const receipt = pending(scope);
      if (!receipt) return null;
      let result;
      try { result = await api("/api/jobs/" + receipt.id); }
      catch (error) {
        if (error.status === 404) { clearJob(scope); throw new Error("服务没有收到这项任务，可重新提交。"); }
        throw error;
      }
      return awaitJob(scope, result, progress);
    }

    async function showEvidence(id, otherRun) {
      const query = ++evidenceQuery, active = bridge.runId();
      const path = otherRun ? "/api/runs/"+encodeURIComponent(otherRun) : runPath();
      const event = await api(path + "/events/" + encodeURIComponent(id));
      if (query !== evidenceQuery || active !== bridge.runId()) return;
      const dialog = $("evidence-dialog");
      $("evidence-title").textContent = "证据 " + id + (otherRun ? " · " + otherRun : "");
      const body = $("evidence-body");
      body.replaceChildren(make("p","field-help",event.kind + " · 窗口 " + event.window + (event.commit_status==="discarded" ? " · 此记录来自重试前作废的尝试" : event.commit_status==="uncommitted" ? " · 该窗口尚未提交" : "")));
      body.append(action("复制此证据的本机链接",async()=>{
        const url=new URL(location.origin+"/");url.searchParams.set("run",otherRun||active);url.searchParams.set("tab","events");url.searchParams.set("event",id);
        await navigator.clipboard.writeText(url.href);notice("已复制证据链接。");
      },"text-button"));
      const raw = event.data;
      if (raw.text) body.append(make("p","quote",raw.text));
      if (raw.changes) for (const change of raw.changes) {
        const node = make("p","evidence-note",change.entity_id + " / " + change.field_id + "　" + describeValue(change.before) + " → " + describeValue(change.after) + "　" + change.reason + " ");
        for (const actionId of change.action_ids) node.append(reference(actionId,otherRun));
        body.append(node);
      }
      body.append(make("pre","evidence-raw",JSON.stringify(raw, null, 2)));
      if (!dialog.open) dialog.showModal();
      body.scrollTop = 0;
    }
    function showSource(source, quote) {
      evidenceQuery++;
      const dialog = $("evidence-dialog"), body = $("evidence-body");
      $("evidence-title").textContent = source.name;
      body.replaceChildren();
      const pages = source.pages || [], select = make("select");
      select.setAttribute("aria-label","选择材料页");
      const content = make("pre","evidence-raw"), characters = Array.from(source.text);
      const render = () => {
        const selected = pages.find(p => p.number === Number(select.value));
        const text = selected ? characters.slice(selected.text_start,selected.text_end).join("") : source.text;
        const offset = quote ? text.indexOf(quote.quote) : -1;
        content.replaceChildren();
        if (offset >= 0) content.append(document.createTextNode(text.slice(0,offset)),make("mark","",quote.quote),document.createTextNode(text.slice(offset + quote.quote.length)));
        else content.textContent = text || "此页未提取到文字。";
      };
      if (pages.length) {
        select.append(...pages.map(p=>new Option("第 "+p.number+" 页 · "+p.method,String(p.number))));
        if (quote) {
          const first = quote.page || pages.find(p=>characters.slice(p.text_start,p.text_end).join("").includes(quote.quote))?.number;
          if (first) select.value=String(first);
        }
        select.addEventListener("change",render); body.append(select);
      }
      if (source.asset_id) {
        const link=make("a","source-original","下载保存的原件"); link.href="/api/materials/"+source.asset_id+"/original";body.append(link);
      }
      body.append(content); render();
      if (!dialog.open) dialog.showModal();
      if (quote) content.querySelector("mark")?.scrollIntoView({block:"center"});
    }
    function textWithReferences(target, value) {
      const parts = value.split(/(\[e\d{6,}\]\(#event-e\d{6,}\)|\be\d{6,}\b)/g);
      for (const part of parts) {
        const match = part.match(/^(?:\[)?(e\d{6,})(?:\]\(#event-\1\))?$/);
        if (match) target.append(reference(match[1]));
        else target.append(document.createTextNode(part));
      }
    }
    function renderReport(result) {
      const root = $("report-reading");
      root.replaceChildren();
      for (const line of result.markdown.split(/\r?\n/)) {
        if (!line.trim() || /^<a id="event-e\d+"><\/a>$/.test(line.trim())) continue;
        const heading = line.match(/^(#{1,4})\s+(.*)$/);
        const node = make(heading ? (heading[1].length === 1 ? "h3" : "h4") : line.startsWith("> ") ? "blockquote" : "p");
        textWithReferences(node, heading ? heading[2] : line.replace(/^> |^- /,""));
        root.append(node);
      }
      root.hidden = false; $("report-source").hidden = false;
    }
    async function reportList(runId, openLatest) {
      const records = await api("/api/runs/" + runId + "/reports");
      if (bridge.runId() !== runId) return;
      const select = $("report-versions");
      select.replaceChildren(...records.slice().reverse().map(r => new Option("窗口 " + r.window + " · " + r.title + " · " + r.id,r.id)));
      select.hidden = $("report-versions-label").hidden = !records.length;
      if (openLatest && records.length) {
        const result = await api("/api/runs/" + runId + "/reports/" + select.value);
        if (bridge.runId() === runId) bridge.displayReport(result);
      }
    }
    function renderAnswers(items) {
      const list = $("answers-list");
      list.replaceChildren();
      for (const answer of items.slice().reverse()) {
        const node = make("article","answer");
        node.append(make("p","eyebrow",(answer.actor_id ? bridge.actorName(answer.actor_id) + " · 主体访谈" : "全局记录分析") + " · 截至窗口 " + answer.cutoff_window),
          make("h4","",answer.question), make("p","",answer.text));
        for (const eid of answer.evidence_ids) node.append(reference(eid));
        if (answer.limitations) node.append(make("p","field-help",answer.limitations));
        list.append(node);
      }
    }
    async function answers(runId) {
      const items = await api("/api/runs/" + runId + "/answers");
      if (bridge.runId() === runId) renderAnswers(items);
    }
    function resetRun(scene) {
      loadEpoch++; observationQuery++; evidenceQuery++;
      $("evidence-dialog").close();
      replay = null; replayRun = null;
      $("answers-list").replaceChildren(); $("ask-text").value = "";
      $("evidence-results").replaceChildren(); $("evidence-search").value = "";
      $("report-reading").hidden = $("report-source").hidden = true;
      $("report-versions").hidden = $("report-versions-label").hidden = true;
      $("replay-empty").hidden = false; $("replay-content").hidden = true;
      $("compare-box").open = false; $("compare-result").replaceChildren(); $("branch-title").value="";
      $("ask-actor").replaceChildren(new Option("全局记录分析",""), ...scene.entities.map(a => new Option(a.name,a.id)));
    }
    async function loadReplay(requested = {}) {
      const id = bridge.runId();
      if (!id) return;
      const epoch = ++loadEpoch;
      const data = await api(runPath() + "/timeline");
      if (bridge.runId() !== id || epoch !== loadEpoch) return;
      const previous = requested.window ?? (replayRun === id && $("replay-window").value !== $("replay-window").max ? $("replay-window").value : null);
      replay = data; replayRun = id;
      const slider = $("replay-window");
      slider.max = String(data.windows.length - 1);
      slider.value = previous ?? slider.max;
      const fields = $("replay-field"), priorField = fields.value;
      fields.replaceChildren(...data.series.map((s,i) => new Option(s.actor_name + " / " + s.field_id, String(i))));
      if (priorField && fields.options[Number(priorField)]) fields.value = priorField;
      const actors = $("replay-actor"), priorActor = requested.actor || actors.value;
      actors.replaceChildren(...data.actors.map(a => new Option(a.name + " · L" + a.depth, a.id)));
      if (data.actors.some(a => a.id === priorActor)) actors.value = priorActor;
      $("replay-counts").textContent = data.statistics.expressions + " 次表达 · " + data.statistics.messages + " 次投递 · 仅统计已提交窗口";
      $("replay-empty").hidden = true; $("replay-content").hidden = false;
      drawReplay();
    }
    function drawReplay() {
      if (!replay) return;
      const item = replay.windows[Number($("replay-window").value)];
      $("compare-result").replaceChildren();
      $("replay-title").textContent = item.window ? "第 " + item.window + " 个窗口" : "初始状态";
      const changes = $("replay-changes"); changes.replaceChildren();
      for (const c of item.consequences) {
        const node = make("p","",c.text + " ");
        for (const id of c.action_ids) node.append(reference(id));
        changes.append(node);
      }
      for (const c of item.changes) {
        changes.append(make("p","change-line",bridge.actorName(c.entity_id) + " / " + c.field_id + "　" + describeValue(c.before) + " → " + describeValue(c.after)), make("p","field-help",c.reason));
      }
      for (const change of item.changes) for (const ref of change.influence_refs || []) {
        const edge = replay.influences.find(e=>e.source===ref.source && e.target===ref.target);
        const row=make("p","quote","世界归因 · "+bridge.actorName(ref.source)+" → "+bridge.actorName(ref.target)+" · 权重 "+edge?.weight+"　"+ref.explanation+" ");
        for(const eid of ref.action_ids)row.append(reference(eid));changes.append(row);
      }
      (item.suggested_fields||[]).forEach((suggestion,index)=>{
        const row=make("div","suggestion");row.append(make("p","",suggestion),action("审阅并定义字段",()=>adoptField(item,index),"text-button"));changes.append(row);
      });
      if (!item.changes.length) changes.append(make("p","field-help",item.window ? "本窗口未更改已定义字段。" : "初始值来自场景配置。"));
      changes.append(reference(item.event_id));
      const receipts = replay.provider_usage;
      $("provider-usage").textContent = receipts.attempts ? "已记录 " + receipts.attempts + " 次传输尝试，" + receipts.with_usage + " 次附用量。传输方式 " + JSON.stringify(receipts.transports) + "。接口返回的计数为 " + JSON.stringify(receipts.reported) : "此运行没有逐次传输回执。离线演示与旧版记录可在事件页核对调用方式。";
      drawRoutes();
      drawSeries();
      loadObservation().catch(onError);
    }
    let routeLimit = 6;
    function drawRoutes() {
      if (!replay) return;
      const self = $("replay-actor").value, win = Number($("replay-window").value);
      if (!$("panel-replay").hidden && bridge.runId()===replayRun) {
        const url=new URL(location.href);url.searchParams.set("window",String(win));url.searchParams.set("actor",self);history.replaceState(null,"",url);
      }
      const mode=$("route-mode").value;
      const graph=window.ChuantuoRoutes.neighborhood(replay,self,win,mode,$("route-search").value);
      const focus=id=>{$("replay-actor").value=id;routeLimit=6;drawRoutes();loadObservation().catch(onError);};
      window.ChuantuoRoutes.draw($("route-canvas"),graph,mode,routeLimit,focus);
      $("route-count").textContent="流入 "+graph.incoming.length+" 个关系对象，流出 "+graph.outgoing.length+" 个。点击主体可切换视角。";
      $("route-more").hidden=Math.max(graph.incoming.length,graph.outgoing.length)<=routeLimit;
      for (const inbound of [true,false]) {
        const list = $(inbound ? "route-in" : "route-out"); list.replaceChildren();
        for (const item of (inbound ? graph.incoming : graph.outgoing).slice(0,routeLimit)) {
          const id=item.actor.id, edge=item.edge, route=item.messages.length?{event_ids:item.messages}:null;
          const row = make("div","route-row");
          const peer = action(bridge.actorName(id), () => {
            $("replay-actor").value=id; drawRoutes(); return loadObservation();
          }, "text-button");
          const weight = edge ? ("权重 " + edge.weight + (edge.origin === "ai_assumption" ? " · AI 估算" : "")) : "未设影响边";
          row.append(peer, make("span","field-help",weight + " · " + (route?.event_ids.length || 0) + " 条消息"));
          if (route) {
            const links = make("details"); links.append(make("summary","","核对消息"));
            for (const eid of route.event_ids) links.append(reference(eid));
            row.append(links);
          }
          list.append(row);
        }
        if (!list.childElementCount) list.append(make("p","field-help","此方向没有预设关系或已投递消息。"));
      }
    }
    function adoptField(item, index) {
      const scene=bridge.scene(),run=bridge.runId(),dialog=$("evidence-dialog"),body=$("evidence-body");
      $("evidence-title").textContent="审阅世界提出的字段建议";body.replaceChildren(make("p","quote",item.suggested_fields[index]));
      const form=make("form","adopt-field-form");
      const input=(label,node)=>{
        const wrap=make("label","",label);wrap.append(node);form.append(wrap);return node;
      };
      const select=(values)=>{const s=make("select");s.append(...values.map(([id,name])=>new Option(name,id)));return s;};
      const actor=input("所属主体",select(scene.entities.map(a=>[a.id,a.name])));
      const domain=input("领域",select(scene.domains.map(d=>[d,d])));
      const field=input("新字段编号",make("input"));field.required=true;field.placeholder="例如 blocked_steps";
      const kind=input("字段类型",select([["number","数值"],["string","文字"],["boolean","布尔值"]]));
      const unit=input("单位（可选）",make("input"));
      const unknown=make("input");unknown.type="checkbox";unknown.checked=true;input("初值未知",unknown);
      const initial=input("初始值",make("input")), bool=input("布尔初始值",select([["true","是"],["false","否"]]));
      function controls(){initial.parentElement.hidden=unknown.checked||kind.value==="boolean";bool.parentElement.hidden=unknown.checked||kind.value!=="boolean";}
      unknown.addEventListener("change",controls);kind.addEventListener("change",controls);controls();
      const shared=make("input");shared.type="checkbox";input("公开此字段（默认仅所属主体可见）",shared);
      const submit=make("button","primary","采纳并建立新项目");submit.type="submit";form.append(submit);
      const message=make("p","inline-error");message.setAttribute("role","alert");form.append(message);
      form.addEventListener("submit",async e=>{
        e.preventDefault();submit.disabled=true;message.textContent="";
        try{
          let value=null;
          if(!unknown.checked){
            if(kind.value==="number"){value=Number(initial.value);if(!initial.value.trim()||!Number.isFinite(value))throw new Error("请输入有效数值，或勾选初值未知。");}
            else value=kind.value==="boolean" ? bool.value==="true" : initial.value;
          }
          const saved=await api("/api/runs/"+run+"/adopt-field",{window:item.window,suggestion_index:index,entity_id:actor.value,
            field:{id:field.value.trim(),kind:kind.value,domain:domain.value,unit:unit.value.trim()||null,value,visibility:{public:shared.checked}}});
          dialog.close();await openProject(saved.id,saved.version);
        }catch(error){message.textContent=error.message;}finally{submit.disabled=false;}
      });
      body.append(make("p","field-help","建议原文将保存在新项目中。你指定的字段在新项目开局时生效。"),form);
      if(!dialog.open)dialog.showModal();
    }
    function drawSeries() {
      const root = $("field-series"); root.replaceChildren();
      const series = replay?.series[Number($("replay-field").value)];
      if (!series) { root.append(make("p","empty-state","场景没有已定义的状态字段。")); return; }
      const selected = Number($("replay-window").value), current = series.points[selected];
      root.append(make("strong","field-current",describeValue(current.value) + (series.unit ? " " + series.unit : "")), make("p","field-help","窗口 " + selected + " · " + series.domain));
      if (series.kind === "number" && series.points.some(p => p.value !== null)) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg","svg");
        svg.setAttribute("viewBox","0 0 300 70"); svg.setAttribute("role","img"); svg.setAttribute("aria-label","数值随窗口变化，空缺表示未知");
        const values = series.points.filter(p => p.value !== null).map(p => p.value);
        const min = Math.min(...values), max = Math.max(...values);
        let path = "", move = true;
        for (const point of series.points) {
          if (point.value === null) { move = true; continue; }
          const x = 7 + point.window * 286 / Math.max(1,series.points.length-1);
          const y = max === min ? 35 : 63 - (point.value-min)*56/(max-min);
          path += (move ? "M" : "L") + x.toFixed(2) + "," + y.toFixed(2) + " "; move = false;
          const dot = document.createElementNS(svg.namespaceURI,"circle");
          dot.setAttribute("cx",String(x)); dot.setAttribute("cy",String(y)); dot.setAttribute("r",point.window === selected ? "4.5" : "2"); svg.append(dot);
        }
        const line = document.createElementNS(svg.namespaceURI,"path"); line.setAttribute("d",path); svg.prepend(line); root.append(svg);
      }
      const details = make("details"); details.append(make("summary","", "逐窗数值与依据"));
      for (const p of series.points) { const row=make("p","series-row","窗口 "+p.window+"　"+describeValue(p.value)+" "); row.append(reference(p.event_id)); details.append(row); }
      root.append(details);
    }
    async function loadObservation() {
      const seq = ++observationQuery, actor = $("replay-actor").value;
      if (!replay || !actor) return;
      const target = $("actor-observation"), win = $("replay-window").value;
      $("actor-export").href=runPath()+"/actors/"+encodeURIComponent(actor)+"/export?through="+win;
      const data = await api(runPath() + "/actors/" + encodeURIComponent(actor) + "/observation?window=" + win);
      if (seq !== observationQuery) return;
      target.replaceChildren();
      const obs = data.observation;
      if (!obs) { target.append(make("p","field-help","此时还没有该主体的观察记录。")); return; }
      const v = obs.data.visible;
      target.append(make("p","field-help","窗口 " + win + " · 第 " + v.round + " 轮行动前，最近一次观察"), reference(obs.id));
      for (const [owner, fields] of Object.entries(v.visible_state)) {
        target.append(make("p","",bridge.actorName(owner) + " · " + Object.entries(fields).map(([k,f]) => k + " = " + describeValue(f.value)).join("，")));
      }
      for (const m of v.messages) target.append(make("p","quote",bridge.actorName(m.from) + " → " + data.actor.name + "　" + m.text));
    }
    $("branch-create").addEventListener("click",async()=>{
      const id=bridge.runId(); if(!id)return; $("branch-create").disabled=true;
      try {
        const project=await api(runPath()+"/branch",{window:Number($("replay-window").value), title:$("branch-title").value});
        await openProject(project.id,project.version);
      } catch(error) { onError(error); }
      finally { $("branch-create").disabled=false; }
    });
    $("compare-box").addEventListener("toggle",async()=>{
      if(!$("compare-box").open)return;
      $("compare-go").disabled=true;
      const id=bridge.runId();
      try {
        const result=await api("/api/runs"); if(id!==bridge.runId())return;
        const select=$("compare-run");
        select.replaceChildren(...result.runs.filter(r=>r.run_id!==id).map(r=>new Option(r.title+" · "+r.run_id+" · "+r.window+" 窗口",r.run_id)));
        $("compare-go").disabled=!select.options.length;
        if(!select.options.length)$("compare-result").textContent="尚无其他运行。可以先建立对照项目，再启动新推演。";
      } catch(error){onError(error);}
    });
    $("compare-go").addEventListener("click",async()=>{
      const id=bridge.runId(), other=$("compare-run").value, left=$("replay-window").value, right=$("compare-window").value;
      const params=new URLSearchParams({left_window:left}); if(right!=="")params.set("right_window",right);
      try {
        const result=await api(runPath()+"/compare/"+encodeURIComponent(other)+"?"+params);
        if(id!==bridge.runId()||other!==$("compare-run").value||left!==$("replay-window").value||right!==$("compare-window").value)return;
        const root=$("compare-result"), notes=make("p","field-help","本场窗口 "+result.left.window+"（"+result.left.duration+"）与对方窗口 "+result.right.window+"（"+result.right.duration+"）。");
        notes.append(reference(result.left.event_id,id),reference(result.right.event_id,other));root.replaceChildren(notes);
        const wrap=make("div","table-scroll"), table=make("table","edge-table"), header=table.createTHead().insertRow();
        for(const title of ["主体 / 字段","本场","对方","对方减本场"])header.append(make("th","",title));
        const body=table.createTBody(), fmt=f=>f ? describeValue(f.value)+(f.unit ? " "+f.unit : "") : "未定义";
        const labels={incompatible:"口径不同",unknown:"值未知",undefined:"字段未对齐",equal:"相同",different:"不同"};
        for(const r of result.rows){
          const row=body.insertRow();
          for(const v of [r.actor_id+" / "+r.field_id,fmt(r.left),fmt(r.right),r.delta_text??labels[r.status]])row.insertCell().textContent=String(v);
        }
        wrap.append(table);root.append(wrap,make("p","field-help",result.note));
      }catch(error){onError(error);}
    });
    $("import-package").addEventListener("click",()=>$("package-file").click());
    $("package-file").addEventListener("change",async()=>{
      const file=$("package-file").files?.[0]; if(!file)return;
      $("import-package").disabled=true;
      try {
        const form=new FormData(); form.append("file",file);
        const result=await api("/api/project-import",form);
        await openProject(result.project.id,result.project.version);
        notice("已作为独立项目导入，恢复 "+result.installed_assets+" 份原件，复用 "+result.reused_assets+" 份已校验原件。");
      } catch(error) { onError(error); }
      finally {$("import-package").disabled=false;$("package-file").value="";}
    });
    let searchEpoch=0;
    $("evidence-search-form").addEventListener("submit", async event => {
      event.preventDefault();
      const query = $("evidence-search").value.trim(), actor = $("actor-filter").value, id = bridge.runId();
      const list = $("evidence-results"), epoch=++searchEpoch;
      if (!query || !id) { list.replaceChildren(make("p","field-help",id ? "请输入关键词。" : "请先打开推演。")); return; }
      try {
        const params = new URLSearchParams({q:query}); if (actor) params.set("actor_id",actor);
        const result = await api(runPath()+"/evidence?"+params);
        if (epoch!==searchEpoch || id!==bridge.runId() || actor!==$("actor-filter").value) return;
        list.replaceChildren(make("p","field-help","匹配 "+result.matched_total+" 个片段，可检索 "+result.eligible_total+" 个片段。"));
        if (!result.matched_total) return;
        for (const item of result.evidence.filter(e=>e.matched)) {
          const card=make("article","evidence-hit");
          card.append(reference(item.id),make("span","field-help",item.locator?.name || ("窗口 "+item.window+" · "+item.kind)));
          card.append(make("p","",item.excerpt.slice(0,420)));
          const source=item.locator?.source_id ? bridge.sourceOf(item.locator.source_id) : null;
          if (source) card.append(action("定位到材料原文",()=>showSource(source,{page:item.locator.page,quote:item.excerpt}),"text-button"));
          list.append(card);
        }
      } catch(error) { onError(error); }
    });
    $("actor-filter").addEventListener("change",()=>{searchEpoch++;$("evidence-results").replaceChildren();});
    $("show-library").addEventListener("click", () => library().catch(onError));
    $("library-refresh").addEventListener("click", () => library().catch(onError));
    $("library-search").addEventListener("input",renderLibrary);
    $("replay-window").addEventListener("input",drawReplay);
    $("replay-field").addEventListener("change",drawSeries);
    $("route-mode").addEventListener("change",()=>{routeLimit=6;drawRoutes();});
    $("route-search").addEventListener("input",()=>{routeLimit=6;drawRoutes();});
    $("route-more").addEventListener("click",()=>{routeLimit+=6;drawRoutes();});
    $("replay-actor").addEventListener("change",() => {drawRoutes();loadObservation().catch(onError);});
    $("report-versions").addEventListener("change", async () => {
      const id = bridge.runId(), eid = $("report-versions").value;
      try {
        const data = await api(runPath() + "/reports/" + eid);
        if (id === bridge.runId() && eid === $("report-versions").value) bridge.displayReport(data);
      } catch (error) { onError(error); }
    });
    return {saveProject, openProject, setProject, library, job, pending, resumeJob, clearJob, resetRun,
      renderReport, reportList, answers, loadReplay, reference, showEvidence, showSource, restoreTask:bridge.restoreTask};
  }
  window.ChuantuoStudio = {create};
})();
