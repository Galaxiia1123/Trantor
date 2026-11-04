"use strict";

function openLetterLab() {
  return caseChoices("如果签字函按时送达","第一天，维修社提出申请，运营社要求有效签字函。原路线中函件未到。选择第二天发生什么，两条路线都会走到第三天。","letter",[
    ["notified","按时送达，并通知运营社","收件台收到有效签字函，运营社能在本轮核对。"],
    ["unseen","按时送达，暂不通知运营社","世界记录了送达，运营社的观察中还没有这条消息。"],
    ["repeat","维持原条件","不增加送达事件，从相同状态重跑。"]
  ]);
}

function openPostLab() {
  return caseChoices("封存单赶得上交接吗","山顶发货站持有一张封存单，收货站要在第二个班次结束前登记。通常山路运单耗时两窗。选择一项改动，观察收货站实际拿到了什么。","post",[
    ["express","提前一个窗口送达","第一窗随信寄出副本，快线在第二窗开始时送到。"],
    ["notice","只发办理进度","封存单正在补签，信件按原时效送达，正文中没有附寄副本。"],
    ["repeat","保持原时效重跑","保留两窗的运送时间和同一份登记原件。"]
  ]);
}

function caseChoices(heading,intro,path,choices) {
  const body=h("div",null,"letter-choices"),feedback=h("p","","form-error");body.append(h("p",intro));
  for (const [id,title,description] of choices) {
    const choice=h("button",null,"letter-choice"); choice.type="button";
    choice.append(h("strong",title),h("small",description));
    choice.onclick=async()=>{
      const buttons=body.querySelectorAll("button"); buttons.forEach(b=>b.disabled=true);
      feedback.textContent="正在本机推演两条路线…";
      try {
        const result=await api("/api/case/"+path+"/"+id,"POST");
        if (!body.isConnected || !$("reading").open) return;
        $("reading").close(); location.hash="run/"+result.run_id+"/"+result.window;
      } catch(err) { feedback.textContent=err.message; } finally { buttons.forEach(b=>b.disabled=false); }
    };
    body.append(choice);
  }
  body.append(feedback,h("p","三个选项均使用本机规则脚本。结果可回看，整个案例无需调用模型。","hint"));
  reading(heading,body);
}

