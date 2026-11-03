(() => {
  "use strict";

  function neighborhood(data, current, windowIndex, mode, query = "") {
    const actors = new Map(data.actors.map(a => [a.id, a]));
    const incoming = new Map(), outgoing = new Map();
    function add(source, target, key, value) {
      if (source !== current && target !== current) return;
      const outbound = source === current;
      const peer = outbound ? target : source;
      const map = outbound ? outgoing : incoming;
      if (!map.has(peer)) map.set(peer, {actor:actors.get(peer), edge:null, messages:[]});
      map.get(peer)[key] = value;
    }
    for (const edge of data.influences) add(edge.source, edge.target, "edge", edge);
    for (const route of data.routes) {
      if (route.window === windowIndex) add(route.source, route.target, "messages", route.event_ids);
    }
    const needle = query.trim().toLowerCase();
    const rows = map => [...map.values()].filter(item => item.actor &&
      (mode !== "messages" || item.messages.length) && (mode !== "influences" || item.edge) &&
      (item.actor.name + " " + item.actor.id).toLowerCase().includes(needle)).sort((a,b) =>
        b.messages.length-a.messages.length || (b.edge?.weight ?? -1)-(a.edge?.weight ?? -1) || a.actor.id.localeCompare(b.actor.id));
    return {center:actors.get(current), incoming:rows(incoming), outgoing:rows(outgoing)};
  }

  const svg = (name, attrs = {}, label) => {
    const element = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
    if (label !== undefined) element.textContent = label;
    return element;
  };

  function draw(container, graph, mode, limit, focus) {
    container.replaceChildren();
    const left = graph.incoming.slice(0,limit), right = graph.outgoing.slice(0,limit);
    if (!graph.center || (!left.length && !right.length)) return;
    const height = Math.max(145, Math.max(left.length,right.length)*67+48);
    const mid = height/2+8;
    const root = svg("svg",{viewBox:"0 0 800 "+height,role:"group","aria-label":graph.center.name+" 的定向关系和已投递消息"});
    const defs=svg("defs");
    for(const [id,color] of [["impact","#287267"],["message","#a06730"]]){
      const marker=svg("marker",{id:"route-arrow-"+id,viewBox:"0 0 12 12",refX:10,refY:6,markerWidth:6,markerHeight:6,orient:"auto"});
      marker.append(svg("path",{d:"M 1 1 L 11 6 L 1 11 z",fill:color}));defs.append(marker);
    }
    root.append(defs);
    const lines=svg("g"),nodes=svg("g");
    root.append(lines,nodes);
    function node(actor,x,y,caption,center=false){
      const g=svg("g",{class:center?"focus-node core-node":"focus-node",transform:"translate("+x+" "+y+")"});
      if(!center) {
        g.setAttribute("role","button");g.setAttribute("tabindex","0");g.setAttribute("aria-label","切换到 "+actor.name);
        g.addEventListener("click",()=>focus(actor.id));
        g.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();focus(actor.id);}});
      }
      const title=svg("title",{},actor.name+" · L"+actor.depth+" · "+caption);
      const chars=Array.from(actor.name), name=chars.length>11?chars.slice(0,10).join("")+"…":actor.name;
      g.append(title,svg("rect",{x:-87,y:-24,width:174,height:48,rx:8}),
        svg("text",{y:-2,"text-anchor":"middle",class:"focus-name"},name),
        svg("text",{y:15,"text-anchor":"middle",class:"focus-label"},"L"+actor.depth+" · "+caption));
      nodes.append(g);
    }
    function link(start,end,style,weight) {
      const [x1,y1]=start,[x2,y2]=end,half=(x1+x2)/2;
      const color=style==="impact"?"#287267":"#a06730";
      const attrs={d:"M"+x1+" "+y1+" C"+half+" "+y1+" "+half+" "+y2+" "+x2+" "+y2,fill:"none",stroke:color,
        "stroke-width":style==="impact"?1+2*weight:2,"marker-end":"url(#route-arrow-"+style+")"};
      if(style==="message")attrs["stroke-dasharray"]="5 4";
      lines.append(svg("path",attrs));
    }
    for(const [rows,inbound] of [[left,true],[right,false]]){
      rows.forEach((item,i)=>{
        const y=mid+(i-(rows.length-1)/2)*67;
        const x=inbound?101:699;
        const peerPoint=[inbound?190:608,y],centerPoint=[inbound?310:490,mid];
        const [from,to]=inbound?[peerPoint,centerPoint]:[centerPoint,peerPoint];
        if(item.edge && mode!=="messages")link([from[0],from[1]-3],[to[0],to[1]-3],"impact",item.edge.weight);
        if(item.messages.length && mode!=="influences")link([from[0],from[1]+4],[to[0],to[1]+4],"message",0);
        const info=[item.edge?"权重 "+item.edge.weight:"无预设边",item.messages.length+" 条消息"].join(" · ");
        node(item.actor,x,y,item.actor.id===graph.center.id?"自反馈 · "+info:info);
      });
    }
    node(graph.center,400,mid,"当前视角",true);
    root.append(svg("text",{x:101,y:18,"text-anchor":"middle",class:"route-caption"},"流入当前主体"),
      svg("text",{x:699,y:18,"text-anchor":"middle",class:"route-caption"},"由当前主体流出"));
    container.append(root);
  }
  const api={neighborhood,draw};
  if(typeof module!=="undefined"&&module.exports)module.exports=api;
  else window.ChuantuoRoutes=api;
})();
