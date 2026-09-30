/* One shared time window for all disk charts; dates are converted to UTC. */
(() => {
  const server = document.getElementById('io-server');
  const disk = document.getElementById('io-disk');
  const navigate = (key, value) => {
    const url = new URL(location.href);
    url.searchParams.set(key, value);
    if (key === 'server_id') url.searchParams.delete('identity');
    location.assign(url);
  };
  server?.addEventListener('change', () => navigate('server_id', server.value));
  disk?.addEventListener('change', () => navigate('identity', disk.value));
  const charts = document.getElementById('io-charts');
  if (!charts) return;
  const form = document.getElementById('io-range-form');
  const range = document.getElementById('io-range');
  const start = document.getElementById('io-start');
  const end = document.getElementById('io-end');
  const error = document.getElementById('io-range-error');
  const auto = document.getElementById('io-auto');
  const params = new URLSearchParams(location.search);
  const localValue = date => new Date(date - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  start.value = localValue(new Date(Date.now() - 3600000));
  end.value = localValue(new Date());
  if (params.has('start') && params.has('end')) {
    const a = new Date(params.get('start')), b = new Date(params.get('end'));
    if (Number.isFinite(+a) && Number.isFinite(+b)) {
      range.value = 'custom'; start.value = localValue(a); end.value = localValue(b);
    }
  } else if ([...range.options].some(o => o.value === params.get('hours'))) range.value = params.get('hours');
  function mode() {
    document.getElementById('io-custom').hidden = range.value !== 'custom';
    start.required = end.required = range.value === 'custom';
    auto.disabled = range.value === 'custom';
    if (auto.disabled) auto.checked = false;
  }
  function render() {
    error.textContent = '';
    let windowRange;
    const url = new URL(location.href);
    for (const key of ['hours', 'start', 'end']) url.searchParams.delete(key);
    if (range.value === 'custom') {
      const a = new Date(start.value), b = new Date(end.value);
      if (!Number.isFinite(+a) || !Number.isFinite(+b) || b <= a || b - a > 365 * 86400000) {
        error.textContent = 'Choose an end after the start, within a maximum period of 365 days.';
        return;
      }
      windowRange = {start: a.toISOString(), end: b.toISOString()};
    } else {
      windowRange = {hours: range.value};
    }
    for (const [key, value] of Object.entries(windowRange)) url.searchParams.set(key, value);
    history.replaceState(null, '', url);
    charts.querySelectorAll('canvas').forEach(canvas => {
      const query = new URLSearchParams({name: 'drive.io.' + canvas.dataset.ioMetric, scope: charts.dataset.identity});
      NASitronChart(canvas, `/api/servers/${charts.dataset.server}/metrics?${query}`, {
        range: windowRange, minZero: true, bytes: canvas.dataset.unit === 'bytes',
        suffix: canvas.dataset.unit === 'bytes' ? '' : canvas.dataset.unit,
        max100: canvas.dataset.ioMetric === 'busy_pct'
      });
    });
  }
  range.addEventListener('change', () => {mode(); if (range.value !== 'custom') render();});
  form.addEventListener('submit', event => {event.preventDefault(); render();});
  setInterval(() => {if (auto.checked && !document.hidden) render();}, 60000);
  mode(); render();
})();
