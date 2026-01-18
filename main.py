#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <SPIFFS.h>
#include <WebServer.h>
#include "RTClib.h"

// ========== SPIFFS CONFIG ==========
#define CONFIG_FILE "/wifi_config.json"

// ========== WIFI CONFIG ==========
String wifiSSID = "";
String wifiPassword = "";
String apiBaseUrl = "https://database-smart-irrigation-production.up.railway.app";

// ========== GLOBAL ==========
WiFiClientSecure client;
RTC_DS3231 rtc;
WebServer configServer(80);

// ========== PIN CONFIGURATION ==========
const int PIN_RELAY = 16;
const int PIN_SOIL = 14;
const int PIN_WATER = 13;
const int PIN_LED = 2;

#define POMPA_ON HIGH
#define POMPA_OFF LOW

// ========== TIMING ==========
unsigned long lastSensorUpdate = 0;
unsigned long lastStatusCheck = 0;
unsigned long lastBlink = 0;
const unsigned long SENSOR_INTERVAL = 5000;
const unsigned long STATUS_INTERVAL = 3000;
const unsigned long BLINK_INTERVAL = 500;

// ========== STATE ==========
String pumpStatus = "OFF";
bool isPaused = false;
bool isConfigMode = false;
bool isWiFiConnected = false;
bool ledState = false;

// ========== FORWARD DECLARATIONS ==========
void handleRoot();
void handleSaveWiFi();
void handleGetStatus();
void handleResetWiFi();

void initSPIFFS() {
  if (!SPIFFS.begin(true)) {
    Serial.println("❌ SPIFFS failed");
    return;
  }
  Serial.println("✅ SPIFFS OK");
}

void loadWiFiConfig() {
  if (!SPIFFS.exists(CONFIG_FILE)) {
    Serial.println("⚠️ Config file not found");
    return;
  }

  File file = SPIFFS.open(CONFIG_FILE, "r");
  StaticJsonDocument<300> doc;
  
  if (deserializeJson(doc, file) == DeserializationError::Ok) {
    wifiSSID = doc["ssid"].as<String>();
    wifiPassword = doc["password"].as<String>();
    apiBaseUrl = doc["api_url"].as<String>();
    
    Serial.println("✅ Config loaded");
    Serial.println("📡 SSID: " + wifiSSID);
  }
  file.close();
}

void saveWiFiConfig(String ssid, String password) {
  StaticJsonDocument<300> doc;
  doc["ssid"] = ssid;
  doc["password"] = password;
  doc["api_url"] = apiBaseUrl;

  File file = SPIFFS.open(CONFIG_FILE, "w");
  serializeJson(doc, file);
  file.close();
  
  Serial.println("✅ WiFi config saved");
}

void startConfigMode() {
  isConfigMode = true;
  Serial.println("\n⚙️ CONFIG MODE");
  
  // AP Mode
  WiFi.mode(WIFI_AP);
  WiFi.softAP("Smart-Irrigation-Setup", "password123");
  
  Serial.println("📱 AP: Smart-Irrigation-Setup");
  Serial.println("🔐 Password: password123");
  Serial.println("🌐 IP: 192.168.4.1");

  configServer.on("/", HTTP_GET, handleRoot);
  configServer.on("/save-wifi", HTTP_POST, handleSaveWiFi);
  configServer.on("/status", HTTP_GET, handleGetStatus);
  configServer.on("/reset", HTTP_POST, handleResetWiFi);
  
  configServer.begin();
  Serial.println("✅ Web server started");
}

