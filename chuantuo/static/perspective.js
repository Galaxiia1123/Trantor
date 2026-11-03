"use strict";

let perspectiveOrigin=null, perspectiveRequest=0;
function perspectiveUrl(run,actor,point,news) {
  return "#actor/"+encodeURIComponent(run)+"/"+encodeURIComponent(actor)+"/"+point.window+"/"+point.round+(news ? "/news" : "");
}
function enterPerspective(actor,window,turn=1) {
  perspectiveOrigin={run:activeRun,url:location.hash,window,scroll:scrollY,actor};
  location.hash=perspectiveUrl(activeRun,actor,{window,round:turn},false);
}
function paperReader(run,actor,doc,point){
  return async()=>{
    const seq=++readingRequest;
    const data=await api("/api/runs/"+run+"/actors/"+encodeURIComponent(actor)+"/papers/"+encodeURIComponent(doc.id)+
                         "?window="+point.window+"&turn="+point.round);
    if(seq!==readingRequest || activeRun!==run)return;
    reading(data.title,h("p",data.acquired ? "第 "+data.acquired+" 窗首次签收的副本" : "开局已持有的原件","eyebrow"),
      h("p",data.text),h("small","当事主体的记录","quiet"),evidenceLinks([data.record],run));
  };
}
function readLetters(run,actor,point,direction,start=0) {
  const body=h("div"),title=direction==="inbox" ? "本轮收到的信" : "本轮发出的信";let serial=0;
  async function show(index) {
    const seq=++serial;
    const q=new URLSearchParams({window:point.window,turn:point.round,direction,index});
    const data=await api("/api/runs/"+run+"/actors/"+encodeURIComponent(actor)+"/letters?"+q);
    if(seq!==serial || !body.isConnected)return;
    const m=data.letter;
    body.replaceChildren(h("small","第 "+(data.index+1)+" / "+data.total+" 封","eyebrow"),
      h("h3",m.sender ? m.sender+"来信" : "致 "+m.to.join("、")),h("p",m.text));
    if(direction==="inbox"){
      if(!m.enclosures.length)body.append(h("p","这封信仅有文字。","hint"));
      for(const doc of m.enclosures)body.append(button("读取随信副本 · "+doc.title,paperReader(run,actor,doc,point),"link"));
    }else if(m.enclosures.length)body.append(h("p","随信附寄 "+m.enclosures.map(id=>point.holdings.find(d=>d.id===id)?.title || id).join("、"),"hint"));
    const nav=h("div",null,"actions");
    for(const [name,i] of [["上一封",index-1],["下一封",index+1]]){
      const b=button(name,()=>show(i));b.disabled=i<0 || i>=data.total;nav.append(b);
    }
    body.append(nav);
  }
  reading(title,body);return show(start);
}
async function perspectivePage(gen) {
  const [,run,encoded,win,round,filter]=location.hash.slice(1).split("/");
  const actor=decodeURIComponent(encoded || ""),news=filter==="news",root=$("perspective-page"),seq=++perspectiveRequest;
  if(!run || !actor)throw new Error("缺少路线或主体编号。");
  const params=new URLSearchParams({window:win || 1,turn:round || 1,news_only:news});
  const data=await api("/api/runs/"+encodeURIComponent(run)+"/actors/"+encodeURIComponent(actor)+"/journey?"+params);
  if(seq!==perspectiveRequest || gen!==routeGeneration)return;
  const m=data.current, stamp=p=>"第 "+p.window+" 窗 · 第 "+p.round+" 轮";
  function move(point,mode=news) {
    if(!point)return;
    history.replaceState(null,"",perspectiveUrl(run,actor,point,mode));
    return perspectivePage(gen);
  }
  const back=h("a","← 返回全局观察","link");
  back.href=perspectiveOrigin?.run===run ? perspectiveOrigin.url : "#run/"+run+"/"+m.window;
  const title=h("h1",data.actor.name+"的视角");
  const header=h("div",null,"perspective-header");header.append(back,h("p","主体手记","eyebrow"),title,
    h("p","一轮一轮，看它先得知了什么，又如何行动。","lead"));
  const nav=h("section",null,"perspective-nav"),controls=h("div",null,"actions");
  for(const [caption,point] of [[news ? "上一次新见闻" : "上一轮",data.previous],[news ? "下一次新见闻" : "下一轮",data.next]]){
    const b=button(caption,()=>move(point),"secondary");b.disabled=!point;controls.append(b);
  }
  const newsFlag=h("input");newsFlag.type="checkbox";newsFlag.checked=news;
  const newsLabel=h("label",null,"check");newsLabel.append(newsFlag,document.createTextNode("只在有新见闻时停留"));
  newsFlag.onchange=safe(()=>move(m,newsFlag.checked));
  const seek=h("form",null,"perspective-seek");
  const numeric=(label,value,max)=>{
    const group=h("label",label),input=h("input");input.type="number";input.min="1";input.max=String(max);input.required=true;input.value=value;
    group.append(input);seek.append(group);return input;
  };
  const w=numeric("跳到窗口",m.window,data.through),r=numeric("轮次",m.round,data.rounds_per_window);
  const jump=h("button","前往");jump.type="submit";seek.append(jump);
  seek.onsubmit=event=>{event.preventDefault();safe(()=>move({window:Number(w.value),round:Number(r.value)}))();};
  controls.append(newsLabel,disclosure("跳到指定时点",seek));
  nav.append(h("p",stamp(m)+"　·　"+data.ordinal+" / "+data.total+" 轮","eyebrow"),controls);
  const saw=h("article",null,"perspective-column"),decided=h("article",null,"perspective-column decision");
  saw.append(h("h2","作决定前，它看到了什么"));
  if(!m.inbox.length)saw.append(h("p","这一轮的收件箱里没有新信。","hint"));
  for(const [i,mail] of m.inbox.entries()){
    const row=h("section",null,"perspective-letter");
    row.append(h("strong",mail.sender+"来信"),h("small",mail.enclosures.length ? "随信附 "+mail.enclosures.length+" 份文书" : "仅有文字","quiet"),h("p",mail.text));
    if(mail.arrived)row.append(h("small","第 "+mail.dispatched+" 窗寄出，第 "+mail.arrived+" 窗抵达。","quiet"));
    if(mail.truncated)row.append(button("读完整来信",()=>readLetters(run,actor,m,"inbox",i),"link"));
    for(const doc of mail.enclosures)row.append(button("随信副本 · "+doc.title,paperReader(run,actor,doc,m),"link"));
    saw.append(row);
  }
  if(m.inbox_total>m.inbox.length)saw.append(button("浏览全部 "+m.inbox_total+" 封来信",()=>readLetters(run,actor,m,"inbox"),"link"));
  for(const change of m.changes)saw.append(h("p",change.owner+" · "+change.label+"　"+watchValue({value:change.before},change.unit)+" → "+watchValue({value:change.after},change.unit),"perspective-change"));
  for(const n of m.notices)saw.append(h("p","收到通知 · "+n,"perspective-change"));
  if(m.own_state)saw.append(disclosure("当时自己的处境",h("p",m.own_state)));
  if(m.holdings.length){
    const papers=h("div");
    for(const doc of m.holdings){
      papers.append(button("查阅《"+doc.title+"》",paperReader(run,actor,doc,m),"link"),
        h("small",doc.received_window ? "第 "+doc.received_window+" 窗首次收到" : "开局即持有","quiet"));
    }
    saw.append(disclosure("此刻已持有的文书（"+m.holdings.length+"）",papers));
  }
  decided.append(h("h2",m.deferred ? "它按约定等待" : "它随后作了什么决定"),
    h("p",m.deferred ? "本轮沿用此前的等待安排，最迟第 "+m.review_at+" 窗重新判断。" : m.text,"perspective-choice"));
  if(m.outbox.length){
    decided.append(h("h3","发出的消息"));
    for(const [i,sent] of m.outbox.entries()){
      const row=h("section",null,"perspective-letter");row.append(h("strong","致 "+sent.to.join("、")),h("p",sent.text));
      if(sent.enclosures.length)row.append(h("small","随信附寄 "+sent.enclosures.map(id=>m.holdings.find(d=>d.id===id)?.title || id).join("、"),"quiet"));
      if(sent.truncated)row.append(button("读完整发信",()=>readLetters(run,actor,m,"outbox",i),"link"));
      decided.append(row);
    }
    if(m.outbox_total>m.outbox.length)decided.append(button("浏览全部 "+m.outbox_total+" 封发信",()=>readLetters(run,actor,m,"outbox"),"link"));
  }else decided.append(h("p","本轮没有对外发信。","hint"));
  decided.append(button("打开本轮决策现场",()=>openActorContext(data.actor,m.window,m.round,false,run),"secondary"));
  if(data.supports_experiment && m.window>data.fork_window) {
    decided.append(button("从这一窗试一个条件",async()=>{
      const ticket=++readingRequest;
      const view=await api("/api/runs/"+run+"/watch?window="+m.window+"&limit=1");
      if(ticket===readingRequest && root.isConnected && !root.hidden && activeRun===run)experimentForm(view,null,m.window-1);
    },"link"),h("small","进入全局实验设置，从本窗开始前分岔。","quiet"));
  }else if(data.parent_id){
    const original=h("a","在母路线查看这一轮","link");original.href=perspectiveUrl(data.parent_id,actor,m,false);decided.append(original);
  }
  const columns=h("div",null,"perspective-columns");columns.append(saw,decided);
  root.replaceChildren(header,nav,columns);
}
