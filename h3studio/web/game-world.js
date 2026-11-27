'use strict';
window.GameWorld=(()=>{
  const button=(key,label,value='')=>'<button type="button" data-gw="'+key+'" value="'+esc(value)+'">'+esc(label)+'</button>';
  const options=rows=>rows.map(([id,s])=>'<option value="'+esc(id)+'">'+esc(s)+'</option>').join('');
  const select=(label,id,rows)=>'<label class="dr-field"><span>'+esc(label)+'</span><select id="'+id+'">'+options(rows)+'</select></label>';
  const input=(label,id,value='',long=false)=>'<label class="dr-field"><span>'+esc(label)+'</span>'+
    (long?'<textarea rows="2" id="'+id+'">'+esc(value)+'</textarea>':'<input id="'+id+'" value="'+esc(value)+'">')+'</label>';
  const chips=items=>items.map(s=>'<span class="gw-tag">'+esc(s)+'</span>').join('');
  function controls(v){
    const c=v.context,player=c.actor;
    const local=v.actions.targets,others=local.filter(t=>t.kind==='character');
    return '<div class="gw-overview"><div><small>Playing as</small><strong>'+esc(player.name)+'</strong></div>'+
      '<div><small>Location</small><strong>'+esc(c.location?.name||'Unassigned')+'</strong></div>'+
      '<div><small>Inventory</small><strong>'+esc(v.inventory.map(e=>e.name).join(', ')||'Empty')+'</strong></div></div>'+
      (c.objectives.length?'<p class="gw-objectives">'+esc(c.objectives.map(o=>o.name+' · '+o.status).join(' / '))+'</p>':'')+
      '<div class="gw-cols"><section><h3>Move through the scene</h3><div class="gw-fields">'+
      select('Move','gwSubject',[['player',player.name],['camera','Camera only']])+select('Distance','gwExtent',[['step','One step'],['nearby','Several paces']])+
      select('Pace','gwSpeed',[['normal','Natural'],['slow','Slow'],['fast','Fast']])+select('Presentation','gwPresentation',[['continuous','Continuous'],['cut','Cut']])+'</div>'+
      '<div class="gw-directions">'+['left','forward','backward','right'].map((d,i)=>button('move',['← Left','↑ Forward','↓ Back','Right →'][i],d)).join('')+'</div>'+
      '<small>Relative steps · player ('+v.navigation.player.join(', ')+') · camera ('+v.navigation.camera.join(', ')+')</small>'+
      (local.length?select('Destination','gwTarget',local.map(t=>[t.id,t.name+(t.kind==='location'?' · exit':'')]))+button('approach','Go to destination'):'')+'</section>'+
      '<section><h3>Interact</h3>'+select('Player action','gwAction',v.actions.actions.map((a,i)=>[String(i),a.label+(a.enabled?'':' · '+a.reason)]))+
      (others.length?select('Give to','gwRecipient',others.map(t=>[t.id,t.name])):'')+button('act','Ask Director for this action')+
      '<div class="gw-context"><small>In sight</small>'+chips(local.filter(t=>t.kind!=='location').map(t=>t.name))+'</div>'+
      (c.rules.length?'<small>World rules</small><p>'+esc(c.rules.join(' · '))+'</p>':'')+'</section></div>'+
      '<details class="gw-edit"><summary>Edit character and world</summary><div class="gw-fields">'+
      select('Change','gwEdit',[['identity','Edit player identity'],['player','Switch player'],['character','Add a character'],['location','Add a connected place'],
                              ['item','Place an inventory item'],['objective','Add an objective'],['rules','Edit world rules']])+
      '<div id="gwEditor"></div></div>'+button('save','Save as playable branch')+'</details>';
  }
  function showEditor(v){
    const k=val('gwEdit'),actor=v.context.actor;
    $('#gwEditor').innerHTML=k==='player'?select('Play as','gwCast',v.cast.map(c=>[c.id,c.name])):
      k==='rules'?input('Rules · one per line','gwDescription',v.context.rules.join('\n'),true):
      input('Name','gwName',k==='identity'?actor.name:'')+input('Description','gwDescription',k==='identity'?actor.description:'',true)+
      (k==='identity'?input('Personality','gwPersonality',actor.personality)+input('Speaking style','gwVoice',actor.speaking_style):'');
    if(k==='player')$('#gwCast').value=actor.id;
  }
  async function mount(el,turn,submit,onEdit){
    const v=await api('director_world',{turn});
    if(!el.isConnected)return;
    el.innerHTML=controls(v);
    showEditor(v);
    const activeAction=()=>v.actions.actions[Number(val('gwAction'))];
    function actionState(){
      const a=activeAction(),submit=el.querySelector('[data-gw="act"]'),r=$('#gwRecipient');
      submit.disabled=!a.enabled || (a.kind==='give'&&!r);
      submit.title=!a.enabled?a.reason:a.kind==='give'&&!r?'Another character must be here to receive the item.':'';
      if(r)r.closest('label').hidden=a.kind!=='give';
    }
    actionState();
    el.onchange=e=>{if(e.target.id==='gwEdit')showEditor(v);if(e.target.id==='gwAction')actionState();};
    el.onclick=handle(async e=>{
      const b=e.target.closest('[data-gw]');if(!b||b.disabled)return;
      b.disabled=true;
      try{
        const key=b.dataset.gw,get=id=>$('#'+id)?.value;
        if(key==='save'){
          const data={turn,edit:val('gwEdit'),revision:v.revision,request_id:crypto.randomUUID()};
          for(const [field,id] of Object.entries({name:'gwName',description:'gwDescription',player:'gwCast',voice:'gwVoice',personality:'gwPersonality'}))
            if(get(id)!==undefined)data[field]=get(id);
          const {turn:newTurn}=await api('director_world_edit',data);await onEdit(newTurn);return;
        }
        const move={kind:'move',camera:val('gwSubject'),speed:val('gwSpeed'),presentation:val('gwPresentation'),extent:val('gwExtent')};
        let intent;
        if(key==='move')intent={...move,direction:b.value};
        if(key==='approach'){
          const t=v.actions.targets.find(t=>t.id===val('gwTarget'));
          intent={...move,extent:t.kind==='location'?'travel':'nearby',target_id:t.id};
        }
        if(key==='act'){
          const a=activeAction();intent={kind:a.kind,target_id:a.target_id};
          if(a.kind==='give')intent.recipient_id=get('gwRecipient');
        }
        if(intent)await submit({parent:turn,world_revision:v.revision,game_intent:intent});
      }finally{b.disabled=false;}
    });
  }
  return {mount};
})();
