"use strict";

function applicableQuestions(scene, questions) {
  return questions.filter(q=>scene.entities.some(a=>a.id===q.actor_id && a.fields.some(f=>f.id===q.field_id &&
    (!["at_least","at_most"].includes(q.operator) || f.kind==="number"))));
}

function watchValue(row, unit) {
  if (row.value===null) return "未知";
  if (row.value===true || row.value===false) return row.value ? "是" : "否";
  return (row.numeric_text ?? String(row.value))+(unit||"");
}
const windowName = n => n ? "第 "+n+" 窗" : "开局";

function watchFinding(row) {
  if (row.status==="violated") return windowName(row.first_miss.window)+"首次记录到不满足";
  if (row.status==="observed") return windowName(row.first_hit.window)+"首次记录达成"+(row.matches===false ? "，观察终点已不满足" : row.matches===null ? "，终点数值未知" : "");
  const messages={tracking:row.changes+" 次已提交变化",pending:"截至此窗仍在观察",not_observed:"观察期内未出现目标结果",
    undetermined:"有窗口的值未知，无法判定整个观察期",ended_early:"运行在期限前结束，记录未覆盖整个期限",
    holding:"截至此窗始终满足，继续观察",maintained:"整个观察期始终满足",pending_end:"指定终点尚未到达",
    met_at_end:"观察终点满足条件",missed_at_end:"观察终点不满足条件",undetermined_end:"终点的值未知"};
  return messages[row.status] || "";
}

function fieldWatchCard(view,row,cls="watch-card") {
  const card=h("button",null,cls);card.type="button";
  card.append(h("small",row.actor+" · "+row.label),h("strong",row.question.title || row.label+"走势"),
    h("span",watchValue(row,row.unit)+"　·　"+watchFinding(row)));
  card.onclick=safe(()=>openFieldWatch(view,row.question,row.id));
  return card;
}

