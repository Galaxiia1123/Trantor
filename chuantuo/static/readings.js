"use strict";

function draftMeanings(scene){
  const items=scene.entities.flatMap(a=>a.fields.filter(f=>f.constraints?.choices?.length).map(f=>({a,f})));
  if(!items.length)return h("span");
  const panel=h("div");
  for(const {a,f} of items)for(const m of f.constraints.meanings || [])
    panel.append(h("p",a.name+" · "+(f.label||f.id)+"　“"+m.value+"”指"+m.text),
      ...((m.provenance?.quotes || []).map(q=>h("small","原文　"+q.quote,"quiet"))));
  if(!panel.childElementCount)panel.append(h("p","候选值目前只有名称。若“通过”或“开班”有不同理解，可以说明它成立的条件。","hint"));
  panel.append(button("编辑一个候选值的含义",()=>{
    const form=h("form",null,"experiment-form"),field=h("select"),value=h("select"),text=h("textarea",null,"editor"),err=h("p",null,"form-error");
    const label=(title,input)=>{const l=h("label",title);l.append(input);form.append(l);};
    for(const [i,{a,f}] of items.entries())field.append(new Option(a.name+" · "+(f.label||f.id),i));
    label("选择要定义的字段",field);label("要说明的值",value);label("这个值在本场景中代表什么",text);text.maxLength=500;
    const pick=()=>items[Number(field.value)].f;
    function load(){text.value=pick().constraints.meanings?.find(m=>m.value===value.value)?.text || "";}
    function options(){value.replaceChildren(...pick().constraints.choices.map(v=>new Option(v,v)));load();}
    field.onchange=options;value.onchange=load;options();
    const save=h("button","保存含义","primary");save.type="submit";form.append(h("p","定义随字段的可见范围提供给主体，世界结算也会读取。清空文字可移除本项定义。","hint"),err,save);
    form.onsubmit=async e=>{
      e.preventDefault();if(save.disabled)return;save.disabled=true;
      const f=pick(),old=f.constraints.meanings || [],rest=old.filter(m=>m.value!==value.value);
      if(text.value.trim())rest.push({value:value.value,text:text.value.trim(),provenance:{kind:"user",explanation:"操作者明确了这个候选值的含义。"}});
      f.constraints.meanings=rest;
      try{await saveProject("编辑候选值的含义");$("reading").close();renderDraft();}
      catch(ex){f.constraints.meanings=old;err.textContent=ex.message;}finally{save.disabled=false;}
    };
    reading("定义结果的含义",form);
  },"link"));return disclosure("结果怎样才算成立",panel);
}

function resultReading(view,info,run){
  const field=view.actors.find(a=>a.id===info.actor_id)?.fields?.[info.field_id];
  const form=h("form",null,"experiment-form"),value=h("select"),a=h("textarea",null,"editor"),b=h("textarea",null,"editor");
  const label=(name,input)=>{const l=h("label",name);l.append(input);form.append(l);};
  value.append(...field.constraints.choices.map(v=>new Option(v,v)));
  value.value=field.constraints.choices.includes(info.after) ? info.after : field.constraints.choices[0];
  form.append(h("p",windowName(info.window)+"　"+info.actor+" · "+info.label+"　存档值为“"+(info.after??"未知")+"”。","eyebrow"));
  label("要判读的候选值",value);label("第一种口径",a);label("第二种口径（可选）",b);
  a.required=true;a.maxLength=b.maxLength=500;a.placeholder="哪些可核对的行为或事实足以达到这个值？";b.placeholder="如果要按另一种含义理解，在这里写下成立条件。";
  function fill(){a.value=field.constraints.meanings?.find(m=>m.value===value.value)?.text || "";b.value="";}
  value.onchange=fill;fill();
  const go=h("button","用同一组记录判读","primary"),msg=h("p","","form-error"),history=h("div");
  go.type="submit";msg.setAttribute("aria-live","polite");form.append(h("p","使用本窗的原始行动、收件与可见事件。两个口径共用一次判读，格式或引用出错时最多再试一次。"+
    (view.mode==="sample" ? "本机脚本仅演示引用。" : ""),"hint"),go,msg);
  const path="/api/runs/"+run,q=new URLSearchParams({window:info.window,actor:info.actor_id,field:info.field_id});
  const heading={supported:"记录支持",contradicted:"记录中有相反证据",unknown:"现有证据不足"};
  function card(saved){
    const box=h("article",null,"reading-result");
    box.append(h("h3","按所选口径判断“"+saved.value+"”"),h("small","原路线当窗提交的值为“"+(saved.original??"未知")+"”。","quiet"));
    for(const finding of saved.findings){
      const row=h("section");row.append(h("p",saved.criteria[finding.index-1]),h("strong",heading[finding.status]),h("p",finding.reason),evidenceLinks(finding.evidence_ids,run));box.append(row);
    }
    box.append(h("small","本次提供 "+saved.coverage.provided+" / "+saved.coverage.eligible+" 条记录或节选。","hint"));return box;
  }
  form.onsubmit=async e=>{
    e.preventDefault();if(busy||go.disabled)return;
    const criteria=[a.value.trim(),b.value.trim()].filter(Boolean);
    if(!a.value.trim() || new Set(criteria).size!==criteria.length){msg.textContent="请填写第一种口径。第二种口径可以留空或填写不同定义。";return;}
    msg.textContent="正在对同一份记录进行判读…";go.disabled=true;
    try{
      await job("field_reading",path+"/jobs/{id}/field-reading",{actor_id:info.actor_id,field_id:info.field_id,window:info.window,value:value.value,criteria},
        async result=>{msg.textContent="判读已单独保存。";const resultCard=card(result);history.prepend(resultCard);
          if(resultCard.isConnected)resultCard.scrollIntoView({block:"nearest"});},run);
    }catch(ex){msg.textContent=ex.message;}finally{go.disabled=false;}
  };
  reading("用不同口径读这一窗",form,history);
  safe(async()=>{const records=await api(path+"/field-readings?"+q);if(history.isConnected && !history.childElementCount)history.replaceChildren(...records.reverse().map(card));})();
}
