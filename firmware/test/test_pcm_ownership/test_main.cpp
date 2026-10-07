#include <unity.h>

// Exercise the real playback implementation with hardware/network stubs. Its
// file-local queues are visible here for ownership and failure-path assertions.
#include "../../src/playback_service.cpp"

static bool cameraActive = false;
static int streamChecks = 0;
static int streamBecomesActiveAt = 0;
static uint32_t currentOutboundGeneration = 0;

bool isOutboundGenerationCurrent(uint32_t generation) { return generation == currentOutboundGeneration; }

bool isCameraSessionActive() { return cameraActive; }
bool isPcmStreamActive() {
    ++streamChecks;
    return streamBecomesActiveAt && streamChecks >= streamBecomesActiveAt;
}
bool audioGateEnter(const char*, uint32_t) { return true; }
void audioGateLeave(const char*) {}
void logAudioMemory(const char*) {}
void setFaceExpression(FaceExpression) {}
void setMouthOpen(float) {}
bool downloadVoice(const String&, uint8_t**, size_t*) { return false; }

static uint8_t* pcm() {
    uint8_t* data = static_cast<uint8_t*>(malloc(2));
    data[0] = 0;
    data[1] = 0;
    return data;
}

void setUp() {
    releaseCurrentPlaybackBuffer();
    clearQueuedPcmPlayback();
    s_playbackState = {};
    s_isPlaying = false;
    s_audioQueue = {};
    s_downloadInFlight = false;
    s_downloadCompleteQueue = nullptr;
    M5 = {};
    cameraActive = false;
    streamChecks = 0;
    streamBecomesActiveAt = 0;
    currentOutboundGeneration = 0;
    testDelayHook = nullptr;
}

void tearDown() {
    releaseCurrentPlaybackBuffer();
    clearQueuedPcmPlayback();
}

static void test_cleanup_preserves_direct_queue_with_same_session() {
    enqueuePcmBuffer(pcm(), 2, "same", false, PcmOrigin::DIRECT);
    enqueuePcmBuffer(pcm(), 2, "same", false, PcmOrigin::OUTBOUND);
    enqueuePcmBuffer(pcm(), 2, "other", false, PcmOrigin::OUTBOUND);
    clearQueuedPcmSession("same", PcmOrigin::OUTBOUND);
    TEST_ASSERT_EQUAL_UINT(2, s_pcmQueue.size());
    TEST_ASSERT_EQUAL_UINT(4, s_pcmQueuedBytes);
    TEST_ASSERT_TRUE(s_pcmQueue.front().origin == PcmOrigin::DIRECT);
}

static void test_cleanup_preserves_direct_staging() {
    TEST_ASSERT_EQUAL(PCM_PLAYBACK_QUEUED, stagePcmPlayback(pcm(), 2, "same", 0, false));
    clearQueuedPcmSession("same", PcmOrigin::OUTBOUND);
    TEST_ASSERT_EQUAL_UINT(2, s_stagedPcmSize);
    TEST_ASSERT_NOT_NULL(s_stagedPcmData);
}

static void test_outbound_staging_cannot_replace_direct_staging() {
    stagePcmPlayback(pcm(), 2, "same", 0, false, PcmOrigin::DIRECT);
    uint8_t* input = pcm();
    TEST_ASSERT_EQUAL(PCM_PLAYBACK_BUSY, stagePcmPlayback(input, 2, "same", 0, false, PcmOrigin::OUTBOUND));
    free(input);
    TEST_ASSERT_TRUE(s_stagedPcmOrigin == PcmOrigin::DIRECT);
    TEST_ASSERT_EQUAL_UINT(2, s_stagedPcmSize);
}

static void test_cleanup_discards_only_queued_outbound_audio() {
    TEST_ASSERT_EQUAL(PCM_PLAYBACK_OK, startPcmPlayback(pcm(), 2, "same", false, PcmOrigin::OUTBOUND));
    TEST_ASSERT_EQUAL(PCM_PLAYBACK_QUEUED, startPcmPlayback(pcm(), 2, "same", true, PcmOrigin::OUTBOUND));
    uint8_t* active = s_currentAudioData;
    clearQueuedPcmSession("same", PcmOrigin::OUTBOUND);
    TEST_ASSERT_TRUE(s_isPlaying);
    TEST_ASSERT_EQUAL_PTR(active, s_currentAudioData);
    TEST_ASSERT_EQUAL_UINT(0, s_pcmQueue.size());
}

static void test_staged_failure_after_consumption_has_consumed_result() {
    streamBecomesActiveAt = 2;
    TEST_ASSERT_EQUAL(PCM_PLAYBACK_SPEAKER_FAILED,
        stagePcmPlayback(pcm(), 2, "outbound", 0, true, PcmOrigin::OUTBOUND));
    TEST_ASSERT_NULL(s_stagedPcmData);
    TEST_ASSERT_NULL(s_currentAudioData);
    TEST_ASSERT_FALSE(s_isPlaying);
}

static void test_camera_busy_keeps_input_caller_owned() {
    cameraActive = true;
    uint8_t* input = pcm();
    TEST_ASSERT_EQUAL(PCM_PLAYBACK_BUSY, stagePcmPlayback(input, 2, "outbound", 0, true, PcmOrigin::OUTBOUND));
    free(input);
    TEST_ASSERT_NULL(s_stagedPcmData);
}

