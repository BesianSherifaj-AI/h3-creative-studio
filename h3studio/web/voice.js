'use strict';

window.StudioVoice = (() => {
  const engineOptions = [['windows', 'Installed Windows voice'], ['local', 'Local speech server']];
  const speedOptions = [[0.5, '0.5×'], [0.75, '0.75×'], [0.9, '0.9×'], [1, '1×'],
                        [1.15, '1.15×'], [1.25, '1.25×'], [1.5, '1.5×'], [2, '2×']];
  const row = (...parts) => '<div class="row">' + parts.join('') + '</div>';
  const placed = (doc, clip) => doc.soundtracks?.['voice-' + clip.id];
  const fmtTime = seconds => Number(seconds).toFixed(2) + ' s';
  const sourceArgs = options => Object.fromEntries(
    ['shot', 'turn', 'take'].filter(key => options[key]).map(key => [key, options[key]]));

  function pronunciations(value) {
    const entries = [];
    for (const line of value.split('\n').filter(s => s.trim())) {
      const pair = line.split('=').map(s => s.trim());
      if (pair.length !== 2 || pair.some(s => !s)) throw Error('Write each pronunciation as name = spoken form.');
      entries.push(pair);
    }
    return Object.fromEntries(entries);
  }

  async function open(options = {}) {
    const doc = get().doc;
    const project = doc.id;
    const clips = Object.values(doc.narrations || {}).reverse();
    const loaded = options.id ? clips.find(clip => clip.id === options.id) : null;
    const preferred = clips.find(clip => clip.speaker === (options.speaker || 'Narrator'));
    const marker = crypto.randomUUID();
    const wrap = body => '<div data-voice-session="' + marker + '">' + body + '</div>';
    const current = () => get().doc.id === project && $('#dialog').open &&
      !!document.querySelector('[data-voice-session="' + marker + '"]');
    let offset = Number(options.offset || 0);
    let status;
    let draft;

    function library() {
      const libraryClips = Object.values(get().doc.narrations || {}).reverse();
      if (!libraryClips.length) return '';
      return sel('Narration library', 'voiceLibrary',
        [['', 'Write a new clip'], ...libraryClips.map(c => [c.id, c.title + ' · ' + c.speaker])], '');
    }

    function installLibrary(selected) {
      const menu = $('#voiceLibrary');
      if (!menu) return;
      menu.value = selected || '';
      menu.onchange = () => {
        if (menu.value) viewClip(get().doc.narrations[menu.value]);
        else compose(draft || {});
      };
    }

    function compose(seed) {
      draft = seed;
      const engine = seed.engine || preferred?.engine || (status.voices.length ? 'windows' : 'local');
      const savedVoice = seed.voice || preferred?.voice || '';
      const savedModel = seed.model || preferred?.model || '';
      const exampleNames = doc.name.startsWith('Tidal Relay') && /\bTidal\b/.test(seed.text || '') ? {Tidal:'Tide-al'} : {};
      const names = seed.pronunciations ?? preferred?.pronunciations ?? exampleNames;
      const windowsList = status.voices.map(v => [v.name, v.name + ' · ' + v.language]);
      const defaultVoice = windowsList.some(v => v[0] === savedVoice) ? savedVoice : windowsList[0]?.[0];
      const lastLocal = clips.find(c => c.engine === 'local');
      const speechSpeed = Number(seed.rate || preferred?.rate || 1);
      const speeds = speedOptions.some(s => s[0] === speechSpeed) ? speedOptions :
        [...speedOptions, [speechSpeed, speechSpeed + '×']];
      const engineHtml = sel('Speech engine', 'voiceEngine', engineOptions, engine) +
        '<div id="voiceWindows">' +
        (windowsList.length ? sel('Voice', 'voiceName', windowsList, defaultVoice) :
          '<p class="note">' + esc(status.warning || 'No Windows desktop voices are available.') + '</p>') + '</div>' +
        '<div id="voiceLocal">' +
        fld('Local server address', 'voiceEndpoint', seed.endpoint || preferred?.endpoint || lastLocal?.endpoint || 'http://127.0.0.1:8880') +
        btn('voiceFindModels', 'Find installed models') +
        row(fld('Model', 'voiceModel', savedModel || lastLocal?.model || '', 'text', 'list="voiceModelList" placeholder="Server model ID"'),
          fld('Voice', 'voiceServerName', engine === 'local' ? savedVoice : lastLocal?.voice || '', 'text', 'list="voiceServerList" placeholder="Server voice ID"')) +
        '<datalist id="voiceModelList"></datalist><datalist id="voiceServerList"></datalist>' +
        '<p class="note">The server must accept /v1/audio/speech and return a 16-bit PCM WAV.</p></div>';
      modal('Narration', wrap(library() + area('Script', 'voiceText', seed.text || '') +
        row(fld('Clip title', 'voiceTitle', seed.title || '', 'text', 'maxlength="120" placeholder="First line of script"'),
            fld('Speaker', 'voiceSpeaker', seed.speaker || 'Narrator', 'text', 'maxlength="100"')) +
        engineHtml + sel('Speaking speed', 'voiceRate', speeds, speechSpeed) +
        '<details><summary>Pronunciation guide</summary>' + area('Name = spoken form, one per line', 'voicePronounce',
          Object.entries(names).map(pair => pair.join(' = ')).join('\n')) +
        '<p class="note">Keep the written name in captions. For example, Tidal = Tide-al.</p></details>' +
        '<p class="note" id="voiceProgress" role="status"></p>'), [
        {label: 'Generate voice', primary: true, run: async () => {
          const engineName = val('voiceEngine');
          const spec = {
            ...sourceArgs(seed.source || options), text: val('voiceText'), title: val('voiceTitle'),
            speaker: val('voiceSpeaker'), rate: Number(val('voiceRate')), engine: engineName,
            pronunciations: pronunciations(val('voicePronounce')),
            voice: engineName === 'windows' ? $('#voiceName')?.value || '' : val('voiceServerName'),
            ...(engineName === 'local' ? {endpoint: val('voiceEndpoint'), model: val('voiceModel')} : {})
          };
          draft = {...spec, source: sourceArgs(spec)};
          const result = await api('voice_render', spec);
          const note = $('#voiceProgress');
          const cancel = document.createElement('button');
          cancel.type = 'button'; cancel.textContent = 'Cancel generation';
          cancel.onclick = handle(async () => {await api('cancel', {id: result.task}); cancel.disabled = true;});
          note.replaceChildren(document.createTextNode('Generating narration… '), cancel);
          try {
            const clip = await waitTask(result.task);
            if (!current()) return;
            await refresh();
            if (current()) viewClip(clip);
          } finally {
            if (note.isConnected) note.replaceChildren();
          }
        }}
      ]);
      const showEngine = () => {
        const windows = val('voiceEngine') === 'windows';
        $('#voiceWindows').hidden = !windows;
        $('#voiceLocal').hidden = windows;
      };
      $('#voiceEngine').onchange = showEngine;
      on('#voiceFindModels', async () => {
        const address = val('voiceEndpoint');
        const {models} = await api('voice_models', {endpoint: address});
        if (!current() || address !== val('voiceEndpoint')) return;
        if (!models.length) throw Error('The server returned an empty model list.');
        const model = $('#voiceModel');
        const voice = $('#voiceServerName');
        const optionsHtml = entries => entries.map(v => '<option value="' + esc(v) + '"></option>').join('');
        $('#voiceModelList').innerHTML = optionsHtml(models.map(m => m.id));
        const showVoices = () => {
          const available = models.find(m => m.id === model.value)?.voices || [];
          $('#voiceServerList').innerHTML = optionsHtml(available);
          if (available.length && !voice.value) {
            voice.value = available.includes('af_heart') ? 'af_heart' : available[0];
          }
        };
        if (!model.value) model.value = models[0].id;
        model.oninput = showVoices;
        showVoices();
      });
      $('#voiceText').maxLength = 6000;
      installLibrary();
      showEngine();
    }

    function viewClip(clip) {
      const bed = placed(get().doc, clip);
      const spot = bed?.offset ?? offset;
      const actions = [
        {label: 'Edit script', run: () => compose({...clip, source: clip.source})},
        {label: 'Save SRT', run: async () => {
          const safeName = clip.title.replace(/[<>:"/\\|?*\x00-\x1F]/g, '_').slice(0, 85);
          const path = await pick('save', safeName + '.srt');
          if (path) {await api('voice_captions', {id: clip.id, path}); toast('Captions saved.');}
        }},
        {label: bed ? 'Update placement' : 'Add to timeline', primary: true, run: async () => {
          await api('voice_place', {id: clip.id, offset: Number(val('voiceOffset')),
            gain_db: Number(val('voiceGain')), track: Number(val('voiceTrack')) - 1});
          await refresh();
          if (current()) viewClip(clip);
          toast('Narration saved to the film soundtrack.');
        }}
      ];
      modal('Narration', wrap(library() + '<div class="status-card"><strong>' + esc(clip.title) +
        '</strong><small>' + esc([clip.speaker, clip.voice, fmtTime(clip.duration)].join(' · ')) +
        '</small><audio id="voiceAudio" controls preload="metadata" style="width:100%" src="' +
        esc(assetUrl(clip.asset)) + '"></audio></div>' + '<details><summary>Script and caption timings</summary>' +
        clip.cues.map(c => '<p class="note"><b>' + esc(fmtTime(c.start / clip.sample_rate)) +
          '</b> ' + esc(c.text) + '</p>').join('') + '</details>' +
        row(fld('Start in film (s)', 'voiceOffset', spot, 'number', 'min="0" max="86400" step="0.001"'),
            fld('Gain (dB)', 'voiceGain', bed?.gain_db ?? 0, 'number', 'min="-96" max="24" step="0.5"'),
            fld('Output track', 'voiceTrack', (bed?.track ?? 0) + 1, 'number', 'min="1" max="8"')) +
        '<p class="note">' + (bed ? 'Placed on the film soundtrack. Captions follow its saved start time.' :
          'Preview this clip, then place it on the film soundtrack.') + '</p>'), actions);
      installLibrary(clip.id);
    }

    modal('Narration', wrap('<p class="note">Loading installed voices…</p>'));
    const position = options.shot && options.offset == null ? api('cut_view').then(view => {
      const clip = view.clips.find(c => c.shot === options.shot);
      if (clip) offset = clip.start_frame / 24;
    }).catch(() => {}) : Promise.resolve();
    const [available] = await Promise.all([api('voice_list'), position]);
    if (!current()) return;
    status = available;
    if (loaded) viewClip(loaded);
    else compose(options);
  }

  return {open};
})();
