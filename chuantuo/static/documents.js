"use strict";

function draftDocuments(scene,review) {
  const panel=h("div");
  if(review?.restored?.some(d=>d.removed_public_copy))
    panel.append(h("p","已按原文登记有明确持有人的凭证。公共背景省去了重复的凭证正文。","hint"));
  let count=0;
  for(const actor of scene.entities)for(const material of actor.materials){
    if(!material.document)continue;
    count++;
    panel.append(disclosure(material.document.title+" · "+actor.name,h("p",material.text)));
  }
  if(scene.domains.length)panel.append(button("登记一份可寄送的原件",()=>{
    const form=h("form",null,"experiment-form"),owner=h("select"),domain=h("select"),name=h("input"),body=h("textarea"),to=h("fieldset");
    function field(label,control){const tag=h("label",label);control.setAttribute("aria-label",label);tag.append(control);form.append(tag);}
    owner.append(...scene.entities.map(a=>new Option(a.name,a.id)));domain.append(...scene.domains.map(d=>new Option(d,d)));
    name.required=true;name.maxLength=100;body.required=true;body.maxLength=1200;body.rows=4;
    field("原件持有人",owner);field("资料领域",domain);field("原件标题",name);field("原件正文",body);
    const redrawn=()=>{
      to.replaceChildren(h("legend","允许接收副本的主体"));
      for(const actor of scene.entities.filter(a=>a.id!==owner.value)){
        const check=h("input"),label=h("label",null,"check");check.type="checkbox";check.value=actor.id;
        label.append(check,document.createTextNode(actor.name));to.append(label);
      }
    };
    owner.onchange=redrawn;redrawn();const submit=h("button","登记原件","primary");submit.type="submit";
    const err=h("p",null,"form-error");form.append(to,h("p","开局只有持有人能读正文。被允许接收的人，要等实际到件才取得副本。","hint"),err,submit);
    form.onsubmit=async e=>{
      e.preventDefault();if(submit.disabled)return;submit.disabled=true;
      const actor=scene.entities.find(a=>a.id===owner.value), prior=actor.materials;
      const provenance={kind:"user",explanation:"操作者登记原件及允许流转的范围。"};
      actor.materials=[...prior,{domain:domain.value,text:body.value,visibility:{public:false,readers:[]},provenance,
        document:{id:"doc_"+uid().slice(0,12),title:name.value.trim()},
        sharing:{allowed_recipients:[...to.querySelectorAll("input:checked")].map(c=>c.value),provenance}}];
      try{await saveProject("登记可寄送的原件");$("reading").close();renderDraft();}
      catch(error){actor.materials=prior;err.textContent=error.message;}finally{submit.disabled=false;}
    };
    reading("登记原件",form);
  },"link"));
  return disclosure("可寄送的原件（"+count+"）",panel);
}

function openDocuments(view,initial=null) {
  const run=activeRun,w=view.settled_through,body=h("div"),select=h("select"),details=h("div");
  select.setAttribute("aria-label","选择要追溯的原件");body.append(select,details);
  let selected=initial,ticket=0;
  async function load(offset=0) {
    const seq=++ticket,params=new URLSearchParams({through:w,offset});
    if(selected)params.set("document",selected);
    const r=await api("/api/runs/"+run+"/documents?"+params);
    if(seq!==ticket || !body.isConnected)return;
    if(!r.selected){details.textContent="这份场景尚未登记可寄送的原件。";return;}
    if(!offset){
      selected=r.selected.id;select.replaceChildren(...r.documents.map(d=>new Option(d.title+" · "+d.origin,d.id)));select.value=selected;
      const head=h("section",null,"reason-summary");head.append(h("small",windowName(w)+" · 原件正文","eyebrow"),h("h3",r.selected.title),h("p",r.selected.text));
      details.replaceChildren(head,h("h3","这个时点谁持有"));
      for(const holder of r.holders){
        const row=h("div",null,"document-holder");row.append(h("strong",holder.name),h("span",holder.window ? windowName(holder.window)+"取得副本" : "开局持有"),evidenceLinks([holder.record],run));details.append(row);
      }
      details.append(disclosure("流转范围与内容指纹",h("p","可以收件的主体包括"+r.permitted.join("、")+"。"),h("code",r.selected.sha256)),
        h("h3","副本传递（"+r.total+"）"));
      if(!r.total)details.append(h("p","截至所选窗口还没有这份原件的副本发件。","hint"));
    }
    details.querySelector(".more-documents")?.remove();
    for(const m of r.items){
      const row=h("article",null,"reason-card");
      row.append(h("strong",m.sender+" → "+m.recipient),h("p",windowName(m.sent_window)+"第 "+m.sent_round+" 轮发出，"+
        (m.receipt ? windowName(m.arrived_window)+"已送达。" : "仍在途中，预计"+windowName(m.due_window)+"送达。")),
        button("发出副本时的决定",()=>openActorContext({id:m.from,name:m.sender},m.sent_window,m.sent_round,false,run),"link"),
        evidenceLinks([m.dispatch,m.receipt].filter(Boolean),run));details.append(row);
    }
    if(r.next<r.total)details.append(button("继续读取流转记录",()=>load(r.next),"link more-documents"));
  }
  select.onchange=safe(()=>{selected=select.value;return load();});reading("原件流转",body);safe(()=>load())();
}
