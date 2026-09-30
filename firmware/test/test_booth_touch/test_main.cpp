#include <unity.h>

#include "../../src/touch_service.cpp"

static bool booth = false;
static bool playing = false;
static unsigned recordingRequests = 0;
static unsigned faceCommands = 0;
static unsigned shakes = 0;
static WhaleFace currentFace = WHALE_CALM;

bool isBoothMode() { return booth; }
bool isPlaybackActive() { return playing; }
bool isPcmStreamActive() { return false; }
const char* getMicStateName() { return "idle"; }
bool requestTouchRecording() { ++recordingRequests; return true; }
const char* getCurrentFaceName() { return whaleFaceName(currentFace); }
uint32_t getFaceCommandRevision() { return faceCommands; }
void setWhaleFace(WhaleFace face) { currentFace = face; ++faceCommands; }
bool isServoReady() { return true; }
bool servoShake() { ++shakes; return true; }

void setUp() {
    status = {};
    pettingOwnsFace = false;
    M5StackChan = {};
    booth = false;
    playing = false;
    recordingRequests = 0;
    faceCommands = 0;
    shakes = 0;
    currentFace = WHALE_CALM;
    testMillis += 10000;
    TEST_ASSERT_TRUE(initTouchService());
}
void tearDown() {}

static void test_normal_click_still_requests_recording() {
    M5StackChan.TouchSensor.clicked = true;
    updateTouchService();
    TEST_ASSERT_EQUAL_UINT(1, recordingRequests);
    TEST_ASSERT_EQUAL_UINT(1, status.recordRequestCount);
    TEST_ASSERT_EQUAL_UINT(0, faceCommands);
}

static void test_booth_click_only_animates_locally() {
    booth = true;
    M5StackChan.TouchSensor.clicked = true;
    updateTouchService();
    TEST_ASSERT_EQUAL_UINT(0, recordingRequests);
    TEST_ASSERT_EQUAL_UINT(0, status.recordRequestCount);
    TEST_ASSERT_EQUAL_UINT(0, status.petCount);
    TEST_ASSERT_EQUAL_UINT(1, faceCommands);
    TEST_ASSERT_EQUAL_UINT(1, shakes);
    TEST_ASSERT_TRUE(currentFace == WHALE_HAPPY);
}

static void test_booth_hold_never_requests_recording() {
    booth = true;
    M5StackChan.TouchSensor.hold = true;
    updateTouchService();
    TEST_ASSERT_EQUAL_UINT(0, recordingRequests);
    TEST_ASSERT_EQUAL_UINT(0, status.petCount);
}

static void test_booth_feedback_does_not_interrupt_speech() {
    booth = true;
    playing = true;
    M5StackChan.TouchSensor.clicked = true;
    updateTouchService();
    TEST_ASSERT_EQUAL_UINT(0, faceCommands);
    TEST_ASSERT_EQUAL_UINT(0, shakes);
    TEST_ASSERT_EQUAL_UINT(0, recordingRequests);
    TEST_ASSERT_EQUAL_UINT(0, status.petCount);
}

static void test_booth_swipe_does_not_emit_host_pet_notification() {
    booth = true;
    handleSwipe(TouchSwipeDirection::FORWARD, testMillis);
    handleSwipe(TouchSwipeDirection::BACKWARD, testMillis + 200);
    TEST_ASSERT_EQUAL_UINT(0, status.petCount);
    TEST_ASSERT_EQUAL_UINT(1, faceCommands);
    TEST_ASSERT_EQUAL_UINT(0, recordingRequests);
}

static void test_normal_swipe_still_notifies_host() {
    handleSwipe(TouchSwipeDirection::FORWARD, testMillis);
    handleSwipe(TouchSwipeDirection::BACKWARD, testMillis + 200);
    TEST_ASSERT_EQUAL_UINT(1, status.petCount);
    TEST_ASSERT_EQUAL_UINT(1, faceCommands);
}

static void test_feedback_does_not_restore_over_newer_face() {
    booth = true;
    M5StackChan.TouchSensor.clicked = true;
    updateTouchService();
    M5StackChan.TouchSensor.clicked = false;
    setWhaleFace(WHALE_CALM);
    const auto commands = faceCommands;
    testMillis += PETTING_DURATION_MS + 1;
    updateTouchService();
    TEST_ASSERT_EQUAL_UINT(commands, faceCommands);
    TEST_ASSERT_TRUE(currentFace == WHALE_CALM);
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_normal_click_still_requests_recording);
    RUN_TEST(test_booth_click_only_animates_locally);
    RUN_TEST(test_booth_hold_never_requests_recording);
    RUN_TEST(test_booth_feedback_does_not_interrupt_speech);
    RUN_TEST(test_booth_swipe_does_not_emit_host_pet_notification);
    RUN_TEST(test_normal_swipe_still_notifies_host);
    RUN_TEST(test_feedback_does_not_restore_over_newer_face);
    return UNITY_END();
}
