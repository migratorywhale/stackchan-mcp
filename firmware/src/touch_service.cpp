#include "touch_service.h"

#include <Arduino.h>
#include <M5StackChan.h>
#include <M5Unified.h>
#include <string.h>

#include "face_service.h"
#include "mic_service.h"
#include "pcm_stream_service.h"
#include "playback_service.h"
#include "servo_service.h"
#include "touch_gesture.h"

namespace {

constexpr uint8_t TOUCH_SENSOR_ADDRESS = 0x68;
constexpr uint32_t CLICK_AFTER_SWIPE_GUARD_MS = 500;
constexpr uint32_t PETTING_DURATION_MS = 5000;
constexpr uint32_t RESUME_RETRY_INTERVAL_MS = 2000;

TouchRuntimeStatus status;
TouchTrajectoryDetector trajectoryDetector;
PettingDetector pettingDetector;
bool touchSensorExpected = false;
bool resumeRetryPending = false;
bool pettingOwnsFace = false;
uint32_t pettingStartedMs = 0;
uint32_t pettingFaceRevision = 0;
uint32_t lastResumeAttemptMs = 0;
uint32_t lastSwipeMs = 0;
WhaleFace faceBeforePetting = WHALE_CALM;

void clearIntensities() {
    for (uint8_t& intensity : status.intensities) {
        intensity = 0;
    }
}

void recordEvent(const char* event, uint32_t nowMs) {
    status.lastEvent = event;
    status.lastEventMs = nowMs;
}

bool audioIsBusy() {
    return isPlaybackActive() || isPcmStreamActive() || strcmp(getMicStateName(), "idle") != 0;
}

void startPetting(uint32_t nowMs) {
    ++status.petCount;
    if (audioIsBusy()) {
        ++status.suppressedPetCount;
        Serial.println("[TOUCH] Petting detected; animation suppressed while audio is busy");
        return;
    }

    const bool stillOwnsCurrentFace =
        status.pettingActive && pettingOwnsFace && getFaceCommandRevision() == pettingFaceRevision;
    if (!stillOwnsCurrentFace) {
        WhaleFace current = WHALE_CALM;
        if (whaleFaceFromName(getCurrentFaceName(), &current)) {
            faceBeforePetting = current;
        }
    }

    // The compiled WHALE_HAPPY asset is koke_cheering.gif.
    setWhaleFace(WHALE_HAPPY);
    pettingFaceRevision = getFaceCommandRevision();
    pettingOwnsFace = true;
    pettingStartedMs = nowMs;
    status.pettingActive = true;

    if (isServoReady()) {
        servoShake();
    }
    Serial.println("[TOUCH] Petting gesture detected");
}

void finishPettingIfDue(uint32_t nowMs) {
    if (!status.pettingActive || nowMs - pettingStartedMs < PETTING_DURATION_MS) {
        return;
    }

    status.pettingActive = false;
    if (pettingOwnsFace && getFaceCommandRevision() == pettingFaceRevision) {
        setWhaleFace(faceBeforePetting);
    }
    pettingOwnsFace = false;
}

void handleSwipe(TouchSwipeDirection direction, uint32_t nowMs) {
    lastSwipeMs = nowMs;
    if (pettingDetector.observe(direction, nowMs)) {
        recordEvent("petting", nowMs);
        startPetting(nowMs);
        return;
    }

    recordEvent(direction == TouchSwipeDirection::FORWARD ? "swipe_forward" : "swipe_backward", nowMs);
}

}  // namespace

bool initTouchService() {
    status.suspended = false;
    status.available = M5.In_I2C.scanID(TOUCH_SENSOR_ADDRESS);
    touchSensorExpected = status.available;
    resumeRetryPending = false;
    clearIntensities();
    trajectoryDetector.reset();
    pettingDetector.reset();
    lastResumeAttemptMs = 0;
    lastSwipeMs = 0;
    Serial.printf("[TOUCH] Si12T %s at 0x%02x\n", status.available ? "ready" : "not found", TOUCH_SENSOR_ADDRESS);
    return status.available;
}

void updateTouchService() {
    const uint32_t nowMs = millis();
    finishPettingIfDue(nowMs);

    if (status.suspended) {
        return;
    }
    if (!status.available) {
        if (resumeRetryPending && nowMs - lastResumeAttemptMs >= RESUME_RETRY_INTERVAL_MS) {
            resumeTouchService();
        }
        return;
    }

    const auto& intensities = M5StackChan.TouchSensor.getIntensities();
    for (size_t i = 0; i < 3; ++i) {
        status.intensities[i] = intensities[i];
    }

    const TouchTrajectoryResult trajectory = trajectoryDetector.update(status.intensities);
    if (trajectory.hasSwipe) {
        handleSwipe(trajectory.direction, nowMs);
        return;
    }
    if (M5StackChan.TouchSensor.wasHold()) {
        recordEvent("hold", nowMs);
        return;
    }
    if (M5StackChan.TouchSensor.wasClicked() && !trajectory.suppressClick &&
        nowMs - lastSwipeMs > CLICK_AFTER_SWIPE_GUARD_MS) {
        ++status.recordRequestCount;
        if (requestTouchRecording()) {
            recordEvent("recording_started", nowMs);
        } else {
            ++status.recordStartFailureCount;
            recordEvent("recording_rejected", nowMs);
        }
    }
}

void suspendTouchService() {
    status.suspended = true;
    clearIntensities();
    trajectoryDetector.reset();
    pettingDetector.reset();
}

bool resumeTouchService() {
    lastResumeAttemptMs = millis();
    const bool busReady = M5.In_I2C.begin();
    const bool sensorPresent = busReady && M5.In_I2C.scanID(TOUCH_SENSOR_ADDRESS);
    if (sensorPresent) {
        M5StackChan.TouchSensor.begin();
    }

    status.available = sensorPresent;
    status.suspended = false;
    clearIntensities();
    trajectoryDetector.reset();
    pettingDetector.reset();
    lastSwipeMs = 0;

    const bool restored = busReady && (!touchSensorExpected || sensorPresent);
    resumeRetryPending = !restored;
    if (!restored) {
        ++status.resumeFailureCount;
        Serial.printf("[TOUCH] Resume failed: bus=%s sensor=%s expected=%s\n",
                      busReady ? "ready" : "failed", sensorPresent ? "ready" : "missing",
                      touchSensorExpected ? "yes" : "no");
        return false;
    }

    Serial.printf("[TOUCH] Internal I2C resumed (sensor=%s)\n",
                  sensorPresent ? "ready" : "not present");
    return true;
}

TouchRuntimeStatus getTouchRuntimeStatus() {
    return status;
}