void handleRoot() {
  String html = R"(
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Smart Irrigation WiFi Setup</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            padding: 40px;
            max-width: 450px;
            width: 100%;
        }
        h1 {
            color: #333;
            margin-bottom: 8px;
            font-size: 28px;
        }
        .subtitle {
            color: #666;
            font-size: 14px;
            margin-bottom: 30px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            color: #333;
            font-weight: 600;
            margin-bottom: 8px;
            font-size: 14px;
        }
        input {
            width: 100%;
            padding: 12px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            font-size: 14px;
            transition: border-color 0.3s;
        }
        input:focus {
            outline: none;
            border-color: #667eea;
        }
        button {
            width: 100%;
            padding: 12px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            margin-top: 20px;
            transition: transform 0.2s;
        }
        button:hover { transform: translateY(-2px); }
        .message {
            padding: 12px;
            border-radius: 8px;
            margin-top: 15px;
            text-align: center;
            font-size: 14px;
            display: none;
        }
        .message.show { display: block; }
        .success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>💧 Smart Irrigation</h1>
        <p class="subtitle">Konfigurasi Koneksi WiFi</p>
        
        <form id="wifiForm">
            <div class="form-group">
                <label for="ssid">📡 Nama WiFi (SSID)</label>
                <input type="text" id="ssid" placeholder="Masukkan nama WiFi Anda" required>
            </div>
            
            <div class="form-group">
                <label for="password">🔑 Password WiFi</label>
                <input type="password" id="password" placeholder="Masukkan password WiFi" required>
            </div>
            
            <button type="submit">✅ Simpan & Koneksi</button>
        </form>
        
        <div id="message" class="message"></div>
    </div>

    <script>
        document.getElementById('wifiForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const ssid = document.getElementById('ssid').value;
            const password = document.getElementById('password').value;
            const msg = document.getElementById('message');
            
            try {
                const res = await fetch('/save-wifi', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ssid, password })
                });
                
                const data = await res.json();
                msg.classList.add('show');
                
                if (data.status === 'success') {
                    msg.className = 'message show success';
                    msg.textContent = '✅ WiFi tersimpan! ESP32 restart...';
                    document.getElementById('wifiForm').disabled = true;
                } else {
                    msg.className = 'message show error';
                    msg.textContent = '❌ ' + (data.message || 'Gagal menyimpan');
                }
            } catch (error) {
                msg.className = 'message show error';
                msg.textContent = '❌ Error: ' + error.message;
            }
        });
    </script>
</body>
</html>
  )";
  
  configServer.send(200, "text/html", html);
}

void handleSaveWiFi() {
  if (!configServer.hasArg("plain")) {
    configServer.send(400, "application/json", "{\"status\":\"error\",\"message\":\"No data\"}");
    return;
  }

  StaticJsonDocument<200> doc;
  if (deserializeJson(doc, configServer.arg("plain")) != DeserializationError::Ok) {
    configServer.send(400, "application/json", "{\"status\":\"error\",\"message\":\"JSON error\"}");
    return;
  }

  String ssid = doc["ssid"];
  String password = doc["password"];

  if (ssid.length() == 0 || password.length() == 0) {
    configServer.send(400, "application/json", "{\"status\":\"error\",\"message\":\"SSID atau password kosong\"}");
    return;
  }

  saveWiFiConfig(ssid, password);
  configServer.send(200, "application/json", "{\"status\":\"success\"}");
  
  Serial.println("🔄 Restarting ESP32...");
  delay(2000);
  ESP.restart();
}

void handleGetStatus() {
  StaticJsonDocument<200> doc;
  doc["mode"] = isConfigMode ? "config" : "normal";
  doc["wifi_connected"] = isWiFiConnected;
  doc["ssid"] = wifiSSID;
  
  String response;
  serializeJson(doc, response);
  configServer.send(200, "application/json", response);
}

void handleResetWiFi() {
  SPIFFS.remove(CONFIG_FILE);
  configServer.send(200, "application/json", "{\"status\":\"success\"}");
  Serial.println("🔄 WiFi reset, restarting...");
  delay(1000);
  ESP.restart();
}

bool connectToWiFi() {
  if (wifiSSID.length() == 0) {
    Serial.println("⚠️ No WiFi config");
    return false;
  }

  Serial.print("📡 Connecting to: " + wifiSSID + "... ");
  
  WiFi.mode(WIFI_STA);
  WiFi.begin(wifiSSID.c_str(), wifiPassword.c_str());
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n✅ WiFi Connected!");
    Serial.print("🌐 IP: ");
    Serial.println(WiFi.localIP());
    isWiFiConnected = true;
    return true;
  } else {
    Serial.println("\n❌ WiFi Failed!");
    isWiFiConnected = false;
    return false;
  }
}

