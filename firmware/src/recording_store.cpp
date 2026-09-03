#include <M5Unified.h>
#include "recording_store.h"

static uint8_t* s_wavBuf = nullptr;
static size_t s_wavSize = 0;
static bool s_wavReady = false;
static RecordingSource s_recordingSource = RecordingSource::NONE;

void clearLastRecording() {
    if (s_wavBuf) {
        free(s_wavBuf);
    }
    s_wavBuf = nullptr;
    s_wavSize = 0;
    s_wavReady = false;
    s_recordingSource = RecordingSource::NONE;
}

bool storeLastRecording(const uint8_t* wav, size_t size, RecordingSource source) {
    clearLastRecording();

    s_wavBuf = (uint8_t*)ps_malloc(size);
    if (!s_wavBuf) {
        Serial.println("[REC] WAV buffer alloc failed");
        return false;
    }
    memcpy(s_wavBuf, wav, size);
    s_wavSize = size;
    s_wavReady = true;
    s_recordingSource = source;
    Serial.printf("[REC] Stored recording: %u bytes source=%s\n",
                  (unsigned)size, recordingSourceName(source));
    return true;
}

bool hasLastRecording() {
    return s_wavReady && s_wavBuf != nullptr && s_wavSize > 0;
}

RecordingSnapshot getLastRecording() {
    RecordingSnapshot snapshot;
    if (hasLastRecording()) {
        snapshot.data = s_wavBuf;
        snapshot.size = s_wavSize;
    }
    return snapshot;
}

RecordingSource getLastRecordingSource() {
    return hasLastRecording() ? s_recordingSource : RecordingSource::NONE;
}

const char* recordingSourceName(RecordingSource source) {
    switch (source) {
        case RecordingSource::VOICE: return "voice";
        case RecordingSource::TOUCH: return "touch";
        default: return "none";
    }
}

void markLastRecordingConsumed() {
    s_wavReady = false;
    s_recordingSource = RecordingSource::NONE;
}
