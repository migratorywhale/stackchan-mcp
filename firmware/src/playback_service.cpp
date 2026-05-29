#include <M5Unified.h>
#include <math.h>
#include <queue>
#include "playback_service.h"
#include "audio_download.h"
#include "config.h"
#include "face_service.h"
#include "wav_parser.h"

struct PlaybackRuntimeState {
    size_t lipSyncOffset = 0;
    unsigned long lastLipMs = 0;
    size_t pcmOffset = 0;
    size_t pcmSize = 0;
    uint32_t sampleRate = 24000;
    uint16_t bytesPerFrame = 2;
    bool currentIsPcm = false;
    String pcmSessionId = "";
    bool pcmFinalSegment = false;
};

static PlaybackRuntimeState s_playbackState;
static std::priority_queue<AudioTask> s_audioQueue;
static bool s_isPlaying = false;
static uint8_t* s_currentAudioData = nullptr;
static size_t s_currentAudioSize = 0;
static unsigned long s_playbackDeadlineMs = 0;
static unsigned long s_playbackStartMs = 0;
static bool s_micResumeRequested = false;
static String s_lastPlayedVoiceId = "";

#define LIPSYNC_INTERVAL_MS   50
#define LIPSYNC_CHUNK_SAMPLES 1024
#define PCM_SAMPLE_RATE       24000
#define PCM_BYTES_PER_SAMPLE  2
#define MAX_PCM_BYTES         (2 * 1024 * 1024)
#define MAX_QUEUED_PCM_BYTES  (2 * 1024 * 1024)

// ── FreeRTOS: URLをCore 0に渡すキュー
//    StringはFreeRTOSキューに乗せられないのでchar配列で渡す
#define MAX_URL_LEN 256
static QueueHandle_t s_downloadQueue = nullptr;

struct PcmBuffer {
    uint8_t* data;
    size_t size;
    String sessionId;
    bool finalSegment;
};

struct DownloadedAudio {
    uint8_t* data;
    size_t size;
};

static std::queue<PcmBuffer> s_pcmQueue;
static size_t s_pcmQueuedBytes = 0;
static QueueHandle_t s_downloadCompleteQueue = nullptr;
static uint8_t* s_retiredPlaybackData = nullptr;
static size_t s_retiredPlaybackSize = 0;

void clearQueuedPcmPlayback() {
    while (!s_pcmQueue.empty()) {
        PcmBuffer dropped = s_pcmQueue.front();
        s_pcmQueue.pop();
        free(dropped.data);
    }
    s_pcmQueuedBytes = 0;
    Serial.println("[PCM] Queue cleared");
}

static bool enqueuePcmBuffer(uint8_t* pcmData, size_t pcmSize, const String& sessionId, bool finalSegment) {
    if (pcmSize > MAX_QUEUED_PCM_BYTES - s_pcmQueuedBytes) {
        Serial.println("[PCM] Queue full");
        return false;
    }
    s_pcmQueue.push({pcmData, pcmSize, sessionId, finalSegment});
    s_pcmQueuedBytes += pcmSize;
    Serial.printf("[PCM] Queued segment: session=%s bytes=%u queued=%u final=%s\n",
                  sessionId.c_str(), (unsigned)pcmSize,
                  (unsigned)s_pcmQueuedBytes, finalSegment ? "true" : "false");
    return true;
}

static void releaseRetiredPlaybackBuffer() {
    if (!s_retiredPlaybackData) {
        return;
    }
    Serial.printf("[PLAY] Releasing retired playback buffer: bytes=%u\n",
                  (unsigned)s_retiredPlaybackSize);
    free(s_retiredPlaybackData);
    s_retiredPlaybackData = nullptr;
    s_retiredPlaybackSize = 0;
}

static void retireCurrentPlaybackBuffer() {
    releaseRetiredPlaybackBuffer();
    if (!s_currentAudioData) {
        return;
    }
    Serial.printf("[PLAY] Retiring playback buffer: bytes=%u speakerPlaying=%s\n",
                  (unsigned)s_currentAudioSize,
                  M5.Speaker.isPlaying() ? "true" : "false");
    s_retiredPlaybackData = s_currentAudioData;
    s_retiredPlaybackSize = s_currentAudioSize;
    s_currentAudioData = nullptr;
    s_currentAudioSize = 0;
}

