// D3 7.9.0 is bundled locally; data never leaves this application.
const d3=globalThis.d3;
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const format=v=>Number.isFinite(v)?d3.format('.7~g')(v):'—';
const colors=['#e036ce','#2563eb','#248e57','#d47c22','#1c86bd','#c84665'];
// Bias-corrected exponential moving average; original observations stay immutable.
export function smoothValues(values,weight){
 let sum=0,mass=0;
 return values.map(value=>{if(!Number.isFinite(value))return null;sum=weight*sum+(1-weight)*value;mass=weight*mass+(1-weight);return sum/mass;});
}
export function chartMarkup(id){return `<div class="chart-controls"><label class="chart-metric">Metric<select data-chart="${id}" class="metric-select" aria-label="Metric"></select></label><span class="chart-axis-fixed">X · Epoch</span><div class="chart-smoothing"><label for="${id}-smooth">Smoothing</label><div class="chart-smoothing-inputs"><input id="${id}-smooth" data-chart="${id}" class="smooth" type="range" min="0" max="99" value="60" aria-label="Smoothing amount"><input data-chart="${id}" class="smooth-value" type="number" min="0" max="0.99" step="0.01" value="0.6" aria-label="Smoothing value"></div></div><label class="chart-window">Recent window <output id="${id}-window">100%</output><input data-chart="${id}" class="zoom" type="range" min="10" max="100" value="100" aria-label="Display range"></label></div><div class="chart-line-key"><span class="key-smoothed">Smoothed</span><span class="key-raw">Original</span></div><div class="legend" id="${id}-legend"></div><div class="chart-frame"><svg class="chart" id="${id}" viewBox="0 0 680 245" tabindex="0" role="img" aria-label="Metric chart. Use a mouse or touch to inspect values, or the left and right arrow keys to explore records."></svg><div class="chart-tooltip" id="${id}-tooltip" role="status" hidden></div></div><p class="chart-summary" id="${id}-summary"></p>`;}
export function initChart(id,list,options={}){
 const svg=$('#'+id),tooltip=$('#'+id+'-tooltip'),select=$(`.metric-select[data-chart="${id}"]`);
 const keys=[...new Set(list.flatMap(r=>Object.keys(r.metrics)))];select.innerHTML=keys.map(k=>`<option>${esc(k)}</option>`).join('');
 const control=cls=>$(`.${cls}[data-chart="${id}"]`),storageKey='geppetto-chart-v2:'+id;
 try{const saved=options.remember===false?{}:JSON.parse(localStorage.getItem(storageKey)||'{}');for(const cls of ['metric-select','smooth','zoom'])if(saved[cls]!==undefined){const el=control(cls);if(el.tagName!=='SELECT'||[...el.options].some(o=>o.value===saved[cls]))el.value=saved[cls];}}catch{}
 for(const [cls,value] of Object.entries(options.controls||{})){const el=control(cls);if(el)el.value=value;}
 function draw(){
  const width=Math.max(320,svg.clientWidth),height=options.height||Math.max(180,svg.clientHeight),right=width-24,bottom=height-37;svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
  const key=select.value,axis='epoch',alpha=+$(`.smooth[data-chart="${id}"]`).value/100,zoom=+$(`.zoom[data-chart="${id}"]`).value/100;
  control('smooth-value').value=String(alpha);control('smooth').setAttribute('aria-valuetext',String(alpha));$('#'+id+'-window').textContent=Math.round(zoom*100)+'%';
  try{if(options.remember!==false)localStorage.setItem(storageKey,JSON.stringify(Object.fromEntries(['metric-select','smooth','zoom'].map(cls=>[cls,control(cls).value]))));}catch{}
  const root=d3.select(svg);root.selectAll('*').remove();root.on('.inspect',null);tooltip.hidden=true;
  const series=list.map((r,i)=>{const raw=(r.metrics[key]||[]).filter(p=>Number.isFinite(p[axis])&&Number.isFinite(p.value)).sort((a,b)=>a[axis]-b[axis]),smoothed=smoothValues(raw.map(p=>p.value),alpha);const points=raw.map((p,i)=>({x:p[axis],y:smoothed[i],raw:p.value}));return {name:r.name,color:r.color||colors[i%colors.length],points:points.slice(-Math.max(1,Math.ceil(points.length*zoom)))};});
  const points=series.flatMap(s=>s.points),summary=$('#'+id+'-summary');
  $('#'+id+'-legend').hidden=!!options.solo;$('#'+id+'-legend').innerHTML=series.map(s=>`<span><i class="swatch" style="--c:${s.color}"></i>${esc(s.name)}${s.points.length?'':' · Not recorded'}</span>`).join('');
  if(!points.length){summary.textContent='No values with recorded epochs. Steps are not treated as epochs.';return;}
  let [xmin,xmax]=d3.extent(points,p=>p.x),[ymin,ymax]=d3.extent(points.flatMap(p=>[p.y,p.raw]));
  if(xmin===xmax){xmin-=.5;xmax+=.5;}
  const pad=ymin===ymax?Math.max(Math.abs(ymin)*.05,.01):(ymax-ymin)*.08;
  const x=d3.scaleLinear().domain([xmin,xmax]).range([54,right]),y=d3.scaleLinear().domain([ymin-pad,ymax+pad]).nice(4).range([bottom,28]);
  root.append('g').attr('transform','translate(54,0)').call(d3.axisLeft(y).ticks(4).tickSize(-(right-54)).tickFormat(d3.format('.4~g'))).call(g=>g.select('.domain').remove()).call(g=>g.selectAll('.tick line').attr('stroke','#e7e7e3'));
  root.append('g').attr('transform',`translate(0,${bottom})`).call(d3.axisBottom(x).ticks(5).tickFormat(d3.format('.5~g')));
  root.append('text').attr('class','chart-x-label').attr('x',right).attr('y',height-4).attr('text-anchor','end').text('Epoch');
  const line=d3.line().x(p=>x(p.x)).y(p=>y(p.y)),rawLine=d3.line().x(p=>x(p.x)).y(p=>y(p.raw));
  series.forEach(s=>{if(alpha)root.append('path').datum(s.points).attr('class','chart-raw').attr('d',rawLine).attr('fill','none').attr('stroke',s.color).attr('stroke-opacity',.22).attr('stroke-width',1.5);});
  series.forEach(s=>{root.append('path').datum(s.points).attr('class','chart-smoothed').attr('d',line).attr('fill','none').attr('stroke',s.color).attr('stroke-width',2.2);if(s.points.length===1)root.append('circle').attr('cx',x(s.points[0].x)).attr('cy',y(s.points[0].y)).attr('r',3).attr('fill',s.color);});
  const cursor=root.append('g').attr('class','chart-crosshair').style('display','none').attr('pointer-events','none');
  const vertical=cursor.append('line').attr('y1',28).attr('y2',bottom).attr('stroke','#64748b').attr('stroke-dasharray','4 3');
  const markers=cursor.append('g'),xs=[...new Set(points.map(p=>p.x))].sort((a,b)=>a-b),bisect=d3.bisector(p=>p.x).center;
  let index=xs.length-1;
  const hide=()=>{tooltip.hidden=true;cursor.style('display','none');};
  function inspect(value){
   index=d3.bisectCenter(xs,value);const anchor=xs[index];cursor.style('display',null);vertical.attr('x1',x(anchor)).attr('x2',x(anchor));
   const selected=series.map(s=>{const valid=s.points.length&&anchor>=s.points[0].x&&anchor<=s.points.at(-1).x;return {...s,point:valid?s.points[bisect(s.points,anchor)]:null};});
   markers.selectAll('circle').data(selected.filter(s=>s.point)).join('circle').attr('cx',s=>x(s.point.x)).attr('cy',s=>y(s.point.y)).attr('r',4.5).attr('fill',s=>s.color).attr('stroke','white').attr('stroke-width',1.5);
   tooltip.innerHTML=options.solo?`<div class="tooltip-single"><span style="color:${selected[0].color}">●</span><strong>Epoch ${format(anchor)}</strong><span>${selected[0].point?format(selected[0].point.raw):'—'}</span></div>`:`<strong>${esc(key)}</strong><div class="tooltip-axis">${esc(axis)} ${format(anchor)}</div><div class="chart-tooltip-scroll"><table><thead><tr><th>Run</th><th>Smoothed</th><th>Original</th><th>${esc(axis)}</th></tr></thead><tbody>${selected.map(s=>`<tr><td><i class="swatch" style="--c:${s.color}"></i>${esc(s.name)}</td><td>${s.point?format(s.point.y):'—'}</td><td>${s.point?format(s.point.raw):'—'}</td><td>${s.point?format(s.point.x):'—'}${s.point&&s.point.x!==anchor?' *':''}</td></tr>`).join('')}</tbody></table></div>${selected.some(s=>s.point&&s.point.x!==anchor)?'<small>* Nearest recorded value</small>':''}`;
   tooltip.hidden=false;tooltip.style.left=x(anchor)>width/2?'8px':'auto';tooltip.style.right=x(anchor)>width/2?'auto':'8px';
  }
  root.append('rect').attr('class','chart-hitarea').attr('x',54).attr('y',28).attr('width',right-54).attr('height',bottom-28).attr('fill','transparent')
   .on('pointermove.inspect',event=>inspect(x.invert(d3.pointer(event,svg)[0])))
   .on('pointerdown.inspect',event=>inspect(x.invert(d3.pointer(event,svg)[0])))
   .on('pointerleave.inspect',event=>{if(event.pointerType!=='touch')hide();});
  root.on('focus.inspect',()=>inspect(xs[index])).on('blur.inspect',hide).on('keydown.inspect',event=>{if(event.key==='Escape'){hide();return;}if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();index=event.key==='Home'?0:event.key==='End'?xs.length-1:Math.max(0,Math.min(xs.length-1,index+(event.key==='ArrowRight'?1:-1)));inspect(xs[index]);});
  summary.textContent=options.solo?'':alpha?'Smoothing '+alpha.toFixed(2)+' · '+points.length+' points':points.length+' points · Original';
 }
 document.querySelectorAll(`[data-chart="${id}"]`).forEach(e=>e.oninput=()=>{if(e.classList.contains('smooth-value')){if(e.value===''||!e.validity.valid)return;control('smooth').value=Math.round(Number(e.value)*100);}draw();});draw();
 const observer=new ResizeObserver(()=>{if(svg.isConnected)draw();else observer.disconnect();});observer.observe(svg);
 return {update(controls){for(const [cls,value] of Object.entries(controls)){const el=control(cls);if(el)el.value=value;}draw();},destroy(){observer.disconnect();}};
}
