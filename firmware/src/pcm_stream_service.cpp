#include "pcm_stream_service.h"

#include <M5Unified.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <driver/i2s.h>
#include <queue>

#include "audio_gate.h"
#include "config_loader.h"
#include "face_service.h"
#include "playback_service.h"

#define PCM_STREAM_PORT              9090
#define PCM_UDP_PORT                 9091
#define PCM_STREAM_SAMPLE_RATE       24000
#define PCM_STREAM_BYTES_PER_SAMPLE  2
// PCM_UDP_FRAME_MS / PCM_UDP_START_FRAMES live in pcm_stream_service.h so
// http_server.cpp can advertise the real preroll in /audio/session.
#define PCM_UDP_FRAME_SAMPLES        ((PCM_STREAM_SAMPLE_RATE * PCM_UDP_FRAME_MS) / 1000)
#define PCM_UDP_FRAME_BYTES          (PCM_UDP_FRAME_SAMPLES * PCM_STREAM_BYTES_PER_SAMPLE)
#define PCM_UDP_SLAB_FRAMES          1
#define PCM_UDP_RING_FRAMES          512
#define PCM_UDP_FADE_SAMPLES         64
#define PCM_UDP_HEADER_BYTES         24
#define PCM_UDP_PACKET_MAX           (PCM_UDP_HEADER_BYTES + PCM_UDP_FRAME_BYTES)
#define PCM_UDP_VERSION              1
#define PCM_UDP_FLAG_END             0x01
#define PCM_UDP_ACTIVE_TIMEOUT_MS    30000
#define PCM_UDP_PREROLL_TIMEOUT_MS   5000
#define PCM_UDP_IDLE_END_GRACE_MS    500
#define PCM_STREAM_SLAB_BYTES        PCM_UDP_FRAME_BYTES
#define PCM_STREAM_PREBUFFER_BYTES   (PCM_UDP_FRAME_BYTES * 12)
#define PCM_STREAM_MAX_BUFFERED      (512 * 1024)
#define PCM_STREAM_MAX_TOTAL         (2 * 1024 * 1024)
#define PCM_STREAM_CHANNEL           0
#define PCM_STREAM_HEADER_MAX        160
#define PCM_STREAM_HEADER_TIMEOUT_MS 3000
#define PCM_STREAM_READ_TIMEOUT_MS   30000

struct StreamSlab {
    uint8_t* data = nullptr;
    size_t size = 0;
};

static WiFiServer s_streamServer(PCM_STREAM_PORT);
static WiFiUDP s_udpServer;
static TaskHandle_t s_streamTask = nullptr;
static bool s_udpI2sRunning = false;
static volatile bool s_active = false;
static volatile bool s_playing = false;
static volatile bool s_clientConnected = false;
static String s_session = "";
static size_t s_bufferedBytes = 0;
static size_t s_totalBytes = 0;
static uint32_t s_underruns = 0;

struct UdpFrameSlot {
    bool valid = false;
    uint32_t seq = 0;
    uint8_t data[PCM_UDP_FRAME_BYTES];
};

static UdpFrameSlot* s_udpRing = nullptr;
static std::queue<StreamSlab> s_udpSubmitted;
static volatile bool s_udpActive = false;
static volatile bool s_udpPlaying = false;
static bool s_udpEnding = false;
static bool s_udpReceivedAny = false;
static String s_udpSession = "";
static uint32_t s_udpToken = 0;
static uint32_t s_udpFirstSeq = 0;
static uint32_t s_udpNextPlaySeq = 0;
static uint32_t s_udpHighestSeq = 0;
static uint32_t s_udpEndSeq = 0;
static uint32_t s_udpFramesReceived = 0;
static uint32_t s_udpFramesLost = 0;
static uint32_t s_udpFramesLate = 0;
static uint32_t s_udpUnderruns = 0;
static uint32_t s_udpFirstAudioMs = 0;
// Last mono sample actually written to I2S (for click-free concealment ramps).
static int16_t s_udpLastSample = 0;
// True when the previous frame written was concealed (or this is session
// start) so the next real frame should fade in instead of jumping.
static bool s_udpConcealedPrev = true;
static unsigned long s_udpStartedMs = 0;
static unsigned long s_udpLastPacketMs = 0;
static String s_udpLastEndReason = "";
static uint32_t s_udpLastFramesReceived = 0;
static uint32_t s_udpLastFramesLost = 0;
static uint32_t s_udpLastFramesLate = 0;
static uint32_t s_udpLastUnderruns = 0;
static uint32_t s_udpLastFirstAudioMs = 0;

