#include <unity.h>

// Use the real state, persistence and microphone paths with hardware stubs.
#include "../../src/mic_service.cpp"
#include "../../src/recording_store.cpp"
#include "../../src/booth_mode.cpp"

static bool gateAvailable = true;
static bool playbackActive = false;
static bool streamActive = false;
static bool resumeRequested = false;
static unsigned gateAttempts = 0;

bool audioGateEnter(const char*, uint32_t) {
    ++gateAttempts;
    return gateAvailable;
}
void audioGateLeave(const char*) {}
void logAudioMemory(const char*) {}
bool isPlaybackActive() { return playbackActive; }
bool isPcmStreamActive() { return streamActive; }
void setFaceExpression(FaceExpression) {}
void requestMicResume() { resumeRequested = true; }
void clearMicResumeRequest() { resumeRequested = false; }
bool shouldResumeMic() { return resumeRequested && !playbackActive && !streamActive; }

void setUp() {
    clearLastRecording();
    free(record_buffer);
    record_buffer = nullptr;
    M5 = {};
    testNvs = {};
    gateAvailable = true;
    playbackActive = false;
    streamActive = false;
    resumeRequested = false;
    gateAttempts = 0;
    capture_stop_pending = false;
    testMillis += 2000;
    mic_state = MIC_IDLE;
    recorded_samples = 0;
    pre_buf_write = 0;
    pre_buf_full = false;
    memset(capture_frame, 0, sizeof(capture_frame));
    TEST_ASSERT_TRUE(initBoothMode());
}

void tearDown() {
    clearLastRecording();
    free(record_buffer);
    record_buffer = nullptr;
}

static void test_new_device_defaults_to_normal() {
    TEST_ASSERT_FALSE(isBoothMode());
    TEST_ASSERT_TRUE(isBoothModePersisted());
    TEST_ASSERT_TRUE(initMicrophone());
    TEST_ASSERT_TRUE(M5.Mic.running);
}

static void test_enable_stops_and_discards_all_local_audio() {
    TEST_ASSERT_TRUE(initMicrophone());
    TEST_ASSERT_TRUE(requestTouchRecording());
    record_buffer[0] = 123;
    recorded_samples = 1;
    pre_trigger_buf[0] = 456;
    pre_buf_write = 1;
    pre_buf_full = true;
    const uint8_t wav[] = {1, 2};
    storeLastRecording(wav, sizeof(wav), RecordingSource::TOUCH);
    TEST_ASSERT_TRUE(setBoothMode(true));
    TEST_ASSERT_TRUE(isBoothMode());
    TEST_ASSERT_TRUE(isBoothModePersisted());
    TEST_ASSERT_FALSE(M5.Mic.running);
    TEST_ASSERT_FALSE(hasLastRecording());
    TEST_ASSERT_EQUAL(MIC_IDLE, mic_state);
    TEST_ASSERT_EQUAL_UINT(0, recorded_samples);
    TEST_ASSERT_EQUAL_UINT(0, pre_buf_write);
    TEST_ASSERT_FALSE(pre_buf_full);
    TEST_ASSERT_EQUAL_INT16(0, record_buffer[0]);
    TEST_ASSERT_EQUAL_INT16(0, pre_trigger_buf[0]);
}

static void test_booth_blocks_touch_vad_and_direct_initialization() {
    TEST_ASSERT_TRUE(setBoothMode(true));
    M5.Speaker.running = true;
    TEST_ASSERT_FALSE(requestTouchRecording());
    TEST_ASSERT_FALSE(initMicrophone());
    updateMicrophone();
    TEST_ASSERT_EQUAL_UINT(0, M5.Mic.recordCount);
    TEST_ASSERT_EQUAL_UINT(0, M5.Mic.beginCount);
    TEST_ASSERT_TRUE(M5.Speaker.running);
}

static void test_playback_resume_cannot_unmute_booth() {
    TEST_ASSERT_TRUE(setBoothMode(true));
    requestMicResume();
    serviceMicrophoneResume();
    TEST_ASSERT_FALSE(resumeRequested);
    TEST_ASSERT_FALSE(M5.Mic.running);
    TEST_ASSERT_EQUAL_UINT(0, M5.Mic.beginCount);
}

static void test_reboot_restores_booth_before_capture() {
    TEST_ASSERT_TRUE(setBoothMode(true));
    boothMode = false;
    persisted = false;
    M5.Mic.running = true;
    TEST_ASSERT_TRUE(initBoothMode());
    TEST_ASSERT_TRUE(isBoothMode());
    TEST_ASSERT_FALSE(M5.Mic.running);
    TEST_ASSERT_FALSE(initMicrophone());
}

static void test_unreadable_setting_blocks_capture() {
    testNvs.beginFails = true;
    TEST_ASSERT_FALSE(initBoothMode());
    TEST_ASSERT_TRUE(isBoothMode());
    TEST_ASSERT_FALSE(isBoothModePersisted());
    TEST_ASSERT_FALSE(initMicrophone());
}