function experimentForm(view,idea=null,priorWindow=null) {
  const run = activeRun, committed = view.status.window, earliest = view.status.experiment?.window || 0;
  const preset=idea?.experiment;
  const form = h("form", null, "experiment-form"), warning = h("p", null, "form-error");
  const select = (label, choices) => {
    const field = h("select");
    field.setAttribute("aria-label",label);
    for (const [value, name] of choices) field.append(new Option(name, value));
    const wrap = h("label",label); wrap.append(field); form.append(wrap);
    return field;
  };
  const picker=scheduledValuePicker(view.actors,false);
  const modes=[["condition","改变一个条件"]];
  if(picker.count)modes.push(["field","设定一个字段的新值"]);
  if(!earliest && picker.openingCount)modes.push(["opening","改变开局初值"]);
  const timing=routeTimingPicker(view.actors);
  if(view.actors.length>=2)modes.push(["transit","改变一条线路的送达时间"]);
  const shift=eventShiftPicker(run,preset?.event_move);
  if(view.scheduled_count)modes.push(["event_time","改变外部事件的发生日期"]);
  modes.push(["repeat","按相同条件再运行一次"]);
  const kind = select("探索方式",modes);
  const range = Array.from({length:committed - earliest}, (_,i) => earliest + i);
  const split = select("从哪里分岔",range.map(w=>[w,w ? "第 " + w + " 窗结算后" : "从开局"]));
  split.value = String(Math.max(earliest,Math.min(priorWindow ?? view.settled_through,committed-1)));
  const through = select("比较到哪个窗口",[]);
  const prompt = h("textarea",null,"editor"); prompt.maxLength = 1600; prompt.rows = 3;
  prompt.placeholder = "例如：运营社在本窗开始时收到一份有效签字函。";
  const levels=numericLevels(picker);
  const label = h("label","改变的条件"); label.append(prompt); form.append(label,picker.node,levels.node,timing.node,shift.node);
  const endInput=h("input"),endLabel=h("label","条件最后有效的窗口（可选）");endInput.type="number";endInput.step="1";endInput.placeholder="持续有效";endLabel.append(endInput);
  const duration=disclosure("设置临时条件的有效期",endLabel,h("p","含最后一窗。到期后继续观察，已有的行动和已结算结果仍保留。","hint"));form.append(duration);
  const openingHint=h("p","首轮开始前就采用新值。只有原本能读这个字段的主体会看到它，运行中的固定约束继续生效。","hint");form.append(openingHint);
  const audience = select("开始时谁知道这个条件",[["world","仅世界系统"],["public","所有主体"],["readers","指定主体和世界系统"]]);
  const group = h("fieldset"); group.append(h("legend","知情主体"));
  for (const actor of view.actors) {
    const label = h("label",null,"check"), box = h("input"); box.type="checkbox"; box.value=actor.id;
    label.append(box,document.createTextNode(actor.name)); group.append(label);
  }
  const repeats=select("新路线数量",[[1,"1 条"],[2,"2 条"],[3,"3 条"],[5,"5 条"],[8,"8 条"],[16,"16 条"]]);
  const pair=h("input"),pairLabel=h("label",null,"check pair-choice");pair.type="checkbox";
  pairLabel.append(pair,document.createTextNode("也按原条件运行相同次数，分别比较两组结果"));form.append(pairLabel);
  const study=studyOptions(view,run,idea?.watch);
  const cost=h("p",null,"experiment-estimate");
  const submit=h("button","启动新路线","primary"); submit.type="submit";
  form.append(group,study.node,cost,warning,submit);
  function limits() {
    const from=Number(split.value), prior=Number(through.value);
    through.replaceChildren(...range.map(w=>w+1).filter(w=>w>from).map(w=>new Option("第 "+w+" 窗",w)));
    through.value=String(prior>from && prior<=committed ? prior : committed);
  }
  function update() {
    const opening=kind.value==="opening", changed=["condition","field"].includes(kind.value), fieldMode=kind.value==="field" || opening;
    picker.setOpening(opening);openingHint.hidden=!opening;
    if(opening && split.value!=="0"){split.value="0";limits();}
    split.disabled=opening;
    duration.hidden=endInput.disabled=kind.value!=="condition";endInput.min=String(Number(split.value)+1);
    levels.show(fieldMode);const extra=levels.tokens().length;
    label.hidden=kind.value!=="condition";audience.parentElement.hidden=!changed;prompt.required=kind.value==="condition";
    timing.node.hidden=timing.node.disabled=kind.value!=="transit";
    shift.update(kind.value==="event_time",Number(split.value),Number(through.value));
    picker.node.hidden=picker.node.disabled=!fieldMode;
    group.hidden=!changed || audience.value!=="readers";
    const windows=Number(through.value)-Number(split.value);
    const calls=windows*(view.actors.length*view.rounds_per_window+1);
    const count=Number(repeats.value);
    pairLabel.hidden=pair.disabled=kind.value==="repeat";
    const contrast=!pair.disabled && pair.checked, conditions=1+extra+(contrast ? 1 : 0),total=count*conditions;
    repeats.parentElement.firstChild.textContent=extra ? "每个取值运行几次" : "新路线数量";
    repeats.setAttribute("aria-label",repeats.parentElement.firstChild.textContent);
    pairLabel.lastChild.textContent=extra ? "也按原条件运行相同次数，单独列出结果" : "也按原条件运行相同次数，分别比较两组结果";
    study.update(total,calls);
    submit.textContent=extra ? "启动多档对照" : contrast ? "启动双条件对照" : count===1 ? "启动新路线" : "启动这组实验";
    cost.textContent=(extra ? conditions+" 档条件，每档 "+count+" 次，共 "+total+" 条路线。":
      contrast ? "原条件与改后条件分别运行 "+count+" 次，每条 "+windows+" 窗。" :
      count===1 ? "只重跑新路线的 "+windows+" 个窗口。" : count+" 条路线依次各跑 "+windows+" 个窗口。")+
      "全部主体逐轮判断约需 "+(calls*total)+" 次基础调用，纠正与重试会增加用量。"+
      (view.status.max_review_interval ? "主体可选择延续等待。" : "")+(contrast ? "存档原路线另作参照。" : "原路线使用已提交记录。");
    if(view.mode==="sample")cost.textContent+=" 这条路线使用本机案例脚本，不产生 API 费用。";
  }
  split.onchange=()=>{limits();update();}; through.onchange=kind.onchange=audience.onchange=repeats.onchange=pair.onchange=update;
  levels.field.oninput=update;picker.node.addEventListener("change",update);
  if(preset){
    kind.value=preset.kind;split.value=String(preset.window);limits();through.value=String(preset.through);
    prompt.value=preset.condition || "";audience.value=preset.audience || "world";
    if(preset.ends_window){endInput.value=String(preset.ends_window);duration.open=true;}
    for(const box of group.querySelectorAll("input"))box.checked=(preset.readers || []).includes(box.value);
    picker.setOpening(preset.kind==="opening");picker.set(preset.assignment);timing.set(preset.route);
    if(idea.additional_values?.length){levels.show(true);levels.field.value=idea.additional_values.join("、");levels.node.open=true;}
  }else limits();
  update();
  form.onsubmit=async event=>{
    event.preventDefault(); if (busy || submit.disabled || activeRun!==run) return;
    const changed=["condition","field"].includes(kind.value), scope=changed ? audience.value : "world";
    const readers=scope==="readers" ? [...group.querySelectorAll("input:checked")].map(el=>el.value) : [];
    if (scope==="readers" && !readers.length) { warning.textContent="请选择至少一个知情主体。"; return; }
    if (kind.value==="condition" && !prompt.value.trim()) {warning.textContent="请写下要改变的条件。";prompt.focus();return;}
    submit.disabled=true; warning.textContent="";
    try {
      const spec={window:Number(split.value),through:Number(through.value),kind:kind.value,condition:kind.value==="condition" ? prompt.value.trim() : "",
                  assignment:["field","opening"].includes(kind.value) ? picker.read() : null,route:kind.value==="transit" ? timing.read() : null,
                  event_move:kind.value==="event_time" ? shift.read() : null,audience:scope,readers};
      if(kind.value==="condition" && endInput.value!=="")spec.ends_window=Number(endInput.value);
      const contrast=!pair.disabled && pair.checked;
      const extra=levels.read(spec.assignment?.value);
      if(extra.length && (1+extra.length+Number(contrast))*Number(repeats.value)>32)throw new Error("一组多档实验最多安排 32 条路线，请减少取值或重复次数。");
      if(Number(repeats.value)>1 || contrast || extra.length) {
        const result=await study.submit(spec,Number(repeats.value),contrast,extra);
        if(activeRun===run && form.isConnected && $("reading").open) {$("reading").close(); location.hash="study/"+result.id;}
      } else await job("experiment","/api/jobs/{id}/experiment/"+run,spec,async result=>{
        let warning="";
        if(idea?.watch){
          try{
            const existing=await api("/api/runs/"+result.run_id+"/watches");
            if(!existing.some(w=>sameValue(w.question,idea.watch)))await api("/api/runs/"+result.run_id+"/watches/"+uid(),"PUT",idea.watch);
          }catch{warning="路线已启动。建议观察卡未能保存，可在观察页重新添加。";}
        }
        $("reading").close();location.hash="run/"+result.run_id;if(warning)notice(warning);
      });
    } catch (err) {warning.textContent=err.message;} finally {submit.disabled=false;}
  };
  reading(idea?.question || "从同一时刻再走一次",h("p",idea ? "已经填入这项设想。可修改条件、窗口和运行次数，按启动后才会调用模型。" :
    "两条路线在分岔前拥有相同的状态、私人记忆和已送达消息。新的条件从下一窗开始生效。","hint"),form);
}

