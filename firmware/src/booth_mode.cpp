#include <nvs.h>

#include "booth_mode.h"
#include "mic_service.h"
#include "playback_service.h"

namespace {
bool boothMode = true;
bool persisted = false;

bool saveMode(bool enabled) {
    nvs_handle_t handle;
    if (nvs_open("sc_booth", NVS_READWRITE, &handle) != ESP_OK) return false;
    uint8_t value = 2;
    const bool saved = nvs_set_u8(handle, "enabled", enabled ? 1 : 0) == ESP_OK &&
                       nvs_commit(handle) == ESP_OK &&
                       nvs_get_u8(handle, "enabled", &value) == ESP_OK &&
                       value == (enabled ? 1 : 0);
    nvs_close(handle);
    return saved;
}
}  // namespace

bool initBoothMode() {
    boothMode = true;
    persisted = false;
    nvs_handle_t handle;
    if (nvs_open("sc_booth", NVS_READWRITE, &handle) == ESP_OK) {
        uint8_t saved = 2;
        const esp_err_t result = nvs_get_u8(handle, "enabled", &saved);
        if (result == ESP_ERR_NVS_NOT_FOUND) {
            boothMode = false;
            persisted = true;
        } else if (result == ESP_OK && saved <= 1) {
            boothMode = saved == 1;
            persisted = true;
        }
        nvs_close(handle);
    }
    // An unreadable setting must not silently turn the microphone back on.
    if (boothMode) suspendMicrophoneCapture();
    return persisted;
}

bool isBoothMode() { return boothMode; }
bool isBoothModePersisted() { return persisted; }

bool setBoothMode(bool enabled) {
    if (enabled) {
        const bool alreadySaved = boothMode && persisted;
        boothMode = true;
        const bool stopped = suspendMicrophoneCapture();
        persisted = alreadySaved || saveMode(true);
        return stopped && persisted;
    }

    if (boothMode || !persisted) {
        if (!isMicrophoneCaptureStopped() && !suspendMicrophoneCapture()) return false;
        if (!saveMode(false)) {
            persisted = false;
            return false;
        }
        boothMode = false;
        persisted = true;
        // The normal recovery path waits for speaker/PCM work to finish.
        requestMicResume();
    }
    return true;
}
