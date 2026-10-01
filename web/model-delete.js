const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function installModelDeletion({api,refresh,toast,getModels,afterDelete}){
 document.querySelector('#main').addEventListener('click',e=>{
  const button=e.target.closest('[data-model-delete]');if(!button)return;
  e.preventDefault();e.stopPropagation();
  const action='delete',id=button.dataset.modelDelete;
  const model=getModels().find(p=>p.id===id)||{id,name:button.dataset.name};
  const modal=document.querySelector('#modal');
  modal.innerHTML=`<h2>Delete model?</h2><p><strong>${esc(model.name)}</strong></p><p>Permanently delete the model and all associated run records. This cannot be undone. External training result files and source data are preserved.</p><p role="alert" class="history-error"></p><div class="wizard-actions"><button class="secondary" id="model-delete-cancel">Cancel</button><button id="model-delete-confirm">Delete model</button></div>`;
  modal.showModal();modal.querySelector('#model-delete-cancel').onclick=()=>modal.close();
  modal.querySelector('#model-delete-confirm').onclick=async()=>{const confirm=modal.querySelector('#model-delete-confirm');confirm.disabled=true;try{const result=await api(`pinocchios/${id}/${action}`,{name:model.name});if(action==='delete'){afterDelete(result.run_ids);history.replaceState(null,'','#node='+encodeURIComponent(result.node_id));}modal.close();await refresh();toast('Model deleted.');}catch(err){modal.querySelector('[role=alert]').textContent=err.message;confirm.disabled=false;}};
 });
}