function showFieldWatches(view) {
  const root=$("field-watches"), run=activeRun;
  const outcomes=$("question-outcomes");
  const hasFields=view.actors.some(a=>Object.keys(a.fields).length);
  root.hidden=playback() || (!hasFields && view.format_version!=="1.1");
  if (root.hidden) {outcomes.hidden=true;outcomes.replaceChildren();return;}
  const stamp=[run,view.settled_through,view.status.status,json(view.status.pause_goals || [])].join("/");
  if(root.dataset.stamp===stamp && root.childElementCount) return;
  root.dataset.stamp=stamp;
  outcomes.hidden=true;outcomes.replaceChildren();
  const head=h("div",null,"watchlist-head");
  const menu=h("div",null,"actions");head.append(h("h2","观察问题"),menu);
  if(hasFields) menu.append(button("添加关注",()=>openFieldWatch(view),"link"));
  if(view.format_version==="1.1") menu.append(button("主体持续事项",()=>openMatters(view),"link"));
  if(view.scheduled_count || view.timed_rule_count)menu.append(button(view.timed_rule_count ? "条件与事件日历" : "外部事件日历",()=>openRunCalendar(view),"link"));
  if(view.has_mail_routes)menu.append(button("查询在途与到件",()=>openRunMail(view),"link"));
  if(view.document_count)menu.append(button("原件流转",()=>openDocuments(view),"link"));
  const list=h("div",null,"watch-cards"); root.replaceChildren(head,list);
  const goals=view.status.pause_goals || [];
  if(goals.length){
    const panel=h("div",null,"pause-goals");
    panel.append(h("h3","达到目标时暂停"));
    const recent=[...goals.filter(g=>g.status==="watching"),...goals.filter(g=>g.status!=="watching").slice(-3)];
    for(const goal of recent){
      const q=goal.question,actor=view.actors.find(a=>a.id===q.actor_id),field=actor?.fields[q.field_id];
      const relation={equals:"等于",at_least:"至少达到",at_most:"不超过",known:"出现已知值"}[q.operator];
      const condition=(actor?.name || q.actor_id)+" · "+(field?.label || q.field_id)+" "+relation+
        (q.operator==="known" ? "" : " "+watchValue({value:q.target},field?.unit));
      const item=h("div",null,"pause-goal");
      item.append(h("strong",q.title || condition));
      if(q.title)item.append(h("p",condition,"hint"));
      const status={watching:"从"+windowName(goal.armed_after)+"之后开始，在满足条件的窗末暂停一次"+
        (q.deadline===null ? "。" : "，最晚观察到第 "+q.deadline+" 窗。"),
        reached:windowName(goal.window)+"已记录达成",expired:"观察期限已到，期限内没有达成记录",
        cancelled:"已取消自动暂停",ended:"本次运行已结束，尚未记录达成"};
      item.append(h("p",status[goal.status],"hint"));
      const actions=h("div",null,"actions");
      actions.append(button("查看这项观察",()=>openFieldWatch(view,q),"link"));
      if(goal.commit_id)actions.append(button("查看当窗结算",()=>evidence(goal.commit_id,run),"link"));
      if(goal.status==="watching")actions.append(button("取消自动暂停",async()=>{
        await api("/api/runs/"+run+"/pause-goals/"+goal.id,"DELETE");
        if(activeRun===run){delete root.dataset.stamp;lastView="";await refreshWatch(routeGeneration);}
      },"link"));
      item.append(actions);panel.append(item);
    }
    root.insertBefore(panel,list);
  }
  api("/api/runs/"+run+"/watches?through="+view.settled_through).then(rows=>{
    if (!list.isConnected) return;
    if (!rows.length) {list.append(h("p",hasFields ? "关注一个字段，观察它何时达到目标、后来是否回落。" : "这个场景以文字互动为主，可以回看各方记录的持续事项。","hint"));return;}
    const download=h("a","下载观察笔记");download.href="/api/runs/"+run+"/watch-notes?through="+view.settled_through;
    menu.append(download);
    const hasOverview=["completed","stopped"].includes(view.status.status) && view.settled_through>0;
    if(hasOverview){
      const heading=h("div",null,"card-top"),cards=h("div",null,"watch-cards");
      heading.append(h("h2","关注问题的结果"),h("small","截至"+windowName(view.settled_through),"quiet"));
      for(const row of rows.slice(0,3))cards.append(fieldWatchCard(view,row));
      outcomes.append(heading,cards);
      if(rows.length>3)outcomes.append(button("查看其余 "+(rows.length-3)+" 项",()=>root.scrollIntoView({block:"start"}),"link"));
      outcomes.hidden=false;
      head.querySelector("h2").textContent=rows.length>3 ? "其他关注问题" : "观察工具";
    }
    for (const row of hasOverview ? rows.slice(3) : rows) {
      list.append(fieldWatchCard(view,row));
    }
  }).catch(err=>{if(list.isConnected){list.append(h("p",err.message,"error"));delete root.dataset.stamp;}});
}

function stepChart(row) {
  const values=row.segments.filter(s=>typeof s.value==="number" && Number.isFinite(s.value)).map(s=>s.value);
  if (row.kind!=="number" || !values.length) return null;
  const lo=values.reduce((a,b)=>Math.min(a,b)), hi=values.reduce((a,b)=>Math.max(a,b));
  if (!Number.isFinite(hi-lo)) return null;
  const ns="http://www.w3.org/2000/svg", svg=document.createElementNS(ns,"svg");
  svg.setAttribute("viewBox","0 0 500 82");svg.setAttribute("class","watch-chart");svg.setAttribute("role","img");
  svg.setAttribute("aria-label",row.label+"从开局到"+windowName(row.through)+"的阶梯图，未知区间留空");
  const x=n=>8+484*n/Math.max(1,row.through+1), y=v=>hi===lo ? 40 : 71-(v-lo)/(hi-lo)*60;
  let points="", prior=false;
  for (const s of row.segments) {
    if (s.value===null || !Number.isFinite(s.value)) {prior=false;continue;}
    points+=(prior ? " L " : " M ")+x(s.start)+" "+y(s.value)+" H "+x(s.end+1);
    prior=true;
  }
  const line=document.createElementNS(ns,"path");line.setAttribute("d",points);line.setAttribute("fill","none");
  line.setAttribute("stroke","currentColor");line.setAttribute("stroke-width","2.5");svg.append(line);return svg;
}

