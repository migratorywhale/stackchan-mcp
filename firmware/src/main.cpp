#include <Arduino.h>

#include <M5Unified.h>
#include <M5StackChan.h>
#include <WiFi.h>
#include "http_server.h"
#include "types.h"
#include "config_loader.h"
#include "mic_service.h"
#include "wifi_manager.h"
#include "wifi_portal.h"
#include "playback_service.h"
#include "pcm_stream_service.h"
#include "face_service.h"
#include "servo_service.h"
#include "touch_service.h"
#include "audio_gate.h"
#include "env_service.h"
#include "camera_service.h"
#include "outbound_service.h"
#include "booth_mode.h"

void setup() {
    Serial.begin(115200);
    delay(1000);

    M5StackChan.begin();
    M5.Display.setBrightness(DISPLAY_BRIGHTNESS);

    // ── BtnA チェック：開機時に押したまま → 強制配网模式 ──────────────────
    // M5.update() を一度呼んでボタン状態を読み取る
    M5.update();
    if (M5.BtnA.isPressed()) {
        Serial.println("[SETUP] BtnA held at boot -> forcing WiFi portal");
        // portal は内部で ESP.restart() するので戻らない
        runWifiPortal();
    }

    initAudioGate();
    initFace();
    if (!initBoothMode()) {
        Serial.println("[WARN] Booth setting unreadable; microphone stays blocked");
    }

    Serial.println("\n=== Stack-chan firmware ===");

    auto spk_cfg = M5.Speaker.config();
    M5.Speaker.config(spk_cfg);
    M5.Speaker.setVolume(SPEAKER_VOLUME);

    if (!isBoothMode() && !initMicrophone()) {
        Serial.println("[ERROR] Microphone initialization failed!");
    }

    if (!initServo()) {
        Serial.println("[WARN] Servo init failed - head movement disabled");
    }

    if (!initTouchService()) {
        Serial.println("[WARN] Touch sensor unavailable");
    }
    if (!initEnvService()) {
        Serial.println("[WARN] Env sensor init failed - no temperature/humidity/pressure");
    }

    // ── WiFi 接続シーケンス ────────────────────────────────────────────────
    // connectWiFi() が false を返したら全部失敗 → 配网 portal に入る
    if (!connectWiFi()) {
        Serial.println("[SETUP] WiFi failed -> starting captive portal");
        // portal は内部で ESP.restart() するので戻らない
        runWifiPortal();
    }

    initPlayback();
    initPcmStreamService();
    initHttpServer();
    initOutboundService();
}

void loop() {
    updateCameraService();
    // The GC0308 owns the internal I2C pins during a burst session. The BSP
    // update polls the top touch sensor on that bus, so leave it paused until
    // the host ends the session or the firmware watchdog expires it.
    if (!isCameraSessionActive()) {
        M5StackChan.update();
    }
    updateTouchService();
    handleHttpServer();
    serviceOutboundCommands();
    serviceWiFi();
    updateServoGesture();

    updatePlayback();
    updateMicrophone();

    serviceMicrophoneResume();

    delay(50);
}
