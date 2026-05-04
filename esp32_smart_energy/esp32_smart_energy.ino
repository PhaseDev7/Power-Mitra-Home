
#include <WiFi.h>
#include <Firebase_ESP_Client.h>
#include <addons/TokenHelper.h>
#include <addons/RTDBHelper.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <time.h>
#include <DHT.h>

#define WIFI_NAME "galaxy54321"
#define WIFI_PASS "Bhumibhumi"

#define FIREBASE_API_KEY "AIzaSyDw_vLWToV0NuoEzIqp_q2pTSRPuh6xnYg"
#define FIREBASE_DB_URL  "https://smart-energy-4f0a2-default-rtdb.asia-southeast1.firebasedatabase.app"

#define RELAY_PIN          25
#define LED_INDICATOR_PIN  26
#define FAN_RELAY_PIN      27
#define CURRENT_SENSOR_PIN 35
#define VOLTAGE_SENSOR_PIN 34
#define LDR_PIN            32
#define DHT_PIN             4

#define DHT_TYPE DHT11

#define ACS712_SENSITIVITY      0.066
#define ACS712_NOISE_FLOOR      0.3

#define ZMPT_CALIBRATION        3.2
#define VOLTAGE_NOISE_FLOOR     10.0

#define LDR_DARK_THRESHOLD  40
#define LDR_INVERT          true

#define FAN_TEMP_THRESHOLD  1.0

#define AC_FREQUENCY            50
#define RMS_SAMPLE_CYCLES       5
#define RMS_SAMPLE_DURATION_MS  (RMS_SAMPLE_CYCLES * (1000 / AC_FREQUENCY))

#define ESP32_ADC_MAX           4095.0
#define ESP32_REF_MV            3300.0

LiquidCrystal_I2C lcd(0x27, 16, 2);
DHT dht(DHT_PIN, DHT_TYPE);

FirebaseData   fbSender;
FirebaseData   fbStream;
FirebaseData   fbFanStream;
FirebaseAuth   fbAuth;
FirebaseConfig fbConfig;

float    currentAmps      = 0.0;
float    voltageVolts     = 0.0;
int      lightPercent     = 0;
bool     roomIsDark       = true;
float    tempCelsius      = 0.0;
float    humidity         = 0.0;
bool     presenceDetected = false;
bool     relayIsOn        = false;
bool     ledIndicatorOn   = false;
bool     fanIsOn          = false;
bool     firebaseReady    = false;
bool     streamStarted    = false;
bool     fanStreamStarted = false;

struct DeviceSchedule {
  bool    enabled  = false;
  uint8_t onHour   = 0;
  uint8_t onMin    = 0;
  uint8_t offHour  = 0;
  uint8_t offMin   = 0;
};

DeviceSchedule relaySchedule;
DeviceSchedule fanSchedule;

unsigned long lastScheduleCheck = 0;
const unsigned long SCHEDULE_CHECK_INTERVAL = 60000;

unsigned long lastSensorTime     = 0;
const unsigned long SENSOR_INTERVAL = 2000;

bool          lcdOverlay       = false;
unsigned long lcdOverlayEnd    = 0;

bool parseHHMM(const String& timeString, uint8_t& hours, uint8_t& minutes) {
  int colonIndex = timeString.indexOf(':');
  if (colonIndex < 1) return false;
  hours = (uint8_t)timeString.substring(0, colonIndex).toInt();
  minutes = (uint8_t)timeString.substring(colonIndex + 1).toInt();
  return true;
}

bool isInWindow(uint8_t nowH, uint8_t nowM,
                uint8_t onH,  uint8_t onM,
                uint8_t offH, uint8_t offM) {
  int now  = nowH  * 60 + nowM;
  int on   = onH   * 60 + onM;
  int off  = offH  * 60 + offM;
  if (on <= off) {
    return (now >= on && now < off);
  } else {
    return (now >= on || now < off);
  }
}