function openMatters(view) {
  const run=activeRun, body=h("div"), filters=h("div",null,"feed-filters"), actors=h("select"), kinds=h("select"), list=h("div");
  actors.setAttribute("aria-label","查看谁的持续事项"); kinds.setAttribute("aria-label","持续事项状态");
  actors.append(new Option("全部主体",""),...view.actors.map(a=>new Option(a.name,a.id)));
  kinds.append(new Option("全部状态",""),new Option("待处理","open"),new Option("已结清","closed"));
  filters.append(actors,kinds);body.append(filters,list);
  let generation=0;
  async function draw(offset=0) {
    const ticket=++generation;
    const params=new URLSearchParams({through:String(view.settled_through),offset:String(offset)});
    if(actors.value)params.set("actor",actors.value);if(kinds.value)params.set("status",kinds.value);
    const data=await api("/api/runs/"+run+"/matters?"+params);
    if(!body.isConnected || ticket!==generation)return;
    if(!offset) list.replaceChildren(h("p","截至"+windowName(data.through)+"，"+data.open+" 项待处理，"+data.closed+" 项已结清。","hint"));
    list.querySelector(".more-matters")?.remove();
    const verbs={opened:"登记",updated:"修订",closed:"结清"};
    for(const note of data.items) {
      const card=h("article",null,"cohort"), history=h("div");
      card.append(h("small",note.actor+" · "+(note.status==="open" ? "待处理" : "已结清"),"quiet"),h("p",note.text));
      if(note.due_window)card.append(h("small","登记期限为第 "+note.due_window+" 窗。","quiet"));
      for(const step of note.steps) {
        const line=h("div",null,"matter-step");
        line.append(h("strong",windowName(step.window)+" · 第 "+step.round+" 轮 · "+verbs[step.kind]));
        if(step.text)line.append(h("p",step.text));
        if(step.due_window)line.append(h("small","当时约定至第 "+step.due_window+" 窗。","quiet"));
        line.append(button("行动原文",()=>evidence(step.record,run),"link"),
          button("当时掌握的信息",()=>openActorContext({id:note.actor_id,name:note.actor},step.window,step.round,false,run),"link"));
        history.append(line);
      }
      card.append(disclosure("查看事项经过（"+note.steps.length+"）",history));list.append(card);
    }
    if(!data.total)list.append(h("p","这个范围内还没有对应的事项。","hint"));
    if(data.next<data.total)list.append(button("读取更多事项",()=>draw(data.next),"link more-matters"));
  }
  actors.onchange=kinds.onchange=safe(()=>draw());
  reading("主体持续事项",h("p","这里展示主体在自己的备忘中登记、修订或结清的事项。实际交付与客观状态仍以窗口结算为准。","hint"),body);
  safe(()=>draw())();
}

