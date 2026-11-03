"use strict";

let stripState=null;

function recordedStrip(start,end,slots) {
  const width=Math.max(1,Math.ceil((end-start+1)/slots)),bins=[];
  let changed=0,first=null,last=null;
  for(let a=start;a<=end;a+=width) {
    const b={start:a,end:Math.min(end,a+width-1),changes:0,messages:0,external:0};
    for(let w=a;w<=b.end;w++){
      const events=example.windows[w].events, commit=events.find(e=>e.kind==="window_committed");
      const n=commit?.data.changes?.length || 0;
      b.changes+=n;b.messages+=events.filter(e=>e.kind==="message_delivered").length;
      b.external+=events.filter(e=>e.kind==="external_event").length;
      if(n){changed++;first??=w;last=w;}
    }
    bins.push(b);
  }
  return {start,through:end,slots:bins,changed_windows:changed,first_change:first,last_change:last};
}

async function showWindowStrip(view) {
  const root=$("window-strip"), run=activeRun;
  const total=playback() ? example.windows.length-1 : view.status.window;
  root.hidden=total<1;
  if(root.hidden)return;
  if(stripState?.run!==run)stripState={run,range:null,cache:new Map(),serial:0};
  const state=stripState, [from,end]=state.range || [1,total];
  const limit=Math.max(8,Math.min(36,Math.floor((Math.min(innerWidth,1120)-95)/26)));
  const key=[from,end,limit].join("/"),stamp=[run,key,view.selected].join("/");
  if(root.dataset.stamp===stamp && root.childElementCount)return;
  root.dataset.stamp=stamp;
  const ticket=++state.serial;
  let data=state.cache.get(key);
  try{
    if(!data){
      data=playback() ? recordedStrip(from,end,limit) : await api("/api/runs/"+run+"/window-strip?start="+from+"&through="+end+"&slots="+limit);
      if(state.cache.size>=6)state.cache.delete(state.cache.keys().next().value);
      state.cache.set(key,data);
    }
  }catch(err){if(state.serial===ticket)delete root.dataset.stamp;throw err;}
  if(activeRun!==run || ticket!==state.serial || stripState!==state)return;
  const focused=root.contains(document.activeElement) ? document.activeElement.dataset.window : null;
  const head=h("div",null,"strip-head"),label=h("div");
  label.append(h("span","局势时间带","eyebrow"),h("strong",from===1 && end===total ? total+" 个已结算窗口" : "第 "+from+" 至 "+end+" 窗"));
  head.append(label);
  if(state.range)head.append(button("返回整场",()=>{state.range=null;delete root.dataset.stamp;return showWindowStrip(view);},"link"));
  const rail=h("div",null,"window-tracks");rail.setAttribute("role","group");rail.setAttribute("aria-label","按窗口探索局势");
  rail.style.gridTemplateColumns="repeat("+data.slots.length+",minmax(0,1fr))";
  const hasCurrent=data.slots.some(b=>b.start<=view.selected && view.selected<=b.end);
  data.slots.forEach((b,index)=>{
    const single=b.start===b.end, chosen=b.start<=view.selected && view.selected<=b.end;
    const text=single ? windowName(b.start) : "第 "+b.start+" 至 "+b.end+" 窗";
    const name=text+"，"+b.changes+" 项字段变化，"+b.messages+" 次投递，"+b.external+" 个外部事件";
    const step=button("",async()=>{
      if(single)await pinWindow(b.start);
      else{state.range=[b.start,b.end];delete root.dataset.stamp;await showWindowStrip(view);}
    },"track-step");
    step.dataset.tone=b.changes ? "change" : b.external || b.messages ? "contact" : "settled";
    step.dataset.window=b.start;step.title=name;step.setAttribute("aria-label",(single ? "观察" : "放大")+name);
    if(chosen)step.setAttribute("aria-current","step");
    step.tabIndex=chosen || (!hasCurrent && index===0) ? 0 : -1;step.append(h("i"));rail.append(step);
  });
  rail.onkeydown=e=>{
    const at=Array.from(rail.children).indexOf(e.target),n=rail.children.length;
    const next={ArrowRight:Math.min(n-1,at+1),ArrowLeft:Math.max(0,at-1),Home:0,End:n-1}[e.key];
    if(at<0 || next==null)return;e.preventDefault();e.target.tabIndex=-1;rail.children[next].tabIndex=0;rail.children[next].focus();
  };
  const meta=h("div",null,"strip-meta"),legend=h("div",null,"strip-legend");
  for(const [tone,title] of [["change","字段变化"],["contact","通信或外部事件"],["settled","其他结算"]]){
    const tag=h("span",title);tag.dataset.tone=tone;legend.append(tag);
  }
  const scale=h("div",null,"strip-scale");
  scale.append(h("span",windowName(from)),h("span",data.slots.some(b=>b.start!==b.end) ? "选择一段以放大" : "选择一个窗口回看"),h("span",windowName(end)));
  meta.append(legend,h("small",data.changed_windows+" 窗发生字段变化","quiet"));
  if(data.last_change && data.last_change!==view.selected)meta.append(button("看最近一次变化 · "+windowName(data.last_change),()=>pinWindow(data.last_change),"link"));
  root.replaceChildren(head,rail,scale,meta);
  if(focused)rail.querySelector('[data-window="'+focused+'"]')?.focus({preventScroll:true});
}

