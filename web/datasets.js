const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

export function datasetBrowser(dataset){
 const counts=dataset.splits;
 return `<div class="section-heading"><h2>Image and label preview</h2><small>Source image and registered labels</small></div><div class="sample-toolbar"><label>Subset<select id="sample-split">${[['train','Training'],['validation','Validation'],['test','Test']].filter(([key])=>counts[key]).map(([key,name])=>`<option value="${key}">${name} · ${counts[key].toLocaleString('en-US')} images</option>`).join('')}</select></label><span id="sample-count" role="status"></span><div class="sample-pages"><button class="secondary" id="sample-prev" aria-label="Previous sample page">←</button><label>Page <input id="sample-page" type="number" min="1" value="1"></label><button class="secondary" id="sample-next" aria-label="Next sample page">→</button></div></div><div class="samples" id="dataset-samples" aria-live="polite"></div>`;
}

export function connectDatasetBrowser(dataset){
 const list=document.querySelector('#dataset-samples');
 if(!list)return;
 const split=document.querySelector('#sample-split'),pageInput=document.querySelector('#sample-page');
 let offset=0,request=0;
 async function load(){
  const generation=++request;
  list.innerHTML='<div class="empty">Loading source samples…</div>';
  const response=await fetch(`/api/datasets/${encodeURIComponent(dataset.id)}/samples?split=${split.value}&offset=${offset}&limit=12`);
  if(!response.ok){if(generation===request&&list.isConnected)list.innerHTML='<div class="notice error">Could not load samples. Refresh and try again.</div>';return;}
  const data=await response.json();
  if(generation!==request||!list.isConnected)return;
  document.querySelector('#sample-count').textContent=`${offset+1}–${Math.min(offset+12,data.total)} / ${data.total.toLocaleString('en-US')} images`;
  document.querySelector('#sample-prev').disabled=offset===0;
  document.querySelector('#sample-next').disabled=offset+12>=data.total;
  pageInput.value=offset/12+1;pageInput.max=Math.max(1,Math.ceil(data.total/12));
  list.innerHTML=data.items.map(s=>{const base=`/api/datasets/${encodeURIComponent(dataset.id)}/samples/${s.index}`;return `<article class="card sample"><div class="sample-frame"><img class="preview-image" src="${base}/image" alt="${esc(s.image)}" loading="lazy"><svg class="label-overlay" viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true"></svg></div><small>Sample #${s.index} · ${esc(s.image)} · ${esc(s.split)}</small><div class="label-summary">Checking labels…</div><div class="controls"><button class="secondary" data-action="label" data-id="${esc(dataset.id)}" data-index="${s.index}" ${s.label||s.number!==undefined?'':'disabled'}>View source labels</button><a class="button secondary" href="${base}/image" target="_blank" rel="noopener">Source image ↗</a></div></article>`;}).join('');
  [...list.querySelectorAll('.sample')].forEach((card,i)=>{
   const sample=data.items[i],img=card.querySelector('img');
   img.addEventListener('error',()=>{card.querySelector('.label-summary').textContent='Cannot access the source image.';card.classList.add('sample-unavailable');});
   if(sample.number!==undefined){card.querySelector('.label-summary').textContent=`Ground truth jersey number: ${sample.number}`;return;}
   if(!sample.label){card.querySelector('.label-summary').textContent='No registered labels';return;}
   fetch(`/api/datasets/${encodeURIComponent(dataset.id)}/samples/${sample.index}/label`).then(async response=>{
    if(!response.ok)throw Error('Cannot access labels.');
    const text=await response.text();
    if(!card.isConnected)return;
    if(dataset.policy?.format==='yolo-pose'){
     const rows=text.trim()?text.trim().split(/\r?\n/).map(line=>line.trim().split(/\s+/).map(Number)):[];
     if(rows.some(row=>row.length<5||row.some(n=>!Number.isFinite(n))))throw Error('Check the YOLO label format.');
     card.querySelector('.label-overlay').innerHTML=rows.map(row=>{const [cls,cx,cy,w,h]=row;if(w<0||h<0||w>1||h>1||cx<0||cx>1||cy<0||cy>1)return '';return `<rect x="${(cx-w/2)*1000}" y="${(cy-h/2)*1000}" width="${w*1000}" height="${h*1000}" fill="none" stroke="white" stroke-width="5"/><rect x="${(cx-w/2)*1000}" y="${(cy-h/2)*1000}" width="${w*1000}" height="${h*1000}" fill="none" stroke="#222" stroke-width="2" stroke-dasharray="5 3"/>`;}).join('');
     card.querySelector('.label-summary').textContent=`Balls: ${rows.length} · Ground truth boxes${rows.some(r=>r.length>=8)?' · Pose labels':''}`;
    }else{
     try{const value=JSON.parse(text);card.querySelector('.label-summary').textContent=value.number!==undefined?`Ground truth jersey number: ${value.number}`:value.boxes?`Ground truth boxes: ${value.boxes.length} total`:'Labels linked';}
     catch{card.querySelector('.label-summary').textContent='Source labels linked';}
    }
   }).catch(error=>{if(card.isConnected)card.querySelector('.label-summary').textContent=error.message;});
  });
 }
 const safeLoad=()=>load().catch(()=>{if(list.isConnected)list.innerHTML='<div class="notice error">Check the connection.</div>';});
 split.onchange=()=>{offset=0;safeLoad();};
 document.querySelector('#sample-prev').onclick=()=>{offset=Math.max(0,offset-12);safeLoad();};
 document.querySelector('#sample-next').onclick=()=>{offset+=12;safeLoad();};
 pageInput.onchange=()=>{const page=Math.max(1,Math.min(+pageInput.max,Math.floor(+pageInput.value)||1));offset=(page-1)*12;safeLoad();};
 safeLoad();
}
