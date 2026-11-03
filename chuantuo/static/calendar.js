"use strict";

function eventAudience(event, actors) {
  if (event.visibility.public) return "通知所有主体";
  const names=(event.visibility.readers || []).map(id=>actors.find(a=>a.id===id)?.name || id);
  return names.length ? "通知"+names.join("、") : "仅世界系统知情";
}

function eventCard(event, actors) {
  const card=h("article",null,"cohort");
  const origin=({user:"用户设定",material:"文稿原文",ai_assumption:"模拟假设",missing:"资料待补"})[event.provenance?.kind] || "用户设定";
  card.append(h("small",windowName(event.window)+" · "+eventAudience(event,actors)+" · "+origin,"quiet"),h("p",event.text));
  for(const set of event.assignments || []) {
    const actor=actors.find(a=>a.id===set.entity_id);
    const field=Array.isArray(actor?.fields) ? actor.fields.find(f=>f.id===set.field_id) : actor?.fields?.[set.field_id];
    card.append(h("p","窗末结算："+(actor?.name || set.entity_id)+"的"+(field?.label || set.field_id)+" → "+watchValue(set,field?.unit),"hint"));
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
  panel.append(button("安排外部事件",()=>scheduleEvent(scene),"link"));return panel;
}

function scheduleEvent(scene) {
  const form=h("form",null,"advanced"), w=h("input"), text=h("textarea",null,"editor"), scope=h("select"), recipients=h("fieldset");
  const field=h("select"), targetWrap=h("div"), fields=[],error=h("p",null,"form-error");
  const labelled=(name,control)=>{const l=h("label",name);control.setAttribute("aria-label",name);l.append(control);return l;};
  w.type="number";w.min="1";w.step="1";w.value="1";w.required=true;
  text.maxLength=1600;text.required=true;text.placeholder="例如：本窗开始，场馆发布限电通知。";
  scope.append(new Option("仅世界系统","world"),new Option("通知所有主体","public"),new Option("通知指定主体","readers"));
  recipients.append(h("legend","接收通知的主体")); recipients.hidden=true;
  scope.onchange=()=>{recipients.hidden=scope.value!=="readers";};
  field.append(new Option("只通知，由主体和世界自行回应",""));
  for(const actor of scene.entities) {
    const l=h("label",null,"check"),box=h("input");box.type="checkbox";box.value=actor.id;l.append(box,document.createTextNode(actor.name));recipients.append(l);
    for(const f of actor.fields) {field.append(new Option(actor.name+" · "+(f.label||f.id),fields.length));fields.push({actor:actor.id,...f});}
  }
  let target;
  field.onchange=()=>{
    targetWrap.replaceChildren();target=null;if(field.value==="")return;
    const f=fields[Number(field.value)], options=f.kind==="boolean" ? [[true,"是"],[false,"否"]] : f.constraints?.choices?.map(v=>[v,v]);
    if(options?.length) {target=h("select");options.forEach(([v,l])=>target.append(new Option(l,String(v))));}
    else {target=h("input");target.required=true;target.type=f.kind==="number" ? "number" : "text";if(f.kind==="number")target.step="any";}
    targetWrap.append(labelled("窗末目标值"+(f.unit ? "（"+f.unit+"）" : ""),target));
  };
  const submit=h("button","保存事件","primary");submit.type="submit";
  form.append(labelled("发生窗口",w),labelled("发生什么",text),labelled("谁会在窗首收到通知",scope),recipients,
    labelled("预定字段值（可选）",field),targetWrap,error,submit);
  form.onsubmit=async ev=>{
    ev.preventDefault();if(submit.disabled)return;
    const readers=scope.value==="readers" ? [...recipients.querySelectorAll("input:checked")].map(b=>b.value) : [];
    if(scope.value==="readers" && !readers.length){error.textContent="请选择通知对象。";return;}
    const assignments=[];
    if(target) {
      const f=fields[Number(field.value)];
      let value=target.value;
      if(f.kind==="number") {
        value=Number(value);
        if(!Number.isFinite(value)||(Number.isInteger(value)&&!Number.isSafeInteger(value))){error.textContent="请输入可精确表示的有限数值。";return;}
      } else if(f.kind==="boolean")value=value==="true";
      assignments.push({entity_id:f.actor,field_id:f.id,value});
    }
    const next={id:"event_"+uid().slice(0,12),window:Number(w.value),text:text.value.trim(),visibility:{public:scope.value==="public",readers},
      assignments,provenance:{kind:"user",explanation:"用户在场景准备中安排。"}};
    const old=scene.scheduled_events || [];scene.scheduled_events=[...old,next];submit.disabled=true;error.textContent="";
    try {await saveProject("安排外部事件");$("reading").close();renderDraft();notice("外部事件已保存。");}
    catch(err){scene.scheduled_events=old;error.textContent=err.message;}finally{submit.disabled=false;}
  };
  reading("安排外部事件",h("p","指定对象在窗首收到情境通知。若明确指定字段值，引擎会在窗末统一提交，并保留事件出处。","hint"),form);
}

async function openRunCalendar(view) {
  const run=activeRun,ticket=++readingRequest, rows=await api("/api/runs/"+run+"/calendar");
  if(run!==activeRun || ticket!==readingRequest)return;
  const body=h("div");
  for(const item of rows) {
    const card=eventCard(item.event,view.actors);
    card.append(h("small",({scheduled:"预定中",pending:"本窗已通知，尚未提交",committed:"已随窗口提交"})[item.status],"quiet"));
    if(item.record)card.append(evidenceLinks([item.record],run));body.append(card);
  }
  reading("外部事件日历",h("p","全局观察者可预览全部安排。主体只在指定窗口收到对自己可见的通知。","hint"),body);
}