const experimentKind = meta => ({extend:"延长观察",repeat:"原样重跑",field:"字段实验",opening:"开局初值实验",transit:"线路实验",event_time:"日历实验"}[meta.kind] || "条件实验");

function extendRun(view){
  const run=activeRun,from=view.status.window,form=h("form",null,"experiment-form"),extra=h("input"),cap=h("input");
  const label=(text,field)=>{const el=h("label",text);el.append(field);return el;};
  extra.type=cap.type="number";extra.required=cap.required=true;extra.min=cap.min="1";extra.max="100";cap.max="1000000";
  extra.value="2";const hint=h("p",null,"experiment-estimate"),error=h("p",null,"form-error"),go=h("button","开始延长观察","primary");
  go.type="submit";let edited=false;
  function update(){
    const n=Number(extra.value);
    if(!Number.isInteger(n)||n<1||n>100){hint.textContent="请选择 1 至 100 个新增窗口。";return;}
    const estimated=n*(view.actors.length*view.rounds_per_window+1);
    if(!edited)cap.value=String(Math.min(1000000,estimated+Math.max(2,Math.ceil(estimated/4))));
    hint.textContent="从第 "+(from+1)+" 窗走到第 "+(from+n)+" 窗。每个主体逐轮行动时约需 "+estimated+
      " 次新调用，等待会减少调用，纠正可能增加用量。"+(view.mode==="sample" ? "本机示例脚本不使用 API 额度。" : "");
  }
  extra.oninput=update;cap.oninput=()=>{edited=true;};update();
  form.append(h("p","从第 "+from+" 窗最后一次结算继续。主体的记忆、持续事项和仍在途中的信件随新路线保留。原来的报告和实验结果可以照常回看。"),
    label("再观察几个窗口",extra),label("新增窗口的模型调用上限",cap),hint,error,go);
  form.onsubmit=async e=>{
    e.preventDefault();if(busy||go.disabled||run!==activeRun)return;
    go.disabled=true;error.textContent="";
    try{
      await job("experiment","/api/jobs/{id}/experiment/"+run,{kind:"extend",window:from,through:from+Number(extra.value),extension_limit:Number(cap.value)},
        async result=>{$("reading").close();location.hash="run/"+result.run_id+"/"+(from+1);});
    }catch(err){error.textContent=err.message;}finally{go.disabled=false;}
  };
  reading("从原终点继续观察",form);
}
const wakeText = reason => ({message:"收到定向消息，重新判断。",external_event:"收到外部事件通知，重新判断。",visible_change:"可见处境发生变化，重新判断。",due:"到达复看窗口或事项期限，重新判断。"}[reason] || "主体重新判断。");

