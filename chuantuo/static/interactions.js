"use strict";

function openInteractions(view) {
  const run=activeRun, end=view.settled_through;
  const body=h("div",null,"communication-map"), controls=h("div",null,"map-controls");
  const range=h("select"), focus=h("select"), mode=h("select"), peer=h("input"), summary=h("p",null,"hint"), graph=h("div",null,"route-canvas"), list=h("div");
  const field=(title,input)=>{const label=h("label",title);input.setAttribute("aria-label",title);label.append(input);return label;};
  range.append(new Option("只看当前窗口","one"),new Option("最近五窗","five"),new Option("开局至此窗","all"));
  mode.append(new Option("关系与投递","both"),new Option("仅已送达消息","messages"),new Option("仅设定的影响关系","influences"));
  peer.type="search";peer.placeholder="对方的名字或编号";
  controls.append(field("通信范围",range),field("中心主体",focus),field("图中显示",mode),field("查找往来对象",peer));
  const legend=h("p",null,"map-legend");legend.append(h("span","设定的有向关系"),h("span","实际投递"));
  const overview=h("section",null,"map-overview");overview.setAttribute("aria-label","通信概览");
  overview.append(controls,summary,legend,graph);body.append(overview,list);
  let data, chosen=$("feed-actor").value || "", limit=5, request=0;
  const cache=new Map();
  function draw() {
    if(!data)return;
    const network=window.ChuantuoRoutes.neighborhood(data,data.focus,null,mode.value,peer.value);
    window.ChuantuoRoutes.draw(graph,network,mode.value,limit,id=>{chosen=id;focus.value=id;safe(load)();});
    const a=network.center;
    summary.textContent=windowName(data.start)+"至"+windowName(data.through)+"，全场 "+data.total+" 次投递，其中跨层 "+data.cross_layer+" 次。"+
      a.name+"发出 "+a.sent+" 次、收到 "+a.received+" 次。";
    list.replaceChildren();
    for(const [direction,entries] of [["收到",network.incoming],["发出",network.outgoing]]) {
      const section=h("section");section.append(h("h3",direction+"方向"));
      for(const item of entries.slice(0,limit)) {
        const row=h("div",null,"map-peer"),details=h("div");
        const label=direction==="收到" ? item.actor.name+" → "+a.name : a.name+" → "+item.actor.name;
        details.append(h("strong",label),h("small",item.messageCount+" 次已送达","quiet"));
        if(item.edge)details.append(h("p",item.edge.description || "历史设定的影响权重为 "+item.edge.weight+"。","hint"));
        row.append(details);
        if(item.messageCount)row.append(button("读双方往来",()=>openConversation(run,a,item.actor,data.start,data.through),"link"));
        section.append(row);
      }
      if(!entries.length)section.append(h("p","当前筛选下没有对应的关系或投递。","hint"));
      list.append(section);
    }
    if(network.incoming.length>limit || network.outgoing.length>limit)
      list.append(button("查看更多往来对象",()=>{limit+=8;draw();},"link"));
  }
  async function load() {
    const id=++request, start=range.value==="one" ? end : range.value==="five" ? Math.max(1,end-4) : 0;
    const from=Math.min(start,end), key=[from,end,chosen].join("/");
    let item=cache.get(key);
    if(!item) {
      const query=new URLSearchParams({start:String(from),through:String(end)});
      if(chosen)query.set("focus",chosen);
      item=await api("/api/runs/"+run+"/interactions?"+query);
      cache.set(key,item);
    }
    if(id!==request || !body.isConnected)return;
    data=item;chosen=data.focus;limit=5;
    focus.replaceChildren(...data.actors.map(a=>new Option(a.name+"（收 "+a.received+" / 发 "+a.sent+"）",a.id)));
    focus.value=data.focus;draw();
  }
  range.onchange=safe(load);focus.onchange=safe(()=>{chosen=focus.value;return load();});
  mode.onchange=draw;peer.oninput=draw;
  reading("通信图",h("p","全局观察视角。实线说明谁可以影响谁，虚线连接这段时间实际发生的投递。点击对端主体可切换中心。","hint"),body);
  safe(load)();
}

