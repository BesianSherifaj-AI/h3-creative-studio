'use strict';
on('#engines',openEngines);
async function h3Dialog(){
  const {shot}=get();if(!shot)throw Error('Add a shot to the timeline first.');
  const saved=await api('recipe_get',{shot:shot.id}), d=saved.recipe;
  let extra={...d.settings};
  const g=shot.guides.filter(x=>['image','first','middle','last','first_frame','last_frame'].includes(x.role)),end=Math.round(shot.duration*24)-1;
  const defaultMode=g.some(x=>x.frame===0)&&g.some(x=>x.frame===end)?'fl2va':g.some(x=>x.frame===0)?'i2va':'t2va';
  const startRevisions=[['','Last displayed video frame']];
  if(shot.start_revision && shot.start_revision!==get().rev?.id) {
    const savedRevision=state.doc.revisions[shot.start_revision];
    if(savedRevision)startRevisions.push([savedRevision.id,'Saved start · '+savedRevision.name]);
  }
  if(get().rev)startRevisions.push([get().rev.id,'Current canvas revision']);
  modal('Prepare H3 shot',area('Shot prompt','hp',typeof d.prompt==='string'?d.prompt:JSON.stringify(d.prompt||''))+sel('Frame conditioning','hm',[['t2va','Text + timed guides'],['i2va','First frame'],['l2va','Last frame'],['fl2va','First and last frames'],['ref2va','Reference images, clips and audio']],d.mode||defaultMode)+
    '<div class="row">'+fld('Seed','hs',d.seed||0,'number','min="0"')+sel('Resolution','hsize',['0.3','0.5','0.7','1.0'],extra.resolution||'0.3')+'</div><div class="row">'+sel('Aspect','hasp',['16:9','9:16','1:1','4:3','3:4'],extra.aspect_ratio||'16:9')+sel('Sampling','hq',['fast','detailed'],extra.quality||'fast')+'</div>'+
    (shot.branch_action==='continue'?sel('Start from','hstart',(shot.start_modes||['visual']).map(v=>[v,({visual:'Displayed visual ending',original_motion:'Original saved motion',regenerate:'Re-render ending for a new motion branch'})[v]]),shot.start_mode==='motion'?'original_motion':shot.start_mode)+sel('Visual start or regenerated ending','hstartrev',startRevisions,shot.start_revision||''):'')+
    '<details><summary>Project defaults and presets</summary>'+sel('Recipe','hRecipe',[['','Project default'],...Object.keys(saved.presets).map(n=>[n,n])],'')+btn('hApplyRecipe','Apply to form')+fld('Preset name','hPresetName')+btn('hSavePreset','Save named preset')+btn('hDefault','Use form as project default')+'</details>'+
    '<details><summary>Ordered LoRAs by mode</summary>'+area('Optional JSON · mode → ordered array of name and strength','hloras',JSON.stringify(extra.loras_by_mode||{}))+'</details><p class="note">Prepare checks the chosen renderer and uploads guide media. Review the resulting recipe before adding it to the queue.</p>',
    [{label:'Save shot settings',run:async()=>{await api('recipe_save',{shot:shot.id,recipe:read()});toast('Shot settings saved.');}},
     {label:'Prepare for review',primary:true,run:async()=>{
      const start=shot.branch_action==='continue'?{start_mode:val('hstart'),start_revision:val('hstart')==='original_motion'?null:val('hstartrev')||null}:{};
      const r=await api('prepare',{shot:shot.id,...read(),...start});await refresh();reviewPlan(r.plan,r.manifest);}}]);
  function read(){return {prompt:val('hp'),mode:val('hm'),seed:+val('hs'),settings:{...extra,resolution:val('hsize'),aspect_ratio:val('hasp'),quality:val('hq'),loras_by_mode:JSON.parse(val('hloras')||'{}')}};}
  on('#hDefault',async()=>{saved.default=await api('recipe_default',{shot:shot.id,recipe:read()});toast('Project default saved.');});
  on('#hSavePreset',async()=>{const name=val('hPresetName').trim();const result=await api('recipe_preset',{shot:shot.id,recipe:read(),name});if(!Object.hasOwn(saved.presets,name))$('#hRecipe').add(new Option(name,name));saved.presets[name]=result;$('#hRecipe').value=name;toast('Preset saved.');});
  on('#hApplyRecipe',()=>{const r=val('hRecipe')?saved.presets[val('hRecipe')]:saved.default;if(!r)throw Error('Save a project default first.');extra={...r.settings};
    for(const [id,value] of Object.entries({hp:r.prompt||'',hm:r.mode||defaultMode,hs:r.seed??0,hsize:extra.resolution||'0.3',hasp:extra.aspect_ratio||'16:9',hq:extra.quality||'fast',hloras:JSON.stringify(extra.loras_by_mode||{})}))$('#'+id).value=value;});
  if(shot.branch_action==='continue'){
    const sync=()=>{$('#hstartrev').disabled=val('hstart')==='original_motion';};
    $('#hstart').onchange=sync;sync();
  }
}
function reviewPlan(id,m){
  const info=m.timeline||{}, choice=m.start_choice;
  const start=choice?'<p>Continuation start · '+esc(choice.mode==='original_motion'?'Original saved motion':
    (choice.mode==='regenerate'?'Regenerate from ':'Use ')+(choice.revision?'canvas revision '+choice.revision:'the last displayed frame'))+'</p>':'';
  modal('Review H3 render','<p><strong>'+esc(m.width)+' × '+esc(m.height)+'</strong> · '+esc(m.mode)+' · seed '+esc(m.seed)+'</p>'+start+
    '<p class="note">'+esc(info.delivery_frames)+' delivered / '+esc(info.render_frames)+' rendered frames. Displayed endpoints map to render indices '+esc(info.first)+' and '+esc(info.last)+'.</p>'+guideRows(m.guides||[])+
    '<details><summary>Workflow manifest</summary><pre>'+esc(JSON.stringify(m,null,2))+'</pre></details>',[{label:'Approve and queue',primary:true,run:async()=>{
      const j=await api('submit_prepared',{plan:id});await refresh();toast('H3 job '+j.id.slice(0,8)+' queued.');await showQueue();
    }}]);
}
function guideRows(guides){return guides.map(g=>'<div class="status-card"><strong>Frame '+esc(g.frame)+' → render '+esc(g.frame_idx)+'</strong><small>'+esc(g.kind||'Guide')+(g.guide_rate?' · '+esc(g.source_frames)+' source frames → '+esc(g.input_frames)+' at 24 fps':'')+' · '+esc(g.retained_frames)+' retained frame(s)'+(g.source_crop_xywh?' · center crop '+g.source_crop_xywh.map(x=>Math.round(x)).join(', '):'')+(g.delivered_audio_seconds!=null?' · '+Number(g.delivered_audio_seconds).toFixed(2)+' s audible':'')+'</small></div>').join('');}
async function guideTiming(){const {shot}=get();if(!shot)throw Error('Select a shot.');const r=await api('guide_info',{shot:shot.id,motion:shot.start_mode==='motion'||shot.start_mode==='original_motion'});modal('Guide timing', '<p class="note">The prepared workflow records the final crop for your selected output size.</p>'+guideRows(r.guides));}
function showMaster(id,asset){
  modal('Saved H3 motion master','<video controls preload="metadata" style="width:100%" src="'+assetUrl(asset)+'"></video>',[{label:'Save a copy',primary:true,run:async()=>{const path=await pick('save','Motion master.mp4');if(path){await api('export_master',{master:id,path});toast('Master saved.');}}}]);
}
function masterDialog(){
  const doc=state.doc,masters=Object.entries(doc.masters||{});
  const shots=doc.cut.map(key=>doc.shots[key]).filter(s=>s.selected_take&&doc.takes[s.selected_take].packet);
  const list=shots.map(s=>'<label class="field"><span><input type="checkbox" style="display:inline;width:auto" data-master-take="'+esc(s.selected_take)+'" checked> '+esc(s.name)+'</span></label>').join('');
  modal('H3 motion masters','<p class="note">Assemble a connected chain of saved motion packets in cut order. A motion master retains native frame padding. Export film applies the editorial trims.</p>'+
    (shots.length?list:'<p>No selected takes contain saved motion packets.</p>')+
    (masters.length?'<h3>Saved masters</h3><div class="dialog-menu">'+masters.map(([id,m],i)=>'<button data-master="'+esc(id)+'">Motion master '+(i+1)+'</button>').join('')+'</div>':''),shots.length?[{label:'Check selected chain',primary:true,run:async()=>{
      const takes=[...document.querySelectorAll('[data-master-take]:checked')].map(x=>x.dataset.masterTake);
      if(takes.length<2)throw Error('Choose at least two connected takes.');
      const r=await api('prepare_master',{takes}),m=r.manifest;
      modal('Review motion master','<p>'+esc(m.frames)+' master frames · '+esc(m.editorial_frames)+' delivered frames · '+esc(m.assembly_mode)+'</p><details><summary>Timing, lineage and audio preflight</summary><pre>'+esc(JSON.stringify({segments:m.segments,audio:m.audio_boundary_preflight},null,2))+'</pre></details>',[{label:'Queue motion master',primary:true,run:async()=>{await api('submit_master',{plan:r.plan});await showQueue();}}]);
    }}]:[]);
  document.querySelectorAll('[data-master]').forEach(b=>b.onclick=handle(()=>{const m=doc.masters[b.dataset.master];showMaster(b.dataset.master,m.asset||m.video_asset);}));
}
function savedPlans(){const plans=Object.entries(state.doc.prepared||{});modal('Prepared shots',plans.length?'<div class="dialog-menu">'+plans.reverse().map(([id,p])=>'<button data-plan="'+id+'">'+esc(state.doc.shots[p.shot]?.name)+' · '+esc(p.bundle.manifest.mode)+'</button>').join('')+'</div>':'<p class="note">Prepare a shot to save its workflow for review.</p>');document.querySelectorAll('[data-plan]').forEach(b=>b.onclick=()=>reviewPlan(b.dataset.plan,state.doc.prepared[b.dataset.plan].bundle.manifest));}
function imageDialog(){
  const {rev}=get(), refs=[],tasks={h3:['create','edit','remove','recolor','outpaint'],qwen21:['create','edit','remove','recolor','outpaint','cutout'],zimage:['create','control','edit','remove','recolor','outpaint'],layered:['layers']};
  let mask='';
  modal('AI image',sel('Model','imodel',[['h3','MiniMax H3 Image'],['qwen21','Qwen Image 2.1'],['zimage','Z-Image Turbo'],['layered','Qwen Image Layered']],'h3')+
    sel('Operation','itask',tasks.h3,rev?'edit':'create')+area('Prompt','iprompt')+fld('Seed','iseed',0,'number','min="0"')+
    '<div id="iSize" class="row">'+fld('Width','iw',768,'number','min="256" max="2048" step="32"')+fld('Height','ih',768,'number','min="256" max="2048" step="32"')+'</div>'+
    '<div id="iMask">'+btn('picMask','Choose edit mask','smallbtn')+btn('clearMask','Clear mask','smallbtn')+'<p class="note" id="maskName">White pixels can change. Black pixels preserve the source.</p></div>'+
    '<div id="iPad">'+fld('Outpaint border · pixels per side','ipad',128,'number','min="32" max="512"')+'</div>'+
    '<div id="iSurface">'+sel('Removal surface','isurface',[['auto','Match a smooth surrounding surface'],['texture','Keep generated texture']],'auto')+'</div>'+
    '<div id="iLayers">'+fld('Number of RGBA layers','ilayers',3,'number','min="2" max="6"')+'</div>'+
    '<div id="iControl">'+sel('Control image','icontrol',[['canny','Canvas edges'],['gray','Grayscale canvas'],['provided','Use canvas as control']],'canny')+'</div>'+
    '<div id="iRefs">'+btn('addRef','Add reference image','smallbtn')+btn('clearRefs','Clear references','smallbtn')+'<p class="note" id="refNames"></p></div>',
    [{label:'Queue image',primary:true,run:async()=>{const model=val('imodel'),task=val('itask');await api('image_ai',{model,task,revision:rev?.id,prompt:val('iprompt'),seed:+val('iseed'),width:+val('iw'),height:+val('ih'),layers:+val('ilayers'),padding:+val('ipad'),surface:val('isurface'),control_mode:val('icontrol'),mask_path:$('#iMask').hidden?'':mask,reference_paths:$('#iRefs').hidden?[]:refs});await showQueue();}}]);
  function fields(){const task=val('itask');$('#iSize').hidden=task!=='create';$('#iMask').hidden=!['edit','remove','recolor','cutout'].includes(task);$('#iPad').hidden=task!=='outpaint';$('#iLayers').hidden=task!=='layers';$('#iSurface').hidden=task!=='remove';$('#iControl').hidden=task!=='control';$('#iRefs').hidden=!['h3','qwen21'].includes(val('imodel'));}
  $('#imodel').onchange=()=>{const options=tasks[val('imodel')],old=val('itask');$('#itask').replaceChildren(...options.map(t=>new Option(t,t)));$('#itask').value=options.includes(old)?old:options[0];fields();};
  $('#itask').onchange=fields;fields();
  on('#picMask',async()=>{const f=await pick('file');if(f){mask=f;$('#maskName').textContent=f;}});
  on('#clearMask',()=>{mask='';$('#maskName').textContent='White pixels can change. Black pixels preserve the source.';});
  on('#addRef',async()=>{if(refs.length>=3)throw Error('Choose up to three references.');const f=await pick('file');if(f){refs.push(f);$('#refNames').textContent=refs.join(' · ');}});
  on('#clearRefs',()=>{refs.length=0;$('#refNames').textContent='';});
}
function customDialog(){modal('Custom ComfyUI workflow',sel('Renderer','ck',['video','image'],'video')+area('API-format graph JSON','cgraph'),[{label:'Check and queue',primary:true,run:async()=>{await api('custom',{kind:val('ck'),shot:state.shotId,graph:JSON.parse(val('cgraph'))});await showQueue();}}]);}
on('#director',()=>directorHome());
function finish(){const {take}=get();modal('Finish with UPSCALE',sel('Workflow','fmode',[['auto','Automatic · per video'],['spatial','Upscale only · keep FPS'],['frames','Add frames only · keep size'],['enhance','DLSS enhancement · keep FPS'],['combined','Upscale + add frames']],'auto')+fld('Target frame rate','ffps','60')+fld('Scale factor','fscale',2,'number','step=".25"')+sel('Preserve HDR','fhdr',[['false','Off'],['true','On']],'false'),[{label:'Queue finishing',primary:true,run:async()=>{await api('finish',{take:take.id,settings:{workflow:val('fmode'),scale:+val('fscale'),target_fps:val('ffps'),preserve_hdr:val('fhdr')==='true'}});await showQueue();}}]);}
async function showQueue(){
  const jobs=await api('jobs');state.jobs=jobs;
  const chips=[...jobs.gpu.map(x=>({id:x.id,name:x.kind,status:x.status,info:x.message,renderer:true})),...jobs.editorial.map(x=>({id:x.id,name:x.name,status:x.state,info:x.error||x.result?.path||''}))];
  const content=chips.length?chips.map(j=>'<div class="status-card"><strong>'+esc(j.name)+'</strong><small>'+esc(j.status)+' · '+esc(j.id.slice(0,8))+'</small><div class="note">'+esc(j.info)+'</div>'+
    (j.name==='finish' && j.status==='succeeded'?'<button data-compare-job="'+esc(j.id)+'">Compare with source</button>':'')+(j.status==='succeeded'?'<button data-adopt="'+esc(j.id)+'">'+(j.name==='director'?'Review storyboard':'Open result in project')+'</button>':['completed','failed','cancelled'].includes(j.status)?'':'<button data-cancel="'+esc(j.id)+'">Cancel job</button>')+'</div>').join(''):'<p class="note">The render queue is empty.</p>';
  modal('Render queue',content+(jobs.error?'<p class="note">'+esc(jobs.error)+'</p>':''),[{label:'Refresh',run:showQueue}]);
  document.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=handle(async()=>{await api('cancel',{id:b.dataset.cancel});await showQueue();}));
  document.querySelectorAll('[data-compare-job]').forEach(b=>b.onclick=handle(()=>window.editTools.compare({job:b.dataset.compareJob})));
  document.querySelectorAll('[data-adopt]').forEach(b=>b.onclick=handle(async()=>{const r=await api('adopt',{id:b.dataset.adopt});
    if(r.master){await refresh();showMaster(r.master,r.asset);return;}
    if(r.director_review){await refresh();directorReview(r.director_review);return;}
    if(r.revision)await setRevision(r.revision);else{await refresh();const t=state.doc.takes[r.take];if(t){state.shotId=t.shot;state.view='take';state.pane='shot';render();}}closeModal();}));
}
on('#queue',showQueue);
async function badge(){const s=await api('jobs');$('#count').textContent=[...s.gpu,...s.editorial].filter(x=>!['succeeded','completed','failed','cancelled'].includes(x.state||x.status)).length;}
async function guard(){const g=await api('guard');$('#gpuStatus').textContent=g.games.length?'GPU paused · '+g.games.join(', '):'GPU queue available';}
(async()=>{try{await refresh();state.layerIndex=Math.max(0,(get().rev?.layers.length||1)-1);render();await Promise.all([badge(),guard()]);}catch(e){toast(e.message);}setInterval(()=>badge().catch(()=>{}),5000);setInterval(()=>guard().catch(()=>{}),30000);})();
