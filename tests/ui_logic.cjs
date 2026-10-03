/* DOM-level regressions; npm install --no-save jsdom@26.1.0 */
const {JSDOM}=require('jsdom');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const script=fs.readFileSync('app/static/app.js','utf8');
const tick=()=>new Promise(r=>setTimeout(r,0));
function dom(html) {
  const d=new JSDOM(html,{url:'http://localhost/',runScripts:'outside-only'}),w=d.window;
  w.matchMedia=()=>({matches:false,addEventListener(){}});
  w.ResizeObserver=class {observe(){} disconnect(){}};
  w.HTMLCanvasElement.prototype.getBoundingClientRect=()=>({width:600,height:225,left:0});
  const ctx=new Proxy({createLinearGradient:()=>({addColorStop(){}}),measureText:()=>({width:20})},{get:(t,p)=>p in t?t[p]:()=>{}});
  w.HTMLCanvasElement.prototype.getContext=()=>ctx;
  return d;
}
(async()=>{
  const d=dom('<article class="panel"><h2>History</h2><canvas></canvas></article>'),w=d.window;
  const requests=[];
  w.fetch=(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}));
  w.eval(script);await tick();
  w.NASitronChart(w.document.querySelector('canvas'),'/metrics');
  const range=w.document.querySelector('.chart-controls select');
  range.value='6';range.dispatchEvent(new w.Event('change'));
  assert.equal(requests.length,2);
  assert.equal(requests[0].options.signal.aborted,true);
  assert.match(requests[1].url.search,/hours=6/);
  const response=v=>({ok:true,json:async()=>({points:[{t:new Date().toISOString(),v}],bucket_seconds:60})});
  requests[1].resolve(response(20));await tick();
  requests[0].resolve(response(99));await tick();
  assert.match(w.document.querySelector('details p').textContent,/latest 20.0/);
  assert.doesNotMatch(w.document.querySelector('details p').textContent,/99/);
  d.window.close();

  const table=dom('<div class="scroll"><table data-table="test"><thead><tr><th>Name</th><th>Size</th></tr></thead><tbody>'+Array.from({length:60},(_,i)=>`<tr><td>Disk ${i}</td><td data-sort="${i}">${i} B</td></tr>`).join('')+'</tbody></table></div>');
  table.window.localStorage.setItem('nasitron:table:/:test',JSON.stringify({sort:999,columns:'bad',pageSize:3}));
  table.window.eval(script);await tick();
  const document=table.window.document;
  assert.equal(document.querySelectorAll('tbody tr:not([hidden])').length,50);
  const input=document.querySelector('input[type=search]');input.value='Disk 59';input.dispatchEvent(new table.window.Event('input'));
  await new Promise(r=>setTimeout(r,180));
  assert.equal(document.querySelectorAll('tbody tr:not([hidden])').length,1);
  assert.match(document.querySelector('tbody tr:not([hidden])').textContent,/Disk 59/);
  const reset=[...document.querySelectorAll('button')].find(b=>b.textContent==='Reset table');reset.click();
  assert.equal(document.querySelectorAll('tbody tr:not([hidden])').length,50);
  assert.equal(document.querySelector('th').getAttribute('aria-sort'),'ascending');
  table.window.close();
  const tz=dom('<body data-timezone="America/New_York"></body>');tz.window.eval(script);await tick();
  assert.equal(tz.window.NASitronTime.toUTC('2026-07-01T12:00').toISOString(),'2026-07-01T16:00:00.000Z');
  assert.ok(Number.isNaN(+tz.window.NASitronTime.toUTC('2026-03-08T02:30')));
  assert.equal(tz.window.NASitronTime.toUTC('2026-11-01T01:30').toISOString(),'2026-11-01T05:30:00.000Z');
  tz.window.close();

  for (const prefix of ['drive.io.', 'pool.']) {
  const io=dom(`<select id="io-server"><option>1</option></select><select id="io-disk"><option>A</option></select><div id="io-compare"><input id="io-drive-search"><label><input type="checkbox" data-drive-compare value="B">Drive B</label><div id="io-selected"></div></div><p id="io-selection-state"></p><select id="io-compare-metric"><option value="read_bps">Read</option></select><form id="io-range-form"><select id="io-range"><option value="1">1h</option><option value="custom">Custom</option></select><input id="io-start"><input id="io-end"><input id="io-auto" type="checkbox"><div id="io-custom"></div></form><p id="io-range-error"></p><div id="io-comparisons"></div><div id="io-charts" data-metric-prefix="${prefix}" data-server="1" data-identity="A"><article class="panel"><h2>Read</h2><canvas data-io-metric="read_bps" data-unit="bytes"></canvas></article></div>`);
  let calls=0;io.window.fetch=async url=>{calls++;const u=new URL(url,'http://localhost');assert.ok(u.searchParams.getAll('pairs').every(p=>JSON.parse(p)[0].startsWith(prefix)));return {ok:true,json:async()=>({series:u.searchParams.getAll('pairs').map(p=>JSON.parse(p)).map(([name,scope])=>({scope,name,points:[{t:new Date().toISOString(),v:5,min:1,max:10}]}))})};};
  io.window.eval(script);await tick();io.window.eval(fs.readFileSync('app/static/disk_io.js','utf8'));await tick();
  assert.equal(calls,1,'A disk page uses a single batched history request');
  const compare=io.window.document.querySelector('#io-compare');compare.querySelector('input[type=checkbox]').checked=true;compare.dispatchEvent(new io.window.Event('change'));await tick();
  assert.equal(calls,2);
  assert.equal(io.window.document.querySelectorAll('#io-comparisons canvas').length,2);
  assert.match(io.window.document.querySelector('#io-charts .chart-status').textContent,/Updated/);
  io.window.close();
  }
  const temp=dom('<article class="panel"><h2>Temperature</h2><canvas></canvas></article>');
  temp.window.eval(script);await tick();
  temp.window.NASitronChart(temp.window.document.querySelector('canvas'),'',{temperatureUnit:'F',suffix:'°F',data:{points:[{t:new Date().toISOString(),v:45,min:40,max:50}]}});
  await tick();
  assert.match(temp.window.document.querySelector('details p').textContent,/Minimum 104.0°F; maximum 122.0°F; latest 113.0°F/);
  temp.window.close();
  console.log('Chart race, inventory paging, and batched comparison checks passed');
})().catch(e=>{console.error(e);process.exit(1)});
