"use strict";

const studyStatus={queued:"等待开始",opening:"恢复分岔现场",pausing:"将在窗口末暂停",stopping:"将在窗口末结束",limit:"达到调用上限",
  ready:"待启动",running:"推演中",paused:"已暂停",failed:"未完成",interrupted:"服务中断",stopped:"已结束",completed:"已完成"};

function studyOptions(view,run) {
  const node=h("fieldset",null,"study-options"), watch=h("select"), cap=h("input");
  node.append(h("legend","重复实验"));
  const title=h("label","想比较什么");watch.setAttribute("aria-label","想比较什么");title.append(watch);node.append(title);
  const choices=[];
  function add(label,question) {watch.append(new Option(label,choices.length));choices.push(question);}
  for(const actor of view.actors) for(const [id,f] of Object.entries(actor.fields))
    add(actor.name+" · "+(f.label || id)+"的终值",{actor_id:actor.id,field_id:id});
  add("只比较过程，由我回看每条路线",null);
  let requested=false;
  cap.type="number";cap.min="1";cap.max="1000000";cap.step="1";cap.required=true;
  const budget=h("label","全组模型调用上限");cap.setAttribute("aria-label","全组模型调用上限");budget.append(cap);
  node.append(budget,h("p","主体判断、世界结算及输出纠正均占用上限。网络层重试可能另产生请求，费用以提供商账单为准。","hint"));
  let edited=false, submission=null;
  cap.oninput=()=>{edited=true;};
  return {node,update(count,calls) {
    node.disabled=node.hidden=count===1;
    if(!edited)cap.value=String(Math.min(1000000,count*(calls+Math.max(2,Math.ceil(calls/4)))));
    if(count>1 && !requested) {
      requested=true;
      api("/api/runs/"+run+"/watches").then(rows=>{
        for(const row of rows) add("观察卡 · "+(row.question.title || row.label),row.question);
      }).catch(()=>{requested=false;});
    }
  },async submit(experiment,repetitions) {
    const body={experiment,repetitions,call_limit:Number(cap.value),watch:choices[Number(watch.value)]};
    const signature=JSON.stringify(body);
    if(submission?.signature!==signature)submission={id:uid(),signature};
    return api("/api/runs/"+run+"/studies/"+submission.id,"PUT",body);
  }};
}

function studyOutcome(row) {
  if(!row)return "尚无观测结果";
  if(row.status==="tracking")return watchValue(row,row.unit);
  const results={observed:"出现过目标结果",not_observed:"未出现目标结果",maintained:"全程满足",violated:"曾经偏离条件",
    met_at_end:"终点达成",missed_at_end:"终点未达成",undetermined:"有未知窗口",undetermined_end:"终值未知",ended_early:"未覆盖指定期限"};
  return results[row.status] || "尚未确定";
}

