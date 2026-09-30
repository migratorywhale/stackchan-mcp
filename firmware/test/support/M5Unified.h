#pragma once
#include "Arduino.h"

struct TestMic {
    bool running = false;
    bool isRunning() const { return running; }
    void end() { running = false; }
};
struct TestSpeaker {
    bool running = false;
    bool playing = false;
    bool fail = false;
    bool isRunning() const { return running; }
    bool isPlaying() const { return playing; }
    bool begin() { running = !fail; return running; }
    void end() { running = false; playing = false; }
    void stop() { playing = false; }
    void setVolume(int) {}
    bool playRaw(const int16_t*, size_t, uint32_t, bool, int, int, bool) {
        playing = !fail;
        return playing;
    }
};
struct TestM5 { TestMic Mic; TestSpeaker Speaker; };
inline TestM5 M5;
