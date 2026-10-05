(() => {
  const data = JSON.parse(document.getElementById('zfs-inventory').textContent);
  const form = document.getElementById('zfs-action-form');
  const action = form.elements.action, pool = document.getElementById('zfs-pool'), target = form.elements.target;
  const choices = document.getElementById('zfs-disks'), search = document.getElementById('zfs-disk-search');
  const required = ['attach', 'replace', 'detach', 'remove', 'offline', 'offline-temporary', 'online', 'expand'];
  const optional = ['clear', 'trim', 'trim-pause', 'trim-stop', 'initialize', 'initialize-pause', 'initialize-stop'];
  const newDisks = ['add', 'create', 'attach', 'replace'];
  const capabilities = data.capabilities || {};
  for (const option of action.options) {
    const base = option.value.split('-')[0];
    if (capabilities.commands?.[base] === false) { option.disabled = true; option.textContent += ' — unsupported on this host'; }
  }
  let submitted = false;
  function show(id, visible) {
    const el = document.getElementById(id);
    el.hidden = !visible;
    el.querySelectorAll('input,select').forEach(e => e.disabled = !visible);
  }
  function members() { return data.pools.find(p => p.name === pool.value)?.members || []; }
  function render() {
    const a = action.value, current = members();
    document.getElementById('zfs-warning').textContent = data.actions.find(x => x.id === a)?.warning || '';
    show('zfs-pool-field', !['create', 'import'].includes(a));
    show('zfs-create-field', a === 'create');
    show('zfs-import-field', a === 'import');
    show('zfs-target-field', required.includes(a) || optional.includes(a));
    target.required = required.includes(a);
    target.replaceChildren(new Option(target.required ? 'Choose a member' : 'Entire pool', ''));
    const expansion = (capabilities.raidz_expansion || []).some(row => row[0] === pool.value && ['enabled','active'].includes(row[2]));
    current.filter(m => m.id && (!m.group || ['attach', 'remove'].includes(a)) && !(a === 'attach' && /^raidz/.test(m.id) && data.capabilities && !expansion))
      .forEach(m => target.add(new Option(`${m.display} · ${m.role || 'data'} · ${m.state}`, m.guid)));
    show('zfs-new-pool-field', a === 'split');
    form.elements.new_pool.required = a === 'split';
    show('zfs-layout-fields', ['add', 'create'].includes(a));
    if (a === 'create') form.elements.role.value = 'data';
    form.elements.role.disabled = a === 'create' || !['add', 'create'].includes(a);
    layoutChoices();
    const diskField = newDisks.includes(a) || a === 'split';
    show('zfs-disks-field', diskField);
    document.getElementById('zfs-disk-legend').textContent = a === 'split' ? 'Mirror members to split' : ['attach', 'replace'].includes(a) ? 'Select one new disk' : 'Select new disks';
    document.getElementById('zfs-disk-help').textContent = a === 'split'
      ? 'Select exactly one direct member from every data, special and dedup mirror. Other members stay in the source pool.'
      : 'Blank whole disks only. Names use stable by-id identifiers; no force, wipe or repartition shortcuts.';
    choices.replaceChildren();
    search.value = '';
    const roots = current.filter(m => !m.parent_guid && /^mirror-\d+$/.test(m.id) && ['data', 'special', 'dedup'].includes(m.role || 'data'));
    const disks = a === 'split' ? current.filter(m => m.id && !m.group && roots.some(r => r.guid === m.parent_guid)) : data.disks;
    if (diskField) disks.forEach(disk => {
      const label = document.createElement('label'), input = document.createElement('input');
      const copy = document.createElement('span'), name = document.createElement('strong'), meta = document.createElement('span');
      input.type = ['attach', 'replace'].includes(a) ? 'radio' : 'checkbox';
      input.name = 'disks'; input.value = disk.id;
      name.textContent = disk.id; copy.className = 'zfs-disk-copy'; meta.className = 'subtle';
      meta.textContent = a === 'split' ? `${roots.find(r => r.guid === disk.parent_guid)?.id} · ${disk.state}`
        : `${(disk.size / 1024 ** 4).toFixed(2)} TiB · Blank disk`;
      copy.append(name, meta); label.append(input, copy); choices.append(label);
    });
    if (diskField && !disks.length) choices.textContent = a === 'split' ? 'No eligible mirror members. A helper update may be required.' : 'No eligible blank disks. Rescan after connecting a blank drive.';
    show('zfs-property-fields', a === 'set');
    propertyValues(); filterDisks(); validate();
  }
  function layoutChoices() {
    const role = form.elements.role.value, layout = form.elements.layout;
    const allowed = ['cache', 'spare'].includes(role) ? ['stripe'] : ['log', 'special', 'dedup'].includes(role) ? ['stripe', 'mirror'] : ['stripe', 'mirror', 'raidz1', 'raidz2', 'raidz3'];
    for (const option of layout.options) option.disabled = !allowed.includes(option.value);
    if (!allowed.includes(layout.value)) layout.value = allowed[0];
  }
  function propertyValues() {
    const values = form.elements.property.value === 'failmode' ? ['wait', 'continue', 'panic'] : ['on', 'off'];
    form.elements.value.replaceChildren(...values.map(v => new Option(v, v)));
  }
  function filterDisks() {
    const query = search.value.trim().toLowerCase();
    const labels = [...choices.querySelectorAll('label')];
    labels.forEach(label => label.hidden = !label.textContent.toLowerCase().includes(query) && !label.querySelector('input').checked);
    document.getElementById('zfs-no-matches').hidden = !labels.length || labels.some(label => !label.hidden);
  }
  function validate() {
    const a = action.value, selected = [...choices.querySelectorAll('input:checked')];
    let message = '';
    if (a === 'create' && !document.getElementById('zfs-create').value.trim()) message = 'Enter a name for the new pool.';
    else if (a === 'import' && !document.getElementById('zfs-import').value) message = 'No exported pools were discovered.';
    else if (!['create', 'import'].includes(a) && !pool.value) message = 'No imported pools. Choose Create or Import to begin.';
    else if (required.includes(a) && !target.value) message = 'Choose the existing member or vdev.';
    else if (newDisks.includes(a)) {
      const minimum = ['attach', 'replace'].includes(a) ? 1 : ({stripe: 1, mirror: 2, raidz1: 2, raidz2: 3, raidz3: 4}[form.elements.layout.value]);
      if (selected.length < minimum) message = `Select ${minimum === 1 ? 'a disk' : 'at least ' + minimum + ' disks'} for this action.`;
    } else if (a === 'split') {
      const current = members(), roots = current.filter(m => !m.parent_guid && ['data', 'special', 'dedup'].includes(m.role || 'data'));
      if (!roots.length || roots.some(r => !/^mirror-\d+$/.test(r.id))) message = 'Split requires mirrored data, special and dedup vdevs.';
      else if (roots.some(r => selected.filter(input => current.find(m => m.id === input.value)?.parent_guid === r.guid).length !== 1)) message = 'Select exactly one disk from every mirror.';
      else if (!form.elements.new_pool.value.trim()) message = 'Enter the destination pool name.';
    }
    document.getElementById('zfs-selected-count').textContent = `${selected.length} selected`;
    document.getElementById('zfs-validation').textContent = message || 'Ready to review. The NAS will validate this selection before execution.';
    document.getElementById('zfs-preview-button').disabled = Boolean(message) || submitted;
    return !message;
  }
  form.elements.role.addEventListener('change', () => {
    if (['log', 'special', 'dedup'].includes(form.elements.role.value)) form.elements.layout.value = 'mirror';
    layoutChoices(); validate();
  });
  action.addEventListener('change', render); pool.addEventListener('change', render);
  form.elements.property.addEventListener('change', propertyValues);
  search.addEventListener('input', filterDisks);
  form.addEventListener('change', () => { filterDisks(); validate(); });
  form.addEventListener('input', validate);
  form.addEventListener('submit', event => {
    if (submitted || !validate()) { event.preventDefault(); return; }
    submitted = true;
    const button = document.getElementById('zfs-preview-button');
    button.disabled = true; button.textContent = 'Validating with NAS…';
  });
  window.addEventListener('pageshow', () => { submitted = false; document.getElementById('zfs-preview-button').textContent = 'Review command →'; validate(); });
  render();
})();