async function studyPage(id,gen) {
  const page=$("study-page"), heading=h("h1","正在打开实验组"), subtitle=h("p",null,"lead"), controls=h("div",null,"actions");
  const header=h("div",null,"watch-head"), titles=h("div");titles.append(heading,subtitle);header.append(titles,controls);
  const progress=h("p",null,"study-progress"), facts=h("article",null,"card study-result"), list=h("article",null,"card study-members");
  const grid=h("div",null,"study-grid");grid.append(facts,list);
  const back=h("a","回到项目与记录");back.href="#library";
  page.replaceChildren(h("div","重复实验","eyebrow"),header,progress,grid,back);
  let previous="",lastControls="",serial=0,filter=null;
  const outcomeKey=row=>JSON.stringify(row.status==="tracking" ? [row.status,row.numeric_text ?? row.value] : [row.status]);
  async function refresh() {
    const ticket=++serial;
    clearTimeout(timer);
    const r=await api("/api/studies/"+encodeURIComponent(id));
    if(gen!==routeGeneration || ticket!==serial)return;
    const spec=r.request.experiment, total=r.members.length;
    heading.textContent=r.condition || "相同条件，再走 "+total+" 次";
    subtitle.textContent=r.title+" · 从"+(spec.window ? windowName(spec.window)+"结算后" : "开局")+"比较至"+windowName(spec.through);
    progress.textContent=(studyStatus[r.status] || r.status)+"　·　"+r.completed+" / "+total+" 条完成　·　模型调用 "+r.calls+" / "+r.request.call_limit;
    if(lastControls!==r.status){
      lastControls=r.status;controls.replaceChildren();
      const control=(label,action)=>button(label,async()=>{
        controls.querySelectorAll("button").forEach(b=>b.disabled=true);
        try{await api("/api/studies/"+id+"/"+action,"POST");}finally{lastControls=""; if(gen===routeGeneration)await refresh();}
      });
      if(r.status==="running")controls.append(control("在窗末暂停","pause"));
      if(["paused","interrupted","failed"].includes(r.status)) controls.append(control("继续这组实验","resume"));
      if(["running","paused","interrupted","failed","pausing"].includes(r.status))controls.append(control("结束实验组","stop"));
    }
    const stamp=JSON.stringify([filter,r.completed,r.status,r.error,r.members.map(m=>[m.status,m.window]),r.outcomes]);
    if(stamp!==previous) {
      previous=stamp;facts.replaceChildren(h("div","观测结果","eyebrow"));
      const b=r.baseline;
      facts.append(h("h2",r.request.watch?.title || (b ? b.actor+" · "+b.label : "逐条比较过程")));
      const baseline=h("a","回看原路线 ↗");baseline.href="#run/"+r.parent_id+"/"+spec.through;
      if(b)facts.append(h("p","原路线　"+studyOutcome(b)));facts.append(baseline);
      if(!r.outcomes.length && b)facts.append(h("p","路线到达共同终点后，这里会列出每种结果出现的次数。","hint"));
      for(const o of r.outcomes){
        const row=h("div",null,"study-outcome"), meter=h("progress");meter.max=Math.max(1,r.completed);meter.value=o.indices.length;
        meter.setAttribute("aria-label",studyOutcome(o.example)+"，"+o.indices.length+" 次");
        const key=outcomeKey(o.example), selected=filter===key;
        const pick=button(o.indices.length+" 次 · 查看路线",async()=>{filter=selected ? null : key;await refresh();},"link");
        pick.setAttribute("aria-pressed",String(selected));
        row.append(h("strong",studyOutcome(o.example)),pick,meter);facts.append(row);
      }
      if(r.outcomes.length)facts.append(h("small","分母为已到达共同终点的 "+r.completed+" 条新路线。次数描述本组采样。","quiet"));
      if(r.error)facts.append(h("p",r.error,"error"));
      list.replaceChildren(h("h2","每条路线"));
      if(filter)list.append(button("显示全部路线",async()=>{filter=null;await refresh();},"link"));
      for(const m of r.members){
        if(filter && (!m.outcome || outcomeKey(m.outcome)!==filter))continue;
        const item=h("div",null,"study-member"), text=h("div");item.dataset.index=m.index;
        text.append(h("strong","路线 "+String(m.index).padStart(2,"0")+"　"+(studyStatus[m.status] || m.status)),
          h("small",m.status==="queued" ? "等待前面的路线结束" : windowName(m.window)+" · "+m.calls+" 次调用"+(m.outcome ? " · "+studyOutcome(m.outcome) : "")));
        item.append(text);
        if(m.outcome?.first_hit) {
          const w=m.outcome.first_hit.window, hit=h("a",windowName(w)+"首次达成"+(w<=spec.window ? "（分岔前）" : ""));
          hit.href="#run/"+m.run_id+"/"+w;text.append(hit);
        }
        if(!["queued","opening"].includes(m.status)){
          const link=h("a","回看 ↗");link.href="#run/"+m.run_id+"/"+m.window;
          link.setAttribute("aria-label","回看路线 "+m.index);item.append(link);
        }
        list.append(item);
      }
    } else for(const m of r.members)if(m.status==="running"){
      const line=list.querySelector('.study-member[data-index="'+m.index+'"] small');
      if(line)line.textContent=windowName(m.window)+" · "+m.calls+" 次调用";
    }
    if(["running","pausing","stopping"].includes(r.status))timer=setTimeout(safe(refresh),1250);
  }
  await refresh();
}