// ════════════════════════════════════════
//  ダウンロードタスク（loop()とは別タスクで動く）
//  loop()をブロックしないための分離
// ════════════════════════════════════════
static void downloadTask(void* arg) {
    char url[MAX_URL_LEN];
    for (;;) {
        if (xQueueReceive(s_downloadQueue, url, portMAX_DELAY) != pdTRUE) continue;

        uint8_t* data = nullptr;
        size_t   size = 0;

        if (downloadVoice(String(url), &data, &size)) {
            DownloadedAudio completed = {data, size};
            if (!s_downloadCompleteQueue ||
                xQueueSend(s_downloadCompleteQueue, &completed, 0) != pdTRUE) {
                Serial.println("[DOWNLOAD] Complete queue full; dropping audio");
                free(data);
                continue;
            }
            Serial.printf("[DOWNLOAD] Ready: %u bytes\n", (unsigned)size);
        } else {
            Serial.println("[DOWNLOAD] Failed");
//            setFaceExpression(FACE_IDLE);
        }
    }
}

// ════════════════════════════════════════
//  初期化（setup()から呼ぶ）
// ════════════════════════════════════════
void initPlayback() {
    s_downloadQueue = xQueueCreate(4, sizeof(char) * MAX_URL_LEN);
    s_downloadCompleteQueue = xQueueCreate(2, sizeof(DownloadedAudio));
    if (!s_downloadQueue || !s_downloadCompleteQueue) {
        Serial.println("[PLAY] Failed to create playback queues");
        return;
    }
    xTaskCreatePinnedToCore(
        downloadTask,
        "downloadTask",
        8192,
        nullptr,
        1,
        nullptr,
        1   // Core 1
    );
    Serial.println("[PLAY] Download task started on Core 1");
}

// ════════════════════════════════════════
//  再生リクエスト受付（ノンブロッキング）
//  enqueueAudioTask()から呼ばれる
// ════════════════════════════════════════
static void startPlayback(const AudioTask& task) {
    if (!s_downloadQueue) {
        Serial.println("[PLAY] Queue not initialized!");
        return;
    }
    char url[MAX_URL_LEN];
    task.voice_url.toCharArray(url, MAX_URL_LEN);
    if (xQueueSend(s_downloadQueue, url, 0) != pdTRUE) {
        Serial.printf("[PLAY] Download queue full; dropped: %s\n", url);
        return;
    }
    setFaceExpression(FACE_THINKING);
    Serial.printf("[PLAY] Queued for download: %s\n", url);
}

// ════════════════════════════════════════
//  ダウンロード完了チェック → Speaker起動
//  loop()から毎回呼ぶ（Core 1でSpeaker操作するために分離）
// ════════════════════════════════════════
static bool startDownloadedWavPlayback(uint8_t* wavData, size_t wavSize) {
    WavInfo wavInfo;
    if (!parseWavInfo(wavData, wavSize, &wavInfo)) {
        Serial.println("[PLAY] Refusing invalid WAV");
        free(wavData);
        setFaceExpression(FACE_IDLE);
        return false;
    }

    retireCurrentPlaybackBuffer();
    s_currentAudioData = wavData;
    s_currentAudioSize = wavSize;
    s_playbackState.pcmOffset = wavInfo.dataOffset;
    s_playbackState.pcmSize = wavInfo.dataSize;
    s_playbackState.sampleRate = wavInfo.sampleRate;
    s_playbackState.bytesPerFrame = (wavInfo.channels * wavInfo.bitsPerSample) / 8;

    // 再生時間 + 2秒のデッドライン
    const float bytes_per_sec = (float)s_playbackState.sampleRate * (float)s_playbackState.bytesPerFrame;
    s_playbackDeadlineMs = millis() +
        (unsigned long)((s_playbackState.pcmSize / bytes_per_sec) * 1000.0f) + 2000;

    // マイク停止 → スピーカー起動
    if (M5.Mic.isRunning()) {
        M5.Mic.end();
        vTaskDelay(pdMS_TO_TICKS(200));  // 固定200ms待機に戻す
    }
    if (!M5.Speaker.isRunning()) {
        M5.Speaker.begin();
    }

    Serial.println("[PLAY] Mic stopped");
    M5.Speaker.setVolume(SPEAKER_VOLUME);
    bool ok = M5.Speaker.playWav(s_currentAudioData, s_currentAudioSize);
    if (!ok) {
        Serial.println("[PLAY] Speaker rejected playWav");
        retireCurrentPlaybackBuffer();
        setFaceExpression(FACE_IDLE);
        s_micResumeRequested = true;
        return false;
    }
    setFaceExpression(FACE_PLAYING);

    s_playbackState.lipSyncOffset = s_playbackState.pcmOffset;
    s_playbackState.lastLipMs     = 0;
    s_isPlaying     = true;
    s_playbackState.currentIsPcm = false;
    s_playbackState.pcmSessionId = "";
    s_playbackState.pcmFinalSegment = false;
    clearQueuedPcmPlayback();
    s_playbackStartMs  = millis();
    Serial.println("[PLAY] Speaker started");
    return true;
}

