#!/usr/bin/env python3
"""
Bluetooth LE Cadence Sensor -> HTTP/WebSocket Real-time Display

Connects to a Wahoo Cadence sensor (or any BLE CSC-compliant sensor)
and serves a web page that displays the current cadence in RPM.

BLE connection is only active while at least one browser client is viewing
the page. Disconnects after a timeout when no viewers remain.
"""

import asyncio
import json
import socket
import struct
import logging
import subprocess
import sys
import os
import fcntl
from pathlib import Path
from bleak import BleakScanner, BleakClient
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Bluetooth CSC Service UUIDs
CSC_SERVICE_UUID = "00001816-0000-1000-8000-00805f9b34fb"
CSC_MEASUREMENT_UUID = "00002a5b-0000-1000-8000-00805f9b34fb"

# How long to keep the BLE connection after the last non-zero cadence reading
IDLE_DISCONNECT_SECONDS = 30 * 60  # 30 minutes

# Shared state
cadence_rpm = 0.0
ble_connected = False
prev_crank_revs = None
prev_crank_time = None
last_nonzero_time: float = 0.0  # monotonic timestamp of last RPM > 0
ws_clients: set[web.WebSocketResponse] = set()

# BLE lifecycle control
ble_task: asyncio.Task | None = None
ble_stop_event = asyncio.Event()


def parse_csc_measurement(data: bytearray) -> tuple[int, int] | None:
    """Parse CSC Measurement notification and return (crank_revs, crank_time) or None."""
    flags = data[0]
    offset = 1

    if flags & 0x01:
        offset += 6  # skip wheel revolution data

    if flags & 0x02:
        crank_revs, crank_time = struct.unpack_from("<HH", data, offset)
        return crank_revs, crank_time

    return None


def compute_rpm(crank_revs: int, crank_time: int) -> float:
    """Compute RPM from current and previous crank data, handling uint16 rollover."""
    global prev_crank_revs, prev_crank_time

    if prev_crank_revs is None:
        prev_crank_revs = crank_revs
        prev_crank_time = crank_time
        return 0.0

    delta_revs = (crank_revs - prev_crank_revs) & 0xFFFF
    delta_time = (crank_time - prev_crank_time) & 0xFFFF

    prev_crank_revs = crank_revs
    prev_crank_time = crank_time

    if delta_revs == 0 or delta_time == 0:
        return 0.0

    return (delta_revs * 1024 * 60) / delta_time


def on_csc_notification(_, data: bytearray):
    """Handle BLE CSC measurement notification."""
    global cadence_rpm, last_nonzero_time
    result = parse_csc_measurement(data)
    if result is None:
        return
    crank_revs, crank_time = result
    cadence_rpm = compute_rpm(crank_revs, crank_time)
    if cadence_rpm > 0:
        last_nonzero_time = asyncio.get_event_loop().time()
    log.info("Cadence: %.1f RPM", cadence_rpm)


