# Power Mitra - Smart Energy Home

A comprehensive IoT-based smart energy monitoring and presence detection system. This project consists of three main components: an ESP32 firmware for hardware control, a Flutter web application for the dashboard interface, and a Python computer vision script for presence detection.

## Project Structure

- `esp32_smart_energy/` : ESP32 firmware for reading sensor data (voltage, current, light, temperature) and controlling relays.
- `smart_energy/` : Flutter Web App that provides a dashboard for monitoring energy usage, turning devices on/off, and configuring schedules.
- `presence_detector/` : Python script that uses OpenCV and YOLO/MediaPipe to detect human presence from camera feeds and updates the backend.

---

## 1. ESP32 Firmware

The ESP32 reads from various sensors (ZMPT101B for voltage, ACS712 for current, DHT11 for temperature/humidity, LDR for light) and controls appliances via relays based on real-time data or Firebase commands.

### Setup Instructions
1. Open `esp32_smart_energy.ino` in the Arduino IDE.
2. Install the required libraries in Arduino IDE:
   - `Firebase ESP32 Client` by Mobizt
   - `DHT sensor library` by Adafruit
   - `LiquidCrystal I2C`
3. Create a `secrets.h` file in the `esp32_smart_energy/` directory with your sensitive credentials:
   ```cpp
   #ifndef SECRETS_H
   #define SECRETS_H

   #define SECRET_WIFI_NAME "your-wifi-ssid"
   #define SECRET_WIFI_PASS "your-wifi-password"
   #define SECRET_FIREBASE_API_KEY "your-firebase-web-api-key"

   #endif
   ```
4. Connect your ESP32 board and click **Upload**.

---

## 2. Flutter Web Dashboard (Smart Energy)

A beautiful, responsive web interface built in Flutter to manage your smart home appliances, view real-time energy usage graphs, and handle presence monitoring.

### Setup Instructions
1. Ensure you have the [Flutter SDK](https://docs.flutter.dev/get-started/install) installed.
2. Navigate to the `smart_energy` directory:
   ```bash
   cd smart_energy
   ```
3. Install dependencies:
   ```bash
   flutter pub get
   ```
4. Create a `secrets.dart` file in `smart_energy/lib/` to hold your Firebase API key securely:
   ```dart
   const String secretFirebaseApiKey = "your-firebase-web-api-key";
   ```
5. Run the web app locally:
   ```bash
   flutter run -d chrome
   ```
6. To build for production deployment:
   ```bash
   flutter build web
   ```

---

## 3. Python Presence Detector

A lightweight Python service that processes video feeds (or demo video files) to detect humans using MediaPipe, and updates the Firebase Realtime Database.

### Setup Instructions
1. Ensure you have Python 3.8+ installed.
2. Navigate to the `presence_detector` directory:
   ```bash
   cd presence_detector
   ```
3. Create a virtual environment and activate it:
   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   # On Mac/Linux:
   source venv/bin/activate
   ```
4. Install the required Python packages:
   ```bash
   pip install -r requirements.txt
   ```
5. Run the presence detector:
   ```bash
   python main.py
   ```
   *(Note: The script currently listens to demo video streams triggered by the Flutter app for testing. It updates Firebase when it detects a person).*