async function experimentShelf(parent) {
  const body=h("div"), select=h("select"), content=h("div"), description=h("p","按同一分岔点、终点、条件和知情范围分组。次数只计入已走到终点的路线。","hint");
  select.setAttribute("aria-label","对照哪个终点字段"); body.append(description,select,content);
  reading("同一起点的路线",body);
  let request=0;
  const display=async(field)=>{
    const ticket=++request;
    const query=field ? "?actor="+encodeURIComponent(field.actor)+"&field="+encodeURIComponent(field.field) : "";
    const result=await api("/api/runs/"+parent+"/alternatives"+query);
    if (!body.isConnected || ticket!==request) return;
    select.hidden=!result.fields.length; select.replaceChildren(...result.fields.map((f,i)=>new Option(f.label,i)));
    select.selectedIndex=result.fields.findIndex(f=>f.actor===result.selected?.actor && f.field===result.selected?.field);
    select.onchange=safe(()=>display(result.fields[select.selectedIndex])); content.replaceChildren();
    if (!result.groups.length) content.append(h("p","这个起点还没有保存其他路线。","empty"));
    const format=v=>v==null ? "未知" : typeof v==="boolean" ? (v ? "是" : "否") : String(v)+(result.selected?.unit||"");
    for (const group of result.groups) {
      const s=group.spec, card=h("article",null,"cohort");
      card.append(h("small","第 "+s.window+" 窗后 → 第 "+s.through+" 窗 · "+group.completed+"/"+group.runs.length+" 条完成","quiet"),
        h("h3",s.condition || "原条件重跑"));
      if(s.kind==="extend")card.append(h("small","原路线在本次延长的起点结束。","quiet"));
      else if(s.kind==="event_time")card.append(h("small","只移动原日历事件，通知对象和事件内容沿用原设定。","quiet"));
      else if(s.kind==="transit")card.append(h("small","通信双方知道线路时效。分岔前已经寄出的消息仍按原定日期送达。","quiet"));
      else if(s.kind!=="repeat") card.append(h("small","初始知情范围："+(s.audience==="public" ? "所有主体" : s.audience==="world" ? "仅世界系统" : s.readers.map(id=>result.actors[id] || id).join("、")+"与世界系统"),"quiet"));
      if(s.ends_window)card.append(h("small","临时条件有效至第 "+s.ends_window+" 窗末。","quiet"));
      if (result.selected) {
        card.append(h("p",(s.kind==="extend" ? "延长起点的值为" : "原路线终值为")+format(group.baseline)+"。"));
        for (const o of group.outcomes) card.append(h("span",format(o.value)+" · "+o.count+" 次","outcome-count"));
      }
      const links=h("div",null,"cohort-links");
      group.runs.forEach((r,i)=>{const a=h("a","路线 "+(group.runs.length-i)+" · "+(statuses[r.status]||r.status));a.href="#run/"+r.run_id+"/"+r.window;
        a.onclick=()=>$("reading").close();links.append(a);});
      card.append(disclosure("打开路线记录",links)); content.append(card);
    }
  };
  await display(null);
}

