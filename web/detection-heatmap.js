// Separable Gaussian smoothing of observed positions, never model attention.
export function blurDensity(values,width,height,sigma=2.5){
 const radius=Math.ceil(sigma*3),kernel=Array.from({length:radius*2+1},(_,i)=>Math.exp(-.5*((i-radius)/sigma)**2));const sum=kernel.reduce((a,b)=>a+b,0);kernel.forEach((_,i)=>kernel[i]/=sum);
 const tmp=new Float64Array(values.length),out=new Float64Array(values.length);
 for(let y=0;y<height;y++)for(let x=0;x<width;x++)for(let k=-radius;k<=radius;k++){const xx=x+k;if(xx>=0&&xx<width)tmp[y*width+x]+=values[y*width+xx]*kernel[k+radius];}
 for(let y=0;y<height;y++)for(let x=0;x<width;x++)for(let k=-radius;k<=radius;k++){const yy=y+k;if(yy>=0&&yy<height)out[y*width+x]+=tmp[yy*width+x]*kernel[k+radius];}
 return out;
}
export function mountDetectionHeatmap(host,data,{kind,metric,cell,imageUrl,onSelect}){
 const map=data.heatmap;if(!map){host.textContent='No position data.';return;}
 const background=map.background,mixed=data.cameras.length>1&&!document.querySelector('#analysis-camera')?.value;
 host.innerHTML=`<div class="detection-heatmap-stage" style="aspect-ratio:${background?background.width/background.height:16/9}">${background&&!mixed?`<img src="${imageUrl(background.index)}" alt="Reference frame for aggregated error positions">`:''}<canvas width="${map.width}" height="${map.height}" tabindex="0" role="img" aria-label="${kind==='fn'?'False negative':'False positive'} position density heatmap. Click or use arrow keys to select a region."></canvas><div class="heatmap-selection" hidden></div></div><div class="density-scale"><span>Low</span><i></i><span>High</span></div><p class="footer-note heatmap-summary"></p>`;
 const density=blurDensity(map[kind],map.width,map.height),support=blurDensity(map[kind==='fn'?'truth':'predictions'],map.width,map.height);
 const values=Array.from(density,(v,i)=>metric==='rate'?(support[i]>1e-6?v/support[i]:0):v),max=metric==='rate'?1:Math.max(...values,0);
 const canvas=host.querySelector('canvas'),ctx=canvas.getContext('2d'),pixels=ctx.createImageData(map.width,map.height);
 for(let i=0;i<values.length;i++){if(values[i]<=0||!max)continue;const t=Math.min(1,values[i]/max),color=globalThis.d3.rgb(globalThis.d3.interpolateTurbo(t));pixels.data.set([color.r,color.g,color.b,Math.round(210*Math.min(1,t*4))],i*4);}
 ctx.putImageData(pixels,0,0);
 host.querySelector('.heatmap-summary').textContent=`${map[kind].reduce((a,b)=>a+b,0)} ${kind==='fn'?'False-negative ground truth':'False-positive predictions'} centers, smoothed into ${metric==='rate'?'local error rate (0–100%)':'relative density'}. ${mixed?'Select a camera to overlay on a source frame.':'The background is a reference frame; positions are aggregated across images.'} Click to view failure images in that region.`;
 const selection=host.querySelector('.heatmap-selection');let keyboardCell=cell??27;
 function mark(index){selection.hidden=index===null;if(index!==null){selection.style.left=(index%8)*12.5+'%';selection.style.top=Math.floor(index/8)*12.5+'%';}}
 mark(cell);
 canvas.onclick=e=>{const r=canvas.getBoundingClientRect();onSelect(Math.min(7,Math.floor((e.clientY-r.top)/r.height*8))*8+Math.min(7,Math.floor((e.clientX-r.left)/r.width*8)));};
 canvas.onkeydown=e=>{const moves={ArrowLeft:-1,ArrowRight:1,ArrowUp:-8,ArrowDown:8};if(e.key in moves){e.preventDefault();keyboardCell=Math.max(0,Math.min(63,keyboardCell+moves[e.key]));mark(keyboardCell);}else if(e.key==='Enter'||e.key===' '){e.preventDefault();onSelect(keyboardCell);}};
}
