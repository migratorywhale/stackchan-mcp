#pragma once

#include <Arduino.h>

#define PCM_UDP_FRAME_MS     10
#define PCM_UDP_START_FRAMES 12

struct PcmStreamStatus {
    bool enabled = false;
    bool active = false;
    bool playing = false;
    bool clientConnected = false;
    const char* session = "";
    uint16_t port = 9090;
    size_t bufferedBytes = 0;
    size_t totalBytes = 0;
    uint32_t underruns = 0;
    bool udpEnabled = false;
    bool udpActive = false;
    bool udpPlaying = false;
    const char* udpSession = "";
    uint16_t udpPort = 9091;
    uint32_t udpToken = 0;
    size_t udpBufferedFrames = 0;
    size_t udpBufferedBytes = 0;
    uint32_t udpFramesReceived = 0;
    uint32_t udpFramesLost = 0;
    uint32_t udpFramesLate = 0;
    uint32_t udpUnderruns = 0;
    uint32_t udpFirstAudioMs = 0;
    const char* udpLastEndReason = "";
    uint32_t udpLastFramesReceived = 0;
    uint32_t udpLastFramesLost = 0;
    uint32_t udpLastFramesLate = 0;
    uint32_t udpLastUnderruns = 0;
    uint32_t udpLastFirstAudioMs = 0;
};

struct UdpPcmSessionResult {
    bool success = false;
    String session = "";
    uint32_t token = 0;
    uint16_t port = 9091;
    const char* error = "";
};

void initPcmStreamService();
bool isPcmStreamActive();
UdpPcmSessionResult beginUdpPcmSession();
bool stopUdpPcmSession(const String& sessionId);
PcmStreamStatus getPcmStreamStatus();
