'use strict';
const directorData=()=>state.doc.director||{boards:{},reviews:{},turns:{},active_turn:null};
const directorButton=(action,label,value='')=>'<button type="button" data-director="'+action+'" data-value="'+esc(value)+'">'+esc(label)+'</button>';
function directorEvents(actions){
  document.querySelectorAll('[data-director]').forEach(b=>b.onclick=handle(async()=>{
    b.disabled=true;
    try{await actions[b.dataset.director](b.dataset.value);}finally{b.disabled=false;}
  }));
}
async function directorRequest(args){
  await api('director',{...args,request_id:crypto.randomUUID()});await showQueue();
}
function directorHome(){
  const data=directorData(),boards=Object.entries(data.boards),turns=Object.entries(data.turns);
  const pending=Object.values(data.reviews).filter(r=>!data.boards[r.id]&&!data.turns[r.id]&&!(r.intent.board&&data.boards[r.intent.board]?.rows[r.intent.row]?.revision===r.id));
  const list=(entries,fn)=>'<div class="dialog-menu">'+entries.map(fn).join('')+'</div>';
  modal('AI Director',sel('Production','dmode',[['film','Film'],['game','Game']],'film')+
    '<div id="dcountbox">'+fld('Number of shots','dcount',2,'number','min="1" max="12"')+'</div>'+
    area('Film brief or opening Game action','dbrief')+
    '<p class="note">Selected model · '+esc(state.preferences.director_model||'Choose a model in Engines')+'</p>'+
    (boards.length?'<h3>Film storyboards</h3>'+list(boards,([id,b])=>directorButton('board',b.title+' · '+b.rows.length+' shots',id)):'')+
    (turns.length?'<h3>Game turns</h3>'+list(turns,([id,t])=>directorButton('turn',(id===data.active_turn?'Current · ':'')+t.action,id)):'')+
    (pending.length?'<h3>Awaiting review</h3>'+list(pending,r=>directorButton('review',r.intent.brief,r.id)):''),
    [{label:'Request plan',primary:true,run:()=>directorRequest({mode:val('dmode'),count:+val('dcount'),prompt:val('dbrief')})}]);
  $('#dmode').onchange=()=>{$('#dcountbox').hidden=val('dmode')==='game';};
  directorEvents({board:directorBoard,turn:directorTurn,review:directorReview});
}
async function directorAccept(review,extra={}){
  const r=await api('director_approve',{review,...extra});await refresh();
  if(r.turn)directorTurn(r.turn);else directorBoard(r.board);
}
function directorReview(id){
  const data=directorData(),review=data.reviews[id];
  if(data.boards[id]){directorBoard(id);return;}
  if(data.turns[id]){directorTurn(id);return;}
  const body='<p class="note">'+esc(review.model)+' · '+esc(review.intent.brief)+'</p>';
  if(review.error){
    modal('Review Director response',body+'<p>'+esc(review.error)+'</p>'+area('Corrected JSON','drepair',review.raw)+
      '<details><summary>Required fields</summary><p>'+esc(review.mode==='game'?'scene, actions and state_changes':'title and shots, each with title, duration and prompt')+'</p></details>',
      [{label:'Approve corrected response',primary:true,run:()=>directorAccept(id,{correction:val('drepair')})}]);return;
  }
  if(review.mode==='game'){
    const d=review.parsed;
    modal('Review proposed Game turn',body+'<p>'+esc(d.scene)+'</p>'+directorConsequences(d)+
      '<p class="note">Accept this turn to add it to the saved branch.</p>',[{label:'Accept turn',primary:true,run:()=>directorAccept(id)}]);return;
  }
  const row=review.parsed.rows[0];
  modal('Review revised shot',body+'<h3>'+esc(row.title)+'</h3><p>'+esc(row.prompt)+'</p><p>'+row.duration+' s</p>',
    [{label:'Approve revision',primary:true,run:()=>directorAccept(id)}]);
}
async function directorBoard(id){
  const data=directorData(),board=data.boards[id];
  state.jobs=await api('jobs');
  const jobs=new Map(state.jobs.gpu.map(j=>[j.id.replaceAll('-',''),j]));
  const rows=board.rows.map((r,i)=>{
    const s=state.doc.shots[r.shot],locked=s?.locked_take,last=r.renders.at(-1),job=jobs.get(last?.plan);
    const renderName=last&&last.version===(r.version||0)?'Regenerate video take':'Render shot';
    return '<section class="status-card"><strong>'+esc(i+1)+'. '+esc(r.title)+'</strong><small>'+r.duration+' s · '+
      (locked?'Locked take '+esc(locked.slice(0,8)):s?'Approved':'Awaiting approval')+(job?' · '+esc(job.status):'')+'</small><p>'+esc(r.prompt)+'</p>'+
      (s?directorButton('open','Open shot',i):directorButton('approve','Approve shot',i))+
      (s?.selected_take?directorButton('lock',locked?'Unlock take':'Lock selected take',i):'')+
      (!locked?directorButton('revise','Revise prompt',i):'')+
      (s&&!locked?directorButton('render',renderName,i):'')+
      (last&&!job&&s&&!locked?directorButton('resume','Resume saved render',i):'')+
      (job?.status==='succeeded'?directorButton('adopt','Open completed take',job.id):'')+'</section>';
  }).join('');
  const requestRender=async(i)=>{
    const a={board:id,request_id:crypto.randomUUID()};
    if(i!==undefined){
      a.row=+i;const row=board.rows[+i],last=row.renders.at(-1);
      if(last){a.retry=true;a.previous=last.plan;}
    }
    const r=await api('director_render',a);await refresh();
    if(r.jobs.length)await showQueue();else {directorBoard(id);toast('All approved shots already have a render request or a locked take.');}
  };
  modal(board.title,rows,[
    {label:'Director',run:directorHome},
    ...(board.rows.some(r=>!r.shot)?[
      {label:'Approve all shots',run:()=>directorAccept(id)},
      {label:'Approve and render all',primary:true,run:async()=>{
        await api('director_approve',{review:id});
        await requestRender();
      }}
    ]:[]),
    ...(board.rows.some(r=>r.shot&&!state.doc.shots[r.shot].locked_take)?[{label:'Render approved shots',primary:true,run:()=>requestRender()}]:[])
  ]);
  directorEvents({
    approve:i=>directorAccept(id,{row:+i}), render:requestRender,
    resume:async i=>{await api('submit_prepared',{plan:board.rows[+i].renders.at(-1).plan});await showQueue();},
    adopt:async j=>{await api('adopt',{id:j});await refresh();directorBoard(id);},
    lock:async i=>{const s=state.doc.shots[board.rows[+i].shot];await api('director_lock',{board:id,row:+i,take:s.selected_take,locked:!s.locked_take});await refresh();directorBoard(id);},
    open:i=>{state.shotId=board.rows[+i].shot;state.view='take';state.pane='shot';state.frame=0;closeModal();render();},
    revise:i=>{const row=board.rows[+i];modal('Revise shot '+(+i+1),'<p>'+esc(row.prompt)+'</p>'+area('Requested change','dchange'),
      [{label:'Request revision',primary:true,run:()=>directorRequest({mode:'film',board:id,row:+i,prompt:val('dchange')})}]);}
  });
}
function directorConsequences(turn){
  return (turn.state_changes||[]).length?'<h3>State changes</h3><ul>'+turn.state_changes.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul>':'';
}
function directorTurn(id){
  const data=directorData(),turn=data.turns[id],children=Object.entries(data.turns).filter(([,t])=>t.parent===id);
  const fromParent=turn.parent?directorButton('previous','Previous turn',turn.parent):'';
  modal('Game · '+turn.action,'<p>'+esc(turn.scene)+'</p>'+directorConsequences(turn)+
    '<div>'+fromParent+(id!==data.active_turn?directorButton('resume','Resume this branch',id):'')+'</div>'+
    (children.length?'<h3>Saved branches</h3><div class="dialog-menu">'+children.map(([k,t])=>directorButton('open',t.action,k)).join('')+'</div>':'')+
    '<h3>Next action</h3><div class="dialog-menu">'+turn.actions.map(v=>directorButton('choice',v,v)).join('')+'</div>'+area('Player action','daction'),
    [{label:'Director',run:directorHome},{label:'Request next turn',primary:true,run:()=>directorRequest({mode:'game',parent:id,prompt:val('daction')})}]);
  directorEvents({previous:directorTurn,open:directorTurn,choice:v=>{$('#daction').value=v;},resume:async()=>{
    await api('director_resume',{turn:id});await refresh();directorTurn(id);toast('Saved branch resumed.');
  }});
}
