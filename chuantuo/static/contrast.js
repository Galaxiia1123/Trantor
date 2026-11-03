"use strict";

function briefHash(left,right,actor,field,window) {
  return "#compare/"+[left,right,actor,field,window].map(encodeURIComponent).join("/");
}

function briefLink(left,right,actor,field,window) {
  const link=h("a","读这两条路线的对照简报 →","brief-link");
  link.href=briefHash(left,right,actor,field,window);return link;
}

async function briefPage(generation) {
  const root=$("compare-page");root.replaceChildren(h("p","正在核对两条路线的结算与行动…","hint"));scrollTo(0,0);
  const [,left,right,actor,field,window]=location.hash.slice(1).split("/").map(decodeURIComponent);
  if(!left || !right || !actor || !field || !/^\d+$/.test(window||"")){root.replaceChildren(h("p","这个对照地址缺少路线、字段或观察时点。"));return;}
  const params=new URLSearchParams({actor,field,through:window});
  const endpoint="/api/runs/"+encodeURIComponent(left)+"/compare/"+encodeURIComponent(right)+"/brief?"+params;
  let data;
  try{data=await api(endpoint);}catch(err){if(generation===routeGeneration)root.replaceChildren(h("p",err.message,"form-error"));return;}
  if(generation!==routeGeneration)return;
  const head=h("header",null,"brief-head"),choice=h("select"),label=h("label","关注哪项结果");
  data.options.forEach((f,i)=>{choice.append(new Option(f.actor+" · "+f.label,i));if(f.actor_id===actor && f.field_id===field)choice.value=String(i);});
  choice.setAttribute("aria-label","关注哪项结果");label.append(choice);
  choice.onchange=()=>{const f=data.options[Number(choice.value)];location.hash=briefHash(left,right,f.actor_id,f.field_id,window);};
  const tools=h("div",null,"brief-tools"),download=h("a","下载这份对照简报");
  download.href=endpoint+"&download=true";download.download="";tools.append(label,download);
  head.append(h("small","两条路线 · 对照简报","eyebrow"),h("h1",data.actor+" · "+data.field),h("p",data.summary,"lead"),tools);
  const premises=h("div",null,"brief-pair");
  for(const side of data.sides){
    const box=h("article",null,"brief-side"),back=h("a","回看"+side.label+" ↗");back.href="#run/"+side.run+"/"+data.through;
    box.append(h("small",side.label,"eyebrow"),h("h3",side.title),h("p",side.condition),back);premises.append(box);
  }
  root.replaceChildren(head,premises,h("h2","沿时间读依据"));
  const counts=data.counts;
  root.append(h("p","分岔后的窗末记录中，"+counts.equal+" 窗相同、"+counts.different+" 窗不同、"+counts.unknown+" 窗至少一侧未知。","hint"));
  for(const point of data.points){
    const section=h("section",null,"brief-point");
    section.append(h("h3",(point.window?"第 "+point.window+" 窗末":"开局")+" · "+point.label));
    const pair=h("div",null,"brief-pair");
    point.sides.forEach((row,i)=>{
      const side=data.sides[i],box=h("article",null,"brief-side"),refs=h("div",null,"brief-evidence");
      box.append(h("small",side.label,"quiet"),h("strong",row.shown),h("p",row.reason),
        button(point.window?"查阅该窗结算":"查阅开局记录",()=>evidence(row.record,side.run),"link"));
      for(const item of row.evidence){
        const ev=h("div",null,"brief-item"),who=item.actor || (item.sender?item.sender+" → "+item.recipient:"外部记录");
        ev.append(h("small",who,"eyebrow"),h("p",item.text+(item.excerpt?"…":"")),
          button("原文 "+item.id,()=>evidence(item.id,side.run),"link"));
        if(item.observation && item.actor_id)ev.append(button("这一轮知道什么",()=>openActorContext({id:item.actor_id,name:item.actor},item.window,item.round,false,side.run),"link"));
        refs.append(ev);
      }
      if(row.total_evidence>row.evidence.length)refs.append(h("small","共引 "+row.total_evidence+" 条，完整编号可在该窗结算中查看。","hint"));
      if(row.evidence.length)box.append(disclosure("结算引用的行动与事件（"+row.total_evidence+"）",refs));
      pair.append(box);
    });
    section.append(pair);root.append(section);
  }
  root.append(h("p",data.scope,"hint"));
}