static constexpr uint8_t CORE_S3_AW88298_I2C_ADDR = 0x36;
static constexpr uint8_t CORE_S3_AW9523_I2C_ADDR = 0x58;
static constexpr gpio_num_t CORE_S3_SPK_BCLK = GPIO_NUM_34;
static constexpr gpio_num_t CORE_S3_SPK_WS = GPIO_NUM_33;
static constexpr gpio_num_t CORE_S3_SPK_DOUT = GPIO_NUM_13;
static constexpr i2s_port_t CORE_S3_SPK_I2S_PORT = I2S_NUM_1;
static constexpr uint32_t PCM_I2S_SAMPLE_RATE = PCM_STREAM_SAMPLE_RATE;

static void handleUdpPackets();

static uint16_t readLe16(const uint8_t* data) {
    return (uint16_t)data[0] | ((uint16_t)data[1] << 8);
}

static uint32_t readLe32(const uint8_t* data) {
    return (uint32_t)data[0] |
           ((uint32_t)data[1] << 8) |
           ((uint32_t)data[2] << 16) |
           ((uint32_t)data[3] << 24);
}

static void freeSlab(StreamSlab slab) {
    if (slab.data) {
        free(slab.data);
    }
}

static void freeQueue(std::queue<StreamSlab>& queue) {
    while (!queue.empty()) {
        freeSlab(queue.front());
        queue.pop();
    }
}

static size_t udpBufferedFrames() {
    if (!s_udpRing) {
        return 0;
    }
    size_t count = 0;
    for (size_t i = 0; i < PCM_UDP_RING_FRAMES; ++i) {
        if (s_udpRing[i].valid) {
            count++;
        }
    }
    return count;
}

static void clearUdpRing() {
    if (!s_udpRing) {
        return;
    }
    for (size_t i = 0; i < PCM_UDP_RING_FRAMES; ++i) {
        s_udpRing[i].valid = false;
        s_udpRing[i].seq = 0;
    }
}

static void clearUdpSubmitted() {
    freeQueue(s_udpSubmitted);
}

static void aw88298WriteReg(uint8_t reg, uint16_t value) {
    M5.In_I2C.writeRegister(CORE_S3_AW88298_I2C_ADDR, reg, (const uint8_t*)&value, 2, 400000);
}

static void setCoreS3AmpEnabled(bool enabled) {
    if (enabled) {
        M5.In_I2C.bitOn(CORE_S3_AW9523_I2C_ADDR, 0x02, 0b00000100, 400000);
        static constexpr uint8_t rateTable[] = {4, 5, 6, 8, 10, 11, 15, 20, 22, 44};
        size_t reg0x06Value = 0;
        size_t rate = (PCM_I2S_SAMPLE_RATE + 1102) / 2205;
        while (rate > rateTable[reg0x06Value] && ++reg0x06Value < sizeof(rateTable)) {}
        reg0x06Value |= 0x14C0;
        aw88298WriteReg(0x61, 0x0673);
        aw88298WriteReg(0x04, 0x4040);
        aw88298WriteReg(0x05, 0x0008);
        aw88298WriteReg(0x06, reg0x06Value);
        aw88298WriteReg(0x0C, 0x0064);
    } else {
        aw88298WriteReg(0x04, 0x4000);
        M5.In_I2C.bitOff(CORE_S3_AW9523_I2C_ADDR, 0x02, 0b00000100, 400000);
    }
}

static void stopUdpI2s() {
    if (s_udpI2sRunning) {
        i2s_zero_dma_buffer(CORE_S3_SPK_I2S_PORT);
        i2s_stop(CORE_S3_SPK_I2S_PORT);
        i2s_driver_uninstall(CORE_S3_SPK_I2S_PORT);
        s_udpI2sRunning = false;
    }
    setCoreS3AmpEnabled(false);
}

