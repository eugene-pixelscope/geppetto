const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export async function mountDiagnostics(host,api,run){
 const {items}=await api(`runs/${run.id}/visualizations`);if(!host.isConnected)return;
 host.innerHTML='<h2>Training diagnostics</h2><p>Histograms, embeddings, and profiler data. Only actual values recorded through the SDK or JSON are shown.</p><label>Import diagnostics JSON<input type="file" accept="application/json" id="diagnostic-file"></label><p role="status"></p><div class="grid" id="diagnostic-charts"></div>';
 const area=host.querySelector('#diagnostic-charts');
 if(!items.length)area.innerHTML='<p>No diagnostics recorded.</p>';
 for(const item of items){
  let marks='',height=240,description='';const d=item.data;
  if(item.type==='histogram'){
   const max=Math.max(1,...d.counts),width=580/d.counts.length;
   marks=d.counts.map((c,i)=>`<rect x="${10+i*width}" y="${220-c/max*200}" width="${width*.9}" height="${c/max*200}" fill="#2563eb"><title>${esc(d.edges[i])}–${esc(d.edges[i+1])}: ${c}</title></rect>`).join('');
   description=`${d.edges[0]} → ${d.edges.at(-1)} · ${d.counts.reduce((a,b)=>a+b,0)} samples`;
  }else if(item.type==='embedding'){
   const xs=d.points.map(p=>p.x),ys=d.points.map(p=>p.y),xmin=Math.min(...xs),ymin=Math.min(...ys),dx=Math.max(...xs)-xmin||1,dy=Math.max(...ys)-ymin||1;
   marks=d.points.map(p=>`<circle cx="${10+(p.x-xmin)/dx*580}" cy="${220-(p.y-ymin)/dy*200}" r="3" fill="#2563eb" opacity=".65"><title>${esc(p.label)} (${p.x}, ${p.y})</title></circle>`).join('');description=`${d.projection} · ${d.points.length} points · Hover over a point to view its label`;
  }else{
   const lanes=[...new Set(d.events.map(e=>e.lane))],min=Math.min(...d.events.map(e=>e.start_us)),max=Math.max(...d.events.map(e=>e.start_us+e.duration_us)),span=max-min||1;height=Math.max(100,lanes.length*30+30);
   marks=lanes.map((lane,i)=>`<text x="0" y="${i*30+18}" font-size="10">${esc(lane.slice(0,18))}</text>`).join('')+d.events.map(e=>`<rect x="${110+(e.start_us-min)/span*480}" y="${lanes.indexOf(e.lane)*30+4}" width="${Math.max(.6,e.duration_us/span*480)}" height="18" fill="#2563eb" opacity=".7"><title>${esc(e.name)} · ${e.duration_us} μs</title></rect>`).join('');description=`${d.events.length} intervals · ${span} μs`;
  }
  const card=document.createElement('article');card.className='card';card.innerHTML=`<h3>${esc(item.name)} · step ${item.step}</h3><svg role="img" aria-label="${esc(item.name)}" viewBox="0 0 600 ${height}" style="width:100%;max-height:600px">${marks}</svg><p>${esc(description)}</p><button type="button">Download JSON</button>`;
  card.querySelector('button').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(item,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download=item.type+'-'+item.step+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};area.append(card);
 }
 host.querySelector('input').onchange=async e=>{try{const file=e.target.files[0];if(!file)return;if(file.size>4*1024*1024)throw Error('Maximum 4 MiB');await api(`runs/${run.id}/visualizations`,JSON.parse(await file.text()));await mountDiagnostics(host,api,run);}catch(error){host.querySelector('[role=status]').textContent=error.message;}};
}
export function mountResume(host,api,run){
 if(!['failed','stopped'].includes(run.state)||!run.artifacts.some(a=>a.resume)||run.frozen?.execution?.backend!=='agent')return;
 host.innerHTML='<h2>Resume from checkpoint</h2><p>Continue after the saved epoch with the same settings and data. Optimizer, scheduler, AMP, and EMA state are restored; the data loader is recreated.</p><button>Review resume settings</button><pre class="config"></pre><p role="status"></p>';
 let request=null;
 host.querySelector('button').onclick=async()=>{try{
  if(!request){request={name:run.name+' · resume',node_id:run.node_id,template_id:run.template_id,dataset_ids:run.frozen.datasets.map(d=>d.id),overrides:run.frozen.config,gpu:run.frozen.gpu,parent_id:run.id,relation:'resume'};const preview=await api('preview',request);request.confirmation_hash=preview.confirmation_hash;host.querySelector('pre').textContent=JSON.stringify(preview.frozen.resume,null,2);host.querySelector('button').textContent='Resume from this checkpoint';}
  else{const result=await api('submissions',request,'POST',{'Idempotency-Key':crypto.randomUUID()});location.hash='run='+result.id;}
 }catch(e){host.querySelector('[role=status]').textContent=e.message;request=null;}};
}