static void test_corrupt_or_failed_read_does_not_unmute() {
    testNvs.exists = true;
    testNvs.value = 2;
    TEST_ASSERT_FALSE(initBoothMode());
    TEST_ASSERT_TRUE(isBoothMode());
    testNvs.value = 0;
    testNvs.readFails = true;
    TEST_ASSERT_FALSE(initBoothMode());
    TEST_ASSERT_TRUE(isBoothMode());
}

static void test_read_error_is_not_a_missing_setting() {
    testNvs.readFails = true;
    TEST_ASSERT_FALSE(initBoothMode());
    TEST_ASSERT_TRUE(isBoothMode());
    TEST_ASSERT_FALSE(isBoothModePersisted());
    TEST_ASSERT_FALSE(initMicrophone());
}

static void test_wrong_nvs_type_does_not_unmute() {
    testNvs.exists = true;
    testNvs.invalidType = true;
    TEST_ASSERT_FALSE(initBoothMode());
    TEST_ASSERT_TRUE(isBoothMode());
    TEST_ASSERT_FALSE(isBoothModePersisted());
}

static void test_commit_failure_does_not_confirm_or_unmute() {
    TEST_ASSERT_TRUE(initMicrophone());
    testNvs.commitFails = true;
    TEST_ASSERT_FALSE(setBoothMode(true));
    TEST_ASSERT_TRUE(isBoothMode());
    TEST_ASSERT_FALSE(isBoothModePersisted());
    TEST_ASSERT_FALSE(M5.Mic.running);
    TEST_ASSERT_FALSE(setBoothMode(false));
    TEST_ASSERT_TRUE(isBoothMode());
    TEST_ASSERT_FALSE(isBoothModePersisted());
    TEST_ASSERT_FALSE(resumeRequested);
}

static void test_async_capture_frame_is_purged_before_resume() {
    TEST_ASSERT_TRUE(initMicrophone());
    capture_frame[0] = 1234;
    M5.Mic.deferRecordFill = true;
    TEST_ASSERT_TRUE(setBoothMode(true));
    TEST_ASSERT_EQUAL_INT16(0, capture_frame[0]);
    TEST_ASSERT_TRUE(setBoothMode(false));
    serviceMicrophoneResume();
    updateMicrophone();
    TEST_ASSERT_EQUAL_UINT(1, M5.Mic.recordCount);
    TEST_ASSERT_EQUAL_INT16(0, pre_trigger_buf[0]);
}

static void test_capture_frame_purged_when_mic_already_stopped() {
    capture_frame[0] = 1234;
    TEST_ASSERT_FALSE(M5.Mic.running);
    TEST_ASSERT_TRUE(setBoothMode(true));
    TEST_ASSERT_EQUAL_INT16(0, capture_frame[0]);
}

static void test_stopped_flag_does_not_bypass_shutdown_gate() {
    gateAvailable = false;
    capture_frame[0] = 1234;
    TEST_ASSERT_FALSE(M5.Mic.running);
    TEST_ASSERT_FALSE(setBoothMode(true));
    TEST_ASSERT_EQUAL_UINT(1, gateAttempts);
    TEST_ASSERT_FALSE(isMicrophoneCaptureStopped());
    // The other task finishes its final asynchronous write before releasing gate.
    capture_frame[0] = 5678;
    gateAvailable = true;
    serviceMicrophoneResume();
    TEST_ASSERT_TRUE(isMicrophoneCaptureStopped());
    TEST_ASSERT_EQUAL_INT16(0, capture_frame[0]);
}

static void test_failed_stop_is_retried_after_pcm_stops_mic() {
    TEST_ASSERT_TRUE(initMicrophone());
    gateAvailable = false;
    TEST_ASSERT_FALSE(setBoothMode(true));
    M5.Mic.running = false;
    capture_frame[0] = 1234;
    TEST_ASSERT_FALSE(setBoothMode(false));
    TEST_ASSERT_TRUE(isBoothMode());
    TEST_ASSERT_FALSE(isMicrophoneCaptureStopped());
    gateAvailable = true;
    serviceMicrophoneResume();
    TEST_ASSERT_TRUE(isMicrophoneCaptureStopped());
    TEST_ASSERT_EQUAL_INT16(0, capture_frame[0]);
    TEST_ASSERT_TRUE(setBoothMode(false));
    serviceMicrophoneResume();
    TEST_ASSERT_TRUE(M5.Mic.running);
}

static void test_enable_write_failure_is_reported_but_runtime_stays_blocked() {
    TEST_ASSERT_TRUE(initMicrophone());
    testNvs.writeFails = true;
    TEST_ASSERT_FALSE(setBoothMode(true));
    TEST_ASSERT_TRUE(isBoothMode());
    TEST_ASSERT_FALSE(isBoothModePersisted());
    TEST_ASSERT_FALSE(M5.Mic.running);
}