int readLightPercent() {
  long sum = 0;
  for (int i = 0; i < 20; i++) {
    sum += analogRead(LDR_PIN);
    delayMicroseconds(100);
  }
  int rawValue = (int)(sum / 20);
#if LDR_INVERT
  int percent = map(rawValue, 0, 4095, 100, 0);
#else
  int percent = map(rawValue, 0, 4095, 0, 100);
#endif
  percent = constrain(percent, 0, 100);
  Serial.printf("[LDR] raw=%d -> %d%% (invert=%s)\n", rawValue, percent, LDR_INVERT ? "ON" : "OFF");
  return percent;
}

bool readDHT() {
  const int   MAX_RETRIES = 3;
  const float T_MIN       = -20.0f;
  const float T_MAX       =  60.0f;
  const float H_MIN       =   5.0f;
  const float H_MAX       =  99.0f;

  for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    if (attempt > 1) delay(1100);

    float temperatureReading = dht.readTemperature(false, true);
    float humidityReading = dht.readHumidity(false);

    Serial.printf("[DHT] Attempt %d: T=%.1f H=%.1f\n", attempt, temperatureReading, humidityReading);

    if (isnan(temperatureReading) || isnan(humidityReading)) {
      Serial.printf("[DHT] Attempt %d: NaN\n", attempt);
      continue;
    }
    if (temperatureReading < T_MIN || temperatureReading > T_MAX || humidityReading < H_MIN || humidityReading > H_MAX) {
      Serial.printf("[DHT] Attempt %d: out of range (T=%.1f H=%.1f)\n", attempt, temperatureReading, humidityReading);
      continue;
    }

    tempCelsius = temperatureReading;
    humidity    = humidityReading;
    Serial.printf("[DHT] OK: %.1f°C | %.1f%%\n", tempCelsius, humidity);
    return true;
  }

  Serial.println("[DHT] All retries failed — keeping last known values");
  return false;
}

float readCurrentRMS() {
  double sum           = 0.0;
  double sumOfSquares  = 0.0;
  int    sampleCount   = 0;
  unsigned long startMs = millis();

  while (millis() - startMs < RMS_SAMPLE_DURATION_MS) {
    double sample  = analogRead(CURRENT_SENSOR_PIN);
    sum           += sample;
    sumOfSquares  += sample * sample;
    sampleCount++;
    delayMicroseconds(200);
  }

  double mean     = sum / sampleCount;
  double variance = (sumOfSquares / sampleCount) - (mean * mean);
  if (variance < 0) variance = 0;

  double rmsADC = sqrt(variance);
  double rmsMV  = (rmsADC / ESP32_ADC_MAX) * ESP32_REF_MV;
  float  amps   = (float)(rmsMV / (ACS712_SENSITIVITY * 1000.0));

  if (amps < ACS712_NOISE_FLOOR) amps = 0.0;
  Serial.printf("[ACS712] mean=%.1f rmsADC=%.3f rmsMV=%.2f -> %.3fA\n",
                mean, rmsADC, rmsMV, amps);
  return amps;
}

float readVoltageRMS() {
  double sum           = 0.0;
  double sumOfSquares  = 0.0;
  int    sampleCount   = 0;
  unsigned long startMs = millis();

  while (millis() - startMs < RMS_SAMPLE_DURATION_MS) {
    double sample  = analogRead(VOLTAGE_SENSOR_PIN);
    sum           += sample;
    sumOfSquares  += sample * sample;
    sampleCount++;
    delayMicroseconds(200);
  }

  double mean     = sum / sampleCount;
  double variance = (sumOfSquares / sampleCount) - (mean * mean);
  if (variance < 0) variance = 0;

  double rmsADC = sqrt(variance);
  double rmsMV  = (rmsADC / ESP32_ADC_MAX) * ESP32_REF_MV;
  float  volts  = (float)((rmsMV / 1000.0) * ZMPT_CALIBRATION * 100.0);

  if (volts < VOLTAGE_NOISE_FLOOR) volts = 0.0;

  Serial.printf("[ZMPT101B] mean=%.1f rmsADC=%.3f rmsMV=%.2f -> %.1fV\n",
                mean, rmsADC, rmsMV, volts);
  return volts;
}