static void checkPendingPlayback() {
    if (s_isPlaying || !s_downloadCompleteQueue) {
        return;
    }

    DownloadedAudio completed = {};
    if (xQueueReceive(s_downloadCompleteQueue, &completed, 0) != pdTRUE) {
        return;
    }
    startDownloadedWavPlayback(completed.data, completed.size);
}

PcmPlaybackResult startPcmPlayback(uint8_t* pcmData, size_t pcmSize, const String& sessionId, bool finalSegment) {
    if (!pcmData || pcmSize == 0) {
        Serial.println("[PCM] Empty body");
        return PCM_PLAYBACK_INVALID;
    }
    if (sessionId.length() == 0) {
        Serial.println("[PCM] Missing session id");
        return PCM_PLAYBACK_INVALID;
    }
    if ((pcmSize % PCM_BYTES_PER_SAMPLE) != 0 || pcmSize > MAX_PCM_BYTES) {
        Serial.printf("[PCM] Invalid size: %u\n", (unsigned)pcmSize);
        return PCM_PLAYBACK_INVALID;
    }
    if (s_isPlaying || M5.Speaker.isPlaying()) {
        if (s_playbackState.currentIsPcm && sessionId == s_playbackState.pcmSessionId &&
            enqueuePcmBuffer(pcmData, pcmSize, sessionId, finalSegment)) {
            return PCM_PLAYBACK_QUEUED;
        }
        Serial.printf("[PCM] Busy; refusing segment session=%s current=%s\n",
                      sessionId.c_str(), s_playbackState.pcmSessionId.c_str());
        return PCM_PLAYBACK_BUSY;
    }

    if (!s_pcmQueue.empty()) {
        clearQueuedPcmPlayback();
    }

    retireCurrentPlaybackBuffer();

    s_currentAudioData = pcmData;
    s_currentAudioSize = pcmSize;
    s_playbackState.pcmOffset = 0;
    s_playbackState.pcmSize = pcmSize;
    s_playbackState.sampleRate = PCM_SAMPLE_RATE;
    s_playbackState.bytesPerFrame = PCM_BYTES_PER_SAMPLE;

    const float bytes_per_sec = (float)PCM_SAMPLE_RATE * (float)PCM_BYTES_PER_SAMPLE;
    s_playbackDeadlineMs = millis() +
        (unsigned long)((pcmSize / bytes_per_sec) * 1000.0f) + 2000;

    if (M5.Mic.isRunning()) {
        M5.Mic.end();
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    if (!M5.Speaker.isRunning()) {
        M5.Speaker.begin();
    }

    M5.Speaker.setVolume(SPEAKER_VOLUME);
    bool ok = M5.Speaker.playRaw((const int16_t*)s_currentAudioData,
                                 s_currentAudioSize / sizeof(int16_t),
                                 PCM_SAMPLE_RATE,
                                 false,
                                 1,
                                 -1,
                                 true);
    if (!ok) {
        Serial.println("[PCM] Speaker rejected playRaw");
        free(s_currentAudioData);
        s_currentAudioData = nullptr;
        s_currentAudioSize = 0;
        s_playbackState.pcmSize = 0;
        setFaceExpression(FACE_IDLE);
        s_micResumeRequested = true;
        return PCM_PLAYBACK_SPEAKER_FAILED;
    }

    setFaceExpression(FACE_PLAYING);
    s_playbackState.lipSyncOffset = 0;
    s_playbackState.lastLipMs = 0;
    s_isPlaying = true;
    s_playbackState.currentIsPcm = true;
    s_playbackState.pcmSessionId = sessionId;
    s_playbackState.pcmFinalSegment = finalSegment;
    s_playbackStartMs = millis();
    Serial.printf("[PCM] Speaker started: session=%s bytes=%u final=%s queue=%u @ 24kHz mono s16le\n",
                  sessionId.c_str(), (unsigned)pcmSize,
                  finalSegment ? "true" : "false", (unsigned)s_pcmQueuedBytes);
    return PCM_PLAYBACK_OK;
}

// ════════════════════════════════════════
//  口パク更新（loop()から毎回呼ぶ）
// ════════════════════════════════════════
static void updateLipSync() {
    if (!s_isPlaying || s_currentAudioData == nullptr || s_currentAudioSize == 0) return;

    unsigned long now = millis();
    if (now - s_playbackState.lastLipMs < LIPSYNC_INTERVAL_MS) return;
    s_playbackState.lastLipMs = now;

    if (s_playbackState.lipSyncOffset < s_playbackState.pcmOffset) s_playbackState.lipSyncOffset = s_playbackState.pcmOffset;
    if (s_playbackState.lipSyncOffset >= s_playbackState.pcmOffset + s_playbackState.pcmSize) {
        setMouthOpen(0.0f);
        return;
    }

    int16_t* pcm = (int16_t*)(s_currentAudioData + s_playbackState.lipSyncOffset);
    size_t remainBytes = s_playbackState.pcmOffset + s_playbackState.pcmSize - s_playbackState.lipSyncOffset;
    size_t samples = min((size_t)LIPSYNC_CHUNK_SAMPLES, remainBytes / sizeof(int16_t));
    if (samples == 0) {
        setMouthOpen(0.0f);
        return;
    }

    float sum = 0.0f;
    for (size_t i = 0; i < samples; i++) {
        float v = (float)pcm[i] / 32768.0f;
        sum += v * v;
    }
    setMouthOpen(constrain(sqrtf(sum / samples) * 8.0f, 0.0f, 1.0f));
    s_playbackState.lipSyncOffset += samples * sizeof(int16_t);
}

PlaybackStatus getPlaybackStatus() {
    PlaybackStatus status;
    status.playing = s_isPlaying;
    status.pcm = s_playbackState.currentIsPcm;
    status.pcmFinalSegment = s_playbackState.pcmFinalSegment;
    status.pcmSession = s_playbackState.pcmSessionId.c_str();
    status.currentBytes = s_currentAudioSize;
    status.queuedPcmBytes = s_pcmQueuedBytes;
    status.queuedPcmSegments = s_pcmQueue.size();
    status.audioQueueDepth = s_audioQueue.size();
    status.micResumeRequested = s_micResumeRequested;
    status.startedMs = s_playbackStartMs;
    status.deadlineMs = s_playbackDeadlineMs;
    return status;
}

// ════════════════════════════════════════
//  再生完了後の次キュー処理
// ════════════════════════════════════════
static void processAudioQueue() {
    if (s_isPlaying) return;

    setMouthOpen(0.0f);

    if (!s_pcmQueue.empty()) {
        PcmBuffer nextPcm = s_pcmQueue.front();
        s_pcmQueue.pop();
        s_pcmQueuedBytes -= nextPcm.size;

        PcmPlaybackResult result = startPcmPlayback(
            nextPcm.data,
            nextPcm.size,
            nextPcm.sessionId,
            nextPcm.finalSegment
        );
        if (result != PCM_PLAYBACK_OK) {
            if (result != PCM_PLAYBACK_SPEAKER_FAILED) {
                free(nextPcm.data);
            }
            Serial.printf("[PCM] Dropped queued segment: result=%d\n", result);
        }
        if (s_isPlaying) {
            return;
        }
    }

    checkPendingPlayback();
    if (s_isPlaying) {
        return;
    }

    if (s_audioQueue.empty()) {
        if (s_playbackState.currentIsPcm && s_playbackState.pcmFinalSegment) {
            Serial.printf("[PCM] Session complete: %s\n", s_playbackState.pcmSessionId.c_str());
        }
        s_playbackState.currentIsPcm = false;
        s_playbackState.pcmSessionId = "";
        s_playbackState.pcmFinalSegment = false;
        setFaceExpression(FACE_IDLE);
        return;
    }

    AudioTask next = s_audioQueue.top();
    s_audioQueue.pop();
    startPlayback(next);
    s_lastPlayedVoiceId = next.voice_id;
}

void enqueueAudioTask(const AudioTask& task) {
    if (s_isPlaying) {
        s_audioQueue.push(task);
        return;
    }
    startPlayback(task);
    s_lastPlayedVoiceId = task.voice_id;
}

static void notifyPlaybackFinished() {
    s_isPlaying = false;
    retireCurrentPlaybackBuffer();
    setMouthOpen(0.0f);
    processAudioQueue();

    if (!s_isPlaying) {
        s_micResumeRequested = true;
    }
}

void updatePlayback() {
    checkPendingPlayback();
    updateLipSync();

    if (s_isPlaying &&
        (millis() - s_playbackStartMs > 1000) &&
        (!M5.Speaker.isPlaying() ||
         (s_playbackDeadlineMs != 0 && millis() > s_playbackDeadlineMs))) {
        if (s_playbackDeadlineMs != 0 && millis() > s_playbackDeadlineMs) {
            Serial.println("[PLAY] Playback timeout -> force stop");
            M5.Speaker.stop();
            clearQueuedPcmPlayback();
        }
        notifyPlaybackFinished();
    }
}

bool isPlaybackActive() {
    return s_isPlaying;
}

bool shouldResumeMic() {
    return s_micResumeRequested && !s_isPlaying;
}

void clearMicResumeRequest() {
    s_micResumeRequested = false;
}
