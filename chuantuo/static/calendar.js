"use strict";

function eventAudience(event, actors) {
  if (event.visibility?.public) return "通知所有主体";
  const names=(event.visibility?.readers || []).map(id=>actors.find(a=>a.id===id)?.name || id);
  return names.length ? "通知"+names.join("、") : "仅世界系统知情";
}

function eventCard(event, actors, cancelled=false) {
  const card=h("article",null,"cohort");
  const origin=({user:"用户设定",material:"文稿原文",ai_assumption:"模拟假设",missing:"资料待补"})[event.provenance?.kind] || "用户设定";
  card.append(h("small",(cancelled ? "原定" : "")+windowName(event.window)+" · "+eventAudience(event,actors)+" · "+origin,"quiet"),h("p",event.text));
  for(const set of event.assignments || []) {
    const actor=actors.find(a=>a.id===set.entity_id);
    const field=Array.isArray(actor?.fields) ? actor.fields.find(f=>f.id===set.field_id) : actor?.fields?.[set.field_id];
    card.append(h("p",(cancelled ? "原定窗末更新" : "窗末结算")+" · "+(actor?.name || set.entity_id)+"的"+(field?.label || set.field_id)+" → "+watchValue(set,field?.unit),"hint"));
  }
  return card;
}

function draftCalendar(scene) {
  const panel=h("div"), events=scene.scheduled_events || [];
  if(!events.length)panel.append(h("p","可安排明确会发生的外部变化，例如某一窗关闭预约通道。","hint"));
  for(const e of [...events].sort((a,b)=>a.window-b.window)) {
    const card=eventCard(e,scene.entities);
    for(const quote of e.provenance?.quotes || [])card.append(h("small","原文：“"+quote.quote+"”","quiet"));
    card.append(button("移除这项安排",async()=>{
      const old=scene.scheduled_events;scene.scheduled_events=old.filter(v=>v.id!==e.id);
      try{await saveProject("移除外部事件");renderDraft();}catch(err){scene.scheduled_events=old;throw err;}
    },"link"));panel.append(card);
  }
  panel.append(button("安排外部事件",()=>scheduleEvent(scene),"link"));
  for(const r of scene.rules.filter(r=>r.active_until))panel.append(h("p","第 "+(r.active_from||1)+" 至 "+r.active_until+" 窗 · "+r.text,"hint"));
  if(scene.rules.length)panel.append(button("调整一条规则的有效期",()=>editRuleTerm(scene),"link"));return panel;
}

function editRuleTerm(scene){
  const form=h("form",null,"experiment-form"),rule=h("select"),start=h("input"),end=h("input"),msg=h("p",null,"form-error");
  const label=(text,input)=>{const l=h("label",text);l.append(input);form.append(l);};
  scene.rules.forEach((r,i)=>rule.append(new Option(r.text.slice(0,100),i)));label("选择场景规则",rule);
  start.type=end.type="number";start.min=end.min="1";start.required=true;end.placeholder="持续有效";
  label("第一有效窗口",start);label("最后有效窗口（可留空）",end);const info=h("p",null,"hint");
  function load(){const r=scene.rules[Number(rule.value)];start.value=r.active_from||1;end.value=r.active_until||"";end.min=start.value;info.textContent=eventAudience(r,scene.entities);}
  rule.onchange=load;start.oninput=()=>{end.min=start.value||"1";};load();const go=h("button","保存有效期","primary");go.type="submit";
  form.append(info,msg,go);form.onsubmit=async e=>{
    e.preventDefault();if(go.disabled)return;go.disabled=true;const i=Number(rule.value),old=scene.rules[i];
    scene.rules[i]={...old,active_from:Number(start.value),active_until:end.value ? Number(end.value) : null,
      provenance:{...old.provenance,kind:"user",explanation:"操作者设定此规则的有效窗口。"}};
    try{await saveProject("修改规则有效期");$("reading").close();renderDraft();}catch(ex){scene.rules[i]=old;msg.textContent=ex.message;}finally{go.disabled=false;}
  };reading("规则的有效窗口",form);
}

