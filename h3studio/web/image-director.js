(() => {
  'use strict';
  const H=()=>window.H3, get=()=>H().get(), esc=s=>H().esc(s), by=id=>document.getElementById(id);
  const refs=()=>get().doc.revisions, request=(fields)=>H().api('image_room_vision',fields);
  const models=[
    ['qwen3vl_4b_fp8_scaled.safetensors','Qwen3-VL 4B · FP8'],
    ['qwen3vl_4b_bf16.safetensors','Qwen3-VL 4B · BF16'],
    ['qwen3vl_8b_fp8_scaled.safetensors','Qwen3-VL 8B · FP8']
  ];
  const picture=(r,title)=>'<figure class="vd-photo"><figcaption>'+esc(title)+'</figcaption><img src="/revision/'+encodeURIComponent(r)+'" alt="'+esc(title)+'"></figure>';
  const notes=()=>get().doc.visual_notes||{};
  const relevant=id=>Object.values(notes()).filter(n=>n.source===id||n.result===id);
  function noteButtons(list){
    return list.length?'<h4>Saved reports</h4><div class="vd-notes">'+list.map(n=>'<button type="button" data-vd-note="'+esc(n.id)+'">'+esc((n.phase==='plan'?'Edit plan':'Result review')+' · '+n.goal)+'</button>').join('')+'</div>':'';
  }
  function bindNotes(){document.querySelectorAll('[data-vd-note]').forEach(b=>b.onclick=()=>note(b.dataset.vdNote));}
  function open(){
    const r=get().rev,history=relevant(r.id),last=history.at(-1),origin=r.generation?.settings?.visual_note;
    const linked=notes()[origin],source=r.generation?.source_revision||r.parent;
    let phase=source?'review':'plan',goal=linked?.goal||last?.goal||'',model=linked?.model||last?.model||models[0][0];
    function form(){
      const candidates=Object.values(refs()).filter(v=>v.id!==r.id);
      const options=candidates.map(v=>[v.id,v.name+' · '+v.id.slice(0,8)]);
      const before=source||candidates[0]?.id;
      const modeopts=[['plan','Plan an edit']];if(candidates.length)modeopts.push(['review','Review a result']);
      const compare=phase==='plan'?picture(r.id,'Source'):picture(before,'Source')+picture(r.id,'Edited result');
      H().modal('Visual Director',sel('Analysis','vdPhase',modeopts,phase)+
        (phase==='review'?sel('Compare this result with','vdBefore',options,before):'')+'<div id="vdPhotos" class="vd-photos">'+compare+'</div>'+
        area('Intended change','vdGoal',goal)+sel('Local vision encoder','vdModel',models,model)+noteButtons(history),
        [{label:phase==='plan'?'Queue visual plan':'Queue result review',primary:true,run:async()=>{
          await request({revision:r.id,source:phase==='review'?val('vdBefore'):r.id,phase,model:val('vdModel'),goal:val('vdGoal'),approved:true});await showQueue();
        }}]);
      by('vdPhase').onchange=e=>{goal=val('vdGoal');model=val('vdModel');phase=e.target.value;form();};
      if(phase==='review')by('vdBefore').onchange=e=>by('vdPhotos').innerHTML=picture(e.target.value,'Source')+picture(r.id,'Edited result');
      bindNotes();
    }
    form();
  }
  function note(id){
    const n=notes()[id];if(!n)return;
    const a=n.answer,target=n.result||n.source,doc=get().doc,revision=doc.revisions[target],actions=[];
    const outcome=a?'<p><strong>'+esc(n.phase==='plan'?'Edit plan':a.verdict.toUpperCase())+'</strong> · '+esc(a.finding)+'</p>':
      '<p>'+esc(n.error)+'</p><pre class="vd-raw">'+esc(n.raw)+'</pre>';
    if(a?.prompt){
      actions.push({label:'Open edit with this prompt',primary:true,run:async()=>{
        const fresh=get().doc;
        if(fresh.compositions[revision.composition].current!==target)throw Error('Restore the reviewed revision in the layer history before applying this instruction.');
        await H().setRevision(target);
        const selAsset=get().doc.image_selections?.[target]?.asset;
        if(n.phase==='plan'&&n.selection&&selAsset!==n.selection)throw Error('The selection has changed. Request a new plan for this canvas.');
        if(a.task==='remove'&&!selAsset)throw Error('Select the object with SAM or the canvas selection tools, then reopen the saved plan.');
        window.ImageRoom.ai(a.task||'edit',val('vdSuggestion'),n.id);
      }});
    }
    if(a?.object)actions.push({label:'Select suggested object',run:async()=>{
      if(get().doc.compositions[revision.composition].current!==target)throw Error('Restore the reviewed source revision first.');
      await H().setRevision(target);by('irAI').value='segment';by('irAI').dispatchEvent(new Event('change'));by('irObject').value=a.object;
    }});
    H().modal(n.phase==='plan'?'Visual edit plan':'Visual result review','<div class="vd-photos">'+picture(n.source,'Source')+(n.result?picture(n.result,'Edited result'):'')+'</div>'+
      '<p>'+esc(n.goal)+'</p>'+outcome+(a?.prompt?area('Suggested instruction','vdSuggestion',a.prompt):'')+
      '<small>'+esc(n.model)+'</small>',actions);
  }
  function points(){
    const rev=get().rev,marks=[];
    H().modal('Select with SAM points','<p class="note">Mark the subject with Include. Use Exclude for nearby objects.</p>'+
      sel('Point type','vdPointMode',[['include','Include'],['exclude','Exclude']],'include')+
      '<div class="vd-point-stage" id="vdPointStage"><img id="vdPointImg" src="/revision/'+encodeURIComponent(rev.id)+'" alt="Mark points on the source image"></div>'+
      '<div class="vd-notes"><output id="vdPointCount"></output><button type="button" id="vdUndoPoint">Undo point</button></div>',
      [{label:'Queue point selection',primary:true,run:async()=>{
        const group=type=>marks.filter(m=>m.type===type).map(({x,y})=>({x,y}));
        await H().api('image_ai',{revision:rev.id,model:'sam',task:'select',prompt:'',positive_points:group('include'),negative_points:group('exclude')});await showQueue();
      }}]);
    const stage=by('vdPointStage'),im=by('vdPointImg');
    function paint(){
      stage.querySelectorAll('b').forEach(b=>b.remove());
      const box=im.getBoundingClientRect(),outer=stage.getBoundingClientRect();
      for(const p of marks){const el=document.createElement('b');el.textContent=p.type==='include'?'+':'−';el.className=p.type;
        el.style.left=(box.left-outer.left+box.width*(p.x+.5)/rev.width)+'px';el.style.top=(box.top-outer.top+box.height*(p.y+.5)/rev.height)+'px';stage.append(el);}
      by('vdPointCount').textContent=marks.filter(m=>m.type==='include').length+' included · '+marks.filter(m=>m.type==='exclude').length+' excluded';
    }
    im.onclick=e=>{
      const b=im.getBoundingClientRect(),type=val('vdPointMode');
      if(marks.filter(p=>p.type===type).length>=64)return;
      marks.push({type,x:Math.min(rev.width-1,Math.floor((e.clientX-b.left)*rev.width/b.width)),y:Math.min(rev.height-1,Math.floor((e.clientY-b.top)*rev.height/b.height))});paint();
    };
    by('vdUndoPoint').onclick=()=>{marks.pop();paint();};im.onload=paint;paint();
  }
  window.ImageDirector={open,note,points};
})();
