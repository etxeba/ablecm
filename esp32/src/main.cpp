/*
 * ESP32-C3 Cadence Monitor
 *
 * Self-contained BLE cadence sensor display: connects to any CSC-compliant
 * sensor over BLE, runs a WiFi AP, and serves a real-time web viewer.
 * Connect to the "Cadence" WiFi network and open http://192.168.4.1
 */

#include <Arduino.h>
#include <WiFi.h>
#include <NimBLEDevice.h>
#include <ESPAsyncWebServer.h>
#include "index_html.h"

// --- WiFi AP settings ---
static const char *AP_SSID = "Cadence";
static const char *AP_PASS = "";  // open network for easy phone access

// --- BLE CSC UUIDs ---
static const NimBLEUUID CSC_SERVICE_UUID("1816");
static const NimBLEUUID CSC_MEASUREMENT_UUID("2A5B");

// --- State ---
static float cadence_rpm = 0.0f;
static bool ble_connected = false;
static bool ble_scanning = false;

static uint16_t prev_crank_revs = 0;
static uint16_t prev_crank_time = 0;
static bool has_prev = false;

static unsigned long last_nonzero_ms = 0;
static const unsigned long IDLE_DISCONNECT_MS = 30UL * 60 * 1000;  // 30 min

static NimBLEClient *pClient = nullptr;
static NimBLEAdvertisedDevice *targetDevice = nullptr;
static bool doConnect = false;

// --- Web server ---
static AsyncWebServer server(80);
static AsyncWebSocket ws("/ws");

// Forward declarations
void startScan();

// --- CSC parsing ---

static void computeRpm(uint16_t crankRevs, uint16_t crankTime) {
    if (!has_prev) {
        prev_crank_revs = crankRevs;
        prev_crank_time = crankTime;
        has_prev = true;
        return;
    }

    uint16_t deltaRevs = crankRevs - prev_crank_revs;  // wraps naturally
    uint16_t deltaTime = crankTime - prev_crank_time;

    prev_crank_revs = crankRevs;
    prev_crank_time = crankTime;

    if (deltaRevs == 0 || deltaTime == 0) {
        cadence_rpm = 0.0f;
        return;
    }

    cadence_rpm = (float)deltaRevs * 1024.0f * 60.0f / (float)deltaTime;
    if (cadence_rpm > 0) {
        last_nonzero_ms = millis();
    }
}

// --- BLE notification callback ---

static void onCSCNotify(NimBLERemoteCharacteristic *pChar,
                        uint8_t *pData, size_t length, bool isNotify) {
    if (length < 1) return;
    uint8_t flags = pData[0];
    size_t offset = 1;

    if (flags & 0x01) {
        offset += 6;  // skip wheel revolution data
    }

    if ((flags & 0x02) && (offset + 4 <= length)) {
        uint16_t crankRevs = pData[offset] | (pData[offset + 1] << 8);
        uint16_t crankTime = pData[offset + 2] | (pData[offset + 3] << 8);
        computeRpm(crankRevs, crankTime);
        Serial.printf("Cadence: %.1f RPM\n", cadence_rpm);
    }
}

// --- BLE scan callback ---

class ScanCallbacks : public NimBLEScanCallbacks {
    void onResult(const NimBLEAdvertisedDevice *advertisedDevice) override {
        // Check for CSC service UUID
        if (advertisedDevice->isAdvertisingService(CSC_SERVICE_UUID)) {
            Serial.printf("Found CSC sensor: %s\n", advertisedDevice->getName().c_str());
            // Stop scanning - we found our device
            NimBLEDevice::getScan()->stop();
            targetDevice = new NimBLEAdvertisedDevice(*advertisedDevice);
            doConnect = true;
            ble_scanning = false;
            return;
        }

        // Fallback: check name
        std::string name = advertisedDevice->getName();
        if (name.length() > 0) {
            std::string lower = name;
            for (auto &c : lower) c = tolower(c);
            if (lower.find("cadence") != std::string::npos) {
                Serial.printf("Found sensor by name: %s\n", name.c_str());
                NimBLEDevice::getScan()->stop();
                targetDevice = new NimBLEAdvertisedDevice(*advertisedDevice);
                doConnect = true;
                ble_scanning = false;
            }
        }
    }

    void onScanEnd(const NimBLEScanResults &results, int reason) override {
        ble_scanning = false;
        if (!doConnect) {
            Serial.println("Scan complete, no CSC sensor found");
        }
    }
};

static ScanCallbacks scanCallbacks;

// --- BLE client callbacks ---

class ClientCallbacks : public NimBLEClientCallbacks {
    void onDisconnect(NimBLEClient *client, int reason) override {
        Serial.printf("Sensor disconnected (reason %d)\n", reason);
        ble_connected = false;
        cadence_rpm = 0.0f;
        has_prev = false;
    }
};

