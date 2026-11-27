'use strict';
window.StudioDelivery = (() => {
  const sizes={landscape:[1920,1080],vertical:[1080,1920],square:[1080,1080]};
  function open(initial='landscape'){
    const cut=CutRoom.deliveryContext(),doc=state.doc;
    const shot=doc.shots[cut?.shot]||get().shot;
    const take=cut?cut.take:(get().take?.video_asset?get().take.id:null);
    const still=shot?.guides?.find(g=>g.revision)?.revision;
    const previewTime=cut?.seconds??Math.max(0,state.frame/24);
    const picture=(still?'<img draggable="false" src="'+revUrl(still)+'" alt="Guide framing preview">':'')+
      (take?'<video id="deliveryPreview" preload="auto" muted playsinline hidden aria-label="Take framing preview"></video>':'');
    const mixTracks=Math.max(1,cut?.audioTracks||0,...Object.values(doc.soundtracks||{}).map(b=>1+(b.track||0)),
      ...doc.cut.map(id=>doc.takes[doc.shots[id].selected_take]?.settings?.audio_route?.length||0));
    const body='<div class="delivery-head"><div class="delivery-stage"><div id="deliveryMask">'+picture+'</div></div><div>'+
      sel('Delivery preset','deliveryPreset',[['landscape','Landscape · 16:9'],['vertical','Vertical · 9:16'],['square','Square · 1:1'],['custom','Custom dimensions']],initial)+
      sel('Picture framing','deliveryFraming',[['fit','Fit entire picture'],['fill','Center fill']],initial==='vertical'?'fill':'fit')+'</div></div>'+
      '<details id="deliveryCrop"><summary>Adjust crop position</summary><div class="row">'+
      fld('Horizontal','deliveryX',50,'range','min="0" max="100"')+fld('Vertical','deliveryY',50,'range','min="0" max="100"')+
      '</div><div class="crop-readout"><output id="deliveryPosition"></output>'+btn('resetCrop','Center crop')+'</div></details>'+
      '<div class="row">'+fld('Width','ew',sizes[initial][0],'number','min="2" step="2"')+fld('Height','eh',sizes[initial][1],'number','min="2" step="2"')+'</div>'+
      '<div class="row">'+sel('Frame rate','ef',['24','25','30','30000/1001','60','60000/1001'],'24')+
      sel('Color','ec',[['bt709','Rec. 709 · SDR'],['bt2020-pq','Rec. 2020 PQ · HDR']],'bt709')+'</div>'+
      fld('Output audio tracks','ea',mixTracks,'number','min="0" max="8"')+
      '<small>Includes mix tracks 1 through the number selected. Choose 0 for silent video.</small>'+
      '<label class="caption-toggle"><input type="checkbox" id="deliveryCaptions" checked> Include available narration captions</label>';
    modal('Export film',body,[{label:'Film soundtrack',run:soundtrackDialog},{label:'Choose export file',primary:true,run:async()=>{
      const path=await pick('save',state.doc.name+'.mp4');if(!path)return;
      const r=await api('export',{path,width:+val('ew'),height:+val('eh'),fps:val('ef'),color:val('ec'),audio_tracks:+val('ea'),framing:val('deliveryFraming'),crop_x:+val('deliveryX'),crop_y:+val('deliveryY'),captions:$('#deliveryCaptions').checked});
      closeModal();toast('Exporting film…');await waitTask(r.task);toast('Film exported to '+path);
    }}]);
    function preview(){
      const m=$('#deliveryMask'),w=Math.max(1,+val('ew')),h=Math.max(1,+val('eh'));
      m.style.setProperty('--delivery-aspect',String(w/h));
      const fill=val('deliveryFraming')==='fill',x=val('deliveryX'),y=val('deliveryY');
      $('#deliveryCrop').hidden=!fill;
      m.classList.toggle('can-reframe',fill&&m.children.length>0);
      m.title=fill?'Drag the picture to reframe it, or use Adjust crop position.':'';
      $('#deliveryX').setAttribute('aria-valuetext',x+'%');
      $('#deliveryY').setAttribute('aria-valuetext',y+'%');
      $('#deliveryPosition').textContent=x+'% horizontal · '+y+'% vertical';
      $('#deliveryFraming').querySelector('option[value="fill"]').textContent=x==='50'&&y==='50'?'Center fill':'Fill around subject';
      for(const child of m.children){
        child.style.objectFit=fill?'cover':'contain';
        child.style.objectPosition=fill?x+'% '+y+'%':'center';
      }
    }
    const mask=$('#deliveryMask');
    let drag=null;
    mask.onpointerdown=event=>{
      if(!event.isPrimary||event.button!==0||val('deliveryFraming')!=='fill')return;
      const media=[...mask.children].find(el=>!el.hidden);
      const width=media?.videoWidth||media?.naturalWidth,height=media?.videoHeight||media?.naturalHeight;
      if(!width||!height)return;
      const rect=mask.getBoundingClientRect();
      const scale=Math.max(rect.width/width,rect.height/height);
      drag={id:event.pointerId,startX:event.clientX,startY:event.clientY,x:+val('deliveryX'),y:+val('deliveryY'),
        overflowX:scale*width-rect.width,overflowY:scale*height-rect.height};
      mask.setPointerCapture(event.pointerId);
      event.preventDefault();
    };
    mask.onpointermove=event=>{
      if(drag?.id!==event.pointerId)return;
      const clamp=n=>Math.round(Math.max(0,Math.min(100,n)));
      if(drag.overflowX>=1)$('#deliveryX').value=clamp(drag.x-(event.clientX-drag.startX)*100/drag.overflowX);
      if(drag.overflowY>=1)$('#deliveryY').value=clamp(drag.y-(event.clientY-drag.startY)*100/drag.overflowY);
      preview();
    };
    mask.onlostpointercapture=()=>{drag=null;};
    const player=$('#deliveryPreview');
    if(player){
      const ready=()=>{player.hidden=false;const guide=$('#deliveryMask img');if(guide)guide.hidden=true;};
      player.onloadedmetadata=()=>{
        const t=Math.min(previewTime,Math.max(0,player.duration-.001));
        if(t>.0005)player.currentTime=t;
      };
      player.onseeked=ready;
      player.onloadeddata=()=>{if(!player.seeking)ready();};
      player.src='/take/'+encodeURIComponent(take);
    }
    $('#deliveryPreset').onchange=()=>{const size=sizes[val('deliveryPreset')];if(size){$('#ew').value=size[0];$('#eh').value=size[1];$('#deliveryFraming').value=val('deliveryPreset')==='vertical'?'fill':'fit';}preview();};
    $('#ew').oninput=$('#eh').oninput=()=>{$('#deliveryPreset').value='custom';preview();};
    $('#deliveryX').oninput=$('#deliveryY').oninput=preview;
    $('#resetCrop').onclick=()=>{$('#deliveryX').value=$('#deliveryY').value=50;preview();};
    $('#deliveryFraming').onchange=preview;preview();
  }
  return {open};
})();
