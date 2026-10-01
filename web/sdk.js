const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export async function mountSdk(host,api,projects,whales){
 const {items}=await api('sdk/tokens');if(!host.isConnected)return;
 host.innerHTML=`<h2>Python SDK</h2><p>Record models, metrics, and checkpoints from your training code.</p><div class="controls"><a class="button secondary" href="/api/sdk/package">Download SDK</a><a class="button secondary" href="/api/sdk/guide">Integration guide</a></div><pre class="config">pip install geppetto_sdk-*.whl</pre><form id="sdk-key-form"><label>Project<select name="project_id">${projects.map(p=>`<option value="${escape(p.id)}">${escape(p.name)}</option>`).join('')}</select></label><label>Environment<select name="node_id">${whales.map(w=>`<option value="${escape(w.id)}">${escape(w.name)}</option>`).join('')}</select></label><label>Key name<input name="name" value="Training SDK" maxlength="160" required></label><button ${projects.length?'':'disabled'}>Create API key</button></form><p class="muted">Project-scoped · Valid for 30 days · Shown once on creation</p><div id="sdk-key-list">${items.map(t=>`<div class="row"><span>${escape(t.name)} · ${escape(projects.find(p=>p.id===t.project_id)?.name||t.project_id)} · ${t.revoked?'Revoked':t.expires_at*1000<Date.now()?'Expired':new Date(t.expires_at*1000).toLocaleDateString('en-US')+' expires'}</span>${t.revoked?'':`<button class="secondary" data-revoke="${escape(t.id)}">Revoke</button>`}</div>`).join('')||'<p class="muted">No API keys issued.</p>'}</div><p id="sdk-message" role="status"></p>`;
 const message=host.querySelector('#sdk-message');
 host.querySelector('form').onsubmit=async event=>{
  event.preventDefault();const form=new FormData(event.target),button=event.submitter;button.disabled=true;
  try{
   const result=await api('sdk/tokens',{name:form.get('name'),project_id:form.get('project_id')});
   const modal=document.querySelector('#modal');
   modal.innerHTML='<h2>API key</h2><p>You cannot view this key again after closing this window.</p><pre class="config" id="sdk-secret"></pre><h3>Training code</h3><pre class="config" id="sdk-code"></pre><p class="muted">For a private CA, set GEPPETTO_CA_CERT to the CA certificate file path.</p><button id="sdk-done">Close</button>';
   modal.querySelector('#sdk-secret').textContent=`export GEPPETTO_URL='${location.origin}'\nexport GEPPETTO_API_KEY='${result.token}'`;
   modal.querySelector('#sdk-code').textContent=`import geppetto_sdk as geppetto\n\nwith geppetto.init(\n    project=${JSON.stringify(result.project_id)},\n    node_id=${JSON.stringify(form.get('node_id'))},\n    name="YOLO26 P2",\n    architecture="YOLO26 P2",\n    config={"epochs": 245, "imgsz": 1920},\\n) as run:\\n    # Call from your training loop\\n    # run.log({"train/loss": loss}, step=epoch, epoch=epoch)\n    # run.progress(epoch, total_epochs=245)\n    # run.log_artifact("best.pt", role="inference_weights")\n    ...`;
   modal.querySelector('#sdk-done').onclick=()=>modal.close();
   modal.addEventListener('close',()=>{modal.innerHTML='';},{once:true});modal.showModal();
   await mountSdk(host,api,projects,whales);
  }catch(error){message.textContent=error.message;}finally{button.disabled=false;}
 };
 host.querySelectorAll('[data-revoke]').forEach(button=>button.onclick=async()=>{button.disabled=true;try{await api(`sdk/tokens/${button.dataset.revoke}/revoke`,{});await mountSdk(host,api,projects,whales);}catch(error){message.textContent=error.message;button.disabled=false;}});
}