void lcdShowMessage(const char* line1, const char* line2, unsigned long durationMs) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(line1);
  lcd.setCursor(0, 1);
  lcd.print(line2);
  lcdOverlay    = true;
  lcdOverlayEnd = millis() + durationMs;
}

void lcdShowReadings() {
  if (lcdOverlay) return;

  lcd.setCursor(0, 0);
  lcd.print("T:");
  lcd.print(tempCelsius, 1);
  lcd.print("C L:");
  lcd.print(lightPercent);
  lcd.print("%  ");

  lcd.setCursor(0, 1);
  lcd.print("I:");
  lcd.print(currentAmps, 2);
  lcd.print("A ");
  lcd.print(relayIsOn ? "L:ON " : "L:OFF");
  lcd.print(fanIsOn   ? "F:ON" : "F:OFF");
}

void onRelayCommand(FirebaseStream data) {
  Serial.printf("[STREAM-LIGHT] Path: %s | Type: %s\n", data.dataPath().c_str(), data.dataType().c_str());

  if (data.dataType() == "boolean") {
    bool newState = data.boolData();

    if (newState != relayIsOn) {
      relayIsOn = newState;
      digitalWrite(RELAY_PIN, relayIsOn ? LOW : HIGH);
      Serial.printf("[RELAY-LIGHT] Switched %s by website\n", relayIsOn ? "ON" : "OFF");
      lcdShowMessage("Light Command:", relayIsOn ? ">> Light ON  <<" : ">> Light OFF <<", 2000);
    } else {
      relayIsOn = newState;
      digitalWrite(RELAY_PIN, relayIsOn ? LOW : HIGH);
      Serial.printf("[RELAY-LIGHT] Synced to %s (initial)\n", relayIsOn ? "ON" : "OFF");
    }
  } else if (data.dataType() == "integer" || data.dataType() == "int") {
    bool newState = (data.intData() != 0);
    relayIsOn = newState;
    digitalWrite(RELAY_PIN, relayIsOn ? LOW : HIGH);
    Serial.printf("[RELAY-LIGHT] Switched %s (int value)\n", relayIsOn ? "ON" : "OFF");
    lcdShowMessage("Light Command:", relayIsOn ? ">> Light ON  <<" : ">> Light OFF <<", 2000);
  }
}

void onFanCommand(FirebaseStream data) {
  Serial.printf("[STREAM-FAN] Path: %s | Type: %s\n", data.dataPath().c_str(), data.dataType().c_str());

  if (data.dataType() == "boolean") {
    bool newState = data.boolData();

    if (newState != fanIsOn) {
      fanIsOn = newState;
      digitalWrite(FAN_RELAY_PIN, fanIsOn ? LOW : HIGH);
      Serial.printf("[RELAY-FAN] Switched %s by website\n", fanIsOn ? "ON" : "OFF");
      lcdShowMessage("Fan Command:", fanIsOn ? ">> Fan ON    <<" : ">> Fan OFF   <<", 2000);
    } else {
      fanIsOn = newState;
      digitalWrite(FAN_RELAY_PIN, fanIsOn ? LOW : HIGH);
      Serial.printf("[RELAY-FAN] Synced to %s (initial)\n", fanIsOn ? "ON" : "OFF");
    }
  } else if (data.dataType() == "integer" || data.dataType() == "int") {
    bool newState = (data.intData() != 0);
    fanIsOn = newState;
    digitalWrite(FAN_RELAY_PIN, fanIsOn ? LOW : HIGH);
    Serial.printf("[RELAY-FAN] Switched %s (int value)\n", fanIsOn ? "ON" : "OFF");
    lcdShowMessage("Fan Command:", fanIsOn ? ">> Fan ON    <<" : ">> Fan OFF   <<", 2000);
  }
}