function fieldContrastCard(focus,comparison,labels){
  const card=h("article",null,"contrast-focus");card.append(h("small","关注字段 · "+focus.actor,"eyebrow"),h("h3",focus.field));
  card.append(briefLink(comparison.left.run_id,comparison.right.run_id,focus.actor_id,focus.field_id,focus.through));
  const {equal,different,unknown}=focus.counts;
  if(focus.opening)card.append(h("p","开局时，"+labels[0]+"为“"+focus.opening.values[0]+"”，"+labels[1]+"为“"+focus.opening.values[1]+"”。"));
  if(focus.opening && !focus.through){
    for(const [i,side] of [[0,"left"],[1,"right"]])card.append(button(labels[i]+" · 开局记录",()=>evidence(focus.opening.records[i],comparison[side].run_id),"link"));
    return card;
  }
  if(!focus.end){card.append(h("p","到这个时点，两条路线仍共享同一段历史。"));return card;}
  const n=focus.through-focus.after;
  card.append(h("p","分岔之后的 "+n+" 个窗口中，"+equal+" 窗的值相同，"+different+" 窗不同"+(unknown ? "，另有 "+unknown+" 窗至少一侧未知" : "")+"。"));
  if(focus.first)card.append(h("p","第 "+focus.first.window+(focus.opening?.kind==="different"?" 窗末仍不同　":" 窗首次不同　")+labels[0]+"为"+focus.first.values[0]+"，"+labels[1]+"为"+focus.first.values[1]+"。"));
  if(focus.first && focus.end.kind==="equal")card.append(h("p","到第 "+focus.end.window+" 窗，两侧重新同为"+focus.end.values[0]+"。"));
  else if(!focus.first)card.append(h("p","第 "+focus.end.window+" 窗末　"+labels[0]+"为"+focus.end.values[0]+"，"+labels[1]+"为"+focus.end.values[1]+"。"));
  const at=focus.first || focus.end;
  for(const [i,side] of [[0,"left"],[1,"right"]])card.append(button(labels[i]+" · 第 "+at.window+" 窗结算",()=>evidence(at.records[i],comparison[side].run_id),"link"));
  return card;
}

function fieldContrastPicker(comparison,actors,labels){
  const fields=comparison.rows.filter(r=>r.left && r.right),chooser=h("select"),content=h("div"),panel=h("div");let seq=0;
  chooser.setAttribute("aria-label","按哪个字段对照");
  fields.forEach((f,i)=>chooser.append(new Option((actors[f.actor_id] || f.actor_id)+" · "+f.left.label,i)));
  if(!fields.length)return h("span");
  const box=disclosure("沿一个字段对照各窗",panel);
  const load=async()=>{
    const f=fields[Number(chooser.value)],ticket=++seq,q=new URLSearchParams({actor:f.actor_id,field:f.field_id,through:comparison.right.window});
    const d=await api("/api/runs/"+comparison.left.run_id+"/compare/"+comparison.right.run_id+"/field?"+q);
    if(ticket===seq && content.isConnected)content.replaceChildren(fieldContrastCard(d,comparison,labels));
  };
  chooser.onchange=safe(load);box.ontoggle=safe(async()=>{if(box.open && !content.childElementCount)await load();});
  panel.append(chooser,content);return box;
}

async function showActorContrast(comparison,actor,name,labels){
  const left=comparison.left.run_id,right=comparison.right.run_id,through=comparison.right.window;
  const ticket=++readingRequest;
  const result=await api("/api/runs/"+left+"/compare/"+right+"/actor/"+encodeURIComponent(actor)+"?through="+through);
  if(ticket!==readingRequest)return;
  const body=h("div",null,"contrast-trail"),r={id:actor,name};
  body.append(h("p",name+" · 从第 "+result.split+" 窗末到第 "+result.through+" 窗末","eyebrow"));
  function section(key,title,empty){
    const match=result[key],card=h("section",null,"contrast-mark");
    card.append(h("h3",title));
    if(!match){card.append(h("p",empty,"hint"));body.append(card);return;}
    card.append(h("p",windowName(match.window)+" · 第 "+match.round+" 轮","contrast-time"));
    const grid=h("div",null,"comparison-grid");
    for(const [idx,side,run] of [[0,"left",left],[1,"right",right]]){
      const col=h("div",null,"contrast-column");col.append(h("h4",labels[idx]));
      for(const group of match.groups){
        const g=group[side];col.append(h("small",group.category,"quiet"));
        if(!g.count)col.append(h("p","此侧没有额外项。","hint"));
        else{
          for(const text of g.examples)col.append(h("p",text));
          if(g.count>g.examples.length)col.append(h("small","共有 "+g.count+" 项。","quiet"));
        }
      }
      col.append(button("打开"+labels[idx]+"的这轮现场",()=>openActorContext(r,match.window,match.round,false,run),"link"));
      grid.append(col);
    }
    card.append(grid);body.append(card);
  }
  section("perception","最早的所见差异","所比较的轮次中，可见字段、信件、文书、通知、资料和规则一致。");
  section("behavior","最早的行动方式差异","等待、复看安排及发信对象与附件没有差异。");
  section("wording","最早的原文差异","决定与发信正文在忽略空白后相同。");
  body.append(h("p","原文用词不同可能来自采样。这里按记录比对，判断是否改变了立场可打开双方当时的现场。","hint"));
  reading(name+"的两条路线",body);
}
