"use strict";

function eventShiftPicker(run,seed=null) {
  const node=h("fieldset",null,"value-picker"),which=h("select"),date=h("input"),notice=h("p",null,"hint");
  const legend=h("legend","改变已有事件的日期");node.append(legend);
  function field(name,input){const tag=h("label",name);input.setAttribute("aria-label",name);tag.append(input);node.append(tag);return tag;}
  which.required=true;date.type="number";date.step="1";date.required=true;
  const eventLabel=field("要改期的外部事件",which),dateLabel=field("改到第几窗",date);node.append(notice);
  let items=null,request=null,from=0,until=0,chosen="",cancel=false;
  function showDate(){
    const item=items?.find(i=>i.event.id===which.value)?.event;
    if(!item){notice.textContent="这个观察区间没有可调整的预定事件。";return;}
    if(which.value!==chosen){
      chosen=which.value;
      date.value=String(seed?.event_id===chosen && seed.new_window!=null ? seed.new_window : item.window+1);
    }
    if(cancel){notice.textContent="原定第 "+item.window+" 窗发生。取消后，预设通知与字段赋值一并撤销，主体继续依据此前所知行动。";return;}
    const n=Number(date.value);notice.textContent="原定第 "+item.window+" 窗发生。"+
      (n>until ? "新日期晚于本次比较终点。":"原有通知范围和预定字段值沿用。");
  }
  which.onchange=showDate;date.oninput=showDate;
  function draw(){
    const selected=which.value || seed?.event_id;
    const eligible=items.filter(i=>i.status!=="cancelled" && i.event.window>from && (!cancel || i.event.window<=until));
    which.replaceChildren(...eligible.map(i=>new Option("第 "+i.event.window+" 窗 · "+i.event.text,i.event.id)));
    if(eligible.some(i=>i.event.id===selected))which.value=selected;
    which.disabled=false;date.min=String(from+1);showDate();
  }
  return {node,update(enabled,start,end,cancelling=false){
    node.disabled=node.hidden=!enabled;from=start;until=end;cancel=cancelling;
    dateLabel.hidden=date.disabled=cancel;date.required=!cancel;
    legend.textContent=cancel ? "取消一个预定事件" : "改变已有事件的日期";
    const label=cancel ? "要取消的外部事件" : "要改期的外部事件";
    eventLabel.firstChild.textContent=label;which.setAttribute("aria-label",label);
    if(!enabled)return;
    if(items){draw();return;}
    if(request)return;
    which.disabled=true;notice.textContent="正在读取原路线的日历…";
    request=api("/api/runs/"+run+"/calendar").then(rows=>{items=rows;draw();}).catch(err=>{request=null;notice.textContent=err.message;});
  },read(){
    if(!which.value || !items)throw new Error("请选择尚未发生的原定事件。");
    return {event_id:which.value,new_window:cancel ? null : Number(date.value)};
  }};
}

function ideaPreview(idea) {
  const card=h("article",null,"comparison-idea"),origin={material:"文稿中的对照问题",calendar:"依据已设日历",route:"依据线路时效",model:"模型拟定的方案",observer:"从所提问题直接识别"};
  card.append(h("small",origin[idea.origin] || "待试设想","eyebrow"),h("h3",idea.question));
  if(idea.reason)card.append(h("p",idea.reason));
  if(idea.condition && idea.condition!==idea.reason)card.append(h("small",idea.condition,"hint"));
  const e=idea.experiment;
  if(idea.additional_values?.length)card.append(h("p","研究取值　"+[e.assignment.value,...idea.additional_values].join(" / ")));
  card.append(h("small","从"+windowName(e.window)+"至"+windowName(e.through)+" · "+(idea.watch?.title || "比较过程和终点状态"),"quiet"));
  if(idea.quote)card.append(disclosure(idea.quote.source_id==="observer_question" ? "展开提问原句" : "展开材料原句",h("p","“"+idea.quote.quote+"”","hint")));
  return card;
}

