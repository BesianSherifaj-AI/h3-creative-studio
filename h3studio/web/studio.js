'use strict';
const $ = s => document.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {doc:null, compId:null, shotId:null, pane:'layers', view:'canvas', frame:0, layerIndex:0, preferences:{}, jobs:{}, plan:null};
const get = () => {
  const doc=state.doc, comp=doc?.compositions[state.compId], rev=doc?.revisions[comp?.current], shot=doc?.shots[state.shotId], take=doc?.takes[shot?.selected_take];
  return {doc,comp,rev,shot,take,layerIndex:state.layerIndex,frame:state.frame};
};
async function api(op,args={}) {
  const res=await fetch('/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...args,op})});
  const body=await res.json();
  if (!res.ok) throw Error(body.error||'Desktop session unavailable.');
  return body;
}
let notification;
function toast(message){clearTimeout(notification);$('#toast').textContent=message;$('#toast').hidden=false;notification=setTimeout(()=>$('#toast').hidden=true,4800);}
function handle(fn){return async e=>{try{await fn(e);}catch(err){if($('#dialog').open)$('#dialogError').textContent=err.message;else toast(err.message);}};}
const on=(sel,fn)=>{const el=$(sel);if(el)el.onclick=handle(fn);};
function closeModal(){if($('#dialog').open)$('#dialog').close();}
function modal(title,body,actions=[]){
  $('#dialogTitle').textContent=title;$('#dialogBody').innerHTML=body;$('#dialogError').textContent='';$('#dialogFoot').replaceChildren();
  for(const item of actions){const b=document.createElement('button');b.type='button';b.textContent=item.label;if(item.primary)b.className='primary';
    b.onclick=handle(async()=>{b.disabled=true;try{await item.run();}finally{b.disabled=false;}});$('#dialogFoot').append(b);}
  if(!$('#dialog').open)$('#dialog').showModal();
}
$('#dialogForm').onsubmit=e=>e.preventDefault();on('#closeDialog',closeModal);
async function pick(kind,filename=''){
  if(window.pywebview?.api?.choose)return await window.pywebview.api.choose(kind,filename);
  return window.prompt(kind==='folder'?'Enter the project folder path':kind==='save'?'Enter the full output path':'Enter the full file path',filename)||'';
}
const fld=(label,id,val='',type='text',extra='')=>'<label class="field">'+esc(label)+'<input id="'+id+'" type="'+type+'" value="'+esc(val)+'" '+extra+'></label>';
const sel=(label,id,opts,value)=>'<label class="field">'+esc(label)+'<select id="'+id+'">'+opts.map(o=>{const [v,l]=Array.isArray(o)?o:[o,o];return '<option value="'+esc(v)+'" '+(String(v)===String(value)?'selected':'')+'>'+esc(l)+'</option>';}).join('')+'</select></label>';
const area=(label,id,val='')=>'<label class="field">'+esc(label)+'<textarea id="'+id+'">'+esc(val)+'</textarea></label>';
const btn=(id,label,css='')=>'<button type="button" id="'+id+'" class="'+css+'">'+esc(label)+'</button>';
const val=id=>$('#'+id).value;
const shownSeconds=()=>{const {take,shot}=get(),r=take?.settings.delivery_trim;return state.view==='take'&&r?r[1]-r[0]:shot?.duration||5;};
const selectedFrame=()=>Math.max(0,Math.round(shownSeconds()*24)-1);
const revUrl=id=>'/revision/'+encodeURIComponent(id);
const assetUrl=id=>'/asset/'+encodeURIComponent(id);
async function refresh(){
  const s=await api('state'),old=state.doc?.id,oldIndex=state.doc?.cut.indexOf(state.shotId)??-1;
  state.doc=s.project;state.folder=s.folder;state.preferences=s.preferences;
  if(!old&&s.notice)toast(s.notice);
  if(old!==s.project.id){state.compId=null;state.shotId=null;state.frame=0;state.plan=null;state.view='canvas';}
  if(!s.project.compositions[state.compId])state.compId=Object.keys(s.project.compositions)[0];
  if(!s.project.shots[state.shotId])state.shotId=s.project.cut[0];
  else if(old===s.project.id && oldIndex>=0 && !s.project.cut.includes(state.shotId)){
    state.shotId=s.project.cut[Math.min(oldIndex,s.project.cut.length-1)];
    state.frame=0;state.plan=null;state.view=get().take?'take':'canvas';
  }
  state.frame=Math.min(state.frame,selectedFrame());state.layerIndex=Math.min(Math.max(state.layerIndex,0),Math.max(0,(get().rev?.layers.length||1)-1));
  render();
}
async function setRevision(id){await refresh();const r=state.doc.revisions[id];if(r){state.compId=r.composition;state.view='canvas';render();}}
function time(f){const secs=Math.floor(f/24);return String(Math.floor(secs/60)).padStart(2,'0')+':'+String(secs%60).padStart(2,'0')+'.'+String(f%24).padStart(2,'0');}
function render(){
  const {doc,comp,rev,shot,take}=get();
  $('#projectMenu').textContent=doc.name+' ▾';$('#libraryName').textContent=doc.name;$('#projectStatus').textContent='Saved locally';
  $('#comps').innerHTML=Object.values(doc.compositions).map(c=>'<button class="lib-item '+(c.id===state.compId?'active':'')+'" data-comp="'+c.id+'"><img src="'+revUrl(c.current)+'" alt=""><span><strong>'+esc(c.name)+'</strong><small>'+doc.revisions[c.current].layers.length+' layers</small></span></button>').join('')||'<p class="note">Import a still to create a composition.</p>';
  $('#shots').innerHTML=doc.cut.map((id,i)=>{const s=doc.shots[id];return '<button class="lib-item '+(id===state.shotId?'active':'')+'" data-shot="'+id+'"><span class="thumb-icon">'+String(i+1).padStart(2,'0')+'</span><span><strong>'+esc(s.name)+'</strong><small>'+Number(s.duration).toFixed(2)+' s'+(s.needs_review?' · review ending':'')+'</small></span></button>';}).join('');
  document.querySelectorAll('[data-comp]').forEach(b=>b.onclick=()=>{state.compId=b.dataset.comp;state.layerIndex=get().rev.layers.length-1;state.view='canvas';render();});
  document.querySelectorAll('[data-shot]').forEach(b=>b.onclick=()=>{state.shotId=b.dataset.shot;state.frame=0;state.plan=null;state.view=get().take?'take':'canvas';render();});
  $('#compName').textContent=(state.view==='canvas'?comp?.name:shot?.name)||'Canvas';$('#workType').textContent=state.view==='canvas'?'COMPOSITION':'SAVED TAKE';
  const img=$('#canvasImage'),vid=$('#player');img.hidden=state.view!=='canvas'||!rev;vid.hidden=state.view!=='take'||!take?.video_asset;
  if(!img.hidden&&img.getAttribute('src')!==revUrl(rev.id))img.src=revUrl(rev.id);
  if(!vid.hidden){const src='/take/'+encodeURIComponent(take.id);if(vid.getAttribute('src')!==src){presentedFrame=null;vid.src=src;}}
  if(vid.hidden&&!vid.paused)vid.pause();
  $('#emptyArt').hidden=!img.hidden||!vid.hidden;$('#emptyArt').textContent=state.view==='take'?'Render a storyboard or generate a take to play it here.':'Import a still to open it on the canvas.';
  $('#canvasTab').classList.toggle('active',state.view==='canvas');$('#takeTab').classList.toggle('active',state.view==='take');
  $('#canvasMeta').textContent=state.view==='canvas'&&rev?rev.width+' × '+rev.height+'  /  RGBA':take?(take.settings.kind||'H3 video'):'';
  $('#revisionMeta').textContent=state.view==='canvas'&&rev?'Revision '+(comp.revisions.indexOf(rev.id)+1)+' / '+comp.revisions.length:take?'Take '+(shot.takes.indexOf(take.id)+1):'';
  $('#shotName').textContent=shot?.name||'Select a shot';$('#durationLabel').textContent=(shot?shownSeconds().toFixed(2):'0')+' s';
  const last=selectedFrame();$('#scrubber').max=last;
  $('#markers').innerHTML=(shot?.guides||[]).map((g,i)=>{const pos=g.frame/(last||1)*100;return '<button class="marker '+(g.frame===0?'edge-first':g.frame===last?'edge-last':'')+'" style="left:'+pos+'%" title="'+esc(g.role)+' at frame '+g.frame+'" data-g="'+i+'">'+(g.revision?'<img src="'+revUrl(g.revision)+'" alt="">':esc(g.role))+'</button>';}).join('');
  document.querySelectorAll('[data-g]').forEach(b=>b.onclick=handle(()=>guideDetails(shot.guides[Number(b.dataset.g)])));
  $('#guide').disabled=!rev||!shot;$('#preview').disabled=!shot;$('#mediaGuide').disabled=!shot;tickFrame();
  document.querySelectorAll('[data-pane]').forEach(b=>b.classList.toggle('active',b.dataset.pane===state.pane));inspector();
}
function tickFrame(){state.frame=Math.max(0,Math.min(state.frame,selectedFrame()));const f=state.frame;$('#scrubber').value=f;$('#playhead').style.left=(f/(selectedFrame()||1)*100)+'%';$('#timecode').textContent=time(f);$('#frameLabel').textContent='Frame '+f+' / '+selectedFrame();}
$('#scrubber').oninput=e=>{state.frame=Number(e.target.value);tickFrame();const t=get().take;if(state.view==='take'&&t){$('#player').currentTime=state.frame/24;}};
const player=$('#player');
let presentedFrame=null;
function guideClock(){if(state.view==='take'){state.frame=Math.floor(player.currentTime*24);tickFrame();}}
for(const event of ['timeupdate','seeked','pause'])player.addEventListener(event,guideClock);
player.addEventListener('seeking',()=>{presentedFrame=null;});
if(player.requestVideoFrameCallback){
  const shown=(_,meta)=>{
    if(!player.seeking)presentedFrame={src:player.currentSrc,seconds:meta.mediaTime};
    player.requestVideoFrameCallback(shown);
  };
  player.requestVideoFrameCallback(shown);
}
async function displayedPosition(){
  const {take,frame}=get();
  if(!take?.video_asset)throw Error('Choose a rendered or imported take.');
  if(state.view!=='take'){
    const linked=state.doc.source_frames?.[state.compId];
    if(linked?.take===take.id)return {take:take.id,source_index:linked.index};
    const retained=player.getAttribute('src')==='/take/'+encodeURIComponent(take.id);
    state.view='take';render();
    if(!retained && frame>0)player.currentTime=frame/24;
  }
  if(!player.paused)player.pause();
  if(player.seeking||player.readyState<2){
    await new Promise((resolve,reject)=>{
      const stop=()=>{clearTimeout(timer);player.removeEventListener('seeked',ready);player.removeEventListener('loadeddata',ready);};
      const ready=()=>{if(!player.seeking&&player.readyState>=2){stop();resolve();}};
      const timer=setTimeout(()=>{stop();reject(Error('Playback did not reach the selected frame.'));},10000);
      player.addEventListener('seeked',ready);player.addEventListener('loadeddata',ready);ready();
    });
  }
  // A seeked event may precede compositor presentation by one display refresh.
  await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
  if(get().take?.id!==take.id)throw Error('The displayed take changed.');
  return presentedFrame?.src===player.currentSrc?{take:take.id,presented_seconds:presentedFrame.seconds,seconds:player.currentTime}:
    {take:take.id,seconds:player.currentTime};
}
async function stepSource(step){
  if(state.view!=='take'){state.view='take';render();}
  const position=await displayedPosition();
  const info=await api('edit_info',{...position,step});
  presentedFrame=null;player.currentTime=info.seek_seconds;
  await displayedPosition();toast('Source frame '+info.source_index+' · PTS '+info.pts);
}
on('#canvasTab',()=>{state.view='canvas';render();});on('#takeTab',()=>{state.view='take';render();});
document.querySelectorAll('[data-pane]').forEach(b=>b.onclick=()=>{state.pane=b.dataset.pane;render();});
function inspector(){
  const {rev,comp,shot,take}=get(),p=$('#pane');
  if(state.pane==='layers'){
    if(!rev){p.innerHTML=btn('importStill','Import still','wide');on('#importStill',()=>importFile('image'));return;}
    const i=state.layerIndex,l=rev.layers[i];
    p.innerHTML='<div class="eyeline">Layer stack '+btn('addLayer','＋')+'</div>'+rev.layers.map((layer,n)=>'<button class="layer-row '+(i===n?'active':'')+'" data-layer="'+n+'"><img src="'+assetUrl(layer.asset)+'" alt=""><span><strong>'+esc(layer.name||'Layer')+'</strong><small>'+(layer.visible===false?'Hidden':esc(layer.blend||'Normal'))+'</small></span></button>').reverse().join('')+
      (l?'<div class="props"><h3>Transform</h3><div class="row">'+fld('X position','lx',l.x||0,'number')+fld('Y position','ly',l.y||0,'number')+'</div><div class="row">'+fld('Scale','ls',l.scale??1,'number','step=".05" min=".05" max="4"')+fld('Rotation °','lr',l.rotation||0,'number')+'</div>'+
      '<div class="row">'+fld('Opacity %','lo',Math.round((l.opacity??1)*100),'number','min="0" max="100"')+sel('Blend','lb',['normal','multiply','screen','overlay','darken','lighten','difference'],l.blend||'normal')+'</div><div class="row">'+btn('visible',l.visible===false?'Show layer':'Hide layer','smallbtn')+btn('applyLayer','Apply edit','soft smallbtn')+'</div>'+
      '<div class="row">'+btn('raise','Raise','smallbtn')+btn('lower','Lower','smallbtn')+'</div><h3>Layer mask</h3><div class="row">'+btn('paint','Paint mask','smallbtn')+btn('loadMask','Load mask','smallbtn')+'</div></div>':'')+
      sel('Composition history','revChoice',comp.revisions.map((v,n)=>[v,'Revision '+(n+1)]),comp.current);
    document.querySelectorAll('[data-layer]').forEach(b=>b.onclick=()=>{state.layerIndex=Number(b.dataset.layer);inspector();});
    on('#addLayer',()=>importFile('layer'));on('#applyLayer',()=>patchLayer({x:Number(val('lx')),y:Number(val('ly')),scale:Number(val('ls')),rotation:Number(val('lr')),opacity:Number(val('lo'))/100,blend:val('lb')}));
    on('#visible',()=>patchLayer({visible:l.visible===false}));on('#raise',()=>patchLayer({move:1}));on('#lower',()=>patchLayer({move:-1}));on('#paint',()=>window.editTools.paint());
    on('#loadMask',async()=>{const path=await pick('file');if(path)await patchLayer({mask_path:path});});
    $('#revChoice').onchange=handle(async e=>{await api('composition',{composition:comp.id,revision:e.target.value});await refresh();});
  }else if(state.pane==='shot'){
    if(!shot){p.innerHTML=btn('shotEmpty','Add shot','wide');on('#shotEmpty',newShot);return;}
    p.innerHTML='<h3>Takes</h3>'+shot.takes.map((k,n)=>'<button data-take="'+k+'" class="take-row '+(k===take?.id?'active':'')+'"><span class="thumb-icon">▷</span><span><strong>Take '+(n+1)+'</strong><small>'+esc(state.doc.takes[k].settings.kind||'H3 video')+'</small></span></button>').join('')+(!take?'<p class="note">Render a storyboard to save your first take.</p>':'')+
    (take?'<div class="props"><h3>Edit footage</h3><div class="row">'+btn('prevSource','Previous source frame')+btn('nextSource','Next source frame')+'</div><div class="row">'+btn('openFrame','Open frame')+btn('applyFrame','Save frame edit')+'</div>'+btn('rangeEdit','Track a range','section-action')+btn('trim','Trim and audio routes','section-action')+'<h3>From this take</h3>'+btn('branchContinue','Continue from this ending','section-action')+btn('branchRetry','Try another take','section-action')+btn('branchScene','Plan a separate scene','section-action')+btn('finish','Finish with UPSCALE','section-action')+(take.settings.kind==='finish' && take.parent_take?btn('compareFinish','Compare with source','section-action'):'')+'</div>':'')+
    '<h3>Cut order</h3><div class="row">'+btn('earlier','Move earlier')+btn('later','Move later')+'</div>'+btn('cutHistory','Cut history','section-action')+btn('master','H3 motion masters','section-action');
    document.querySelectorAll('[data-take]').forEach(b=>b.onclick=handle(async()=>{await api('select',{shot:shot.id,take:b.dataset.take});state.view='take';await refresh();}));
    on('#openFrame',()=>window.editTools.openFrame());on('#applyFrame',()=>window.editTools.oneFrame());on('#rangeEdit',()=>window.editTools.range());on('#trim',trimTake);on('#finish',finish);on('#compareFinish',()=>window.editTools.compare({take:take.id}));
    on('#prevSource',()=>stepSource(-1));on('#nextSource',()=>stepSource(1));
    if(shot.locked_take && $('#branchRetry')){
      $('#branchRetry').disabled=true;
      $('#branchRetry').textContent='Try another take · locked';
      $('#branchRetry').title='Unlock this shot in its Film storyboard to replace it.';
    }
    [['#branchContinue','continue'],['#branchRetry','retry'],['#branchScene','scene']].forEach(([id,action])=>on(id,async()=>{const r=await api('branch',{take:take.id,action});state.shotId=r.shot;state.view='canvas';state.plan=null;await refresh();toast('Shot added to the cut.');}));
    on('#earlier',()=>moveShot(-1));on('#later',()=>moveShot(1));on('#cutHistory',()=>window.recoverTools.history());on('#master',masterDialog);
  }else{
    p.innerHTML='<h3>MiniMax H3 video</h3><p class="note">Generate a take from the shot’s guides and a text prompt.</p>'+btn('h3Gen','Prepare H3 shot','soft section-action')+btn('guideSpec','Check guide timing','section-action')+'<h3>AI image</h3>'+btn('imageGen','Generate or edit a still','section-action')+'<h3>Saved workflows</h3>'+btn('savedPlans','Review prepared shots','section-action')+btn('customGraph','Run custom API workflow','section-action');
    on('#h3Gen',h3Dialog);on('#guideSpec',guideTiming);on('#imageGen',imageDialog);on('#savedPlans',savedPlans);on('#customGraph',customDialog);
  }
}
async function patchLayer(changes){const {rev,layerIndex}=get();const r=await api('layer',{revision:rev.id,index:layerIndex,...changes});if(changes.move)state.layerIndex=Math.max(0,Math.min(rev.layers.length-1,layerIndex+changes.move));await setRevision(r.revision);}
let drag=null;
$('#canvasImage').onpointerdown=e=>{const {rev,layerIndex}=get();if(!rev?.layers[layerIndex])return;drag={x:e.clientX,y:e.clientY,rev:rev.id};e.target.setPointerCapture(e.pointerId);};
$('#canvasImage').onpointerup=handle(async e=>{if(!drag)return;const d=drag;drag=null;const {rev,layerIndex}=get();if(rev?.id!==d.rev)return;const b=e.target.getBoundingClientRect(),ratio=Math.min(b.width/rev.width,b.height/rev.height),dx=Math.round((e.clientX-d.x)/ratio),dy=Math.round((e.clientY-d.y)/ratio);if(Math.abs(dx)+Math.abs(dy)>1)await patchLayer({x:(rev.layers[layerIndex].x||0)+dx,y:(rev.layers[layerIndex].y||0)+dy});});
async function importFile(type){
  closeModal();const path=await pick('file');if(!path)return;
  if(type==='clip'){const r=await api('import_clip',{path});state.shotId=r.shot;state.view='take';await refresh();}
  else{const r=await api('image',{path,revision:type==='layer'?get().rev?.id:undefined});await setRevision(r.revision);state.layerIndex=get().rev.layers.length-1;render();}
}
function addMedia(){modal('Import media','<div class="dialog-menu">'+btn('impStill','Still image → new composition')+btn('impLayer','Image → new layer')+btn('impClip','Video → timeline shot')+'</div>');on('#impStill',()=>importFile('image'));on('#impLayer',()=>importFile('layer'));on('#impClip',()=>importFile('clip'));}
on('#addMedia',addMedia);
function newShot(){modal('New shot',fld('Shot name','newName','Untitled shot')+fld('Delivery duration · seconds','newDur',5,'number','min="1" max="86400"'),[{label:'Create shot',primary:true,run:async()=>{const r=await api('shot',{name:val('newName'),duration:Number(val('newDur'))});state.shotId=r.shot;state.plan=null;await refresh();closeModal();}}]);}
on('#newShot',newShot);
async function moveShot(dir){const order=[...state.doc.cut],i=order.indexOf(state.shotId),j=i+dir;if(j<0||j>=order.length)return;[order[i],order[j]]=[order[j],order[i]];await api('cut',{order});await refresh();}
function guideDialog(media=false){
  const {shot,rev}=get();const end=Math.max(0,Math.round(shot.duration*24)-1);let content=fld('Shot delivery frame','guideFrame',Math.min(state.frame,end),'number','min="0" max="'+end+'"')+'<div class="row">'+btn('gFirst','First')+btn('gMiddle','Middle')+btn('gLast','Last')+'</div>';
  content+=media?sel('Media type','gRole',['image','video','audio'],'video')+'<label class="field"><input id="clipSound" type="checkbox" style="width:auto;display:inline"> Include clip audio</label>':'<p class="note">Use the current revision of '+esc(rev.name)+'.</p>';
  content+=sel('Use in Reference mode','guideUsage',[['both','Reference and timed guide'],['timed','Timed guide only'],['reference','Reference only']],'both');
  modal('Place a frame guide',content,[{label:media?'Choose media':'Place guide',primary:true,run:async()=>{const frame=Number(val('guideFrame'));let args=media?{role:val('gRole'),path:await pick('file'),with_audio:$('#clipSound').checked}:{role:'image',revision:rev.id};if(media&&!args.path)return;
    await api('guide',{shot:shot.id,frame,usage:val('guideUsage'),...args});state.frame=frame;await refresh();closeModal();toast('Guide placed at frame '+frame+'.');}}]);
  on('#gFirst',()=>$('#guideFrame').value=0);on('#gMiddle',()=>$('#guideFrame').value=Math.round(end/2));on('#gLast',()=>$('#guideFrame').value=end);
}
on('#guide',()=>guideDialog());on('#mediaGuide',()=>guideDialog(true));
function guideDetails(g){state.frame=g.frame;tickFrame();modal('Guide · frame '+g.frame,'<p>'+esc(g.role)+' · '+(g.revision?'saved composition revision':'imported media')+'</p>'+(g.revision?'<img style="width:100%;border-radius:5px" src="'+revUrl(g.revision)+'" alt="Saved guide">':''),[
  {label:'Remove guide',run:async()=>{await api('unguided',{shot:state.shotId,frame:g.frame,role:g.role});await refresh();closeModal();}},
  ...(g.revision?[{label:'Open revision',primary:true,run:async()=>{const r=state.doc.revisions[g.revision];await api('composition',{composition:r.composition,revision:r.id});await setRevision(r.id);closeModal();}}]:[])]);}
async function waitTask(id){
  for(;;){const jobs=await api('jobs'),j=jobs.editorial.find(x=>x.id===id);if(!j)throw Error('Editorial task is unavailable.');
    if(j.state==='completed')return j.result;if(['failed','cancelled'].includes(j.state))throw Error(j.error||j.state);await new Promise(r=>setTimeout(r,500));}
}
on('#preview',async()=>{const b=$('#preview');b.disabled=true;try{const s=await api('storyboard',{shot:state.shotId});toast('Rendering CPU storyboard…');await waitTask(s.task);state.view='take';state.pane='shot';await refresh();toast('Storyboard take saved.');}finally{b.disabled=false;}});
function trimTake(){const {take}=get(),range=take.settings.delivery_trim||[0,take.duration];modal('Trim and route audio',fld('Source in · seconds','tStart',range[0],'number','min="0" step=".001"')+fld('Source out · seconds','tEnd',range[1],'number','min="0" step=".001"')+fld('Source audio per output track · blank for auto','route',(take.settings.audio_route||[]).map(x=>x===null?'silence':x).join(', '))+'<p class="note">Audio tracks start at 0. Enter “0, silence” to route the first source track and a silent second track.</p>',[{label:'Save trimmed take',primary:true,run:async()=>{const audio=val('route').trim()?val('route').split(',').map(s=>s.trim()==='silence'?null:Number(s.trim())):[];await api('trim',{take:take.id,start:Number(val('tStart')),end:Number(val('tEnd')),audio});await refresh();closeModal();}}]);}
function editSoundtrack(bed) {
  let file='';
  modal(bed?'Edit soundtrack':'Add soundtrack',btn('bedFile','Choose audio file')+'<p id="bedName" class="note">'+esc(bed?.name||'Choose narration, music or a video with audio.')+'</p>'+
    fld('Timeline offset (seconds)','bedOffset',bed?.offset??0,'number','min="0" step=".001"')+
    fld('Gain (dB)','bedGain',bed?.gain_db??0,'number','min="-96" max="24" step=".1"')+
    fld('Source audio track','bedStream',(bed?.stream??0)+1,'number','min="1"')+
    fld('Output audio track','bedTrack',(bed?.track??0)+1,'number','min="1" max="8"'),[
    ...(bed?[{label:'Remove from mix',run:async()=>{await api('soundtrack_remove',{id:bed.id});await refresh();soundtrackDialog();}}]:[]),
    {label:'Save to film',primary:true,run:async()=>{if(!file&&!bed)throw Error('Choose an audio file.');await api('soundtrack_save',{id:bed?.id,path:file,offset:+val('bedOffset'),gain_db:+val('bedGain'),stream:+val('bedStream')-1,track:+val('bedTrack')-1});await refresh();soundtrackDialog();}}]);
  on('#bedFile',async()=>{const chosen=await pick('file');if(chosen){file=chosen;$('#bedName').textContent=chosen;}});
}
function soundtrackDialog(){
  const beds=Object.values(state.doc.soundtracks||{});
  modal('Film soundtrack','<p class="note">Music and narration mix with the audio from your selected takes.</p><div class="dialog-menu">'+
    beds.map(b=>'<button data-bed="'+esc(b.id)+'">'+esc(b.name)+' · '+esc(b.offset)+' s · '+esc(b.gain_db)+' dB · track '+esc(b.track+1)+'</button>').join('')+'</div>',
    [{label:'Add soundtrack',primary:true,run:()=>editSoundtrack(null)},{label:'Export settings',run:()=>$('#export').click()}]);
  document.querySelectorAll('[data-bed]').forEach(b=>b.onclick=()=>editSoundtrack(state.doc.soundtracks[b.dataset.bed]));
}
on('#export',()=>{modal('Export film','<p class="note">'+state.doc.cut.length+' shots in timeline order</p><div class="row">'+fld('Width','ew',1920,'number')+fld('Height','eh',1080,'number')+'</div>'+sel('Delivery frame rate','ef',['24','25','30','30000/1001','60','60000/1001'],'24')+sel('Delivery color','ec',[['bt709','Rec. 709 · SDR'],['bt2020-pq','Rec. 2020 PQ · HDR']],'bt709')+fld('Audio tracks','ea',1,'number','min="0" max="8"'),[{label:'Film soundtrack',run:soundtrackDialog},{label:'Choose export file',primary:true,run:async()=>{const path=await pick('save',state.doc.name+'.mp4');if(!path)return;const r=await api('export',{path,width:+val('ew'),height:+val('eh'),fps:val('ef'),color:val('ec'),audio_tracks:+val('ea')});closeModal();toast('Exporting film…');await waitTask(r.task);toast('Film exported to '+path);}}]);});
function projectMenu(){
  const recent=(state.preferences.recent_projects||[]).filter(path=>path!==state.folder);
  const recentList=recent.length?'<h3>Recent projects</h3><div class="dialog-menu">'+recent.map((path,i)=>'<button data-recent="'+i+'" title="'+esc(path)+'">'+esc(state.preferences.recent_labels?.[path]||path.split(/[\\/]/).pop())+'</button>').join('')+'</div>':'';
  modal(state.doc.name,'<p class="note">'+esc(state.folder)+'</p><div class="dialog-menu">'+btn('openProject','Open project folder')+btn('newProject','New project')+btn('historyProject','Restore a previous cut')+'</div>'+recentList);
  document.querySelectorAll('[data-recent]').forEach(b=>b.onclick=handle(async()=>{await api('open',{path:recent[Number(b.dataset.recent)]});await refresh();closeModal();}));
  on('#openProject',async()=>{const path=await pick('folder');if(path){await api('open',{path});await refresh();closeModal();}});on('#newProject',()=>{modal('New project',fld('Project name','projectTitle','Untitled film')+'<p class="note">Choose an empty project folder. The library and shots will travel with it.</p>',[{label:'Choose folder',primary:true,run:async()=>{const path=await pick('folder');if(path){await api('new',{path,name:val('projectTitle')});await refresh();closeModal();}}}]);});on('#historyProject',()=>window.recoverTools.history());}
on('#projectMenu',projectMenu);on('#recovery',()=>window.recoverTools.open());
window.H3={api,refresh,pick,toast,modal,closeModal,esc,get,setRevision,waitTask,displayedPosition};
