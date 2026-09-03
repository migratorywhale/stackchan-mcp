#include <unity.h>

#include <stdint.h>

#include "touch_gesture.h"

TouchTrajectoryResult sample(TouchTrajectoryDetector& detector, uint8_t front, uint8_t middle, uint8_t back) {
    const uint8_t intensities[3] = {front, middle, back};
    return detector.update(intensities);
}

void test_single_zone_tap_remains_a_click() {
    TouchTrajectoryDetector detector;
    TEST_ASSERT_FALSE(sample(detector, 1, 0, 0).hasSwipe);

    const TouchTrajectoryResult release = sample(detector, 0, 0, 0);
    TEST_ASSERT_FALSE(release.hasSwipe);
    TEST_ASSERT_FALSE(release.suppressClick);
}

void test_front_to_back_stroke_emits_forward_and_suppresses_release_click() {
    TouchTrajectoryDetector detector;
    TEST_ASSERT_FALSE(sample(detector, 2, 0, 0).hasSwipe);

    TouchTrajectoryResult stroke = sample(detector, 0, 2, 0);
    TEST_ASSERT_TRUE(stroke.hasSwipe);
    TEST_ASSERT_EQUAL(static_cast<int>(TouchSwipeDirection::FORWARD), static_cast<int>(stroke.direction));

    stroke = sample(detector, 0, 0, 2);
    TEST_ASSERT_TRUE(stroke.hasSwipe);
    TEST_ASSERT_EQUAL(static_cast<int>(TouchSwipeDirection::FORWARD), static_cast<int>(stroke.direction));
    TEST_ASSERT_TRUE(stroke.suppressClick);
    TEST_ASSERT_TRUE(sample(detector, 0, 0, 0).suppressClick);
}

void test_back_to_front_stroke_emits_backward() {
    TouchTrajectoryDetector detector;
    TEST_ASSERT_FALSE(sample(detector, 0, 0, 2).hasSwipe);

    TouchTrajectoryResult stroke = sample(detector, 0, 2, 0);
    TEST_ASSERT_TRUE(stroke.hasSwipe);
    TEST_ASSERT_EQUAL(static_cast<int>(TouchSwipeDirection::BACKWARD), static_cast<int>(stroke.direction));

    stroke = sample(detector, 2, 0, 0);
    TEST_ASSERT_TRUE(stroke.hasSwipe);
    TEST_ASSERT_EQUAL(static_cast<int>(TouchSwipeDirection::BACKWARD), static_cast<int>(stroke.direction));
}

void test_adjacent_zone_round_trip_triggers_petting() {
    TouchTrajectoryDetector trajectory;
    PettingDetector petting;
    TEST_ASSERT_FALSE(sample(trajectory, 3, 0, 0).hasSwipe);

    TouchTrajectoryResult stroke = sample(trajectory, 3, 3, 0);
    TEST_ASSERT_TRUE(stroke.hasSwipe);
    TEST_ASSERT_EQUAL(static_cast<int>(TouchSwipeDirection::FORWARD), static_cast<int>(stroke.direction));
    TEST_ASSERT_FALSE(petting.observe(stroke.direction, 1000));

    stroke = sample(trajectory, 3, 0, 0);
    TEST_ASSERT_TRUE(stroke.hasSwipe);
    TEST_ASSERT_EQUAL(static_cast<int>(TouchSwipeDirection::BACKWARD), static_cast<int>(stroke.direction));
    TEST_ASSERT_TRUE(petting.observe(stroke.direction, 1100));
}

void test_minor_adjacent_zone_crosstalk_does_not_become_a_swipe() {
    TouchTrajectoryDetector detector;
    TEST_ASSERT_FALSE(sample(detector, 3, 0, 0).hasSwipe);
    TEST_ASSERT_FALSE(sample(detector, 3, 1, 0).hasSwipe);
    TEST_ASSERT_FALSE(sample(detector, 3, 0, 0).hasSwipe);
    TEST_ASSERT_FALSE(sample(detector, 0, 0, 0).suppressClick);
}

void test_reverse_hysteresis_ignores_one_level_wobble() {
    TouchTrajectoryDetector detector;
    TEST_ASSERT_FALSE(sample(detector, 3, 3, 0).hasSwipe);
    TEST_ASSERT_FALSE(sample(detector, 3, 2, 0).hasSwipe);
    TEST_ASSERT_FALSE(sample(detector, 3, 3, 0).hasSwipe);
    TEST_ASSERT_FALSE(sample(detector, 0, 0, 0).suppressClick);
}

void test_continuous_reversal_triggers_petting_without_lifting() {
    TouchTrajectoryDetector trajectory;
    PettingDetector petting;
    TEST_ASSERT_FALSE(sample(trajectory, 2, 0, 0).hasSwipe);

    TouchTrajectoryResult stroke = sample(trajectory, 0, 0, 2);
    TEST_ASSERT_TRUE(stroke.hasSwipe);
    TEST_ASSERT_FALSE(petting.observe(stroke.direction, 1000));

    stroke = sample(trajectory, 2, 0, 0);
    TEST_ASSERT_TRUE(stroke.hasSwipe);
    TEST_ASSERT_TRUE(petting.observe(stroke.direction, 1200));
    TEST_ASSERT_TRUE(sample(trajectory, 0, 0, 0).suppressClick);
}

