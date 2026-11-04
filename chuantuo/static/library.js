"use strict";

const openRouteFamilies=new Set(), routeFamilyLimits=new Map();

function routeFamilies(runs) {
  const byId=new Map(runs.map(run=>[run.run_id,run])), owners=new Map(), families=new Map();
  for(const run of runs) {
    const path=[],seen=new Map();
    let cursor=run,key;
    while(cursor && !owners.has(cursor.run_id)) {
      if(seen.has(cursor.run_id)) {
        key="cycle:"+path.slice(seen.get(cursor.run_id)).map(item=>item.run_id).sort()[0];
        break;
      }
      seen.set(cursor.run_id,path.length);path.push(cursor);
      const parent=cursor.experiment?.parent_id;
      if(!parent){key=cursor.run_id;break;}
      if(!byId.has(parent)){key="missing:"+parent;break;}
      cursor=byId.get(parent);
    }
    key=key || owners.get(cursor.run_id);
    for(const item of path)owners.set(item.run_id,key);
    if(!families.has(key))families.set(key,{key,root:byId.get(key),members:[],byId});
    families.get(key).members.push(run);
  }
  return [...families.values()];
}

function orderedFamily(family) {
  const children=new Map(),members=new Set(family.members.map(run=>run.run_id));
  const roots=[];
  for(const run of family.members) {
    const parent=run.experiment?.parent_id;
    if(!members.has(parent))roots.push(run);
    else {
      if(!children.has(parent))children.set(parent,[]);
      children.get(parent).push(run);
    }
  }
  const result=[],seen=new Set();
  // The second pass also keeps damaged cyclic links readable.
  for(const root of [...roots,...family.members]) {
    const stack=[{run:root,depth:0}];
    while(stack.length) {
      const item=stack.pop();
      if(seen.has(item.run.run_id))continue;
      seen.add(item.run.run_id);result.push(item);
      for(const child of [...(children.get(item.run.run_id)||[])].reverse())
        stack.push({run:child,depth:item.depth+1});
    }
  }
  return result;
}

function routeTimestamp(run) {
  const date=new Date(run.updated_at);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
}

function routeModelLabel(run) {
  if(run.mode==="demo")return "本机脚本";
  if(run.mode==="mixed")return "脚本与模型";
  return (run.model_provider==="codex_cli" ? "ChatGPT 订阅" : "模型接口")+(run.model_name ? " · "+run.model_name : "");
}

function familyCard(family,matching,searching) {
  const card=h("article",null,"route-family"),latest=matching[0],all=family.members;
  card.dataset.family=family.key;
  const title=family.root?.title || all.at(-1).title;
  const head=h("div",null,"route-family-head"),heading=h("div");
  heading.append(h("h3",title),h("p",searching ? matching.length+" / "+all.length+" 条路线匹配" : all.length+" 条相关路线","hint"));
  head.append(heading,button(all.length===1 ? "回看" : "打开最近路线",()=>{location.hash="run/"+latest.run_id;}));
  card.append(head);
  const line=h("div",null,"route-latest");
  line.append(h("span",statuses[latest.status] || latest.status,"route-status"),
    h("span",latest.window+"/"+latest.windows+" 窗"),h("time",routeTimestamp(latest)));
  if(["failed","interrupted"].includes(latest.status))line.classList.add("needs-attention");
  if(latest.status==="running")line.classList.add("is-running");
  card.append(line);
  const name=latest.experiment ? experimentKind(latest.experiment)+" · "+(latest.experiment.condition || "原条件") : "原路线";
  card.append(h("p",name,"route-latest-name"),h("small",routeModelLabel(latest),"quiet"));
  if(family.key.startsWith("missing:"))card.append(h("p","这组路线的原始记录不在本机，仍可分别回看已有结果。","hint"));
  if(family.key.startsWith("cycle:"))card.append(h("p","路线来源存在循环，请在开局记录中核对。已有记录可分别回看。","error"));
  if(all.length===1 && !latest.experiment)return card;
  const body=h("div"),details=disclosure("查看路线脉络",body);
  details.classList.add("route-family-details");details.open=searching || openRouteFamilies.has(family.key);
  const selected=new Set(matching.map(run=>run.run_id));
  const entries=orderedFamily(family).filter(item=>selected.has(item.run.run_id));
  function draw() {
    const limit=routeFamilyLimits.get(family.key) || 12,list=h("ol",null,"route-lineage");
    for(const {run,depth} of entries.slice(0,limit)) {
      const item=h("li",null,"route-node");item.dataset.run=run.run_id;
      item.style.setProperty("--route-depth",Math.min(depth,3));
      const top=h("div",null,"route-node-head"),label=run.experiment ? experimentKind(run.experiment) : "原路线";
      top.append(h("strong",label),button("回看",()=>{location.hash="run/"+run.run_id;},"link"));
      item.append(top);
      if(run.experiment?.condition)item.append(readablePassage(run.experiment.condition));
      item.append(h("p",(statuses[run.status]||run.status)+" · "+run.window+"/"+run.windows+" 窗 · "+routeTimestamp(run)+" · "+run.run_id,"route-node-meta"));
      item.append(h("small",routeModelLabel(run),"quiet"));
      const parent=run.experiment?.parent_id;
      if(parent) {
        const caption=(run.experiment.window ? "第 "+run.experiment.window+" 窗结算后" : "开局")+"分岔";
        if(family.byId.has(parent)) {
          const origin=family.byId.get(parent);
          const source=h("a",caption+" · 回看"+(origin.experiment ? experimentKind(origin.experiment) : "原路线"),"route-origin");
          source.href="#run/"+parent+"/"+run.experiment.window;item.append(source);
        } else item.append(h("small",caption+" · 来源记录不在本机","quiet"));
      }
      if(run.experiment?.reuse_first_round)item.append(h("small","首轮相同输入可沿用原判断","quiet"));
      list.append(item);
    }
    body.replaceChildren(list);
    if(entries.length>limit)body.append(button("再显示 "+Math.min(12,entries.length-limit)+" 条路线",()=>{
      routeFamilyLimits.set(family.key,limit+12);draw();
    },"link"));
  }
  details.addEventListener("toggle",()=>{
    if(!details.isConnected)return;
    if(!searching) {
      if(details.open)openRouteFamilies.add(family.key);
      else openRouteFamilies.delete(family.key);
    }
    if(details.open && !body.childElementCount)draw();
  });
  if(details.open)draw();
  card.append(details);return card;
}
