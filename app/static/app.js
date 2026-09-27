/* Shared charts and accessible, persistent inventory controls. No external CDN. */
const preference = {
  read(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem('nasitron:' + key)) ?? fallback;
    } catch (_) {
      return fallback;
    }
  },
  write(key, value) {
    try {
      localStorage.setItem('nasitron:' + key, JSON.stringify(value));
    } catch (_) {}
  }
};
window.NASitronChart = function(canvas, source, options = {}) {
  if (!canvas) return;
  canvas._dispose?.();
  const controls = document.createElement('div');
  controls.className = 'chart-controls';
  const rangeLabel = document.createElement('label');
  rangeLabel.textContent = 'Range ';
  const range = document.createElement('select');
  for (const [h, label] of [
      [0.25, '15 minutes'],
      [1, '1 hour'],
      [6, '6 hours'],
      [24, '24 hours'],
      [168, '7 days'],
      [720, '30 days']
    ]) range.add(new Option(label, h));
  range.value = String(options.range?.hours || '24');
  rangeLabel.append(range);
  const refresh = document.createElement('button');
  refresh.type = 'button';
  refresh.textContent = 'Refresh';
  const autoLabel = document.createElement('label');
  const auto = document.createElement('input');
  auto.type = 'checkbox';
  autoLabel.append(auto, ' Auto refresh (60s)');
  controls.append(rangeLabel, refresh, autoLabel);
  if (!options.range) canvas.before(controls);
  const status = document.createElement('p');
  status.className = 'chart-status subtle';
  status.setAttribute('role', 'status');
  canvas.after(status);
  const tooltip = document.createElement('p');
  tooltip.className = 'chart-tooltip';
  tooltip.hidden = true;
  status.after(tooltip);
  const summary = document.createElement('details');
  const summaryTitle = document.createElement('summary');
  summaryTitle.textContent = 'Accessible data summary';
  const summaryText = document.createElement('p');
  summary.append(summaryTitle, summaryText);
  tooltip.after(summary);
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', 'Historical metric chart. A text summary follows.');
  canvas.tabIndex = 0;
  let points = [],
    gap = 180000,
    loading = false,
    abort, disposed = false,
    selected = 0, windowStart, windowEnd;
  const ctx = canvas.getContext('2d');
  const format = value => options.bytes ? (() => {
    let i = 0,
      n = value;
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    while (Math.abs(n) >= 1024 && i < u.length - 1) {
      n /= 1024;
      i++;
    }
    return n.toFixed(1) + ' ' + u[i] + '/s';
  })() : value.toFixed(options.decimals ?? 1) + (options.suffix || '');

  function draw() {
    const width = Math.max(280, canvas.getBoundingClientRect().width),
      height = 220,
      dpr = devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!points.length) return;
    let lo = Math.min(...points.map(p => p.v)),
      hi = Math.max(...points.map(p => p.v));
    if (options.minZero) lo = Math.min(0, lo);
    if (options.max100) hi = Math.max(100, hi);
    if (lo === hi) {
      lo = Math.max(0, lo - 1);
      hi += 1;
    }
    const left = 65,
      right = 14,
      top = 12,
      bottom = 40,
      end = windowEnd || Date.now(),
      start = windowStart || end - Number(range.value) * 3600000;
    const x = t => left + (t - start) / (end - start) * (width - left - right),
      y = v => top + (hi - v) / (hi - lo) * (height - top - bottom);
    ctx.font = '11px system-ui';
    ctx.fillStyle = '#a9b7cb';
    ctx.strokeStyle = '#263447';
    for (let i = 0; i < 5; i++) {
      const v = lo + (hi - lo) * i / 4;
      ctx.beginPath();
      ctx.moveTo(left, y(v));
      ctx.lineTo(width - right, y(v));
      ctx.stroke();
      ctx.fillText(format(v), 2, y(v) + 4);
    }
    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, width - left - right, height - top - bottom);
    ctx.clip();
    ctx.strokeStyle = '#FC1859';
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((p, i) => {
      if (i === 0 || p.t - points[i - 1].t > gap) ctx.moveTo(x(p.t), y(p.v));
      else ctx.lineTo(x(p.t), y(p.v));
    });
    ctx.stroke();
    // Isolated observations must remain visible without drawing across gaps.
    points.forEach((p, i) => {
      const beforeGap = i === 0 || p.t - points[i - 1].t > gap;
      const afterGap = i === points.length - 1 || points[i + 1].t - p.t > gap;
      if (beforeGap && afterGap) {
        ctx.beginPath();
        ctx.arc(x(p.t), y(p.v), 2.5, 0, Math.PI * 2);
        ctx.fillStyle = '#FC1859';
        ctx.fill();
      }
    });
    ctx.restore();
    const time = t => new Date(t).toLocaleString([], {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    });
    ctx.fillStyle = '#a9b7cb';
    ctx.fillText(time(start), left, height - 12);
    const last = time(end);
    ctx.fillText(last, width - right - ctx.measureText(last).width, height - 12);
    canvas._timeAt = px => start + Math.max(0, Math.min(1, (px - left) / (width - left - right))) * (end - start);
  }
  async function load() {
    if (loading || disposed) return;
    loading = true;
    refresh.disabled = true;
    status.textContent = 'Loading history…';
    abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 15000);
    try {
      const url = new URL(source, location.origin);
      if (options.range) {
        for (const [key, value] of Object.entries(options.range)) url.searchParams.set(key, value);
      } else url.searchParams.set('hours', range.value);
      const response = await fetch(url, {
        cache: 'no-store',
        signal: abort.signal
      });
      if (!response.ok) throw new Error(response.status === 401 ? 'Session expired; sign in again.' : 'History unavailable (' + response.status + ').');
      const data = await response.json();
      windowStart = Date.parse(data.start || options.range?.start) || undefined;
      windowEnd = Date.parse(data.end || options.range?.end) || undefined;
      if (disposed) return;
      points = (data.points || []).map(p => ({
        t: Date.parse(p.t),
        v: Number(p.v)
      })).filter(p => Number.isFinite(p.t) && Number.isFinite(p.v)).sort((a, b) => a.t - b.t);
      gap = Math.max(data.expected_interval_seconds || 60, data.bucket_seconds || 1) * 3000;
      status.textContent = points.length ? 'Updated ' + new Date().toLocaleTimeString() + ' · ' + points.length + ' samples · times shown in your browser timezone' : 'No samples in this time range.';
      summaryText.textContent = points.length ? `Minimum ${format(Math.min(...points.map(p=>p.v)))}; maximum ${format(Math.max(...points.map(p=>p.v)))}; latest ${format(points[points.length-1].v)} at ${new Date(points[points.length-1].t).toLocaleString()}. Missing intervals are gaps. Use left/right arrows on the chart for individual samples.` : 'No data available.';
      draw();
    } catch (error) {
      if (!disposed) status.textContent = (error.name === 'AbortError' ? 'Request timed out.' : error.message) + ' Select Refresh to retry.';
    } finally {
      clearTimeout(timeout);
      loading = false;
      refresh.disabled = false;
    }
  }

  function show(index) {
    if (!points.length) return;
    selected = Math.max(0, Math.min(points.length - 1, index));
    const p = points[selected];
    tooltip.textContent = new Date(p.t).toLocaleString() + ' · ' + format(p.v);
    tooltip.hidden = false;
  }

  function pointer(e) {
    if (!points.length || !canvas._timeAt) return;
    const t = canvas._timeAt(e.clientX - canvas.getBoundingClientRect().left);
    let index = 0;
    points.forEach((p, i) => {
      if (Math.abs(p.t - t) < Math.abs(points[index].t - t)) index = i;
    });
    show(index);
  }

  function key(e) {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      show(selected + (e.key === 'ArrowRight' ? 1 : -1));
    }
  }
  canvas.addEventListener('pointermove', pointer);
  canvas.addEventListener('keydown', key);
  range.addEventListener('change', load);
  refresh.addEventListener('click', load);
  const observer = new ResizeObserver(draw);
  observer.observe(canvas);
  const timer = setInterval(() => {
    if (auto.checked && !document.hidden && canvas.getClientRects().length) load();
  }, 60000);
  canvas._dispose = () => {
    disposed = true;
    abort?.abort();
    clearInterval(timer);
    observer.disconnect();
    controls.remove();
    status.remove();
    tooltip.remove();
    summary.remove();
    canvas.removeEventListener('pointermove', pointer);
    canvas.removeEventListener('keydown', key);
  };
  load();
};

