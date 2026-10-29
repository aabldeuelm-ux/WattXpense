/*
 * WattXpense ESP32 example firmware
 * ================================
 *
 * THIS IS AN EXAMPLE, NOT A TESTED PRODUCT.
 *
 * No ESP32, current sensor or relay has been connected to this project. This sketch shows
 * the message contract the backend expects; it has not been validated on real hardware.
 * Mains wiring is dangerous. Use a qualified electrician and a properly rated enclosure.
 *
 * What it does:
 *   - connects to Wi-Fi and NTP,
 *   - samples an ACS712-family current sensor,
 *   - optionally samples a voltage sensor (otherwise it sends a nominal voltage and marks
 *     the measurement as an assumption),
 *   - posts one telemetry sample per interval with the device's wxdev_ token,
 *   - polls for relay commands and acknowledges the state it applied.
 *
 * Libraries (Arduino Library Manager):
 *   - ArduinoJson (v6 or newer)
 *
 * Board: any ESP32 with Wi-Fi. Change the configuration block to match your wiring.
 */

#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <time.h>

// ---------------------------------------------------------------------------------------
// Configuration: change every value in this block
// ---------------------------------------------------------------------------------------

const char *WIFI_SSID = "your-wifi";
const char *WIFI_PASSWORD = "your-password";

// The API origin. Use https:// and WiFiClientSecure for an untrusted network.
const char *API_BASE = "http://192.168.1.10:8011";
const char *DEVICE_TOKEN = "wxdev_replace_with_the_token_shown_once";
const char *DEVICE_ID = "esp32-main-01";

// GPIO configuration. Set PIN_VOLTAGE to -1 when no voltage sensor is fitted.
const int PIN_CURRENT = 34;
const int PIN_VOLTAGE = 35;
const int PIN_RELAY = 26;
const bool RELAY_HARDWARE_PRESENT = false;

// Calibration. Measure these; do not copy a datasheet value and call it calibrated.
const int ADC_MIDPOINT = 2048;          // ADC count with zero current
const float ADC_REFERENCE_MV = 3300.0;  // ADC full-scale reference
const float MV_PER_AMP = 66.0;          // ACS712-30A is nominally 66 mV/A
const float VOLTAGE_CALIBRATION = 1.0;  // multiplier for the optional voltage sensor

// Fallback voltage used only when PIN_VOLTAGE is -1. The sample is marked as an
// assumption so the dashboard can label the derived power as an estimate.
const float NOMINAL_VOLTAGE = 230.0;
const float ASSUMED_POWER_FACTOR = 1.0;

const unsigned long SAMPLE_INTERVAL_MS = 60000UL;
const unsigned long RELAY_POLL_INTERVAL_MS = 5000UL;

// ---------------------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------------------

unsigned long lastSampleAt = 0;
unsigned long lastRelayPollAt = 0;
bool relayKnown = false;
String relayAppliedState = "unknown";

// ---------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------

String isoTimestampUtc() {
  time_t now = time(nullptr);
  struct tm utc;
  gmtime_r(&now, &utc);
  char buffer[32];
  strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%SZ", &utc);
  return String(buffer);
}

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to Wi-Fi");
  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 30000UL) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi failed. Will retry on the next cycle.");
    return;
  }
  Serial.print("Connected: ");
  Serial.println(WiFi.localIP());
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
}

float readCurrentRms() {
  // ACS712 output sits at mid-supply for zero current. Sample a burst and take RMS.
  const int samples = 400;
  double sumSquares = 0.0;
  const float mvPerCount = ADC_REFERENCE_MV / 4095.0;

  for (int i = 0; i < samples; i++) {
    int raw = analogRead(PIN_CURRENT);
    float mv = raw * mvPerCount;
    float midpointMv = ADC_MIDPOINT * mvPerCount;
    float amps = (mv - midpointMv) / MV_PER_AMP;
    sumSquares += amps * amps;
    delayMicroseconds(200);
  }
  return sqrt(sumSquares / samples);
}

float readVoltageRms() {
  if (PIN_VOLTAGE < 0) {
    return NOMINAL_VOLTAGE;
  }
  // A voltage-sense module outputs a small signal proportional to mains voltage. The
  // multiplier must be calibrated against a trusted multimeter.
  const int samples = 400;
  double sumSquares = 0.0;
  const float mvPerCount = ADC_REFERENCE_MV / 4095.0;

  for (int i = 0; i < samples; i++) {
    int raw = analogRead(PIN_VOLTAGE);
    float mv = raw * mvPerCount;
    float volts = mv * VOLTAGE_CALIBRATION;
    sumSquares += volts * volts;
    delayMicroseconds(200);
  }
  return sqrt(sumSquares / samples);
}

