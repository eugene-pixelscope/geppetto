export const detectionColors={truth:'#22c55e',miss:'#ef4444',prediction:'#38bdf8',falsePositive:'#fb923c'};
export function detectionOverlay(row){
 const {width,height}=row;
 const rect=(b,color,dashed=false)=>{const [cx,cy,w,h]=b;return `<rect x="${(cx-w/2)*width}" y="${(cy-h/2)*height}" width="${w*width}" height="${h*height}" fill="none" stroke="${color}" stroke-width="2.5" vector-effect="non-scaling-stroke" ${dashed?'stroke-dasharray="6 4"':''}/>`;};
 const label=(b,text,color)=>{const x=Math.max(0,(b[0]-b[2]/2)*width),y=Math.max(height*.025,(b[1]-b[3]/2)*height-height*.005);return `<text x="${x}" y="${y}" fill="${color}" stroke="#111" stroke-width="2" paint-order="stroke" font-size="${height*.022}" font-weight="700">${text}</text>`;};
 const pred=row.predictions.map(p=>rect(p.box,p.false_positive?detectionColors.falsePositive:detectionColors.prediction,true)+label(p.box,`${p.false_positive?'FP ':''}${p.score.toFixed(2)}`,p.false_positive?detectionColors.falsePositive:detectionColors.prediction)).join('');
 const truth=row.truth.map(g=>rect(g.box,detectionColors.truth)).join('');
 // A red outer halo marks a miss without replacing the green ground-truth box.
 const missed=row.truth.filter(g=>g.missed).map(g=>{const [x,y,w,h]=g.box;return rect([x,y,w+.008,h+.008],detectionColors.miss,true)+label([x,y,w+.008,h+.008],'FN',detectionColors.miss);}).join('');
 return `<g data-overlay="prediction">${pred}</g><g data-overlay="truth">${truth}${missed}</g>`;
}
export const detectionLegend=()=>`<span style="--overlay-color:${detectionColors.truth}">Ground truth: Solid green</span><span style="--overlay-color:${detectionColors.miss}">False negative: Red outline · FN</span><span style="--overlay-color:${detectionColors.prediction}">Prediction: Dashed blue</span><span style="--overlay-color:${detectionColors.falsePositive}">False positive: Dashed orange · FP</span>`;