void test_one_way_adjacent_stroke_suppresses_release_click() {
    TouchTrajectoryDetector detector;
    TEST_ASSERT_FALSE(sample(detector, 2, 0, 0).hasSwipe);
    TEST_ASSERT_TRUE(sample(detector, 0, 2, 0).hasSwipe);
    TEST_ASSERT_TRUE(sample(detector, 0, 0, 0).suppressClick);
}

void test_equal_edge_contact_does_not_invent_a_direction() {
    TouchTrajectoryDetector detector;
    TEST_ASSERT_FALSE(sample(detector, 2, 1, 2).hasSwipe);
    TEST_ASSERT_FALSE(sample(detector, 0, 0, 0).suppressClick);
}

void test_reset_clears_pending_click_suppression() {
    TouchTrajectoryDetector detector;
    sample(detector, 2, 0, 0);
    TEST_ASSERT_TRUE(sample(detector, 0, 0, 2).suppressClick);
    detector.reset();
    TEST_ASSERT_FALSE(sample(detector, 0, 0, 0).suppressClick);
}

void test_one_direction_does_not_trigger_petting() {
    PettingDetector detector;
    TEST_ASSERT_FALSE(detector.observe(TouchSwipeDirection::FORWARD, 1000));
}

void test_opposite_swipes_inside_window_trigger_petting() {
    PettingDetector detector;
    TEST_ASSERT_FALSE(detector.observe(TouchSwipeDirection::FORWARD, 1000));
    TEST_ASSERT_TRUE(detector.observe(TouchSwipeDirection::BACKWARD, 2200));
}

void test_same_direction_does_not_trigger_petting() {
    PettingDetector detector;
    TEST_ASSERT_FALSE(detector.observe(TouchSwipeDirection::FORWARD, 1000));
    TEST_ASSERT_FALSE(detector.observe(TouchSwipeDirection::FORWARD, 1200));
}

void test_latest_same_direction_swipe_starts_a_new_pair_window() {
    PettingDetector detector;
    TEST_ASSERT_FALSE(detector.observe(TouchSwipeDirection::FORWARD, 1000));
    TEST_ASSERT_FALSE(detector.observe(TouchSwipeDirection::FORWARD, 2000));
    TEST_ASSERT_TRUE(detector.observe(TouchSwipeDirection::BACKWARD, 3400));
}

void test_stale_opposite_swipe_does_not_trigger_petting() {
    PettingDetector detector;
    TEST_ASSERT_FALSE(detector.observe(TouchSwipeDirection::BACKWARD, 1000));
    TEST_ASSERT_FALSE(detector.observe(TouchSwipeDirection::FORWARD, 2600));
}

void test_detector_resets_after_a_completed_pair() {
    PettingDetector detector;
    TEST_ASSERT_FALSE(detector.observe(TouchSwipeDirection::FORWARD, 1000));
    TEST_ASSERT_TRUE(detector.observe(TouchSwipeDirection::BACKWARD, 1100));
    TEST_ASSERT_FALSE(detector.observe(TouchSwipeDirection::FORWARD, 1200));
}

void test_window_handles_millis_wraparound() {
    PettingDetector detector;
    TEST_ASSERT_FALSE(detector.observe(TouchSwipeDirection::BACKWARD, UINT32_MAX - 500));
    TEST_ASSERT_TRUE(detector.observe(TouchSwipeDirection::FORWARD, 300));
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_single_zone_tap_remains_a_click);
    RUN_TEST(test_front_to_back_stroke_emits_forward_and_suppresses_release_click);
    RUN_TEST(test_back_to_front_stroke_emits_backward);
    RUN_TEST(test_adjacent_zone_round_trip_triggers_petting);
    RUN_TEST(test_minor_adjacent_zone_crosstalk_does_not_become_a_swipe);
    RUN_TEST(test_reverse_hysteresis_ignores_one_level_wobble);
    RUN_TEST(test_continuous_reversal_triggers_petting_without_lifting);
    RUN_TEST(test_one_way_adjacent_stroke_suppresses_release_click);
    RUN_TEST(test_equal_edge_contact_does_not_invent_a_direction);
    RUN_TEST(test_reset_clears_pending_click_suppression);
    RUN_TEST(test_one_direction_does_not_trigger_petting);
    RUN_TEST(test_opposite_swipes_inside_window_trigger_petting);
    RUN_TEST(test_same_direction_does_not_trigger_petting);
    RUN_TEST(test_latest_same_direction_swipe_starts_a_new_pair_window);
    RUN_TEST(test_stale_opposite_swipe_does_not_trigger_petting);
    RUN_TEST(test_detector_resets_after_a_completed_pair);
    RUN_TEST(test_window_handles_millis_wraparound);
    return UNITY_END();
}
