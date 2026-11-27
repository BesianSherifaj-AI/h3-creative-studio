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
      '<button type="button" id="'+kind+'-start">Start and use</button> <button type="button" id="'+kind+'-stop">Stop</button> <button type="button" id="'+kind+'-requirements">Check models</button><div class="model-check" id="'+kind+'-report"></div>'+
      '<details><summary>Installation and log</summary>'+
      '<button type="button" id="'+kind+'-browse">Choose ComfyUI installation folder</button>'+
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
    const active=Object.entries(connections).map(([key,[input,label]])=>field(label,input,data.connections[key])+
      (key==='director_model'?'<button type="button" id="discoverModels">Find models in LM Studio</button><div id="lmChoices"></div>':'')).join('');
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
      control(kind+'-requirements',async()=>{
        const result=id(kind+'-report');
        result.textContent='Reading the renderer model list…';
        const details=await ui.api('check_models',{kind,url:id(connections[kind+'_url'][0]).value.trim()});
        const list=details.recipes.map(r=>{
          const absent=r.files.filter(f=>f.installed===false).length+r.missing_nodes.length;
          const status=details.connected?(absent?' · '+absent+' missing':' · found'):'';
          const files=r.files.map(f=>{
            const size=f.bytes>=1e9?(f.bytes/1e9).toFixed(1)+' GB':Math.ceil(f.bytes/1e6)+' MB';
            const publisher=f.bytes?' <button type="button" data-model-file="'+safe(f.name)+'">Publisher · '+size+' ↗</button>':'';
            return '<li>'+(!details.connected?'':f.installed?'✓ ':'Missing · ')+'<code>models/'+safe(f.folder)+'/'+safe(f.name)+'</code>'+publisher+'</li>';
          });
          if(r.missing_nodes.length)files.push('<li>Missing nodes · '+safe(r.missing_nodes.join(', '))+'</li>');
          return '<details><summary>'+safe(r.title+status)+'</summary><ul>'+files.join('')+'</ul></details>';
        }).join('');
        result.innerHTML='<p>'+safe(details.connected?'Model files and nodes reported by '+details.url:'Renderer offline. Open a recipe to see the required files and their ComfyUI folders.')+'</p>'+list+'<button type="button" class="model-source">Installation guides and model sources ↗</button>';
        result.querySelectorAll('[data-model-file]').forEach(button=>{button.onclick=()=>window.pywebview?.api?.open_model_file(button.dataset.modelFile);});
        result.querySelector('.model-source').onclick=()=>window.pywebview?.api?.open_guide(kind==='image'?'image':'community');
      });
      control(kind+'-browse',async()=>{const path=await ui.pick('folder');if(path)id(kind+'-source').value=path;});
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
    control('discoverModels',async()=>{
      const {models}=await ui.api('director_models',{url:id('lmUrl').value.trim()});
      const host=id('lmChoices');host.replaceChildren();
      if(!models.length){host.textContent='Start the LM Studio local server and make a model available.';return;}
      const list=document.createElement('select');list.setAttribute('aria-label','Available LM Studio models');list.add(new Option('Choose a model…',''));
      models.forEach(m=>list.add(new Option(m,m)));if(models.includes(id('lmModel').value))list.value=id('lmModel').value;
      list.onchange=()=>{if(list.value)id('lmModel').value=list.value;};host.append(list);
    });
    control('pickFinish',async()=>{const path=await ui.pick('folder');if(path)id('finishPath').value=path;});
    setTimeout(poll,5000);
  }
  window.openEngines=openEngines;
})();
