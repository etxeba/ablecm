#pragma once

const char INDEX_HTML[] PROGMEM = R"rawliteral(<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cadence Monitor</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #1a1a2e;
    color: #eee;
    display: flex;
    flex-direction: column;
    align-items: center;
    min-height: 100vh;
    padding: 2rem 1rem;
  }
  .header { text-align: center; margin-bottom: 2rem; }
  .rpm-value {
    font-size: 8rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    line-height: 1;
    transition: color 0.3s;
  }
  .rpm-label { font-size: 1.5rem; opacity: 0.6; margin-top: 0.3rem; }
  .status {
    margin-top: 1rem;
    font-size: 1rem;
    opacity: 0.5;
  }
  .status .dot {
    display: inline-block;
    width: 10px; height: 10px;
    border-radius: 50%;
    margin-right: 6px;
    vertical-align: middle;
  }
  .dot.on { background: #4ade80; }
  .dot.off { background: #f87171; }
  .chart-container {
    width: 100%;
    max-width: 900px;
    flex: 1;
    min-height: 300px;
    position: relative;
  }
  canvas { width: 100% !important; height: 100% !important; }
</style>
</head>
<body>
<div class="header">
  <div class="rpm-value" id="rpm">--</div>
  <div class="rpm-label">RPM</div>
  <div class="status">
    <span class="dot off" id="dot"></span>
    <span id="status-text">Connecting...</span>
  </div>
</div>
<div class="chart-container">
  <canvas id="chart"></canvas>
</div>
<script>
  const rpmEl = document.getElementById("rpm");
  const dotEl = document.getElementById("dot");
  const statusEl = document.getElementById("status-text");
  const canvas = document.getElementById("chart");
  const ctx = canvas.getContext("2d");

  const HISTORY_MS = 30 * 60 * 1000;
  const history = [];

  function addPoint(rpm) {
    const now = Date.now();
    history.push({t: now, rpm: rpm});
    const cutoff = now - HISTORY_MS;
    while (history.length > 0 && history[0].t < cutoff) history.shift();
  }

  function drawChart() {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const W = rect.width;
    const H = rect.height;
    const pad = {top: 20, right: 20, bottom: 40, left: 50};
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;

    ctx.clearRect(0, 0, W, H);

    const now = Date.now();
    const tMin = now - HISTORY_MS;
    const tMax = now;

    let maxRpm = 120;
    for (const p of history) { if (p.rpm > maxRpm) maxRpm = p.rpm; }
    maxRpm = Math.ceil(maxRpm / 20) * 20;

    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.fillStyle = "rgba(255,255,255,0.4)";
    ctx.font = "12px -apple-system, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";

    const ySteps = 5;
    for (let i = 0; i <= ySteps; i++) {
      const val = (maxRpm / ySteps) * i;
      const y = pad.top + plotH - (i / ySteps) * plotH;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + plotW, y);
      ctx.stroke();
      ctx.fillText(Math.round(val), pad.left - 8, y);
    }

    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const tSteps = 6;
    for (let i = 0; i <= tSteps; i++) {
      const t = tMin + (i / tSteps) * HISTORY_MS;
      const x = pad.left + (i / tSteps) * plotW;
      const d = new Date(t);
      const label = d.getHours().toString().padStart(2, "0") + ":" +
                    d.getMinutes().toString().padStart(2, "0");
      ctx.beginPath();
      ctx.moveTo(x, pad.top);
      ctx.lineTo(x, pad.top + plotH);
      ctx.stroke();
      ctx.fillText(label, x, pad.top + plotH + 8);
    }

    ctx.strokeStyle = "rgba(255,255,255,0.15)";
    ctx.strokeRect(pad.left, pad.top, plotW, plotH);

    if (history.length < 2) return;

    function toX(t) { return pad.left + ((t - tMin) / HISTORY_MS) * plotW; }
    function toY(rpm) { return pad.top + plotH - (rpm / maxRpm) * plotH; }

    ctx.beginPath();
    ctx.moveTo(toX(history[0].t), pad.top + plotH);
    for (const p of history) ctx.lineTo(toX(p.t), toY(p.rpm));
    ctx.lineTo(toX(history[history.length - 1].t), pad.top + plotH);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
    grad.addColorStop(0, "rgba(99, 179, 237, 0.3)");
    grad.addColorStop(1, "rgba(99, 179, 237, 0.02)");
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    for (let i = 0; i < history.length; i++) {
      const x = toX(history[i].t);
      const y = toY(history[i].rpm);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = "#63b3ed";
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();
  }

  window.addEventListener("resize", drawChart);
  setInterval(drawChart, 5000);

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + location.host + "/ws");

    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      rpmEl.textContent = data.rpm > 0 ? Math.round(data.rpm) : "--";
      if (data.connected) {
        dotEl.className = "dot on";
        statusEl.textContent = "Sensor connected";
      } else {
        dotEl.className = "dot off";
        statusEl.textContent = "Scanning for sensor...";
      }
      addPoint(data.rpm);
      drawChart();
    };

    ws.onclose = () => {
      dotEl.className = "dot off";
      statusEl.textContent = "Server disconnected, reconnecting...";
      setTimeout(connect, 2000);
    };

    ws.onerror = () => ws.close();
  }

  connect();
  drawChart();
</script>
</body>
</html>)rawliteral";
