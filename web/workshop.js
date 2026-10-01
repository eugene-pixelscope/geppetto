// Original whale retained; Pinocchio redrawn to match the Geppetto brand portrait.
// Running animation represents observed execution state, not throughput.
export const whaleIcon = `<svg viewBox="0 0 340 204" fill="none" aria-hidden="true" class="whale-art">
  <path d="M81 107C61 102 43 85 39 65C26 65 14 70 10 81C12 102 32 119 57 122C21 130 18 154 31 168C55 169 75 157 89 141C118 185 209 195 274 171C318 155 335 121 324 85C314 49 262 38 202 45C151 50 112 68 94 97L81 107Z" fill="currentColor"/>
  <path d="M134 151C143 177 170 195 192 188L175 156" fill="currentColor" stroke="#fafafa" stroke-width="2"/>
  <path d="M271 133C290 139 308 133 318 123" stroke="#fafafa" stroke-width="2.5" stroke-linecap="round"/>
  <circle cx="281" cy="104" r="4" fill="#fafafa"/>
  <path d="M225 36V19M218 29C206 29 205 17 211 12M232 29C246 29 251 17 245 11" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
  <path d="M63 191H111M224 191H290M21 184H47" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" opacity=".2"/>
</svg>`;
// Rounded ink outlines and white fills echo the Geppetto brand portrait.
export const puppetIcon = `<svg viewBox="0 0 120 140" class="puppet" aria-hidden="true" fill="white" stroke="currentColor" stroke-width="3.8" stroke-linecap="round" stroke-linejoin="round">
 <ellipse class="run-shadow" cx="57" cy="134" rx="24" ry="3" fill="currentColor" stroke="none" opacity=".12"/>
 <g class="run-body">
  <g class="run-leg run-leg-far"><path d="M48 91Q41 89 40 98L40 111Q45 115 51 110L54 98Z" fill="#deded9"/><g class="run-shin run-shin-far"><path d="M41 109L40 124Q44 128 50 124L50 109" fill="#deded9"/><path d="M40 123Q35 127 38 132L59 132Q62 126 50 123Z" fill="currentColor"/></g></g>
  <g class="run-arm run-arm-far"><path d="M52 68Q44 65 40 73L34 85L41 90L51 78" fill="#deded9"/><g class="run-forearm run-forearm-far"><path d="M36 84L44 96Q48 101 52 96Q55 92 49 89L43 81" fill="#deded9"/></g></g>
  <path d="M49 59L63 59L65 72L47 72Z"/>
  <path d="M43 68Q52 63 64 68L69 89Q58 98 40 91L40 78Z"/>
  <path d="M43 70L50 75L55 68L60 76L65 70" stroke-width="2.8"/>
  <path d="M42 86Q54 91 67 86L69 99L56 104L51 99L39 99Z" fill="currentColor"/>
  <g class="run-leg run-leg-near"><path d="M55 98Q63 93 65 101L64 113Q59 117 54 112Z"/><g class="run-shin run-shin-near"><path d="M55 111L55 126Q60 129 65 125L64 111"/><path d="M55 124Q51 128 53 133L76 133Q78 127 65 124Z" fill="currentColor"/></g></g>
  <g class="run-head">
   <path d="M38 30Q29 37 34 48L40 55L47 39Z" fill="currentColor"/>
   <path d="M40 30Q62 22 74 37L76 44L95 48Q99 51 94 54L77 55Q73 69 57 67Q43 66 39 53Q29 55 29 46Q29 39 37 40Z"/>
   <path d="M37 43Q32 42 34 48M63 56Q67 60 72 56" fill="none" stroke-width="2.8"/>
   <path d="M61 38Q65 35 69 38" fill="none" stroke-width="2.8"/>
   <ellipse cx="67" cy="45" rx="3" ry="4.5" fill="currentColor" stroke="none"/>
   <path d="M37 32Q40 44 49 39L47 32Q57 37 65 31" fill="currentColor"/>
   <path class="run-feather" d="M42 20Q24 15 27 5Q39 6 45 18"/>
   <path d="M36 28Q37 15 49 8Q53 20 68 29Z"/>
   <path d="M36 24Q49 20 61 24L68 29L36 30Z" fill="currentColor" stroke="none"/>
   <path d="M29 30Q48 24 73 30Q77 36 66 36L35 36Q27 36 29 30Z"/>
  </g>
  <g class="run-arm run-arm-near"><path d="M57 71Q64 66 69 73L73 85Q70 90 64 88L58 79Z"/><g class="run-forearm run-forearm-near"><path d="M65 85L75 80Q74 75 79 74Q85 73 87 79Q88 83 83 86L72 94Q66 95 64 90Z"/></g></g>
 </g>
</svg>`;

