#pragma once

#include <stddef.h>
#include <stdint.h>

struct RecordingSnapshot {
    const uint8_t* data = nullptr;
    size_t size = 0;
};

void clearLastRecording();
bool storeLastRecording(const uint8_t* wav, size_t size);
bool hasLastRecording();
RecordingSnapshot getLastRecording();
void markLastRecordingConsumed();
