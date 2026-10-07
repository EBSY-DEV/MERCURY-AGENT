"""Build design/mercury-brand.pen: the Mercury by EBSY tokens + UI kit.

Source of truth is mercury/web/app.css. Re-run after changing tokens:

    python design/build_brand_pen.py | pen interactive --out design/mercury-brand.pen | tee /tmp/pen.log
    python design/build_brand_pen.py rename < /tmp/pen.log

Contents: two brand + UI token sheets (light/dark) and two "Today" dashboard
screens (light/dark) mirroring the implemented page, with sample data.
"""
import json
from pathlib import Path

L = {  # light
    "bg": "#F7F5FD", "panel": "#FFFFFF", "panel-raised": "#F2EEFC",
    "border": "#E8E3F5", "border-strong": "#D5CCEC",
    "text": "#211C35", "text-2": "#575073", "text-3": "#716A90",
    "accent": "#6D53D3", "accent-deep": "#5639BD", "accent-contrast": "#FFFFFF",
    "accent-soft": "#ECE6FD", "accent-line": "#D9CFFA",
}
D = {  # dark
    "bg": "#14111E", "panel": "#1B1728", "panel-raised": "#242036",
    "border": "#2C2740", "border-strong": "#3D3657",
    "text": "#EDE9F8", "text-2": "#ADA5C8", "text-3": "#8A82A8",
    "accent": "#B7A4F7", "accent-deep": "#C9BAFA", "accent-contrast": "#1A1530",
    "accent-soft": "#2B2545", "accent-line": "#4A3F78",
}
STATUS = [  # name, fg, bg
    ("good", "#2F6B47", "#E7F5EC"), ("wait", "#8A5B12", "#FCF1DC"),
    ("bad", "#A3343A", "#FDEBEC"), ("active", "#2D5F8A", "#E6F0FB"),
    ("note", "#9B3570", "#FCEAF4"),
]

variables = {k: {"type": "color", "value": [
    {"value": L[k], "theme": {"mode": "light"}},
    {"value": D[k], "theme": {"mode": "dark"}}]} for k in L}
for n, fg, bg in STATUS:
    variables[f"s-{n}"] = {"type": "color", "value": fg}
    variables[f"s-{n}-bg"] = {"type": "color", "value": bg}
variables["font-sans"] = {"type": "string", "value": "Geist"}
variables["font-mono"] = {"type": "string", "value": "Geist Mono"}
variables["r-sm"] = {"type": "number", "value": 8}
variables["r-md"] = {"type": "number", "value": 12}
variables["shadow-sm"] = {"type": "color", "value": [  # --shadow-sm
    {"value": "#402C8C14", "theme": {"mode": "light"}},
    {"value": "#0000004D", "theme": {"mode": "dark"}}]}

