const {JSDOM} = require('jsdom');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const html = execFileSync(process.env.PYTHON || '.venv/bin/python', ['-c', `
from jinja2 import Environment, FileSystemLoader
from remote.nasitron_root_helper import STORAGE_ACTIONS
from types import SimpleNamespace
loader = FileSystemLoader('app/templates')
env = Environment(loader=loader, autoescape=True)
# Include every option to test the shared form's visibility and submitted values.
source = loader.get_source(env, 'storage_action_form.html')[0]
start = source.index('{% for a in data.actions if ')
end = source.index('%}', start) + 2
source = source[:start] + '{% for a in data.actions %}' + source[end:]
print(env.from_string(source).render(server=SimpleNamespace(id=1), csrf_token=lambda:'csrf', data=dict(actions=[dict(id=k,label=v[0],warning=v[1]) for k,v in STORAGE_ACTIONS.items()], datasets=[dict(name='tank/data')], snapshots=[])))
`], {encoding:'utf8'});
const dom = new JSDOM(html + `<input data-storage-filter="rows"><table id="rows"><tbody><tr><td>tank/photos</td></tr><tr><td>tank/media</td></tr></tbody></table><form id="storage-policy-form"><div id="storage-policy-feedback" tabindex="-1" hidden></div><input name="name" value="Daily"><input name="confirm_text"><button class="primary">Enable schedule</button><select name="kind"><option>snapshot</option><option>replication</option><option>smart</option></select><div data-policy-kinds="snapshot replication"><input name="dataset" value="tank/data"></div><div data-policy-kinds="replication"><input name="destination" value="backup/data"></div><div data-policy-kinds="smart"><input name="disk" value="scsi-disk"></div></form>`, {runScripts:'outside-only'});
const w=dom.window, d=w.document, form=d.getElementById('storage-action-form');
w.eval(fs.readFileSync('app/static/storage.js','utf8'));
function choose(action) { form.elements.action.value=action; form.elements.action.dispatchEvent(new w.Event('change')); }
function values() {return new w.FormData(form);}
choose('dataset-set');
assert.equal(form.elements.property.disabled,false);
assert.equal(form.elements.value.required,true);
choose('dataset-inherit');
assert.equal(values().has('value'),false);
assert.equal(values().has('property'),true);
choose('snapshot-clone');
assert.equal(form.elements.new_pool.required,true);
assert.equal(values().has('property'),false);
form.elements.target.value='tank/data@backup';
form.dispatchEvent(new w.Event('submit'));
assert.equal(values().get('pool'),'tank');
choose('helper-update');
assert.equal(values().has('target'),false);
assert.equal(values().has('new_pool'),false);
form.dispatchEvent(new w.Event('submit'));
assert.equal(values().get('pool'),'host');
choose('smart-long');
assert.equal(form.elements.target.required,true);
const filter=d.querySelector('[data-storage-filter]');filter.value='photos';filter.dispatchEvent(new w.Event('input'));
assert.deepEqual([...d.querySelectorAll('tbody tr')].map(r=>r.hidden),[false,true]);
const policy=d.getElementById('storage-policy-form');
assert.equal(new w.FormData(policy).has('disk'),false);
policy.elements.kind.value='smart';policy.elements.kind.dispatchEvent(new w.Event('change'));
assert.equal(new w.FormData(policy).has('dataset'),false);
assert.equal(new w.FormData(policy).has('disk'),true);
console.log('Storage action forms, snapshot filters and policy fields passed');

(async () => {
  const confirmation=policy.elements.confirm_text;
  assert.equal(confirmation.checkValidity(),false);
  confirmation.value='ENABLE Daily';confirmation.dispatchEvent(new w.Event('input'));
  assert.equal(confirmation.checkValidity(),true);
  let calls=0;
  w.fetch=async()=>{calls++;return {ok:false,redirected:false,json:async()=>({detail:'Choose a different NAS host'})};};
  policy.dispatchEvent(new w.Event('submit',{cancelable:true}));
  policy.dispatchEvent(new w.Event('submit',{cancelable:true}));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls,1);
  assert.equal(policy.elements.disk.value,'scsi-disk');
  assert.equal(policy.querySelector('button').disabled,false);
  assert.equal(d.getElementById('storage-policy-feedback').hidden,false);
  assert.equal(d.getElementById('storage-policy-feedback').textContent,'Choose a different NAS host');
  console.log('Schedule validation, duplicate submission and inline error recovery passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