static void test_advancing_pcm_keeps_later_segments() {
    enqueuePcmBuffer(pcm(), 2, "same", false, PcmOrigin::DIRECT);
    enqueuePcmBuffer(pcm(), 2, "same", true, PcmOrigin::DIRECT);
    processAudioQueue();
    TEST_ASSERT_TRUE(s_isPlaying);
    TEST_ASSERT_EQUAL_UINT(1, s_pcmQueue.size());
    TEST_ASSERT_EQUAL_UINT(2, s_pcmQueuedBytes);
}

static void test_same_session_different_owner_cannot_join_playback() {
    TEST_ASSERT_EQUAL(PCM_PLAYBACK_OK, startPcmPlayback(pcm(), 2, "same", false));
    uint8_t* input = pcm();
    TEST_ASSERT_EQUAL(PCM_PLAYBACK_BUSY, startPcmPlayback(input, 2, "same", true, PcmOrigin::OUTBOUND));
    free(input);
    TEST_ASSERT_EQUAL_UINT(0, s_pcmQueue.size());
}

static void invalidateDuringWait(uint32_t) { ++currentOutboundGeneration; }

static void test_disconnect_during_completion_wait_drops_queued_pcm() {
    enqueuePcmBuffer(pcm(), 2, "queued", true, PcmOrigin::OUTBOUND, 0);
    M5.Speaker.running = true;
    s_isPlaying = true;
    testDelayHook = invalidateDuringWait;
    TEST_ASSERT_TRUE(notifyPlaybackFinished());
    TEST_ASSERT_FALSE(s_isPlaying);
    TEST_ASSERT_FALSE(M5.Speaker.playing);
    TEST_ASSERT_EQUAL_UINT(0, s_pcmQueue.size());
}

static void test_disconnect_during_speaker_setup_does_not_start_audio() {
    M5.Mic.running = true;
    testDelayHook = invalidateDuringWait;
    TEST_ASSERT_EQUAL(PCM_PLAYBACK_SPEAKER_FAILED,
        startPcmPlayback(pcm(), 2, "outbound", true, PcmOrigin::OUTBOUND, 0));
    TEST_ASSERT_FALSE(M5.Speaker.playing);
    TEST_ASSERT_FALSE(s_isPlaying);
    TEST_ASSERT_NULL(s_currentAudioData);
    TEST_ASSERT_TRUE(s_micResumeRequested);
}

static void test_cleanup_covers_staging_from_an_earlier_session() {
    stagePcmPlayback(pcm(), 2, "S1", 0, false, PcmOrigin::OUTBOUND, 0);
    TEST_ASSERT_EQUAL(PCM_PLAYBACK_OK,
        startPcmPlayback(pcm(), 2, "S2", true, PcmOrigin::OUTBOUND, 0));
    TEST_ASSERT_NOT_NULL(s_stagedPcmData);
    ++currentOutboundGeneration;
    clearStaleOutboundPcm();
    TEST_ASSERT_NULL(s_stagedPcmData);
    TEST_ASSERT_EQUAL_UINT(0, s_stagedPcmSize);
    notifyPlaybackFinished();
    TEST_ASSERT_EQUAL(PCM_PLAYBACK_QUEUED, stagePcmPlayback(pcm(), 2, "direct", 0, false));
}

static void test_stale_queue_cleanup_preserves_current_generation_and_direct() {
    enqueuePcmBuffer(pcm(), 2, "same", false, PcmOrigin::OUTBOUND, 0);
    enqueuePcmBuffer(pcm(), 2, "same", false, PcmOrigin::OUTBOUND, 1);
    enqueuePcmBuffer(pcm(), 2, "same", false, PcmOrigin::DIRECT, 0);
    currentOutboundGeneration = 1;
    clearStaleOutboundPcm();
    TEST_ASSERT_EQUAL_UINT(2, s_pcmQueue.size());
    TEST_ASSERT_EQUAL_UINT(4, s_pcmQueuedBytes);
    TEST_ASSERT_EQUAL_UINT(1, s_pcmQueue.front().generation);
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_cleanup_preserves_direct_queue_with_same_session);
    RUN_TEST(test_cleanup_preserves_direct_staging);
    RUN_TEST(test_outbound_staging_cannot_replace_direct_staging);
    RUN_TEST(test_cleanup_discards_only_queued_outbound_audio);
    RUN_TEST(test_staged_failure_after_consumption_has_consumed_result);
    RUN_TEST(test_camera_busy_keeps_input_caller_owned);
    RUN_TEST(test_advancing_pcm_keeps_later_segments);
    RUN_TEST(test_same_session_different_owner_cannot_join_playback);
    RUN_TEST(test_disconnect_during_completion_wait_drops_queued_pcm);
    RUN_TEST(test_disconnect_during_speaker_setup_does_not_start_audio);
    RUN_TEST(test_cleanup_covers_staging_from_an_earlier_session);
    RUN_TEST(test_stale_queue_cleanup_preserves_current_generation_and_direct);
    return UNITY_END();
}