JS = r"""
SetVariables(%(vars)s);
const T=(o)=>({type:"text",fontFamily:"$font-sans",fill:"$text",...o});
const M=(o)=>({type:"text",fontFamily:"$font-mono",fill:"$text-3",fontSize:11,...o});
const sheet=(name,theme,x)=>Insert(document,{type:"frame",name,theme:{mode:theme},x,y:0,width:1280,layout:"vertical",gap:44,padding:56,fill:"$bg",cornerRadius:16,clip:true});
const build=(root)=>{
  const head=Insert(root,{type:"frame",name:"Masthead",width:"fill_container",alignItems:"center",justifyContent:"space_between"});
  const brand=Insert(head,{type:"frame",name:"Brand",gap:14,alignItems:"center"});
  const mark=Insert(brand,{type:"frame",name:"Mark",width:52,height:52,cornerRadius:15,justifyContent:"center",alignItems:"center",fill:{type:"gradient",gradientType:"linear",rotation:220,colors:[{color:"#C7B6FF",position:0},{color:"#9C84F0",position:0.55},{color:"#7A5EDB",position:1}]},effect:{type:"shadow",offset:{x:0,y:4},blur:14,color:"#6D53D340"}});
  Insert(mark,T({name:"M",content:"M",fontSize:26,fontWeight:"600",fill:"#FFFFFF"}));
  const wm=Insert(brand,{type:"frame",name:"Wordmark",layout:"vertical",gap:2});
  Insert(wm,T({name:"Name",content:"Mercury",fontSize:30,fontWeight:"600",letterSpacing:-0.8}));
  const by=Insert(wm,{type:"frame",name:"By",gap:4});
  Insert(by,T({name:"by",content:"by",fontSize:14,fontWeight:"500",fill:"$text-3"}));
  Insert(by,T({name:"EBSY",content:"EBSY",fontSize:14,fontWeight:"600",fill:"$accent-deep"}));
  Insert(head,M({name:"Sheet label",content:"Brand + UI tokens",fontSize:12}));

  const sec=(title,sub)=>{const s=Insert(root,{type:"frame",name:title,layout:"vertical",gap:16,width:"fill_container"});
    const h=Insert(s,{type:"frame",name:"Heading",layout:"vertical",gap:4});
    Insert(h,T({name:"Title",content:title,fontSize:20,fontWeight:"600",letterSpacing:-0.4}));
    if(sub)Insert(h,T({name:"Sub",content:sub,fontSize:13,fill:"$text-2"}));return s;};

  const pal=sec("Palette","Lavender-tinted neutrals, one violet accent. Tokens swap per theme.");
  const row=(parent,items)=>{const r=Insert(parent,{type:"frame",name:"Row",gap:10,width:"fill_container"});
    for(const [k,label] of items){const c=Insert(r,{type:"frame",name:k,layout:"vertical",gap:8,width:"fill_container"});
      Insert(c,{type:"rectangle",name:"Swatch",width:"fill_container",height:72,cornerRadius:"$r-md",fill:"$"+k,stroke:"$border",strokeWidth:1});
      Insert(c,T({name:"Token",content:label,fontSize:12,fontWeight:"500"}));
      Insert(c,M({name:"Var",content:"--"+k}));}};
  row(pal,[["accent","Accent"],["accent-deep","Accent deep"],["accent-soft","Accent soft"],["accent-line","Accent line"],["text","Text"],["text-2","Text 2"],["text-3","Text 3"]]);
  row(pal,[["bg","Background"],["panel","Panel"],["panel-raised","Panel raised"],["border","Border"],["border-strong","Border strong"]]);

  const st=sec("Status","A Phosphor icon and a word. Hue lives only in the glyph; no tinted pills.");
  const sr=Insert(st,{type:"frame",name:"Badges",gap:10});
  Update(sr,{gap:28});
  for(const [n,l,ic,c] of [["good","Sent","check-circle","#2F9460"],["wait","Waiting on you","clock","#C98A1E"],["bad","Bounced","warning-circle","$s-bad"],["active","Scheduled","arrow-circle-right","$s-active"],["note","Catch-all","info","$s-note"],["idle","Draft","circle-dashed","$text-3"]])
    {const b=Insert(sr,{type:"frame",name:"Badge "+n,gap:6,alignItems:"center"});Insert(b,{type:"icon",name:"Icon",library:"phosphor",icon:ic,width:15,height:15,fill:c});Insert(b,T({name:"Label",content:l,fontSize:12,fontWeight:"500",fill:n==="bad"?"$s-bad":"$text-2"}));}

  const ty=sec("Type","Geist for everything, Geist Mono for every figure. No serif.");
  for(const [n,s,w,ls,f] of [["Page title",28,"600",-0.85,"$font-sans"],["Section",18,"600",-0.4,"$font-sans"],["Body",14,"400",0,"$font-sans"],["Figure 1,284",26,"500",-0.8,"$font-mono"]]){
    const r=Insert(ty,{type:"frame",name:n,gap:24,alignItems:"center",width:"fill_container"});
    Insert(r,M({name:"Spec",content:s+" / "+w,width:110,textGrowth:"fixed-width"}));
    Insert(r,{type:"text",name:"Sample",content:n,fontFamily:f,fontSize:s,fontWeight:w,letterSpacing:ls,fill:"$text"});}

  const co=sec("Components","Cards 12px, controls 8px, nav pills fully round. Inbox rows lead with their state.");
  const nav=Insert(co,{type:"frame",name:"Nav pills",gap:4,alignItems:"center"});
  for(const [l,a] of [["Today",1],["Signals",0],["Discover",0],["Outbox",0]]){
    const p=Insert(nav,{type:"frame",name:"Pill "+l,padding:[7,14],cornerRadius:999,...(a?{fill:"$accent-soft",stroke:"$accent-line",strokeWidth:1}:{})});
    Insert(p,T({name:"Label",content:l,fontSize:13,fontWeight:a?"600":"500",fill:a?"$accent-deep":"$text-2"}));}
  const btns=Insert(co,{type:"frame",name:"Buttons",gap:10,alignItems:"center"});
  const bp=Insert(btns,{type:"frame",name:"Primary",padding:[9,16],cornerRadius:"$r-sm",fill:"$accent"});
  Insert(bp,T({name:"Label",content:"Approve",fontSize:13,fontWeight:"500",fill:"$accent-contrast"}));
  const bs=Insert(btns,{type:"frame",name:"Secondary",padding:[9,16],cornerRadius:"$r-sm",fill:"$panel",stroke:"$border-strong",strokeWidth:1});
  Insert(bs,T({name:"Label",content:"Skip",fontSize:13,fontWeight:"500"}));
  const q=Insert(co,{type:"frame",name:"Queue item",width:640,cornerRadius:"$r-md",fill:"$panel",stroke:"$border",strokeWidth:1,effect:{type:"shadow",offset:{x:0,y:1},blur:3,color:"#402C8C14"}});
  const qb=Insert(q,{type:"frame",name:"Body",padding:[16,20],gap:24,width:"fill_container",alignItems:"center"});
  const qt=Insert(qb,{type:"frame",name:"Text",layout:"vertical",gap:2,width:"fill_container"});
  const qs=Insert(qt,{type:"frame",name:"State",gap:6,alignItems:"center",padding:[0,0,4,0]});
  Insert(qs,{type:"icon",name:"Icon",library:"phosphor",icon:"clock",width:14,height:14,fill:"#C98A1E"});
  Insert(qs,T({name:"Label",content:"Needs you",fontSize:11.5,fontWeight:"500",fill:"$text-3"}));
  Insert(qt,T({name:"Title",content:"3 emails waiting for approval",fontSize:15,fontWeight:"600",letterSpacing:-0.2}));
  Insert(qt,T({name:"Detail",content:"Review them in the Outbox before Mercury sends.",fontSize:13,fill:"$text-2",width:"fill_container",textGrowth:"fixed-width"}));
  const qa=Insert(qb,{type:"frame",name:"Action",padding:[7,14],cornerRadius:"$r-sm",fill:"$panel",stroke:"$border-strong",strokeWidth:1});
  Insert(qa,T({name:"Label",content:"Review",fontSize:12,fontWeight:"500"}));
};
lightId=sheet("Mercury by EBSY / Light","light",0);build(lightId);
darkId=sheet("Mercury by EBSY / Dark","dark",1360);build(darkId);

const GRAD={type:"gradient",gradientType:"linear",rotation:220,colors:[{color:"#C7B6FF",position:0},{color:"#9C84F0",position:0.55},{color:"#7A5EDB",position:1}]};
const BAR={type:"gradient",gradientType:"linear",rotation:270,colors:[{color:"#C7B6FF",position:0},{color:"$accent",position:1}]};
const SH={type:"shadow",offset:{x:0,y:1},blur:2,color:"$shadow-sm"};
const F=(p,o)=>Insert(p,{type:"frame",...o});
const I=(p,ic,s,f)=>Insert(p,{type:"icon",name:"Icon "+ic,library:"phosphor",icon:ic,width:s,height:s,fill:f});
const W="fill_container";
const hOf=(id)=>Get(id,(n,c)=>n.id===id?c.bounds:undefined)[0];
const panel=(p,title,sub,w)=>{const x=F(p,{name:title,layout:"vertical",width:w||W,fill:"$panel",stroke:"$border",strokeWidth:1,cornerRadius:"$r-md",clip:true});
  const h=F(x,{name:"Head",layout:"vertical",gap:2,padding:[16,18,4,18],width:W});
  Insert(h,T({name:"Title",content:title,fontSize:15,fontWeight:"600",letterSpacing:-0.2}));
  Insert(h,T({name:"Sub",content:sub,fontSize:13,fill:"$text-3"}));return x;};
const btn2=(p,l)=>{const b=F(p,{name:"Button "+l,padding:[6,11],cornerRadius:"$r-sm",fill:"$panel",stroke:"$border-strong",strokeWidth:1});
  Insert(b,T({name:"Label",content:l,fontSize:13,fontWeight:"500"}));return b;};

const screen=(name,theme,x,y)=>{
  const s=F(document,{name,theme:{mode:theme},x,y,width:1440,height:1000,clip:true,fill:"$bg",padding:[8,8,8,0]});
  const sb=F(s,{name:"Sidebar",width:252,height:W,layout:"vertical",gap:18,padding:[10,14,6,14]});
  const br=F(sb,{name:"Brand",gap:10,alignItems:"center",padding:[2,6]});
  const mk=F(br,{name:"Mark",width:30,height:30,cornerRadius:9,justifyContent:"center",alignItems:"center",fill:GRAD,effect:{type:"shadow",offset:{x:0,y:2},blur:8,color:"#6D53D340"}});
  Insert(mk,T({name:"M",content:"M",fontSize:15,fontWeight:"600",fill:"#FFFFFF"}));
  const wm=F(br,{name:"Wordmark",layout:"vertical",gap:1});
  Insert(wm,T({name:"Name",content:"Mercury",fontSize:15.5,fontWeight:"600",letterSpacing:-0.3}));
  const by=F(wm,{name:"By",gap:3});
  Insert(by,T({name:"by",content:"by",fontSize:11.5,fontWeight:"500",fill:"$text-3"}));
  Insert(by,T({name:"EBSY",content:"EBSY",fontSize:11.5,fontWeight:"600",fill:"$accent-deep"}));
  const cta=F(sb,{name:"CTA",gap:8,width:W});
  const fb=F(cta,{name:"Find businesses",width:W,height:34,gap:6,justifyContent:"center",alignItems:"center",cornerRadius:"$r-sm",fill:"$accent",effect:SH});
  I(fb,"plus",14,"$accent-contrast");
  Insert(fb,T({name:"Label",content:"Find businesses",fontSize:13,fontWeight:"500",fill:"$accent-contrast"}));
  const rf=F(cta,{name:"Refresh",width:34,height:34,justifyContent:"center",alignItems:"center",cornerRadius:"$r-sm",fill:"$panel",stroke:"$border",strokeWidth:1,effect:SH});
  I(rf,"arrow-clockwise",16,"$text-2");
  const item=(p,ic,l,a,meta)=>{const b=F(p,{name:"Nav "+l,width:W,padding:[7,10],gap:10,alignItems:"center",cornerRadius:"$r-sm",...(a?{fill:"$panel",stroke:"$border",strokeWidth:1,effect:SH}:{})});
    I(b,ic,17,a?"$accent":"$text-3");
    Insert(b,T({name:"Label",content:l,fontSize:13.5,fontWeight:a?"600":"500",fill:a?"$text":"$text-2",width:W,textGrowth:"fixed-width"}));
    if(meta==="2"){const c=F(b,{name:"Count",width:20,height:18,cornerRadius:999,fill:"$accent-soft",justifyContent:"center",alignItems:"center"});
      Insert(c,M({name:"N",content:"2",fontSize:10.5,fontWeight:"600",fill:"$accent-deep"}));}
    else if(meta)Insert(b,T({name:"Meta",content:meta,fontSize:11.5,fill:"$text-3"}));return b;};
  const nav=F(sb,{name:"Nav",layout:"vertical",gap:16,width:W});
  item(nav,"squares-four","Today",1,"2");
  for(const [g,its] of [["Prospecting",[["funnel","Signals"],["compass","Discover"],["buildings","Companies"],["address-book","Contacts"]]],["Outreach",[["megaphone","Campaigns"],["tray","Outbox"],["chat-circle-text","Conversations"]]],["Workspace",[["activity","Activity"],["gauge","Usage"],["gear-six","Settings"]]]]){
    const gr=F(nav,{name:"Group "+g,layout:"vertical",gap:1,width:W});
    const lb=F(gr,{name:"Group label",padding:[0,10,6,10]});
    Insert(lb,T({name:"Label",content:g,fontSize:11.5,fontWeight:"500",fill:"$text-3"}));
    for(const [ic,l] of its)item(gr,ic,l,0);}
  F(sb,{name:"Spacer",width:W,height:W});
  const ft=F(sb,{name:"Foot",layout:"vertical",gap:1,width:W});
  const ag=F(ft,{name:"Agent card",width:W,padding:[11,12],gap:10,alignItems:"center",fill:"$panel",stroke:"$border",strokeWidth:1,cornerRadius:"$r-md",effect:SH});
  Insert(ag,{type:"ellipse",name:"Status dot",width:8,height:8,fill:"$text-3",stroke:"$panel-raised",strokeWidth:3,strokeAlignment:"outer"});
  const at=F(ag,{name:"Agent text",layout:"vertical",width:W});
  Insert(at,T({name:"Title",content:"Mercury is stopped",fontSize:12.5,fontWeight:"600"}));
  Insert(at,T({name:"Sub",content:"Open controls",fontSize:11.5,fill:"$text-3"}));
  I(ag,"caret-right",14,"$text-3");
  F(ft,{name:"Gap",width:W,height:7});
  item(ft,"question","Help",0);item(ft,"circle-half","Appearance",0,"Auto");

  const sh=F(s,{name:"Content sheet",width:W,height:W,layout:"vertical",fill:"$panel",stroke:"$border",strokeWidth:1,cornerRadius:14,clip:true,effect:SH});
  const tb=F(sh,{name:"Topbar",width:W,height:56,padding:[0,18],gap:8,alignItems:"center",stroke:"$border",strokeWidth:{bottom:1}});
  I(tb,"squares-four",16,"$text-3");
  Insert(tb,T({name:"Crumb",content:"Today",fontSize:13.5,fontWeight:"500"}));
  const mn=F(sh,{name:"Main",width:W,height:W,layout:"vertical",gap:16,padding:[28,28,20,28]});
  const hd=F(mn,{name:"Section head",layout:"vertical",gap:4,padding:[0,0,6,0]});
  Insert(hd,T({name:"Title",content:"Today",fontSize:24,fontWeight:"600",letterSpacing:-0.67}));
  Insert(hd,T({name:"Sub",content:"Anything waiting on you, then everything Mercury has built so far.",fontSize:13.5,fill:"$text-2"}));

  const kr=F(mn,{name:"KPIs",width:W,gap:16});
  for(const [l,v,b,r] of [["Companies","412","380"," profiled"],["Contacts","214","1,906"," signals collected"],["Awaiting approval","12","","Next send today, 10:40 AM"],["Live conversations","7","2"," new replies today"]]){
    const k=F(kr,{name:"KPI "+l,layout:"vertical",width:W,fill:"$panel",stroke:"$border",strokeWidth:1,cornerRadius:"$r-md",clip:true});
    const kb=F(k,{name:"Body",layout:"vertical",gap:8,padding:[15,16,16,16],width:W});
    Insert(kb,T({name:"Label",content:l,fontSize:12.5,fill:"$text-2"}));
    Insert(kb,T({name:"Value",content:v,fontSize:28,fontWeight:"600",letterSpacing:-0.85}));
    const kf=F(k,{name:"Foot",width:W,padding:[10,16],gap:3,fill:"$panel-raised",stroke:"$border",strokeWidth:{top:1}});
    if(b)Insert(kf,T({name:"Figure",content:b,fontSize:12,fontWeight:"600",fill:"$accent-deep"}));
    Insert(kf,T({name:"Text",content:r,fontSize:12,fill:"$text-3"}));}

  const r1=F(mn,{name:"Row 1",width:W,gap:16});
  const ny=panel(r1,"Needs you","Decisions only you can make, most blocking first.");
  const ib=F(ny,{name:"Inbox",layout:"vertical",width:W,padding:[10,0,0,0]});
  for(const [t,d,a] of [["12 emails waiting for your approval","Opening emails to roofers in Denver and Boulder. Mercury sends them on a human-like schedule once you approve.","Review outbox"],["Ridgeline Roofing asked about pricing","Mercury drafted a reply with the Growth plan and a 15-minute call link. Check it before it goes out.","Open reply"]]){
    const q=F(ib,{name:"Queue item",width:W,padding:[14,18],gap:24,alignItems:"center",stroke:"$border",strokeWidth:{top:1}});
    const qt=F(q,{name:"Text",layout:"vertical",gap:2,width:W});
    const qs=F(qt,{name:"State",gap:6,alignItems:"center",padding:[0,0,3,0]});
    I(qs,"clock",14,"#C98A1E");
    Insert(qs,T({name:"Label",content:"Needs you",fontSize:11.5,fontWeight:"500",fill:"$text-3"}));
    Insert(qt,T({name:"Title",content:t,fontSize:14,fontWeight:"600",letterSpacing:-0.14}));
    Insert(qt,T({name:"Detail",content:d,fontSize:13,lineHeight:1.5,fill:"$text-2",width:W,textGrowth:"fixed-width"}));
    btn2(q,a);}
  const qa=panel(r1,"Quick actions","Shortcuts to the usual next steps.",368);
  const ql=F(qa,{name:"List",layout:"vertical",width:W,padding:[6,8,10,8]});
  for(const [ic,t,d] of [["funnel","Confirm signals","Decide what defines a good prospect."],["compass","Find businesses","Estimate first, then run a source."],["tray","Review the outbox","Approve emails before they send."],["download-simple","Export prospects","Sequencer-ready CSV."]]){
    const r=F(ql,{name:"Quick "+t,width:W,padding:10,gap:12,alignItems:"center",cornerRadius:"$r-sm"});
    const sq=F(r,{name:"Icon tile",width:32,height:32,cornerRadius:"$r-sm",fill:"$accent-soft",justifyContent:"center",alignItems:"center"});
    I(sq,ic,17,"$accent-deep");
    const tx=F(r,{name:"Text",layout:"vertical",width:W});
    Insert(tx,T({name:"Title",content:t,fontSize:13.5,fontWeight:"600"}));
    Insert(tx,T({name:"Sub",content:d,fontSize:12.5,fill:"$text-3"}));
    I(r,"caret-right",14,"$text-3");}

  const r2=F(mn,{name:"Row 2",width:W,gap:16});
  const pp=panel(r2,"Pipeline","How far each business has made it through Mercury.");
  const fu=F(pp,{name:"Funnel",layout:"vertical",gap:12,width:W,padding:[16,18,18,18]});
  const stages=[["Found","Businesses discovered",412],["Profiled","Website and signals read",380],["Contacts","Decision-makers found",214],["In outbox","Drafted, waiting to send",36],["Talking","Replied, in conversation",7]];
  const tracks=[];
  for(const [k,d,v] of stages){
    const r=F(fu,{name:"Stage "+k,width:W,gap:16,alignItems:"center"});
    const kk=F(r,{name:"Key",layout:"vertical",width:170});
    Insert(kk,T({name:"Stage",content:k,fontSize:13,fontWeight:"600"}));
    Insert(kk,T({name:"Desc",content:d,fontSize:12,fill:"$text-3"}));
    tracks.push([F(r,{name:"Track",width:W,height:10,cornerRadius:99,fill:"$panel-raised",clip:true}),v]);
    Insert(r,{type:"text",name:"Value",content:v.toLocaleString("en-US"),fontFamily:"$font-mono",fontSize:13.5,fontWeight:"500",fill:"$text",width:64,textGrowth:"fixed-width",textAlign:"right"});}
  const pf=F(pp,{name:"Foot",width:W,padding:[11,18],justifyContent:"space_between",alignItems:"center",fill:"$panel-raised",stroke:"$border",strokeWidth:{top:1}});
  Insert(pf,T({name:"Text",content:"1.7%% of found businesses are talking to you",fontSize:12.5,fill:"$text-2"}));
  const lk=F(pf,{name:"Link",gap:5,alignItems:"center"});
  Insert(lk,T({name:"Label",content:"Open companies",fontSize:12.5,fontWeight:"500",fill:"$text-2"}));
  I(lk,"caret-right",13,"$text-2");
  const ac=panel(r2,"Recent activity","What Mercury did this morning.",368);
  const al=F(ac,{name:"List",layout:"vertical",width:W,padding:[8,0,6,0]});
  [["chat-circle-text","Ridgeline Roofing replied","Classified as interested","10:41"],["tray","12 emails staged for review","Opening sequence, Denver roofers","10:05"],["address-book","Verified 31 contact emails","Reoon, 4 catch-alls flagged risky","9:48"],["buildings","Profiled 52 company websites","Booking, ads and tech-stack signals","9:30"],["compass","Found 48 roofers in Denver, CO","DataForSEO listings, $0.02","9:12"]].forEach(([ic,t,d,tm],i)=>{
    const r=F(al,{name:"Event "+(i+1),width:W,padding:[10,18],gap:12,alignItems:"start",...(i?{stroke:"$border",strokeWidth:{top:1}}:{})});
    const ii=F(r,{name:"Icon wrap",padding:[1,0,0,0]});I(ii,ic,16,"$text-3");
    const tx=F(r,{name:"Text",layout:"vertical",gap:1,width:W});
    Insert(tx,T({name:"Title",content:t,fontSize:13,fontWeight:"500"}));
    Insert(tx,T({name:"Sub",content:d,fontSize:12,fill:"$text-3"}));
    Insert(r,M({name:"Time",content:tm,fontSize:11.5}));});
  for(const [t,v] of tracks){const w=hOf(t).width;Insert(t,{type:"rectangle",name:"Fill",width:Math.max(8,Math.round(w*v/412)),height:10,cornerRadius:99,fill:BAR});}
  return s;};
const sy=Math.max(hOf(lightId).height,hOf(darkId).height)+120;
todayLightId=screen("Mercury / Today (light)","light",0,sy);
todayDarkId=screen("Mercury / Today (dark)","dark",1560,sy);
Get(document,(n,c)=>c.problems&&Print(n.name,"|",c.parentCtx&&c.parentCtx.node.name,"|",c.problems));
for(const [id,f] of [[lightId,"light"],[darkId,"dark"],[todayLightId,"today-light"],[todayDarkId,"today-dark"]])Print("EXPORTMAP",id,f);
Export([lightId,darkId,todayLightId,todayDarkId],"png","%(out)s");
""" % {"vars": json.dumps(variables), "out": str(Path(__file__).resolve().parent / "exports")}


def rename_exports(log: str) -> None:
    """Exports land as <nodeId>.png; rename them from the EXPORTMAP lines."""
    import re
    out = Path(__file__).resolve().parent / "exports"
    for node_id, name in re.findall(r"EXPORTMAP (\S+) ([\w-]+)", log):
        src = out / f"{node_id}.png"
        if src.exists():
            src.replace(out / f"{name}.png")
            print(f"{src.name} -> {name}.png")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["rename"]:  # pen output on stdin
        rename_exports(sys.stdin.read())
        sys.exit()
    js = " ".join(line.strip() for line in JS.strip().splitlines())
    print("execute(" + json.dumps({"input": js}) + ")")
    print("save()")
    print("exit()")