function scheduledValuePicker(actors, optional) {
  const node=h("fieldset",null,"value-picker"), fields=[], field=h("select"), targetWrap=h("div");
  let opening=false;
  const label=(name,control)=>{const tag=h("label",name);control.setAttribute("aria-label",name);tag.append(control);return tag;};
  const all=[];
  for(const actor of actors) {
    const entries=Array.isArray(actor.fields) ? actor.fields : Object.entries(actor.fields).map(([id,f])=>({id,...f}));
    for(const f of entries)all.push({actor:actor.id,actorName:actor.name,...f});
  }
  function options() {
    const prior=fields[Number(field.value)];
    fields.length=0;field.replaceChildren();if(optional)field.append(new Option("只通知，由主体和世界自行回应",""));
    for(const f of all.filter(f=>opening || !f.constraints?.fixed)) {
      field.append(new Option(f.actorName+" · "+(f.label||f.id),fields.length));fields.push(f);
      if(prior===f)field.value=String(fields.length-1);
    }
  }
  let target, unknown;
  function redraw() {
    targetWrap.replaceChildren();target=unknown=null;if(field.value==="")return;
    const f=fields[Number(field.value)],options=f.kind==="boolean" ? [[true,"是"],[false,"否"]] : f.constraints?.choices?.map(v=>[v,v]);
    if(options?.length) {target=h("select");options.forEach(([v,l])=>target.append(new Option(l,String(v))));}
    else {
      target=h("input");target.required=true;target.type=f.kind==="number" ? "number" : "text";
      if(f.kind==="number"){target.step="any";if(f.constraints?.minimum!=null)target.min=f.constraints.minimum;if(f.constraints?.maximum!=null)target.max=f.constraints.maximum;}
    }
    targetWrap.append(label((opening?"开局初值":"窗末目标值")+(f.unit ? "（"+f.unit+"）" : ""),target));
    if(f.constraints?.allow_unknown!==false){
      unknown=h("input");unknown.type="checkbox";const line=h("label",null,"check");
      line.append(unknown,document.createTextNode("明确设为未知"));unknown.onchange=()=>{target.disabled=unknown.checked;};targetWrap.append(line);
    }
  }
  field.onchange=redraw;options();redraw();
  node.append(label(optional ? "预定字段值（可选）" : "要改变的字段",field),targetWrap);
  return {node,count:fields.length,openingCount:all.length,setOpening(value){
    if(value!==opening){opening=value;options();redraw();}
  },selected:()=>field.value==="" || unknown?.checked ? null : fields[Number(field.value)],set(value){
    if(!value)return;
    const index=fields.findIndex(f=>f.actor===value.entity_id && f.id===value.field_id);
    if(index<0)return;field.value=String(index);redraw();
    if(unknown){unknown.checked=value.value===null;unknown.onchange();}
    if(target && value.value!==null)target.value=String(value.value);
  },read(){
    if(!target)return null;
    const f=fields[Number(field.value)];let value=target.value;
    if(unknown?.checked)value=null;
    else if(!target.checkValidity())throw new Error("请填写符合字段约束的目标值。");
    else if(f.kind==="number") {
      value=Number(value);if (!Number.isFinite(value)||(Number.isInteger(value)&&!Number.isSafeInteger(value)))throw new Error("请输入可精确表示的有限数值。");
    } else if(f.kind==="boolean")value=value==="true";
    return {entity_id:f.actor,field_id:f.id,value};
  }};
}

function scheduleEvent(scene) {
  const form=h("form",null,"advanced"), w=h("input"), text=h("textarea",null,"editor"), scope=h("select"), recipients=h("fieldset");
  const picker=scheduledValuePicker(scene.entities,true),error=h("p",null,"form-error");
  const labelled=(name,control)=>{const l=h("label",name);control.setAttribute("aria-label",name);l.append(control);return l;};
  w.type="number";w.min="1";w.step="1";w.value="1";w.required=true;
  text.maxLength=1600;text.required=true;text.placeholder="例如：本窗开始，场馆发布限电通知。";
  scope.append(new Option("仅世界系统","world"),new Option("通知所有主体","public"),new Option("通知指定主体","readers"));
  recipients.append(h("legend","接收通知的主体")); recipients.hidden=true;
  scope.onchange=()=>{recipients.hidden=scope.value!=="readers";};
  for(const actor of scene.entities) {
    const l=h("label",null,"check"),box=h("input");box.type="checkbox";box.value=actor.id;l.append(box,document.createTextNode(actor.name));recipients.append(l);
  }
  const submit=h("button","保存事件","primary");submit.type="submit";
  form.append(labelled("发生窗口",w),labelled("发生什么",text),labelled("谁会在窗首收到通知",scope),recipients,
    picker.node,error,submit);
  form.onsubmit=async ev=>{
    ev.preventDefault();if(submit.disabled)return;
    const readers=scope.value==="readers" ? [...recipients.querySelectorAll("input:checked")].map(b=>b.value) : [];
    if(scope.value==="readers" && !readers.length){error.textContent="请选择通知对象。";return;}
    let assignment;
    try{assignment=picker.read();}catch(err){error.textContent=err.message;return;}
    const next={id:"event_"+uid().slice(0,12),window:Number(w.value),text:text.value.trim(),visibility:{public:scope.value==="public",readers},
      assignments:assignment ? [assignment] : [],provenance:{kind:"user",explanation:"用户在场景准备中安排。"}};
    const old=scene.scheduled_events || [];scene.scheduled_events=[...old,next];submit.disabled=true;error.textContent="";
    try {await saveProject("安排外部事件");$("reading").close();renderDraft();notice("外部事件已保存。");}
    catch(err){scene.scheduled_events=old;error.textContent=err.message;}finally{submit.disabled=false;}
  };
  reading("安排外部事件",h("p","指定对象在窗首收到情境通知。若明确指定字段值，引擎会在窗末统一提交，并保留事件出处。","hint"),form);
}