function openConversation(run,one,two,start,end) {
  const body=h("div"),form=h("form",null,"conversation-search"),q=h("input"),summary=h("p",null,"hint"),items=h("div");
  q.type="search";q.maxLength=200;q.placeholder="在双方原话中查找";q.setAttribute("aria-label","查找双方消息");
  const search=h("button","查找");search.type="submit";form.append(q,search);body.append(form,summary,items);
  const names={[one.id]:one.name,[two.id]:two.name};
  let current="", ticket=0;
  async function page(offset=0) {
    const id=++ticket, params=new URLSearchParams({one:one.id,two:two.id,start:String(start),through:String(end),offset:String(offset),q:current});
    const result=await api("/api/runs/"+run+"/conversation?"+params);
    if(!body.isConnected || id!==ticket)return;
    if(!offset)items.replaceChildren();items.querySelector(".more-messages")?.remove();
    summary.textContent=windowName(start)+"至"+windowName(end)+" · "+result.total+" 条匹配的定向投递";
    for(const message of result.items) {
      const card=h("article",null,"conversation-entry");
      card.append(h("small",windowName(message.window)+" · 第 "+message.round+" 轮发出"+(message.dispatch ? "　→　"+windowName(message.arrived_window)+"窗首送达" : ""),"quiet"),
        h("h3",names[message.from]+" → "+names[message.to]),h("p",message.text));
      const actions=h("div",null,"actions");
      actions.append(button("发送时的决策现场",()=>openActorContext({id:message.from,name:names[message.from]},message.window,message.round,false,run),"link"),
        button("投递原文",()=>evidence(message.receipt,run),"link"));
      card.append(actions);
      if(message.enclosures?.length)card.append(button("查阅随信副本 · "+message.enclosures.map(d=>d.title).join("、"),()=>evidence(message.receipt,run),"link"));
      if(message.reply_to || message.has_replies)card.append(button("沿这封信读回复",()=>openReplyChain(run,message.receipt,end),"link"));
      if(message.seen) {
        card.append(button("收件人于"+windowName(message.seen.window)+"第 "+message.seen.round+" 轮看到",()=>openActorContext({id:message.to,name:names[message.to]},message.seen.window,message.seen.round,false,run),"link"));
      } else {
        const statuses={beyond_range:"下一次观察在所选时段之后。",no_next_observation:"消息已送达，运行在收件人的下一轮观察之前结束。",
          awaiting_observation:"消息已送达，正在等候收件人的下一轮已提交观察。",unverified:"尚未在原始观察中核对到这条消息。"};
        card.append(h("small",statuses[message.stage],"quiet"));
      }
      items.append(card);
    }
    if(!result.total)items.append(h("p","这个时段没有符合条件的消息。","hint"));
    if(result.next<result.total)items.append(button("继续读取对话",()=>page(result.next),"link more-messages"));
  }
  form.onsubmit=e=>{e.preventDefault();current=q.value.trim();safe(()=>page())();};
  reading(one.name+"与"+two.name+"的往来",body);safe(()=>page())();
}

