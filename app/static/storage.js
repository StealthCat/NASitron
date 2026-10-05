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
      const smart=a.startsWith('smart-'), snapshot=a.startsWith('snapshot-'), helper=a.startsWith('helper-');
      const label=document.getElementById('storage-target-label');
      if(label) label.textContent=smart?'Physical disk by-id name':snapshot?'Snapshot':'Dataset';
      form.elements.target.placeholder=smart?'scsi-SATA_…':snapshot?'tank/data@daily':'tank/data';
      if(smart) form.elements.target.removeAttribute('list'); else form.elements.target.setAttribute('list','storage-targets');
      const hint=document.getElementById('storage-action-hint');
      if(hint) hint.textContent=smart?'Enter the physical disk’s by-id basename without /dev/disk/by-id/. Review the exact command on the next page.':helper?'Review the helper version and digest, then type the confirmation on the next page.':snapshot?'Use dataset@snapshot. Review the exact command and its recovery implications before confirming.':'Use an existing dataset, or a new child name when creating one. Mountpoint changes are restricted to /mnt and /srv. Review the command before confirming.';

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
    const feedback = document.getElementById('storage-policy-feedback');
    const name = policy.elements.namedItem('name'), confirmation = policy.elements.confirm_text;
    function validateConfirmation() {
      if (confirmation && name) confirmation.setCustomValidity(confirmation.value === 'ENABLE ' + name.value ? '' : 'Type ENABLE followed by the exact policy name.');
    }
    if (name && confirmation) {
      name.addEventListener('input', validateConfirmation);
      confirmation.addEventListener('input', validateConfirmation);
      validateConfirmation();
    }
    let pending = false;
    policy.addEventListener('submit', async event => {
      if (!feedback) return;
      event.preventDefault();
      if (pending) return;
      const body = new FormData(policy);
      const button = policy.querySelector('button.primary');
      const label = button.textContent;
      pending = true; button.disabled = true; button.textContent = 'Saving…';
      feedback.hidden = true;
      try {
        const response = await fetch(policy.action, {method:'POST', body, credentials:'same-origin'});
        if (response.redirected && response.ok) { window.location.assign(response.url); return; }
        let detail = 'Could not save the schedule. Review the fields and try again.';
        try { const result = await response.json(); if (typeof result.detail === 'string') detail = result.detail; } catch (_) {}
        feedback.textContent = detail;
      } catch (_) {
        feedback.textContent = 'Connection interrupted. Check the schedules list before retrying; the save may have completed.';
      }
      pending = false; button.disabled = false; button.textContent = label;
      feedback.hidden = false; feedback.focus();
    });
  }
})();