async function openRunCalendar(view) {
  const run=activeRun,ticket=++readingRequest;
  const [rows,terms]=await Promise.all([api("/api/runs/"+run+"/calendar"),view.timed_rule_count ?
    api("/api/runs/"+run+"/timed-rules?window="+view.selected) : Promise.resolve([])]);
  if(run!==activeRun || ticket!==readingRequest)return;
  const body=h("div");
  for(const rule of terms){
    const card=h("article",null,"cohort");card.append(h("small","第 "+rule.start+" 至 "+rule.end+" 窗有效 · "+
      ({upcoming:"尚未生效",active:"当窗有效",expired:"本窗已到期"}[rule.status]),"eyebrow"),h("p",rule.text),
      h("small",eventAudience(rule,view.actors),"quiet"));body.append(card);
  }
  for(const item of rows) {
    const card=eventCard(item.event,view.actors,item.status==="cancelled");
    if(item.status==="cancelled"){
      card.prepend(h("p","已取消的原定安排","eyebrow"));
      card.append(h("p","在"+windowName(item.after_window)+"之后取消，预设通知与字段赋值一并撤销。","hint"),
        button("查看取消依据",()=>evidence(item.cancellation_record,run),"link"));
      body.append(card);continue;
    }
    card.append(h("small",({scheduled:"预定中",pending:"本窗已通知，尚未提交",committed:"已随窗口提交",beyond_run:"运行结束时尚未到预定日期"})[item.status],"quiet"));
    if(item.record)card.append(evidenceLinks([item.record],run));body.append(card);
  }
  reading(terms.length ? "条件与事件日历" : "外部事件日历",h("p","全局观察者可预览全部安排。主体按窗口和知情范围获得条件与通知。","hint"),body);
}
function entryText(period) {
  if(period.closed_before_start)return "的转入期限在本场景开局前已结束";
  const first=period.first || 1;
  if(!period.last && first===1)return "各窗均可转入";
  return period.last ? "第 "+first+" 至 "+period.last+" 窗可转入" : "第 "+first+" 窗起可转入";
}

function draftEntryWindows(scene) {
  const panel=h("div"),fields=[];
  for(const a of scene.entities)for(const f of a.fields)if(f.constraints?.choices?.length)fields.push({a,f});
  if(!fields.length)return h("span");
  for(const {a,f} of fields)for(const p of f.constraints.entry_windows || [])
    panel.append(h("p",a.name+" · "+(f.label || f.id)+"　“"+p.value+"”"+entryText(p),"hint"));
  panel.append(button("编辑某个候选值的期限",()=>{
    const form=h("form",null,"experiment-form"),field=h("select"),value=h("select"),first=h("input"),last=h("input");
    const error=h("p",null,"form-error");
    function label(text,ctl){const l=h("label",text);ctl.setAttribute("aria-label",text);l.append(ctl);form.append(l);}
    field.append(...fields.map(({a,f},i)=>new Option(a.name+" · "+(f.label||f.id),i)));
    first.type=last.type="number";first.min=last.min="1";first.required=true;last.placeholder="不限";
    label("选择文字字段",field);label("转入哪个候选值",value);label("最早转入窗口",first);label("最晚转入窗口",last);
    function show(){const p=fields[Number(field.value)].f.constraints.entry_windows?.find(r=>r.value===value.value);first.value=p?.first||1;last.value=p?.last||"";}
    function options(){value.replaceChildren(...fields[Number(field.value)].f.constraints.choices.map(v=>new Option(v,v)));show();}
    field.onchange=options;value.onchange=show;options();
    const submit=h("button","保存转入期限","primary");submit.type="submit";
    form.append(h("p","期限只约束从其他值转入。已登记的值可以在后面的窗口保持。","hint"),error,submit);
    form.onsubmit=async e=>{e.preventDefault();if(submit.disabled)return;submit.disabled=true;
      const f=fields[Number(field.value)].f,prev=f.constraints.entry_windows||[];
      const p={value:value.value,first:Number(first.value),last:last.value ? Number(last.value) : null,provenance:{kind:"user",explanation:"操作者设定该值的转入期限。"}};
      f.constraints.entry_windows=[...prev.filter(v=>v.value!==p.value),p];
      try{await saveProject("修改候选值的转入期限");$("reading").close();renderDraft();}
      catch(err){f.constraints.entry_windows=prev;error.textContent=err.message;}finally{submit.disabled=false;}
    };
    reading("字段值的转入期限",form);
  },"link"));
  return disclosure("字段值的转入期限",panel);
}
