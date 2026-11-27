/* Source-pixel brushes are saved as a new revision or an exact timeline correction. */
(() => {
  "use strict";
  const ui = () => window.H3;
  const by = id => document.getElementById(id);
  const url = id => "/asset/" + encodeURIComponent(id);
  const issue = e => {
    if (by("dialog").open) by("dialogError").textContent = e.message;
    else ui().toast(e.message);
  };
  const act = (id, fn) => { by(id).onclick = async () => { try { await fn(); } catch (e) { issue(e); } }; };
  const img = async id => {
    const image = new Image();
    image.src = url(id);
    await image.decode();
    return image;
  };

  async function brush({title,asset,old,initial,save,back}) {
    const [source, saved] = await Promise.all([img(asset),old ? img(old) : null]);
    const w = source.naturalWidth, h = source.naturalHeight;
    const scale = Math.min(820/w, 400/h);
    const cw = Math.max(1,Math.round(w*scale)), ch = Math.max(1,Math.round(h*scale));
    const strokes = [];
    const opts = '<label class="field">Brush mode<select id="brushMode"><option value="add">Add to mask</option><option value="erase">Erase from mask</option></select></label>' +
      '<label class="field">Brush radius · source pixels<input id="brushSize" type="number" min="1" max="'+Math.min(w,h)+'" value="'+Math.max(2,Math.round(Math.min(w,h)*.035))+'"></label>' +
      '<label class="field">Starting mask<select id="brushInitial">'+(saved?'<option value="current">Saved mask</option>':'')+
      '<option value="clear">Empty</option><option value="full">Full frame</option></select></label>';
    ui().modal(title,'<p class="note">The blue overlay marks selected pixels.</p>'+
      '<canvas id="brushCanvas" width="'+cw+'" height="'+ch+'" aria-label="Paint source mask" '+
      'style="display:block;max-width:100%;height:auto;margin:8px auto;background:#111;touch-action:none;cursor:crosshair"></canvas>'+
      '<div class="row" style="align-items:end;flex-wrap:wrap">'+opts+'<button type="button" id="brushUndo">Undo stroke</button></div>',[
        ...(back ? [{label:"Back",run:back}] : []),
        {label:"Save mask",primary:true,run:async () => {
          await save({initial:by("brushInitial").value,strokes});
        }}
      ]);
    const canvas = by("brushCanvas"), ctx=canvas.getContext("2d");
    by("brushInitial").value = initial;
    by("brushMode").value = ((initial==="current" && !saved) || initial==="full") ? "erase" : "add";
    const mask=document.createElement("canvas");
    mask.width=cw;mask.height=ch;
    const mc=mask.getContext("2d",{willReadFrequently:true});
    const tint=document.createElement("canvas");
    tint.width=cw;tint.height=ch;
    const tc=tint.getContext("2d");
    function redraw() {
      const start=by("brushInitial").value;
      mc.fillStyle=start==="full"?"white":"black";
      mc.fillRect(0,0,cw,ch);
      if(start==="current" && saved)mc.drawImage(saved,0,0,cw,ch);
      for(const stroke of strokes) {
        const r=Math.max(.5,stroke.radius*Math.min(cw,ch));
        const points=stroke.points.map(([x,y])=>[x*(cw-1),y*(ch-1)]);
        mc.fillStyle=mc.strokeStyle=stroke.show?"white":"black";
        mc.lineWidth=r*2;mc.lineCap="round";mc.lineJoin="round";
        mc.beginPath();mc.moveTo(points[0][0],points[0][1]);
        for(const [x,y] of points.slice(1))mc.lineTo(x,y);
        mc.stroke();
        mc.beginPath();mc.arc(points[0][0],points[0][1],r,0,Math.PI*2);mc.fill();
      }
      const pixels=mc.getImageData(0,0,cw,ch);
      for(let i=0;i<pixels.data.length;i+=4){
        const a=Math.round(pixels.data[i]*.50);
        pixels.data[i]=79;pixels.data[i+1]=162;pixels.data[i+2]=250;pixels.data[i+3]=a;
      }
      tc.putImageData(pixels,0,0);
      ctx.clearRect(0,0,cw,ch);ctx.drawImage(source,0,0,cw,ch);ctx.drawImage(tint,0,0);
    }
    let active=null, paintRequest=0;
    const requestPaint=()=>{if(!paintRequest)paintRequest=requestAnimationFrame(()=>{paintRequest=0;redraw();});};
    function addPoint(e) {
      const box=canvas.getBoundingClientRect();
      const clamp=n=>Math.min(1,Math.max(0,n));
      active.points.push([clamp((e.clientX-box.x)/box.width),clamp((e.clientY-box.y)/box.height)]);
    }
    canvas.onpointerdown=e=>{
      if(active || e.button!==0)return;
      const r=Number(by("brushSize").value);
      if(!Number.isFinite(r)||r<1||r>Math.min(w,h)){issue(Error("Enter a valid source-pixel brush radius."));return;}
      canvas.setPointerCapture(e.pointerId);
      active={show:by("brushMode").value==="add",radius:r/Math.min(w,h),points:[]};
      strokes.push(active);addPoint(e);requestPaint();
    };
    canvas.onpointermove=e=>{if(active){addPoint(e);requestPaint();}};
    canvas.onpointerup=canvas.onpointercancel=e=>{if(active){active=null;requestPaint();}};
    by("brushInitial").onchange=requestPaint;
    act("brushUndo",()=>{strokes.pop();requestPaint();});
    redraw();
  }

  async function paint() {
    const {rev,layerIndex}=ui().get(), layer=rev?.layers[layerIndex];
    if(!layer)throw Error("Choose a layer first.");
    await brush({title:"Paint layer mask",asset:layer.asset,old:layer.mask,initial:layer.mask?"current":"full",
      save:async payload=>{
        const r=await ui().api("paint_layer",{revision:rev.id,index:layerIndex,...payload});
        await ui().setRevision(r.revision);ui().closeModal();ui().toast("Saved a new masked layer revision.");
      }});
  }

  async function openFrame() {
    const r=await ui().api("frame",await ui().displayedPosition());
    await ui().setRevision(r.revision);ui().toast("Opened the source frame at PTS "+r.pts+".");
  }
  async function finish(r) {
    ui().closeModal();ui().toast("Rendering a lossless edit and playback preview…");
    await ui().waitTask(r.task);await ui().refresh();ui().toast("Edited take saved.");
  }
  async function oneFrame() {
    const {rev}=ui().get();
    if(!rev)throw Error("Open a source frame first.");
    await finish(await ui().api("frame_branch",{revision:rev.id}));
  }

  async function range() {
    const selection=await ui().displayedPosition();
    const info=await ui().api("edit_info",selection);
    const draft={start:info.source_index,end:Math.min(info.source_end,info.source_index+23),
                 at:info.source_index,action:"recolor",color:"#657dff",state:"keyframe"};
    const number=(id,label,value)=>'<label class="field">'+label+'<input id="'+id+'" type="number" step="1" min="'+info.source_start+
      '" max="'+info.source_end+'" value="'+value+'"></label>';
    const fields=()=>{draft.start=Number(by("rangeStart").value);draft.end=Number(by("rangeEnd").value);
      draft.at=Number(by("rangeAt").value);draft.action=by("rangeAction").value;draft.color=by("rangeColor").value;
      draft.state=by("rangeState").value;};
    async function inspect() {
      if (!Number.isInteger(draft.at) || draft.at<draft.start || draft.at>draft.end)
        throw Error("Choose an inspection frame inside the range.");
      const frame=await ui().api("edit_info",{take:selection.take,source_index:draft.at,propagate_from:draft.start,preview:true});
      const tracking=frame.tracking;
      const [source,mask]=await Promise.all([img(frame.asset),tracking.mask?img(tracking.mask):null]);
      const scale=Math.min(820/source.width,380/source.height);
      const w=Math.max(1,Math.round(source.width*scale)),h=Math.max(1,Math.round(source.height*scale));
      const status={tracked:"Propagated mask",keyframe:"Saved correction",reappears:"Reappearance correction",
                    hidden:"Subject hidden",ambiguous:"Tracking needs a correction",unmarked:"No starting mask"};
      const move=async index=>{draft.at=index;await inspect();};
      ui().modal("Inspect tracked mask",
        '<canvas id="trackOverlay" width="'+w+'" height="'+h+'" aria-label="Tracked source mask" style="display:block;max-width:100%;margin:auto"></canvas>'+
        '<label><input type="checkbox" id="trackVisible" checked> Show mask overlay</label>'+
        '<p id="trackStatus">'+ui().esc(status[tracking.state])+' · frame '+draft.at+' · PTS '+frame.pts+'</p>'+
        (tracking.detail?'<p class="note">'+ui().esc(tracking.detail)+'</p>':'')+
        '<div class="row"><button type="button" id="trackPrev">Previous</button>'+number("trackAt","Source frame",draft.at)+
        '<button type="button" id="trackGo">Inspect frame</button><button type="button" id="trackNext">Next</button></div>',[
          {label:"Back to range",run:panel},
          {label:"Play from this frame",run:()=>play(draft.at)},
          ...(tracking.at!=null && tracking.at!==draft.at?[{label:"Go to first uncertain frame",run:()=>move(tracking.at)}]:[]),
          {label:"Correct this frame",primary:true,run:()=>brush({title:"Correct frame "+draft.at,asset:frame.asset,
            old:tracking.mask,initial:mask?"current":"clear",back:inspect,save:async payload=>{
              await ui().api("edit_anchor",{take:selection.take,source_index:draft.at,
                state:tracking.state==="hidden"?"reappears":"keyframe",base_mask:tracking.mask,...payload});
              await inspect();
            }})}
        ]);
      const ctx=by("trackOverlay").getContext("2d");
      const tint=document.createElement("canvas");tint.width=w;tint.height=h;
      if(mask){
        const mc=tint.getContext("2d",{willReadFrequently:true});mc.drawImage(mask,0,0,w,h);
        const pixels=mc.getImageData(0,0,w,h);
        for(let i=0;i<pixels.data.length;i+=4){
          const alpha=Math.round(pixels.data[i]*.60);
          pixels.data.set([79,162,250,alpha],i);
        }
        mc.putImageData(pixels,0,0);
      }
      const redraw=()=>{ctx.drawImage(source,0,0,w,h);if(mask && by("trackVisible").checked)ctx.drawImage(tint,0,0);};
      by("trackVisible").onchange=redraw;redraw();
      by("trackPrev").disabled=draft.at<=draft.start;by("trackNext").disabled=draft.at>=draft.end;
      act("trackPrev",()=>move(draft.at-1));act("trackNext",()=>move(draft.at+1));
      act("trackGo",()=>move(Number(by("trackAt").value)));
    }
    async function play(startAt) {
      let samples=[],resume=null,next=null,stopped=false,raf=0,shown=startAt;
      async function batch(index,prior) {
        const data=await ui().api("edit_info",{take:selection.take,source_index:index,
          propagate_from:draft.start,play_end:draft.end,resume:prior});
        await Promise.all(data.samples.map(async s=>{
          if(!s.overlay)return;
          const image=new Image();image.src=s.overlay;await image.decode();s.image=image;
        }));
        return data;
      }
      const first=await batch(startAt,null);
      if(!first.samples.length)throw Error("There are no review frames in this range.");
      function use(data){
        samples=data.samples;resume=data.resume;
        next=resume?batch(resume.index+1,resume).catch(error=>({error})):null;
      }
      use(first);
      ui().modal("Play tracked mask",
        '<div style="position:relative;margin:auto;width:min(100%,'+Math.round(380*info.width/info.height)+'px)">'+
        '<video id="trackMotion" playsinline preload="auto" style="display:block;width:100%"></video>'+
        '<canvas id="motionOverlay" width="480" height="'+Math.max(1,Math.round(480*info.height/info.width))+
        '" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none"></canvas></div>'+
        '<p id="motionStatus" aria-live="off"></p><label><input type="checkbox" id="motionVisible" checked> Show tracked mask</label>',[
          {label:"Pause and inspect",primary:true,run:()=>stop(shown)}
        ]);
      const v=by("trackMotion"),canvas=by("motionOverlay"),ctx=canvas.getContext("2d");
      let generation=0;
      function cleanup(){stopped=true;generation++;cancelAnimationFrame(raf);v.pause();}
      async function stop(index){if(stopped)return;cleanup();draft.at=index;await inspect();}
      const dialog=by("dialog");
      dialog.addEventListener("close",cleanup,{once:true});
      function paint(sample){
        shown=sample.index;ctx.clearRect(0,0,canvas.width,canvas.height);
        if(sample.image && by("motionVisible").checked)ctx.drawImage(sample.image,0,0,canvas.width,canvas.height);
        by("motionStatus").textContent="Source frame "+sample.index+" · "+sample.state;
      }
      async function advance(seconds) {
        if(stopped || !v.isConnected || !dialog.open)return;
        const end=samples[samples.length-1];
        if(seconds+.000002>=end.end){
          if(end.state==="ambiguous" || !next){await stop(end.index);return;}
          const prepared=next;
          v.pause();
          const data=await prepared;
          if(stopped)return;
          if(data.error)throw data.error;
          use(data);
          v.currentTime=samples[0].start;
          await v.play();seconds=samples[0].start;
        }
        const sample=samples.findLast(s=>s.start<=seconds+.000002)||samples[0];
        paint(sample);
        if(sample.state==="ambiguous"){await stop(sample.index);return;}
        const token=++generation;
        // Native presentation timestamps keep the overlay on the sample decoded by WebView.
        if(v.requestVideoFrameCallback){
          v.requestVideoFrameCallback((_,m)=>{if(token===generation)advance(m.mediaTime).catch(issue);});
        }else raf=requestAnimationFrame(()=>advance(v.currentTime).catch(issue));
      }
      v.onended=()=>stop(samples[samples.length-1].index).catch(issue);
      v.onerror=()=>{cleanup();issue(Error("The source review video could not be decoded."));};
      v.src="/take/"+encodeURIComponent(selection.take);
      await new Promise((resolve,reject)=>{
        if(v.readyState>=2)return resolve();
        v.addEventListener("loadeddata",resolve,{once:true});
        v.addEventListener("error",()=>reject(Error("Could not open source playback.")),{once:true});
      });
      if(stopped)return;
      v.currentTime=samples[0].start;
      paint(samples[0]);
      if(samples[0].state==="ambiguous"){await stop(samples[0].index);return;}
      await v.play();await advance(samples[0].start);
    }
    async function panel() {
      const detail=await ui().api("edit_info",{take:selection.take,source_index:draft.at});
      const keys=detail.keys.map(k=>{
        const label={keyframe:"Keyframe",reappears:"Reappears",hidden:"Hidden"}[k.state] || k.state;
        return '<button type="button" data-mask-frame="'+k.index+'">'+k.index+' · '+ui().esc(label)+'</button>';
      }).join(" ");
      ui().modal("Edit a source-frame range",
        '<p class="note">Choose inclusive source-frame numbers. Add a mask on the first frame, then add corrections wherever the subject changes or disappears.</p>'+
        '<div class="row">'+number("rangeStart","First frame",draft.start)+number("rangeEnd","Last frame",draft.end)+'</div>'+
        '<div class="row">'+number("rangeAt","Correction frame",draft.at)+
        '<label class="field">Correction<select id="rangeState"><option value="keyframe">Keyframe</option><option value="hidden">Hidden</option>'+
        '<option value="reappears">Reappears</option></select></label></div>'+
        '<div class="row"><button type="button" id="rangePaint">Set correction</button><button type="button" id="rangeImport">Import mask…</button>'+
        '<button type="button" id="rangeDelete">Delete correction</button><button type="button" id="rangeInspect">Inspect tracked mask</button>'+
        '<button type="button" id="rangePlay">Play tracked range</button></div>'+
        '<p class="note">'+(keys || "No correction frames saved.")+'</p>'+
        '<div class="row"><label class="field">Effect<select id="rangeAction"><option value="recolor">Recolor</option>'+
        '<option value="blur">Blur</option><option value="remove">Remove</option></select></label>'+
        '<label class="field">Tint<input type="color" id="rangeColor" value="'+ui().esc(draft.color)+'"></label></div>',
        [{label:"Render edited take",primary:true,run:async()=>{
          fields();await finish(await ui().api("edit_range",{take:selection.take,start_index:draft.start,end_index:draft.end,
                                            action:draft.action,color:draft.color}));
        }}]);
      by("rangeAction").value=draft.action;by("rangeState").value=draft.state;
      act("rangeInspect",async()=>{fields();await inspect();});
      act("rangePlay",async()=>{fields();await play(draft.start);});
      document.querySelectorAll("[data-mask-frame]").forEach(button=>{
        button.onclick=()=>{by("rangeAt").value=button.dataset.maskFrame;};
      });
      act("rangeDelete",async()=>{fields();await ui().api("edit_anchor",{take:selection.take,source_index:draft.at,state:"delete"});await panel();});
      act("rangeImport",async()=>{fields();if(draft.state==="hidden")throw Error("Choose Keyframe or Reappears for a mask.");
        const path=await ui().pick("file");if(path){await ui().api("edit_anchor",{take:selection.take,source_index:draft.at,state:draft.state,path});await panel();}});
      act("rangePaint",async()=>{
        fields();
        const command={take:selection.take,source_index:draft.at,state:draft.state};
        if(draft.state==="hidden"){await ui().api("edit_anchor",command);await panel();return;}
        const source=await ui().api("edit_info",{...command,preview:true});
        const old=source.keys.find(k=>k.index===draft.at)?.mask;
        await brush({title:"Frame "+draft.at+" · selection",asset:source.asset,old,initial:old?"current":"clear",
          back:panel,save:async payload=>{await ui().api("edit_anchor",{...command,...payload});await panel();}});
      });
    }
    await panel();
  }
  async function compare(args) {
    const review=await ui().api("compare_finish",args);
    by("player")?.pause();
    const clip=(id,title,side)=>'<section style="flex:1;min-width:0"><h3>'+title+'</h3>'+
      '<div style="position:relative"><video id="'+id+'" src="'+ui().esc(side.url)+'" playsinline preload="auto" style="width:100%;display:block"></video>'+
      '<canvas id="'+id+'Still" hidden style="position:absolute;inset:0;width:100%;height:100%"></canvas></div>'+
      '<output id="'+id+'Pos"></output></section>';
    ui().modal("Compare source and finish",
      '<div class="row">'+clip("compareLeft","Source",review.source)+clip("compareRight","Finished",review.finished)+'</div>'+
      '<label class="field">Shared playhead <output id="compareTime"></output><input id="compareScrub" type="range" min="0" max="'+review.duration+'" step="any" value="0"></label>'+
      '<div class="row"><button type="button" id="comparePlay">Play both</button><label class="field">Listen to<select id="compareAudio">'+
      '<option value="source">Source</option><option value="finished">Finished</option><option value="mute">Mute both</option></select></label></div>');
    const left=by("compareLeft"),right=by("compareRight"),slider=by("compareScrub"),toggle=by("comparePlay");
    let playing=false,raf=0,transport=0;
    const mediaRows=[[left,review.source],[right,review.finished]];
    function frameAt(side,time) {
      const runs=side.runs;
      let lo=0,hi=runs.length;
      while(lo+1<hi){const mid=(lo+hi)>>1;if(runs[mid][1]<=time+1e-12)lo=mid;else hi=mid;}
      const [index,start,interval,count]=runs[lo];
      const offset=Math.min(count-1,Math.max(0,Math.floor((time-start+1e-12)/interval)));
      return {index:index+offset,start:start+offset*interval,mid:start+(offset+.5)*interval};
    }
    function label(video,sample) {
      by(video.id+"Pos").textContent="Frame "+sample.index+" · "+sample.start.toFixed(4)+" s";
    }
    function clock(at) {slider.value=at;by("compareTime").textContent=at.toFixed(6)+" s";}
    function seek(video,time) {
      if(video.readyState>=2 && !video.seeking && Math.abs(video.currentTime-time)<1e-8)return Promise.resolve();
      return new Promise((resolve,reject)=>{
        const done=()=>{clearTimeout(timer);video.removeEventListener("seeked",done);resolve();};
        const timer=setTimeout(()=>{video.removeEventListener("seeked",done);reject(Error("Frame comparison seek timed out."));},8000);
        video.addEventListener("seeked",done);video.currentTime=time;
      });
    }
    async function pauseAt(seconds) {
      const id=++transport,at=Math.max(0,Math.min(seconds,review.duration));
      playing=false;left.pause();right.pause();right.playbackRate=1;
      cancelAnimationFrame(raf);toggle.textContent="Play both";clock(at);slider.dataset.settled="false";
      const frames=mediaRows.map(([video,side])=>({video,sample:frameAt(side,at),still:by(video.id+"Still")}));
      // Pausing Chromium can leave the compositor one sample ahead of currentTime. Decode
      // inside both intended sample intervals before capturing the two visible hold frames.
      await Promise.all(frames.map(({video,sample})=>seek(video,sample.mid)));
      await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
      if(id!==transport || !left.isConnected || !by("dialog").open)return;
      for(const {video,sample,still} of frames){
        const scale=Math.min(1,1920/Math.max(video.videoWidth,video.videoHeight));
        still.width=Math.max(1,Math.round(video.videoWidth*scale));still.height=Math.max(1,Math.round(video.videoHeight*scale));
        still.getContext("2d").drawImage(video,0,0,still.width,still.height);
        still.hidden=false;label(video,sample);
      }
      // Park both underlying transports at the same cut time for the next Play command.
      await Promise.all(frames.map(({video})=>seek(video,at)));
      if(id===transport)slider.dataset.settled="true";
    }
    function sync(){
      if(!playing || !left.isConnected)return;
      clock(left.currentTime);
      if(left.currentTime>=review.duration || left.ended || right.ended){pauseAt(review.duration).catch(issue);return;}
      const drift=left.currentTime-right.currentTime;
      if(!right.seeking && Math.abs(drift)>.048)right.currentTime=left.currentTime;
      else right.playbackRate=1+Math.max(-.05,Math.min(.05,drift*.9));
      raf=requestAnimationFrame(sync);
    }
    act("comparePlay",async()=>{
      if(playing){await pauseAt(left.currentTime);return;}
      const id=++transport,at=+slider.value>=review.duration-.00001?0:+slider.value;
      await Promise.all(mediaRows.map(([v])=>seek(v,at)));if(id!==transport)return;
      for(const [v] of mediaRows)by(v.id+"Still").hidden=true;
      playing=true;toggle.textContent="Pause both";
      try{await Promise.all([left.play(),right.play()]);sync();}catch(err){await pauseAt(at);throw err;}
    });
    slider.oninput=()=>pauseAt(Number(slider.value)).catch(issue);
    const sound=()=>{left.muted=by("compareAudio").value!=="source";right.muted=by("compareAudio").value!=="finished";};
    by("compareAudio").onchange=sound;sound();
    for(const [video,side] of mediaRows){
      label(video,frameAt(side,0));
      if(video.requestVideoFrameCallback){
        const tick=(_,m)=>{if(video.isConnected && by("dialog").open){if(playing)label(video,frameAt(side,m.mediaTime+.000001));video.requestVideoFrameCallback(tick);}};
        video.requestVideoFrameCallback(tick);
      }else video.ontimeupdate=()=>{if(playing)label(video,frameAt(side,video.currentTime));};
      video.onerror=()=>issue(Error("The comparison preview could not be decoded."));
    }
    by("dialog").addEventListener("close",()=>{transport++;playing=false;cancelAnimationFrame(raf);left.pause();right.pause();},{once:true});
  }
  window.editTools={paint,range,openFrame,oneFrame,compare};
})();
