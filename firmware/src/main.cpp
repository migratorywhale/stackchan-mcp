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

    Serial.println("\n=== Stack-chan firmware ===");

    auto spk_cfg = M5.Speaker.config();
    M5.Speaker.config(spk_cfg);
    M5.Speaker.setVolume(SPEAKER_VOLUME);

    if (!initMicrophone()) {
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
}

void loop() {
    static uint32_t lastMicResumeAttemptMs = 0;

    M5StackChan.update();
    updateTouchService();
    handleHttpServer();
    serviceWiFi();
    updateServoGesture();

    updatePlayback();
    updateMicrophone();

    // Playback can stop the microphone before the normal completion path has
    // a chance to request a resume. Keep the request latched until begin()
    // succeeds so one transient failure cannot leave the device deaf.
    if (!M5.Mic.isRunning()) {
        requestMicResume();
    }

    // マイク再開（完了検知より前に置く）
    if (shouldResumeMic()) {
        if (M5.Mic.isRunning()) {
            clearMicResumeRequest();
        } else if (millis() - lastMicResumeAttemptMs >= 1000) {
            lastMicResumeAttemptMs = millis();
            if (initMicrophone()) {
                clearMicResumeRequest();
                Serial.println("[MIC] Mic resumed after playback");
            } else {
                Serial.println("[MIC] Mic resume failed; retrying");
            }
        }
    }

    delay(50);
}