void onStreamTimeout(bool timeout) {
  if (timeout) {
    Serial.println("[STREAM] Timeout — will auto-resume");
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n====== SMART ENERGY MONITOR ======");

  lcd.init();
  lcd.backlight();
  lcdShowMessage("Smart Energy", "Starting...", 0);

  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, HIGH);

  pinMode(LED_INDICATOR_PIN, OUTPUT);
  digitalWrite(LED_INDICATOR_PIN, HIGH);

  pinMode(FAN_RELAY_PIN, OUTPUT);
  digitalWrite(FAN_RELAY_PIN, HIGH);

  pinMode(LDR_PIN, INPUT);

  dht.begin();
  Serial.println("[DHT] Sensor initialized on pin D4 (GPIO4)");

  lcdShowMessage("WiFi:", "Connecting...", 0);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_NAME, WIFI_PASS);
  Serial.print("[WIFI] Connecting");
  int wifiAttempts = 0;
  while (WiFi.status() != WL_CONNECTED && wifiAttempts < 60) {
    Serial.print(".");
    delay(500);
    wifiAttempts++;
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WIFI] FAILED — running in offline mode");
    lcdShowMessage("WiFi FAILED!", "Offline mode...", 2000);
    delay(2000);
    return;
  }

  Serial.printf("[WIFI] Connected! IP: %s\n", WiFi.localIP().toString().c_str());
  lcdShowMessage("WiFi Connected!", WiFi.localIP().toString().c_str(), 1000);
  delay(1000);

  lcdShowMessage("Syncing clock...", "", 0);
  configTime(19800, 0, "pool.ntp.org", "time.nist.gov");
  Serial.print("[NTP] Syncing");
  time_t now = time(nullptr);
  int ntpRetries = 0;
  while (now < 1000000000L && ntpRetries < 20) {
    Serial.print(".");
    delay(500);
    now = time(nullptr);
    ntpRetries++;
  }
  Serial.println();
  Serial.printf("[NTP] %s\n", now > 1000000000L ? "Time synced!" : "FAILED — Firebase may not connect");

  lcdShowMessage("Firebase:", "Signing in...", 0);
  fbConfig.api_key = FIREBASE_API_KEY;
  fbConfig.database_url = FIREBASE_DB_URL;

  Serial.print("[FIREBASE] Signing up anonymously... ");
  bool signedUp = false;
  for (int attempt = 1; attempt <= 3 && !signedUp; attempt++) {
    if (Firebase.signUp(&fbConfig, &fbAuth, "", "")) {
      signedUp = true;
      Serial.println("OK");
    } else {
      Serial.printf("Attempt %d failed: %s\n", attempt,
                    fbConfig.signer.signupError.message.c_str());
      if (attempt < 3) delay(2000);
    }
  }
  if (!signedUp) {
    Serial.println("[FIREBASE] Anonymous sign-up failed — will retry in loop");
  }

  fbConfig.token_status_callback = tokenStatusCallback;
  Firebase.begin(&fbConfig, &fbAuth);
  Firebase.reconnectWiFi(true);
  fbSender.setResponseSize(4096);

  lcdShowMessage("Firebase:", "Connecting...", 0);
  Serial.print("[FIREBASE] Waiting for ready");
  unsigned long firebaseWaitStart = millis();
  while (!Firebase.ready() && millis() - firebaseWaitStart < 15000) {
    Serial.print(".");
    delay(500);
  }
  Serial.println();

  firebaseReady = Firebase.ready();
  if (firebaseReady) {
    Serial.println("[FIREBASE] Ready!");
    lcdShowMessage("Firebase:", "Connected!", 1000);

    if (Firebase.RTDB.beginStream(&fbStream, "/device_status/relay_on")) {
      Firebase.RTDB.setStreamCallback(&fbStream, onRelayCommand, onStreamTimeout);
      streamStarted = true;
      Serial.println("[STREAM] Listening for light relay commands");
    } else {
      Serial.printf("[STREAM-LIGHT] FAILED: %s\n", fbStream.errorReason().c_str());
    }

    if (Firebase.RTDB.beginStream(&fbFanStream, "/device_status/fan_on")) {
      Firebase.RTDB.setStreamCallback(&fbFanStream, onFanCommand, onStreamTimeout);
      fanStreamStarted = true;
      Serial.println("[STREAM] Listening for fan relay commands");
    } else {
      Serial.printf("[STREAM-FAN] FAILED: %s\n", fbFanStream.errorReason().c_str());
    }
  } else {
    Serial.println("[FIREBASE] NOT ready — will retry in loop");
    lcdShowMessage("Firebase:", "Retrying...", 1000);
  }

  delay(1000);
  lcd.clear();
  lcdOverlay = false;
  Serial.println("====== SETUP COMPLETE ======\n");
}

