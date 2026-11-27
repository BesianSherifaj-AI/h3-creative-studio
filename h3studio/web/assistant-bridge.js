'use strict';

window.StudioAssistant = (() => {
  const title = 'AI clients';

  function reviewList(items) {
    if (!items.length) return '<p class="note">New proposals appear here. Film and Game also show drafts in their Director inbox.</p>';
    return items.map(item => {
      const shot = item.kind === 'propose_shot_draft';
      const choice = item.kind === 'propose_game_choice';
      const voice = item.kind === 'propose_voice_script';
      const room = voice ? 'Voice' : shot ? 'Motion' : item.kind === 'propose_film' ? 'Film' : 'Game';
      const label = voice ? 'Use in Voice' : shot ? 'Apply draft' : choice ? 'Play choice' : 'Review in ' + room;
      const excerpt = item.script ? item.script.speaker + ' · ' + item.script.text : item.prompt || '';
      const detail = excerpt ? '<p class="dr-prose">' + esc(excerpt) + '</p>' : '';
      const guide = item.guide ? '<p class="note">Timed image guide · frame ' + esc(item.guide.frame) +
        ' · revision ' + esc(item.guide.revision) + '. Replaces the timed image at this frame.</p>' : '';
      return '<section class="dr-inbox"><small>' + room + '</small><h3>' + esc(item.title) + '</h3>' + detail + guide +
        '<div class="dr-actions"><button type="button" data-ai-review="' + esc(item.id) + '">' + label + '</button>' +
        '<button type="button" data-ai-dismiss="' + esc(item.id) + '">Dismiss</button></div></section>';
    }).join('');
  }

  async function open() {
    const info = await api('assistant_status');
    const timeLabel = stamp => new Date(stamp * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    const instructions = info.active
      ? '<p class="note">In your AI client, add a local MCP server using the command and arguments below. This pairing expires at ' +
        esc(timeLabel(info.expires)) + ' or when you close or switch projects.</p><p class="note">' +
        (info.last_seen ? 'Last client activity at '+esc(timeLabel(info.last_seen))+'.' : 'Waiting for the client to discover Studio tools.') + '</p>'
      : '<p class="note">Connect a local MCP client to this project. It can inspect the edit, read a Game world and help prepare creative work.</p>';
    const config = info.active
      ? '<label class="field">MCP client configuration<textarea id="aiClientConfig" rows="5" spellcheck="false" readonly>' +
        esc(JSON.stringify(info.config, null, 2)) + '</textarea></label><div class="dr-actions"><button type="button" id="aiCopy">Copy configuration</button></div>'
      : sel('Permission','aiScope',[['read','Read project'],['propose','Read project and propose drafts']],'read') +
        '<p class="note">You review each proposed change in Studio. Render and export remain under your control.</p>';
    const buttons = [
      {label:'Refresh inbox',run:open},
      info.active ? {label:'Disconnect client',run:async()=>{await api('assistant_revoke'); await open();}}
                  : {label:'Enable connection',primary:true,run:async()=>{await api('assistant_enable',{scope:$('#aiScope').value}); await open();}}
    ];
    const saved = info.scripts.length ? '<details><summary>Accepted voice scripts (' + info.scripts.length + ')</summary>' +
      info.scripts.map((s,i)=>'<p><button type="button" data-ai-script="'+i+'">'+esc(s.title)+' · '+esc(s.speaker)+'</button></p>').join('') + '</details>' : '';
    modal(title,'<small>'+esc(info.name)+'</small><h3>'+(info.active ? (info.scope==='read'?'Read access enabled':'Reviewable drafts enabled') : 'Pair a local client')+'</h3>'+
      instructions+config+'<h3>Proposals for review</h3>'+reviewList(info.pending)+saved,buttons);
    document.querySelectorAll('[data-ai-script]').forEach(b=>b.onclick=handle(()=>window.StudioVoice.open(info.scripts[Number(b.dataset.aiScript)])));
    on('#aiCopy',async()=>{
      const el=$('#aiClientConfig');
      try { await navigator.clipboard.writeText(el.value); toast('Client configuration copied.'); }
      catch { el.focus(); el.select(); toast('Configuration selected. Press Ctrl+C to copy.'); }
    });
    document.querySelectorAll('[data-ai-review],[data-ai-dismiss]').forEach(b=>b.onclick=handle(async()=>{
      const key=b.dataset.aiReview||b.dataset.aiDismiss;
      const item=info.pending.find(entry=>entry.id===key);
      if (!item) return;
      if (b.dataset.aiReview && ['propose_film','propose_game_turn'].includes(item.kind)) {
        await refresh();
        if (get().doc.id !== info.project || state.folder !== info.folder) throw Error('Reopen AI clients for this project.');
        return window.DirectorRoom.review(key);
      }
      b.disabled=true;
      try {
        const decision=await api('assistant_decide',{project:info.project,project_folder:info.folder,
          proposal:key,decision:b.dataset.aiDismiss?'dismiss':'approve'});
        await refresh();
        if (get().doc.id !== info.project || state.folder !== info.folder) throw Error('Reopen AI clients for this project.');
        if (b.dataset.aiReview && decision.status==='approved') {
          if (item.kind==='propose_game_choice') return window.DirectorRoom.playTurn(decision.result.turn,info.project,info.folder);
          if (item.script) return window.StudioVoice.open(item.script);
        }
        await open();
      } finally { b.disabled=false; }
    }));
  }
  return {open};
})();
