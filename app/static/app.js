window.NASitronChart = async function(canvas, url, options = {}) {
  const response = await fetch(url);
  if (!response.ok) return;
  const data = await response.json();
  const points = data.points || [];
  const ctx = canvas.getContext("2d");

  function draw() {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(300, rect.width) * dpr;
    canvas.height = Math.max(160, rect.height || 180) * dpr;
    ctx.scale(dpr, dpr);
    const w = canvas.width / dpr;
    const h = canvas.height / dpr;
    ctx.clearRect(0, 0, w, h);

    const styles = getComputedStyle(document.documentElement);
    const fg = styles.getPropertyValue("--muted").trim() || "#98a2b3";
    const line = styles.getPropertyValue("--accent").trim() || "#65b8ff";
    const grid = styles.getPropertyValue("--border").trim() || "#293243";
    ctx.font = "12px system-ui";
    ctx.fillStyle = fg;

    if (!points.length) {
      ctx.fillText("No historical samples yet", 16, 28);
      return;
    }

    const values = points.map(p => Number(p.v));
    let min = Math.min(...values);
    let max = Math.max(...values);
    if (options.minZero) min = Math.min(0, min);
    if (min === max) { min -= 1; max += 1; }
    const pad = {l: 54, r: 12, t: 12, b: 28};
    const iw = w - pad.l - pad.r;
    const ih = h - pad.t - pad.b;

    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + ih * i / 4;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
      const val = max - (max - min) * i / 4;
      const text = options.bytes ? formatBytes(val) : val.toFixed(options.decimals ?? 1) + (options.suffix || "");
      ctx.fillStyle = fg;
      ctx.fillText(text, 4, y + 4);
    }

    ctx.strokeStyle = line;
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((p, i) => {
      const x = pad.l + (points.length === 1 ? iw / 2 : iw * i / (points.length - 1));
      const y = pad.t + ih - ((Number(p.v) - min) / (max - min)) * ih;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();

    const first = new Date(points[0].t);
    const last = new Date(points[points.length - 1].t);
    ctx.fillStyle = fg;
    ctx.fillText(first.toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"}), pad.l, h - 7);
    const endText = last.toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"});
    ctx.fillText(endText, w - pad.r - ctx.measureText(endText).width, h - 7);
  }

  function formatBytes(v) {
    const units = ["B","KiB","MiB","GiB","TiB","PiB"];
    let n = Number(v), i = 0;
    while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + units[i];
  }

  draw();
  new ResizeObserver(draw).observe(canvas);
};

window.toggleTheme = function() {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("nasitron-theme", next);
};
(function() {
  const saved = localStorage.getItem("nasitron-theme");
  if (saved) document.documentElement.dataset.theme = saved;
})();
