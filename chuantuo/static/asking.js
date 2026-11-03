"use strict";

function askExperiment(view) {
  const run=activeRun,path="/api/runs/"+run,from=Math.max(view.status.experiment?.window || 0,Math.min(view.settled_through-1,view.status.window-1));
  const body=h("div"),form=h("form",null,"experiment-form"),area=h("textarea",null,"editor"),label=h("label","如果改变什么，你想观察什么？");
  area.rows=3;area.required=true;area.maxLength=3000;area.placeholder="例如：把可用席位分别设为 6、12、18 个，开班进度会一样吗？";
  const go=h("button","拟定对照方案","primary"),status=h("p","","hint"),results=h("div"),previous=h("div");go.type="submit";status.setAttribute("aria-live","polite");
  const allow=h("input");allow.type="checkbox";allow.checked=view.mode!=="sample";
  const opt=h("label",null,"check");opt.append(allow,document.createTextNode("文字没有直接匹配时，允许用原路线的模型拟定一次"));
  label.append(area);form.append(label,h("p","未写明时点时，默认从第 "+(from+1)+" 窗开始改变条件，对照至第 "+view.status.window+" 窗。可以在开跑前编辑。","hint"));
  if(view.mode!=="sample")form.append(disclosure("拟定方式",opt));
  form.append(h("p",view.mode==="sample" ? "这条路线使用本机案例脚本，可直接识别明确的字段取值与事件改期。" :
    "能直接识别的取值或日期无需模型，其余问题最多调用一次。此步只生成方案。","quiet"),go,status);
  body.append(form,results,disclosure("这条路线上已经问过的问题",previous),
              button("直接填写对照条件",()=>experimentForm(view),"link"));
  const names=Object.fromEntries(view.actors.map(a=>[a.id,a.name]));
  function render(plan,root){
    root.replaceChildren(h("p",plan.calls ? "本次拟定使用 1 次模型调用。" : plan.method==="reused" ? "复用了同一问题和场景的模型答复，本次没有新增调用。" : "本次拟定未调用模型。","quiet"));
    if(plan.clarification)root.append(h("p",plan.clarification,"plan-clarification"));
    if(!plan.ideas.length)return;
    for(const idea of plan.ideas){
      const card=ideaPreview(idea),e=idea.experiment;
      if(idea.watch_note)card.append(h("p",idea.watch_note,"hint"));
      const levels=1+(idea.additional_values||[]).length,estimate=levels*(e.through-e.window)*(view.actors.length*view.rounds_per_window+1);
      if(e.ends_window)card.append(h("small","临时条件只在第 "+(e.window+1)+" 至 "+e.ends_window+" 窗有效。","quiet"));
      if(["field","condition"].includes(e.kind))card.append(h("small","窗口开始时　"+(e.audience==="public" ? "通知所有主体" : e.audience==="readers" ? "通知"+e.readers.map(a=>names[a] || a).join("、") : "仅世界系统知情")+
        (e.kind==="field" ? "，字段在窗末提交，之后按原可见范围呈现。" : "。"),"hint"));
      card.append(h("small",levels+" 档各跑一次，主体逐轮行动时约需 "+estimate+" 次基础调用。","quiet"),
        button("查看并编辑这条方案",()=>experimentForm(view,idea),"secondary"));root.append(card);
    }
  }
  function saved(plan){
    const detail=h("div"),item=disclosure(plan.question,detail);item.ontoggle=()=>{if(item.open && !detail.childElementCount)render(plan,detail);};return item;
  }
  form.onsubmit=async event=>{
    event.preventDefault();if(busy||go.disabled)return;const q=area.value.trim();if(!q){area.focus();return;}
    go.disabled=true;status.textContent="正在对照已有主体、字段和已提交的时段…";results.replaceChildren();
    try{
      await job("experiment_plan",path+"/jobs/{id}/experiment-plan",{question:q,default_split:from,allow_model:allow.checked},async plan=>{
        render(plan,results);previous.prepend(saved(plan));status.textContent=plan.clarification && !plan.ideas.length ? "可补充上方问题后再次拟定。" : "方案已保存。";
      },run);
    }catch(e){status.textContent=e.message;}finally{go.disabled=false;}
  };
  reading("问这条路线一个如果",body);
  safe(async()=>{const rows=await api(path+"/experiment-plans");if(previous.isConnected && !previous.childElementCount)previous.replaceChildren(...rows.map(saved));})();
}