window.toggleSidebar = function(force) {
  const sidebar = document.getElementById('sidebar'),
    backdrop = document.getElementById('sidebar-backdrop'),
    toggle = document.getElementById('nav-toggle');
  if (!sidebar) return;
  const open = typeof force === 'boolean' ? force : !sidebar.classList.contains('open');
  sidebar.classList.toggle('open', open);
  backdrop?.classList.toggle('open', open);
  toggle?.setAttribute('aria-expanded', String(open));
  if (open) sidebar.querySelector('a')?.focus();
  else toggle?.focus();
};
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') window.toggleSidebar(false);
  const sidebar = document.getElementById('sidebar');
  if (event.key === 'Tab' && sidebar?.classList.contains('open')) {
    const items = [...sidebar.querySelectorAll('a,button,input,select')].filter(e => e.getClientRects().length);
    if (event.shiftKey && document.activeElement === items[0]) {
      event.preventDefault();
      items.at(-1)?.focus();
    } else if (!event.shiftKey && document.activeElement === items.at(-1)) {
      event.preventDefault();
      items[0]?.focus();
    }
  }
});

function inventory(table, index) {
  const rows = [...table.tBodies[0]?.rows || []].filter(r => r.cells.length === table.tHead?.rows[0].cells.length);
  if (!rows.length) return;
  const key = 'table:' + location.pathname + ':' + (table.dataset.table || index),
    saved = preference.read(key, {});
  const headers = [...table.tHead.rows[0].cells],
    wrapper = table.closest('.scroll') || table;
  const toolbar = document.createElement('div');
  toolbar.className = 'table-toolbar';
  const dataset = table.dataset.table === 'datasets';
  const search = dataset ? document.getElementById('dataset-search') : document.createElement('input');
  search.type = 'search';
  search.placeholder = 'Search name, server, pool, serial…';
  search.setAttribute('aria-label', 'Search inventory');
  search.value = saved.query || '';
  const type = dataset ? document.getElementById('dataset-type-filter') : null;
  if (type) type.value = saved.type || 'all';
  if (!dataset) {
    const label = document.createElement('label');
    label.textContent = 'Search ';
    label.append(search);
    toolbar.append(label);
  }
  const selectors = [];
  for (const field of ['server', 'health']) {
    const values = [...new Set(rows.map(r => r.dataset[field]).filter(Boolean))].sort();
    if (!values.length) continue;
    const label = document.createElement('label');
    label.textContent = field === 'server' ? 'Server ' : 'Health ';
    const select = document.createElement('select');
    select.add(new Option('All', ''));
    values.forEach(v => select.add(new Option(v, v)));
    select.value = saved[field] || '';
    label.append(select);
    toolbar.append(label);
    selectors.push([field, select]);
  }
  const columns = document.createElement('details'),
    columnTitle = document.createElement('summary');
  columnTitle.textContent = 'Columns';
  columns.append(columnTitle);
  let visible = saved.columns || ((table.dataset.defaultColumns || '').split(',').filter(Boolean).map(Number));
  if (!visible.length) visible = headers.map((_, i) => i);
  headers.forEach((h, i) => {
    const label = document.createElement('label'),
      check = document.createElement('input');
    check.type = 'checkbox';
    check.checked = visible.includes(i);
    label.append(check, ' ' + h.textContent.trim());
    columns.append(label);
    check.addEventListener('change', () => {
      if (check.checked) visible.push(i);
      else visible = visible.filter(v => v !== i);
      render();
    });
  });
  const reset = document.createElement('button');
  reset.type = 'button';
  reset.textContent = 'Reset filters';
  const count = document.createElement('span');
  count.setAttribute('role', 'status');
  const prev = document.createElement('button'),
    next = document.createElement('button');
  prev.type = next.type = 'button';
  prev.textContent = 'Previous';
  next.textContent = 'Next';
  const pageSize = document.createElement('select');
  [25, 50, 100, 250].forEach(n => pageSize.add(new Option(n + ' rows', n)));
  pageSize.setAttribute('aria-label', 'Rows per page');
  pageSize.value = saved.pageSize || '50';
  toolbar.append(columns, reset, pageSize, prev, count, next);
  wrapper.before(toolbar);
  let page = 0,
    sort = Number.isInteger(saved.sort) ? saved.sort : 0,
    descending = !!saved.descending;

  function value(cell) {
    if (cell.dataset.sort !== undefined) return Number(cell.dataset.sort);
    const text = cell.textContent.trim();
    const size = text.match(/^([\d.]+)\s*(KiB|MiB|GiB|TiB|PiB|B)(?:$|\/)/);
    if (size) return Number(size[1]) * 1024 ** (['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'].indexOf(size[2]));
    if (/^[-+]?\d+(\.\d+)?(?:%|x|°C| h)?$/.test(text)) return parseFloat(text);
    return text;
  }

  function render() {
    const query = search.value.toLowerCase().trim();
    const matching = rows.filter(r => r.textContent.toLowerCase().includes(query) && selectors.every(([key, select]) => !select.value || r.dataset[key] === select.value) && (!type || type.value === 'all' || r.dataset.datasetType === type.value || (type.value === 'root' && r.dataset.root === 'true')));
    matching.sort((a, b) => {
      const x = value(a.cells[sort]),
        y = value(b.cells[sort]);
      return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), undefined, {
        numeric: true
      })) * (descending ? -1 : 1);
    });
    page = Math.max(0, Math.min(page, Math.ceil(matching.length / Number(pageSize.value)) - 1));
    rows.forEach(r => r.hidden = true);
    matching.forEach((r, i) => {
      table.tBodies[0].append(r);
      r.hidden = i < page * Number(pageSize.value) || i >= (page + 1) * Number(pageSize.value);
    });
    headers.forEach((h, i) => {
      h.hidden = !visible.includes(i);
      h.setAttribute('aria-sort', i === sort ? (descending ? 'descending' : 'ascending') : 'none');
      rows.forEach(r => r.cells[i].hidden = !visible.includes(i));
    });
    prev.disabled = page === 0;
    next.disabled = (page + 1) * Number(pageSize.value) >= matching.length;
    count.textContent = matching.length + ' matches · page ' + (page + 1) + ' / ' + Math.max(1, Math.ceil(matching.length / Number(pageSize.value)));
    if (dataset) {
      document.getElementById('dataset-visible-count').textContent = matching.length;
      document.getElementById('dataset-empty-filter').hidden = matching.length !== 0;
    }
    const state = {
      query: search.value,
      columns: visible,
      sort,
      descending,
      pageSize: pageSize.value,
      type: type?.value
    };
    selectors.forEach(([k, s]) => state[k] = s.value);
    preference.write(key, state);
  }
  headers.forEach((h, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'sort-button';
    b.textContent = h.textContent.trim() + ' ↕';
    h.replaceChildren(b);
    b.addEventListener('click', () => {
      descending = sort === i ? !descending : false;
      sort = i;
      render();
    });
  });
  [search, type, pageSize, ...selectors.map(x => x[1])].filter(Boolean).forEach(e => e.addEventListener(e === search ? 'input' : 'change', () => {
    page = 0;
    render();
  }));
  reset.addEventListener('click', () => {
    search.value = '';
    if (type) type.value = 'all';
    selectors.forEach(x => x[1].value = '');
    page = 0;
    render();
  });
  prev.addEventListener('click', () => {
    page--;
    render();
  });
  next.addEventListener('click', () => {
    page++;
    render();
  });
  render();
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('table[data-table]').forEach(inventory);
  const density = document.getElementById('display-density');
  if (density) {
    density.value = preference.read('density', 'compact');
    document.body.dataset.density = density.value;
    density.addEventListener('change', () => {
      document.body.dataset.density = density.value;
      preference.write('density', density.value);
    });
  }
  const updated = document.getElementById('page-updated');
  if (updated) updated.textContent = 'Page loaded ' + new Date().toLocaleTimeString();
  document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(document.getElementById(button.dataset.copy).textContent);
      button.textContent = 'Copied';
    } catch (_) {
      button.textContent = 'Select the command and copy manually';
    }
  }));
  const tabs = [...document.querySelectorAll('[data-page-tab]')].filter(t => t.getClientRects().length),
    panes = [...document.querySelectorAll('[data-page-pane]')];

  function activate(name) {
    if (!tabs.some(t => t.dataset.pageTab === name)) name = tabs[0]?.dataset.pageTab;
    tabs.forEach((tab, i) => {
      const active = tab.dataset.pageTab === name;
      tab.id = 'server-tab-' + i;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      tab.classList.toggle('active', active);
    });
    panes.forEach((p, i) => {
      p.hidden = p.dataset.pagePane !== name;
      p.id = p.id || 'server-pane-' + i;
      p.setAttribute('role', 'tabpanel');
      const tab = tabs.find(t => t.dataset.pageTab === p.dataset.pagePane);
      if (tab) {
        p.setAttribute('aria-labelledby', tab.id);
        tab.setAttribute('aria-controls', p.id);
      }
    });
  }
  tabs.forEach((tab, i) => {
    tab.addEventListener('click', () => {
      location.hash = tab.dataset.pageTab;
      activate(tab.dataset.pageTab);
    });
    tab.addEventListener('keydown', e => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
        e.preventDefault();
        const j = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
        tabs[j].click();
        tabs[j].focus();
      }
    });
  });
  if (tabs.length) {
    activate(location.hash.slice(1));
    window.addEventListener('hashchange', () => activate(location.hash.slice(1)));
  }
  document.querySelectorAll('[data-page-refresh]').forEach(select => {
    select.value = preference.read('page-refresh', '0');
    let timer;

    function schedule() {
      clearInterval(timer);
      preference.write('page-refresh', select.value);
      if (Number(select.value)) timer = setInterval(() => {
        if (!document.hidden) location.reload();
      }, Number(select.value) * 1000);
    }
    select.addEventListener('change', schedule);
    schedule();
  });
  document.querySelectorAll('.side-nav a').forEach(a => {
    if (new URL(a.href).pathname === location.pathname) {
      a.classList.add('active');
      a.setAttribute('aria-current', 'page');
    }
  });
});

document.querySelectorAll('[data-topology-toggle]').forEach(button => {
  button.addEventListener('click', () => {
    button.closest('.pool-topology').querySelectorAll('details.topology-group').forEach(group => {
      group.open = button.dataset.topologyToggle === 'expand';
    });
  });
});
