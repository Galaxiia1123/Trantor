"use strict";

function draftCapacities(scene) {
  const body=h("div"),names=Object.fromEntries(scene.entities.map(a=>[a.id,a.name]));
  const fieldName=ref=>names[ref.entity_id]+" · "+(scene.entities.find(a=>a.id===ref.entity_id)?.fields.find(f=>f.id===ref.field_id)?.label||ref.field_id);
  for(const [i,pool] of (scene.capacity_pools||[]).entries()){
    const card=h("div",null,"line");
    card.append(h("strong",pool.name),h("p","合计占用　"+pool.members.map(fieldName).join("、")),
      h("p","上限取自　"+(typeof pool.limit==="object"?fieldName(pool.limit):pool.limit)),
      button("修改这项约束",()=>editCapacity(scene,i),"link"));body.append(card);
  }
  if(scene.entities.some(a=>a.fields.some(f=>f.kind==="number")))body.append(button("增加共享上限",()=>editCapacity(scene,null),"link"));
  return disclosure("共享容量约束（"+(scene.capacity_pools||[]).length+"）",body);
}

function editCapacity(scene,index){
  const old=index===null?null:scene.capacity_pools[index],form=h("form",null,"advanced");
  const name=h("input"),cap=h("select"),fixed=h("input"),entries=[],members=h("fieldset"),publicRule=h("input");
  name.required=true;name.maxLength=100;name.value=old?.name||"";name.placeholder="例如，共用排期工时";
  for(const a of scene.entities)for(const f of a.fields.filter(f=>f.kind==="number"))entries.push({entity_id:a.id,field_id:f.id,name:a.name+" · "+(f.label||f.id),unit:f.unit||""});
  const label=(text,node)=>{const box=h("label",text);node.setAttribute("aria-label",text);box.append(node);form.append(box);return box;};
  label("共享上限名称",name);cap.append(new Option("填写固定容量","constant"));
  entries.forEach((f,i)=>cap.append(new Option(f.name,i)));label("容量依据",cap);
  fixed.type="number";fixed.min="0";fixed.step="any";fixed.required=true;fixed.value=typeof old?.limit==="number"?String(old.limit):"";
  const fixedLabel=label("固定容量",fixed);
  if(old && typeof old.limit==="object")cap.value=String(entries.findIndex(e=>e.entity_id===old.limit.entity_id && e.field_id===old.limit.field_id));
  const redraw=()=>{fixedLabel.hidden=fixed.disabled=cap.value!=="constant";};cap.onchange=redraw;redraw();
  members.append(h("legend","共同占用这份容量的字段"));
  entries.forEach((entry,i)=>{
    const box=h("input"),row=h("label",null,"check");box.type="checkbox";box.value=String(i);
    box.checked=Boolean(old?.members.some(ref=>ref.entity_id===entry.entity_id && ref.field_id===entry.field_id));
    row.append(box,document.createTextNode(entry.name+(entry.unit?"（"+entry.unit+"）":"")));members.append(row);
  });form.append(members);
  publicRule.type="checkbox";publicRule.checked=Boolean(old?.visibility?.public);
  const scope=h("label",null,"check");scope.append(publicRule,document.createTextNode("所有主体知道这项约束"));form.append(scope);
  form.append(h("p","字段仍按自己的读权提供给主体。只有能查看全部组成项时，主体才会收到计算出的余量。","hint"));
  const error=h("p",null,"form-error"),save=h("button","保存约束","primary");save.type="submit";form.append(error,save);
  form.onsubmit=async event=>{
    event.preventDefault();const chosen=[...members.querySelectorAll("input:checked")].map(n=>entries[Number(n.value)]);
    if(chosen.length<2){error.textContent="请选择至少两个实际占用字段。";return;}
    const ref=value=>({entity_id:value.entity_id,field_id:value.field_id});
    const limit=cap.value==="constant"?Number(fixed.value):ref(entries[Number(cap.value)]);
    if(typeof limit==="number" && (!Number.isFinite(limit) || (Number.isInteger(limit)&&!Number.isSafeInteger(limit)))){error.textContent="请输入能精确表示的非负容量。";return;}
    const pool={id:old?.id||"capacity_"+uid().slice(0,12),name:name.value.trim(),limit,members:chosen.map(ref),
      visibility:publicRule.checked?{public:true}:old?.visibility?.public?{public:false,readers:[]}:old?.visibility||{public:false,readers:[]},
      provenance:{kind:"user",explanation:"操作者明确设定的共同容量上限。"}};
    const previous=scene.capacity_pools||[];scene.capacity_pools=[...previous];
    if(index===null)scene.capacity_pools.push(pool);else scene.capacity_pools[index]=pool;
    save.disabled=true;error.textContent="";
    try{await saveProject("修改共享容量约束");$("reading").close();renderDraft();}
    catch(err){scene.capacity_pools=previous;error.textContent=err.message;}finally{save.disabled=false;}
  };reading(old?"修改共享上限":"增加共享上限",form);
}

function capacityText(usage,unit=""){
  const cap=usage.capacity===null?"容量未知":"容量 "+usage.capacity+unit;
  if(usage.remaining===null)return cap+"，已知占用 "+usage.known_used+unit+"，"+(usage.unknown?usage.unknown+" 项占用未知":"余量待核对");
  return "已占用 "+usage.known_used+" / "+usage.capacity+unit+"，剩余 "+usage.remaining+unit;
}

async function showCapacities(view){
  const root=$("capacity-summary");root.hidden=!view.capacity_count;if(root.hidden)return;
  const run=activeRun,w=view.settled_through,key=run+"/"+w;
  if(root.dataset.key===key)return;root.dataset.key=key;root.replaceChildren(h("p","正在核对共同容量…","hint"));
  const data=await api("/api/runs/"+run+"/capacities?window="+w);
  if(activeRun!==run || root.dataset.key!==key)return;
  const draw=(limit=4)=>{
    root.replaceChildren(h("h2","共享容量"));
    for(const pool of data.pools.slice(0,limit)){
      const card=h("div",null,"capacity-row");card.append(h("strong",pool.name),h("p",capacityText(pool.usage,pool.unit||"")));
      const used=Number(pool.usage.known_used),total=Number(pool.usage.capacity);
      if(pool.usage.remaining!==null && total>0 && Number.isFinite(total) && Number.isFinite(used)){
        const meter=h("progress");meter.max=total;meter.value=used;meter.setAttribute("aria-label",pool.name+"已占用");card.append(meter);
      }
      card.append(button("查看组成与依据",()=>capacityDetails(pool,data,view),"link"));root.append(card);
    }
    if(limit<data.pools.length)root.append(button("展开其余 "+(data.pools.length-limit)+" 项",()=>draw(data.pools.length),"link"));
  };draw();
}

function capacityDetails(pool,data,view){
  const body=h("div");body.append(h("p",capacityText(pool.usage,pool.unit||""),"lead"));
  const parts=[...(pool.capacity_field?[{...pool.capacity_field,isCapacity:true}]:[]),...pool.members];
  for(const part of parts){
    const row=h("div",null,"line");row.append(h("strong",(part.isCapacity?"容量　":"占用　")+part.name+" · "+part.label),
      h("p",part.value===null?"未知":String(part.value)+(pool.unit||"")),
      button("查阅这项字段",()=>openFieldReason(view,part.actor,part.field,data.window),"link"));body.append(row);
  }
  if(pool.provenance.quotes?.length)body.append(disclosure("约束原句",bullets(pool.provenance.quotes.map(q=>q.quote))));
  body.append(evidenceLinks([data.record],activeRun));reading(pool.name,body);
}