async def ble_session():
    """Run a single BLE session: scan, connect, stream until stopped or disconnected."""
    global ble_connected, prev_crank_revs, prev_crank_time, cadence_rpm

    ble_stop_event.clear()

    consecutive_failures = 0
    MAX_FAILURES_BEFORE_RESET = 3

    while not ble_stop_event.is_set():
        ble_connected = False
        prev_crank_revs = None
        prev_crank_time = None
        cadence_rpm = 0.0

        # Reset the BT adapter if we've failed too many times in a row
        if consecutive_failures >= MAX_FAILURES_BEFORE_RESET:
            log.warning("Too many connection failures (%d), resetting BT adapter", consecutive_failures)
            # Remove all cached cadence devices so we get a fresh discovery
            result = subprocess.run(
                ["bluetoothctl", "devices"], capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3 and "cadence" in line.lower():
                    subprocess.run(
                        ["bluetoothctl", "remove", parts[1]],
                        capture_output=True, timeout=5,
                    )
            subprocess.run(["sudo", "hciconfig", "hci0", "down"], capture_output=True, timeout=5)
            await asyncio.sleep(2)
            subprocess.run(["sudo", "hciconfig", "hci0", "up"], capture_output=True, timeout=5)
            await asyncio.sleep(3)
            consecutive_failures = 0

        log.info("Scanning for BLE cadence sensor...")
        device = None
        while device is None and not ble_stop_event.is_set():
            device = await BleakScanner.find_device_by_filter(
                lambda d, adv: (
                    CSC_SERVICE_UUID in [str(u).lower() for u in (adv.service_uuids or [])]
                    or (d.name and "cadence" in d.name.lower())
                ),
                timeout=15.0,
            )
            if device is None:
                log.info("No CSC sensor found, retrying...")

        if ble_stop_event.is_set():
            break

        log.info("Found sensor: %s (%s)", device.name, device.address)

        try:
            # Disconnect at BlueZ level first to avoid stale connection state
            subprocess.run(
                ["bluetoothctl", "disconnect", device.address],
                capture_output=True, timeout=5,
            )
            await asyncio.sleep(1)

            client = BleakClient(device, timeout=30.0)
            await client.connect()
            log.info("Connected to %s", device.name)

            services = client.services
            if not services.get_service(CSC_SERVICE_UUID):
                log.warning("Device %s does not have CSC service, skipping", device.name)
                await client.disconnect()
                await asyncio.sleep(2)
                continue

            ble_connected = True
            consecutive_failures = 0  # connected successfully
            try:
                await client.start_notify(CSC_MEASUREMENT_UUID, on_csc_notification)
                while client.is_connected and not ble_stop_event.is_set():
                    await broadcast_cadence()
                    # Check idle timeout: disconnect if no non-zero reading for 30 min
                    if last_nonzero_time > 0:
                        idle = asyncio.get_event_loop().time() - last_nonzero_time
                        if idle >= IDLE_DISCONNECT_SECONDS:
                            log.info("No cadence for %dm, disconnecting", IDLE_DISCONNECT_SECONDS // 60)
                            break
                    await asyncio.sleep(1)
                if ble_stop_event.is_set():
                    log.info("Stopping BLE session (shutdown)")
                elif not client.is_connected:
                    log.warning("Sensor disconnected")
            finally:
                ble_connected = False
                cadence_rpm = 0.0
                try:
                    await client.disconnect()
                except Exception:
                    pass
        except Exception as e:
            consecutive_failures += 1
            log.error("BLE error with %s (%s) [attempt %d]: %s",
                      device.name, device.address, consecutive_failures, e)
            subprocess.run(
                ["bluetoothctl", "disconnect", device.address],
                capture_output=True, timeout=5,
            )
            await asyncio.sleep(5)

    ble_connected = False
    cadence_rpm = 0.0
    log.info("BLE session ended")


def ensure_ble_running():
    """Start the BLE session if not already running."""
    global ble_task, last_nonzero_time
    if ble_task is None or ble_task.done():
        log.info("Starting BLE session (viewer connected)")
        # Reset so the idle timer doesn't fire immediately
        last_nonzero_time = asyncio.get_event_loop().time()
        ble_stop_event.clear()
        ble_task = asyncio.create_task(ble_session())


async def broadcast_cadence():
    """Send current cadence to all WebSocket clients."""
    global ws_clients
    msg = json.dumps({"rpm": round(cadence_rpm, 1), "connected": ble_connected})
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_str(msg)
        except Exception:
            dead.add(ws)
    ws_clients -= dead


# --- HTTP handlers ---

async def handle_index(_request):
    return web.Response(text=HTML_PAGE, content_type="text/html")


async def handle_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.add(ws)
    log.info("WebSocket client connected (%d total)", len(ws_clients))

    # Start BLE if needed
    ensure_ble_running()

    try:
        async for _ in ws:
            pass
    finally:
        ws_clients.discard(ws)
        log.info("WebSocket client disconnected (%d total)", len(ws_clients))

    return ws


async def on_cleanup(app):
    ble_stop_event.set()
    if ble_task and not ble_task.done():
        try:
            await asyncio.wait_for(ble_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


HTML_PAGE = """\
<!DOCTYPE html>
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
  const history = [];  // [{t: timestamp_ms, rpm: number}]

  function addPoint(rpm) {
    const now = Date.now();
    history.push({t: now, rpm: rpm});
    // prune older than 30 min
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

    // Y axis: auto-scale with minimum of 120
    let maxRpm = 120;
    for (const p of history) { if (p.rpm > maxRpm) maxRpm = p.rpm; }
    maxRpm = Math.ceil(maxRpm / 20) * 20;

    // Grid lines and labels
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

    // Time labels along X axis
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

    // Plot area border
    ctx.strokeStyle = "rgba(255,255,255,0.15)";
    ctx.strokeRect(pad.left, pad.top, plotW, plotH);

    if (history.length < 2) return;

    // Draw filled area + line
    function toX(t) { return pad.left + ((t - tMin) / HISTORY_MS) * plotW; }
    function toY(rpm) { return pad.top + plotH - (rpm / maxRpm) * plotH; }

    // Filled area
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

    // Line
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

  // Redraw on resize
  window.addEventListener("resize", drawChart);

  // Redraw periodically to scroll the time axis even without new data
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
</html>
"""


LOCK_FILE = Path(__file__).with_suffix(".lock")


def acquire_lock():
    """Acquire an exclusive lock to prevent duplicate instances."""
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Another instance is already running (lock file: %s)", LOCK_FILE)
        sys.exit(1)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd  # must keep reference so lock isn't released by GC


def main():
    lock = acquire_lock()  # noqa: F841 — held for process lifetime

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.on_cleanup.append(on_cleanup)

    log.info("Starting server on http://0.0.0.0:8999")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 8999))
    web.run_app(app, sock=sock)


if __name__ == "__main__":
    main()
