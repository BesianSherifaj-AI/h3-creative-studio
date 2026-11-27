'use strict';
window.StudioShell=(()=>{
  const roomNames={image:'Image',motion:'Motion',film:'Film',game:'Game',edit:'Edit'};
  const modes=[['t2va','Text and timed guides'],['i2va','First frame'],['l2va','Last frame'],['fl2va','First and last frames'],['ref2va','Reference media']];
  const edits=new Map();
  let current='motion', serial=0;
  const key=()=>state.doc.id+'/'+state.shotId;
  function filter(){
    const term=$('#assetFilter').value.toLowerCase().trim();
    const entries=[...document.querySelectorAll('.library .lib-item')];
    entries.forEach(el=>el.hidden=!el.textContent.toLowerCase().includes(term));
    $('#binEmpty').hidden=!term||entries.some(el=>!el.hidden);
  }
  function chooseShot(id){
    if(!state.doc?.shots[id])return;
    state.shotId=id;state.frame=0;state.plan=null;
    const guide=get().shot.guides.find(g=>g.revision);
    if(guide)state.compId=state.doc.revisions[guide.revision].composition;
  }
  async function selectRoom(next,shot=null){
    if(!roomNames[next]||!state.doc)return;
    if(current!==next){
      if(current==='edit')CutRoom.close();
      if(current==='image')ImageRoom.close();
      if(current==='film'||current==='game')DirectorRoom.close();
    }
    current=next;document.body.dataset.room=next;
    document.body.classList.remove('monitor-focus');
    $('#player').pause();
    document.querySelectorAll('[data-room-select]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.roomSelect===next)));
    $('#footerRoom').textContent=roomNames[next];$('#toggleLibrary').disabled=!['image','motion'].includes(next);
    if(next==='image'){state.view='canvas';state.pane='layers';}
    if(next==='motion')state.pane='ai';
    render();
    if(next==='image')ImageRoom.open();
    if(next==='film'||next==='game')DirectorRoom.open(next);
    if(next==='edit')await CutRoom.open(shot);
  }
  function inferred(shot){
    const frames=shot.guides.filter(g=>g.revision||g.role==='image').map(g=>g.frame);
    const first=frames.includes(0),last=frames.includes(Math.round(shot.duration*24)-1);
    return first&&last?'fl2va':first?'i2va':last?'l2va':'t2va';
  }
  async function prepareShot(id,mode){
    chooseShot(id);await selectRoom('motion');await h3Dialog();
    if(mode&&modes.some(([v])=>v===mode))$('#hm').value=mode;
  }
  function showRecipe(r,shot,node){
    const end=Math.max(0,Math.round(shot.duration*24)-1);
    const places=[['First',0],['Middle',Math.round(end/2)],['Last',end]];
    const guideSlots=places.map(([name,frame])=>{
      const g=shot.guides.find(g=>g.frame===frame&&(g.revision||g.role==='image'));
      const src=g?.revision?revUrl(g.revision):g?.asset?assetUrl(g.asset):'';
      return '<button data-slot="'+frame+'" title="'+name+' guide · frame '+frame+'">'+(src?'<img alt="" src="'+src+'">':'<i class="no-cue">+</i>')+'<span>'+name+'</span></button>';
    }).join('');
    node.innerHTML='<h3>Generate motion</h3><p>MiniMax H3</p>'+sel('Conditioning','motionMode',modes,r.mode||inferred(shot))+
      '<h4>Frame guides</h4><div class="motion-guides">'+guideSlots+'</div>'+area('Shot prompt','motionPrompt',r.prompt||'')+
      '<div class="row">'+fld('Seed','motionSeed',r.seed??0,'number','min="0" max="9007199254740991"')+
      sel('Sampling','motionQuality',['fast','detailed'],r.settings?.quality||'fast')+'</div>'+
      btn('motionReview','Review generation','primary')+btn('h3Gen','Advanced settings','section-action')+
      '<div class="quiet-actions">'+btn('saveMotionDraft','Save shot settings')+btn('guideSpec','Check guide timing')+btn('savedPlans','Prepared recipes')+btn('customGraph','Custom API workflow')+'</div>';
    const read=()=>({...r,mode:val('motionMode'),seed:Number(val('motionSeed')),prompt:val('motionPrompt'),settings:{...r.settings,quality:val('motionQuality')}});
    if(edits.has(key())) $('#projectStatus').textContent='Unsaved shot settings';
    node.oninput=()=>{edits.set(key(),read());$('#projectStatus').textContent='Unsaved shot settings';};
    node.querySelectorAll('[data-slot]').forEach(b=>b.onclick=handle(()=>{
      state.frame=Number(b.dataset.slot);tickFrame();
      const g=shot.guides.find(g=>g.frame===state.frame&&(g.revision||g.role==='image'));
      if(g)return guideDetails(g);
      if(get().rev)return guideDialog(false);
      guideDialog(true);
    }));
    const advanced=async()=>{const draft=read(),cacheKey=key();await h3Dialog();$('#dialog').addEventListener('close',()=>{edits.delete(cacheKey);if(state.doc&&current==='motion'&&state.pane==='ai')draw();},{once:true});$('#hp').value=draft.prompt;$('#hm').value=draft.mode;$('#hs').value=draft.seed;$('#hq').value=draft.settings.quality;};
    on('#h3Gen',advanced);
    on('#saveMotionDraft',async()=>{await api('recipe_save',{shot:shot.id,recipe:read()});edits.delete(key());$('#projectStatus').textContent='Saved locally';toast('Shot settings saved.');});
    on('#motionReview',async()=>{
      if(shot.branch_action==='continue')return advanced();
      const draft=read(),go=$('#motionReview');go.disabled=true;
      try{
        const result=await api('prepare',{shot:shot.id,...draft});
        await api('recipe_save',{shot:shot.id,recipe:draft});edits.delete(key());await refresh();reviewPlan(result.plan,result.manifest);
      }finally{go.disabled=false;}
    });
    on('#guideSpec',guideTiming);on('#savedPlans',savedPlans);on('#customGraph',customDialog);
  }
  function draw(){
    if(!state.doc)return;
    $('#compCount').textContent=String(Object.keys(state.doc.compositions).length);
    filter();
    if(current==='image')return ImageRoom.refresh();
    if(current==='film'||current==='game')return DirectorRoom.refresh();
    if(current==='edit'){CutRoom.refresh().catch(e=>toast(e.message));return;}
    if(state.pane!=='ai')return;
    const shot=get().shot,container=$('#pane'),epoch=++serial;
    if(!shot){container.innerHTML='<h3>Generate motion</h3>'+btn('newMotionShot','Create a shot','section-action primary');on('#newMotionShot',newShot);return;}
    container.innerHTML='<div class="motion-form"></div>';const node=container.firstElementChild;
    const cached=edits.get(key());
    if(cached){showRecipe(cached,shot,node);return;}
    api('recipe_get',{shot:shot.id}).then(data=>{if(epoch===serial&&node.isConnected)showRecipe(data.recipe,shot,node);}).catch(e=>{if(node.isConnected)node.textContent=e.message;});
  }
  function commands(){
    const actions=[
      ['Image','Edit layers and selections',()=>selectRoom('image')],
      ['Image','Generate, outpaint or decompose an image',async()=>{await selectRoom('image');imageDialog();}],
      ['Motion','Prepare a shot with frame guides',()=>selectRoom('motion')],
      ['Film','Write or approve a storyboard',()=>selectRoom('film')],
      ['Game','Resume or branch a story',()=>selectRoom('game')],
      ['Edit','Cut, trim and mix the film',()=>selectRoom('edit')],
      ['Audio','Mix music and imported audio',soundtrackDialog],
      ['Voice','Create narration and captions',()=>StudioVoice.open()],
      ['AI clients','Connect a local assistant',()=>StudioAssistant.open()],
      ['Examples','Open an editable workshop',()=>StudioWorkshops.open()],
      ['Media','Import a still, layer or video',addMedia],
      ['Delivery','Export the film',()=>$('#export').click()],
      ['Project','Import a legacy project',()=>window.recoverTools.open()],
      ['System','Set up engines and media tools',setup]
    ];
    modal('Find a tool','<label class="field">Search tools<input id="toolQuery" placeholder="Try a task, such as trim or outpaint"></label><div id="toolMatches" class="dialog-menu"></div>');
    const run=async action=>{closeModal();await action();};
    function matches(){
      const query=val('toolQuery').trim().toLowerCase(),list=$('#toolMatches');
      list.replaceChildren();
      for(const [group,label,action] of actions){
        if(!(group+' '+label).toLowerCase().includes(query))continue;
        const b=document.createElement('button');b.type='button';b.innerHTML='<strong>'+esc(group)+'</strong> · '+esc(label);b.onclick=handle(()=>run(action));list.append(b);
      }
      if(!list.childElementCount)list.textContent='No matching tools.';
    }
    $('#toolQuery').oninput=matches;$('#toolQuery').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();$('#toolMatches button')?.click();}};matches();$('#toolQuery').focus();
  }
  async function setup(){
    const {media:m}=await api('community_status');
    const links=[['comfy','ComfyUI for Windows'],['video','MiniMax H3'],['nodes','MMH3 Media'],['attention','Attention nodes'],['image','Image models'],['director','LM Studio'],['finish','UPSCALE']];
    const sources='<details><summary>Engine installation guides and model sources</summary><div class="setup-links">'+links.map(([k,title])=>'<button type="button" data-doc="'+k+'">'+title+' ↗</button>').join('')+'</div></details>';
    const engineHelp='<section><h3>AI engines</h3><p>Connect ComfyUI for local image and motion generation, LM Studio for Film and Game writing, and UPSCALE for finishing. Choose the installed model and engine in Engines.</p>'+btn('setupEngines','Configure engines')+sources+'</section>';
    const toolHelp='<section><h3>Media tools</h3><p>'+(m.ready?'FFmpeg and FFprobe are available for editing and film export.':'Install FFmpeg and FFprobe for timeline previews, audio analysis and export. The verified 100 MiB download goes into your local Studio folder.')+'</p>'+(!m.ready&&m.can_install?btn('getMediaTools','Install media tools','primary'):'')+'</section>';
    modal('Studio setup','<div class="setup-items">'+toolHelp+engineHelp+'<section><h3>Assistant connection</h3><p>Pair a local MCP client with the open project.</p>'+btn('setupAssistant','Connect an AI client')+'</section><section><h3>Workspace shortcuts</h3><p>Image · V move, H pan, R rectangle, L lasso, B brush, 0 fit. Edit · Space play/pause, S split, Ctrl+Z undo.</p></section></div>');
    document.querySelectorAll('[data-doc]').forEach(b=>b.onclick=handle(async()=>{if(!await window.pywebview?.api?.open_guide(b.dataset.doc))toast('Open the engine setup links in the project README.');}));
    on('#setupEngines',openEngines);on('#setupAssistant',()=>StudioAssistant.open());
    on('#getMediaTools',async()=>{const result=await api('media_install');closeModal();await showQueue();await waitTask(result.task);$('#mediaSetupLabel').hidden=true;await CutRoom.refresh(true);toast('Media tools are ready.');});
  }
  document.querySelectorAll('[data-room-select]').forEach(b=>b.onclick=handle(()=>selectRoom(b.dataset.roomSelect)));
  api('community_status').then(s=>{$('#mediaSetupLabel').hidden=s.media.ready;}).catch(()=>{});
  on('#commands',commands);
  document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'&&!$('#dialog').open){e.preventDefault();commands();}});
  on('#director',()=>selectRoom('film'));on('#help',setup);
  on('#openStarter',()=>StudioWorkshops.open());
  on('#toggleLibrary',()=>document.body.classList.toggle('library-closed'));
  on('#focusMonitor',()=>document.body.classList.toggle('monitor-focus'));
  $('#assetFilter').oninput=filter;
  window.addEventListener('h3-open-shot',handle(async e=>{chooseShot(e.detail.shot);await selectRoom('motion');state.pane='shot';state.view=get().take?'take':'canvas';render();}));
  return {selectRoom,prepareShot,draw,setup};
})();
