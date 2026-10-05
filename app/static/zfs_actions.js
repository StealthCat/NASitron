(() => {
  const data=JSON.parse(document.getElementById('zfs-inventory').textContent), form=document.getElementById('zfs-action-form');
  const action=form.elements.action, pool=document.getElementById('zfs-pool'), target=form.elements.target;
  const required=['attach','replace','detach','remove','offline','offline-temporary','online','expand'];
  const optional=['clear','trim','trim-pause','trim-stop','initialize','initialize-pause','initialize-stop'];
  function show(id, visible) {const el=document.getElementById(id);el.hidden=!visible;el.querySelectorAll('input,select').forEach(e=>e.disabled=!visible);}
  function render() {
    const a=action.value, p=data.pools.find(p=>p.name===pool.value), members=p?.members||[];
    document.getElementById('zfs-warning').textContent=data.actions.find(x=>x.id===a)?.warning||'';
    show('zfs-pool-field',!['create','import'].includes(a));show('zfs-create-field',a==='create');show('zfs-import-field',a==='import');
    show('zfs-target-field',required.includes(a)||optional.includes(a));target.required=required.includes(a);
    target.replaceChildren(new Option(required.includes(a)?'Choose a member':'Entire pool',''));
    members.filter(m=>m.id&&(!m.group||['attach','remove'].includes(a))).forEach(m=>target.add(new Option(m.display+' · '+m.state,m.guid)));
    show('zfs-new-pool-field',a==='split');form.elements.new_pool.required=a==='split';
    show('zfs-layout-fields',['add','create'].includes(a));
    if(a==='create') form.elements.role.value='data';
    form.elements.role.disabled=a==='create'||!['add','create'].includes(a);
    layoutChoices();
    const diskField=['add','create','attach','replace','split'].includes(a);show('zfs-disks-field',diskField);
    const choices=document.getElementById('zfs-disks');choices.replaceChildren();
    const disks=a==='split'?members.filter(m=>m.id&&!m.group).map(m=>({id:m.id})):data.disks;
    if(diskField) disks.forEach(d=>{const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.name='disks';input.value=d.id;label.append(input,document.createTextNode(d.id+(d.size?' · '+(d.size/1024**4).toFixed(2)+' TiB':'')));choices.append(label);});
    if(diskField&&!disks.length) choices.textContent='No eligible disks found.';
    show('zfs-property-fields',a==='set');propertyValues();
  }
  function layoutChoices(){const role=form.elements.role.value,layout=form.elements.layout;const allowed=['cache','spare'].includes(role)?['stripe']:['log','special','dedup'].includes(role)?['stripe','mirror']:['stripe','mirror','raidz1','raidz2','raidz3'];for(const option of layout.options)option.disabled=!allowed.includes(option.value);if(!allowed.includes(layout.value))layout.value=allowed[0];}
  function propertyValues(){const values=form.elements.property.value==='failmode'?['wait','continue','panic']:['on','off'];form.elements.value.replaceChildren(...values.map(v=>new Option(v,v)));}
  form.elements.role.addEventListener('change',layoutChoices);action.addEventListener('change',render);pool.addEventListener('change',render);form.elements.property.addEventListener('change',propertyValues);render();
})();
