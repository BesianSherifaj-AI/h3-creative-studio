(() => {
  'use strict';
  const id = key => document.getElementById(key), ui = () => window.H3;
  const image = url => { const item = new Image(); item.src=url; return item.decode().then(()=>item); };
  let active=false, tool='move', factor=1, panX=0, panY=0, view=null, pending=null, busy=false, drawId=0, resize;
  let preview, ctx, committed, layers=[], selection=null, textures=new Map(), generation=0, stage, strip;
  let combine='replace', radius=18, tolerance=24, redo=[];
  const icon = name => '<svg class="icon" aria-hidden="true"><use href="/icons.svg#'+name+'"></use></svg>';
  const label = (key,text,title='') => '<button type="button" data-ir="'+key+'" aria-label="'+ui().esc(title||text)+'" title="'+ui().esc(title||text)+'">'+text+'</button>';
  const selected = () => ui().get().rev;
  const savedSelection = () => ui().get().doc?.image_selections?.[selected()?.id] || null;
  const call = (op,fields={}) => ui().api('image_room_'+op,{revision:selected()?.id,...fields});
  const protect = fn => async (...args) => {try {await fn(...args);}catch(e){if(id('dialog').open)id('dialogError').textContent=e.message;else ui().toast(e.message);}};
  const setTool = name => {tool=name; pending=null; updateTools(); schedule();};
  const fit = () => {factor=1;panX=0;panY=0;updateTools();schedule();};
  function geometry(){
    const rev=selected(),w=preview.clientWidth,h=preview.clientHeight;
    if(!rev || !w || !h)return {sx:0,sy:0,s:1};
    const s=Math.max(.001,Math.min((w-24)/rev.width,(h-24)/rev.height))*factor;
    return {sx:(w-rev.width*s)/2+panX,sy:(h-rev.height*s)/2+panY,s};
  }
  function zoom(value,anchor){
    if(!selected())return;
    const before=geometry(), x=anchor?.x??preview.clientWidth/2, y=anchor?.y??preview.clientHeight/2;
    factor=Math.max(.1,Math.min(32,value));const after=geometry();
    panX+=x-(after.sx+(x-before.sx)/before.s*after.s);
    panY+=y-(after.sy+(y-before.sy)/before.s*after.s);
    updateTools();schedule();
  }
  function local(e){
    const rect=preview.getBoundingClientRect();
    return {x:e.clientX-rect.left,y:e.clientY-rect.top};
  }
  function source(e){
    const g=geometry(),p=local(e);
    return {x:(p.x-g.sx)/g.s,y:(p.y-g.sy)/g.s};
  }
  const pixel = p => [Math.min(selected().width-1,Math.max(0,Math.floor(p.x))),Math.min(selected().height-1,Math.max(0,Math.floor(p.y)))];
  function schedule(){if(!drawId)drawId=requestAnimationFrame(()=>{drawId=0;draw();});}

  async function texture(layer){
    const key=[layer.asset,layer.mask||'',layer.color||''].join('/');
    if(textures.has(key))return textures.get(key);
    const work=(async()=>{
      const [src,mask]=await Promise.all([image('/asset/'+layer.asset),layer.mask?image('/asset/'+layer.mask):null]);
      const ratio=Math.min(1,1024/Math.max(src.naturalWidth,src.naturalHeight)),c=document.createElement('canvas');
      c.width=Math.max(1,Math.round(src.naturalWidth*ratio));c.height=Math.max(1,Math.round(src.naturalHeight*ratio));
      const g=c.getContext('2d',{willReadFrequently:true});g.drawImage(src,0,0,c.width,c.height);
      if(mask){
        const m=document.createElement('canvas');m.width=c.width;m.height=c.height;
        const mc=m.getContext('2d',{willReadFrequently:true});mc.drawImage(mask,0,0,m.width,m.height);
        const mv=mc.getImageData(0,0,m.width,m.height).data,px=g.getImageData(0,0,c.width,c.height);
        for(let i=3;i<px.data.length;i+=4)px.data[i]=Math.round(px.data[i]*mv[i-3]/255);
        g.putImageData(px,0,0);
      }
      if(layer.color){g.globalCompositeOperation='source-in';g.fillStyle=layer.color;g.fillRect(0,0,c.width,c.height);}
      return {c,g,w:src.naturalWidth,h:src.naturalHeight};
    })();
    textures.set(key,work);
    if(textures.size>12)textures.delete(textures.keys().next().value);
    try{return await work;}catch(e){textures.delete(key);throw e;}
  }
  function matrix(layer,t,offset={x:0,y:0}){
    const s=layer.scale??1, w=t.w*s,h=t.h*s;
    return new DOMMatrix().translate((layer.x||0)+offset.x+w/2,(layer.y||0)+offset.y+h/2)
      .rotate(layer.rotation||0).scale(layer.flip_x?-s:s,layer.flip_y?-s:s).translate(-t.w/2,-t.h/2);
  }
  function hit(p){
    const rev=selected();
    if(!rev||p.x<0||p.y<0||p.x>=rev.width||p.y>=rev.height)return -1;
    for(let i=rev.layers.length-1;i>=0;i--){
      const layer=rev.layers[i],t=layers[i];if(!t||layer.visible===false||layer.opacity===0)continue;
      const q=new DOMPoint(p.x,p.y).matrixTransform(matrix(layer,t).inverse());
      if(q.x>=0&&q.y>=0&&q.x<t.w&&q.y<t.h&&t.g.getImageData(Math.floor(q.x*t.c.width/t.w),Math.floor(q.y*t.c.height/t.h),1,1).data[3]>12)return i;
    }
    return -1;
  }
  function path(points,kind){
    if(!points.length)return;
    ctx.beginPath();const [x,y]=points[0];ctx.moveTo(x,y);
    if(kind==='rectangle'){const last=points.at(-1);ctx.rect(Math.min(x,last[0]),Math.min(y,last[1]),Math.abs(x-last[0])+1,Math.abs(y-last[1])+1);}
    else {for(const [px,py] of points.slice(1))ctx.lineTo(px,py);if(kind==='lasso')ctx.closePath();}
  }
  function draw(){
    if(!active||!preview)return;
    const rev=selected(),w=preview.clientWidth,h=preview.clientHeight,dpr=Math.min(2,window.devicePixelRatio||1);
    if(!w||!h)return;
    if(preview.width!==Math.round(w*dpr)||preview.height!==Math.round(h*dpr)){preview.width=Math.round(w*dpr);preview.height=Math.round(h*dpr);}
    ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
    if(!rev||view!==rev.id)return;
    const {sx,sy,s}=geometry();ctx.save();ctx.translate(sx,sy);ctx.scale(s,s);
    ctx.save();ctx.beginPath();ctx.rect(0,0,rev.width,rev.height);ctx.clip();
    if(pending?.type==='move' && layers.length===rev.layers.length){
      rev.layers.forEach((l,i)=>{
        const t=layers[i];if(!t||l.visible===false)return;
        ctx.save();ctx.globalAlpha=l.opacity??1;ctx.globalCompositeOperation=l.blend==='normal'||!l.blend?'source-over':l.blend;
        const m=matrix(l,t,pending.i===i?pending.delta:undefined);ctx.transform(m.a,m.b,m.c,m.d,m.e,m.f);
        ctx.drawImage(t.c,0,0,t.w,t.h);ctx.restore();
      });
    }else if(committed)ctx.drawImage(committed,0,0,rev.width,rev.height);
    if(selection && (!pending||pending.type==='select'&&combine!=='replace')){ctx.globalAlpha=.38;ctx.drawImage(selection,0,0,rev.width,rev.height);ctx.globalAlpha=1;}
    if(pending?.type==='select'){
      path(pending.points,tool);ctx.lineWidth=tool==='brush'?2*radius+1:1.5/s;
      ctx.fillStyle='rgba(60,175,233,.3)';ctx.strokeStyle=tool==='brush'?'rgba(90,203,247,.5)':'#87d9ff';
      ctx.lineCap='round';ctx.lineJoin='round';
      if(tool!=='brush')ctx.fill();else if(pending.points.length===1){const [x,y]=pending.points[0];ctx.beginPath();ctx.arc(x,y,radius,0,Math.PI*2);ctx.fill();}
      ctx.stroke();
    }
    ctx.restore();
    const i=ui().get().layerIndex,l=rev.layers[i],t=layers[i];
    if(tool==='move'&&l&&t&&l.visible!==false){
      const m=matrix(l,t,pending?.type==='move'&&pending.i===i?pending.delta:undefined);
      const corners=[[0,0],[t.w,0],[t.w,t.h],[0,t.h]].map(([x,y])=>new DOMPoint(x,y).matrixTransform(m));
      ctx.beginPath();corners.forEach((q,k)=>k?ctx.lineTo(q.x,q.y):ctx.moveTo(q.x,q.y));ctx.closePath();ctx.strokeStyle='#91d0f7';ctx.lineWidth=1/s;ctx.stroke();
      for(const q of corners){ctx.fillStyle='#172531';ctx.fillRect(q.x-2.5/s,q.y-2.5/s,5/s,5/s);ctx.strokeRect(q.x-2.5/s,q.y-2.5/s,5/s,5/s);}
    }
    ctx.restore();
  }
  async function load(){
    const rev=selected(),tag=++generation;
    preview.dataset.ready='false';
    if(!rev){view=null;committed=null;layers=[];selection=null;schedule();return;}
    const changed=rev.id!==view;
    if(changed){
      const old=view&&ui().get().doc.revisions[view];if(!old||old.composition!==rev.composition||old.width!==rev.width||old.height!==rev.height)fit();
      view=rev.id;committed=null;layers=[];pending=null;selection=null;
    }
    const item=await image('/revision/'+rev.id);
    if(tag!==generation||!active)return;
    committed=item;schedule();
    const ready=[];for(const l of rev.layers){ready.push(await texture(l));if(tag!==generation||!active)return;}
    layers=ready;preview.dataset.ready='true';schedule();
    const s=savedSelection();if(!s){selection=null;schedule();return;}
    const mask=await image('/asset/'+s.asset);
    if(tag!==generation||!active)return;
    const k=Math.min(1,1024/Math.max(mask.naturalWidth,mask.naturalHeight)),c=document.createElement('canvas');
    c.width=Math.max(1,Math.round(mask.naturalWidth*k));c.height=Math.max(1,Math.round(mask.naturalHeight*k));
    const g=c.getContext('2d',{willReadFrequently:true});g.drawImage(mask,0,0,c.width,c.height);const px=g.getImageData(0,0,c.width,c.height);
    for(let i=0;i<px.data.length;i+=4){px.data[i+3]=px.data[i];px.data[i]=82;px.data[i+1]=192;px.data[i+2]=250;}
    g.putImageData(px,0,0);selection=c;schedule();
  }
  function updateTools(){
    if(!strip||!active)return;
    const rev=selected(),sel=savedSelection(),g=geometry();
    strip.querySelectorAll('[data-ir-tool]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.irTool===tool)));
    strip.querySelectorAll('[data-needs-revision]').forEach(e=>e.disabled=!rev);
    id('irSelectMode').value=['rectangle','lasso','brush','wand'].includes(tool)?tool:'';
    const selecting=['rectangle','lasso','brush','wand'].includes(tool);
    id('irSelectContext').hidden=!rev||(!selecting&&!sel);id('irCombineLabel').hidden=!selecting;
    id('irRadiusLabel').hidden=tool!=='brush';id('irToleranceLabel').hidden=tool!=='wand';
    id('irBounds').textContent=sel?(sel.bounds[2]-sel.bounds[0])+' × '+(sel.bounds[3]-sel.bounds[1])+' px':'';
    id('irClear').disabled=!sel;
    id('irZoom').textContent=rev?Math.round(g.s*100)+'%':'Fit';
    preview.dataset.tool=tool;preview.hidden=!rev;
  }
  let stamp='';
  function refresh(){
    if(!active)return;
    updateTools();const r=selected(),key=(r?.id||'')+':'+(savedSelection()?.asset||'');
    if(stamp!==key){stamp=key;protect(load)();}else schedule();
  }
  async function saveRevision(op,fields={}){
    const res=await call(op,fields);if(res.revision){redo=[];await ui().setRevision(res.revision);refresh();}
  }
  async function history(forward){
    const {rev,comp,doc}=ui().get();if(!rev)return;
    const candidate=redo.at(-1);
    const id=forward?(doc.revisions[candidate]?.parent===rev.id?candidate:comp.revisions.find(k=>doc.revisions[k].parent===rev.id)):rev.parent;
    if(!id||!comp.revisions.includes(id)){ui().toast(forward?'No later revision.':'This is the first revision.');return;}
    await ui().api('composition',{composition:comp.id,revision:id});if(forward)redo.pop();else redo.push(rev.id);
    await ui().refresh();refresh();
  }
  async function nudge(dx,dy,index=ui().get().layerIndex,revision=selected()?.id){
    const r=selected(),l=r?.layers[index];if(!l||r.id!==revision||(!dx&&!dy))return;
    const v=await ui().api('layer',{revision,index,x:(l.x||0)+dx,y:(l.y||0)+dy});
    redo=[];await ui().setRevision(v.revision);refresh();
  }
  const adjust=()=>{
    const {rev,layerIndex}=ui().get(),name=rev.layers[layerIndex]?.name||'Layer';
    const rows=['brightness','contrast','saturation','sharpness'].map(k=>'<label class="ir-adjust">'+k+'<input type="range" min="0" max="300" value="100" data-factor="'+k+'"><output>100%</output></label>').join('');
    ui().modal('Adjust '+name,rows,[{label:'Apply to layer',primary:true,run:async()=>{
      const fields={index:layerIndex};id('dialogBody').querySelectorAll('[data-factor]').forEach(e=>fields[e.dataset.factor]=Number(e.value)/100);
      await saveRevision('adjust',fields);ui().closeModal();
    }}]);
    id('dialogBody').querySelectorAll('[data-factor]').forEach(e=>e.oninput=()=>{e.nextElementSibling.value=e.value+'%';});
  };
  function requiredSelection(){const s=savedSelection();if(!s)throw Error('Draw a rectangle, lasso or brush selection first.');return s.asset;}
  function ai(task,suggested='',visualNote=null){
    if(!selected()){imageDialog();return;}
    const selection=savedSelection()?.asset;
    if(task==='remove')requiredSelection();
    const models=task==='cutout'?[['qwen21','Qwen Image 2.1']]:[['h3','MiniMax H3 Image'],['qwen21','Qwen Image 2.1'],['zimage','Z-Image Turbo']];
    const titles={edit:'AI image edit',remove:'Remove object',cutout:'AI cutout',recolor:'Recolor selection'};
    ui().modal(titles[task],sel('Image model','irModel',models,models[0][0])+area('Instruction','irPrompt',suggested||(task==='remove'?'Remove the selected object and match the surrounding surface.':task==='cutout'?'Keep the main subject and remove the background.':''))+
      fld('Seed','irSeed',0,'number','min="0"')+(task==='remove'?sel('Repair','irSurface',[['auto','Match surrounding surface'],['texture','Keep generated texture']],'auto'):'')+
      '<p class="note">'+(task==='cutout'?'Qwen creates a full-canvas cutout with transparency.':selection?'The saved canvas selection defines the editable area.':'This edit applies to the whole composition.')+'</p>',[{label:'Queue image',primary:true,run:async()=>{
        await call('ai',{model:val('irModel'),task,prompt:val('irPrompt'),seed:+val('irSeed'),surface:id('irSurface')?.value||'auto',selection:selection||null,visual_note:visualNote});await showQueue();
      }}]);
  }
  function guide(){
    const {doc,rev,shot}=ui().get(),choices=doc.cut.map(k=>[k,doc.shots[k].name]);choices.push(['','New 5-second shot']);
    ui().modal('Use image as video guide',sel('Shot','irShot',choices,shot?.id||'')+sel('Delivered position','irSlot',[['first','First frame'],['middle','Middle frame'],['last','Last frame']],'first')+'<p class="note" id="irGuideSummary"></p>',[{label:'Place guide',primary:true,run:async()=>{
      const r=await call('guide',{shot:val('irShot'),slot:val('irSlot')});state.shotId=r.shot;state.frame=r.frame;await ui().refresh();ui().closeModal();ui().toast('Placed '+rev.name+' at frame '+r.frame+'.');
    }}]);
    function summary(){const s=doc.shots[val('irShot')],last=Math.round((s?.duration||5)*24)-1,n={first:0,middle:Math.round(last/2),last}[val('irSlot')];
      const replacement=s?.guides.some(g=>g.frame===n&&['image','first','middle','last','first_frame','last_frame'].includes(g.role||'image'));
      id('irGuideSummary').textContent='Frame '+n+' · '+rev.name+(replacement?' · Replaces the image guide at this frame.':' · Saves this exact revision.');}
    id('irShot').onchange=id('irSlot').onchange=summary;summary();
  }
  async function editMenu(value){
    if(value==='adjust'){adjust();return;}
    if(value==='resize'){
      const rev=selected();
      ui().modal('Resize a copy', '<div class="row">'+fld('Width (px)','irWidth',rev.width,'number','min="1" max="8192"')+
        fld('Height (px)','irHeight',rev.height,'number','min="1" max="8192"')+'</div><label><input type="checkbox" id="irRatio" checked> Keep aspect ratio</label><p class="note">Creates a flattened composition and keeps the original layers in the library.</p>',
        [{label:'Create resized copy',primary:true,run:async()=>{await saveRevision('resize',{width:+val('irWidth'),height:+val('irHeight')});ui().closeModal();}}]);
      for(const [key,other,ratio] of [['irWidth','irHeight',rev.height/rev.width],['irHeight','irWidth',rev.width/rev.height]]){
        id(key).oninput=()=>{if(id('irRatio').checked && +val(key)>0)id(other).value=Math.max(1,Math.round(+val(key)*ratio));};
      }return;
    }
    if(value==='mask'){await window.editTools.paint();return;}
    if(value==='crop'||value==='cutout'){await saveRevision(value,{selection:requiredSelection()});return;}
    const {layerIndex,rev}=ui().get();
    if(value==='delete'){
      ui().modal('Delete layer?', '<p>'+ui().esc(rev.layers[layerIndex].name||'Selected layer')+'</p>',
        [{label:'Delete layer',primary:true,run:async()=>{await saveRevision('delete',{index:layerIndex});ui().closeModal();}}]);return;
    }
    await saveRevision(value.startsWith('flip')?'flip':value,{index:layerIndex,axis:value==='flipX'?'x':'y'});
    if(value==='duplicate'){state.layerIndex=layerIndex+1;inspector();refresh();}
  }
  function mount(){
    stage=id('stage');strip=id('imageRoomTools');if(!stage||!strip)return false;
    if(preview)return true;
    preview=document.createElement('canvas');preview.id='imageRoomCanvas';preview.tabIndex=0;preview.setAttribute('aria-label','Image canvas');stage.append(preview);ctx=preview.getContext('2d');
    strip.innerHTML='<div class="ir-bar" role="toolbar" aria-label="Image tools"><div class="ir-group"><button data-ir-tool="move" data-needs-revision title="Move a visible layer (V). Alt-drag moves the layer selected in the inspector.">↖ Move</button><button data-ir-tool="pan" data-needs-revision title="Pan (H or hold Space)">Pan</button><select id="irSelectMode" data-needs-revision aria-label="Selection tool"><option value="">Select</option><option value="rectangle">Rectangle</option><option value="lasso">Lasso</option><option value="brush">Brush</option><option value="wand">Color wand</option></select></div>'+
    '<div class="ir-group ir-zoom">'+label('minus','−','Zoom out')+label('fit','<span id="irZoom">Fit</span>','Fit image (0)')+label('plus','＋','Zoom in')+'</div>'+
    '<div class="ir-group"><select id="irEdit" data-needs-revision aria-label="Image operations"><option value="">Edit</option><option value="adjust">Adjust light and color</option><option value="crop">Crop canvas to selection</option><option value="resize">Resize a copy</option><option value="cutout">Extract transparent cutout</option><option value="mask">Paint layer mask</option><option value="duplicate">Duplicate layer</option><option value="delete">Delete layer</option><option value="flipX">Flip layer horizontally</option><option value="flipY">Flip layer vertically</option></select>'+
    '<select id="irAI" aria-label="AI image tools"><option value="">✦ AI tools</option><option value="create">Generate image</option><option value="edit">Edit image</option><option value="remove">Remove object</option><option value="recolor">Recolor selection</option><option value="cutout">AI cutout</option><option value="segment">Select an object · SAM</option><option value="upscale">Enhance · SeedVR2</option><option value="visual">Visual Director · plan or review</option><option value="points">Select with points · SAM</option><option value="advanced">Outpaint, ControlNet and layers…</option></select></div>'+
    '<div class="ir-group">'+label('guide',icon('frame')+'Video guide','Place the saved image in a shot')+label('export',icon('download')+'Export','Export PNG, JPEG or WebP')+'</div><div class="ir-group ir-history">'+label('undo',icon('undo-2'),'Undo revision (Ctrl+Z)')+label('redo',icon('redo-2'),'Redo revision (Ctrl+Shift+Z)')+'</div></div>'+
    '<div id="irSelectContext" class="ir-context" hidden><label id="irCombineLabel">Selection <select id="irCombine"><option value="replace">Replace</option><option value="add">Add</option><option value="subtract">Subtract</option></select></label><label id="irRadiusLabel">Radius <input id="irRadius" type="number" value="18" min="1" max="512"> px</label><label id="irToleranceLabel">Color tolerance <input id="irTolerance" type="number" value="24" min="0" max="255"></label><span id="irBounds"></span><button id="irClear">Clear selection</button></div>';
    const commands={minus:()=>zoom(factor/1.25),plus:()=>zoom(factor*1.25),fit,guide,undo:()=>history(false),redo:()=>history(true),export:async()=>{
      if(!selected())throw Error('Choose a composition first.');const path=await ui().pick('save',selected().name+'.png');if(!path)return;
      const result=await call('export',{path});ui().toast('Saved '+result.path);
    }};
    for(const b of strip.querySelectorAll('[data-ir]')){
      if(!['minus','fit','plus'].includes(b.dataset.ir))b.setAttribute('data-needs-revision','');
      b.onclick=protect(commands[b.dataset.ir]);
    }
    strip.querySelectorAll('[data-ir-tool]').forEach(b=>b.onclick=()=>setTool(b.dataset.irTool));
    id('irSelectMode').onchange=e=>setTool(e.target.value||'move');id('irCombine').onchange=e=>combine=e.target.value;
    id('irRadius').oninput=e=>radius=Math.max(1,Math.min(512,Math.round(+e.target.value)||1));
    id('irTolerance').oninput=e=>tolerance=Math.max(0,Math.min(255,Math.round(+e.target.value)||0));
    id('irClear').onclick=protect(async()=>{await call('clear',{previous:savedSelection()?.asset});await ui().refresh();refresh();});
    id('irEdit').onchange=protect(async e=>{const v=e.target.value;e.target.value='';if(v)await editMenu(v);});
    id('irAI').onchange=protect(async e=>{const v=e.target.value;e.target.value='';if(!v)return;if(v==='create'){imageDialog();id('itask').value='create';id('itask').dispatchEvent(new Event('change'));return;}if(!selected())throw Error('Import a still or generate an image first.');if(v==='advanced'){imageDialog();return;}if(v==='visual'){window.ImageDirector.open();return;}if(v==='points'){window.ImageDirector.points();return;}
      if(v==='segment'||v==='upscale'){
        const segment=v==='segment',rev=selected().id;
        const fields=segment?area('Object description','irObject',''):sel('Enlargement','irScale',[['2','2×'],['3','3×'],['4','4×']],'2');
        ui().modal(segment?'Select an object with SAM 3.1':'Enhance with SeedVR2',fields,[{label:segment?'Queue selection':'Queue enhancement',primary:true,run:async()=>{
          await ui().api('image_ai',{revision:rev,model:segment?'sam':'seedvr2',task:segment?'select':'upscale',prompt:segment?val('irObject'):'',scale:segment?undefined:Number(val('irScale'))});
          await showQueue();
        }}]);return;
      }ai(v);});
    preview.onpointerdown=down;preview.onpointermove=move;preview.onpointerup=protect(up);
    preview.onpointercancel=()=>{pending=null;schedule();};
    preview.addEventListener('wheel',e=>{if(!active)return;e.preventDefault();zoom(factor*Math.exp(-e.deltaY*.0018),local(e));},{passive:false});
    resize=new ResizeObserver(()=>{if(active){updateTools();schedule();}});resize.observe(stage);
    return true;
  }
  let space=false;
  function down(e){
    if(!active||busy||!selected()||pending||![0,1].includes(e.button))return;
    preview.focus({preventScroll:true});preview.setPointerCapture(e.pointerId);const p=source(e),loc=local(e);
    if(tool==='pan'||space||e.button===1){pending={type:'pan',start:loc,px:panX,py:panY};}
    else if(tool==='move'){
      const i=e.altKey?ui().get().layerIndex:hit(p);if(i<0||!selected().layers[i])return;
      state.layerIndex=i;inspector();pending={type:'move',start:p,i,rev:selected().id,delta:{x:0,y:0}};}
    else if(p.x>=0&&p.y>=0&&p.x<selected().width&&p.y<selected().height){pending={type:'select',points:[pixel(p)],rev:selected().id};}
    e.preventDefault();schedule();
  }
  function move(e){
    if(!pending)return;
    if(pending.type==='pan'){const p=local(e);panX=pending.px+p.x-pending.start.x;panY=pending.py+p.y-pending.start.y;}
    else if(pending.type==='move'){const p=source(e);pending.delta={x:Math.round(p.x-pending.start.x),y:Math.round(p.y-pending.start.y)};}
    else {const pair=pixel(source(e)),last=pending.points.at(-1);if(tool==='rectangle')pending.points[1]=pair;else if(tool!=='wand'&&pending.points.length<16000&&(pair[0]!==last[0]||pair[1]!==last[1]))pending.points.push(pair);}
    schedule();
  }
  async function up(e){
    if(!pending)return;move(e);const activeDrag=pending;pending=null;busy=true;
    try{
      if(activeDrag.type==='move')await nudge(activeDrag.delta.x,activeDrag.delta.y,activeDrag.i,activeDrag.rev);
      else if(activeDrag.type==='select'&&activeDrag.rev===selected()?.id){
        await call('selection',{shape:tool,points:activeDrag.points,radius,tolerance,combine,previous:savedSelection()?.asset||null});await ui().refresh();refresh();
      }
    }finally{busy=false;schedule();}
  }
  function key(e){
    if(!active||!selected()||id('dialog').open||e.target.closest('input,textarea,select,[contenteditable=true]')||busy)return;
    if(e.key===' '){space=true;e.preventDefault();return;}
    const k=e.key.toLowerCase();let fn;
    if((e.ctrlKey||e.metaKey)&&k==='z')fn=()=>history(e.shiftKey);
    else if((e.ctrlKey||e.metaKey)&&k==='y')fn=()=>history(true);
    else if(e.ctrlKey||e.metaKey||e.altKey)return;
    else if(['v','h','r','l','b','w'].includes(k))fn=()=>setTool(({v:'move',h:'pan',r:'rectangle',l:'lasso',b:'brush',w:'wand'})[k]);
    else if(k==='0')fn=fit;
    else if(k==='+'||k==='=')fn=()=>zoom(factor*1.25);
    else if(k==='-')fn=()=>zoom(factor/1.25);
    else if(k==='escape')fn=async()=>{if(!pending && savedSelection()){await call('clear',{previous:savedSelection().asset});await ui().refresh();}pending=null;setTool('move');refresh();};
    else if(k.startsWith('arrow')&&tool==='move'){const n=e.shiftKey?10:1;fn=()=>nudge(({arrowleft:-n,arrowright:n})[k]||0,({arrowup:-n,arrowdown:n})[k]||0);}
    if(fn){e.preventDefault();busy=true;protect(fn)().finally(()=>{busy=false;});}
  }
  document.addEventListener('keydown',key);document.addEventListener('keyup',e=>{if(e.key===' ')space=false;});window.addEventListener('blur',()=>{space=false;pending=null;schedule();});
  window.ImageRoom={
    open(){if(!mount())return;active=true;strip.hidden=false;stage.classList.add('ir-active');refresh();},
    close(){active=false;pending=null;space=false;generation++;if(strip)strip.hidden=true;if(stage)stage.classList.remove('ir-active');if(preview)preview.hidden=true;layers=[];textures.clear();committed=null;selection=null;stamp='';},
    refresh,ai
  };
})();
