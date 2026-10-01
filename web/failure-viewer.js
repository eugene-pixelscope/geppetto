import {detectionOverlay,detectionLegend} from './detection-overlay.js';
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const location=b=>Math.min(7,Math.floor(b[1]*8))*8+Math.min(7,Math.floor(b[0]*8));
export function openFailureViewer(rows,index,imageUrl,{kind='fn',cell=null}={}){
 const modal=document.querySelector('#modal');let current=index,truth=true,predictions=true,view,drag;
 modal.classList.add('failure-dialog');
 modal.addEventListener('close',()=>{modal.classList.remove('failure-dialog');modal.innerHTML='';},{once:true});
 const row=()=>rows[current];
 function update(){modal.querySelector('#failure-canvas').setAttribute('viewBox',`${view.x} ${view.y} ${view.w} ${view.h}`);}
 function full(){const r=row();view={x:0,y:0,w:r.width,h:r.height};update();}
 function clamp(){const r=row();view.w=Math.max(32,Math.min(r.width,view.w));view.h=Math.max(32,Math.min(r.height,view.h));view.x=Math.max(0,Math.min(r.width-view.w,view.x));view.y=Math.max(0,Math.min(r.height-view.h,view.y));update();}
 function zoom(factor){const cx=view.x+view.w/2,cy=view.y+view.h/2;view.w*=factor;view.h*=factor;view.x=cx-view.w/2;view.y=cy-view.h/2;clamp();}
 function focus(box){const r=row(),ratio=r.width/r.height;view={w:Math.max(100,box[2]*r.width*5),h:Math.max(100,box[3]*r.height*5)};if(view.w/view.h<ratio)view.w=view.h*ratio;else view.h=view.w/ratio;view.x=box[0]*r.width-view.w/2;view.y=box[1]*r.height-view.h/2;clamp();}
 function draw(){
  const r=row(),errors=(kind==='fn'?r.truth.filter(g=>g.missed):r.predictions.filter(p=>p.false_positive)).filter(p=>cell===null||location(p.box)===cell);
  modal.innerHTML=`<div class="row"><h2>Failure image</h2><button id="failure-close" class="secondary" aria-label="Close failure image">Close</button></div><p class="failure-name">${esc(r.image)}</p><div class="failure-toolbar"><label><input id="failure-truth" type="checkbox" ${truth?'checked':''}> Ground truth</label><label><input id="failure-predictions" type="checkbox" ${predictions?'checked':''}> Predictions</label><button id="failure-zoom-in" class="secondary" aria-label="Zoom in">＋</button><button id="failure-zoom-out" class="secondary" aria-label="Zoom out">−</button><button id="failure-fit" class="secondary">Fit to view</button><a href="${esc(imageUrl(r))}" target="_blank" rel="noopener" class="button secondary">Original</a></div><div class="failure-canvas-wrap"><svg id="failure-canvas" viewBox="0 0 ${r.width} ${r.height}" role="img" aria-label="Ground truth and prediction boxes. Drag to pan the zoomed area."><image href="${esc(imageUrl(r))}" width="${r.width}" height="${r.height}"/>${detectionOverlay(r)}</svg></div><div class="failure-focus"><span>${kind==='fn'?'False negative':'False positive'} zoom to location</span>${errors.map((p,i)=>`<button class="secondary" data-focus-box="${i}">${i+1}${p.score===undefined?'':' · '+p.score.toFixed(3)}</button>`).join('')}</div><p class="overlay-legend">${detectionLegend()}</p><div class="row"><button id="failure-prev" class="secondary" ${current===0?'disabled':''}>Previous image</button><span>Current list: ${current+1} / ${rows.length} · False negatives: ${r.fn} · False positives: ${r.fp}</span><button id="failure-next" class="secondary" ${current===rows.length-1?'disabled':''}>Next image</button></div>`;
  const find=s=>modal.querySelector(s);find('[data-overlay=truth]').style.display=truth?'inline':'none';find('[data-overlay=prediction]').style.display=predictions?'inline':'none';view={x:0,y:0,w:r.width,h:r.height};drag=null;
  find('#failure-close').onclick=()=>modal.close();find('#failure-prev').onclick=()=>{current--;draw();};find('#failure-next').onclick=()=>{current++;draw();};
  find('#failure-truth').onchange=e=>{truth=e.target.checked;find('[data-overlay=truth]').style.display=truth?'inline':'none';};find('#failure-predictions').onchange=e=>{predictions=e.target.checked;find('[data-overlay=prediction]').style.display=predictions?'inline':'none';};
  find('#failure-fit').onclick=full;find('#failure-zoom-in').onclick=()=>zoom(.5);find('#failure-zoom-out').onclick=()=>zoom(2);
  modal.querySelectorAll('[data-focus-box]').forEach(button=>button.onclick=()=>focus(errors[Number(button.dataset.focusBox)].box));
  const svg=find('#failure-canvas');
  svg.onpointerdown=e=>{if(e.button!==0)return;const matrix=svg.getScreenCTM().inverse(),point=new DOMPoint(e.clientX,e.clientY).matrixTransform(matrix);drag={matrix,point,view:{...view}};svg.setPointerCapture(e.pointerId);};
  svg.onpointermove=e=>{if(!drag)return;const point=new DOMPoint(e.clientX,e.clientY).matrixTransform(drag.matrix);view.x=drag.view.x+drag.point.x-point.x;view.y=drag.view.y+drag.point.y-point.y;clamp();};svg.onpointerup=svg.onpointercancel=()=>{drag=null;};
  if(errors.length)focus(errors[0].box);
 }
 draw();if(!modal.open)modal.showModal();
}
