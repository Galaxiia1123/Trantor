"use strict";
(() => {
  const data=JSON.parse(document.getElementById("replay").textContent);
  const root=document.getElementById("root"),dialog=document.getElementById("reader"),record=document.getElementById("record");
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;};
  const button=(text,fn)=>{const b=el("button",text,"link");b.type="button";b.addEventListener("click",fn);return b;};
  const details=(text,child)=>{const d=el("details");d.append(el("summary",text),child);return d;};
  const display=(value,unit)=>value===null?"未知":typeof value==="boolean"?(value?"是":"否"):String(value?.exact_integer??value)+(unit||"");
  const routes=data.routes.map(route=>{
    const byId=new Map(route.events.map(e=>[e.id,e])),windows=new Map(),commits=new Map(),contexts=new Map(),lastObserved=new Map();
    const original=route.events.find(e=>e.kind==="run_created"),scene=original.data.scenario;
    const names=new Map(scene.entities.map(a=>[a.id,a.name]));
    for(const event of route.events){
      if(!windows.has(event.window))windows.set(event.window,[]);
      windows.get(event.window).push(event);
      if(!event.discarded && event.kind==="window_committed")commits.set(event.window,event);
      if(event.kind==="observation")lastObserved.set(event.data.actor_id,event);
      if(["expression","decision_deferred"].includes(event.kind)){
        const observed=lastObserved.get(event.data.actor_id);
        if(observed?.window===event.window && observed.data.visible.round===event.data.round)contexts.set(event.id,observed);
      }
    }
    const initial=Object.fromEntries(scene.entities.map(a=>[a.id,Object.fromEntries(a.fields.map(f=>[f.id,f.value]))]));
    return {...route,byId,windows,commits,contexts,scene,names,initial};
  });
  let selected=0,windowNumber=routes[0].through,actor="",limit=40,technical=false;
  const heading=el("h1"),meta=el("p",undefined,"hint"),scope=el("p",data.scope,"hint"),controls=el("div",undefined,"controls");
  const routeChoice=el("select"),time=el("input"),who=el("select");
  routeChoice.setAttribute("aria-label","查看哪条路线");time.setAttribute("aria-label","观察窗口");who.setAttribute("aria-label","关注主体");
  time.type="number";time.min="0";time.step="1";time.required=true;
  routes.forEach((r,i)=>routeChoice.append(new Option((routes.length>1?(i?"新路线 · ":"原路线 · "):"")+r.title,String(i))));
  const label=(name,input)=>{const n=el("label",name);n.append(input);return n;};
  controls.append(label("路线",routeChoice),label("观察窗口",time),label("关注主体",who));
  const comparisons=el("section",undefined,"card"),states=el("section",undefined,"card"),feed=el("section",undefined,"card"),reports=el("section",undefined,"card");
  root.append(heading,meta,scope,controls,comparisons,states,feed,reports,el("footer","生成于 "+new Date(data.created).toLocaleString()+" · Trantor 推演回放"));
  document.getElementById("close").addEventListener("click",()=>dialog.close());
  function openRecord(route,id){
    const event=route.byId.get(id);record.replaceChildren(el("h2",route.title+" · "+id));
    if(!event)record.append(el("p","此编号的记录正文未收录在这份回放中。"));
    else record.append(el("p",(event.window?"第 "+event.window+" 窗":"开局")+" · "+event.kind+(event.discarded?" · 已作废":""),"hint"),
      el("pre",JSON.stringify(event.data,null,2)));
    if(!dialog.open)dialog.showModal();
  }
  const refs=(route,ids)=>{const n=el("span");for(const id of [...new Set(ids||[])])n.append(button(id,()=>openRecord(route,id)));return n;};
  const stateAt=(r,w)=>w===0?r.initial:r.commits.get(w)?.data.state;
  function setHash(){
    location.hash=new URLSearchParams({route:String(selected),window:String(windowNumber),actor}).toString();
  }
  function fromHash(){
    const values=new URLSearchParams(location.hash.slice(1)),r=Number(values.get("route")||0);
    selected=Number.isInteger(r)&&r>=0&&r<routes.length?r:0;
    const w=values.has("window")?Number(values.get("window")):routes[selected].through;
    windowNumber=Number.isInteger(w)&&w>=0&&w<=routes[selected].through?w:routes[selected].through;
    actor=routes[selected].names.has(values.get("actor"))?values.get("actor"):"";
    limit=40;render();
  }
  function render(){
    const route=routes[selected],state=stateAt(route,windowNumber),commit=route.commits.get(windowNumber);
    heading.textContent=route.title;routeChoice.value=String(selected);time.max=String(route.through);time.value=String(windowNumber);
    const config=route.events.find(e=>e.kind==="run_created").data.config;
    const model=config.model || config.models?.configs?.[config.models.world];
    meta.textContent=(model?.provider==="demo"?"本机脚本记录":"模型推演记录")+(model?.model?" · "+model.model:"")+" · "+route.id+" · 文件截至第 "+route.through+" 窗";
    who.replaceChildren(new Option("全部主体",""),...route.scene.entities.map(a=>new Option(a.name,a.id)));who.value=actor;
    comparisons.hidden=routes.length<2;comparisons.replaceChildren();
    if(routes.length>1){
      comparisons.append(el("h2",(windowNumber?"第 "+windowNumber+" 窗":"开局")+"的两条路线"));
      const a=stateAt(routes[0],windowNumber),b=stateAt(routes[1],windowNumber);let count=0;
      for(const entity of routes[0].scene.entities)for(const f of entity.fields){
        if(!(f.id in (b?.[entity.id]||{})))continue;
        const x=a?.[entity.id]?.[f.id],y=b[entity.id][f.id];
        if(JSON.stringify(x)!==JSON.stringify(y)){count++;comparisons.append(el("p",entity.name+" · "+(f.label||f.id)+"　"+display(x,f.unit)+" → "+display(y,f.unit),"diff"));}
      }
      if(!count)comparisons.append(el("p","这个时点的共同字段值相同。","hint"));
      comparisons.append(el("p","逐项比较已保存结果，保留每条路线自己的行动与收件依据。","hint"));
    }
    states.replaceChildren(el("h2",windowNumber?"当前处境":"开局处境"));
    const grid=el("div",undefined,"states");
    for(const entity of route.scene.entities.filter(a=>!actor||a.id===actor)){
      const box=el("article",undefined,"actor");box.append(el("h3",entity.name));
      for(const field of entity.fields)box.append(el("p",(field.label||field.id)+"为"+display(state?.[entity.id]?.[field.id]??null,field.unit)+"。"));
      if(!entity.fields.length)box.append(el("p",entity.identity,"hint"));
      grid.append(box);
    }
    states.append(grid,refs(route,[commit?.id||"e000001"]));
    feed.replaceChildren(el("h2","过程记录"));
    const show=el("input");show.type="checkbox";show.checked=technical;
    const toggle=el("label",undefined,"check");toggle.append(show,document.createTextNode("同时阅读运行和修正记录"));feed.append(toggle);
    show.addEventListener("change",()=>{technical=show.checked;limit=40;render();});
    const primary=new Set(["expression","decision_deferred","message_delivered","message_dispatched","external_event","window_committed"]);
    const rows=(route.windows.get(windowNumber)||[]).filter(e=>(technical||primary.has(e.kind)) &&
      (!actor||e.data.actor_id===actor||e.data.from===actor||e.data.to===actor||e.kind==="window_committed"));
    for(const event of rows.slice(0,limit)){
      const d=event.data,box=el("article",undefined,"entry"+(event.discarded?" invalid":""));
      const person=route.names.get(d.actor_id||d.from)||"世界";
      const kind={expression:"决定",decision_deferred:"延续等待",message_delivered:"已送达",message_dispatched:"已寄出",window_committed:"窗口结算",external_event:"外部事件"}[event.kind]||event.kind;
      box.append(el("small",person+(d.to?" → "+(route.names.get(d.to)||d.to):"")+" · "+kind+(event.discarded?" · 已作废":"")));
      if(d.text)box.append(el("p",d.text));
      if(event.kind==="window_committed"){
        for(const c of d.consequences||[])box.append(el("p",c.text),refs(route,[...(c.event_ids||[]),...(c.action_ids||[])]));
        if(!d.consequences?.length)box.append(el("p","本窗已完成统一结算。"));
      }else if(event.kind==="decision_deferred")box.append(el("p","按此前决定等待，本轮没有新的模型判断。"));
      else if(!d.text && technical)box.append(el("pre",JSON.stringify(d,null,2)));
      box.append(refs(route,[event.id]));
      const observed=route.contexts.get(event.id);
      if(observed)box.append(button("当轮观察",()=>openRecord(route,observed.id)));
      feed.append(box);
    }
    if(!rows.length)feed.append(el("p",windowNumber?"没有符合筛选的记录。":"这是推演开始前的状态。","empty"));
    if(rows.length>limit)feed.append(button("继续阅读本窗",()=>{limit+=40;render();}));
    reports.replaceChildren(el("h2","已生成的报告"));
    const reportRecords=route.events.filter(e=>e.kind==="report_created" && e.window<=windowNumber && !e.discarded);
    for(const event of reportRecords){
      const content=el("div"),r=event.data.report;
      for(const row of event.data.coverage?.observation_questions || []){
        const q=row.question;
        content.append(el("h3","关注问题 · "+(q.title || row.actor+" · "+row.label)));
        if(q.operator!=="track"){
          const condition={equals:"等于",at_least:"至少达到",at_most:"不超过",known:"出现已知值"}[q.operator];
          const period={ever:"观察期内",always:"整个观察期",at_end:"观察终点"}[q.criterion];
          content.append(el("p",period+" · "+condition+(q.target===null ? "" : display(q.target,row.unit))+
            (q.deadline===null ? "" : " · 期限为第 "+q.deadline+" 窗")));
        }
        content.append(el("p",row.actor+" · "+row.label+" · "+windowName(row.through)+"为"+display(row.value,row.unit)),
          el("p",watchFinding(row)),refs(route,[...new Set([row.record,row.first_hit?.record,row.first_miss?.record].filter(Boolean))]));
      }
      for(const section of r.sections){
        content.append(el("h3",section.title),el("p",section.text),refs(route,section.evidence_ids));
        for(const claim of section.claims||[])content.append(el("p",claim.text),refs(route,claim.evidence_ids));
      }
      reports.append(details(r.title+" · 截至第 "+event.window+" 窗",content));
    }
    if(!reportRecords.length)reports.append(el("p","截至这个窗口，文件中尚无已生成的报告。","hint"));
  }
  routeChoice.addEventListener("change",()=>{selected=Number(routeChoice.value);windowNumber=Math.min(windowNumber,routes[selected].through);setHash();});
  time.addEventListener("change",()=>{const n=Number(time.value);if(time.checkValidity()&&Number.isInteger(n)){windowNumber=n;setHash();}});
  who.addEventListener("change",()=>{actor=who.value;setHash();});
  window.addEventListener("hashchange",fromHash);fromHash();
})();
