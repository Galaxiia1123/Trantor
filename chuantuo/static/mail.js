"use strict";

const transitText=n=>n ? "发出后的第 "+n+" 个窗口开始时送达" : "发出当轮结束时送达";

function routeTimingPicker(actors) {
  const node=h("fieldset",null,"value-picker"), from=h("select"),to=h("select"),time=h("input"),example=h("p",null,"hint");
  node.append(h("legend","定向通信线路"));
  const field=(name,control)=>{const label=h("label",name);control.setAttribute("aria-label",name);label.append(control);node.append(label);};
  from.append(...actors.map(a=>new Option(a.name,a.id)));field("发件主体",from);field("收件主体",to);
  time.type="number";time.min="0";time.max="365";time.step="1";time.required=true;time.value="2";field("运送窗口数",time);
  const refresh=()=>{const n=Number(time.value);example.textContent=Number.isInteger(n)&&n>=0 ?
    (n ? "第 1 窗寄出，第 "+(1+n)+" 窗首送达。" : "消息在发出当轮结束时送达。")+"已经在途的消息保留原定时效。" : "";};
  function pairs(){const old=to.value;to.replaceChildren(...actors.filter(a=>a.id!==from.value).map(a=>new Option(a.name,a.id)));
    if(old && old!==from.value)to.value=old;}
  pairs();from.onchange=pairs;time.oninput=refresh;refresh();node.append(example);
  return {node,set(route){if(route){from.value=route.source;pairs();to.value=route.target;time.value=String(route.transit_windows);refresh();}},
          read:()=>({source:from.value,target:to.value,transit_windows:Number(time.value)})};
}

function draftMailRoutes(scene) {
  const content=h("div"), names=Object.fromEntries(scene.entities.map(a=>[a.id,a.name]));
  async function save(routes) {
    const old=scene.mail_routes;scene.mail_routes=routes;
    try{await saveProject("调整定向通信的送达时效");}catch(err){scene.mail_routes=old;throw err;}
    renderDraft();
  }
  for(const r of scene.mail_routes || []){
    const card=h("div",null,"cohort");card.append(h("strong",names[r.source]+" → "+names[r.target]),h("p","从第 "+r.active_from+" 窗发件起，"+transitText(r.transit_windows)+"。"));
    for(const q of r.provenance?.quotes||[])card.append(h("small","“"+q.quote+"”","quiet"));
    card.append(button("移除时效",()=>save(scene.mail_routes.filter(old=>old!==r)),"link"));content.append(card);
  }
  if(scene.entities.length>=2)content.append(button("设置线路时效",()=>{
    const picker=routeTimingPicker(scene.entities),form=h("form",null,"experiment-form");
    const start=h("input");start.type="number";start.min="1";start.value="1";start.required=true;
    const name=h("label","从第几窗的发件起生效");name.append(start);
    const send=h("button","保存线路时效","primary");send.type="submit";
    form.append(picker.node,name,send);
    form.onsubmit=safe(async e=>{e.preventDefault();if(send.disabled)return;send.disabled=true;
      const r={...picker.read(),active_from:Number(start.value),provenance:{kind:"user",explanation:"操作者设定定向送达时效。"}};
      const rows=(scene.mail_routes||[]).filter(old=>!(old.source===r.source&&old.target===r.target&&old.active_from===r.active_from));
      try{await save([...rows,r]);$("reading").close();}finally{send.disabled=false;}
    });
    reading("设置发件与送达时间",form);
  },"link"));
  return disclosure("定向通信时效（"+(scene.mail_routes||[]).length+"）",h("p","没有单设时效的方向，默认当轮结束后送达。","hint"),content);
}

function mailMoveBoundary(view,sent,due,arrived) {
  if(playback())return null;
  const end=view.settled_through, latest=view.status.window;
  const split=Math.max(view.status.experiment?.window||0,!arrived && end<latest ? end : sent);
  return split<latest && split<due ? split : null;
}