static void test_disable_write_failure_does_not_unmute() {
    TEST_ASSERT_TRUE(setBoothMode(true));
    testNvs.writeFails = true;
    TEST_ASSERT_FALSE(setBoothMode(false));
    TEST_ASSERT_TRUE(isBoothMode());
    TEST_ASSERT_FALSE(isBoothModePersisted());
    TEST_ASSERT_FALSE(M5.Mic.running);
}

static void test_disable_readback_failure_reports_uncertain_persistence() {
    TEST_ASSERT_TRUE(setBoothMode(true));
    testNvs.readFails = true;
    TEST_ASSERT_FALSE(setBoothMode(false));
    TEST_ASSERT_TRUE(isBoothMode());
    TEST_ASSERT_FALSE(isBoothModePersisted());
    TEST_ASSERT_FALSE(M5.Mic.running);
}

static void test_enable_does_not_stop_playing_speaker() {
    TEST_ASSERT_TRUE(initMicrophone());
    playbackActive = true;
    M5.Speaker.running = true;
    M5.Speaker.playing = true;
    TEST_ASSERT_TRUE(setBoothMode(true));
    TEST_ASSERT_TRUE(M5.Speaker.running);
    TEST_ASSERT_TRUE(M5.Speaker.playing);
    TEST_ASSERT_FALSE(M5.Mic.running);
}

static void test_audio_gate_failure_blocks_sampling_then_retries_stop() {
    TEST_ASSERT_TRUE(initMicrophone());
    gateAvailable = false;
    TEST_ASSERT_FALSE(setBoothMode(true));
    TEST_ASSERT_TRUE(isBoothMode());
    TEST_ASSERT_TRUE(M5.Mic.running);
    updateMicrophone();
    TEST_ASSERT_EQUAL_UINT(0, M5.Mic.recordCount);
    gateAvailable = true;
    serviceMicrophoneResume();
    TEST_ASSERT_FALSE(M5.Mic.running);
}

static void test_disable_waits_for_playback_then_restores_capture() {
    TEST_ASSERT_TRUE(setBoothMode(true));
    playbackActive = true;
    M5.Speaker.running = true;
    TEST_ASSERT_TRUE(setBoothMode(false));
    serviceMicrophoneResume();
    TEST_ASSERT_FALSE(M5.Mic.running);
    TEST_ASSERT_TRUE(M5.Speaker.running);
    playbackActive = false;
    serviceMicrophoneResume();
    TEST_ASSERT_TRUE(M5.Mic.running);
    TEST_ASSERT_FALSE(resumeRequested);
    TEST_ASSERT_TRUE(initBoothMode());
    TEST_ASSERT_FALSE(isBoothMode());
}

static void test_repeated_enable_does_not_rewrite_flash() {
    TEST_ASSERT_TRUE(setBoothMode(true));
    const unsigned writes = testNvs.writes;
    TEST_ASSERT_TRUE(setBoothMode(true));
    TEST_ASSERT_EQUAL_UINT(writes, testNvs.writes);
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_new_device_defaults_to_normal);
    RUN_TEST(test_enable_stops_and_discards_all_local_audio);
    RUN_TEST(test_booth_blocks_touch_vad_and_direct_initialization);
    RUN_TEST(test_playback_resume_cannot_unmute_booth);
    RUN_TEST(test_reboot_restores_booth_before_capture);
    RUN_TEST(test_unreadable_setting_blocks_capture);
    RUN_TEST(test_corrupt_or_failed_read_does_not_unmute);
    RUN_TEST(test_read_error_is_not_a_missing_setting);
    RUN_TEST(test_wrong_nvs_type_does_not_unmute);
    RUN_TEST(test_commit_failure_does_not_confirm_or_unmute);
    RUN_TEST(test_async_capture_frame_is_purged_before_resume);
    RUN_TEST(test_capture_frame_purged_when_mic_already_stopped);
    RUN_TEST(test_stopped_flag_does_not_bypass_shutdown_gate);
    RUN_TEST(test_failed_stop_is_retried_after_pcm_stops_mic);
    RUN_TEST(test_enable_write_failure_is_reported_but_runtime_stays_blocked);
    RUN_TEST(test_disable_write_failure_does_not_unmute);
    RUN_TEST(test_disable_readback_failure_reports_uncertain_persistence);
    RUN_TEST(test_enable_does_not_stop_playing_speaker);
    RUN_TEST(test_audio_gate_failure_blocks_sampling_then_retries_stop);
    RUN_TEST(test_disable_waits_for_playback_then_restores_capture);
    RUN_TEST(test_repeated_enable_does_not_rewrite_flash);
    return UNITY_END();
}
