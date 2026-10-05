(() => {
  document.querySelectorAll('[data-storage-filter]').forEach(input => input.addEventListener('input', () => {
    const query = input.value.trim().toLowerCase();
    document.getElementById(input.dataset.storageFilter).querySelectorAll('tbody tr').forEach(row => row.hidden = !row.textContent.toLowerCase().includes(query));
  }));
  const form = document.getElementById('storage-action-form');
  if (form) {
    function show(id, visible) { const el=document.getElementById(id); el.hidden=!visible; el.querySelectorAll('input,select').forEach(e=>e.disabled=!visible); }
    function render() {
      const a=form.elements.action.value;
      show('storage-target', !a.startsWith('helper-'));
      show('storage-property', ['dataset-set','dataset-inherit'].includes(a));
      show('storage-value', ['dataset-set','zvol-create'].includes(a));
      show('storage-clone', a==='snapshot-clone');
      form.elements.target.required=!a.startsWith('helper-');
      form.elements.new_pool.required=a==='snapshot-clone';
      form.elements.value.required=['dataset-set','zvol-create'].includes(a);
      document.getElementById('storage-action-warning').textContent=form.elements.action.selectedOptions[0]?.dataset.warning||'';
    }
    form.elements.action.addEventListener('change',render);
    form.addEventListener('submit',()=>{form.elements.pool.value=/^(helper|smart)-/.test(form.elements.action.value)?'host':form.elements.target.value.split('/')[0].split('@')[0];});render();
  }
  const policy=document.getElementById('storage-policy-form');
  if (policy) {
    function render(){policy.querySelectorAll('[data-policy-kinds]').forEach(el=>{el.hidden=!el.dataset.policyKinds.split(' ').includes(policy.elements.kind.value);el.querySelectorAll('input,select').forEach(e=>e.disabled=el.hidden);});}
    policy.elements.kind.addEventListener('change',render);render();
  }
})();
