(() => {
  "use strict";
  const copy = value => JSON.parse(JSON.stringify(value));
  const list = value => Array.isArray(value) ? value : [];
  function create(bridge) {
    const $ = id => document.getElementById(id), key = "chuantuo.assistant-definition.v1";
    let modelConfig = null, mode = "guided", locked = false;
    const defaultsForDraft = () => ({batches:0,goal:"",constraints:"",granularity:"",text:"",sources:[],proposal:null,records:[],answers:{},revision:""});
    let data = defaultsForDraft();
    try {const saved = JSON.parse(localStorage.getItem(key) || "null");if(saved?.sources && typeof saved.goal === "string")data={...data,...saved};}catch{/* First use or unavailable storage. */}
    const el = (tag,cls,text) => {const node=document.createElement(tag);if(cls)node.className=cls;if(text!=null)node.textContent=text;return node;};
    const help = text => el("p","field-help",text);
    const button = (label,fn,cls="text-button") => {const node=el("button",cls,label);node.type="button";node.addEventListener("click",fn);return node;};
    function persist(){try{localStorage.setItem(key,JSON.stringify(data));}catch{$("assistant-model-note").textContent="当前草稿较大，未能保存在浏览器中。采纳后可下载场景文件。";}}
    function setMode(value){mode=value;const guided=value==="guided";$("assistant-definition").hidden=!guided;$("definition-workspace").classList.toggle("guided-mode",guided);$("definition-guided").setAttribute("aria-pressed",String(guided));$("definition-expert").setAttribute("aria-pressed",String(!guided));}
    function syncConfig(config){
      modelConfig=config;
      for(const [id,role] of [["assistant-parser-model","parser"],["assistant-world-model","world"]]){
        $(id).replaceChildren();for(const [name,model]of Object.entries(config.models.configs))$(id).append(new Option(`${name} · ${model.provider} / ${model.model}`,name));$(id).value=config.models[role];
      }
      const parser=config.models.configs[config.models.parser],world=config.models.configs[config.models.world];
      $("assistant-model-note").textContent=parser?.provider==="demo"?"当前为固定内容的离线演示，不会理解上传材料。选择真实模型后，AI 才会根据材料整理。":`整理将调用所选模型，可能产生费用。${parser?.supports_images?"同时读取原图与提取文字。":"使用提取文字；未启用图片理解。"}${world?.provider==="demo"?"影响估权仍是离线演示。":"缺失权重由所选世界模型估算。"}`;
      $("assistant-generate").textContent=parser?.provider==="demo"?"试用 AI 整理流程（离线）":"让 AI 整理场景";
    }
    function renderSources(){
      const root=$("assistant-sources");root.replaceChildren();
      data.sources.forEach((source,index)=>{
        const card=el("details","assistant-source");const summary=el("summary");
        const methods=new Set(list(source.pages).map(page=>page.method));
        summary.append(el("strong","",source.name),el("span","",`${source.pages?.length ? source.pages.length+" 页" : "连续正文"} · ${source.text.length.toLocaleString()} 字符${methods.has("ocr")?" · OCR":""}`));card.append(summary);
        if(source.asset_id){const link=el("a","source-original","下载原件");link.href=`/api/materials/${encodeURIComponent(source.asset_id)}/original`;card.append(link);}
        card.append(button("逐页核对文字",()=>bridge.showSource(source)));
        for(const warning of list(source.warnings))card.append(help(warning));
        const preview=el("pre","source-text-preview",source.text||"未识别到文字，可启用视觉模型读取原图。");card.append(preview,button("移除此材料",()=>{if(locked)return;data.sources.splice(index,1);persist();renderSources();}));root.append(card);
      });
    }
    async function upload(files){
      const selected=Array.from(files||[]);if(!selected.length||locked)return;
      await bridge.perform("assistant-generate",async()=>{
        const progress=$("assistant-progress");progress.hidden=false;
        try{
          for(let i=0;i<selected.length;i++){
            progress.textContent=`正在读取 ${i+1}/${selected.length}：${selected[i].name}。扫描页与图片会在本地识别文字。`;
            const form=new FormData();form.append("file",selected[i]);
            const result=await bridge.materialJob(form, message=>{progress.textContent=message;});applyMaterial(result);
          }
          bridge.notice(`已导入 ${selected.length} 份材料，原件保存在本机。尚未调用场景整理模型。`);
        }finally{progress.hidden=true;$("assistant-files").value="";}
      });
    }
    function applyMaterial(result, acknowledge=true) {
      if (!data.sources.some(source=>source.asset_id===result.source.asset_id)) data.sources.push(result.source);
      persist(); renderSources(); if(acknowledge)bridge.materialApplied();
    }
    function section(title,items){
      const details=el("details","assistant-preview-section");details.append(el("summary","",`${title} · ${items.length}`));const contents=el("ul");for(const text of items)contents.append(el("li","",text));details.append(contents);return details;
    }
    function renderProposal(){
      const draft=data.proposal;$("assistant-proposal").hidden=!draft;$("assistant-empty").hidden=Boolean(draft);if(!draft)return;
      $("assistant-summary").textContent=draft.summary;
      const scene=draft.scenario;$("assistant-stats").replaceChildren(...[`${scene.entities.length} 个主体`,`${scene.subsystems.length} 个子系统`,`${scene.domains.length} 个领域`,`${scene.influences.length} 条影响关系`].map(text=>el("span","",text)));
      const preview=$("assistant-preview");preview.replaceChildren();
      const names=new Map(scene.entities.map(a=>[a.id,a.name]));
      const sourceNames=new Map(scene.sources.map(source=>[source.id,source.name]));
      const evidence=item=>{const p=item.provenance;return p?`〔${p.kind==="ai_assumption"?"AI 假设":p.kind==="missing"?"缺失":p.kind==="user"?"用户设定":"材料"}：${list(p.source_ids).map(id=>sourceNames.get(id)||id).join("、")}${p.explanation?` ${p.explanation}`:""}〕`:"";};
      const systems=new Map(scene.subsystems.map(system=>[system.id,system]));
      preview.append(section("参与主体",scene.entities.map(a=>`${a.name}：${a.identity} ${evidence(a)}`)),section("内部层级",scene.entities.map(a=>{const parent=systems.get(a.subsystem_id);return parent?`${names.get(parent.owner_id)} / ${parent.name} → ${a.name}`:`${a.name}：顶层主体`;})),section("领域与状态",scene.entities.flatMap(a=>a.fields.map(field=>`${a.name} · ${field.domain} / ${field.id}：${field.value===null?"未知":String(field.value)}${field.unit?` ${field.unit}`:""}；${field.visibility.public?"公开":`仅本主体${field.visibility.readers.length?`及 ${field.visibility.readers.map(id=>names.get(id)||id).join("、")}`:""}`} ${evidence(field)}`))),section("世界规则",scene.rules.map(rule=>`${rule.text} ${evidence(rule)}`)),section("假设与缺失",[...scene.assumptions.map(text=>`假设：${text}`),...scene.missing_information.map(text=>`待补充：${text}`)]));
      if(scene.influences.length)preview.append(section("影响关系",scene.influences.map(e=>`${names.get(e.source)||e.source} → ${names.get(e.target)||e.target} · ${e.weight??"待赋权"} · ${e.provenance.kind==="ai_assumption"?"AI 估算":"材料 / 用户依据"}：${e.provenance.explanation}`)));
      const citations = [...scene.entities,...scene.rules,...scene.influences,...scene.entities.flatMap(a=>[...a.materials,...a.fields])].flatMap(item => list(item.provenance?.quotes).map(q=>({q,item})));
      if (citations.length) {
        const box=el("details","assistant-preview-section");box.append(el("summary","","可核对的原文摘引 · "+citations.length));
        for (const {q,item} of citations) {
          const source=scene.sources.find(s=>s.id===q.source_id);
          if(source) box.append(button((item.name||item.id||item.domain||"定义")+" · "+q.quote,()=>bridge.showSource(source,q)));
        }
        preview.append(box);
      }
      if(draft.changes?.length)preview.append(section("本次整理",draft.changes));
      if(draft.warnings?.length)preview.append(section("材料与调用提示",draft.warnings));
      const questions=$("assistant-questions");questions.replaceChildren();
      if(draft.questions.length)questions.append(el("h3","","这些信息会让推演更明确"),help("可先回答关键项，也可保留缺失标记后采纳草案。"));
      for(const question of draft.questions){
        const card=el("div","assistant-question"),label=el("label","",question.question),answer=el("textarea");answer.rows=2;answer.id=`assistant-answer-${question.id}`;label.htmlFor=answer.id;answer.value=data.answers[question.id]||"";answer.placeholder="补充你的判断，或暂时留空";
        answer.addEventListener("input",()=>{data.answers[question.id]=answer.value;persist();});card.append(label);if(question.reason)card.append(help(question.reason));
        if(question.options.length){const options=el("div","answer-options");for(const text of question.options)options.append(button(text,()=>{data.answers[question.id]=text;answer.value=text;persist();}));card.append(options);}
        card.append(answer);questions.append(card);
      }
      $("assistant-revision").value=data.revision;
    }
    async function generate(refine){
      await bridge.perform(refine?"assistant-refine":"assistant-generate",async()=>{
        if(!data.goal.trim())throw new Error("先用一句话说明想推演的问题。");
        const config=bridge.config(),model=bridge.model(config,"parser"),world=bridge.model(config,"world");
        const answers=refine?list(data.proposal?.questions).filter(q=>data.answers[q.id]?.trim()).map(q=>({question:q.question,answer:data.answers[q.id]})):[];
        if(refine&&!answers.length&&!data.revision.trim())throw new Error("请回答一个问题，或写一句希望调整的内容。");
        const sources=copy(data.sources);
        if(data.text.trim())sources.push({id:`pasted_${Date.now()}`,name:"用户粘贴的原始材料",text:data.text});
        $("assistant-progress").hidden=false;$("assistant-progress").textContent="AI 正在整理主体、层级、领域与规则，并检查缺失信息。完成后还会补齐缺失的影响权重。";
        try{
          const result=await bridge.definitionJob({goal:data.goal,constraints:data.constraints,granularity:data.granularity,sources,current_scenario:refine?data.proposal?.scenario:null,revision:refine?data.revision:"",answers,model,world_model:world,max_source_batches:Number(data.batches || 0)}, refine, stage => {$("assistant-progress").textContent=stage;});
          applyResult(result, refine);
        }finally{$("assistant-progress").hidden=true;}
      });
    }
    function applyResult(result, refine) {
      if (!refine) data.records = [];
      data.records.push(...result.records);
      data.proposal = {...result, records:undefined};
      data.answers = {}; data.revision = ""; $("assistant-revision").value = "";
      persist(); renderProposal(); bridge.jobApplied();
      bridge.notice("草案已准备好。可审阅、补充回答或采纳后设置推演。");
    }
    const formFields = [["assistant-batches","batches"],["assistant-goal","goal"],["assistant-text","text"],["assistant-granularity","granularity"],["assistant-constraints","constraints"],["assistant-revision","revision"]];
    function restore(value) {
      data = {...defaultsForDraft(), ...copy(value || {})};
      for (const [id, field] of formFields) $(id).value = data[field];
      persist(); renderSources(); renderProposal();
    }
    for(const [id,field] of formFields){
      $(id).value=data[field];$(id).addEventListener("input",()=>{data[field]=$(id).value;persist();});
    }
    $("assistant-files").addEventListener("change",()=>upload($("assistant-files").files));
    $("assistant-dropzone").addEventListener("dragover",event=>{event.preventDefault();$("assistant-dropzone").classList.add("drag-over");});
    $("assistant-dropzone").addEventListener("dragleave",()=>$("assistant-dropzone").classList.remove("drag-over"));
    $("assistant-dropzone").addEventListener("drop",event=>{event.preventDefault();$("assistant-dropzone").classList.remove("drag-over");upload(event.dataTransfer.files);});
    $("assistant-generate").addEventListener("click",()=>generate(false));$("assistant-refine").addEventListener("click",()=>generate(true));
    $("assistant-accept").addEventListener("click",()=>bridge.perform("assistant-accept",async()=>{
      if(!data.proposal)return;
      if(data.revision.trim()||Object.values(data.answers).some(answer=>answer.trim()))throw new Error("补充回答和修改意见尚未整理进草案。请先点击“根据补充更新草案”，再采纳。 ");
      bridge.accept(data.proposal.scenario,data.records);setMode("expert");bridge.review();bridge.notice("已采纳 AI 草案并保留材料与整理记录。核对窗口和模型后即可启动。上一个场景可恢复。");
    }));
    $("assistant-use-current").addEventListener("click",()=>bridge.perform("assistant-use-current",async()=>{
      const current=bridge.bundle();data.goal=data.goal||current.scenario.background||current.scenario.title;$("assistant-goal").value=data.goal;
      data.sources=copy(current.scenario.sources);data.records=copy(current.preparation_records);data.proposal={scenario:current.scenario,summary:"已载入当前场景。描述你希望改变的内容，AI 会据此修订，原场景在采纳前保持原样。",questions:[],changes:[],warnings:[]};persist();renderSources();renderProposal();
    }));
    for(const [id,role] of [["assistant-parser-model","parser"],["assistant-world-model","world"]])$(id).addEventListener("change",()=>{
      try{const config=bridge.config();config.models[role]=$(id).value;bridge.writeConfig(config);bridge.clearError();}catch(error){bridge.error(error);}
    });
    const defaults={deepseek:["https://api.deepseek.com","DEEPSEEK_API_KEY"],openai:["https://api.openai.com/v1","OPENAI_API_KEY"],anthropic:["https://api.anthropic.com/v1","ANTHROPIC_API_KEY"],custom:["","CUSTOM_API_KEY"]};
    $("assistant-provider").addEventListener("change",()=>{const [url,env]=defaults[$("assistant-provider").value];$("assistant-model-url").value=url;$("assistant-key-env").value=env;$("assistant-protocol").value="";$("assistant-vision").checked=false;});
    $("assistant-add-model").addEventListener("click",()=>bridge.perform("assistant-add-model",async()=>{
      const config=bridge.config(),provider=$("assistant-provider").value,name=$("assistant-model-name").value.trim(),env=$("assistant-key-env").value.trim();
      if(!name||!/^[A-Za-z0-9][A-Za-z0-9._:/-]*$/.test(name)||name.includes("://"))throw new Error("请填写实际模型 ID。");
      if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(env))throw new Error("密钥处只填写环境变量名，例如 DEEPSEEK_API_KEY。");
      const url=$("assistant-model-url").value.trim();let parsed;try{parsed=new URL(url);}catch{throw new Error("请填写有效 API 地址。");}
      if(parsed.username||parsed.password||parsed.search||parsed.hash)throw new Error("API 地址不能包含密钥、用户名或查询参数。");
      if(parsed.protocol!=="https:"&&!(parsed.protocol==="http:"&&["localhost","127.0.0.1","[::1]"].includes(parsed.hostname)))throw new Error("外部接口需使用 HTTPS。");
      let i=1;while(config.models.configs[`definition_model_${i}`])i++;
      const protocol=$("assistant-protocol").value||null;
      const allowed={openai:["chat_completions","responses"],anthropic:["messages"],deepseek:["chat_completions"],custom:["chat_completions","responses","messages"]};
      if(protocol&&!allowed[provider].includes(protocol))throw new Error("接口协议与所选提供商不匹配。");
      const id=`definition_model_${i}`;config.models.configs[id]={provider,protocol,model:name,base_url:url,api_key_env:env,timeout_seconds:180,max_tokens:16384,context_limit_chars:250000,max_retries:2,json_output:provider==="deepseek",supports_images:$("assistant-vision").checked};config.models.parser=id;bridge.writeConfig(config);bridge.notice(`已将 ${id} 用于场景整理。影响估权模型仍由上方单独选择。尚未调用接口。`);
    }));
    $("definition-guided").addEventListener("click",()=>setMode("guided"));$("definition-expert").addEventListener("click",()=>setMode("expert"));
    renderSources();renderProposal();setMode("guided");
    return {syncConfig, restore, snapshot:()=>copy(data), applyMaterial, applyResult, show:setMode,lock(value){locked=value;$("assistant-controls").disabled=value;},get mode(){return mode;}};
  }
  window.ChuantuoAssistantDefinition={create};
})();