static ClientCallbacks clientCallbacks;

// --- BLE connect logic ---

static bool connectToSensor() {
    if (pClient == nullptr) {
        pClient = NimBLEDevice::createClient();
        pClient->setClientCallbacks(&clientCallbacks);
        pClient->setConnectionParams(12, 12, 0, 400);
        pClient->setConnectTimeout(15);
    }

    Serial.printf("Connecting to %s...\n", targetDevice->getAddress().toString().c_str());

    if (!pClient->connect(targetDevice)) {
        Serial.println("Connection failed");
        return false;
    }

    Serial.println("Connected, discovering services...");

    NimBLERemoteService *pService = pClient->getService(CSC_SERVICE_UUID);
    if (pService == nullptr) {
        Serial.println("CSC service not found on device");
        pClient->disconnect();
        return false;
    }

    NimBLERemoteCharacteristic *pChar = pService->getCharacteristic(CSC_MEASUREMENT_UUID);
    if (pChar == nullptr) {
        Serial.println("CSC measurement characteristic not found");
        pClient->disconnect();
        return false;
    }

    if (!pChar->subscribe(true, onCSCNotify)) {
        Serial.println("Failed to subscribe to notifications");
        pClient->disconnect();
        return false;
    }

    Serial.println("Subscribed to CSC notifications");
    ble_connected = true;
    has_prev = false;
    cadence_rpm = 0.0f;
    last_nonzero_ms = millis();
    return true;
}

// --- BLE scan start ---

void startScan() {
    if (ble_scanning) return;
    ble_scanning = true;
    doConnect = false;

    if (targetDevice) {
        delete targetDevice;
        targetDevice = nullptr;
    }

    NimBLEScan *pScan = NimBLEDevice::getScan();
    pScan->setScanCallbacks(&scanCallbacks);
    pScan->setActiveScan(true);
    pScan->setInterval(100);
    pScan->setWindow(99);
    pScan->start(15, false);  // 15 second scan
    Serial.println("BLE scan started");
}

// --- WebSocket events ---

static void onWsEvent(AsyncWebSocket *server, AsyncWebSocketClient *client,
                      AwsEventType type, void *arg, uint8_t *data, size_t len) {
    if (type == WS_EVT_CONNECT) {
        Serial.printf("WS client #%u connected\n", client->id());
    } else if (type == WS_EVT_DISCONNECT) {
        Serial.printf("WS client #%u disconnected\n", client->id());
    }
}

// --- Broadcast cadence to all WS clients ---

static unsigned long lastBroadcast = 0;

static void broadcastCadence() {
    if (millis() - lastBroadcast < 1000) return;
    lastBroadcast = millis();

    if (ws.count() == 0) return;

    char buf[64];
    snprintf(buf, sizeof(buf), "{\"rpm\":%.1f,\"connected\":%s}",
             cadence_rpm, ble_connected ? "true" : "false");
    ws.textAll(buf);
}

// --- Arduino setup ---

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println("\n=== Cadence Monitor (ESP32-C3) ===");

    // Start WiFi AP
    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID, AP_PASS);
    delay(100);
    Serial.printf("WiFi AP '%s' started, IP: %s\n",
                  AP_SSID, WiFi.softAPIP().toString().c_str());

    // Init BLE
    NimBLEDevice::init("CadenceMonitor");
    NimBLEDevice::setMTU(64);
    NimBLEDevice::setPower(3);  // +3 dBm

    // Web server routes
    server.on("/", HTTP_GET, [](AsyncWebServerRequest *request) {
        request->send(200, "text/html", INDEX_HTML);
    });

    ws.onEvent(onWsEvent);
    server.addHandler(&ws);

    server.begin();
    Serial.println("Web server started on port 80");

    // Start first scan
    startScan();
}

// --- Arduino loop ---

void loop() {
    // Handle pending BLE connection
    if (doConnect && targetDevice) {
        doConnect = false;
        if (!connectToSensor()) {
            Serial.println("Will retry scan in 5s...");
            delay(5000);
            startScan();
        }
    }

    // If disconnected and not scanning, restart scan
    if (!ble_connected && !ble_scanning && !doConnect) {
        delay(3000);
        startScan();
    }

    // Idle timeout: disconnect if no cadence for 30 min
    if (ble_connected && last_nonzero_ms > 0) {
        if (millis() - last_nonzero_ms >= IDLE_DISCONNECT_MS) {
            Serial.println("No cadence for 30m, disconnecting");
            if (pClient && pClient->isConnected()) {
                pClient->disconnect();
            }
            ble_connected = false;
            cadence_rpm = 0.0f;
        }
    }

    // Broadcast to WebSocket clients
    broadcastCadence();

    // Clean up disconnected WS clients
    ws.cleanupClients();

    delay(10);
}
