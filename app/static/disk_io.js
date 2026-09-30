/* Batched disk history, one time window, shared comparison scales. */
(() => {
  const server = document.getElementById('io-server'), disk = document.getElementById('io-disk');
  const navigate = (key, value) => {
    const url = new URL(location.href); url.searchParams.set(key,value);
    if (key==='server_id') {url.searchParams.delete('identity');url.searchParams.delete('compare');}
    location.assign(url);
  };
  server?.addEventListener('change',()=>navigate('server_id',server.value));
  disk?.addEventListener('change',()=>navigate('identity',disk.value));
  const charts=document.getElementById('io-charts'); if (!charts) return;
  const form=document.getElementById('io-range-form'), range=document.getElementById('io-range');
  const start=document.getElementById('io-start'), end=document.getElementById('io-end');
  const error=document.getElementById('io-range-error'), auto=document.getElementById('io-auto');
  const compare=document.getElementById('io-compare'), metric=document.getElementById('io-compare-metric');
  const comparison=document.getElementById('io-comparisons');
  const params=new URLSearchParams(location.search);
  const localValue=date=>new Date(date-date.getTimezoneOffset()*60000).toISOString().slice(0,19);
  start.value=localValue(new Date(Date.now()-3600000)); end.value=localValue(new Date());
  if(params.has('start')&&params.has('end')) {
    const a=new Date(params.get('start')),b=new Date(params.get('end'));
    if(Number.isFinite(+a)&&Number.isFinite(+b)) {range.value='custom';start.value=localValue(a);end.value=localValue(b);}
  } else if([...range.options].some(o=>o.value===params.get('hours'))) range.value=params.get('hours');
  const selected=params.getAll('compare').slice(0,3);
  [...compare.options].forEach(o=>o.selected=selected.includes(o.value));
  if([...metric.options].some(o=>o.value===params.get('compare_metric'))) metric.value=params.get('compare_metric');
  function mode() {
    document.getElementById('io-custom').hidden=range.value!=='custom';
    start.required=end.required=range.value==='custom';auto.disabled=range.value==='custom';
    if(auto.disabled) auto.checked=false;
  }
  let generation=0, controller;
  async function render() {
    error.textContent='';
    const extra=[...compare.selectedOptions].map(o=>o.value).filter(s=>s!==charts.dataset.identity);
    if(extra.length>3) {error.textContent='Choose up to three additional drives (four total).';return;}
    let windowRange;
    const url=new URL(location.href);
    for(const key of ['hours','start','end','compare','compare_metric']) url.searchParams.delete(key);
    if(range.value==='custom') {
      const a=new Date(start.value),b=new Date(end.value);
      if(!Number.isFinite(+a)||!Number.isFinite(+b)||b<=a||b-a>365*86400000) {error.textContent='Choose an end after the start, within a maximum period of 365 days.';return;}
      windowRange={start:a.toISOString(),end:b.toISOString()};
    } else windowRange={hours:range.value};
    for(const [key,value] of Object.entries(windowRange)) url.searchParams.set(key,value);
    extra.forEach(s=>url.searchParams.append('compare',s));url.searchParams.set('compare_metric',metric.value);
    history.replaceState(null,'',url);
    controller?.abort();const request=++generation;controller=new AbortController();
    const activeController=controller, timeout=setTimeout(()=>activeController.abort(),15000);
    const canvases=[...charts.querySelectorAll('canvas')];
    const query=new URLSearchParams(windowRange);
    canvases.forEach(c=>query.append('names','drive.io.'+c.dataset.ioMetric));
    const scopes=[charts.dataset.identity,...extra];scopes.forEach(s=>query.append('scopes',s));
    charts.setAttribute('aria-busy','true');error.textContent='Loading shared history…';
    try {
      const response=await fetch(`/api/servers/${charts.dataset.server}/metrics/batch?${query}`,{signal:activeController.signal,cache:'no-store'});
      if(!response.ok) throw new Error(response.status===401?'Session expired; sign in again.':`History unavailable (${response.status}).`);
      const data=await response.json();if(request!==generation) return;
      const series=(scope,key)=>data.series.find(s=>s.scope===scope&&s.name==='drive.io.'+key);
      const options=(c,scope)=>({data:series(scope,c.dataset.ioMetric),range:windowRange,minZero:true,bytes:c.dataset.unit==='bytes',suffix:c.dataset.unit==='bytes'?'':c.dataset.unit,max100:c.dataset.ioMetric==='busy_pct'});
      canvases.forEach(c=>NASitronChart(c,'',options(c,charts.dataset.identity)));
      comparison.querySelectorAll('canvas').forEach(c=>c._dispose?.());comparison.replaceChildren();
      if(extra.length) {
        const source=canvases.find(c=>c.dataset.ioMetric===metric.value);
        const all=scopes.flatMap(s=>series(s,metric.value)?.points||[]);
        const bounds=[0,Math.max(1,...all.map(p=>p.max??p.v))];
        for(const scope of scopes) {
          const article=document.createElement('article');article.className='panel chart-wrap';
          const title=document.createElement('h2');title.textContent=scope+' · '+metric.selectedOptions[0].textContent;
          const canvas=document.createElement('canvas');canvas.className='chart';canvas.dataset.ioMetric=metric.value;canvas.dataset.unit=source.dataset.unit;
          article.append(title,canvas);comparison.append(article);
          NASitronChart(canvas,'',{...options(canvas,scope),bounds,syncGroup:'disk-comparison'});
        }
      }
      error.textContent='';
    } catch(e) {if(request===generation) error.textContent=(e.name==='AbortError'?'History request timed out.':e.message)+' Use Apply / Refresh to retry. Previously displayed charts may be from an earlier selection.';}
    finally {clearTimeout(timeout);if(request===generation)charts.removeAttribute('aria-busy');}
  }
  range.addEventListener('change',()=>{mode();if(range.value!=='custom')render();});
  form.addEventListener('submit',e=>{e.preventDefault();render();});
  compare.addEventListener('change',render);metric.addEventListener('change',render);
  setInterval(()=>{if(auto.checked&&!document.hidden)render();},60000);
  mode();render();
})();
