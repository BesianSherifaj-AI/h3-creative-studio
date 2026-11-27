/* Film edit room. Timeline coordinates are delivered 24 fps frames; trims address source seconds. */
(() => {
  "use strict";
  const H=()=>window.H3, root=()=>document.getElementById("cutWorkspace");
  const q=id=>root().querySelector("#"+id);
  const esc=v=>H().esc(v);
  const room={active:false,view:null,selected:null,frame:0,zoom:80,track:0,epoch:0,film:null,
              pending:false,refreshing:0,forceRefresh:false,requestedShot:null,strips:new Map()};
  const current=()=>room.view?.clips.find(c=>c.shot===room.selected);
  const shown=()=>room.view?.clips.find(c=>c.start_frame<=room.frame && room.frame<c.end_frame) || room.view?.clips.at(-1);
  const tc=n=>{n=Math.max(0,Math.round(n));return [Math.floor(n/1440),Math.floor(n/24)%60,n%24].map(i=>String(i).padStart(2,"0")).join(":");};
  const call=(op,args={})=>H().api(op,{version:room.view?.version,track:room.track,...args});
  function safe(fn){return async e=>{try{await fn(e);}catch(err){if(room.active)H().toast(err.message);}};}
  function stamp(){return room.view.project+"|"+room.track+"|";}
  function button(id,title,caption,other=""){return '<button type="button" id="'+id+'" title="'+title+'" '+other+'>'+caption+'</button>';}
  function controls(){
    return '<div class="cut-stage"><section class="cut-viewer"><header><span class="cut-dot"></span>Program <span id="cutMonitorLabel"></span></header>'+
      '<div class="cut-monitor"><video id="cutVideo" preload="metadata" playsinline></video><span id="cutUnavailable"></span></div>'+
      '<div class="cut-transport">'+button("cutPrev","Previous frame","&#9666;")+button("cutPlay","Play or pause the film (Space)","&#9654; Play film")+
      button("cutNext","Next frame","&#9656;")+'<output id="cutClock"></output><select id="cutAudio" aria-label="Monitor audio track"></select></div></section>'+
      '<aside class="cut-inspector" id="cutProperties"></aside></div>'+
      '<section class="cut-arrangement"><header><strong>Film timeline</strong><span id="cutSummary"></span><div class="cut-tools">'+
      button("cutImport","Add footage to the cut","Import video")+button("cutScore","Mix music, dialogue or narration","Soundtrack")+button("cutUndo","Undo timeline edit","Undo")+button("cutRedo","Redo timeline edit","Redo")+button("cutSplit","Split at playhead (S)","Split")+
      button("cutRemove","Remove selected shot from this cut","Remove")+
      '<label class="cut-zoom">Zoom <input id="cutZoom" aria-label="Timeline zoom" type="range" min="28" max="240" step="2"></label></div></header>'+
      '<div class="cut-scroller" id="cutScroll"><div class="cut-sheet" id="cutSheet"><div class="cut-row cut-scale"><b>Time</b><div id="cutRuler" class="cut-rail"></div></div>'+
      '<div class="cut-row cut-picture"><b>V1 <small>Picture</small></b><div id="cutPicture" class="cut-rail"></div></div>'+
      '<div class="cut-row cut-wave"><b id="cutTrackLabel">A1 <small>Clip audio</small></b><div id="cutAudioLane" class="cut-rail"></div></div>'+
      '<div id="cutBeds"></div><div class="cut-indicator" id="cutHead" aria-hidden="true"></div></div></div></section>';
  }
  function seek(frame,follow=true){
    room.frame=Math.max(0,Math.min(Math.max(0,room.view.frames-1),Math.round(frame)));
    if(follow && shown()?.shot!==room.selected){room.selected=shown()?.shot;inspector();selection();}
    updateHead();
    const video=q("cutVideo"),clip=shown();
    q("cutMonitorLabel").textContent=clip?.name||"";
    if(room.film){
      if(video.dataset.film!==room.film){video.dataset.film=room.film;video.setAttribute("src","/asset/"+room.film);}
      video.currentTime=room.frame/24;
    }else{
      const src=clip?.asset?"/take/"+clip.take:"";
      if(src){
        if(video.getAttribute("src")!==src)video.src=src;
        video.currentTime=Math.max(0,(room.frame-clip.start_frame)/24);
      }else if(video.hasAttribute("src")){video.removeAttribute("src");video.load();}
    }
    q("cutUnavailable").textContent=clip&&!clip.asset?(clip.error||"This shot has no rendered take."):"";
  }
  function updateHead(){
    q("cutClock").textContent=tc(room.frame)+" / "+tc(room.view.frames);
    q("cutHead").style.left="calc(var(--cut-gutter) + "+(room.frame/24*room.zoom)+"px)";
    q("cutSplit").disabled=!canSplit();q("cutSplit").title=canSplit()?"Split at playhead (S)":"Place the playhead inside an unlocked shot";
  }
  function canSplit(){const c=current();return c?.asset && !c.locked && room.frame>c.start_frame && room.frame<c.end_frame;}
  function selection(){
    for(const el of root().querySelectorAll("[data-cut-shot]"))el.classList.toggle("chosen",el.dataset.cutShot===room.selected);
  }
  function inspector(){
    const c=current(),box=q("cutProperties");
    q("cutRemove").disabled=!c||c.locked;
    if(!c){box.innerHTML='<h3>Clip inspector</h3><p>Select a clip on the timeline.</p>';return;}
    const disabled=!c.asset||c.locked?"disabled":"";
    box.innerHTML='<header>Clip inspector'+(c.locked?'<span>Locked</span>':'')+'</header><h3>'+esc(c.name)+'</h3>'+
      '<p>'+esc(c.kind||"Shot")+(c.width?' · '+c.width+' × '+c.height:'')+'</p>'+
      '<div class="cut-io"><label>Source in <input id="cutIn" type="number" min="0" step=".001" value="'+c.source_in+'" '+disabled+'></label>'+
      '<label>Source out <input id="cutOut" type="number" step=".001" max="'+c.source_duration+'" value="'+c.source_out+'" '+disabled+'></label></div>'+
      '<div class="cut-edit-actions">'+button("cutApply","Save source in and out","Apply trim",disabled)+
      button("cutOpenShot","Open this shot in the studio","Open shot")+'</div><dl><dt>Timeline in</dt><dd>'+tc(c.start_frame)+
      '</dd><dt>Duration</dt><dd>'+tc(c.end_frame-c.start_frame)+'</dd></dl>';
    q("cutApply").onclick=safe(()=>edit("cut_trim",{start:Number(q("cutIn").value),end:Number(q("cutOut").value)}));
    q("cutOpenShot").onclick=()=>window.dispatchEvent(new CustomEvent("h3-open-shot",{detail:{shot:c.shot}}));
  }
  function waveSvg(peaks){
    if(!peaks?.length)return "";
    const up=peaks.map((p,i)=>i+","+(16-15*Math.sqrt(Math.max(0,p))).toFixed(2)).join(" ");
    const down=peaks.map((p,i)=>(peaks.length-1-i)+","+(16+15*Math.sqrt(Math.max(0,peaks[peaks.length-1-i]))).toFixed(2)).join(" ");
    return '<svg viewBox="0 0 '+(peaks.length-1)+' 32" preserveAspectRatio="none" aria-hidden="true"><polygon points="'+up+' '+down+'"/></svg>';
  }
  function visual(el,strip){
    if(!el || !strip)return;
    if(el.classList.contains("cut-filmstrip")){
      el.replaceChildren(...strip.images.map(url=>{const im=new Image();im.alt="";im.src=url;return im;}));
    }else{el.innerHTML=waveSvg(strip.peaks);}
  }
  async function loadStrips(){
    const epoch=room.epoch;
    const jobs=[...room.view.clips.filter(c=>c.asset).map(c=>({take:c.take})),
      ...room.view.beds.filter(b=>b.track===room.track).map(b=>({bed:b.id}))];
    async function worker(){
      while(jobs.length && room.active && epoch===room.epoch){
        const job=jobs.shift(),id=job.take||job.bed,key=stamp()+id;
        try{
          let strip=room.strips.get(key);if(!strip){strip=await call("cut_strip",job);room.strips.set(key,strip);}
          if(!room.active || epoch!==room.epoch)return;
          for(const el of root().querySelectorAll('[data-strip="'+id+'"]'))visual(el,strip);
          if(job.bed){
            const el=root().querySelector('[data-bed-block="'+id+'"]');
            if(el)el.style.width=Math.max(1,Math.min(strip.duration,room.view.frames/24-Number(el.dataset.offset))*room.zoom)+"px";
          }
        }catch(err){
          if(!room.active || epoch!==room.epoch)return;
          for(const el of root().querySelectorAll('[data-strip="'+id+'"]'))el.title=err.message;
        }
      }
    }
    await Promise.all([worker(),worker()]);
  }
  function bedWidth(b){const duration=room.strips.get(stamp()+b.id)?.duration ?? Infinity;return Math.max(1,Math.min(duration,room.view.frames/24-b.offset)*room.zoom);}
  function clipStyle(c){return "left:"+c.start_frame/24*room.zoom+"px;width:"+Math.max(1,(c.end_frame-c.start_frame)/24*room.zoom)+"px";}
  function arrange(){
    const v=room.view;
    const w=Math.max(1,Math.ceil(v.frames/24*room.zoom));
    q("cutSheet").style.setProperty("--cut-width",w+"px");
    const step=room.zoom>=150?.5:room.zoom>=60?1:2;
    const marks=[];
    const visible=q("cutScroll").scrollLeft/room.zoom;
    const end=Math.min(v.frames/24,visible+(q("cutScroll").clientWidth+360)/room.zoom);
    for(let t=Math.max(0,Math.floor((visible-2)/step)*step);t<=end;t+=step)
      marks.push('<i style="left:'+t*room.zoom+'px"><span>'+tc(t*24).slice(0,5)+(step<1?"."+String(Math.round(t%1*24)).padStart(2,"0"):"")+'</span></i>');
    q("cutRuler").innerHTML=marks.join("");
    q("cutPicture").innerHTML=v.clips.map(c=>'<div class="cut-clip" draggable="true" role="button" tabindex="0" aria-label="'+esc(c.name)+'" data-cut-shot="'+c.shot+'" style="'+clipStyle(c)+'">'+
      '<div class="cut-filmstrip" data-strip="'+(c.take||"")+'"></div><span>'+esc(c.name)+(c.locked?" · Locked":"")+'</span>'+
      (c.asset&&!c.locked?'<i data-edge="in" title="Drag source in"></i><i data-edge="out" title="Drag source out"></i>':'')+'</div>').join("");
    q("cutAudioLane").innerHTML=v.clips.map(c=>'<div class="cut-peaks" data-cut-shot="'+c.shot+'" data-strip="'+(c.take||"")+'" style="'+clipStyle(c)+'"></div>').join("");
    q("cutBeds").innerHTML=v.beds.filter(b=>b.track===room.track).map(b=>'<div class="cut-row cut-bed"><b>Mix <small>Soundtrack</small></b><div class="cut-rail">'+
      '<div class="cut-bed-block" data-bed-block="'+b.id+'" data-offset="'+b.offset+'" style="left:'+b.offset*room.zoom+'px;width:'+bedWidth(b)+'px"><div data-strip="'+b.id+'"></div><span>'+esc(b.name)+'</span></div></div></div>').join("");
    q("cutTrackLabel").innerHTML="A"+(room.track+1)+" <small>Clip audio</small>";
    for(const el of root().querySelectorAll("[data-strip]"))visual(el,room.strips.get(stamp()+el.dataset.strip));
    selection();updateHead();
  }
  function clockAt(e){const box=q("cutRuler").getBoundingClientRect();return Math.round((e.clientX-box.left)/room.zoom*24);}
  function pickFrame(e){q("cutVideo").pause();seek(clockAt(e));}
  function hookArrangement(){
    const picture=q("cutPicture"),ruler=q("cutRuler");
    ruler.onpointerdown=e=>{ruler.setPointerCapture(e.pointerId);pickFrame(e);};
    ruler.onpointermove=e=>{if(ruler.hasPointerCapture(e.pointerId))pickFrame(e);};
    q("cutAudioLane").onclick=pickFrame;
    picture.onclick=e=>{if(!e.target.closest("[data-edge]"))pickFrame(e);};
    picture.onkeydown=e=>{if(e.key==="Enter" && e.target.dataset.cutShot){const c=room.view.clips.find(s=>s.shot===e.target.dataset.cutShot);q("cutVideo").pause();seek(c.start_frame);}};
    picture.ondragstart=e=>{const clip=e.target.closest("[data-cut-shot]");if(!clip || edgeDrag){e.preventDefault();return;}
      e.dataTransfer.setData("text/plain",clip.dataset.cutShot);e.dataTransfer.effectAllowed="move";};
    picture.ondragover=e=>{e.preventDefault();e.dataTransfer.dropEffect="move";};
    picture.ondrop=safe(async e=>{
      e.preventDefault();const from=e.dataTransfer.getData("text/plain"),target=e.target.closest("[data-cut-shot]");
      const ids=room.view.clips.map(c=>c.shot);if(!ids.includes(from)||target?.dataset.cutShot===from)return;
      const order=ids.filter(s=>s!==from);
      const pos=target?order.indexOf(target.dataset.cutShot)+(e.clientX>target.getBoundingClientRect().left+target.offsetWidth/2?1:0):order.length;
      order.splice(pos,0,from);room.selected=from;await edit("cut_reorder",{order});
    });
    let edgeDrag=null;
    picture.onpointerdown=e=>{
      const edge=e.target.closest("[data-edge]");if(!edge || e.button!==0)return;
      const c=room.view.clips.find(s=>s.shot===edge.closest("[data-cut-shot]").dataset.cutShot);
      e.preventDefault();e.stopPropagation();q("cutVideo").pause();room.selected=c.shot;inspector();selection();
      edgeDrag={c,edge:edge.dataset.edge,x:e.clientX};picture.setPointerCapture(e.pointerId);
    };
    picture.onpointermove=e=>{
      if(!edgeDrag)return;
      const {c,edge,x}=edgeDrag,delta=Math.round((e.clientX-x)/room.zoom*24)/24;
      if(edge==="in")q("cutIn").value=Math.max(0,Math.min(c.source_out-1/24,c.source_in+delta)).toFixed(6);
      else q("cutOut").value=Math.min(c.source_duration,Math.max(c.source_in+1/24,c.source_out+delta)).toFixed(6);
    };
    picture.onpointerup=safe(async e=>{
      if(!edgeDrag)return;const dx=Math.abs(e.clientX-edgeDrag.x);edgeDrag=null;
      if(picture.hasPointerCapture(e.pointerId))picture.releasePointerCapture(e.pointerId);
      if(dx>2)await edit("cut_trim",{start:+q("cutIn").value,end:+q("cutOut").value});
    });
    picture.onpointercancel=()=>{edgeDrag=null;inspector();};
    let lastBucket=0;
    q("cutScroll").onscroll=()=>{const bucket=Math.floor(q("cutScroll").scrollLeft/140);if(bucket!==lastBucket){lastBucket=bucket;rulerOnly();}};
    function rulerOnly(){
      const step=room.zoom>=150?.5:room.zoom>=60?1:2,scroll=q("cutScroll"),start=Math.max(0,scroll.scrollLeft/room.zoom-2);
      let html="";for(let t=Math.floor(start/step)*step;t<=(scroll.scrollLeft+scroll.clientWidth+180)/room.zoom && t<=room.view.frames/24;t+=step)
        html+='<i style="left:'+t*room.zoom+'px"><span>'+tc(t*24).slice(0,5)+(step<1?"."+String(Math.round(t%1*24)).padStart(2,"0"):"")+'</span></i>';
      ruler.innerHTML=html;
    }
  }
  async function edit(op,args={}){
    q("cutVideo").pause();
    const result=await call(op,{shot:room.selected,frame:room.frame,...args});
    if(result.shot)room.requestedShot={id:result.shot,start:false};
    await H().refresh();await refresh();
  }
  async function play(){
    const vid=q("cutVideo");if(!vid.paused){vid.pause();return;}
    if(room.pending || !room.view.clips.length)return;
    const epoch=room.epoch;
    if(!room.film){
      room.pending=true;q("cutPlay").textContent="Preparing film…";q("cutPlay").disabled=true;
      try{
        const request=await call("cut_preview");const r=request.task?await H().waitTask(request.task):request;
        if(!room.active || room.epoch!==epoch)return;
        room.film=r.asset;seek(room.frame,false);
      }finally{if(room.active && room.epoch===epoch){room.pending=false;q("cutPlay").disabled=false;q("cutPlay").textContent="▶ Play film";}}
    }
    if(room.frame>=room.view.frames-1)seek(0);
    await vid.play();
  }
  function connect(){
    const vid=q("cutVideo");
    vid.onplay=()=>q("cutPlay").textContent="Ⅱ Pause";
    vid.onpause=()=>{if(!room.pending)q("cutPlay").textContent="▶ Play film";syncClock();};
    vid.ontimeupdate=()=>{if(!vid.paused)syncClock();};
    function syncClock(){
      if(!room.active || !room.film || vid.seeking || vid.dataset.film!==room.film)return;
      room.frame=Math.min(room.view.frames-1,Math.floor(vid.currentTime*24+.000001));updateHead();
      const c=shown();q("cutMonitorLabel").textContent=c?.name||"";
      if(c && room.selected!==c.shot){room.selected=c.shot;inspector();selection();}
      const x=room.frame/24*room.zoom,s=q("cutScroll");if(x>s.scrollLeft+s.clientWidth-110 || x<s.scrollLeft)s.scrollLeft=Math.max(0,x-90);
    };
    q("cutImport").onclick=safe(()=>importFile("clip"));q("cutScore").onclick=safe(soundtrackDialog);
    q("cutPlay").onclick=safe(play);
    q("cutPrev").onclick=()=>{vid.pause();seek(room.frame-1);};q("cutNext").onclick=()=>{vid.pause();seek(room.frame+1);};
    q("cutUndo").onclick=safe(()=>edit("cut_undo"));q("cutRedo").onclick=safe(()=>edit("cut_redo"));
    q("cutSplit").onclick=safe(()=>edit("cut_split"));q("cutRemove").onclick=safe(()=>edit("cut_remove"));
    q("cutZoom").value=room.zoom;q("cutZoom").oninput=()=>{room.zoom=Number(q("cutZoom").value);arrange();};
    q("cutAudio").onchange=()=>{room.track=Number(q("cutAudio").value);room.film=null;refresh(true).catch(e=>H().toast(e.message));};
    hookArrangement();
  }
  function revealRequested(){
    const request=room.requestedShot;room.requestedShot=null;
    const clip=room.view.clips.find(c=>c.shot===request?.id);
    if(!clip)return false;
    q("cutVideo").pause();room.selected=clip.shot;inspector();selection();
    const inside=room.frame>=clip.start_frame && room.frame<clip.end_frame;
    seek(!request.start && inside?room.frame:clip.start_frame,false);
    const x=room.frame/24*room.zoom,scroll=q("cutScroll");
    if(x<scroll.scrollLeft || x>scroll.scrollLeft+scroll.clientWidth-100)scroll.scrollLeft=Math.max(0,x-80);
    return true;
  }
  async function refresh(force=false,shot=null){
    if(!room.active)return;
    if(force)room.forceRefresh=true;
    if(shot)room.requestedShot={id:shot,start:true};
    const serial=++room.refreshing,view=await H().api("cut_view");
    if(!room.active||serial!==room.refreshing)return;
    if(!room.forceRefresh && view.project===room.view?.project && view.version===room.view.version){revealRequested();return;}
    room.forceRefresh=false;
    if(room.view?.project!==view.project){room.selected=null;room.frame=0;room.track=0;room.strips.clear();}
    for(const bed of view.beds)if(JSON.stringify(bed)!==JSON.stringify(room.view?.beds.find(b=>b.id===bed.id))){for(let i=0;i<8;i++)room.strips.delete(view.project+"|"+i+"|"+bed.id);}
    q("cutVideo").pause();q("cutVideo").dataset.film="";room.film=null;room.pending=false;room.epoch++;room.view=view;
    room.track=Math.min(room.track,view.audio_tracks-1);
    q("cutAudio").innerHTML=Array.from({length:view.audio_tracks},(_,i)=>'<option value="'+i+'">Monitor A'+(i+1)+'</option>').join("");
    q("cutAudio").value=room.track;q("cutPlay").disabled=!view.clips.length;q("cutPlay").textContent="▶ Play film";
    q("cutUndo").disabled=!view.undo;q("cutRedo").disabled=!view.redo;q("cutUndo").title=view.undo?"Undo "+view.undo:"No edit to undo";q("cutRedo").title=view.redo?"Redo "+view.redo:"No edit to redo";
    q("cutSummary").textContent=view.clips.length+" "+(view.clips.length===1?"shot":"shots")+" · "+tc(view.frames)+" · 24 fps";
    if(!current())room.selected=shown()?.shot;
    arrange();
    if(!revealRequested()){inspector();seek(room.frame,false);}
    loadStrips();
  }
  async function open(shot=null){
    if(room.active)return refresh(false,shot);room.active=true;root().hidden=false;root().innerHTML=controls();connect();
    await refresh(true,shot);
  }
  function close(){
    room.active=false;room.epoch++;room.refreshing++;room.pending=false;room.requestedShot=null;room.forceRefresh=false;
    const video=q("cutVideo");if(video){video.pause();video.removeAttribute("src");video.load();}
    root().hidden=true;
  }
  document.addEventListener("keydown",safe(async e=>{
    if(!room.active || document.querySelector("dialog[open]") || /INPUT|SELECT|TEXTAREA|BUTTON/.test(e.target.tagName))return;
    if(e.code==="Space"){e.preventDefault();await play();}
    else if(e.code==="KeyS"&&!e.ctrlKey && canSplit()){e.preventDefault();await edit("cut_split");}
    else if(e.ctrlKey && e.code==="KeyZ"){e.preventDefault();await edit(e.shiftKey?"cut_redo":"cut_undo");}
  }));
  window.CutRoom={open,close,refresh,reveal:shot=>refresh(false,shot)};
})();
