#pragma once
#include "types.h"

void initPlayback();                 // setup()で呼ぶ
void updatePlayback();
bool isPlaybackActive();
bool shouldResumeMic();
void clearMicResumeRequest();
void requestMicResume();
bool enqueueAudioTask(const AudioTask& task);
enum PcmPlaybackResult {
    PCM_PLAYBACK_OK,
    PCM_PLAYBACK_QUEUED,
    PCM_PLAYBACK_BUSY,
    PCM_PLAYBACK_SESSION_MISMATCH,
    PCM_PLAYBACK_INVALID,
    PCM_PLAYBACK_SPEAKER_FAILED,
};
enum class PcmOrigin { DIRECT, OUTBOUND };
struct PlaybackStatus {
    bool playing = false;
    bool pcm = false;
    bool pcmFinalSegment = false;
    const char* pcmSession = "";
    size_t currentBytes = 0;
    size_t queuedPcmBytes = 0;
    size_t queuedPcmSegments = 0;
    size_t audioQueueDepth = 0;
    size_t downloadQueueDepth = 0;
    bool downloadInFlight = false;
    unsigned long downloadAgeMs = 0;
    unsigned long downloadWatchdogMs = 0;
    bool micResumeRequested = false;
    unsigned long startedMs = 0;
    unsigned long deadlineMs = 0;
};
// OK/QUEUED/SPEAKER_FAILED consume pcmData; all other results leave it caller-owned.
PcmPlaybackResult startPcmPlayback(uint8_t* pcmData, size_t pcmSize, const String& sessionId, bool finalSegment,
                                 PcmOrigin origin = PcmOrigin::DIRECT, uint32_t generation = 0);
PcmPlaybackResult stagePcmPlayback(uint8_t* pcmData, size_t pcmSize, const String& sessionId, long seq, bool finalSegment,
                                 PcmOrigin origin = PcmOrigin::DIRECT, uint32_t generation = 0);
void clearQueuedPcmPlayback();
// Main-loop only; does not stop the active segment or discard other sessions.
void clearQueuedPcmSession(const String& sessionId, PcmOrigin origin, uint32_t generation = 0);
void clearStaleOutboundPcm();
PlaybackStatus getPlaybackStatus();
