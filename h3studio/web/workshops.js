'use strict';
window.StudioWorkshops = (() => {
  async function launch(key) {
    const target=await api('example_open',{lesson:key});
    closeModal();await refresh();
    if(target.shot && state.doc.shots[target.shot]){
      state.shotId=target.shot;state.frame=0;state.view='canvas';state.layerIndex=0;
      const guide=get().shot.guides.find(g=>g.revision);
      if(guide)state.compId=state.doc.revisions[guide.revision].composition;
    }
    const room={voice:'edit',delivery:'edit',assistant:'motion'}[key]||key;
    await StudioShell.selectRoom(room,target.shot);
    if(key==='voice')await StudioVoice.open({text:'Across the water, a light answers.',speaker:'Mara',shot:target.shot});
    if(key==='delivery')StudioDelivery.open('vertical');
    if(key==='assistant')await StudioAssistant.open();
    if(key==='game')document.querySelector('[data-dr=playScene]')?.click();
  }
  async function open() {
    const lessons=await api('example_list');
    const cards=lessons.map(l=>'<button type="button" class="lesson-card" data-lesson="'+esc(l.id)+'"><img alt="" src="'+esc(l.image)+'"><span><small>'+esc(l.room)+'</small><strong>'+esc(l.title)+'</strong><span>'+esc(l.detail)+'</span></span></button>').join('');
    modal('Editable examples','<p class="lesson-intro">Each lesson opens its own local copy of Tidal Relay and keeps your edits when you return.</p><div class="lesson-grid">'+cards+'</div>',[{label:'Open original example film',run:async()=>{await api('sample_open');closeModal();await refresh();await StudioShell.selectRoom('motion');}}]);
    const grid=$('.lesson-grid');
    grid.onclick=handle(async event=>{
      const card=event.target.closest('[data-lesson]');
      if(!card||grid.getAttribute('aria-busy')==='true')return;
      grid.setAttribute('aria-busy','true');
      const buttons=[...grid.querySelectorAll('button'),...$('#dialogFoot').querySelectorAll('button')];
      buttons.forEach(button=>button.disabled=true);
      try{await launch(card.dataset.lesson);}
      finally{grid.removeAttribute('aria-busy');buttons.forEach(button=>button.disabled=false);}
    });
  }
  return {open};
})();
