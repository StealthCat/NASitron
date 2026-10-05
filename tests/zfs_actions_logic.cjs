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
 pools=[dict(name='primary-z2',members=[dict(guid='200',id='raidz2-0',display='raidz2-0',state='ONLINE',group=True),dict(guid='300',id='scsi-old',display='scsi-old',state='ONLINE',group=False)])],
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
