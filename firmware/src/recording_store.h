#pragma once

#include <stddef.h>
#include <stdint.h>

enum class RecordingSource : uint8_t {
    NONE,
    VOICE,
    TOUCH,
};

struct RecordingSnapshot {
    const uint8_t* data = nullptr;
    size_t size = 0;
};

void clearLastRecording();
bool storeLastRecording(const uint8_t* wav, size_t size, RecordingSource source);
bool hasLastRecording();
RecordingSnapshot getLastRecording();
RecordingSource getLastRecordingSource();
const char* recordingSourceName(RecordingSource source);
void markLastRecordingConsumed();