function openRequestedReplies(run,through) {
  const body=h("div"), filters=h("div",null,"map-controls"), who=h("select"), status=h("select");
  const head=h("p",null,"hint"), list=h("div"), moreBox=h("div");let sequence=0;
  const choose=(title,control)=>{const label=h("label",title);control.setAttribute("aria-label",title);label.append(control);return label;};
  who.append(new Option("全部主体",""));
  for(const [key,label] of [["all","全部问询"],["outstanding","尚未收回"],["overdue","已过截止仍未收回"],["answered","已有回函送达"]])status.append(new Option(label,key));
  filters.append(choose("往来主体",who),choose("回函进度",status));body.append(filters,head,list,moreBox);
  const stages={outbound:"原信在途",waiting:"已送达，尚未见明确回信",returning:"回信在途",answered:"回信已送达"};
  const times={open:"未设截止",on_time:"按期到件",late:"截止后到件",due:"本窗截止",overdue:"已过截止",upcoming:"尚未到期"};
  async function draw(offset=0) {
    const ticket=++sequence, params=new URLSearchParams({through,stage:status.value,offset});
    if(who.value)params.set("actor",who.value);
    const result=await api("/api/runs/"+run+"/requested-replies?"+params);
    if(ticket!==sequence || !body.isConnected)return;
    if(who.options.length===1)result.actors.forEach(a=>who.append(new Option(a.name,a.id)));
    if(!offset)list.replaceChildren();moreBox.replaceChildren();
    head.textContent="截至"+windowName(through)+"，"+result.counts.all+" 封信明确请求回函，"+result.counts.answered+" 封已收到回复，"+result.counts.overdue+" 封超过希望送达的窗口。";
    for(const item of result.items){
      const card=h("article",null,"conversation-entry requested-letter"),timing=item.by==="open"?times.open:"希望第 "+item.by+" 窗末前收到 · "+times[item.timing];
      card.append(h("small",stages[item.status]+" · "+timing,"eyebrow"),h("h3",item.sender_name+" → "+item.recipient_name),
        h("p",item.text+(item.excerpt?"…":"")),h("small",windowName(item.sent)+"发出"+(item.arrival?"，"+windowName(item.arrival)+"原信送达":"")+
          (item.reply_sent?"，"+windowName(item.reply_sent)+"发出回信":"")+(item.reply_arrival?"，"+windowName(item.reply_arrival)+"收回":"")+"。","quiet"));
      card.append(button("查原信",()=>evidence(item.id,run),"link"));
      if(item.receipt)card.append(button("沿这封信读回复",()=>openReplyChain(run,item.receipt,through),"link"));
      if(item.reply_id)card.append(button("查回函",()=>evidence(item.reply_id,run),"link"));list.append(card);
    }
    if(!result.total)list.append(h("p","这个范围内没有符合条件的问询。","hint"));
    if(result.next<result.total)moreBox.append(button("更多问询",()=>draw(result.next),"link"));
  }
  who.onchange=status.onchange=safe(()=>draw());
  body.append(h("p","按发件人登记的请求及收件人的原信引用追踪。收到回复只表明答复到件，具体立场可回看原文。","hint"));
  reading("哪些信在等回函",body);safe(()=>draw())();
}

function openReplyChain(run,receipt,through){
  const body=h("div"),status=h("p","","hint"),list=h("div");body.append(status,list);
  async function load(offset=0){
    const q=new URLSearchParams({through,offset}),r=await api("/api/runs/"+run+"/reply-chain/"+encodeURIComponent(receipt)+"?"+q);
    if(!body.isConnected)return;
    status.textContent="截至"+windowName(through)+"，这组往来有 "+r.total+" 封。只按发信时明确引用的原信连接。";
    list.querySelector(".more-replies")?.remove();
    for(const m of r.items){
      const card=h("article",null,"conversation-entry reply-item");card.dataset.selected=String(m.id===receipt);
      card.append(h("small",m.parent ? "回复 "+m.parent : "最初的信","eyebrow"),h("h3",m.sender+" → "+m.recipient),
        h("p",m.text+(m.truncated ? "…" : "")),h("small",windowName(m.sent_window)+"第 "+m.round+" 轮发出，"+
          (m.received_window ? windowName(m.received_window)+"已送达。" : "仍在途中，预计"+windowName(m.due_window)+"到达。"),"quiet"));
      if(m.enclosures.length)card.append(h("small","随信附件　"+m.enclosures.join("、"),"quiet"));
      card.append(evidenceLinks([m.id],run));list.append(card);
    }
    if(r.next<r.total)list.append(button("继续读取回复",()=>load(r.next),"link more-replies"));
  }
  reading("这封信的回复",body);safe(load)();
}