async function singleMailMove(view,dispatch,split,preset=null) {
  const run=activeRun,ticket=++readingRequest;
  const mail=await api("/api/runs/"+run+"/mail/"+encodeURIComponent(dispatch)+"/at/"+split);
  if(run!==activeRun || ticket!==readingRequest)return;
  const names=Object.fromEntries(view.actors.map(a=>[a.id,a.name])), form=h("form",null,"advanced");
  form.append(h("p",names[mail.from]+" → "+names[mail.to]+" · "+windowName(mail.sent_window)+"寄出","eyebrow"),
    h("p",mail.text),h("p","从"+windowName(split)+"结算后分岔。此时这封信预计"+windowName(mail.due_window)+"送达，收件人仍须等实际到件才能阅读。","hint"));
  if(mail.enclosures.length)form.append(h("p","随信附件　"+mail.enclosures.join("、")));
  function number(label,value,max){
    const wrap=h("label",label),input=h("input");input.setAttribute("aria-label",label);input.type="number";input.step="1";input.min=String(split+1);
    input.max=String(max);input.required=true;input.value=String(value);wrap.append(input);form.append(wrap);return input;
  }
  const fallback=mail.due_window>split+1?mail.due_window-1:mail.due_window+1;
  const next=number("这封信改在第几窗到达",preset?.delivery_move?.new_window ?? fallback,1000000);
  const end=number("比较到哪个窗口",preset?.through ?? view.status.window,view.status.window);
  const cost=h("p",null,"hint"),error=h("p",null,"form-error"),go=h("button","按新日期开一条路线","primary");go.type="submit";
  const estimate=()=>{cost.textContent=view.mode==="sample"?"使用本地案例脚本。":
    "新路线重跑 "+Math.max(0,Number(end.value)-split)+" 窗，按每轮全体判断约 "+Math.max(0,Number(end.value)-split)*(view.actors.length*view.rounds_per_window+1)+" 次基础调用。";};
  end.oninput=estimate;estimate();form.append(cost,error,go);
  form.onsubmit=async e=>{
    e.preventDefault();if(go.disabled || busy)return;
    const due=Number(next.value),through=Number(end.value);
    if(due===mail.due_window || Math.min(due,mail.due_window)>through){error.textContent="请选不同的新日期，并让比较时段至少覆盖一个到件日。";return;}
    go.disabled=true;error.textContent="";
    try {
      await job("experiment","/api/jobs/{id}/experiment/"+run,{kind:"delivery_time",window:split,through,delivery_move:{dispatch_id:dispatch,new_window:due}},
        async result=>{$("reading").close();location.hash="run/"+result.run_id;});
    }catch(err){error.textContent=err.message;}finally{go.disabled=false;}
  };
  reading("只调整这封在途信",form);
}

function openRunMail(view) {
  const run=activeRun,end=view.settled_through, body=h("div"),controls=h("div",null,"interaction-controls");
  const who=h("select"),stage=h("select"),status=h("p",null,"hint"),list=h("div"),routes=h("div");
  who.setAttribute("aria-label","按通信主体筛选");stage.setAttribute("aria-label","按邮件状态筛选");
  const names=Object.fromEntries(view.actors.map(a=>[a.id,a.name]));
  who.append(new Option("全部主体",""),...view.actors.map(a=>new Option(a.name,a.id)));
  stage.append(new Option("全部邮件",""),new Option("仍在途中","pending"),new Option("已经送达","arrived"));
  controls.append(who,stage);body.append(controls,status,list,disclosure("当时的线路时效",routes));
  let serial=0;
  async function page(offset=0){
    const ticket=++serial,params=new URLSearchParams({through:end,offset});
    if(who.value)params.set("actor",who.value);if(stage.value)params.set("stage",stage.value);
    const r=await api("/api/runs/"+run+"/mail?"+params);if(!body.isConnected || ticket!==serial)return;
    if(!offset)list.replaceChildren();list.querySelector(".more-mail")?.remove();
    status.textContent="截至"+windowName(end)+"，"+r.pending+" 件在途，"+r.arrived+" 件已送达。";
    routes.replaceChildren(...r.routes.map(p=>h("p",names[p.source]+" → "+names[p.target]+"　"+transitText(p.transit_windows))));
    for(const m of r.items){
      const card=h("article",null,"conversation-entry");card.append(h("small",m.stage==="arrived" ? "已送达" : r.ended ? "运行结束时仍在途" : "在途","eyebrow"),
        h("h3",names[m.from]+" → "+names[m.to]),h("p",m.text),
        h("small",windowName(m.sent_window)+"第 "+m.sent_round+" 轮发出，"+(m.receipt ? "于" : "预计")+windowName(m.due_window)+"窗首送达。","quiet"));
      card.append(button("发件时的决定",()=>openActorContext({id:m.from,name:names[m.from]},m.sent_window,m.sent_round,false,run),"link"));
      if(m.seen)card.append(button("收件时看到的内容",()=>openActorContext({id:m.to,name:names[m.to]},m.due_window,1,false,run),"link"));
      if(m.original_due_window)card.append(h("small","寄出时预计"+windowName(m.original_due_window)+"到达，后来单独改期。","hint"));
      const split=mailMoveBoundary(view,m.sent_window,m.due_window,m.receipt);
      if(split!==null)card.append(button("只调整这封信的到件日",()=>singleMailMove(view,m.dispatch,split),"link"));
      card.append(evidenceLinks([m.dispatch,m.receipt].filter(Boolean),run));list.append(card);
    }
    if(!r.total)list.append(h("p","这个时点还没有符合筛选的跨窗邮件。","empty"));
    if(r.next<r.total)list.append(button("继续读取邮件",()=>page(r.next),"link more-mail"));
  }
  who.onchange=stage.onchange=safe(()=>page());
  reading("在途与到件",body);safe(()=>page())();
}
