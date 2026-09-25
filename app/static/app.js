window.NASitronChart = async function(canvas, url, options = {}) {
  if (!canvas) return;
  const response = await fetch(url);
  if (!response.ok) return;
  const data = await response.json();
  const points = data.points || [];
  const ctx = canvas.getContext("2d");

  function formatBytes(v) {
    const units = ["B","KiB","MiB","GiB","TiB","PiB"];
    let n = Number(v), i = 0;
    while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + units[i];
  }

  function draw() {
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    const cssW = Math.max(300, rect.width);
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
      ctx.fillText("Waiting for historical samples…", 14, 24);
      return;
    }

    const values = points.map(p => Number(p.v));
    let min = Math.min(...values), max = Math.max(...values);
    if (options.minZero) min = Math.min(0, min);
    if (options.max100) max = Math.max(100, max);
    if (min === max) { min -= 1; max += 1; }

    const pad = {l: 52, r: 12, t: 12, b: 26};
    const iw = cssW - pad.l - pad.r;
    const ih = cssH - pad.t - pad.b;

    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + ih * i / 4;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(cssW - pad.r, y); ctx.stroke();
      const val = max - (max - min) * i / 4;
      const label = options.bytes ? formatBytes(val) : val.toFixed(options.decimals ?? 1) + (options.suffix || "");
      ctx.fillStyle = fg;
      ctx.fillText(label, 2, y + 4);
    }

    const gradient = ctx.createLinearGradient(0, pad.t, 0, cssH - pad.b);
    gradient.addColorStop(0, "rgba(252,24,89,.28)");
    gradient.addColorStop(1, "rgba(252,24,89,0)");

    ctx.beginPath();
    points.forEach((p, i) => {
      const x = pad.l + (points.length === 1 ? iw / 2 : iw * i / (points.length - 1));
      const y = pad.t + ih - ((Number(p.v) - min) / (max - min)) * ih;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.lineTo(cssW - pad.r, cssH - pad.b);
    ctx.lineTo(pad.l, cssH - pad.b);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    points.forEach((p, i) => {
      const x = pad.l + (points.length === 1 ? iw / 2 : iw * i / (points.length - 1));
      const y = pad.t + ih - ((Number(p.v) - min) / (max - min)) * ih;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = accent;
    ctx.lineWidth = 2.3;
    ctx.shadowColor = "rgba(252,24,89,.35)";
    ctx.shadowBlur = 7;
    ctx.stroke();
    ctx.shadowBlur = 0;

    const first = new Date(points[0].t);
    const last = new Date(points[points.length - 1].t);
    ctx.fillStyle = fg;
    const formatTime = d => d.toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"});
    ctx.fillText(formatTime(first), pad.l, cssH - 6);
    const end = formatTime(last);
    ctx.fillText(end, cssW - pad.r - ctx.measureText(end).width, cssH - 6);
  }

  draw();
  const observer = new ResizeObserver(draw);
  observer.observe(canvas);
};

window.toggleSidebar = function(force) {
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebar-backdrop");
  if (!sidebar || !backdrop) return;
  const open = typeof force === "boolean" ? force : !sidebar.classList.contains("open");
  sidebar.classList.toggle("open", open);
  backdrop.classList.toggle("open", open);
};

document.addEventListener("keydown", event => {
  if (event.key === "Escape") window.toggleSidebar(false);
});
