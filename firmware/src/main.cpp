#include <Arduino.h>

#include <M5Unified.h>
#include <M5StackChan.h>
#include <WiFi.h>
#include "http_server.h"
#include "types.h"
#include "config_loader.h"
#include "mic_service.h"
#include "wifi_manager.h"
#include "playback_service.h"
#include "pcm_stream_service.h"
#include "face_service.h"
#include "servo_service.h"
#include "camera_service.h"
#include "audio_gate.h"
#include "env_service.h"

void setup() {
    Serial.begin(115200);
    delay(1000);

    M5StackChan.begin();
    M5.Display.setBrightness(DISPLAY_BRIGHTNESS);
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

    if (!initCamera()) {
        Serial.println("[WARN] Camera init failed - vision disabled");
    }
    if (!initEnvService()) {
        Serial.println("[WARN] Env sensor init failed - no temperature/humidity/pressure");
    }


    connectWiFi();
    initPlayback();
    initPcmStreamService();
    initHttpServer();
}
void loop() {
    static uint32_t lastMicResumeAttemptMs = 0;

    M5StackChan.update();
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
