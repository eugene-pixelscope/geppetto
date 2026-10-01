const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const terminal=r=>['succeeded','failed','stopped','skipped'].includes(r.state);
export async function mountGradcam(host,run,api){
 const base=`runs/${encodeURIComponent(run.id)}/gradcam`;
 let data,offset=0,index=null,preview=null,settings=null,generation=0,timer;
 host.innerHTML='<section class="card"><h2>Grad-CAM · Model attention</h2><p role="status">Checking weights and execution environment…</p></section>';
 try{data=await api(base);}catch(e){host.querySelector('p').textContent=e.message;return;}
 if(!host.isConnected)return;
 host.innerHTML=`<section class="card"><h2>Grad-CAM · Model attention</h2><p>Backpropagate the detection score for the selected image to show positive contributions. Runs on one GPU through the Kubernetes agent.</p><p class="footer-note">This explains model contributions, not ground truth or error frequency. Colors are normalized per image, so absolute importance cannot be compared across images. Low-scoring candidates can also be analyzed; this does not imply a successful detection.</p><div class="analysis-controls"><label>Weights<select data-cam="checkpoint">${data.checkpoints.map(a=>`<option value="${esc(a.id)}">${esc(a.path)}</option>`).join('')}</select></label><label>Execution server<select data-cam="profile">${data.profiles.map(p=>`<option value="${esc(p.id)}">${esc(p.node_name)} · ${esc(p.template_id)}</option>`).join('')}</select></label><label>Dataset version<select data-cam="dataset">${data.datasets.map(d=>`<option value="${esc(d.id)}">${esc(d.name)} · ${esc(d.version.slice(0,8))}</option>`).join('')}</select></label><label>Split<select data-cam="split"><option value="validation">Validation</option><option value="test">Test</option></select></label><label>Input size<select data-cam="size">${[640,960,1280,1920].map(s=>`<option ${s===960?'selected':''}>${s}</option>`).join('')}</select></label><label>Detection candidate rank<input data-cam="rank" type="number" value="1" min="1" max="20" step="1"></label></div><p class="footer-note">Ball class · Ranked by score. YOLO11 selects post-NMS candidates; YOLO26 selects one-to-one candidates.</p><div class="gradcam-review-action"><button data-cam="preview">Review selected image and weights</button><div data-cam="review"></div></div><div data-cam="samples" class="samples gradcam-samples"></div><div class="controls gradcam-pagination"><button data-cam="prev" class="secondary">Previous image</button><span data-cam="page"></span><button data-cam="next" class="secondary">Next image</button></div><p data-cam="message" role="status"></p><div data-cam="results"></div></section>`;
 const q=name=>host.querySelector(`[data-cam="${name}"]`),message=q('message'),pageSize=()=>matchMedia('(max-width:760px)').matches?2:6;
 function invalidate(){preview=null;settings=null;q('review').innerHTML='';}
 if(!data.checkpoints.length||!data.profiles.length||!data.datasets.length){message.textContent='Requires completed inference weights, an active agent execution server, and a verified dataset version.';q('preview').disabled=true;}
 async function samples(){
  invalidate();const seq=++generation;index=null;
  q('samples').innerHTML='';if(!q('dataset').value)return;
  try{const page=await api(`datasets/${encodeURIComponent(q('dataset').value)}/samples?split=${q('split').value}&offset=${offset}&limit=${pageSize()}`);
   if(!host.isConnected||generation!==seq)return;
   q('samples').innerHTML=page.items.map(s=>`<button type="button" class="card cam-sample" value="${s.index}" aria-pressed="false" aria-label="Select ${esc(s.image.split('/').pop())}"><img class="preview-image" loading="lazy" src="/api/datasets/${encodeURIComponent(q('dataset').value)}/samples/${s.index}/image" alt=""><span class="footer-note">${esc(s.image.split('/').pop())}</span><span class="cam-selected" aria-hidden="true">✓</span></button>`).join('')||'<p>No images in this split.</p>';
   const size=pageSize();q('page').textContent=`${page.total?offset+1:0}–${Math.min(offset+size,page.total)} / ${page.total}`;q('prev').disabled=offset===0;q('next').disabled=offset+size>=page.total;
   q('samples').querySelectorAll('.cam-sample').forEach(el=>el.onclick=()=>{q('samples').querySelectorAll('.cam-sample').forEach(button=>button.setAttribute('aria-pressed','false'));el.setAttribute('aria-pressed','true');index=Number(el.value);invalidate();});
  }catch(e){message.textContent=e.message;}
 }
 ['dataset','split'].forEach(k=>q(k).onchange=()=>{offset=0;samples();});
 ['checkpoint','profile','size','rank'].forEach(k=>q(k).onchange=invalidate);
 q('prev').onclick=()=>{offset=Math.max(0,offset-pageSize());samples();};q('next').onclick=()=>{offset+=pageSize();samples();};
 q('preview').onclick=async()=>{
  if(index===null){message.textContent='Select an image.';return;}
  if(!q('rank').reportValidity())return;
  const seq=generation;settings={checkpoint_id:q('checkpoint').value,profile_id:q('profile').value,dataset_id:q('dataset').value,index,imgsz:Number(q('size').value),target_rank:Number(q('rank').value)};
  const requestSettings=settings;q('preview').disabled=true;message.textContent='Verifying checkpoint and image content hashes…';
  try{const result=await api(base+'/preview',requestSettings);if(!host.isConnected||generation!==seq||settings!==requestSettings)return;preview=result;
   const p=preview.frozen.evaluation;q('review').innerHTML=`<p>${esc(p.sample.image)} · Input ${settings.imgsz} · Candidate ${settings.target_rank} · 1 GPU</p><p class="footer-note">Weights SHA-256 ${esc(p.checkpoint_sha256)}<br>Image SHA-256 ${esc(p.sample.image_file.sha256)}</p><button data-cam="submit">Compute Grad-CAM</button>`;message.textContent='Image and weights verified.';
   const key=crypto.randomUUID();q('submit').onclick=async()=>{q('submit').disabled=true;try{const created=await api(base,{...requestSettings,confirmation_hash:result.confirmation_hash},'POST',{'Idempotency-Key':key});message.textContent='Grad-CAM job submitted.';invalidate();await refreshResults(created.id);}catch(e){message.textContent=e.message;if(q('submit'))q('submit').disabled=false;}};
  }catch(e){message.textContent=e.message;}finally{if(host.isConnected)q('preview').disabled=false;}
 };
 let chosen=null,resultGeneration=0;
 async function show(r){
  chosen=r.id;const seq=++resultGeneration,view=q('result-view');
  const artifact=name=>r.artifacts.find(a=>a.path===name&&a.complete);
  const url=a=>`/api/runs/${encodeURIComponent(r.id)}/artifacts/${encodeURIComponent(a.id)}?download=false`;
  view.innerHTML=`<p>${esc(r.name)} · ${esc(r.state)}${r.resource_released?' · GPU released':''}</p>`;
  if(!terminal(r)){view.insertAdjacentHTML('beforeend','<p>The agent is computing the result. Results refresh automatically.</p>');return;}
  if(r.state!=='succeeded'){view.insertAdjacentHTML('beforeend',`<pre class="log">${esc((r.logs||[]).slice(-20).join('\n')||'Check the run details for errors.')}</pre>`);return;}
  const source=artifact('gradcam-source.png'),heat=artifact('gradcam-heatmap.png'),meta=artifact('gradcam.json');
  if(!source||!heat||!meta){view.insertAdjacentHTML('beforeend','<p>No completed Grad-CAM result file.</p>');return;}
  try{const response=await fetch(url(meta));if(!response.ok)throw Error('Could not load Grad-CAM metadata.');const m=await response.json();if(!host.isConnected||seq!==resultGeneration)return;
   const [w,h]=m.original_size,[x1,y1,x2,y2]=m.box_xyxy;
   view.insertAdjacentHTML('beforeend',`${!m.has_positive_signal?'<p class="notice">No positive Grad-CAM signal. Showing the original image only.</p>':''}<p>Target ${esc(m.class_name)} · Candidate ${m.rank} · Score ${(m.score*100).toFixed(2)}% · ${esc(m.layer)}</p><label>Heatmap opacity <input data-cam="opacity" type="range" min="0" max="100" value="70"> <span data-cam="opacity-value">70%</span></label><div class="sample-frame cam-frame"><img class="preview-image" src="${url(source)}" alt="Grad-CAM original image"><img data-cam="heat" class="cam-heat" src="${url(heat)}" style="opacity:.7" alt="Grad-CAM positive contributions"><svg class="label-overlay" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-label="Selected detection candidate"><rect x="${x1}" y="${y1}" width="${x2-x1}" height="${y2-y1}" fill="none" stroke="#00bfff" stroke-width="2" vector-effect="non-scaling-stroke"/></svg></div><p class="footer-note">Yellow → Red: Increasing positive contribution · Cyan box: Target candidate · ${esc(m.branch)} · Based on class logit</p><div class="controls">${['gradcam-overlay.png','gradcam-values.npy','gradcam.json'].map(name=>{const a=artifact(name);return a?`<a class="button secondary" href="${url(a).replace('download=false','download=true')}">${esc(name)} download</a>`:'';}).join('')}</div>`);
   q('opacity').oninput=e=>{q('heat').style.opacity=Number(e.target.value)/100;q('opacity-value').textContent=e.target.value+'%';};
  }catch(e){if(host.isConnected&&seq===resultGeneration)view.insertAdjacentHTML('beforeend',`<p>${esc(e.message)}</p>`);}
 }
 async function refreshResults(preferred){
  clearTimeout(timer);try{const latest=await api(base);if(!host.isConnected)return;data.runs=latest.runs;
   q('results').innerHTML=`<h3>Grad-CAM results</h3><select data-cam="history">${data.runs.map(r=>`<option value="${esc(r.id)}">${esc(r.name)} · ${esc(r.state)}</option>`).join('')}</select><div data-cam="result-view"></div>`;
   if(data.runs.length){const r=data.runs.find(r=>r.id===(preferred||chosen))||data.runs[0];q('history').value=r.id;q('history').onchange=e=>show(data.runs.find(r=>r.id===e.target.value));await show(r);}else q('results').innerHTML='<p class="muted">No Grad-CAM results yet.</p>';
   if(data.runs.some(r=>!terminal(r)||!r.resource_released))timer=setTimeout(()=>{if(host.isConnected)refreshResults();},5000);
  }catch(e){if(host.isConnected)message.textContent=e.message;}
 }
 await Promise.all([samples(),refreshResults()]);
}
