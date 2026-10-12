let config={}, busy=false, serverBlocked=true, lastJob='', lastFinal='', lastState='', lastError='';
let pictures=[], pictureLimit=24, activeJob='', activeFile=null, fileRequest=0;
const thumbnailCache=new Map(), thumbnailQueue=[];let thumbnailActive=0;
const fields=['seconds','image','comfy_url','ollama_url','ollama_model','width','height','steps','seed','headroom_gb','reference_audio','speech_wpm','speech_pause_seconds','unet','clip','video_vae','audio_vae'];
const numbers=new Set(['seconds','width','height','steps','seed','headroom_gb','speech_wpm','speech_pause_seconds']);
const labels={created:'Vorbereitung',planning:'Ollama plant den Clip',encoding:'Prompt und Referenzbild werden vorbereitet',generating:'ComfyUI berechnet die Bewegung',decoding:'Bilder und Ton werden ausgegeben',exporting:'Video wird zusammengesetzt',complete:'Fertig',paused:'Angehalten',error:'Unterbrochen',safety_stopped:'Durch Speicherschutz gestoppt'};
const attemptLabels={waiting:'Warte auf Antwort',received:'Antwort erhalten',accepted:'Gültiger Plan',rejected:'Abgelehnt',request_failed:'Verbindungsfehler'};
const $=id=>document.getElementById(id);
function notice(text,error=false){$('notice').textContent=text;$('notice').classList.toggle('error',error)}
const hardwareFields=['ram_available_gib','vram_gib','gpu_arch','threshold_mode','ram_start_gib','ram_stop_gib','vram_start_gib'];
const modelFields=['unet','clip','video_vae','audio_vae'];
let hardwareData=null, hardwareBlocked=false, hardwareSequence=0, hardwareTimer=null, hardwareLoading=false;
function values(){const c={...config};for(const f of fields)c[f]=numbers.has(f)?Number($(f).value):$(f).value.trim();c.hardware_profile={};for(const f of hardwareFields)c.hardware_profile[f]=['gpu_arch','threshold_mode'].includes(f)?$(f).value:Number($(f).value);c.hardware_profile.headroom_gib=c.headroom_gb;return c}
function modelOptions(c){for(const role of modelFields){const select=$(role);select.replaceChildren();for(const m of hardwareData?.options?.[role]||[]){const o=document.createElement('option');o.value=m.path||m.filename;o.disabled=!m.installed;o.textContent=m.label+' · '+(m.path||'nicht installiert');select.append(o)}if(!Array.from(select.options).some(o=>o.value===c[role])){const o=document.createElement('option');o.value=c[role]||'';o.textContent=(c[role]||'Bitte ein installiertes Modell wählen')+' · gespeichert / ungeprüft';select.append(o)}select.value=c[role]||''}}
function fill(c){config=c;modelOptions(c);for(const f of fields)$(f).value=c[f]??'';for(const f of hardwareFields)$(f).value=c.hardware_profile?.[f]??'';selectedPicture();memoryHint()}
function memoryHint(){const w=Number($('width').value),h=Number($('height').value);if(!(w>0&&h>0))return;const s=Math.min(1,Math.sqrt(736*416/(w*h)),736/Math.max(w,h));const a=Math.max(128,Math.floor(w*s/32)*32),b=Math.max(128,Math.floor(h*s/32)*32);$('memoryHint').textContent=`Neue Projekte werden mit ${a} × ${b} Pixeln erzeugt.${a!==w||b!==h?' Die größere Eingabe wird für das 16-GB-Profil verkleinert.':''}`}
$('width').oninput=memoryHint;$('height').oninput=memoryHint;
function hardwareBusy(){for(const id of [...hardwareFields,...modelFields,'headroom_gb','hardwareSave','hardwareRefresh','reviewPrompts'])$(id).disabled=busy||hardwareLoading;for(const b of document.querySelectorAll('[data-preset]'))b.disabled=busy||hardwareLoading||b.dataset.available!=='true';$('run').disabled=busy||serverBlocked||hardwareBlocked||hardwareLoading}
async function hardwareAttempt(fn){try{await fn()}catch(e){$('hardwareIssues').textContent=e.message}}
function renderHardware(info){
 hardwareData=info;hardwareBlocked=info.issues.length>0;
 for(const f of hardwareFields)$(f).value=info.settings.hardware_profile[f];$('headroom_gb').value=info.settings.headroom_gb;
 modelOptions(values());
 const p=info.settings.hardware_profile;
 $('hardwareBudget').textContent=`Für die Planung: ${info.effective_ram_gib} GiB RAM${info.verified_budget_gib?` · geprüfte Containergrenze: ${info.verified_budget_gib} GiB`:' · Servergrenze noch nicht bestätigt'}. Start: ${p.ram_start_gib} GiB RAM / ${p.vram_start_gib} GiB VRAM frei. RAM-Stopp unter ${p.ram_stop_gib} GiB. GPU-Reserve: ${p.headroom_gib} GiB.${info.gpu_name?' Erkannte GPU: '+info.gpu_name+'.':''}`;
 $('hardwareIssues').textContent=[info.warning,...info.issues].filter(Boolean).join('\n');
 const wasOpen=$('modelRecommendations').querySelector('details')?.open;
 $('modelRecommendations').replaceChildren();const alternatives=document.createElement('details');alternatives.open=!!wasOpen;const summary=document.createElement('summary');summary.textContent='Weitere Modellvarianten vergleichen';alternatives.append(summary);
 for(const r of info.presets){const row=document.createElement('div');row.className='model-option';const top=document.createElement('div');top.className='statusbar';const name=document.createElement('strong');name.textContent=r.title+(r.id===info.recommended?' · Empfehlung':'');if(r.id===info.recommended)name.className='recommended';
  const b=button(r.applicable?'Übernehmen & speichern':r.missing.length?'Nicht installiert':'Budget reicht nicht',()=>hardwareAttempt(async()=>{hardwareLoading=true;hardwareBusy();try{const response=await api('hardware-apply',{settings:values(),preset:r.id});fill(response.settings);$('hardwareMessage').textContent=response.message;await refreshHardware(false)}finally{hardwareLoading=false;hardwareBusy()}}));b.dataset.preset=r.id;b.dataset.available=String(r.applicable);top.append(name,b);
  const detail=document.createElement('p');detail.className='small';detail.textContent=r.detail;const size=document.createElement('p');size.className='hint';size.textContent=`Planungsrichtwert: mindestens ${r.ram_gib} GiB RAM-Budget · ${r.vram_gib} GB VRAM.`;row.append(top,detail,size);
  if(r.missing.length||r.issues.length){const reason=document.createElement('p');reason.className='hint';reason.textContent=[...r.issues,r.missing.length?'Fehlende oder mehrdeutige Dateien: '+r.missing.join(', '):''].filter(Boolean).join(' ');row.append(reason)}(r.id===info.recommended?$('modelRecommendations'):alternatives).append(row)
 }
 $('modelRecommendations').append(alternatives);
 hardwareBusy();
}
async function refreshHardware(refresh=false){const sequence=++hardwareSequence;try{const info=await api('hardware',{settings:values(),refresh});if(sequence===hardwareSequence)renderHardware(info)}catch(e){if(sequence===hardwareSequence){hardwareBlocked=true;$('hardwareIssues').textContent=e.message;hardwareBusy()}}}
function hardwareChanged(manual=false){if(manual)$('threshold_mode').value='manual';hardwareBlocked=true;hardwareSequence++;clearTimeout(hardwareTimer);$('hardwareMessage').textContent='Noch nicht gespeichert. Mit „Profil speichern“ für neue Projekte übernehmen.';hardwareBusy();hardwareTimer=setTimeout(()=>refreshHardware(false),400)}
for(const f of ['ram_available_gib','vram_gib'])$(f).oninput=()=>hardwareChanged();
for(const f of ['ram_start_gib','ram_stop_gib','vram_start_gib','headroom_gb'])$(f).oninput=()=>hardwareChanged(true);
for(const f of ['gpu_arch','threshold_mode',...modelFields])$(f).onchange=()=>hardwareChanged();
$('hardwareRefresh').onclick=()=>hardwareAttempt(async()=>{hardwareLoading=true;hardwareBusy();$('hardwareMessage').textContent='Lese System und installierte Modelle …';try{await refreshHardware(true);$('hardwareMessage').textContent='Prüfung abgeschlossen. Änderungen mit „Profil speichern“ sichern.'}finally{hardwareLoading=false;hardwareBusy()}});
$('hardwareSave').onclick=()=>hardwareAttempt(async()=>{const r=await api('save',{settings:values()});fill(r.settings);$('hardwareMessage').textContent='Hardware, Schwellwerte und Modelle gespeichert.';await refreshHardware(false)});
async function api(path,data){const r=await fetch('/api/'+path,{method:data?'POST':'GET',headers:{'Content-Type':'application/json','X-H3-Token':token},body:data?JSON.stringify(data):undefined});const j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j}
async function attempt(fn){try{await fn()}catch(e){notice(e.message,true)}}
function button(text,fn){const b=document.createElement('button');b.className='secondary';b.textContent=text;b.onclick=fn;return b}
function nextThumbnail(){
 while(thumbnailActive<4&&thumbnailQueue.length){const task=thumbnailQueue.shift();thumbnailActive++;task().finally(()=>{thumbnailActive--;nextThumbnail()})}
}
function thumbnailURL(server,path){
 const key=server+'\n'+path;if(thumbnailCache.has(key))return thumbnailCache.get(key);
 const result=new Promise((resolve,reject)=>{thumbnailQueue.push(async()=>{try{const r=await fetch('/api/image-preview',{method:'POST',headers:{'Content-Type':'application/json','X-H3-Token':token},body:JSON.stringify({comfy_url:server,image:path})});if(!r.ok)throw Error('Vorschau nicht verfügbar');const url=URL.createObjectURL(await r.blob());resolve(url)}catch(e){thumbnailCache.delete(key);reject(e)}})});
 thumbnailCache.set(key,result);nextThumbnail();
 if(thumbnailCache.size>64){const first=thumbnailCache.keys().next().value;const previous=thumbnailCache.get(first);thumbnailCache.delete(first);previous.then(url=>URL.revokeObjectURL(url),()=>{})}
 return result;
}
function loadThumbnail(img,server,path){
 const key=server+'\n'+path;img.dataset.imageKey=key;
 thumbnailURL(server,path).then(url=>{if(img.dataset.imageKey===key){img.src=url;img.classList.remove('hidden')}},()=>{if(img.dataset.imageKey===key)img.onerror?.()});
}
const thumbnailObserver=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting){const img=entry.target;thumbnailObserver.unobserve(img);loadThumbnail(img,img.dataset.server,img.dataset.path)}});
function selectedPicture(){
 const path=$('image').value.trim().replaceAll('\\','/');
 $('selectedPicture').classList.toggle('hidden',!path);$('selectedName').textContent=path;
 if(!path){$('selectedThumbnail').dataset.imageKey='';$('selectedThumbnail').removeAttribute('src');return}
 loadThumbnail($('selectedThumbnail'),$('comfy_url').value,path);
}
$('selectedThumbnail').onerror=()=>$('selectedThumbnail').classList.add('hidden');
$('image').onchange=selectedPicture;$('comfy_url').onchange=()=>{selectedPicture();hardwareData=null;hardwareChanged()};$('ollama_url').onchange=()=>hardwareChanged();
$('check').onclick=()=>attempt(async()=>{notice('Prüfe Verbindungen und Nodes …');$('check').disabled=true;try{const r=await api('check',{settings:values()});fill(r.settings);$('images').replaceChildren(...r.images.map(x=>{const o=document.createElement('option');o.value=x;return o}));notice(r.message)}finally{$('check').disabled=busy}});
$('save').onclick=()=>attempt(async()=>{const r=await api('save',{settings:values()});fill(r.settings);notice(r.message);await refreshHardware(false)});
$('upload').onclick=()=>$('file').click();
$('file').onchange=()=>attempt(async()=>{
 const f=$('file').files[0];if(!f)return;if(f.size>24*1024*1024)throw Error('Bild muss kleiner als 24 MB sein.');
 notice('Lade Startbild zu ComfyUI …');
 const raw=await new Promise((ok,fail)=>{const r=new FileReader;r.onload=()=>ok(r.result.split(',')[1]);r.onerror=fail;r.readAsDataURL(f)});
 const r=await api('upload',{settings:values(),name:f.name,base64:raw});$('image').value=r.image;selectedPicture();
 await api('save',{settings:values()});$('file').value='';notice('Startbild gespeichert und zur Bildauswahl hinzugefügt.');
});
async function loadPictures(){
 $('pictureRefresh').disabled=true;$('pictureMessage').textContent='Lade Bildauswahl …';
 $('pictureGrid').replaceChildren();$('pictureMore').classList.add('hidden');$('pictureCount').textContent='';
 try{const r=await api('images',{settings:values()});pictures=r.images;pictureLimit=24;$('pictureMessage').textContent=r.warning||'';renderPictures()}
 catch(e){pictures=[];$('pictureMessage').textContent=e.message}
 finally{$('pictureRefresh').disabled=false}
}
function renderPictures(){
 thumbnailObserver.disconnect();
 const query=$('pictureSearch').value.toLocaleLowerCase();
 const filtered=pictures.filter(p=>p.path.toLocaleLowerCase().includes(query));
 $('pictureGrid').replaceChildren();$('pictureCount').textContent=filtered.length?`${Math.min(pictureLimit,filtered.length)} von ${filtered.length} Bildern`:'Keine passenden Bilder gefunden.';
 for(const p of filtered.slice(0,pictureLimit)){
  const tile=document.createElement('button');tile.className='picture-tile';tile.title=p.path;tile.setAttribute('aria-pressed',String(p.path===$('image').value));tile.disabled=busy;
  const img=document.createElement('img');img.alt=p.name;img.decoding='async';img.dataset.server=$('comfy_url').value;img.dataset.path=p.path;
  img.onerror=()=>{const missing=document.createElement('span');missing.className='missing';missing.textContent='Vorschau nicht verfügbar';img.replaceWith(missing)};
  const name=document.createElement('span');name.textContent=p.name;const source=document.createElement('small');source.textContent=p.source==='uploaded'?'Hochgeladen':p.source==='used'?'Bereits verwendet':'ComfyUI';name.append(source);tile.append(img,name);
  tile.onclick=async()=>{if(busy)return;tile.disabled=true;try{const c=values();c.image=p.path;await api('save',{settings:c});$('image').value=p.path;selectedPicture();$('picturePicker').close();notice('Startbild ausgewählt und gespeichert.')}catch(e){$('pictureMessage').textContent=e.message;tile.disabled=busy}};
  $('pictureGrid').append(tile);
  thumbnailObserver.observe(img);
 }
 $('pictureMore').classList.toggle('hidden',filtered.length<=pictureLimit);
}
$('picturepick').onclick=()=>{if(busy)return;$('picturePicker').showModal();$('pictureSearch').value='';loadPictures()};
$('pictureClose').onclick=()=>$('picturePicker').close();$('pictureRefresh').onclick=loadPictures;
$('pictureSearch').oninput=()=>{pictureLimit=24;renderPictures()};$('pictureMore').onclick=()=>{pictureLimit+=24;renderPictures()};
$('run').onclick=()=>attempt(async()=>{if(busy)return;const m=$('master').value.trim();if(!m)throw Error('Bitte den gesamten Clip in einer Zeile beschreiben.');$('run').disabled=true;try{const review=$('reviewPrompts').checked;const r=await api('start',{settings:values(),master_prompt:m,review_prompts:review});fill(r.settings);notice(review?'Ollama plant den Clip. Danach kannst du alle Take-Prompts prüfen.':'Automatischer Ablauf mit Speicherprüfung gestartet.');await poll()}catch(e){$('run').disabled=false;throw e}});
$('pause').onclick=()=>attempt(async()=>notice((await api('pause',{})).message));
async function poll(){
 try{
  const s=await api('status');busy=s.running;serverBlocked=s.server_safety?.blocked!==false;$('pause').disabled=!busy;
  $('serverSafety').textContent=serverBlocked?(s.server_safety?.reason||'Server-Speicherschutz noch nicht geprüft.'):(s.server_safety?.warning||'');
  for(const id of ['check','save','upload','picturepick',...fields])$(id).disabled=busy;
  hardwareBusy();
  for(const b of document.querySelectorAll('[data-resume],.picture-tile'))b.disabled=busy||b.dataset.blocked==='true'||(b.dataset.startsJob==='true'&&serverBlocked);
  if(!s.job)return;
  $('progresscard').classList.remove('hidden');$('status').textContent=s.prompt_review_pending?'Ollama-Prompts prüfen':labels[s.status]||s.status;$('reviewPromptsOpen').classList.toggle('hidden',!s.prompt_review_pending);$('count').textContent=`${s.done||0} / ${s.total||0} Takes`;
  $('progress').max=s.total||1;$('progress').value=s.done||0;$('log').textContent=s.messages.join('\n');
  if(s.error&&s.error!==lastError)notice(s.error,true);lastError=s.error||'';
  if(s.job!==lastJob){lastJob=s.job;lastFinal='';$('result').classList.add('hidden');$('video').removeAttribute('src')}
  const state=JSON.stringify([s.job,s.status,s.done,s.error]);if(state!==lastState){lastState=state;await jobs()}
  if(s.final_url&&s.final_url!==lastFinal){showVideo(s.final_url,s.job);notice('Fertig. Dein Video ist bereit.')}
 }catch(e){notice('Verbindung zur lokalen Steuerung unterbrochen. Startfenster prüfen. '+e.message,true)}
}
function showVideo(url,id){lastFinal=url;$('result').classList.remove('hidden');$('resultTitle').textContent='Fertiger Clip · '+id;$('videoError').textContent='';$('resultlink').href=url;$('video').src=url}
$('video').onerror=()=>{$('videoError').textContent='Das Video konnte nicht geladen werden. Prüfe, ob ComfyUI erreichbar ist, und öffne es erneut über die Projektliste. Nach einem Neustart der lokalen Steuerung bitte die Seite neu laden.'};
async function jobs(){
 const list=await api('jobs');$('jobs').replaceChildren();
 if(!list.length){const p=document.createElement('p');p.textContent='Noch keine Projekte.';$('jobs').append(p)}
 for(const j of list){
  const row=document.createElement('div');row.className='job';const p=document.createElement('p');p.textContent=`${j.prompt_review_pending?'Ollama-Prompts prüfen':labels[j.status]||j.status} · ${j.done}/${j.total} Takes · ${j.id}\n${j.master_prompt.slice(0,150)}`;p.style.whiteSpace='pre-wrap';row.append(p);
  const actions=document.createElement('div');actions.className='job-actions';actions.append(button('Logs & Ollama',()=>openJob(j.id)));
  if(j.final_url){actions.append(button('Video anzeigen',()=>{showVideo(j.final_url,j.id);$('result').scrollIntoView({block:'center'})}));const a=document.createElement('a');a.className='linkbutton';a.textContent='Video öffnen';a.href=j.final_url;a.target='_blank';a.rel='noopener';actions.append(a)}
  else if(j.prompt_review_pending){actions.append(button('Prompts prüfen',()=>openJob(j.id)))}
  else{const b=button(j.resume_blocked?'Fortsetzen gesperrt':'Fortsetzen',()=>attempt(async()=>{if(busy||j.resume_blocked)return;b.disabled=true;await api('resume',{job:j.id});notice('Projekt wird fortgesetzt.');await poll()}));b.dataset.resume='';b.dataset.startsJob='true';b.dataset.blocked=String(!!j.resume_blocked);b.disabled=busy||j.resume_blocked||serverBlocked;b.title=j.resume_blocked?'Auflösung zu groß oder Speicherfehler: Servergrenzen prüfen und neues Projekt vorbereiten.':'';actions.append(b)}
  const copy=button('Als neues Projekt vorbereiten',()=>attempt(async()=>{if(busy)return;const r=await api('job-copy',{job:j.id});fill(r.settings);await refreshHardware(false);$('master').value=r.master_prompt;notice('Prompt und Startbild übernommen, Auflösung an das Speicherprofil angepasst. Mit „Gesamten Clip erzeugen“ einen neuen Job starten.');$('master').focus();$('master').scrollIntoView({block:'center'})}));copy.dataset.resume='';copy.disabled=busy;actions.append(copy);
  row.append(actions);$('jobs').append(row);
 }
}
async function openJob(id){
 activeJob=id;fileRequest++;activeFile=null;$('fileViewer').classList.add('hidden');$('promptReview').classList.add('hidden');$('promptReviewList').replaceChildren();$('reviewContinue').classList.add('hidden');$('jobError').textContent='';$('jobLog').textContent='Lade Projektprotokoll …';$('attemptList').replaceChildren();$('attemptHint').textContent='';
 $('jobLogDownload').classList.add('hidden');$('jobPlan').classList.add('hidden');$('jobResources').classList.add('hidden');$('jobSubtitle').textContent=id;
 if(!$('jobDialog').open)$('jobDialog').showModal();await loadJob();
}
async function loadJob(){
 const id=activeJob;$('jobReload').disabled=true;
 try{
  const j=await api('job-details',{job:id});if(id!==activeJob)return;
  $('jobSubtitle').textContent=`${j.id} · ${labels[j.status]||j.status}`;$('jobError').textContent=j.error||'';
  const reviewed=j.prompt_review_prompts||[];const reviewSection=$('promptReview');reviewSection.classList.toggle('hidden',!reviewed.length);$('promptReviewNote').textContent=j.prompt_review_pending?'Prüfe die vollständigen Prompts. ComfyUI hat noch keinen Take gestartet. Fortsetzen bestätigt diese Texte und startet den Clip.':'Die folgenden gespeicherten Texte sind die Prompts, die für die Takes verwendet werden.';
  $('promptReviewList').replaceChildren();for(const item of reviewed){const article=document.createElement('article');article.className='prompt-review-item';const title=document.createElement('h4');title.textContent=`Take ${item.take}`;const text=document.createElement('pre');text.textContent=item.text;article.append(title,text);$('promptReviewList').append(article)}
  const continueButton=$('reviewContinue');continueButton.classList.toggle('hidden',!j.prompt_review_pending);continueButton.disabled=busy||serverBlocked;
  $('jobLog').textContent=j.log||'Für dieses Projekt ist noch kein dauerhafter Verlauf gespeichert.';
  $('jobLogDownload').classList.toggle('hidden',!j.has_log);$('jobPlan').classList.toggle('hidden',!j.has_plan);
  $('jobResources').classList.toggle('hidden',!j.has_resources);
  $('attemptHint').textContent=j.attempts.length?'Jeder Versuch bleibt erhalten, auch abgelehnte Antworten und Verbindungsfehler.':j.legacy?'Dieser Job stammt aus einer älteren Version. Abgelehnte Ollama-Antworten wurden damals nicht gespeichert und können nicht nachträglich angezeigt werden. Beim Fortsetzen werden neue Versuche protokolliert.':'Noch kein Ollama-Versuch gespeichert.';
  $('attemptList').replaceChildren();
  for(const a of j.attempts){const label=`${new Date(a.started_at).toLocaleString('de-AT')} · Versuch ${a.attempt} · ${attemptLabels[a.status]||a.status}`;const b=button(label,()=>showJobFile(id,a.file));b.dataset.file=a.file;b.setAttribute('aria-pressed',String(activeFile?.filename===a.file));$('attemptList').append(b)}
 }catch(e){$('jobError').textContent=e.message}finally{$('jobReload').disabled=false}
}
async function showJobFile(job,filename){
 const sequence=++fileRequest;activeFile=null;$('fileViewer').classList.remove('hidden');$('fileTitle').textContent='Lade Datei …';$('fileContent').textContent='';$('fileRaw').textContent='';$('fileInfo').textContent='';$('fileError').textContent='';$('fileWarnings').textContent='';$('fileDownload').disabled=true;
 try{
  const f=await api('job-file',{job,file:filename});if(sequence!==fileRequest||job!==activeJob)return;activeFile=f;
  const data=filename==='resources.jsonl'?{}:JSON.parse(f.content);$('fileRaw').textContent=f.content;
  $('fileWarnings').textContent=(data.warnings||[]).map(w=>'Zeit-Hinweis: '+w).join('\n');
  if(filename==='resources.jsonl'){$('fileTitle').textContent='RAM, VRAM und Schutzstopps';$('fileInfo').textContent='Verfügbare Speichermengen in GiB. Frühere Jobs haben keine rückwirkenden Messwerte.';$('fileContent').textContent=f.content}
  else if(filename==='plan.json'){$('fileTitle').textContent='Gültiger Plan';$('fileContent').textContent=f.content}
  else{$('fileTitle').textContent=`Ollama · Versuch ${data.attempt} · ${attemptLabels[data.status]||data.status}`;$('fileInfo').textContent=`${data.request.model} · ${new Date(data.started_at).toLocaleString('de-AT')}${data.elapsed_seconds!=null?' · '+data.elapsed_seconds+' s':''}`;$('fileError').textContent=data.validation_error||data.error||'';$('fileContent').textContent=data.response?.message?.content||(data.response?JSON.stringify(data.response,null,2):'Noch keine Antwort empfangen. Mit „Aktualisieren“ den aktuellen Stand laden.')}
  for(const b of $('attemptList').querySelectorAll('button'))b.setAttribute('aria-pressed',String(b.dataset.file===filename));$('fileDownload').disabled=false;
 }catch(e){if(sequence===fileRequest)$('fileError').textContent=e.message}
}
function download(file){const url=URL.createObjectURL(new Blob([file.content],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=activeJob+'_'+file.filename.replaceAll('/','_');a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
$('jobClose').onclick=()=>$('jobDialog').close();
$('reviewPromptsOpen').onclick=()=>{if(lastJob)openJob(lastJob)};
$('reviewContinue').onclick=()=>attempt(async()=>{if(busy||serverBlocked)return;$('reviewContinue').disabled=true;try{await api('resume',{job:activeJob});notice('Prompt-Prüfung bestätigt. Projekt wird fortgesetzt.');$('jobDialog').close();await poll()}catch(e){$('reviewContinue').disabled=false;throw e}});
$('jobReload').onclick=async()=>{const file=activeFile?.filename;await loadJob();if(file)await showJobFile(activeJob,file)};
$('jobLogDownload').onclick=async()=>{try{download(await api('job-file',{job:activeJob,file:'events.jsonl'}))}catch(e){$('jobError').textContent=e.message}};
$('jobPlan').onclick=()=>showJobFile(activeJob,'plan.json');$('fileDownload').onclick=()=>{if(activeFile)download(activeFile)};
$('jobResources').onclick=()=>showJobFile(activeJob,'resources.jsonl');
$('refresh').onclick=()=>attempt(jobs);
const polling=setInterval(poll,3000);
$('shutdown').onclick=()=>attempt(async()=>{const r=await api('shutdown',{});clearInterval(polling);notice(r.message);for(const b of document.querySelectorAll('button'))b.disabled=true});
attempt(async()=>{fill(await api('settings'));await jobs();await poll();await refreshHardware(true)});