void updateSensorData() {
  if (!isWiFiConnected) return;

  int soilRaw = analogRead(PIN_SOIL);
  int waterRaw = analogRead(PIN_WATER);
  
  float soilPercent = map(soilRaw, 4095, 0, 0, 100);
  float waterPercent = map(waterRaw, 4095, 0, 0, 100);
  
  Serial.print("🌱 Soil: ");
  Serial.print(soilPercent);
  Serial.print("% | 💧 Water: ");
  Serial.print(waterPercent);
  Serial.println("%");

  HTTPClient http;
  String fullUrl = apiBaseUrl + "/api/sensor/save";
  http.begin(client, fullUrl.c_str());
  http.addHeader("Content-Type", "application/json");
  http.setConnectTimeout(5000);
  
  StaticJsonDocument<200> doc;
  doc["moisture_level"] = soilPercent;
  doc["water_level"] = waterPercent;
  
  String jsonBody;
  serializeJson(doc, jsonBody);
  
  int httpCode = http.POST(jsonBody);
  
  if (httpCode == 200) {
    String response = http.getString();
    StaticJsonDocument<200> respDoc;
    if (deserializeJson(respDoc, response) == DeserializationError::Ok) {
      String command = respDoc["command"];
      
      if (command == "ON") {
        digitalWrite(PIN_RELAY, POMPA_ON);
        pumpStatus = "ON";
      } else {
        digitalWrite(PIN_RELAY, POMPA_OFF);
        pumpStatus = "OFF";
      }
    }
  }
  
  http.end();
}

void checkControlStatus() {
  if (!isWiFiConnected) return;

  HTTPClient http;
  String fullUrl = apiBaseUrl + "/api/control/status";
  http.begin(client, fullUrl.c_str());
  http.setConnectTimeout(5000);
  
  int httpCode = http.GET();
  
  if (httpCode == 200) {
    String response = http.getString();
    StaticJsonDocument<300> doc;
    if (deserializeJson(doc, response) == DeserializationError::Ok) {
      if (doc["pause_end_time"].isNull() || String(doc["pause_end_time"]) == "null") {
        if (isPaused) {
          isPaused = false;
          Serial.println("⏸️ Jeda selesai!");
        }
      } else {
        isPaused = true;
      }
    }
  }
  
  http.end();
}

void updateLED() {
  if (millis() - lastBlink >= BLINK_INTERVAL) {
    lastBlink = millis();
    ledState = !ledState;
    
    if (isConfigMode) {
      digitalWrite(PIN_LED, ledState ? HIGH : LOW);  // Blink
    } else if (isWiFiConnected) {
      digitalWrite(PIN_LED, HIGH);  // Terang
    } else {
      digitalWrite(PIN_LED, LOW);  // Mati
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(2000);
  
  Serial.println("\n\n🚀 Smart Irrigation v8");
  Serial.println("========================");
  
  pinMode(PIN_RELAY, OUTPUT);
  digitalWrite(PIN_RELAY, POMPA_OFF);
  
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  
  client.setInsecure();
  
  initSPIFFS();
  loadWiFiConfig();
  
  if (!connectToWiFi()) {
    startConfigMode();
  }
  
  if (!rtc.begin()) {
    Serial.println("⚠️ RTC not found");
  } else {
    Serial.println("✅ RTC OK");
  }
  
  Serial.println("✅ Ready!");
  Serial.println("========================\n");
}

void loop() {
  updateLED();
  
  if (isConfigMode) {
    configServer.handleClient();
    return;
  }

  if (millis() - lastSensorUpdate >= SENSOR_INTERVAL) {
    updateSensorData();
    lastSensorUpdate = millis();
  }
  
  if (millis() - lastStatusCheck >= STATUS_INTERVAL) {
    checkControlStatus();
    lastStatusCheck = millis();
  }
}