// Successful model: a proud upward-left glance with arms folded across the chest.
export const proudPuppetIcon = `<svg viewBox="0 0 120 140" class="puppet puppet-proud" aria-hidden="true" fill="white" stroke="currentColor" stroke-width="3.8" stroke-linecap="round" stroke-linejoin="round">
 <ellipse cx="60" cy="134" rx="27" ry="3" fill="currentColor" stroke="none" opacity=".12"/>
 <path d="M44 101L43 126L54 127L57 104M64 104L67 127L78 126L75 101"/>
 <path d="M43 123Q33 124 32 131Q42 136 55 131L54 124ZM67 124L66 131Q79 136 88 131Q87 124 77 123Z" fill="currentColor"/>
 <path d="M52 64L51 77L69 77L68 64"/>
 <path d="M44 75Q60 69 76 75L80 102Q60 110 40 102Z"/>
 <path d="M46 75L53 83L60 77L67 83L74 75M60 81V101" fill="none" stroke-width="2.8"/>
 <path d="M41 100Q60 105 79 100L78 110L64 111L60 107L56 111L42 110Z" fill="currentColor"/>
 <g class="proud-folded-arms">
  <path d="M42 77Q34 77 32 87L31 95Q34 101 43 100L75 87L71 79L44 90L46 82Z"/>
  <path d="M68 80Q72 76 77 79L81 84Q82 88 77 90L69 88"/>
  <path d="M78 77Q86 78 88 87L89 95Q87 102 79 101L43 88L47 80L77 90L74 83Z"/>
  <path d="M48 81Q43 77 39 80L35 85Q35 90 42 91L49 89"/>
  <path d="M40 84L45 86M76 83L72 85" fill="none" stroke-width="2.2"/>
 </g>
 <g transform="rotate(8 60 61)">
  <path d="M79 39Q89 34 92 43Q95 55 82 58"/>
  <path d="M34 34Q36 20 59 21Q82 22 84 39L82 53Q78 70 60 72Q42 72 35 59L32 51L17 43Q13 40 17 38L34 41Z"/>
  <path d="M34 34L36 41L42 33Q49 39 55 31Q66 39 73 32L79 48Q86 42 84 31Z" fill="currentColor" stroke="none"/>
  <path d="M81 44Q87 41 86 48" fill="none" stroke-width="2.5"/>
  <path d="M37 39Q42 35 47 38M57 39Q64 34 70 39" fill="none" stroke-width="2.6"/>
  <path d="M37 45Q42 40 47 44M57 46Q63 40 69 45" fill="none" stroke-width="2.8"/>
  <ellipse cx="39" cy="45" rx="2.4" ry="3.4" fill="currentColor" stroke="none"/>
  <ellipse cx="60" cy="46" rx="2.6" ry="3.6" fill="currentColor" stroke="none"/>
  <path d="M35 50Q38 53 43 50" fill="none" stroke-width="2.3"/>
  <path d="M43 59Q56 64 69 56Q64 68 55 68Q47 67 43 59Z" fill="currentColor" stroke-width="2"/>
  <path d="M47 60Q56 63 65 59" fill="none" stroke="white" stroke-width="2.6"/>
  <path d="M42 20Q28 14 31 5Q44 6 48 18"/>
  <path d="M38 25Q45 11 62 6Q65 18 78 26Z"/>
  <path d="M40 21Q57 17 72 21L80 28L37 28Z" fill="currentColor" stroke="none"/>
  <path d="M30 28Q59 20 87 28Q91 34 79 35L38 35Q26 34 30 28Z"/>
 </g>
</svg>`;

const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const evidence=new Map();
let connected=true;

export function describeProgress(run, previous=null, now=Date.now()) {
 const observed=run?.last_success_at??run?.observed_at??run?.progress?.observed_at;
 const fresh=Number.isFinite(observed)&&now-observed*1000>=0&&now-observed*1000<180000;
 const phase=run?.progress?.phase||run?.phase;
 const moving=!!(run?.state==='running'&&fresh&&!run.fetch_error&&!run.progress?.log_error&&!['validation','Validation','evaluating'].includes(phase));
 return {moving};
}

export function workshopScene(run=null) {
 const p=run?.progress||{},state=describeProgress(run);
 const known=Number.isFinite(p.epoch)&&p.epoch>=0&&Number.isFinite(p.total)&&p.total>0;
 const finished=run?.state==='succeeded';
 const percent=finished?100:known?Math.min(100,Math.max(0,p.epoch/p.total*100)):0;
 const label=finished?'Arrived · Job completed':known?`${p.epoch_basis==='completed'?'Completed':'Recorded'} epochs · ${Math.round(percent)}%`:'Waiting for progress updates';
 return `<section class="workshop-card training-status ${state.moving?'is-running':''}" aria-label="Training progress path" data-workshop="${escapeHTML(run?.id||'idle')}">
   <div class="training-epoch"><span class="phase-label">${p.epoch_basis==='completed'?'Completed epochs':'Epoch'}</span><div class="epoch-number">${escapeHTML(p.epoch??'—')}<span> / ${escapeHTML(p.total??'—')}</span></div></div>
   <div class="training-route" role="progressbar" aria-label="Job progress" aria-valuemin="0" aria-valuemax="100" ${known||finished?`aria-valuenow="${percent}"`:''} aria-valuetext="${escapeHTML(label)}">
     <div class="training-track" aria-hidden="true"><span class="route-line"></span><span class="route-covered" style="width:${percent}%"></span><span class="route-start"></span><span class="route-finish"><svg viewBox="0 0 24 32" fill="none"><path d="M3 31V2" stroke="currentColor" stroke-width="2"/><path d="M4 2H22V16H4Z" fill="white" stroke="currentColor"/><path d="M4 2H10V9H4ZM16 2H22V9H16ZM10 9H16V16H10Z" fill="currentColor"/></svg></span><div class="route-runner" style="left:${percent}%">${finished?proudPuppetIcon:puppetIcon}</div></div>
     <div class="route-labels" aria-hidden="true"><span>Start</span><span>Finish</span></div>
     <p class="route-caption">${escapeHTML(label)}</p>
   </div>
 </section>`;
}
export function observeWorkshops(runs, now=Date.now()) {
 connected=true;
 for(const run of runs)evidence.set(run.id,run);
 updateWorkshops(now);
}
export function setWorkshopConnection(ok){connected=ok;updateWorkshops();}
export function updateWorkshops(now=Date.now()) {
 for(const card of document.querySelectorAll('[data-workshop]')){
  const run=evidence.get(card.dataset.workshop);
  card.classList.toggle('is-running',connected&&describeProgress(run,null,now).moving);
 }
}
