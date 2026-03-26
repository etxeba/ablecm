# Bluetooth Cadence Monitor

Real-time cycling cadence display that connects to any BLE CSC (Cycling Speed and Cadence) sensor and shows RPM in a web browser with a 30-minute rolling chart.

Two implementations are provided: a **Raspberry Pi** version and a self-contained **ESP32-C3** version.

## Web UI

Both versions serve the same dark-themed web interface:

- Large real-time RPM readout
- Connection status indicator
- 30-minute rolling time-series chart with auto-scaling Y axis
- Auto-reconnecting WebSocket for live updates

## Raspberry Pi Version

Python server using `bleak` for BLE and `aiohttp` for HTTP/WebSocket.

### Requirements

- Raspberry Pi (or any Linux box) with Bluetooth LE
- Python 3.10+

### Setup

```sh
python -m venv venv
source venv/bin/activate
pip install bleak aiohttp
```

### Usage

```sh
python cadence_server.py
```

Open `http://<pi-ip>:8999` in a browser. The BLE connection starts automatically when a viewer connects and disconnects after 30 minutes of inactivity.

### Features

- Smart BLE lifecycle: only connects while a browser is viewing
- Automatic Bluetooth adapter reset after repeated connection failures
- Lock file prevents duplicate instances

## ESP32-C3 Version

Self-contained firmware for the ESP32-C3 Supermini. No Pi or external server needed -- the ESP32 runs a WiFi access point and serves the web UI directly.

### Requirements

- ESP32-C3 Supermini (or any ESP32-C3 board)
- [PlatformIO](https://platformio.org/)

### Build and Flash

```sh
cd esp32
pio run -t upload
```

### Usage

1. Power on the ESP32
2. Connect to the **Cadence** WiFi network (open, no password)
3. Open **http://192.168.4.1** in a browser

The device immediately begins scanning for a BLE cadence sensor and streams data to any connected browser via WebSocket.

### Arduino IDE Alternative

If you prefer Arduino IDE over PlatformIO:

1. Install the ESP32 board package (Espressif Arduino core) via Board Manager
2. Select board **ESP32C3 Dev Module**, enable **USB CDC On Boot**
3. Install libraries via Library Manager: **NimBLE-Arduino**, **ESPAsyncWebServer**, **AsyncTCP**
4. Open `esp32/src/main.cpp` as a sketch with `index_html.h` in the same folder

## BLE Protocol

Both versions use the standard Bluetooth CSC profile:

- **Service:** `0x1816` (Cycling Speed and Cadence)
- **Characteristic:** `0x2A5B` (CSC Measurement)
- Sensor discovery: CSC service UUID advertisement, with fallback to name matching ("cadence")
- Cadence is computed from cumulative crank revolutions and crank event time (1/1024s resolution)

## Compatibility

Tested with Wahoo Cadence sensors. Should work with any BLE sensor that advertises the standard CSC service.