function showWatchTrace(root,row,run) {
  const banner=h("div",null,"watch-finding");banner.append(h("strong",watchFinding(row)),h("p",windowName(row.through)+"的"+row.label+"为"+watchValue(row,row.unit)+"。"));
  if (row.status==="observed") banner.append(button("看首次达成的依据",()=>evidence(row.first_hit.record,run),"link"));
  if (row.status==="violated") banner.append(button("看首次不满足的依据",()=>evidence(row.first_miss.record,run),"link"));
  if (row.unknown_windows) banner.append(h("small",row.unknown_windows+" 个观察时点的值未知（含开局）。","quiet"));
  root.replaceChildren(banner);
  if(row.baseline) {
    const b=row.baseline, comparison=h("p",null,"watch-baseline");
    comparison.append("同一时期的原路线："+watchValue(b,b.unit)+"，"+watchFinding(b)+"。 ",
      button("原路线依据",()=>evidence(b.status==="violated" ? b.first_miss.record : b.status==="observed" ? b.first_hit.record : b.record,b.run_id),"link"));root.append(comparison);
  }
  const chart=stepChart(row);if(chart)root.append(chart);
  const trace=h("div",null,"watch-trace");let visible=8;
  const draw=()=>{
    trace.replaceChildren(h("h3","状态经过"));
    for (const segment of row.segments.slice(-visible).reverse()) {
      const card=h("div",null,"trace-row"), info=h("div");
      info.append(h("small",windowName(segment.start)+(segment.end===segment.start ? "" : " 至 "+windowName(segment.end)),"quiet"),
        h("strong",watchValue(segment,row.unit)));
      if (segment.reason) info.append(h("p",segment.reason));
      if (segment.events?.length)info.append(h("small","外部事件依据","quiet"),evidenceLinks(segment.events,run));
      card.append(info,button("查看依据",()=>evidence(segment.record,run),"link"));trace.append(card);
    }
    if (row.segments.length>visible) trace.append(button("显示更早的变化",()=>{visible+=16;draw();},"link"));
  };draw();root.append(trace);
}

