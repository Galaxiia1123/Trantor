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
  },async submit(experiment,repetitions,compare_original=false) {
    const body={experiment,repetitions,compare_original,call_limit:Number(cap.value),watch:choices[Number(watch.value)]};
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
  const armName=id=>id==="original" ? "原条件" : "改后条件";
  async function refresh() {
    const ticket=++serial;
    clearTimeout(timer);
    const r=await api("/api/studies/"+encodeURIComponent(id));
    if(gen!==routeGeneration || ticket!==serial)return;
    const spec=r.request.experiment, total=r.members.length;
    heading.textContent=r.condition || "相同条件，再走 "+total+" 次";
    subtitle.textContent=r.title+" · 从"+(spec.window ? windowName(spec.window)+"结算后" : "开局")+"比较至"+windowName(spec.through)+
      (r.request.compare_original ? " · 两种条件各运行 "+r.request.repetitions+" 次" : "");
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
      const notes=h("a","下载实验记录");notes.href="/api/studies/"+id+"/notes";controls.append(notes);
    }
    const stamp=JSON.stringify([filter,r.completed,r.status,r.error,r.members.map(m=>[m.status,m.window]),r.outcomes,r.arms?.map(a=>a.outcomes)]);
    if(stamp!==previous) {
      previous=stamp;facts.replaceChildren(h("div","观测结果","eyebrow"));
      const b=r.baseline;
      facts.append(h("h2",r.request.watch?.title || (b ? b.actor+" · "+b.label : "逐条比较过程")));
      const baseline=h("a","回看原路线 ↗");baseline.href="#run/"+r.parent_id+"/"+spec.through;
      if(b)facts.append(h("p","存档原路线　"+studyOutcome(b)));facts.append(baseline);
      let visibleIndices=null;
      function pick(arm,o,text){
        const key=arm+"/"+o.key, selected=filter===key;if(selected)visibleIndices=new Set(o.indices);
        const toggle=button(text,async()=>{filter=selected ? null : key;await refresh();},"link");
        toggle.setAttribute("aria-pressed",String(selected));return toggle;
      }
      function numeric(summary,where){
        if(!summary || summary.known<2)return;
        const unit=b?.unit || "";where.append(h("small","中位数 "+summary.median+unit+"，范围 "+summary.min+unit+" 至 "+summary.max+unit+
          "（"+summary.known+" 个已知值"+(summary.unknown ? "，另有 "+summary.unknown+" 个未知" : "")+"）。","quiet"));
      }
      if(r.arms?.length){
        const table=h("table",null,"study-contrast"),head=table.createTHead().insertRow();
        for(const text of ["观测结果",...r.arms.map(a=>armName(a.arm))]){const cell=h("th",text);cell.scope="col";head.append(cell);}
        const tbody=table.createTBody(),keys=new Map();
        for(const arm of r.arms)for(const o of arm.outcomes)keys.set(o.key,studyOutcome(o.example));
        for(const [key,label] of keys){
          const row=tbody.insertRow(),name=h("th",label);name.scope="row";row.append(name);
          for(const arm of r.arms){
            const cell=row.insertCell(),o=arm.outcomes.find(v=>v.key===key), count=o?.indices.length || 0;
            if(o){const toggle=pick(arm.arm,o,count+" / "+arm.completed);toggle.setAttribute("aria-label",armName(arm.arm)+" · "+label+" · 查看 "+count+" 条路线");cell.append(toggle);}
            else cell.textContent=arm.completed ? "0 / "+arm.completed : "尚无终值";
          }
        }
        facts.append(table);
        if(!keys.size)facts.append(h("p",b ? "路线到达共同终点后显示结果。" : "这组实验分别保留每次独立运行的过程。","hint"));
        for(const arm of r.arms){
          const note=h("div",null,"arm-notes");note.append(h("strong",armName(arm.arm)+"　"+arm.completed+"/"+arm.total+" 条完成"));
          numeric(arm.numeric,note);facts.append(note);
        }
        facts.append(h("small","每轮分别运行两种条件，先后顺序轮换。每列只统计本条件已到终点的路线，存档原路线单独作为参照。","quiet"));
        if(r.arms.every(a=>a.completed))facts.append(button("并排读两条路线",()=>compareStudyRoutes(r),"link"));
      }else{
        if(!r.outcomes.length && b)facts.append(h("p","路线到达共同终点后，这里会列出每种结果出现的次数。","hint"));
        for(const o of r.outcomes){
          const row=h("div",null,"study-outcome"), meter=h("progress");meter.max=Math.max(1,r.completed);meter.value=o.indices.length;
          meter.setAttribute("aria-label",studyOutcome(o.example)+"，"+o.indices.length+" 次");
          row.append(h("strong",studyOutcome(o.example)),pick("single",o,o.indices.length+" 次 · 查看路线"),meter);facts.append(row);
        }
        numeric(r.numeric,facts);
        if(r.outcomes.length)facts.append(h("small","分母为已到达共同终点的 "+r.completed+" 条新路线。次数描述本组采样。","quiet"));
      }
      if(r.error)facts.append(h("p",r.error,"error"));
      list.replaceChildren(h("h2","每条路线"));
      if(filter)list.append(button("显示全部路线",async()=>{filter=null;await refresh();},"link"));
      for(const m of r.members){
        if(visibleIndices && !visibleIndices.has(m.index))continue;
        const item=h("div",null,"study-member"), text=h("div");item.dataset.index=m.index;
        text.append(h("strong",(m.arm ? armName(m.arm)+" · 第 "+m.replicate+" 次" : "路线 "+String(m.index).padStart(2,"0"))+"　"+(studyStatus[m.status] || m.status)),
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

function compareStudyRoutes(study) {
  const body=h("div"),controls=h("form",null,"study-compare-controls"),result=h("div"),spec=study.request.experiment;
  function pick(arm,title){
    const select=h("select");select.setAttribute("aria-label",title);
    for(const m of study.members.filter(m=>m.arm===arm && m.status==="completed"))
      select.append(new Option("第 "+m.replicate+" 次运行"+(m.outcome ? " · "+studyOutcome(m.outcome) : ""),m.index));
    const label=h("label",title);label.append(select);controls.append(label);return select;
  }
  const left=pick("original","原条件路线"),right=pick("changed","改后条件路线"),n=h("input");
  n.type="number";n.min=String(spec.window);n.max=String(spec.through);n.required=true;n.value=String(spec.through);
  const label=h("label","并排回看窗口");label.append(n);
  const go=h("button","查看这一窗");go.type="submit";controls.append(label,go);body.append(controls,result);
  let ticket=0;
  async function load(){
    if(!controls.reportValidity())return;
    const seq=++ticket,params=new URLSearchParams({original:left.value,changed:right.value,window:n.value});
    const data=await api("/api/studies/"+study.id+"/compare?"+params);
    if(!body.isConnected || seq!==ticket)return;
    const which=select=>study.members.find(m=>m.index===Number(select.value)).replicate;
    result.replaceChildren();drawExperimentComparison(result,data,data.actors,false,{labels:["原条件 · 第 "+which(left)+" 次","改后条件 · 第 "+which(right)+" 次"],
      selectWindow:async w=>{n.value=String(w);await load();}});
  }
  controls.onsubmit=safe(async e=>{e.preventDefault();await load();});left.onchange=right.onchange=safe(load);
  reading("两条实验路线的实际差异",body);safe(load)();
}