static bool startUdpI2s() {
    if (s_udpI2sRunning) {
        return true;
    }
    if (M5.Speaker.isRunning()) {
        M5.Speaker.end();
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    i2s_driver_uninstall(CORE_S3_SPK_I2S_PORT);
    i2s_config_t i2sCfg;
    memset(&i2sCfg, 0, sizeof(i2sCfg));
    i2sCfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
    i2sCfg.sample_rate = PCM_I2S_SAMPLE_RATE;
    i2sCfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
    i2sCfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
    i2sCfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    i2sCfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
    i2sCfg.dma_buf_count = 8;
    i2sCfg.dma_buf_len = 240;
    i2sCfg.use_apll = false;
    i2sCfg.tx_desc_auto_clear = true;
    i2sCfg.fixed_mclk = 0;
    esp_err_t err = i2s_driver_install(CORE_S3_SPK_I2S_PORT, &i2sCfg, 0, nullptr);
    if (err != ESP_OK) {
        Serial.printf("[PCM/UDP] i2s_driver_install failed: %d\n", (int)err);
        return false;
    }

    i2s_pin_config_t pinCfg;
    memset(&pinCfg, ~0u, sizeof(pinCfg));
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(4, 4, 1)
    pinCfg.mck_io_num = I2S_PIN_NO_CHANGE;
#else
    pinCfg.mck_io_num = I2S_PIN_NO_CHANGE;
#endif
    pinCfg.bck_io_num = CORE_S3_SPK_BCLK;
    pinCfg.ws_io_num = CORE_S3_SPK_WS;
    pinCfg.data_out_num = CORE_S3_SPK_DOUT;
    pinCfg.data_in_num = I2S_PIN_NO_CHANGE;
    err = i2s_set_pin(CORE_S3_SPK_I2S_PORT, &pinCfg);
    if (err != ESP_OK) {
        Serial.printf("[PCM/UDP] i2s_set_pin failed: %d\n", (int)err);
        i2s_driver_uninstall(CORE_S3_SPK_I2S_PORT);
        return false;
    }
    err = i2s_start(CORE_S3_SPK_I2S_PORT);
    if (err != ESP_OK) {
        Serial.printf("[PCM/UDP] i2s_start failed: %d\n", (int)err);
        i2s_driver_uninstall(CORE_S3_SPK_I2S_PORT);
        return false;
    }
    s_udpI2sRunning = true;
    setCoreS3AmpEnabled(true);
    return true;
}

static bool writeUdpI2sFrame(const uint8_t* frame) {
    if (!s_udpI2sRunning) {
        return false;
    }
    int16_t out[PCM_UDP_FRAME_SAMPLES * 2];
    const int16_t* in = (const int16_t*)frame;
    for (size_t i = 0; i < PCM_UDP_FRAME_SAMPLES; ++i) {
        int16_t sample = in[i];
        out[i * 2] = sample;
        out[i * 2 + 1] = sample;
    }
    size_t offset = 0;
    uint8_t* bytes = (uint8_t*)out;
    size_t byteCount = sizeof(out);
    while (offset < byteCount) {
        size_t written = 0;
        esp_err_t err = i2s_write(CORE_S3_SPK_I2S_PORT, bytes + offset, byteCount - offset, &written, 0);
        if (err != ESP_OK) {
            return false;
        }
        offset += written;
        if (offset < byteCount) {
            handleUdpPackets();
            vTaskDelay(pdMS_TO_TICKS(1));
        }
    }
    return true;
}

// Fills `frame` (PCM_UDP_FRAME_SAMPLES mono int16 samples) with a linear
// ramp from `fromSample` down to exactly 0 at the last sample. Used both to
// conceal lost/late frames and to fade out the tail of a session, avoiding
// the instantaneous jump ("click") a flat-zero fill would cause. Integer
// math only, int32 intermediates to avoid overflow (max |fromSample| *
// denom ~= 32768 * 239, well within int32 range).
static void fillConcealmentRamp(uint8_t* frame, int32_t fromSample) {
    int16_t* samples = (int16_t*)frame;
    const int32_t denom = (int32_t)PCM_UDP_FRAME_SAMPLES - 1;
    for (size_t i = 0; i < PCM_UDP_FRAME_SAMPLES; ++i) {
        int32_t remaining = denom - (int32_t)i;
        int32_t value = (fromSample * remaining) / denom;
        samples[i] = (int16_t)value;
    }
}

// Fades in the first PCM_UDP_FADE_SAMPLES samples of `frame` from 0 up to
// their original value, in place. Used when real audio resumes after one
// or more concealed frames (or at session start) to avoid a click.
static void applyFadeIn(uint8_t* frame) {
    int16_t* samples = (int16_t*)frame;
    const size_t fadeSamples =
        (PCM_UDP_FRAME_SAMPLES < PCM_UDP_FADE_SAMPLES) ? PCM_UDP_FRAME_SAMPLES : PCM_UDP_FADE_SAMPLES;
    if (fadeSamples < 2) {
        return;
    }
    const int32_t denom = (int32_t)fadeSamples - 1;
    for (size_t i = 0; i < fadeSamples; ++i) {
        int32_t value = ((int32_t)samples[i] * (int32_t)i) / denom;
        samples[i] = (int16_t)value;
    }
}

static bool writePcmI2s(const uint8_t* data, size_t size) {
    uint8_t frame[PCM_UDP_FRAME_BYTES];
    size_t offset = 0;
    while (offset < size) {
        size_t chunk = min((size_t)PCM_UDP_FRAME_BYTES, size - offset);
        memcpy(frame, data + offset, chunk);
        if (chunk < PCM_UDP_FRAME_BYTES) {
            memset(frame + chunk, 0, PCM_UDP_FRAME_BYTES - chunk);
        }
        if (!writeUdpI2sFrame(frame)) {
            return false;
        }
        offset += chunk;
    }
    return true;
}

static void resetUdpState(bool keepSession) {
    clearUdpRing();
    clearUdpSubmitted();
    s_udpPlaying = false;
    s_udpEnding = false;
    s_udpReceivedAny = false;
    s_udpFirstSeq = 0;
    s_udpNextPlaySeq = 0;
    s_udpHighestSeq = 0;
    s_udpEndSeq = 0;
    s_udpFramesReceived = 0;
    s_udpFramesLost = 0;
    s_udpFramesLate = 0;
    s_udpUnderruns = 0;
    s_udpFirstAudioMs = 0;
    s_udpStartedMs = millis();
    s_udpLastPacketMs = 0;
    s_udpLastSample = 0;
    s_udpConcealedPrev = true;
    if (!keepSession) {
        s_udpActive = false;
        s_udpSession = "";
        s_udpToken = 0;
    }
}

static bool prepareUdpSpeaker() {
    if (!audioGateEnter("pcm-udp", 1000)) {
        Serial.println("[PCM/UDP] Audio gate busy");
        return false;
    }
    if (M5.Mic.isRunning()) {
        M5.Mic.end();
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    if (!startUdpI2s()) {
        audioGateLeave("pcm-udp");
        Serial.println("[PCM/UDP] I2S start failed");
        return false;
    }
    setFaceExpression(FACE_PLAYING);
    return true;
}

static void finishUdpSession(const char* reason, bool waitForDrain = true) {
    if (s_udpPlaying) {
        (void)waitForDrain;
        // Fade the last played sample down to silence instead of cutting
        // the amplifier off mid-waveform, to avoid an end-of-session click.
        if (s_udpI2sRunning && s_udpLastSample != 0) {
            uint8_t fadeFrame[PCM_UDP_FRAME_BYTES];
            fillConcealmentRamp(fadeFrame, s_udpLastSample);
            writeUdpI2sFrame(fadeFrame);
            s_udpLastSample = 0;
        }
        stopUdpI2s();
        audioGateLeave("pcm-udp");
        requestMicResume();
    }
    Serial.printf("[PCM/UDP] complete: session=%s reason=%s frames=%u lost=%u late=%u underruns=%u\n",
                  s_udpSession.c_str(), reason, (unsigned)s_udpFramesReceived,
                  (unsigned)s_udpFramesLost, (unsigned)s_udpFramesLate,
                  (unsigned)s_udpUnderruns);
    s_udpLastEndReason = reason;
    s_udpLastFramesReceived = s_udpFramesReceived;
    s_udpLastFramesLost = s_udpFramesLost;
    s_udpLastFramesLate = s_udpFramesLate;
    s_udpLastUnderruns = s_udpUnderruns;
    s_udpLastFirstAudioMs = s_udpFirstAudioMs;
    resetUdpState(false);
    setMouthOpen(0.0f);
    setFaceExpression(FACE_IDLE);
}

static bool takeUdpFrame(uint32_t seq, uint8_t* out) {
    if (!s_udpRing) {
        memset(out, 0, PCM_UDP_FRAME_BYTES);
        return false;
    }
    UdpFrameSlot& slot = s_udpRing[seq % PCM_UDP_RING_FRAMES];
    if (!slot.valid || slot.seq != seq) {
        memset(out, 0, PCM_UDP_FRAME_BYTES);
        return false;
    }
    memcpy(out, slot.data, PCM_UDP_FRAME_BYTES);
    slot.valid = false;
    return true;
}

static bool hasUdpFrame(uint32_t seq) {
    if (!s_udpRing) {
        return false;
    }
    UdpFrameSlot& slot = s_udpRing[seq % PCM_UDP_RING_FRAMES];
    return slot.valid && slot.seq == seq;
}

static void handleUdpPackets() {
    for (;;) {
        int packetSize = s_udpServer.parsePacket();
        if (packetSize <= 0) {
            return;
        }
        uint8_t packet[PCM_UDP_PACKET_MAX];
        int readSize = s_udpServer.read(packet, min(packetSize, (int)sizeof(packet)));
        if (!s_udpActive || readSize < PCM_UDP_HEADER_BYTES) {
            continue;
        }
        if (memcmp(packet, "SCP1", 4) != 0 || packet[4] != PCM_UDP_VERSION) {
            continue;
        }
        uint8_t flags = packet[5];
        uint16_t headerBytes = readLe16(packet + 6);
        uint32_t token = readLe32(packet + 8);
        uint32_t seq = readLe32(packet + 12);
        uint16_t payloadBytes = readLe16(packet + 20);
        if (headerBytes != PCM_UDP_HEADER_BYTES || token != s_udpToken) {
            continue;
        }

        s_udpLastPacketMs = millis();
        if (flags & PCM_UDP_FLAG_END) {
            s_udpEnding = true;
            s_udpEndSeq = seq;
            continue;
        }
        if (payloadBytes != PCM_UDP_FRAME_BYTES ||
            readSize < (int)(PCM_UDP_HEADER_BYTES + PCM_UDP_FRAME_BYTES)) {
            continue;
        }
        if (s_udpReceivedAny && seq < s_udpNextPlaySeq) {
            s_udpFramesLate++;
            continue;
        }

        if (!s_udpRing) {
            continue;
        }
        UdpFrameSlot& slot = s_udpRing[seq % PCM_UDP_RING_FRAMES];
        if (slot.valid && slot.seq == seq) {
            continue;
        }
        slot.seq = seq;
        memcpy(slot.data, packet + PCM_UDP_HEADER_BYTES, PCM_UDP_FRAME_BYTES);
        slot.valid = true;
        s_udpFramesReceived++;
        if (!s_udpReceivedAny) {
            s_udpReceivedAny = true;
            s_udpFirstSeq = seq;
            s_udpNextPlaySeq = seq;
            s_udpHighestSeq = seq;
        } else if (seq > s_udpHighestSeq) {
            s_udpHighestSeq = seq;
        }
    }
}

static void pumpUdpPlayback() {
    if (!s_udpActive) {
        return;
    }
    unsigned long now = millis();
    if (!s_udpReceivedAny) {
        if (now - s_udpStartedMs > PCM_UDP_PREROLL_TIMEOUT_MS) {
            finishUdpSession("preroll-timeout");
        }
        return;
    }
    if (!s_udpPlaying) {
        if (udpBufferedFrames() < PCM_UDP_START_FRAMES && !s_udpEnding) {
            return;
        }
        if (!prepareUdpSpeaker()) {
            finishUdpSession("speaker-failed");
            return;
        }
        s_udpPlaying = true;
        s_udpFirstAudioMs = millis() - s_udpStartedMs;
        Serial.printf("[PCM/UDP] started: session=%s first_audio_ms=%u\n",
                      s_udpSession.c_str(), (unsigned)s_udpFirstAudioMs);
    }

    if (s_udpEnding && s_udpNextPlaySeq >= s_udpEndSeq) {
        finishUdpSession("end");
        return;
    }
    if (!s_udpEnding && now - s_udpLastPacketMs > PCM_UDP_ACTIVE_TIMEOUT_MS) {
        finishUdpSession("timeout");
        return;
    }
    if (!s_udpEnding && now - s_udpLastPacketMs > PCM_UDP_IDLE_END_GRACE_MS) {
        if (udpBufferedFrames() == 0) {
            finishUdpSession("idle", false);
        } else {
            s_udpEnding = true;
            s_udpEndSeq = s_udpHighestSeq + 1;
        }
        return;
    }
    bool enough = true;
    for (uint32_t i = 0; i < PCM_UDP_SLAB_FRAMES; ++i) {
        uint32_t seq = s_udpNextPlaySeq + i;
        if (s_udpEnding && seq >= s_udpEndSeq) {
            break;
        }
        if (!hasUdpFrame(seq)) {
            enough = false;
            break;
        }
    }
    if (!enough && !s_udpEnding && s_udpNextPlaySeq > s_udpHighestSeq) {
        return;
    }
    if (!s_udpEnding && s_udpNextPlaySeq + PCM_UDP_SLAB_FRAMES > s_udpHighestSeq + 1) {
        return;
    }

    for (uint32_t i = 0; i < PCM_UDP_SLAB_FRAMES; ++i) {
        uint32_t seq = s_udpNextPlaySeq++;
        uint8_t frame[PCM_UDP_FRAME_BYTES];
        bool gotFrame = false;
        if (s_udpEnding && seq >= s_udpEndSeq) {
            // Trailing padding frame(s) past the announced end sequence:
            // ramp to silence rather than jump straight to a flat zero.
            fillConcealmentRamp(frame, s_udpLastSample);
            s_udpConcealedPrev = true;
        } else if (takeUdpFrame(seq, frame)) {
            gotFrame = true;
        } else {
            s_udpFramesLost++;
            s_udpUnderruns++;
            // Conceal the loss/underrun with a ramp to silence instead of
            // a hard drop to zero (takeUdpFrame() already zeroed `frame`,
            // this overwrites it with the ramp).
            fillConcealmentRamp(frame, s_udpLastSample);
            s_udpConcealedPrev = true;
        }
        if (gotFrame && s_udpConcealedPrev) {
            // Real audio resuming after concealment (or session start):
            // fade in instead of jumping straight to full amplitude.
            applyFadeIn(frame);
            s_udpConcealedPrev = false;
        }
        if (!writeUdpI2sFrame(frame)) {
            finishUdpSession("i2s-write-failed");
            return;
        }
        s_udpLastSample = ((const int16_t*)frame)[PCM_UDP_FRAME_SAMPLES - 1];
    }
}

static bool readHeaderLine(WiFiClient& client, String& line) {
    const unsigned long deadline = millis() + PCM_STREAM_HEADER_TIMEOUT_MS;
    line = "";
    while (millis() < deadline && client.connected()) {
        while (client.available() > 0) {
            int value = client.read();
            if (value < 0) {
                break;
            }
            if (value == '\n') {
                return true;
            }
            if (line.length() >= PCM_STREAM_HEADER_MAX) {
                return false;
            }
            if (value != '\r') {
                line += (char)value;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    return false;
}

static String headerValue(const String& line, const char* key) {
    String prefix = String(key) + "=";
    int start = line.indexOf(prefix);
    if (start < 0) {
        return "";
    }
    start += prefix.length();
    int end = line.indexOf(' ', start);
    if (end < 0) {
        end = line.length();
    }
    return line.substring(start, end);
}

static bool validateHeader(const String& line, String& session, String& error) {
    if (!line.startsWith("STACKCHAN_PCM_STREAM/1 ")) {
        error = "ERR PROTOCOL\n";
        return false;
    }
    session = headerValue(line, "session");
    if (session.length() == 0 || session.length() > 64) {
        error = "ERR SESSION\n";
        return false;
    }
    if (headerValue(line, "rate") != "24000" ||
        headerValue(line, "channels") != "1" ||
        headerValue(line, "width") != "2") {
        error = "ERR FORMAT\n";
        return false;
    }
    return true;
}

static bool prepareStreamSpeaker() {
    if (!audioGateEnter("pcm-stream", 1000)) {
        Serial.println("[PCM/TCP] Audio gate busy");
        return false;
    }
    if (M5.Mic.isRunning()) {
        M5.Mic.end();
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    if (!startUdpI2s()) {
        audioGateLeave("pcm-stream");
        Serial.println("[PCM/TCP] I2S start failed");
        return false;
    }
    setFaceExpression(FACE_PLAYING);
    return true;
}

static void endStreamSpeaker() {
    stopUdpI2s();
    setMouthOpen(0.0f);
    setFaceExpression(FACE_IDLE);
    audioGateLeave("pcm-stream");
    requestMicResume();
}

static StreamSlab readSlab(WiFiClient& client, bool& eof, bool& invalid) {
    StreamSlab slab;
    eof = false;
    invalid = false;
    uint8_t* data = (uint8_t*)ps_malloc(PCM_STREAM_SLAB_BYTES);
    if (!data) {
        invalid = true;
        Serial.println("[PCM/TCP] slab allocation failed");
        return slab;
    }

    const unsigned long deadline = millis() + PCM_STREAM_READ_TIMEOUT_MS;
    size_t size = 0;
    while (size < PCM_STREAM_SLAB_BYTES) {
        int available = client.available();
        if (available > 0) {
            int want = min((int)(PCM_STREAM_SLAB_BYTES - size), available);
            int got = client.read(data + size, want);
            if (got > 0) {
                size += got;
                continue;
            }
        }
        if (!client.connected()) {
            eof = true;
            break;
        }
        if (millis() > deadline) {
            invalid = true;
            Serial.println("[PCM/TCP] read timeout");
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }

    if (size == 0) {
        free(data);
        return slab;
    }
    if ((size % PCM_STREAM_BYTES_PER_SAMPLE) != 0) {
        free(data);
        invalid = true;
        Serial.printf("[PCM/TCP] invalid slab size: %u\n", (unsigned)size);
        return slab;
    }
    slab.data = data;
    slab.size = size;
    return slab;
}

static bool readOneQueuedSlab(
    WiFiClient& client,
    std::queue<StreamSlab>& pending,
    bool& eof
) {
    bool invalid = false;
    StreamSlab slab = readSlab(client, eof, invalid);
    if (invalid) {
        return false;
    }
    if (!slab.data) {
        return true;
    }
    if (s_totalBytes > PCM_STREAM_MAX_TOTAL - slab.size ||
        s_bufferedBytes > PCM_STREAM_MAX_BUFFERED - slab.size) {
        Serial.println("[PCM/TCP] stream too large");
        freeSlab(slab);
        return false;
    }
    pending.push(slab);
    s_bufferedBytes += slab.size;
    s_totalBytes += slab.size;
    return true;
}

static void playQueuedStream(WiFiClient& client, const String& session) {
    std::queue<StreamSlab> pending;
    std::queue<StreamSlab> submitted;
    bool eof = false;

    while (!eof && s_bufferedBytes < PCM_STREAM_PREBUFFER_BYTES) {
        if (!readOneQueuedSlab(client, pending, eof)) {
            freeQueue(pending);
            return;
        }
    }
    if (pending.empty()) {
        Serial.println("[PCM/TCP] empty stream");
        return;
    }
    if (!prepareStreamSpeaker()) {
        freeQueue(pending);
        return;
    }

    s_playing = true;
    Serial.printf("[PCM/TCP] started: session=%s prebuffer=%u\n",
                  session.c_str(), (unsigned)s_bufferedBytes);

    while (!pending.empty() || !eof) {
        if (pending.empty()) {
            s_underruns++;
            if (!readOneQueuedSlab(client, pending, eof)) {
                break;
            }
            continue;
        }

        StreamSlab slab = pending.front();
        pending.pop();
        s_bufferedBytes -= slab.size;
        if (!writePcmI2s(slab.data, slab.size)) {
            Serial.println("[PCM/TCP] I2S write failed");
            freeSlab(slab);
            break;
        }
        submitted.push(slab);
        while (submitted.size() > 2) {
            freeSlab(submitted.front());
            submitted.pop();
        }
        if (!eof && s_bufferedBytes < PCM_STREAM_PREBUFFER_BYTES) {
            if (!readOneQueuedSlab(client, pending, eof)) {
                break;
            }
        }
    }

    freeQueue(pending);
    endStreamSpeaker();
    freeQueue(submitted);
    s_playing = false;
    Serial.printf("[PCM/TCP] complete: session=%s bytes=%u underruns=%u\n",
                  session.c_str(), (unsigned)s_totalBytes, (unsigned)s_underruns);
}

static void handleStreamClient(WiFiClient client) {
    String line;
    String session;
    String error;

    s_clientConnected = true;
    if (!readHeaderLine(client, line) || !validateHeader(line, session, error)) {
        client.print(error.length() ? error : "ERR HEADER\n");
        client.stop();
        s_clientConnected = false;
        return;
    }
    if (s_active || s_udpActive || isPlaybackActive() || M5.Speaker.isPlaying()) {
        client.print("ERR BUSY\n");
        client.stop();
        s_clientConnected = false;
        return;
    }

    s_active = true;
    s_session = session;
    s_bufferedBytes = 0;
    s_totalBytes = 0;
    s_underruns = 0;
    client.print("OK\n");
    playQueuedStream(client, session);
    client.stop();
    s_session = "";
    s_active = false;
    s_clientConnected = false;
}

static void pcmStreamTask(void*) {
    s_streamServer.begin();
    s_udpServer.begin(PCM_UDP_PORT);
    Serial.printf("[PCM/TCP] Listening on port %u\n", PCM_STREAM_PORT);
    Serial.printf("[PCM/UDP] Listening on port %u\n", PCM_UDP_PORT);
    for (;;) {
        WiFiClient client = s_streamServer.available();
        if (client) {
            handleStreamClient(client);
        }
        handleUdpPackets();
        pumpUdpPlayback();
        vTaskDelay(pdMS_TO_TICKS(1));
    }
}

void initPcmStreamService() {
    if (s_streamTask) {
        return;
    }
    xTaskCreatePinnedToCore(
        pcmStreamTask,
        "pcmStream",
        8192,
        nullptr,
        1,
        &s_streamTask,
        1
    );
}

bool isPcmStreamActive() {
    return s_active || s_playing || s_udpActive || s_udpPlaying;
}

UdpPcmSessionResult beginUdpPcmSession() {
    UdpPcmSessionResult result;
    result.port = PCM_UDP_PORT;
    if (!s_udpRing) {
        s_udpRing = (UdpFrameSlot*)ps_malloc(sizeof(UdpFrameSlot) * PCM_UDP_RING_FRAMES);
        if (!s_udpRing) {
            result.error = "alloc failed";
            return result;
        }
        memset(s_udpRing, 0, sizeof(UdpFrameSlot) * PCM_UDP_RING_FRAMES);
    }
    if (s_active || s_playing || s_udpActive || s_udpPlaying || isPlaybackActive() || M5.Speaker.isPlaying()) {
        result.error = "busy";
        return result;
    }
    resetUdpState(false);
    s_udpActive = true;
    s_udpToken = esp_random();
    if (s_udpToken == 0) {
        s_udpToken = 1;
    }
    s_udpSession = String("udp_") + String(s_udpToken, HEX) + String("_") + String(millis(), HEX);
    s_udpStartedMs = millis();
    result.success = true;
    result.session = s_udpSession;
    result.token = s_udpToken;
    Serial.printf("[PCM/UDP] session started: session=%s token=%08x\n",
                  s_udpSession.c_str(), (unsigned)s_udpToken);
    return result;
}

bool stopUdpPcmSession(const String& sessionId) {
    if (!s_udpActive || (sessionId.length() > 0 && sessionId != s_udpSession)) {
        return false;
    }
    finishUdpSession("stopped");
    return true;
}

PcmStreamStatus getPcmStreamStatus() {
    PcmStreamStatus status;
    status.enabled = s_streamTask != nullptr;
    status.active = s_active;
    status.playing = s_playing;
    status.clientConnected = s_clientConnected;
    status.session = s_session.c_str();
    status.port = PCM_STREAM_PORT;
    status.bufferedBytes = s_bufferedBytes;
    status.totalBytes = s_totalBytes;
    status.underruns = s_underruns;
    status.udpEnabled = s_streamTask != nullptr;
    status.udpActive = s_udpActive;
    status.udpPlaying = s_udpPlaying;
    status.udpSession = s_udpSession.c_str();
    status.udpPort = PCM_UDP_PORT;
    status.udpToken = s_udpToken;
    status.udpBufferedFrames = udpBufferedFrames();
    status.udpBufferedBytes = status.udpBufferedFrames * PCM_UDP_FRAME_BYTES;
    status.udpFramesReceived = s_udpFramesReceived;
    status.udpFramesLost = s_udpFramesLost;
    status.udpFramesLate = s_udpFramesLate;
    status.udpUnderruns = s_udpUnderruns;
    status.udpFirstAudioMs = s_udpFirstAudioMs;
    status.udpLastEndReason = s_udpLastEndReason.c_str();
    status.udpLastFramesReceived = s_udpLastFramesReceived;
    status.udpLastFramesLost = s_udpLastFramesLost;
    status.udpLastFramesLate = s_udpLastFramesLate;
    status.udpLastUnderruns = s_udpLastUnderruns;
    status.udpLastFirstAudioMs = s_udpLastFirstAudioMs;
    return status;
}