function showExperimentTools(view) {
  const root=$("experiment-tools"), meta=view.status.experiment;
  const letter=view.mode==="sample" && ["letter-desk-v1","letter-desk-v2","letter-desk-v3"].includes(view.model);
  const post=view.mode==="sample" && view.model==="mountain-post-v1";
  const available=!playback() && view.format_version==="1.1" && view.status.window>(meta?.window || 0);
  const stamp=[activeRun,view.selected,view.status.window,view.settled_through].join("/");
  if (root.dataset.stamp===stamp && root.childElementCount) return;
  root.dataset.stamp=stamp;
  root.hidden=!available && !meta; root.replaceChildren();
  if (root.hidden) return;
  const bar=h("div",null,"experiment-bar"), text=h("div");
  text.append(h("span",meta ? experimentKind(meta) : "条件实验","eyebrow"),
    h("h2",meta ? (meta.condition || "同一开局，再走一次") : "如果有一个条件不同呢？"));
  bar.append(text);
  const choices=h("div",null,"actions");
  if (available) {
    if(!letter && !post)choices.append(button("问一个如果",()=>askExperiment(view),"experiment-start"));
    choices.append(button(meta ? "再开一条路线" : "试一个条件",letter ? openLetterLab : post ? openPostLab : ()=>experimentForm(view),letter||post ? "experiment-start" : "link"));
    if(letter || post)choices.append(button("问一个如果",()=>askExperiment(view),"link"));
  }
  choices.append(button("已有路线",()=>experimentShelf(meta?.parent_id || activeRun),"link")); bar.append(choices);
  root.append(bar);
  if (!meta) {
    root.append(h("p","保留当时各方的记忆，改变一个条件，对照之后的决定和结果。","hint"));
    if(view.has_comparisons && view.mode!=="sample")safe(()=>runIdeas(view,root))();return;
  }
  const source=h("a","回看原路线"); source.href="#run/"+meta.parent_id+"/"+meta.window;
  if(meta.kind==="extend"){
    source.textContent="回到原路线终点";
    const from=meta.window,w=view.settled_through,body=h("div",null,"continuation-result");root.append(body,source);
    if(w<=from){
      body.append(h("p",view.selected<from ? "这一段沿用原路线的已提交记录。" : "原路线到此结束，后面的发展会记在当前路线。","hint"));return;
    }
    const run=activeRun;
    api("/api/runs/"+run+"/experiment?window="+w).then(result=>{
      if(run!==activeRun || !body.isConnected)return;
      const changed=result.rows.filter(row=>row.status==="different");
      body.append(h("strong","从第 "+from+" 窗末到第 "+w+" 窗末"),h("p","新结算 "+result.observed+" 窗，"+(result.changed_windows ?
        "其中 "+result.changed_windows+" 窗有字段变化。" : "已定义字段保持原值。")));
      for(const row of changed){
        const f=row.right;
        body.append(button(f.actor_name+" · "+f.label+"　"+watchValue(row.left,f.unit)+" → "+watchValue(f,f.unit),
          ()=>openFieldReason(view,row.actor_id,row.field_id,w),"link"));
      }
      if(result.first_change && result.first_change!==w)body.append(button("看首次变化 · "+windowName(result.first_change),()=>pinWindow(result.first_change),"link"));
    }).catch(e=>{if(body.isConnected)body.textContent=e.message;});return;
  }
  const origin=h("p",null,"hint"); origin.append("第 "+meta.window+" 窗后分岔，比较至第 "+meta.through+" 窗。 ",source);
  if(meta.ends_window)origin.append("　条件从第 "+(meta.window+1)+" 窗起生效，至第 "+meta.ends_window+" 窗末结束。");
  if(meta.kind==="event_time"){
    origin.append("　事件内容与通知对象沿用原日历。");
    if(meta.original_event?.record)origin.append(button("查阅原日期的事件凭据",()=>evidence(meta.original_event.record,meta.parent_id),"link"));
  }else if(meta.kind==="opening")origin.append("　原初值“"+(meta.opening_change.before??"未知")+"”，新初值“"+(meta.opening_change.after??"未知")+"”。第一轮按该字段原有的可见范围呈现。");
  else if(meta.kind==="transit")origin.append("　新时效只用于分岔后的新发件。通信双方能看到这个时效。");
  else if (meta.kind!=="repeat") {
    const allowed=view.actors.filter(a=>meta.readers.includes(a.id)).map(a=>a.name);
    origin.append("　最初知情范围为"+(meta.audience==="public" ? "所有主体" : meta.audience==="world" ? "仅世界系统" : allowed.join("、")+"与世界系统")+"。");
  }
  root.append(origin);
  if (view.selected<meta.window) {root.append(h("p","此时两条路线仍在共享同一段历史。","hint"));return;}
  const summary=h("div",null,"experiment-diff"); root.append(summary);
  if (view.settled_through<=meta.window && meta.kind!=="opening") {summary.textContent="分岔后的首个窗口结算后，这里会显示对照结果。"; return;}
  const run=activeRun, w=view.settled_through;
  api("/api/runs/"+run+"/experiment?window="+w).then(result=>{
    if (activeRun===run && summary.isConnected) drawExperimentComparison(summary,result,view.actors,letter||post);
  }).catch(error=>{if(summary.isConnected) summary.replaceChildren(h("p",error.message,"error"));});
}

