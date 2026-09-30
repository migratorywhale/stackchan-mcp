#pragma once

#include "recording_store.h"

enum MicState {
    MIC_IDLE = 0,
    MIC_TRIGGERING,
    MIC_RECORDING,
    MIC_SENDING
};

inline bool canReplaceWithTouch(MicState state, RecordingSource source) {
    return state == MIC_IDLE || state == MIC_TRIGGERING ||
           (state == MIC_RECORDING && source == RecordingSource::VOICE);
}
