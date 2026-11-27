'use strict';
window.DirectorRoom=(()=>{
  let mode=null, project=null, boardId=null, turnId=null, reviewId=null, rowIndex=0, editing=false, editVersion=0;
  let legacyId=null, legacyBranch=null;
  let inbox=[], videoJobs=[], timer=null, polling=false, displayedDoc=null;
  const drafts=new Map();
  const host=()=>document.querySelector('#directorWorkspace');
  const store=()=>state.doc.director||{boards:{},reviews:{},turns:{}};
  const action=(key,label,value='',primary=false)=>'<button type="button" data-dr="'+key+'" data-v="'+esc(value)+'"'+(primary?' class="dr-primary"':'')+'>'+esc(label)+'</button>';
  const options=(values,selected)=>values.map(([v,l])=>'<option value="'+esc(v)+'"'+(v===selected?' selected':'')+'>'+esc(l)+'</option>').join('');
  const scope=()=>[project,mode,boardId,turnId,reviewId,rowIndex].join('/');
  const draft=(key,fallback='')=>drafts.get(scope()+'/'+key)??fallback;
  const note=t=>'<p class="dr-note">'+esc(t)+'</p>';
  const field=(title,key,value='',multiline=false,attrs='')=>'<label class="dr-field"><span>'+esc(title)+'</span>'+(multiline?
    '<textarea id="'+key+'" '+attrs+'>'+esc(draft(key,value))+'</textarea>':'<input id="'+key+'" value="'+esc(draft(key,value))+'" '+attrs+'>')+'</label>';
  function modePicker(id){
    return '<label class="dr-field"><span>H3 frame conditioning</span><select id="'+id+'">'+options([
      ['t2va','Text and timed guides'],['i2va','First frame'],['l2va','Last frame'],['fl2va','First and last frames'],
      ['ref2va','Image, video and audio references']],draft(id,'t2va'))+'</select></label>';
  }
  function savedPicker(){
    const data=store(),film=mode==='film';
    const values=film?Object.entries(data.boards).map(([k,b])=>[k,b.title]):Object.entries(data.turns).filter(([,t])=>!t.parent).map(([k,t])=>[k,t.action]);
    if(!film)for(const story of Object.values(state.doc.legacy?.documents||{}).map(v=>v.document)){
      if(story?.mode==='game' && story.id && state.doc.legacy.documents['stories/'+story.id+'.json']?.document===story){
        values.push(['imported:'+story.id,'Imported · '+(story.title||story.id)]);
      }
    }
    const selected=film?boardId:legacyId?'imported:'+legacyId:lineage(turnId)[0];
    return '<label class="dr-library"><span>'+ (film?'Saved storyboards':'Saved games')+'</span><select id="drSaved">'+
      options([['',film?'New storyboard':'New game'],...values],selected||'')+'</select></label>';
  }
  function lineage(id){
    const seen=new Set(),ids=[];
    while(id && store().turns[id] && !seen.has(id)){seen.add(id);ids.unshift(id);id=store().turns[id].parent;}
    return ids;
  }
  function model(){
    const name=mode==='film'?'Film':'Game';
    return '<div class="dr-model"><span>'+name+' Director profile</span><strong>'+esc(state.preferences[mode+'_director_model']||state.preferences.director_model||'Choose a model')+'</strong>'+action('profile','Edit '+name+' profile')+'</div>';
  }
  function profile(){
    const prefix=mode+'_director_',p=state.preferences,title=mode==='film'?'Film':'Game';
    const chosen=p[prefix+'model']||'';
    modal(title+' Director profile',fld('Local Director address · leave blank to use Engines','drProfileURL',p[prefix+'url']||'')+
      sel('Model','drProfileModel',[['','Use Engines selection'+(p.director_model?' · '+p.director_model:'')],...(chosen?[[chosen,chosen]]:[])],chosen)+
      btn('drFetchModels','List models at address')+area('Direction for every '+title+' request','drProfileNotes',p[prefix+'instructions']||''),
      [{label:'Save profile',primary:true,run:async()=>{
        await api('settings',{[prefix+'model']:val('drProfileModel'),[prefix+'url']:val('drProfileURL'),[prefix+'instructions']:val('drProfileNotes')});
        closeModal();await refresh();paint();
      }}]);
    on('#drFetchModels',async()=>{
      const {models}=await api('director_models',{url:val('drProfileURL')||p.director_url});
      const pick=$('#drProfileModel'),current=pick.value;
      pick.innerHTML=options([['','Use Engines selection'],...(current&&!models.includes(current)?[[current,current+' · unavailable']]:[]),...models.map(m=>[m,m])],current);
    });
  }
  function starter(){
    const film=mode==='film';
    const counts=Array.from({length:12},(_,i)=>[String(i+1),String(i+1)]);
    return '<section class="dr-compose"><h2>'+(film?'Plan a film':'Start a game')+'</h2>'+
      field(film?'Film brief':'Opening scene or player action','drBrief','',true,'rows="5"')+
      (film?'<div class="dr-pair"><label class="dr-field"><span>Shots</span><select id="drCount">'+options(counts,draft('drCount','3'))+'</select></label>'+
        '<label class="dr-field"><span>Approval preference</span><select id="drPreference">'+options([['storyboard','Storyboard only'],['per_shot','Individual shots'],['all','All shots']],state.preferences.film_approval||'per_shot')+'</select></label></div>':
        '<details class="dr-details"><summary>Player and starting location</summary>'+field('Player name','drPlayer')+field('Appearance','drAppearance')+field('Opening location','drLocation')+'</details>')+
      action('request',film?'Generate storyboard':'Generate opening turn','',true)+'</section>';
  }
  function inboxMarkup(){
    const data=store(),pendingReviews=Object.values(data.reviews).filter(r=>r.mode===mode&&!data.boards[r.id]&&!data.turns[r.id]&&!(r.intent.board&&data.boards[r.intent.board]?.rows[r.intent.row]?.revision===r.id));
    const outstanding=inbox.filter(j=>!data.reviews[j.id]);
    const items=pendingReviews.map(r=>'<div class="dr-job"><div><strong>'+esc(r.intent.brief)+'</strong><small>Ready for review</small></div>'+action('review','Review',r.id)+'</div>');
    for(const j of outstanding){
      const completed=j.status==='succeeded', failed=['failed','cancelled'].includes(j.status);
      items.push('<div class="dr-job"><div><strong>'+esc(j.brief||'Director request')+'</strong><small>'+esc(j.status)+(j.message?' · '+esc(j.message):'')+'</small></div>'+
        (completed?action('adopt','Open response',j.id):failed?'':action('cancel','Cancel request',j.id))+'</div>');
    }
    return '<h3>Director requests</h3>'+(items.join('')||note('No pending requests.'));
  }
  function mediaFor(shot,takeId){
    const take=state.doc.takes[takeId||shot?.selected_take];
    const guide=[...(take?.guides||[]),...(shot?.guides||[])].find(g=>g.revision&&state.doc.revisions[g.revision]||
      state.doc.assets[g.asset]&&['image','first','last','middle','first_frame','last_frame','reference_image'].includes(g.role));
    const poster=guide?(guide.revision?revUrl(guide.revision):assetUrl(guide.asset)):
      state.doc.assets[take?.ending_asset]?assetUrl(take.ending_asset):'';
    if(!poster&&!state.doc.assets[take?.video_asset])return null;
    return {poster,take:state.doc.assets[take?.video_asset]?take.id:null,
      label:guide?'Guide · frame '+guide.frame:poster?'Saved ending frame':'Saved video take'};
  }
  function mediaCard(media,context){
    if(!media)return '';
    const display=media.poster?'<img src="'+media.poster+'" alt="'+esc(context+' · '+media.label)+'">':
      '<video controls preload="metadata" src="/take/'+encodeURIComponent(media.take)+'"></video>';
    return '<figure class="dr-screen"><div class="dr-frame">'+display+
      (media.take&&media.poster?action('playPreview','Play take',media.take):'')+'</div><figcaption>'+
      esc(context)+'<span>'+esc(media.label)+'</span></figcaption></figure>';
  }
  function turnVisual(id){
    const ids=lineage(id);
    for(let i=ids.length-1;i>=0;i--){
      const t=store().turns[ids[i]],own=state.doc.shots[t.shot],reference=state.doc.shots[t.reference_shot];
      const media=mediaFor(own)||mediaFor(null,t.source_take)||mediaFor(reference);
      if(media){
        const from=ids[i]!==id?'From turn '+(i+1)+' · ':'';
        return mediaCard(media,from+(own&&mediaFor(own)?own.name:t.source_take?'Imported scene':reference?.name||'Scene'));
      }
    }
    return '';
  }
  function importedGame(){
    const d=state.doc.legacy,story=d.documents['stories/'+legacyId+'.json'].document;
    const branches=Object.keys(story.branches||{});
    if(!branches.includes(legacyBranch))legacyBranch=branches.includes(story.active_branch_id)?story.active_branch_id:branches[0];
    const chain=d.cuts['story:'+legacyId+':'+legacyBranch]||[];
    const rows=chain.map((entry,i)=>{
      const t=(story.turns||[]).find(t=>t.run_id===entry.legacy_run),take=state.doc.takes[entry.take];
      return '<section class="dr-turn"><small>Scene '+(i+1)+'</small><h3>'+esc(t?.message||entry.legacy_run)+'</h3>'+
        (t?.plan?.action?'<p class="dr-prose">'+esc(t.plan.action)+'</p>':'')+
        (take?.video_asset&&state.doc.assets[take.video_asset]?'<div class="dr-preview"><video controls preload="metadata" src="/take/'+encodeURIComponent(entry.take)+'"></video></div>':note('Saved video unavailable.'))+'</section>';
    }).join('');
    return '<h2>'+esc(story.title||story.id)+'</h2>'+note(story.premise||'Imported Game session')+
      '<label class="dr-field"><span>Imported branches</span><select id="drLegacyBranch">'+options(branches.map(b=>[b,b]),legacyBranch)+'</select></label>'+
      '<div class="dr-transcript">'+rows+'</div><div class="dr-actions">'+
      (legacyBranch?action('resumeImport','Continue this Game branch','',true):'')+
      (chain.length?action('openLegacyCut','Open branch in timeline'):'')+'</div>';
  }
  function renderProgress(plan,locked){
    const job=videoJobs.find(j=>j.id.replaceAll('-','')===plan.replaceAll('-',''));
    const status=job?.status||'prepared';
    return '<span>Render · '+esc(status)+'</span>'+
      (status==='succeeded'?action('adoptVideo','Open completed take',job.id):!job&&!locked?action('resumeRender','Resume saved render',plan):'');
  }
  function renderPanel(plan,locked=false){
    return '<div class="dr-render" data-dr-plan="'+esc(plan)+'" data-locked="'+locked+'">'+renderProgress(plan,locked)+'</div>';
  }
  function filmBoard(){
    const board=store().boards[boardId];rowIndex=Math.min(rowIndex,board.rows.length-1);
    const selected=board.rows[rowIndex],shot=state.doc.shots[selected.shot],take=state.doc.takes[shot?.selected_take];
    const preview=mediaCard(mediaFor(shot),selected.title),locked=!!shot?.locked_take,last=selected.renders.at(-1);
    const nav=board.rows.map((r,i)=>{
      const s=state.doc.shots[r.shot],thumb=mediaFor(s)?.poster;
      return '<button class="dr-shot'+(i===rowIndex?' is-current':'')+'" data-dr="row" data-v="'+i+'" aria-pressed="'+(i===rowIndex)+'">'+
        (thumb?'<img class="dr-thumb" alt="" loading="lazy" src="'+thumb+'">':'')+'<span><small>Shot '+String(i+1).padStart(2,'0')+'</small><strong>'+esc(r.title)+'</strong><small>'+r.duration+' s · '+(s?.locked_take?'Locked':s?.selected_take?'Take saved':s?'Approved':'Draft')+'</small></span></button>';
    }).join('');
    const draftsRemain=board.rows.some(r=>!r.shot),preferred=state.preferences.film_approval||'per_shot';
    const summary='<div class="dr-boardhead"><div><h2>'+esc(board.title)+'</h2><small>'+board.rows.length+' shots · '+board.rows.reduce((v,r)=>v+r.duration,0)+' s'+(board.author?' · '+esc(board.author):'')+'</small></div><div class="dr-actions">'+
      (draftsRemain?action('approveAll','Approve all shots')+action('renderAll','Approve and render all','',preferred==='all'):'')+
      (board.rows.some(r=>r.shot&&!state.doc.shots[r.shot]?.locked_take)?action('renderApproved','Render approved shots'):'')+'</div></div>';
    let detail;
    if(editing){
      detail=field('Shot title','drTitle',selected.title)+field('Duration in seconds','drDuration',selected.duration,false,'type="number" min="3" max="15"')+
        field('Generation prompt','drPrompt',selected.prompt,true,'rows="4"')+'<div class="dr-actions">'+action('saveEdit','Save shot edits','',true)+action('cancelEdit','Cancel edits')+'</div>';
    }else{
      detail='<div class="dr-boardhead"><div><small>Shot '+(rowIndex+1)+' · '+selected.duration+' s</small><h2>'+esc(selected.title)+'</h2></div>'+
        (!locked?action('edit','Edit shot'):'')+'</div>'+preview+'<p class="dr-prose">'+esc(selected.prompt)+'</p>'+
        '<div class="dr-actions">'+(!shot?action('approve','Approve shot','',preferred==='per_shot'):'')+
        (shot&&!locked?action('render',last?'Regenerate video take':'Render shot','',true):'')+
        (take?action('lock',locked?'Unlock take':'Lock selected take'):'')+(shot?action('timeline','Open timeline',shot.id):'')+'</div>';
      if(last)detail+=renderPanel(last.plan,locked);
      if(!locked){
        detail+='<details class="dr-details"><summary>Revise with Director</summary>'+field('Requested change','drChange','',true)+action('revise','Request revised shot')+'</details>';
        if(shot)detail+='<div class="dr-guide">'+modePicker('drMode')+action('prepare','Prepare with frame guides',shot.id)+'</div>';
      }
    }
    return summary+'<div class="dr-board"><nav aria-label="Storyboard shots">'+nav+'</nav><article>'+detail+'</article></div>';
  }
  function game(){
    const data=store(),turn=data.turns[turnId],path=lineage(turnId),root=path[0];
    const branches=Object.entries(data.turns).filter(([key])=>lineage(key)[0]===root);
    const transcript=path.map((key,i)=>{
      const t=data.turns[key];
      return '<section class="dr-turn"><div class="dr-turnhead"><small>Turn '+(i+1)+(t.author?' · '+esc(t.author):'')+'</small>'+
        (key===turnId?'':action('turn','Branch from here',key))+'</div><h3>'+esc(t.action)+'</h3><p class="dr-prose">'+esc(t.scene)+'</p>'+
        (t.state_changes.length?'<ul class="dr-changes">'+t.state_changes.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul>':'')+'</section>';
    }).join('');
    const shot=state.doc.shots[turn.shot],prev=turnVisual(turnId);
    const last=turn.renders?.at(-1);
    const clips=branchClips(path);
    return '<div class="dr-game-grid"><article class="dr-current"><div class="dr-game-marker"><small>Turn '+path.length+
      (turn.author?' · '+esc(turn.author):'')+'</small>'+(turnId!==data.active_turn?action('resumeGame','Resume this branch'):'<span class="dr-status">Current branch</span>')+'</div>'+
      '<h2>'+esc(turn.action)+'</h2><p class="dr-narration">'+esc(turn.scene)+'</p>'+
      (turn.state_changes.length?'<ul class="dr-changes">'+turn.state_changes.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul>':'')+
      '<div class="dr-move"><h3>Next player action</h3><div class="dr-choices">'+turn.actions.map(v=>action('choice',v,v)).join('')+'</div>'+
      field('Your move','drMove','',true,'rows="2"')+'<div class="dr-actions">'+action('move','Generate next turn','',true)+action('world','World & inventory')+'</div></div></article>'+
      '<section class="dr-game-screen">'+(prev||note('Add a frame guide or generate a video for this scene.'))+
      '<div class="dr-guide">'+modePicker('drGameMode')+action('gameRender',last?'Regenerate scene video':'Generate scene video','',true)+action('gameVideo','Prepare with frame guides')+'</div>'+
      (last?renderPanel(last,!!shot?.locked_take):'')+
      '<div class="dr-actions">'+(shot?action('timeline','Open timeline',shot.id):'')+(clips.length?action('playBranch','Play branch · '+clips.length+' clips'):'')+'</div><div id="drPlayback"></div></section></div>'+
      '<section class="gw-panel" id="drWorld" hidden></section><section class="dr-history"><label class="dr-field"><span>Saved turns and branches</span><select id="drBranch">'+options(branches.map(([k,t])=>[k,'Turn '+lineage(k).length+' · '+t.action]),turnId)+'</select></label>'+
      '<details><summary>Conversation · '+path.length+' turn'+(path.length===1?'':'s')+'</summary><div class="dr-transcript">'+transcript+'</div></details></section>';
  }
  function branchClips(path){
    return path.map(k=>{const t=store().turns[k];return state.doc.shots[t.shot]?.selected_take||t.source_take;})
      .filter(k=>state.doc.assets[state.doc.takes[k]?.video_asset]);
  }
  function reviewPanel(){
    const r=store().reviews[reviewId],d=r.parsed;
    let content;
    if(r.error)content=note(r.error)+field('Corrected response JSON','drCorrection',r.raw,true,'rows="8"');
    else if(r.mode==='game'){
      content='<p class="dr-prose">'+esc(d.scene)+'</p><ul class="dr-changes">'+d.state_changes.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul>';
      if(r.intent.resolved)content+=note(d.outcome==='blocked'?'Action blocked · inventory and position stay as they are.':'On acceptance · '+r.intent.brief);
      if(!r.intent.resolved && d.outcome!=='blocked' && d.world_effects?.length)content+='<details class="dr-details" open><summary>World changes on acceptance</summary>'+
        d.world_effects.map(e=>note(Object.entries(e).map(([k,v])=>k.replaceAll('_',' ')+' · '+String(v)).join(' / '))).join('')+'</details>';
      for(const group of ['locations','entities'])if(d.discoveries?.[group]?.length)content+=note('Discovered · '+d.discoveries[group].map(x=>x.name).join(', '));
    }
    else content=d.rows.map(t=>'<h3>'+esc(t.title)+'</h3><p class="dr-prose">'+esc(t.prompt)+'</p>').join('');
    return '<section class="dr-compose"><small>'+esc(r.model)+'</small><h2>'+esc(r.mode==='game'?'Review Game turn':r.intent.board?'Review shot revision':'Review Film plan')+'</h2>'+
      note(r.intent.brief)+content+'<div class="dr-actions">'+action('accept',r.mode==='game'?'Accept turn':r.intent.board?'Approve revision':'Approve storyboard','',true)+action('back','Back')+'</div></section>';
  }
  function paint(){
    if(!mode||!state.doc||!host())return;
    displayedDoc=state.doc;
    const data=store();
    if(project!==state.doc.id){
      project=state.doc.id;boardId=null;turnId=null;reviewId=null;legacyId=null;legacyBranch=null;rowIndex=0;editing=false;inbox=[];videoJobs=[];drafts.clear();
    }
    if(boardId===null || boardId&&!data.boards[boardId])boardId=Object.keys(data.boards).at(-1)||'';
    if(turnId===null || turnId&&!data.turns[turnId])turnId=data.active_turn||Object.keys(data.turns).at(-1)||'';
    if(reviewId&&!data.reviews[reviewId])reviewId=null;
    const film=mode==='film';
    host().innerHTML='<header class="dr-header"><div><p class="dr-eyebrow">AI Director</p><h1>'+(film?'Film storyboard':'Game director')+'</h1></div>'+
      '<div class="dr-headtools">'+savedPicker()+action('new',film?'New film':'New game')+'</div></header>'+
      '<div class="dr-layout'+(!film&&turnId&&!reviewId&&!legacyId?' dr-game-layout':'')+'"><div class="dr-main">'+(reviewId?reviewPanel():film&&boardId?filmBoard():!film&&legacyId?importedGame():!film&&turnId?game():starter())+
      '</div><aside class="dr-side">'+model()+'<section class="dr-inbox">'+inboxMarkup()+'</section></aside></div>';
    host().oninput=e=>{if(e.target.id)drafts.set(scope()+'/'+e.target.id,e.target.value);};
    host().onchange=handle(async e=>{
      const el=e.target;
      if(el.id==='drSaved'){
        reviewId=null;editing=false;rowIndex=0;legacyId=null;legacyBranch=null;
        if(film)boardId=el.value;else if(el.value.startsWith('imported:'))legacyId=el.value.slice(9);else turnId=el.value;paint();
      }
      if(el.id==='drBranch'){turnId=el.value;reviewId=null;paint();}
      if(el.id==='drLegacyBranch'){legacyBranch=el.value;paint();}
      if(el.id==='drPreference'){await api('settings',{film_approval:el.value});state.preferences.film_approval=el.value;}
    });
    host().onclick=handle(async e=>{
      const b=e.target.closest('[data-dr]');if(!b)return;
      b.disabled=true;try{await command(b.dataset.dr,b.dataset.v,b);}finally{b.disabled=false;}
    });
  }
  function chooseReview(id){
    const data=store(),r=data.reviews[id];
    if(!r)return;
    mode=r.mode;editing=false;legacyId=null;
    if(data.boards[id]){boardId=id;rowIndex=0;reviewId=null;}
    else if(data.turns[id]){turnId=id;reviewId=null;}
    else reviewId=id;
    paint();host().scrollTop=0;
  }
  async function request(fields){
    const ticket=crypto.randomUUID();
    await api('director',{mode,request_id:ticket,...fields});
    await poll();toast('Director request queued.');
    host()?.querySelector('.dr-inbox')?.scrollIntoView({block:'nearest'});
  }
  async function updated(op,args){
    const result=await api(op,args);
    await refresh();
    return result;
  }
  async function prepare(id,conditioning){
    await window.StudioShell.prepareShot(id,conditioning);
  }
  async function command(key,value,button){
    const board=store().boards[boardId],row=board?.rows[rowIndex],shot=state.doc.shots[row?.shot];
    switch(key){
      case 'new': reviewId=null;editing=false;rowIndex=0;legacyId=null;if(mode==='film')boardId='';else turnId='';break;
      case 'settings': return openEngines();
      case 'profile': return profile();
      case 'world': {
        const panel=$('#drWorld');
        if(!panel.hidden){panel.hidden=true;return;}
        panel.hidden=false;
        await GameWorld.mount(panel,turnId,request,async id=>{turnId=id;await refresh();paint();host().scrollTop=0;});
        panel.scrollIntoView({block:'start'});return;
      }
      case 'playPreview': {
        const frame=button.closest('.dr-frame'),player=document.createElement('video');
        player.controls=true;player.poster=frame.querySelector('img').src;player.src='/take/'+encodeURIComponent(value);
        frame.replaceChildren(player);await player.play();return;
      }
      case 'request': return request({prompt:val('drBrief'),...(mode==='film'?{count:+val('drCount')}:{parent:null,
        game_setup:{name:val('drPlayer'),description:val('drAppearance'),location:val('drLocation')}})});
      case 'review': return chooseReview(value);
      case 'adopt': {const r=await updated('adopt',{id:value});chooseReview(r.director_review);return poll();}
      case 'cancel': await api('cancel',{id:value});return poll();
      case 'back': reviewId=null;break;
      case 'accept': {
        const r=await updated('director_approve',{review:reviewId,...($('#drCorrection')?{correction:val('drCorrection')}:{})});
        if(r.board)boardId=r.board;if(r.turn)turnId=r.turn;reviewId=null;break;
      }
      case 'row': rowIndex=+value;editing=false;break;
      case 'edit': editing=true;editVersion=row.version||0;break;
      case 'cancelEdit':
      case 'saveEdit':
        if(key==='saveEdit')await updated('director_edit',{board:boardId,row:rowIndex,version:editVersion,title:val('drTitle'),duration:+val('drDuration'),prompt:val('drPrompt')});
        for(const id of ['drTitle','drDuration','drPrompt'])drafts.delete(scope()+'/'+id);editing=false;break;
      case 'approve': await updated('director_approve',{review:boardId,row:rowIndex});break;
      case 'approveAll': await updated('director_approve',{review:boardId});break;
      case 'revise': return request({board:boardId,row:rowIndex,prompt:val('drChange')});
      case 'render':
      case 'renderApproved':
      case 'renderAll': {
        const args={board:boardId,request_id:crypto.randomUUID()};
        if(key==='render'){
          args.row=rowIndex;const last=row.renders.at(-1);if(last){args.previous=last.plan;args.retry=true;}
        }else if(key==='renderAll')await updated('director_approve',{review:boardId});
        const result=await updated('director_render',args);await poll();toast(result.jobs.length+' video jobs queued.');break;
      }
      case 'resumeRender': await api('submit_prepared',{plan:value});await poll();break;
      case 'adoptVideo': await updated('adopt',{id:value});break;
      case 'lock': await updated('director_lock',{board:boardId,row:rowIndex,take:shot.selected_take,locked:!shot.locked_take});break;
      case 'timeline': state.shotId=value;state.frame=0;state.view='take';state.pane='shot';return window.StudioShell.selectRoom('edit',value);
      case 'prepare': return prepare(value,val('drMode'));
      case 'turn': turnId=value;break;
      case 'resumeGame': await updated('director_resume',{turn:turnId});break;
      case 'resumeImport': {
        const r=await updated('director_resume_import',{story:legacyId,branch:legacyBranch});legacyId=null;turnId=r.turn;break;
      }
      case 'openLegacyCut': await updated('legacy_cut',{key:'story:'+legacyId+':'+legacyBranch});return window.StudioShell.selectRoom('edit');
      case 'choice': $('#drMove').value=value;drafts.set(scope()+'/drMove',value);$('#drMove').focus();return;
      case 'move': return request({parent:turnId,prompt:val('drMove')});
      case 'gameVideo': {
        const conditioning=val('drGameMode'),r=await updated('director_game_shot',{turn:turnId});
        return prepare(r.shot,conditioning);
      }
      case 'gameRender':
        await updated('director_game_render',{turn:turnId,mode:val('drGameMode'),request_id:crypto.randomUUID()});
        await poll();toast('Scene video queued.');break;
      case 'playBranch': {
        const clips=branchClips(lineage(turnId));
        const target=$('#drPlayback'),player=document.createElement('video'),caption=document.createElement('small');
        target.replaceChildren(caption,player);target.className='dr-preview';player.controls=true;let index=0;
        async function next(){caption.textContent='Clip '+(index+1)+' of '+clips.length;player.src='/take/'+encodeURIComponent(clips[index]);await player.play();}
        player.onended=handle(async()=>{if(++index<clips.length)await next();});
        await next();return;
      }
    }
    paint();
    if(['new','accept','back','resumeImport'].includes(key))host().scrollTop=0;
  }
  async function poll(){
    if(!mode||!project||polling)return;
    const room=mode,owner=project;polling=true;
    try{
      const list=await api('director_inbox',{mode:room});
      if(mode!==room||project!==owner)return;
      inbox=list;
      const panel=host()?.querySelector('.dr-inbox');if(panel)panel.innerHTML=inboxMarkup();
      if(room==='film'?store().boards[boardId]?.rows.some(r=>r.renders.length):store().turns[turnId]?.renders?.length){
        const jobs=(await api('jobs')).gpu.filter(j=>j.kind==='video').map(j=>({id:j.id,status:j.status}));
        if(mode===room&&project===owner&&JSON.stringify(jobs)!==JSON.stringify(videoJobs)){
          videoJobs=jobs;
          for(const panel of host().querySelectorAll('[data-dr-plan]')){
            panel.innerHTML=renderProgress(panel.dataset.drPlan,panel.dataset.locked==='true');
          }
        }
      }
    }finally{polling=false;}
  }
  function open(next){
    if(!['film','game'].includes(next)||!host())return;
    if(next!==mode){reviewId=null;editing=false;inbox=[];host().scrollTop=0;}mode=next;host().hidden=false;
    paint();poll().catch(e=>toast(e.message));clearInterval(timer);timer=setInterval(()=>poll().catch(()=>{}),2500);
  }
  function close(){mode=null;clearInterval(timer);if(host())host().hidden=true;}
  async function review(id){
    const r=store().reviews[id];if(!r)return;
    closeModal();await window.StudioShell.selectRoom(r.mode);chooseReview(id);
  }
  return {open,close,refresh(){if(displayedDoc!==state.doc)paint();},review};
})();