void loop() {
  if (lcdOverlay && millis() > lcdOverlayEnd) {
    lcdOverlay = false;
    lcd.clear();
  }

  if (!firebaseReady && Firebase.ready()) {
    firebaseReady = true;
    Serial.println("[FIREBASE] Connected (late)!");

    if (!streamStarted) {
      if (Firebase.RTDB.beginStream(&fbStream, "/device_status/relay_on")) {
        Firebase.RTDB.setStreamCallback(&fbStream, onRelayCommand, onStreamTimeout);
        streamStarted = true;
        Serial.println("[STREAM] Listening for light relay commands (late start)");
      }
    }
    if (!fanStreamStarted) {
      if (Firebase.RTDB.beginStream(&fbFanStream, "/device_status/fan_on")) {
        Firebase.RTDB.setStreamCallback(&fbFanStream, onFanCommand, onStreamTimeout);
        fanStreamStarted = true;
        Serial.println("[STREAM] Listening for fan relay commands (late start)");
      }
    }
  }

  if (millis() - lastSensorTime > SENSOR_INTERVAL) {
    lastSensorTime = millis();

    currentAmps = readCurrentRMS();

    voltageVolts = readVoltageRMS();

    lightPercent = readLightPercent();
    roomIsDark   = (lightPercent < LDR_DARK_THRESHOLD);

    readDHT();

    if (relayIsOn && !roomIsDark) {
      relayIsOn = false;
      digitalWrite(RELAY_PIN, HIGH);
      Serial.println("[SMART-LIGHT] Room is bright — overriding light relay OFF");
      lcdShowMessage("Auto-OFF:", "Sufficient light", 2000);
      if (firebaseReady && Firebase.ready()) {
        Firebase.RTDB.setBool(&fbSender, "/device_status/relay_on", false);
      }
    }

    bool shouldLedBeOn = roomIsDark && presenceDetected;
    if (shouldLedBeOn != ledIndicatorOn) {
      ledIndicatorOn = shouldLedBeOn;
      digitalWrite(LED_INDICATOR_PIN, ledIndicatorOn ? LOW : HIGH);
      Serial.printf("[LED-DEMO] Pin 26 → %s (dark=%s, presence=%s)\n",
                    ledIndicatorOn   ? "ON"  : "OFF",
                    roomIsDark       ? "YES" : "NO",
                    presenceDetected ? "YES" : "NO");
      if (firebaseReady && Firebase.ready()) {
        Firebase.RTDB.setBool(&fbSender, "/device_status/led_indicator_on", ledIndicatorOn);
      }
    }

    if (firebaseReady && Firebase.ready()) {
      FirebaseData presenceReader;
      if (Firebase.RTDB.getBool(&presenceReader, "/device_status/presence_detected")) {
        presenceDetected = presenceReader.boolData();
      }
    }

    if (!fanSchedule.enabled) {
      bool shouldFanBeOn  = presenceDetected && (tempCelsius >= FAN_TEMP_THRESHOLD);
      bool shouldFanBeOff = !presenceDetected || (tempCelsius < (FAN_TEMP_THRESHOLD - 1.0));

      if (shouldFanBeOn && !fanIsOn) {
        fanIsOn = true;
        digitalWrite(FAN_RELAY_PIN, LOW);
        Serial.printf("[SMART-FAN] Auto-ON: Temp %.1f°C >= %.1f°C & presence detected\n",
                      tempCelsius, FAN_TEMP_THRESHOLD);
        lcdShowMessage("Smart Fan:", "Auto-ON (HOT+PRES)", 2000);
        if (firebaseReady && Firebase.ready()) {
          Firebase.RTDB.setBool(&fbSender, "/device_status/fan_on", true);
        }
      } else if (shouldFanBeOff && fanIsOn) {
        fanIsOn = false;
        digitalWrite(FAN_RELAY_PIN, HIGH);
        if (!presenceDetected) {
          Serial.println("[SMART-FAN] Auto-OFF: No presence detected");
          lcdShowMessage("Smart Fan:", "Auto-OFF (empty)", 2000);
        } else {
          Serial.printf("[SMART-FAN] Auto-OFF: Temp %.1f°C < %.1f°C\n",
                        tempCelsius, FAN_TEMP_THRESHOLD - 1.0);
          lcdShowMessage("Smart Fan:", "Auto-OFF (cooled)", 2000);
        }
        if (firebaseReady && Firebase.ready()) {
          Firebase.RTDB.setBool(&fbSender, "/device_status/fan_on", false);
        }
      }
    } else {
      Serial.println("[SMART-FAN] Auto-logic skipped — fan schedule is active");
    }

    Serial.printf("[SENSOR] I: %.2fA | V: %.1fV | L: %d%% | T: %.1f°C | H: %.1f%% | Dark: %s | Relay: %s | LED26: %s | Fan: %s | Presence: %s\n",
                  currentAmps, voltageVolts, lightPercent, tempCelsius, humidity,
                  roomIsDark       ? "YES" : "NO",
                  relayIsOn        ? "ON"  : "OFF",
                  ledIndicatorOn   ? "ON"  : "OFF",
                  fanIsOn          ? "ON"  : "OFF",
                  presenceDetected ? "YES" : "NO");

    lcdShowReadings();

    if (firebaseReady && Firebase.ready()) {
      if (!Firebase.RTDB.setFloat(&fbSender, "/device_status/current", currentAmps)) {
        Serial.printf("[FB-ERR] current: %s\n", fbSender.errorReason().c_str());
      }
      if (!Firebase.RTDB.setFloat(&fbSender, "/device_status/voltage", voltageVolts)) {
        Serial.printf("[FB-ERR] voltage: %s\n", fbSender.errorReason().c_str());
      }
      if (!Firebase.RTDB.setInt(&fbSender, "/device_status/light_percent", lightPercent)) {
        Serial.printf("[FB-ERR] light: %s\n", fbSender.errorReason().c_str());
      }
      if (!Firebase.RTDB.setBool(&fbSender, "/device_status/room_is_dark", roomIsDark)) {
        Serial.printf("[FB-ERR] room_is_dark: %s\n", fbSender.errorReason().c_str());
      }
      if (!Firebase.RTDB.setFloat(&fbSender, "/device_status/temperature", tempCelsius)) {
        Serial.printf("[FB-ERR] temperature: %s\n", fbSender.errorReason().c_str());
      }
      if (!Firebase.RTDB.setFloat(&fbSender, "/device_status/humidity", humidity)) {
        Serial.printf("[FB-ERR] humidity: %s\n", fbSender.errorReason().c_str());
      }
    }
  }

  if (firebaseReady && Firebase.ready() &&
      (millis() - lastScheduleCheck > SCHEDULE_CHECK_INTERVAL)) {
    lastScheduleCheck = millis();

    struct tm timeinfo;
    if (!getLocalTime(&timeinfo)) {
      Serial.println("[SCHEDULE] Could not get local time — skipping");
    } else {
      uint8_t nowH = (uint8_t)timeinfo.tm_hour;
      uint8_t nowM = (uint8_t)timeinfo.tm_min;
      Serial.printf("[SCHEDULE] Local time: %02d:%02d\n", nowH, nowM);

      {
        FirebaseData scheduleData;
        if (Firebase.RTDB.getJSON(&scheduleData, "/schedules/relay")) {
          FirebaseJson& jsonData = scheduleData.jsonObject();
          FirebaseJsonData result;
          jsonData.get(result, "enabled");
          bool isEnabled = (result.stringValue == "true" || result.intValue == 1);
          relaySchedule.enabled = isEnabled;
          if (isEnabled) {
            String onTimeString, offTimeString;
            jsonData.get(result, "on_time");  onTimeString  = result.stringValue;
            jsonData.get(result, "off_time"); offTimeString = result.stringValue;
            parseHHMM(onTimeString,  relaySchedule.onHour,  relaySchedule.onMin);
            parseHHMM(offTimeString, relaySchedule.offHour, relaySchedule.offMin);

            bool shouldBeOn = isInWindow(nowH, nowM,
                                         relaySchedule.onHour, relaySchedule.onMin,
                                         relaySchedule.offHour, relaySchedule.offMin);

            if (shouldBeOn && !relayIsOn) {
              relayIsOn = true;
              digitalWrite(RELAY_PIN, LOW);
              Firebase.RTDB.setBool(&fbSender, "/device_status/relay_on", true);
              Serial.println("[SCHEDULE] Light: schedule → ON");
              lcdShowMessage("Schedule:", "Light ON (timed)", 2000);
            } else if (!shouldBeOn && relayIsOn) {
              relayIsOn = false;
              digitalWrite(RELAY_PIN, HIGH);
              Firebase.RTDB.setBool(&fbSender, "/device_status/relay_on", false);
              Serial.println("[SCHEDULE] Light: schedule → OFF");
              lcdShowMessage("Schedule:", "Light OFF (timed)", 2000);
            }
          }
        }
      }

      {
        FirebaseData scheduleData;
        if (Firebase.RTDB.getJSON(&scheduleData, "/schedules/fan")) {
          FirebaseJson& jsonData = scheduleData.jsonObject();
          FirebaseJsonData result;
          jsonData.get(result, "enabled");
          bool isEnabled = (result.stringValue == "true" || result.intValue == 1);
          fanSchedule.enabled = isEnabled;
          if (isEnabled) {
            String onTimeString, offTimeString;
            jsonData.get(result, "on_time");  onTimeString  = result.stringValue;
            jsonData.get(result, "off_time"); offTimeString = result.stringValue;
            parseHHMM(onTimeString,  fanSchedule.onHour,  fanSchedule.onMin);
            parseHHMM(offTimeString, fanSchedule.offHour, fanSchedule.offMin);

            bool shouldBeOn = isInWindow(nowH, nowM,
                                         fanSchedule.onHour, fanSchedule.onMin,
                                         fanSchedule.offHour, fanSchedule.offMin);

            if (shouldBeOn && !fanIsOn) {
              fanIsOn = true;
              digitalWrite(FAN_RELAY_PIN, LOW);
              Firebase.RTDB.setBool(&fbSender, "/device_status/fan_on", true);
              Serial.println("[SCHEDULE] Fan: schedule → ON");
              lcdShowMessage("Schedule:", "Fan ON (timed)", 2000);
            } else if (!shouldBeOn && fanIsOn) {
              fanIsOn = false;
              digitalWrite(FAN_RELAY_PIN, HIGH);
              Firebase.RTDB.setBool(&fbSender, "/device_status/fan_on", false);
              Serial.println("[SCHEDULE] Fan: schedule → OFF");
              lcdShowMessage("Schedule:", "Fan OFF (timed)", 2000);
            }
          }
        }
      }
    }
  }
}