function openFieldReason(view,actorId,fieldId,window=view.settled_through) {
  const run=activeRun,body=h("div",null,"reason-reader"),choose=h("select"),content=h("div");
  const fields=[];
  for(const actor of view.actors)for(const [id,f] of Object.entries(actor.fields)){
    choose.append(new Option(actor.name+" · "+(f.label || id),fields.length));fields.push({actor:actor.id,id});
  }
  if(!fields.length || playback())return evidence(view.state_record);
  const change=view.settlement?.data.changes?.[0], target=actorId || change?.entity_id, wanted=fieldId || change?.field_id;
  choose.selectedIndex=Math.max(0,fields.findIndex(f=>f.actor===target && f.id===wanted));
  choose.setAttribute("aria-label","要解释的字段");body.append(choose,content);
  let ticket=0;
  async function load(offset=0){
    const seq=++ticket,{actor,id}=fields[choose.selectedIndex];
    const params=new URLSearchParams({window,actor,field:id,offset});
    if(!offset)content.replaceChildren(h("p","正在读取这项字段的结算记录…","hint"));
    const r=await api("/api/runs/"+run+"/field-reason?"+params);
    if(seq!==ticket || !body.isConnected)return;
    if(!offset){
      const hero=h("section",null,"reason-summary");
      const value=(v,exact)=>watchValue({value:v,numeric_text:exact},r.unit);
      const phrase=r.changed ? value(r.before,r.exact_before)+" → "+value(r.after,r.exact_after) : value(r.after,r.exact_after);
      hero.append(h("span",windowName(window)+" · "+(window===0 ? "开局设定" : r.changed ? "字段发生变化" : "保持原值"),"eyebrow"),h("h3",phrase),
        h("p",r.reason));content.replaceChildren(hero);
      if(r.previous_change)content.append(button("查阅上一次变化 · "+windowName(r.previous_change),()=>openFieldReason(view,r.actor_id,r.field_id,r.previous_change),"link"));
      content.append(h("h3",r.total ? "结算引用的记录（"+r.total+"）" : window===0 ? "开局来源" : "这项审查没有引用行动或事件"));
      if(r.definition?.quotes?.length)content.append(disclosure("字段定义的原文",...r.definition.quotes.map(q=>h("p","“"+q.quote+"”","hint"))));
    }
    content.querySelector(".more-reasons")?.remove();
    for(const e of r.evidence){
      const card=h("article",null,"reason-card"),isAction=e.kind==="expression",postal=e.kind==="message_delivered";
      card.dataset.kind=isAction ? "action" : "event";
      card.append(h("small",isAction ? e.actor+"的决定 · 第 "+e.round+" 轮" : postal ? e.sender+" → "+e.recipient+" · 已送达" : "预定外部事件","eyebrow"),h("p",e.text));
      if(isAction && e.observation){
        const inbox=e.inbox.map(m=>h("p",m.from+"来信　"+m.text,"hint"));
        if(e.inbox_count>e.inbox.length)inbox.push(h("small","其余来信可在完整决策现场查阅。","quiet"));
        if(inbox.length)card.append(disclosure("本轮收到的 "+e.inbox_count+" 封来信",...inbox));
        card.append(button("查看"+e.actor+"当时知道什么",()=>openActorContext({id:e.actor_id,name:e.actor},e.window,e.round,false,run),"link"));
      }else if(postal && e.seen){
        card.append(h("small",windowName(e.sent_window)+"寄出，"+windowName(e.window)+"窗首到达。","quiet"),
          button("查看收件人的首次观察",()=>openActorContext({id:e.recipient_id,name:e.recipient},e.window,1,false,run),"link"));
      }
      card.append(evidenceLinks([e.id],run));content.append(card);
    }
    if(r.next<r.total)content.append(button("继续读取引用",()=>load(r.next),"more-reasons"));
    else {
      if(r.influences.length)content.append(disclosure("本次审查提到的有向关系",...r.influences.map(v=>h("p",v.from_name+" → "+v.to_name+"　"+v.explanation))));
      content.append(button("完整"+(window ? "窗口结算" : "开局记录")+" · "+r.record,()=>evidence(r.record,run),"link"));
    }
  }
  choose.onchange=safe(()=>load());
  reading("这项状态如何得出",body);safe(()=>load())();
}
