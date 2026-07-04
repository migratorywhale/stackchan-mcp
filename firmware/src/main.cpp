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

    connectWiFi();
    initPlayback();
    initPcmStreamService();
    initHttpServer();
}
void loop() {
    M5StackChan.update();
    handleHttpServer();
    serviceWiFi();
    updateServoGesture();

    updatePlayback();
    updateMicrophone();

    // マイク再開（完了検知より前に置く）
    if (shouldResumeMic()) {
        clearMicResumeRequest();
        if (!M5.Mic.isRunning()) {
            if (initMicrophone()) {
                Serial.println("[MIC] Mic resumed after playback");
            } else {
                Serial.println("[MIC] Mic resume failed");
            }
        }
    }

    delay(50);
}
