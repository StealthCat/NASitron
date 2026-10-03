const {JSDOM}=require('jsdom');
const fs=require('node:fs'), assert=require('node:assert/strict');
const dom=new JSDOM(`<button data-assign-bay data-dialog="bay-dialog-1" data-action="/enclosures/4/assign" data-slot="3" data-location="Rack · Bay 3"></button><dialog id="bay-dialog-1" class="bay-dialog"><form><input name="slot"><input name="empty_only" value="true"><input name="csrf_token" value="test"><p data-bay-location></p><select name="identity" required><option value="">Choose</option><option value="disk-a">Disk A</option></select><p data-bay-error hidden></p><button type="button" data-close-bay-dialog>Cancel</button><button type="submit">Assign</button></form></dialog>`,{url:'http://localhost/drive-bays',runScripts:'outside-only'});
const w=dom.window,d=w.document,dialog=d.querySelector('dialog'),form=d.querySelector('form');
dialog.showModal=()=>dialog.setAttribute('open','');dialog.close=()=>dialog.removeAttribute('open');
let sent;
w.fetch=async(url,opts)=>{sent={url,opts};return {ok:false,json:async()=>({detail:'This bay was assigned while the page was open.'})};};
w.eval(fs.readFileSync('app/static/drive_bays.js','utf8'));
(async()=>{
d.querySelector('[data-assign-bay]').click();assert.ok(dialog.hasAttribute('open'));assert.equal(form.elements.slot.value,'3');assert.equal(form.action,'http://localhost/enclosures/4/assign');assert.equal(d.querySelector('[data-bay-location]').textContent,'Rack · Bay 3');
form.elements.identity.value='disk-a';form.dispatchEvent(new w.Event('submit',{cancelable:true}));await new Promise(r=>setTimeout(r,0));assert.equal(sent.opts.body.get('identity'),'disk-a');assert.equal(sent.opts.body.get('slot'),'3');assert.equal(sent.opts.body.get('empty_only'),'true');assert.equal(sent.opts.body.get('csrf_token'),'test');assert.match(d.querySelector('[data-bay-error]').textContent,/already|assigned/);assert.equal(d.querySelector('[type=submit]').disabled,false);
d.querySelector('[data-close-bay-dialog]').click();assert.equal(dialog.hasAttribute('open'),false);d.querySelector('[data-assign-bay]').click();assert.equal(form.elements.identity.value,'');assert.equal(d.querySelector('[data-bay-error]').hidden,true);dom.window.close();console.log('Bay popup selection, target, CSRF, conflict feedback, cancellation and reset passed');
})().catch(e=>{console.error(e);process.exit(1)});