function drawExperimentComparison(root, result, actors, isLetter, options={}) {
  const [leftLabel,rightLabel]=options.labels || ["原路线","新路线"];
  const w=result.right.window, changed=result.rows.filter(r=>r.status==="different");
  const names=Object.fromEntries(actors.map(a=>[a.id,a.name]));
  const intro=h("div",null,"comparison-heading");
  intro.append(h("strong",(w?"第 "+w+" 窗":"开局")+" · "+changed.length+" 项状态不同"));
  const first=result.first_state_difference;
  if (first!=null && first!==w) {
    const caption=first===0?"开局已有差异":"首次状态分化 · 第 "+first+" 窗";
    if(options.selectWindow)intro.append(button(caption,()=>options.selectWindow(first),"link"));
    else{const link=h("a",caption);link.href="#run/"+result.right.run_id+"/"+first;intro.append(link);}
  } else intro.append(h("small",first===0?"开局初值不同":first!=null?"首次状态分化":"截至共同终点，未观察到字段差异","quiet"));
  root.append(intro);
  const assigned=result.experiment?.assignment, defined=result.rows.filter(row=>row.left && row.right);
  const outcomes=defined.filter(row=>row.actor_id!==assigned?.entity_id || row.field_id!==assigned?.field_id);
  const target=result.focus || outcomes.find(row=>row.status==="different") || outcomes[0] || defined[0];
  if(target)root.append(briefLink(result.left.run_id,result.right.run_id,target.actor_id,target.field_id,w));
  if(result.focus)root.append(fieldContrastCard(result.focus,result,[leftLabel,rightLabel]));
  else root.append(fieldContrastPicker(result,names,[leftLabel,rightLabel]));
  if(result.first_event_difference){
    const at=result.first_event_difference,txt="两条路线的外部事件在"+windowName(at)+"首次不同";
    if(at===w)root.append(h("p",txt+"。","hint"));
    else if(options.selectWindow)root.append(button(txt,()=>options.selectWindow(at),"link"));
    else{const a=h("a",txt);a.href="#run/"+result.right.run_id+"/"+at;root.append(a);}
  }
  const value=side=>side?.value==null ? "未知" : typeof side.value==="boolean" ? (side.value ? "是" : "否") : String(side.value)+(side.unit || "");
  const grid=h("div",null,"comparison-grid");
  for (const [side,head,proof,label] of [["left",result.left,result.baseline,leftLabel],["right",result.right,result.variant,rightLabel]]) {
    const card=h("article",null,"comparison-side"); card.append(h("h3",label));
    for (const r of changed) card.append(h("p",(names[r.actor_id]||r.actor_id)+" · "+r[side].label+"　"+value(r[side]),"comparison-fact"));
    for(const e of proof.events || []){
      const scope=e.visibility.public ? "所有主体" : e.visibility.readers.map(k=>names[k]||k).join("、");
      card.append(h("small",scope ? "本窗通知了"+scope : "本窗仅世界系统记录","quiet"),h("p",e.text),
        button("外部事件凭据 · "+e.record,()=>evidence(e.record,head.run_id),"link"));
    }
    for (const c of proof.consequences.slice(0,2)) card.append(h("p",c.text));
    if (!proof.consequences.length) card.append(h("p","此窗没有登记客观后果。","hint"));
    card.append(button(w?"查阅本窗结算":"查阅这条路线的开局",()=>evidence(head.event_id,head.run_id),"link")); grid.append(card);
  }
  root.append(grid);
  const details=h("div"), select=h("select"); select.setAttribute("aria-label","选择要比较决定的主体");
  select.append(...actors.map(a=>new Option(a.name,a.id))); const decisions=h("div"); details.append(select,decisions);
  details.append(button("查找这个主体从哪一轮开始不同",()=>showActorContrast(result,select.value,names[select.value],[leftLabel,rightLabel]),"link"));
  const redraw=()=>{
    decisions.replaceChildren();
    for (const [label,proof,head] of [[leftLabel,result.baseline,result.left],[rightLabel,result.variant,result.right]]) {
      const section=h("section"); section.append(h("h4",label));
      for (const a of proof.actions.filter(x=>x.actor_id===select.value)) {
        section.append(h("p","第 "+a.round+" 轮 · "+a.text),
          button("查看当时的决策现场",()=>openActorContext({id:a.actor_id,name:names[a.actor_id]},w,a.round,false,head.run_id),"link"));
      }
      decisions.append(section);
    }
  };
  select.onchange=redraw; redraw();
  root.append(disclosure("并排查看主体的决定",details),h("p",isLetter ? "此处展示虚构案例在固定规则下产生的结果。" : "一次对照展示这两次运行的实际差异。模型采样也会影响结果。","hint"));
}