function openFieldWatch(view, preset={}, watchId=null) {
  const run=activeRun, fieldData=[], body=h("div"), form=h("form",null,"watch-form"), result=h("div");
  const field=h("select"), op=h("select"), criterion=h("select"), targetBox=h("div"), deadline=h("input"), title=h("input"), error=h("p",null,"form-error");
  const label=(name,control)=>{const l=h("label",name);control.setAttribute("aria-label",name);l.append(control);return l;};
  for(const a of view.actors) {
    const group=document.createElement("optgroup");group.label=a.name;
    for(const [id,f] of Object.entries(a.fields)) {group.append(new Option(f.label,fieldData.length));fieldData.push({actor:a.id,id,...f});}
    if(group.children.length)field.append(group);
  }
  if(!fieldData.length) return;
  field.selectedIndex=Math.max(0,fieldData.findIndex(f=>f.actor===preset.actor_id && f.id===preset.field_id));
  title.maxLength=160;title.placeholder="例如：申请能在期限前进入复核吗？";title.value=preset.title||"";
  deadline.type="number";deadline.min="0";deadline.step="1";deadline.placeholder="不限";deadline.value=preset.deadline??"";
  criterion.append(new Option("曾经达到","ever"),new Option("始终保持","always"),new Option("只看终点","at_end"));criterion.value=preset.criterion || "ever";
  const criterionLabel=label("判断口径",criterion),deadlineLabel=label("观察到第几窗（可选）",deadline), actions=h("div",null,"actions");
  const preview=h("button","查看结果");preview.type="submit";
  const store=button(watchId ? "保存修改" : "固定到观察台",()=>save(),"primary");
  const pauseId=uid();
  const pauseAt=button("达到这个条件时暂停",async()=>{
    pauseAt.disabled=true;error.textContent="";
    try{await api("/api/runs/"+run+"/pause-goals/"+pauseId,"PUT",spec());await finish();}
    catch(err){error.textContent=err.message;}finally{pauseAt.disabled=false;}
  });
  pauseAt.title="从当前已结算窗口之后开始关注，在满足条件的窗末暂停一次。";
  actions.append(preview,store,pauseAt);
  if(watchId) actions.append(button("移除关注",async()=>{
    await api("/api/runs/"+run+"/watches/"+watchId,"DELETE");await finish();
  },"link"));
  let target, ticket=0;
  function operators(preferred) {
    const f=fieldData[field.selectedIndex];
    const list=[["track","追踪变化"],["equals","等于"],["known","有已知值"]];
    if(f.kind==="number") list.push(["at_least","至少达到"],["at_most","不超过"]);
    op.replaceChildren(...list.map(([v,l])=>new Option(l,v)));
    op.value=list.some(([v])=>v===preferred) ? preferred : "track";
  }
  function targetControl(value) {
    const f=fieldData[field.selectedIndex], choices=f.constraints?.choices;
    if(f.kind==="boolean" || choices?.length) {
      target=h("select");const items=f.kind==="boolean" ? [["true","是"],["false","否"]] : choices.map(c=>[c,c]);
      target.append(...items.map(([v,l])=>new Option(l,v))); if(value!=null)target.value=String(value);
    } else {
      target=h("input");target.type="text";target.maxLength=1200;target.value=value==null ? "" : String(value);
      if(f.kind==="number") target.inputMode="decimal";
    }
    targetBox.replaceChildren(label("目标值"+(f.unit ? "（"+f.unit+"）" : ""),target));
    targetBox.hidden=["track","known"].includes(op.value);criterionLabel.hidden=deadlineLabel.hidden=op.value==="track";
    updatePause();
  }
  function updatePause(){
    pauseAt.hidden=op.value==="track" || criterion.value!=="ever" ||
      ["completed","stopped"].includes(view.status.status) || Boolean(view.status.experiment?.study);
  }
  function spec() {
    const f=fieldData[field.selectedIndex];
    const raw=targetBox.hidden ? null : target.value;
    if(f.kind==="number" && raw!==null && !raw.trim())throw new Error("请填写目标数值。");
    if(!deadlineLabel.hidden && !deadline.validity.valid)throw new Error("观察期限需要是非负整数。");
    return {actor_id:f.actor,field_id:f.id,title:title.value.trim(),operator:op.value,criterion:criterionLabel.hidden ? "ever" : criterion.value,
      target:raw===null ? null : f.kind==="boolean" ? raw==="true" : raw,
      deadline:deadlineLabel.hidden || deadline.value==="" ? null : Number(deadline.value)};
  }
  async function evaluate() {
    const id=++ticket;error.textContent="";
    try {
      const data=await api("/api/runs/"+run+"/field-observation?through="+view.settled_through,"POST",spec());
      if(id===ticket && body.isConnected)showWatchTrace(result,data,run);
    }catch(err){if(id===ticket){error.textContent=err.message;result.replaceChildren();}}
  }
  async function finish() {
    if(activeRun!==run)return;
    $("reading").close();delete $("field-watches").dataset.stamp;lastView="";await refreshWatch(routeGeneration);
  }
  const id=watchId || uid();
  async function save() {
    store.disabled=true;
    try {await api("/api/runs/"+run+"/watches/"+id,"PUT",spec());await finish();}
    catch(err){error.textContent=err.message;}finally{store.disabled=false;}
  }
  field.onchange=()=>{operators("track");targetControl(null);evaluate();};
  op.onchange=()=>{targetControl(null);ticket++;result.replaceChildren();if(targetBox.hidden)evaluate();};
  criterion.onchange=updatePause;
  form.oninput=e=>{if(e.target!==title){ticket++;result.replaceChildren();}};
  form.onsubmit=event=>{event.preventDefault();evaluate();};
  operators(preset.operator);targetControl(preset.target);
  const settings=h("div",null,"watch-fields");settings.append(label("观察字段",field),label("观察方式",op),targetBox,criterionLabel,deadlineLabel);
  form.append(settings,label("问题名称（可选）",title),actions,error);body.append(form,result);
  reading("字段观察",h("p","只使用所选窗口及以前的已提交记录。观察问题保存在本机，不会发给主体。","hint"),body);evaluate();
}
