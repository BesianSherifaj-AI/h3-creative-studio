'use strict';

(() => {
  const kinds = ['video', 'image'];
  const names = {video:'H3 video', image:'AI image'};
  const connections = {
    video_url:['videoUrl', 'H3 video · ComfyUI URL'],
    image_url:['imageUrl', 'Image editor · ComfyUI URL'],
    director_url:['lmUrl', 'Director · LM Studio URL'],
    director_model:['lmModel', 'Director · exact model identifier'],
    finishing_install:['finishPath', 'UPSCALE installation folder']
  };
  const id = name => document.getElementById(name);
  const safe = value => window.H3.esc(value);
  const field = (label, name, value, placeholder='', number=false) =>
    '<label class="field">'+safe(label)+'<input type="'+(number?'number':'text')+'" id="'+name+
    '" value="'+safe(value)+'" placeholder="'+safe(placeholder)+'" '+(number?'min="1024" max="65535"':'')+'></label>';

  function profileCard(kind, data) {
    const {profile, detected} = data;
    const input = (label,key) => field(label,kind+'-'+key,profile[key],detected[key],key==='port');
    return '<section class="status-card"><strong>'+names[kind]+'</strong><p class="note" role="status" id="'+kind+'-status"></p>'+
      '<button type="button" id="'+kind+'-start">Start and use</button> <button type="button" id="'+kind+'-stop">Stop</button>'+
      '<details><summary>Installation and log</summary>'+
      input('Loopback port','port')+input('ComfyUI code folder','source')+input('Python executable','python')+
      (kind==='video'?input('Custom nodes folder','custom_nodes'):'')+
      '<label class="field">Model-path YAML files · one per line<textarea id="'+kind+'-models" placeholder="'+
      safe(detected.model_configs.join('\n'))+'">'+safe((profile.model_configs||[]).join('\n'))+'</textarea></label>'+
      '<small id="'+kind+'-files"></small><small id="'+kind+'-log"></small></details></section>';
  }
  function readProfile(kind) {
    const input = key => id(kind+'-'+key)?.value.trim() || '';
    const config = {};
    for (const key of ['source','python','custom_nodes']) if(input(key)) config[key]=input(key);
    if(input('port')) config.port=Number(input('port'));
    const files=input('models').split(/\r?\n/).map(path=>path.trim()).filter(Boolean);
    if(files.length) config.model_configs=files;
    return config;
  }
  function drawStatus(kind, data) {
    const {status:s, detected:d} = data;
    const devices=s.device?' · '+s.device:'';
    const guard=s.guard?.length?' · Game guard: '+s.guard.join(', '):'';
    id(kind+'-status').textContent=s.url+' · '+s.detail+devices+guard;
    id(kind+'-start').disabled=Boolean(s.guard?.length)||['starting','occupied','stopping'].includes(s.state);
    id(kind+'-stop').disabled=!s.owned;
    id(kind+'-files').textContent=d.missing.length?'Missing: '+d.missing.join(', '):'Installed code and model paths found.';
    id(kind+'-log').textContent='Worker log: '+s.log;
  }
  function control(key, task) {
    const element=id(key);
    element.onclick=async()=>{
      element.disabled=true;
      try { await task(); }
      catch(e) { id('dialogError').textContent=e.message; }
      finally { if(element.isConnected && element.dataset.enable!=='status') element.disabled=false; }
    };
  }
  async function openEngines() {
    const ui=window.H3;
    const data=await ui.api('engine_status');
    const active=Object.entries(connections).map(([key,[input,label]])=>field(label,input,data.connections[key])).join('');
    ui.modal('Local engines','<div id="localEngineControls">'+kinds.map(k=>profileCard(k,data.engines[k])).join('')+
      '<details open><summary>Connections</summary>'+active+'<button type="button" id="pickFinish">Choose UPSCALE folder</button></details></div>',[
      {label:'Refresh status',run:async()=>{await refreshStatus();}},
      {label:'Save settings',primary:true,run:async()=>{
        await ui.api('engine_config',{profiles:Object.fromEntries(kinds.map(k=>[k,readProfile(k)])),
          connections:Object.fromEntries(Object.entries(connections).map(([key,[input]])=>[key,id(input).value.trim()]))});
        await ui.refresh();ui.closeModal();ui.toast('Local engine settings saved.');
      }}
    ]);
    const box=id('localEngineControls');
    let busy=false;
    async function refreshStatus(){
      if(!box.isConnected||!box.closest('dialog')?.open||busy) return;
      busy=true;
      try {
        const fresh=await ui.api('engine_status');
        if(box.isConnected) for(const kind of kinds) drawStatus(kind,fresh.engines[kind]);
      } finally {busy=false;}
    }
    function poll(){
      if(!box.isConnected||!box.closest('dialog')?.open) return;
      refreshStatus().catch(()=>{}).finally(()=>setTimeout(poll,5000));
    }
    for(const kind of kinds) {
      drawStatus(kind,data.engines[kind]);
      for(const action of ['start','stop']){
        const key=kind+'-'+action;
        id(key).dataset.enable='status';
        control(key,async()=>{
          try {
            if(action==='start') await ui.api('engine_config',{kind,profile:readProfile(kind)});
            const result=await ui.api(action+'_engine',{kind});
            if(action==='start'&&result.owned&&['ready','starting'].includes(result.state)){
              id(connections[kind+'_url'][0]).value=result.url;
              await ui.refresh();
            }
            ui.toast(result.detail);
          } finally {
            await refreshStatus();
          }
        });
      }
    }
    control('pickFinish',async()=>{const path=await ui.pick('folder');if(path)id('finishPath').value=path;});
    setTimeout(poll,5000);
  }
  window.openEngines=openEngines;
})();
