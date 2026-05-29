#include <unity.h>

#include <stdint.h>
#include <string.h>

#include "wav_parser.h"

static void writeLe16(uint8_t* p, uint16_t value) {
    p[0] = value & 0xff;
    p[1] = (value >> 8) & 0xff;
}

static void writeLe32(uint8_t* p, uint32_t value) {
    p[0] = value & 0xff;
    p[1] = (value >> 8) & 0xff;
    p[2] = (value >> 16) & 0xff;
    p[3] = (value >> 24) & 0xff;
}

static void makeValidWav(uint8_t* wav, uint32_t sampleRate = 24000,
                         uint16_t channels = 1, uint16_t bitsPerSample = 16,
                         uint32_t dataSize = 4) {
    memset(wav, 0, 48);
    memcpy(wav, "RIFF", 4);
    writeLe32(wav + 4, 40);
    memcpy(wav + 8, "WAVE", 4);
    memcpy(wav + 12, "fmt ", 4);
    writeLe32(wav + 16, 16);
    writeLe16(wav + 20, 1);
    writeLe16(wav + 22, channels);
    writeLe32(wav + 24, sampleRate);
    writeLe32(wav + 28, sampleRate * channels * bitsPerSample / 8);
    writeLe16(wav + 32, channels * bitsPerSample / 8);
    writeLe16(wav + 34, bitsPerSample);
    memcpy(wav + 36, "data", 4);
    writeLe32(wav + 40, dataSize);
}

void test_parse_valid_24khz_mono_s16le_wav() {
    uint8_t wav[48];
    makeValidWav(wav);

    WavInfo info;
    TEST_ASSERT_TRUE(parseWavInfo(wav, sizeof(wav), &info));
    TEST_ASSERT_EQUAL_UINT32(24000, info.sampleRate);
    TEST_ASSERT_EQUAL_UINT16(1, info.channels);
    TEST_ASSERT_EQUAL_UINT16(16, info.bitsPerSample);
    TEST_ASSERT_EQUAL_UINT32(44, info.dataOffset);
    TEST_ASSERT_EQUAL_UINT32(4, info.dataSize);
}

void test_rejects_missing_riff_header() {
    uint8_t wav[48];
    makeValidWav(wav);
    memcpy(wav, "NOPE", 4);

    WavInfo info;
    TEST_ASSERT_FALSE(parseWavInfo(wav, sizeof(wav), &info));
}

void test_rejects_unsupported_sample_rate() {
    uint8_t wav[48];
    makeValidWav(wav, 16000);

    WavInfo info;
    TEST_ASSERT_FALSE(parseWavInfo(wav, sizeof(wav), &info));
}

void test_rejects_stereo_audio() {
    uint8_t wav[48];
    makeValidWav(wav, 24000, 2);

    WavInfo info;
    TEST_ASSERT_FALSE(parseWavInfo(wav, sizeof(wav), &info));
}

void test_rejects_empty_data_chunk() {
    uint8_t wav[48];
    makeValidWav(wav, 24000, 1, 16, 0);

    WavInfo info;
    TEST_ASSERT_FALSE(parseWavInfo(wav, sizeof(wav), &info));
}

void test_rejects_chunk_overflow() {
    uint8_t wav[48];
    makeValidWav(wav);
    writeLe32(wav + 40, 100);

    WavInfo info;
    TEST_ASSERT_FALSE(parseWavInfo(wav, sizeof(wav), &info));
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_parse_valid_24khz_mono_s16le_wav);
    RUN_TEST(test_rejects_missing_riff_header);
    RUN_TEST(test_rejects_unsupported_sample_rate);
    RUN_TEST(test_rejects_stereo_audio);
    RUN_TEST(test_rejects_empty_data_chunk);
    RUN_TEST(test_rejects_chunk_overflow);
    return UNITY_END();
}

