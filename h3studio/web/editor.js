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
    const scale = Math.min(1, 820/w, 400/h);
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

  function selected() {
    const {take,frame}=ui().get();
    if(!take?.video_asset)throw Error("Choose a rendered or imported take.");
    return {take:take.id,seconds:frame/24};
  }
  async function openFrame() {
    const r=await ui().api("frame",selected());
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
    const selection=selected();
    const info=await ui().api("edit_info",selection);
    const draft={start:info.source_index,end:Math.min(info.source_end,info.source_index+23),
                 at:info.source_index,action:"recolor",color:"#657dff",state:"keyframe"};
    const number=(id,label,value)=>'<label class="field">'+label+'<input id="'+id+'" type="number" step="1" min="'+info.source_start+
      '" max="'+info.source_end+'" value="'+value+'"></label>';
    const fields=()=>{draft.start=Number(by("rangeStart").value);draft.end=Number(by("rangeEnd").value);
      draft.at=Number(by("rangeAt").value);draft.action=by("rangeAction").value;draft.color=by("rangeColor").value;
      draft.state=by("rangeState").value;};
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
        '<button type="button" id="rangeDelete">Delete correction</button></div>'+
        '<p class="note">'+(keys || "No correction frames saved.")+'</p>'+
        '<div class="row"><label class="field">Effect<select id="rangeAction"><option value="recolor">Recolor</option>'+
        '<option value="blur">Blur</option><option value="remove">Remove</option></select></label>'+
        '<label class="field">Tint<input type="color" id="rangeColor" value="'+ui().esc(draft.color)+'"></label></div>',
        [{label:"Render edited take",primary:true,run:async()=>{
          fields();await finish(await ui().api("edit_range",{take:selection.take,start_index:draft.start,end_index:draft.end,
                                            action:draft.action,color:draft.color}));
        }}]);
      by("rangeAction").value=draft.action;by("rangeState").value=draft.state;
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
  window.editTools={paint,range,openFrame,oneFrame};
})();
