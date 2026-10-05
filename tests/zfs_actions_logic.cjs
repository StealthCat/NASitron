/* Test the actual server-rendered maintenance form and dynamic controls. */
const {JSDOM} = require('jsdom');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const html = execFileSync(process.env.PYTHON || '.venv/bin/python', ['-c', `
from jinja2 import Environment, FileSystemLoader
from types import SimpleNamespace
from remote.nasitron_root_helper import ACTION_SPECS
loader=FileSystemLoader('app/templates')
source=loader.get_source(Environment(), 'zfs_actions.html')[0].replace("{% extends 'base.html' %}", '')
env=Environment(autoescape=True)
print(env.from_string(source).render(server=SimpleNamespace(id=1,name='NAS'), csrf_token=lambda:'token', error='', result=None,
 inventory=dict(protocol=1,actions=[dict(id=k,label=v[0],warning=v[1]) for k,v in ACTION_SPECS.items()],
 pools=[dict(name='primary-z2',members=[dict(guid='200',id='mirror-0',display='mirror-0',state='ONLINE',group=True,parent_guid='',role='data'),dict(guid='300',id='scsi-old',display='scsi-old',state='ONLINE',group=False,parent_guid='200',role='data')])],
 disks=[dict(id='scsi-new',size=6000000000000)],importable=[dict(name='backup',guid='999')])))
`], {encoding:'utf8'});
const dom = new JSDOM(html, {runScripts:'outside-only', url:'http://localhost'});
const w = dom.window, d=w.document, form=d.getElementById('zfs-action-form');
w.eval(fs.readFileSync('app/static/zfs_actions.js','utf8'));
function choose(action) {form.elements.action.value=action;form.elements.action.dispatchEvent(new w.Event('change'));}
function values() {return new w.FormData(form);}
choose('replace');
assert.equal(d.getElementById('zfs-target-field').hidden, false);
assert.equal(form.elements.target.required,true);
assert.equal(form.elements.target.options.length,2); // only leaf, no RAIDZ group
assert.equal(form.querySelector('[name=disks]').value,'scsi-new');
assert.equal(form.textContent.includes('/dev/disk/by-id/'),false);
assert.equal(values().getAll('pool').length,1);
choose('attach');
assert.equal(form.elements.target.options.length,3); // RAIDZ expansion supported by host
choose('split');
assert.equal(form.querySelector('[name=disks]').value,'scsi-old');
assert.equal(form.elements.new_pool.required,true);
assert.equal(d.getElementById('zfs-layout-fields').hidden,true);
choose('create');
d.getElementById('zfs-create').value='newpool';
assert.deepEqual(values().getAll('pool'),['newpool']);
assert.equal(values().has('target'),false);
assert.equal(form.elements.role.value,'data');
choose('import');
assert.deepEqual(values().getAll('pool'),['999']);
assert.equal(values().has('disks'),false);
assert.equal(values().has('layout'),false);
choose('set');
form.elements.property.value='failmode';form.elements.property.dispatchEvent(new w.Event('change'));
assert.deepEqual([...form.elements.value.options].map(o=>o.value),['wait','continue','panic']);
choose('destroy');
assert.match(d.getElementById('zfs-warning').textContent,/DESTROYS/);
assert.equal(values().has('property'),false);
assert.equal(values().has('disks'),false);
assert.equal(d.getElementById('zfs-target-field').hidden,true);
console.log('ZFS action form checks passed');
// Selection constraints and disabled-state guidance.
choose('replace');
assert.equal(d.getElementById('zfs-preview-button').disabled,true);
form.elements.target.value='300';
const candidate=form.querySelector('[name=disks]');
assert.equal(candidate.type,'radio');
candidate.checked=true;candidate.dispatchEvent(new w.Event('change',{bubbles:true}));
assert.equal(d.getElementById('zfs-preview-button').disabled,false);
d.getElementById('zfs-disk-search').value='does-not-match';
d.getElementById('zfs-disk-search').dispatchEvent(new w.Event('input',{bubbles:true}));
assert.equal(candidate.closest('label').hidden,false); // selected disks never vanish
choose('add');
form.elements.role.value='cache';form.elements.role.dispatchEvent(new w.Event('change',{bubbles:true}));
assert.equal(form.elements.layout.value,'stripe');
assert.equal([...form.elements.layout.options].find(o=>o.value==='mirror').disabled,true);
choose('split');
form.elements.new_pool.value='backup';
form.querySelector('[name=disks]').checked=true;
form.dispatchEvent(new w.Event('change',{bubbles:true}));
assert.equal(d.getElementById('zfs-preview-button').disabled,false);
const firstSubmit=new w.Event('submit',{cancelable:true});form.dispatchEvent(firstSubmit);
assert.equal(firstSubmit.defaultPrevented,false);
const secondSubmit=new w.Event('submit',{cancelable:true});form.dispatchEvent(secondSubmit);
assert.equal(secondSubmit.defaultPrevented,true);
// Empty hosts keep actions visible with a specific reason instead of a dead form.
const emptyHtml=html.replace(/(<script id="zfs-inventory" type="application\/json">)(.*?)(<\/script>)/s,(_,a,b,c)=>{
 const inventory=JSON.parse(b);inventory.pools=[];inventory.disks=[];inventory.importable=[];return a+JSON.stringify(inventory)+c;
});
const empty=new JSDOM(emptyHtml,{runScripts:'outside-only'});
empty.window.document.getElementById('zfs-pool').replaceChildren();
empty.window.document.getElementById('zfs-import').replaceChildren();
empty.window.eval(fs.readFileSync('app/static/zfs_actions.js','utf8'));
assert.match(empty.window.document.getElementById('zfs-validation').textContent,/No imported pools/);
const confirmDom=new JSDOM('<form><input name="confirm_text" data-confirmation="REPLACE tank"><button type="submit">Execute confirmed action</button><span id="zfs-submit-status"></span></form>',{runScripts:'outside-only'});
const cw=confirmDom.window,cf=cw.document.querySelector('form'),ci=cf.querySelector('input');
cw.eval(fs.readFileSync('app/static/zfs_confirm.js','utf8'));
ci.value='wrong';ci.dispatchEvent(new cw.Event('input'));
assert.equal(ci.checkValidity(),false);
ci.value='REPLACE tank';ci.dispatchEvent(new cw.Event('input'));
assert.equal(ci.checkValidity(),true);
cf.dispatchEvent(new cw.Event('submit',{cancelable:true}));
const replay=new cw.Event('submit',{cancelable:true});cf.dispatchEvent(replay);
assert.equal(replay.defaultPrevented,true);
assert.equal(cf.querySelector('button').disabled,true);
console.log('ZFS selection, empty-state, confirmation and duplicate-submit regressions passed');
