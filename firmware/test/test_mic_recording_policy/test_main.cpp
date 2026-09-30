#include <unity.h>

#include "mic_recording_policy.h"

void test_idle_accepts_touch_regardless_of_previous_source() {
    TEST_ASSERT_TRUE(canReplaceWithTouch(MIC_IDLE, RecordingSource::VOICE));
    TEST_ASSERT_TRUE(canReplaceWithTouch(MIC_IDLE, RecordingSource::TOUCH));
    TEST_ASSERT_TRUE(canReplaceWithTouch(MIC_IDLE, RecordingSource::NONE));
}

void test_touch_replaces_ambient_trigger() {
    TEST_ASSERT_TRUE(canReplaceWithTouch(MIC_TRIGGERING, RecordingSource::VOICE));
    TEST_ASSERT_TRUE(canReplaceWithTouch(MIC_TRIGGERING, RecordingSource::TOUCH));
}

void test_touch_takes_over_ambient_recording() {
    TEST_ASSERT_TRUE(canReplaceWithTouch(MIC_RECORDING, RecordingSource::VOICE));
}

void test_second_touch_does_not_truncate_touch_recording() {
    TEST_ASSERT_FALSE(canReplaceWithTouch(MIC_RECORDING, RecordingSource::TOUCH));
}

void test_unknown_recording_source_is_not_replaced() {
    TEST_ASSERT_FALSE(canReplaceWithTouch(MIC_RECORDING, RecordingSource::NONE));
}

void test_sending_is_not_interrupted() {
    TEST_ASSERT_FALSE(canReplaceWithTouch(MIC_SENDING, RecordingSource::VOICE));
    TEST_ASSERT_FALSE(canReplaceWithTouch(MIC_SENDING, RecordingSource::TOUCH));
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_idle_accepts_touch_regardless_of_previous_source);
    RUN_TEST(test_touch_replaces_ambient_trigger);
    RUN_TEST(test_touch_takes_over_ambient_recording);
    RUN_TEST(test_second_touch_does_not_truncate_touch_recording);
    RUN_TEST(test_unknown_recording_source_is_not_replaced);
    RUN_TEST(test_sending_is_not_interrupted);
    return UNITY_END();
}
