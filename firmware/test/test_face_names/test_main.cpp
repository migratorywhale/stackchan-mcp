#include <unity.h>

#include <string.h>

#include "face_names.h"

void test_whale_face_name_returns_stable_api_names() {
    TEST_ASSERT_EQUAL_STRING("calm", whaleFaceName(WHALE_CALM));
    TEST_ASSERT_EQUAL_STRING("thinking", whaleFaceName(WHALE_THINKING));
    TEST_ASSERT_EQUAL_STRING("happy", whaleFaceName(WHALE_HAPPY));
    TEST_ASSERT_EQUAL_STRING("sleepy", whaleFaceName(WHALE_SLEEPY));
    TEST_ASSERT_EQUAL_STRING("shy", whaleFaceName(WHALE_SHY));
    TEST_ASSERT_EQUAL_STRING("smug", whaleFaceName(WHALE_SMUG));
    TEST_ASSERT_EQUAL_STRING("pouty", whaleFaceName(WHALE_POUTY));
}

void test_whale_face_name_returns_unknown_for_invalid_value() {
    TEST_ASSERT_EQUAL_STRING("unknown", whaleFaceName((WhaleFace)99));
}

void test_whale_face_from_name_parses_valid_names() {
    WhaleFace face = WHALE_CALM;

    TEST_ASSERT_TRUE(whaleFaceFromName("happy", &face));
    TEST_ASSERT_EQUAL(WHALE_HAPPY, face);

    TEST_ASSERT_TRUE(whaleFaceFromName("pouty", &face));
    TEST_ASSERT_EQUAL(WHALE_POUTY, face);
}

void test_whale_face_from_name_rejects_invalid_input() {
    WhaleFace face = WHALE_SHY;

    TEST_ASSERT_FALSE(whaleFaceFromName("missing", &face));
    TEST_ASSERT_EQUAL(WHALE_SHY, face);
    TEST_ASSERT_FALSE(whaleFaceFromName(nullptr, &face));
    TEST_ASSERT_FALSE(whaleFaceFromName("happy", nullptr));
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_whale_face_name_returns_stable_api_names);
    RUN_TEST(test_whale_face_name_returns_unknown_for_invalid_value);
    RUN_TEST(test_whale_face_from_name_parses_valid_names);
    RUN_TEST(test_whale_face_from_name_rejects_invalid_input);
    return UNITY_END();
}