function draftBrief(scene,config) {
  const body=h("div"),brief=scene.public_brief;
  const inferred=!brief && scene.sources?.length && (scene.scheduled_events?.length || scene.rules.some(r=>!r.visibility?.public) ||
    scene.entities.some(a=>[...a.fields,...a.materials].some(item=>!item.visibility?.public)));
  body.append(h("p",brief ? brief.text || "没有统一的公开简报。各主体从自己的档案、可见资料和许可范围内的通知开始。" :
    inferred ? "共同简报取自材料中标明公开的段落。打开主体预览可查看实际文字。" : scene.background));
  if(brief)body.append(h("small",brief.provenance?.explanation || "共同简报在开局发给所有主体。","quiet"));
  body.append(button("预览主体的开局所见",()=>openingPreview(scene,config),"link"),button("编辑共同简报",async()=>{
    const form=h("form",null,"experiment-form"),area=h("textarea",null,"editor"),error=h("p",null,"form-error"),save=h("button","保存共同简报","primary");
    const label=h("label","所有主体开局可读的文字");area.rows=5;area.maxLength=4800;
    const view=inferred ? await api("/api/opening-view","POST",{scenario:scene,config,actor_id:scene.entities[0].id}) : null;
    if(!body.isConnected)return;area.value=brief ? brief.text : view ? view.background : scene.background;
    label.append(area);save.type="submit";form.append(label,h("p","只写所有主体都能获知的内容。留空时，每个主体仍会收到自己的档案和可见资料。","hint"),error,save);
    form.onsubmit=async e=>{e.preventDefault();if(save.disabled)return;save.disabled=true;
      const old=scene.public_brief;scene.public_brief={text:area.value.trim(),provenance:{kind:"user",explanation:"操作者明确设定共同开局简报。"}};
      try{await saveProject("修改共同开局简报");$("reading").close();renderDraft();}
      catch(err){scene.public_brief=old;error.textContent=err.message;}finally{save.disabled=false;}
    };
    reading("编辑共同简报",form);
  },"link"));
  return disclosure("各主体的开局信息",body);
}

function openingPreview(scene,config) {
  const body=h("div"),select=h("select"),content=h("div");
  select.setAttribute("aria-label","预览哪个主体的开局信息");select.append(...scene.entities.map(a=>new Option(a.name,a.id)));
  body.append(select,content);let ticket=0;
  async function load(){
    const seq=++ticket,view=await api("/api/opening-view","POST",{scenario:scene,config,actor_id:select.value});
    if(!body.isConnected || seq!==ticket)return;
    const names=Object.fromEntries(view.roster.map(a=>[a.id,a.name]));
    content.replaceChildren(h("h3",view.self.name+"的开局所见"),h("p",view.self.identity+"　"+(view.self.preferences||"")),
      h("h4","共同简报"),h("p",view.background || "本场从各自的档案与资料开始。","hint"));
    if(Object.keys(view.state).length)content.append(h("h4","实际可见的状态"),bullets(Object.entries(view.state).map(([id,text])=>(names[id]||id)+" · "+text)));
    if(view.visible_materials.length)content.append(disclosure("可见资料（"+view.visible_materials.length+"）",...view.visible_materials.map(m=>h("p",m.text))));
    if(view.rules.length)content.append(disclosure("已知规则与条件",...view.rules.map(r=>h("p",r.text))));
    if(view.documents?.length)content.append(h("h4","已持有原件"),bullets(view.documents.map(d=>d.title)));
    if(view.external_events?.length)content.append(h("h4","第一窗开始时按设定会收到的通知"),bullets(view.external_events.map(e=>e.text)));
  }
  select.onchange=safe(load);reading("开局信息预览",h("p","按当前设定在本机展开。启动后，实际收件和决定会另行记录。","hint"),body);safe(load)();
}

async function runIdeas(view,root) {
  const run=activeRun,box=h("div",null,"comparison-ideas");
  root.append(box);
  const ideas=await api("/api/runs/"+run+"/ideas");
  if(!box.isConnected || activeRun!==run)return;
  for(const idea of ideas){
    const card=ideaPreview(idea);
    if(idea.ready && view.status.window>=idea.experiment.through)card.append(button("按这个问题对照",()=>experimentForm(view,idea),"link"));
    else card.append(h("small","原路线到达"+windowName(idea.experiment.through)+"后可开始对照。","quiet"));
    box.append(card);
  }
}