bool postJson(const String &path, const String &body, String &responseBody) {
  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }
  HTTPClient http;
  WiFiClient client;
  String url = String(API_BASE) + path;
  if (!http.begin(client, url)) {
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);
  int code = http.POST(body);
  responseBody = http.getString();
  http.end();

  if (code < 200 || code >= 300) {
    Serial.print("POST ");
    Serial.print(path);
    Serial.print(" failed: ");
    Serial.print(code);
    Serial.print(" ");
    Serial.println(responseBody);
    return false;
  }
  return true;
}

bool getJson(const String &path, String &responseBody) {
  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }
  HTTPClient http;
  WiFiClient client;
  String url = String(API_BASE) + path;
  if (!http.begin(client, url)) {
    return false;
  }
  http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);
  int code = http.GET();
  responseBody = http.getString();
  http.end();

  if (code < 200 || code >= 300) {
    Serial.print("GET ");
    Serial.print(path);
    Serial.print(" failed: ");
    Serial.print(code);
    Serial.print(" ");
    Serial.println(responseBody);
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------------------
// Telemetry
// ---------------------------------------------------------------------------------------

void sendTelemetry() {
  float current = readCurrentRms();
  float voltage = readVoltageRms();
  bool voltageAssumed = PIN_VOLTAGE < 0;

  DynamicJsonDocument doc(768);
  JsonObject reading = doc.createNestedArray("readings").createNestedObject();
  reading["ts"] = isoTimestampUtc();
  reading["current"] = current;
  reading["voltage"] = voltage;
  reading["power_factor"] = ASSUMED_POWER_FACTOR;
  reading["sensor_status"] = "ok";
  reading["measurement_status"] =
      voltageAssumed ? "voltage assumed nominal; power derived, not measured"
                     : "ok";
  reading["simulated"] = false;

  String body;
  serializeJson(doc, body);

  String response;
  if (postJson("/api/device/telemetry", body, response)) {
    Serial.print("Telemetry sent: ");
    Serial.print(current, 2);
    Serial.print(" A at ");
    Serial.print(voltage, 1);
    Serial.println(" V");
  }
}

// ---------------------------------------------------------------------------------------
// Relay
// ---------------------------------------------------------------------------------------

void applyRelayState(const String &state) {
  if (!RELAY_HARDWARE_PRESENT) {
    return;
  }
  if (state == "pending_on" || state == "on") {
    digitalWrite(PIN_RELAY, HIGH);
    relayAppliedState = "on";
    relayKnown = true;
  } else if (state == "pending_off" || state == "off") {
    digitalWrite(PIN_RELAY, LOW);
    relayAppliedState = "off";
    relayKnown = true;
  }
}

void acknowledgeRelay(const String &state) {
  DynamicJsonDocument doc(256);
  doc["state"] = state;
  doc["note"] = "example firmware; state applied by command, no physical feedback";
  String body;
  serializeJson(doc, body);

  String response;
  if (!postJson("/api/device/relay/ack", body, response)) {
    Serial.println("Relay acknowledgement failed; command may be retried by the user.");
  }
}

void pollRelay() {
  if (!RELAY_HARDWARE_PRESENT) {
    return;
  }
  String response;
  if (!getJson("/api/device/relay", response)) {
    return;
  }

  DynamicJsonDocument doc(512);
  if (deserializeJson(doc, response) != DeserializationError::Ok) {
    Serial.println("Relay response was not valid JSON.");
    return;
  }
  const char *state = doc["relay"]["relay_state"] | "unknown";
  if (String(state) == "pending_on" || String(state) == "pending_off") {
    applyRelayState(state);
    acknowledgeRelay(relayAppliedState);
  }
}

// ---------------------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(PIN_RELAY, OUTPUT);
  digitalWrite(PIN_RELAY, LOW);  // fail safe: relay open at boot
  if (PIN_VOLTAGE >= 0) {
    pinMode(PIN_VOLTAGE, INPUT);
  }
  analogReadResolution(12);

  Serial.println();
  Serial.println("WattXpense ESP32 example. Not a tested product.");
  Serial.print("Device id: ");
  Serial.println(DEVICE_ID);

  connectWifi();
}

void loop() {
  connectWifi();

  unsigned long now = millis();
  if (lastSampleAt == 0 || now - lastSampleAt >= SAMPLE_INTERVAL_MS) {
    lastSampleAt = now;
    sendTelemetry();
  }
  if (lastRelayPollAt == 0 || now - lastRelayPollAt >= RELAY_POLL_INTERVAL_MS) {
    lastRelayPollAt = now;
    pollRelay();
  }

  delay(50);
}
