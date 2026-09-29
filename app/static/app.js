window.NASitronChart = async function(canvas, url, options = {}) {
  if (!canvas) return;
  const label = canvas.getAttribute("aria-label") || canvas.closest(".panel")?.querySelector("h2")?.textContent.trim() || "Historical telemetry";
  canvas.setAttribute("role", "img");
  let points = [];
  let error = false;
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error("Metric request failed");
    const data = await response.json();
    points = (data.points || [])
      .filter(p => p.v !== null && Number.isFinite(Number(p.v)) && Number.isFinite(Date.parse(p.t)))
      .sort((a, b) => Date.parse(a.t) - Date.parse(b.t));
  } catch {
    error = true;
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  let selected = -1;
  let coordinates = [];
  canvas.tabIndex = 0;
  canvas.setAttribute("aria-label", label + ". " + (error ? "Unable to load telemetry." : points.length ? "Use left and right arrow keys to inspect samples." : "Waiting for historical samples."));
  const readout = document.createElement("div");
  readout.className = "chart-readout subtle";
  readout.setAttribute("aria-live", "polite");
  canvas.insertAdjacentElement("afterend", readout);

  function formatValue(v) {
    return options.bytes ? formatBytes(v) : Number(v).toFixed(options.decimals ?? 1) + (options.suffix || "");
  }

  function updateReadout() {
    const p = points[selected] || points[points.length - 1];
    readout.textContent = p ? `${selected < 0 ? "Latest · " : ""}${formatValue(p.v)} · ${new Date(p.t).toLocaleString()}` : "";
  }

  function formatBytes(v) {
    const units = ["B","KiB","MiB","GiB","TiB","PiB"];
    let n = Number(v), i = 0;
    while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + units[i];
  }

  function draw() {
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    const cssW = Math.max(1, rect.width);
    const cssH = Math.max(170, rect.height || 195);
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const styles = getComputedStyle(document.documentElement);
    const fg = styles.getPropertyValue("--muted").trim() || "#8e9bb0";
    const accent = styles.getPropertyValue("--accent").trim() || "#FC1859";
    const grid = styles.getPropertyValue("--border-soft").trim() || "#172131";
    ctx.font = "11px Inter, system-ui, sans-serif";

    if (!points.length) {
      ctx.fillStyle = fg;
      ctx.textAlign = "center";
      ctx.fillText(error ? "Unable to load telemetry." : "Waiting for historical samples…", cssW / 2, cssH / 2);
      ctx.textAlign = "left";
      return;
    }

    const values = points.map(p => Number(p.v));
    let min = Math.min(...values), max = Math.max(...values);
    if (options.minZero) min = Math.min(0, min);
    if (options.max100) max = Math.max(100, max);
    if (min === max) { min -= 1; max += 1; }

    const axisLabels = Array.from({length: 5}, (_, i) => formatValue(max - (max - min) * i / 4));
    const pad = {l: Math.min(cssW / 3, Math.max(...axisLabels.map(v => ctx.measureText(v).width)) + 12), r: 12, t: 12, b: 28};
    const iw = cssW - pad.l - pad.r;
    const ih = cssH - pad.t - pad.b;

    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 5]);
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + ih * i / 4;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(cssW - pad.r, y); ctx.stroke();
      ctx.fillStyle = fg;
      ctx.fillText(axisLabels[i], 2, y + 4);
    }
    ctx.setLineDash([]);

    const gradient = ctx.createLinearGradient(0, pad.t, 0, cssH - pad.b);
    gradient.addColorStop(0, "rgba(252,24,89,.18)");
    gradient.addColorStop(1, "rgba(252,24,89,0)");

    const startTime = Date.parse(points[0].t);
    const span = Date.parse(points[points.length - 1].t) - startTime;
    coordinates = points.map(p => {
      const x = pad.l + (span ? iw * (Date.parse(p.t) - startTime) / span : iw / 2);
      const y = pad.t + ih - ((Number(p.v) - min) / (max - min)) * ih;
      return {x, y};
    });
    ctx.beginPath();
    coordinates.forEach(({x, y}, i) => {
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.lineTo(coordinates[coordinates.length - 1].x, cssH - pad.b);
    ctx.lineTo(coordinates[0].x, cssH - pad.b);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    coordinates.forEach(({x, y}, i) => {
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = accent;
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.shadowBlur = 0;

    const marker = coordinates[selected] || coordinates[coordinates.length - 1];
    if (selected >= 0) {
      ctx.strokeStyle = fg;
      ctx.lineWidth = .7;
      ctx.setLineDash([3, 5]);
      ctx.beginPath(); ctx.moveTo(marker.x, pad.t); ctx.lineTo(marker.x, cssH - pad.b); ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.beginPath(); ctx.arc(marker.x, marker.y, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = accent; ctx.fill();

    const first = new Date(points[0].t);
    const last = new Date(points[points.length - 1].t);
    ctx.fillStyle = fg;
    const formatTime = d => d.toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"});
    ctx.fillText(formatTime(first), pad.l, cssH - 6);
    const end = formatTime(last);
    if (ctx.measureText(formatTime(first)).width + ctx.measureText(end).width + 10 < iw) {
      ctx.fillText(end, cssW - pad.r - ctx.measureText(end).width, cssH - 6);
    }
  }

  updateReadout();
  draw();
  canvas.addEventListener("pointermove", event => {
    if (!coordinates.length) return;
    const x = event.clientX - canvas.getBoundingClientRect().left;
    const nearest = coordinates.reduce((best, p, i) => Math.abs(p.x - x) < Math.abs(coordinates[best].x - x) ? i : best, 0);
    if (selected === nearest) return;
    selected = nearest;
    updateReadout(); draw();
  });
  canvas.addEventListener("pointerleave", () => { selected = -1; updateReadout(); draw(); });
  canvas.addEventListener("keydown", event => {
    if (!points.length || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = selected < 0 ? points.length - 1 : selected;
    selected = event.key === "Home" ? 0 : event.key === "End" ? points.length - 1 : Math.max(0, Math.min(points.length - 1, current + (event.key === "ArrowRight" ? 1 : -1)));
    updateReadout(); draw();
  });
  const observer = new ResizeObserver(draw);
  observer.observe(canvas);
};

window.toggleSidebar = function(force) {
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebar-backdrop");
  if (!sidebar || !backdrop) return;
  const wasOpen = sidebar.classList.contains("open");
  const open = typeof force === "boolean" ? force : !wasOpen;
  sidebar.classList.toggle("open", open);
  backdrop.classList.toggle("open", open);
  const toggle = document.getElementById("navigation-toggle");
  if (toggle) toggle.setAttribute("aria-expanded", String(open));
  if (window.matchMedia("(max-width: 900px)").matches) {
    sidebar.inert = !open;
    document.body.classList.toggle("navigation-open", open);
    if (open) sidebar.querySelector("a")?.focus();
    else if (wasOpen) toggle?.focus();
  }
};

document.addEventListener("keydown", event => {
  if (event.key === "Escape") window.toggleSidebar(false);
  const sidebar = document.getElementById("sidebar");
  if (event.key === "Tab" && sidebar?.classList.contains("open") && window.matchMedia("(max-width: 900px)").matches) {
    const links = [...sidebar.querySelectorAll("a, button")];
    const first = links[0], last = links[links.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});

document.addEventListener("DOMContentLoaded", () => {
  document.querySelector(".nav-item.active")?.setAttribute("aria-current", "page");
  const sidebar = document.getElementById("sidebar");
  const mobile = window.matchMedia("(max-width: 900px)");
  function syncNavigation() {
    if (sidebar) sidebar.inert = mobile.matches && !sidebar.classList.contains("open");
    if (!mobile.matches) {
      sidebar?.classList.remove("open");
      document.getElementById("sidebar-backdrop")?.classList.remove("open");
      document.getElementById("navigation-toggle")?.setAttribute("aria-expanded", "false");
      document.body.classList.remove("navigation-open");
    }
  }
  mobile.addEventListener("change", syncNavigation);
  syncNavigation();
  document.querySelectorAll(".scroll").forEach(pane => {
    pane.tabIndex = 0;
    pane.setAttribute("role", "region");
    pane.setAttribute("aria-label", (pane.querySelector("table th")?.textContent.trim() || "Data") + " table; scroll horizontally for more columns");
  });
});
