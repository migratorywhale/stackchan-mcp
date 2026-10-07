#include <M5Unified.h>
#include <WebServer.h>
#include <uri/UriBraces.h>
#include <ArduinoJson.h>
#include "http_server.h"
#include "types.h"
#include "servo_service.h"
#include "camera_service.h"
#include "face_service.h"
#include "playback_service.h"
#include "mic_service.h"
#include "recording_store.h"
#include "pcm_upload.h"
#include "audio_gate.h"
#include "pcm_stream_service.h"
#include "env_service.h"
#include "touch_service.h"
#include "firmware_command.h"
#include "booth_mode.h"

static WebServer httpServer(80);

// Both transports execute these handlers on the main loop. Network callbacks
// never enter this adapter or the hardware services it calls.
class CommandContext {
public:
    FirmwareCommand* outbound = nullptr;

    bool hasArg(const String& name) {
        if (!outbound) return httpServer.hasArg(name);
        return name == "plain" ? ::outbound::hasRequestBody(outbound->request)
                               : !outbound->request["query"][name].isUnbound();
    }
    String arg(const String& name) {
        if (!outbound) return httpServer.arg(name);
        if (name == "plain") {
            String body;
            if (hasArg(name)) serializeJson(outbound->request["body"], body);
            return body;
        }
        return String(outbound->request["query"][name] | "");
    }
    String header(const char* name) {
        return outbound ? String("") : httpServer.header(name);
    }
    String uri() {
        return outbound ? String(outbound->request["path"] | "") : httpServer.uri();
    }
    void send(int status, const char* contentType, const String& body) {
        if (!outbound) {
            httpServer.send(status, contentType, body);
            return;
        }
        outbound->status = status;
        outbound->body = body;
        outbound->contentType = contentType;
    }
    bool send_P(int status, const char* contentType, const char* data, size_t size) {
        if (!outbound) {
            httpServer.send_P(status, contentType, data, size);
            return true;
        }
        uint8_t* frame = nullptr;
        const auto result = ::outbound::copyBinaryResponse(outbound->info.id, contentType,
            reinterpret_cast<const uint8_t*>(data), size, &frame, ps_malloc);
        if (result == ::outbound::BinaryCopyResult::TOO_LARGE) {
            send(413, "application/json", "{\"success\":false,\"error\":\"binary response too large\"}");
            return false;
        }
        if (result != ::outbound::BinaryCopyResult::OK) {
            send(503, "application/json", "{\"success\":false,\"error\":\"response allocation failed\"}");
            return false;
        }
        outbound->binaryFrame = frame;
        outbound->binarySize = size;
        outbound->status = status;
        outbound->contentType = contentType;
        return true;
    }
};

static CommandContext server;

static String   s_pcm_diag_session = "";
static long     s_pcm_diag_next_seq = 0;

static const char* PCM_HEADER_SESSION = "X-Stackchan-Pcm-Session";
static const char* PCM_HEADER_SEQ = "X-Stackchan-Pcm-Seq";
static const char* PCM_HEADER_FINAL = "X-Stackchan-Pcm-Final";
static const char* PCM_HEADER_MODE = "X-Stackchan-Pcm-Mode";

// ────────────────────────────────────────────
// POST /play
// body: {"voice_url": "http://..."}
// → AudioTaskをキューに積んで再生
// ────────────────────────────────────────────
static void handlePlay() {
    if (!server.hasArg("plain")) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"no body\"}");
        return;
    }

    JsonDocument doc;
    if (deserializeJson(doc, server.arg("plain")) != DeserializationError::Ok) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"json parse error\"}");
        return;
    }

    const char* voice_url = doc["voice_url"] | "";
    if (strlen(voice_url) == 0) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"voice_url required\"}");
        return;
    }

    AudioTask task;
    task.voice_id  = String("mcp_") + String(millis());
    task.voice_url = String(voice_url);
    task.priority  = PRIORITY_NORMAL;
    if (!enqueueAudioTask(task)) {
        Serial.printf("[HTTP] POST /play -> queue failed: %s\n", voice_url);
        server.send(503, "application/json", "{\"success\":false,\"error\":\"play queue full\"}");
        return;
    }

    Serial.printf("[HTTP] POST /play -> queued: %s\n", voice_url);
    server.send(200, "application/json", "{\"success\":true}");
}

static String headerOrArg(const char* headerName, const char* argName) {
    String value = server.header(headerName);
    if (value.length() > 0) {
        return value;
    }
    return server.arg(argName);
}

// ────────────────────────────────────────────
// POST /play/pcm
// body: raw 24kHz mono s16le PCM
// ────────────────────────────────────────────
static void handlePlayPcm() {
    const char* uploadError = server.outbound ? nullptr : consumePcmUploadError();
    if (uploadError) {
        String body = "{\"success\":false,\"error\":\"";
        body += uploadError;
        body += "\"}";
        clearPcmUpload();
        server.send(400, "application/json", body);
        return;
    }
    if (server.outbound ? !server.outbound->pcmData : !hasPcmUploadBody()) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"no pcm body\"}");
        return;
    }

    PcmUploadBuffer upload;
    if (server.outbound) {
        upload = {server.outbound->pcmData, server.outbound->info.binarySize};
        server.outbound->pcmData = nullptr;
    } else {
        upload = takePcmUploadBody();
    }
    const size_t pcmSize = upload.size;
    String sessionId = headerOrArg(PCM_HEADER_SESSION, "session");
    String seqArg = headerOrArg(PCM_HEADER_SEQ, "seq");
    long seq = seqArg.length() ? seqArg.toInt() : -1;
    String finalArg = headerOrArg(PCM_HEADER_FINAL, "final");
    bool finalSegment = finalArg == "1" || finalArg == "true";
    String pcmMode = headerOrArg(PCM_HEADER_MODE, "mode");
    bool stagedMode = pcmMode == "staged" || server.arg("staged") == "1" || server.arg("defer") == "1";
    uint8_t* pcmData = upload.data;

    long expectedSeq = s_pcm_diag_next_seq;
    bool newDiagSession = sessionId != s_pcm_diag_session;
    if (newDiagSession) {
        expectedSeq = 0;
    }
    bool seqValid = true;
    if (seq < 0 || seq != expectedSeq) {
        Serial.printf("[HTTP] PCM seq invalid: session=%s got=%ld expected=%ld final=%s bytes=%u\n",
                      sessionId.c_str(), seq, expectedSeq,
                      finalSegment ? "true" : "false", (unsigned)pcmSize);
        seqValid = false;
    }

    if (!seqValid) {
        free(pcmData);
        if (server.outbound) clearQueuedPcmSession(sessionId, PcmOrigin::OUTBOUND, server.outbound->generation);
        else clearQueuedPcmPlayback();
        server.send(409, "application/json", "{\"success\":false,\"error\":\"pcm seq invalid\"}");
        return;
    }
    const PcmOrigin origin = server.outbound ? PcmOrigin::OUTBOUND : PcmOrigin::DIRECT;
    const uint32_t generation = server.outbound ? server.outbound->generation : 0;
    PcmPlaybackResult result = stagedMode
        ? stagePcmPlayback(pcmData, pcmSize, sessionId, seq, finalSegment, origin, generation)
        : startPcmPlayback(pcmData, pcmSize, sessionId, finalSegment, origin, generation);
    if (result != PCM_PLAYBACK_OK && result != PCM_PLAYBACK_QUEUED) {
        if (result != PCM_PLAYBACK_SPEAKER_FAILED) {
            free(pcmData);
        }
        Serial.printf("[HTTP] POST /play/pcm failed -> session=%s seq=%ld bytes=%u final=%s result=%d\n",
                      sessionId.c_str(), seq, (unsigned)pcmSize,
                      finalSegment ? "true" : "false", result);
        if (result == PCM_PLAYBACK_BUSY) {
            server.send(409, "application/json", "{\"success\":false,\"error\":\"playback busy\"}");
        } else if (result == PCM_PLAYBACK_SESSION_MISMATCH) {
            server.send(409, "application/json", "{\"success\":false,\"error\":\"pcm session mismatch\"}");
        } else if (result == PCM_PLAYBACK_SPEAKER_FAILED) {
            server.send(500, "application/json", "{\"success\":false,\"error\":\"speaker failed\"}");
        } else {
            server.send(400, "application/json", "{\"success\":false,\"error\":\"invalid pcm\"}");
        }
        return;
    }

    if (newDiagSession) {
        s_pcm_diag_session = sessionId;
        Serial.printf("[HTTP] PCM diag new session=%s\n", sessionId.c_str());
    }
    s_pcm_diag_next_seq = seq + 1;

    Serial.printf("[HTTP] POST /play/pcm -> session=%s seq=%ld bytes=%u final=%s mode=%s result=%d queued=%s\n",
                  sessionId.c_str(), seq, (unsigned)pcmSize,
                  finalSegment ? "true" : "false", stagedMode ? "staged" : "stream",
                  result,
                  result == PCM_PLAYBACK_QUEUED ? "true" : "false");
    if (result == PCM_PLAYBACK_QUEUED) {
        if (stagedMode) {
            server.send(202, "application/json", "{\"success\":true,\"queued\":true,\"staged\":true,\"format\":\"s16le\",\"sample_rate\":24000,\"channels\":1}");
        } else {
            server.send(202, "application/json", "{\"success\":true,\"queued\":true,\"staged\":false,\"format\":\"s16le\",\"sample_rate\":24000,\"channels\":1}");
        }
    } else {
        server.send(200, "application/json", "{\"success\":true,\"queued\":false,\"staged\":false,\"format\":\"s16le\",\"sample_rate\":24000,\"channels\":1}");
    }
}

static void handlePlayPcmRaw() {
    HTTPRaw& raw = httpServer.raw();

    if (raw.status == RAW_START) {
        handlePcmUploadRaw(PCM_UPLOAD_RAW_START, nullptr, 0);
        return;
    }

    if (raw.status == RAW_WRITE) {
        handlePcmUploadRaw(PCM_UPLOAD_RAW_WRITE, raw.buf, raw.currentSize);
        return;
    }

    if (raw.status == RAW_END) {
        handlePcmUploadRaw(PCM_UPLOAD_RAW_END, nullptr, 0);
        return;
    }

    if (raw.status == RAW_ABORTED) {
        handlePcmUploadRaw(PCM_UPLOAD_RAW_ABORTED, nullptr, 0);
    }
}

// ────────────────────────────────────────────
// POST /mode
// body: {"mode": "mcp"}
// → Recording is always MCP pull mode; this endpoint clears stale recordings.
// ────────────────────────────────────────────
static void handleMode() {
    if (!server.hasArg("plain")) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"no body\"}");
        return;
    }

    JsonDocument doc;
    if (deserializeJson(doc, server.arg("plain")) != DeserializationError::Ok) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"json parse error\"}");
        return;
    }

    const char* mode = doc["mode"] | "";
    if (strcmp(mode, "mcp") == 0) {
        clearLastRecording();
        Serial.println("[HTTP] Mode -> MCP (buffer cleared)");
        server.send(200, "application/json", "{\"success\":true,\"mode\":\"mcp\"}");
    } else {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"mode must be mcp\"}");
    }
}

// ────────────────────────────────────────────
// GET/POST /booth: persistent microphone capture policy.
// ────────────────────────────────────────────
static void sendBoothStatus(bool success = true) {
    if (isBoothMode() && !isMicrophoneCaptureStopped()) success = false;
    JsonDocument doc;
    doc["success"] = success;
    doc["booth_mode"] = isBoothMode();
    doc["persisted"] = isBoothModePersisted();
    doc["capture_allowed"] = !isBoothMode();
    doc["mic_running"] = M5.Mic.isRunning();
    if (!success) doc["error"] = "Booth mode transition incomplete; inspect status before retrying";
    String body;
    serializeJson(doc, body);
    server.send(success ? 200 : 503, "application/json", body);
}

static void handleBoothStatus() { sendBoothStatus(); }

static void handleBoothSet() {
    JsonDocument doc;
    if (!server.hasArg("plain") ||
        deserializeJson(doc, server.arg("plain")) != DeserializationError::Ok ||
        !doc.is<JsonObject>() || doc.size() != 1 || !doc["enabled"].is<bool>()) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"expected only enabled: boolean\"}");
        return;
    }
    sendBoothStatus(setBoothMode(doc["enabled"].as<bool>()));
}

// GET /audio/status: recording state and monotonic host pet-notification counter.
static void handleAudioStatus() {
    TouchRuntimeStatus touch = getTouchRuntimeStatus();
    String body = "{\"ready\":";
    body += !isBoothMode() && hasLastRecording() ? "true" : "false";
    body += ",\"booth_mode\":";
    body += isBoothMode() ? "true" : "false";
    body += ",\"mode\":\"mcp\",\"source\":\"";
    body += recordingSourceName(getLastRecordingSource());
    body += "\",\"touch_pet_count\":";
    body += String(touch.petCount);
    body += "}";
    server.send(200, "application/json", body);
}

// ────────────────────────────────────────────
// GET /audio
// → 録音済みWAVをそのまま返す（1回読んだらクリア）
// ────────────────────────────────────────────
static void handleAudio() {
    if (isBoothMode()) {
        server.send(409, "application/json", "{\"success\":false,\"error\":\"microphone capture blocked by booth mode\"}");
        return;
    }
    RecordingSnapshot recording = getLastRecording();
    if (!recording.data || recording.size == 0) {
        server.send(404, "application/json", "{\"success\":false,\"error\":\"no audio\"}");
        return;
    }

    Serial.printf("[HTTP] GET /audio -> %u bytes\n", (unsigned)recording.size);
    // Failed outbound copies must leave the recording available. Once copied,
    // the response owns its bytes independently; delivery is never replayed.
    if (server.send_P(200, "audio/wav", (const char*)recording.data, recording.size)) {
        markLastRecordingConsumed();
    }
}

// ────────────────────────────────────────────
// POST /move
// body: {"x": float, "y": float, "speed": int}
// → Servo move head (degrees)
// ────────────────────────────────────────────
static void handleMove() {
    if (!server.hasArg("plain")) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"no body\"}");
        return;
    }

    JsonDocument doc;
    if (deserializeJson(doc, server.arg("plain")) != DeserializationError::Ok) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"json parse error\"}");
        return;
    }

    float x = doc["x"] | 0.0f;
    float y = doc["y"] | 0.0f;
    int speed = doc["speed"] | 50;
    bool interruptGesture = doc["interrupt_gesture"] | true;

    if (!isServoReady()) {
        server.send(503, "application/json", "{\"success\":false,\"error\":\"servo not ready\"}");
        return;
    }
    if (!interruptGesture && getServoStatus().gestureActive) {
        server.send(409, "application/json",
                    "{\"success\":false,\"error\":\"gesture active\"}");
        return;
    }
    bool ack = servoMove(x, y, speed);

    Serial.printf("[HTTP] POST /move -> x=%.1f y=%.1f speed=%d\n", x, y, speed);
    server.send(200, "application/json", ack ? "{\"success\":true,\"ack\":true}" : "{\"success\":true,\"ack\":false}");
}

// ────────────────────────────────────────────
// POST /home
// → Return head to center position
// ────────────────────────────────────────────
static void handleHome() {
    if (!isServoReady()) {
        server.send(503, "application/json", "{\"success\":false,\"error\":\"servo not ready\"}");
        return;
    }
    bool ack = servoHome(50);
    Serial.println("[HTTP] POST /home");
    server.send(200, "application/json", ack ? "{\"success\":true,\"ack\":true}" : "{\"success\":true,\"ack\":false}");
}

// ────────────────────────────────────────────
// POST /nod
// → Nod "yes" gesture
// ────────────────────────────────────────────
static void handleNod() {
    if (!isServoReady()) {
        server.send(503, "application/json", "{\"success\":false,\"error\":\"servo not ready\"}");
        return;
    }
    bool ack = servoNod();
    Serial.println("[HTTP] POST /nod");
    server.send(200, "application/json", ack ? "{\"success\":true,\"ack\":true}" : "{\"success\":true,\"ack\":false}");
}

// ────────────────────────────────────────────
// POST /shake
// → Shake "no" gesture
// ────────────────────────────────────────────
static void handleShake() {
    if (!isServoReady()) {
        server.send(503, "application/json", "{\"success\":false,\"error\":\"servo not ready\"}");
        return;
    }
    bool ack = servoShake();
    Serial.println("[HTTP] POST /shake");
    server.send(200, "application/json", ack ? "{\"success\":true,\"ack\":true}" : "{\"success\":true,\"ack\":false}");
}

static void addFeedback(JsonObject obj, const ServoFeedback& fb) {
    obj["ok"] = fb.ok;
    obj["position"] = fb.position;
    obj["speed"] = fb.speed;
    obj["load"] = fb.load;
    obj["voltage"] = fb.voltage;
    obj["temperature"] = fb.temperature;
    obj["moving"] = fb.moving;
    obj["current"] = fb.current;
}

// ────────────────────────────────────────────
// GET /servo/status
// → Servo communication and feedback diagnostics
// ────────────────────────────────────────────
static void handleServoStatus() {
    ServoStatus status = getServoStatus();
    JsonDocument doc;
    doc["ready"] = status.ready;
    doc["last_command_ok"] = status.lastCommandOk;
    doc["last_yaw_raw"] = status.lastYawRaw;
    doc["last_pitch_raw"] = status.lastPitchRaw;
    doc["last_yaw_result"] = status.lastYawResult;
    doc["last_pitch_result"] = status.lastPitchResult;
    doc["last_command_ms"] = status.lastCommandMs;
    doc["gesture_active"] = status.gestureActive;
    doc["gesture"] = status.gestureName;

    JsonObject yaw = doc["yaw"].to<JsonObject>();
    JsonObject pitch = doc["pitch"].to<JsonObject>();
    addFeedback(yaw, status.yaw);
    addFeedback(pitch, status.pitch);

    String body;
    serializeJson(doc, body);
    server.send(200, "application/json", body);
}

// ────────────────────────────────────────────
// GET /touch/status
// → Si12T touch-strip state and camera handoff diagnostics
// ────────────────────────────────────────────
static void handleTouchStatus() {
    TouchRuntimeStatus status = getTouchRuntimeStatus();
    JsonDocument doc;
    doc["available"] = status.available;
    doc["booth_mode"] = isBoothMode();
    doc["suspended"] = status.suspended;
    doc["petting_active"] = status.pettingActive;
    JsonArray intensities = doc["intensities"].to<JsonArray>();
    for (uint8_t intensity : status.intensities) {
        intensities.add(intensity);
    }
    doc["last_event"] = status.lastEvent;
    doc["last_event_ms"] = status.lastEventMs;
    doc["pet_count"] = status.petCount;
    doc["suppressed_pet_count"] = status.suppressedPetCount;
    doc["record_request_count"] = status.recordRequestCount;
    doc["record_start_failure_count"] = status.recordStartFailureCount;
    doc["resume_failure_count"] = status.resumeFailureCount;

    String body;
    serializeJson(doc, body);
    server.send(200, "application/json", body);
}

// ────────────────────────────────────────────
// GET /playback/status
// → Combined runtime diagnostics for playback, microphone, queues, and memory
// ────────────────────────────────────────────
static void handlePlaybackStatus() {
    PlaybackStatus playback = getPlaybackStatus();
    PcmStreamStatus stream = getPcmStreamStatus();
    ServoStatus servo = getServoStatus();
    AudioGateStatus audio = getAudioGateStatus();
    MicRuntimeStatus mic = getMicRuntimeStatus();

    JsonDocument doc;
    bool streamActive = stream.active || stream.playing;
    bool udpActive = stream.udpActive || stream.udpPlaying;
    doc["playing"] = playback.playing || streamActive || udpActive;
    doc["kind"] = udpActive ? "udp_pcm" : (streamActive ? "pcm_stream" : (playback.pcm ? "pcm" : (playback.playing ? "wav" : "idle")));
    doc["pcm_session"] = playback.pcmSession;
    doc["pcm_final_segment"] = playback.pcmFinalSegment;
    doc["pcm_stream_enabled"] = stream.enabled;
    doc["pcm_stream_port"] = stream.port;
    doc["pcm_stream_active"] = stream.active;
    doc["pcm_stream_playing"] = stream.playing;
    doc["pcm_stream_client_connected"] = stream.clientConnected;
    doc["pcm_stream_session"] = stream.session;
    doc["pcm_stream_buffered_bytes"] = stream.bufferedBytes;
    doc["pcm_stream_total_bytes"] = stream.totalBytes;
    doc["pcm_stream_underruns"] = stream.underruns;
    doc["udp_audio_enabled"] = stream.udpEnabled;
    doc["udp_audio_active"] = stream.udpActive;
    doc["udp_audio_playing"] = stream.udpPlaying;
    doc["udp_audio_session"] = stream.udpSession;
    doc["udp_audio_port"] = stream.udpPort;
    doc["udp_audio_token"] = stream.udpToken;
    doc["udp_audio_buffered_frames"] = stream.udpBufferedFrames;
    doc["jitter_ms"] = (unsigned)(stream.udpBufferedFrames * 10);
    doc["udp_audio_buffered_bytes"] = stream.udpBufferedBytes;
    doc["frames_received"] = stream.udpFramesReceived;
    doc["frames_lost"] = stream.udpFramesLost;
    doc["frames_late"] = stream.udpFramesLate;
    doc["underruns"] = stream.udpUnderruns;
    doc["first_audio_ms"] = stream.udpFirstAudioMs;
    doc["udp_last_end_reason"] = stream.udpLastEndReason;
    doc["udp_last_frames_received"] = stream.udpLastFramesReceived;
    doc["udp_last_frames_lost"] = stream.udpLastFramesLost;
    doc["udp_last_frames_late"] = stream.udpLastFramesLate;
    doc["udp_last_underruns"] = stream.udpLastUnderruns;
    doc["udp_last_first_audio_ms"] = stream.udpLastFirstAudioMs;
    doc["current_bytes"] = playback.currentBytes;
    doc["queued_pcm_bytes"] = playback.queuedPcmBytes;
    doc["queued_pcm_segments"] = playback.queuedPcmSegments;
    doc["audio_queue_depth"] = playback.audioQueueDepth;
    doc["download_queue_depth"] = playback.downloadQueueDepth;
    doc["download_in_flight"] = playback.downloadInFlight;
    doc["download_age_ms"] = playback.downloadAgeMs;
    doc["download_watchdog_ms"] = playback.downloadWatchdogMs;
    doc["started_ms"] = playback.startedMs;
    doc["deadline_ms"] = playback.deadlineMs;
    doc["mic_state"] = getMicStateName();
    doc["booth_mode"] = isBoothMode();
    doc["booth_mode_persisted"] = isBoothModePersisted();
    doc["mic_enabled"] = mic.enabled;
    doc["mic_running"] = mic.running;
    doc["mic_last_rms"] = mic.lastRms;
    doc["mic_recent_peak_rms"] = mic.recentPeakRms;
    doc["mic_last_frame_ms"] = mic.lastFrameMs;
    doc["mic_frame_count"] = mic.frameCount;
    doc["mic_record_failure_count"] = mic.recordFailureCount;
    doc["mic_trigger_count"] = mic.triggerCount;
    doc["mic_touch_trigger_count"] = mic.touchTriggerCount;
    doc["mic_recording_source"] = mic.recordingSource;
    doc["mic_stored_recording_count"] = mic.storedRecordingCount;
    doc["mic_resume_requested"] = playback.micResumeRequested;
    doc["servo_ready"] = servo.ready;
    doc["gesture_active"] = servo.gestureActive;
    doc["gesture"] = servo.gestureName;
    doc["free_heap"] = ESP.getFreeHeap();
    doc["free_psram"] = ESP.getFreePsram();
    doc["audio_gate_initialized"] = audio.initialized;
    doc["audio_gate_locked"] = audio.locked;
    doc["audio_gate_owner"] = audio.owner;
    doc["audio_gate_lock_count"] = audio.lockCount;
    doc["audio_gate_failed_acquire_count"] = audio.failedAcquireCount;
    doc["stack_watermark"] = audio.stackWatermark;

    String body;
    serializeJson(doc, body);
    server.send(200, "application/json", body);
}

static bool validateAudioSessionBody() {
    if (!server.hasArg("plain") || server.arg("plain").length() == 0) {
        return true;
    }
    JsonDocument req;
    if (deserializeJson(req, server.arg("plain")) != DeserializationError::Ok) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"json parse error\"}");
        return false;
    }
    const char* codec = req["codec"] | "pcm_s16le";
    int sampleRate = req["sample_rate"] | 24000;
    int channels = req["channels"] | 1;
    int sampleWidth = req["sample_width"] | 2;
    int frameMs = req["frame_ms"] | 10;
    if (strcmp(codec, "pcm_s16le") != 0 || sampleRate != 24000 ||
        channels != 1 || sampleWidth != 2 || frameMs != 10) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"unsupported audio session format\"}");
        return false;
    }
    return true;
}

static void handleAudioSessionStart() {
    if (!validateAudioSessionBody()) {
        return;
    }
    UdpPcmSessionResult result = beginUdpPcmSession();
    if (!result.success) {
        String body = "{\"success\":false,\"error\":\"";
        body += result.error;
        body += "\"}";
        server.send(409, "application/json", body);
        return;
    }

    JsonDocument doc;
    doc["success"] = true;
    doc["session"] = result.session;
    doc["transport"] = "udp";
    doc["codec"] = "pcm_s16le";
    doc["sample_rate"] = 24000;
    doc["channels"] = 1;
    doc["sample_width"] = 2;
    doc["frame_ms"] = PCM_UDP_FRAME_MS;
    doc["jitter_ms"] = PCM_UDP_START_FRAMES * PCM_UDP_FRAME_MS;
    doc["start_buffer_ms"] = PCM_UDP_START_FRAMES * PCM_UDP_FRAME_MS;
    doc["udp_port"] = result.port;
    doc["token"] = result.token;
    String body;
    serializeJson(doc, body);
    server.send(200, "application/json", body);
}

static void handleAudioSessionStop() {
    String prefix = "/audio/session/";
    String uri = server.uri();
    String sessionId = uri.startsWith(prefix) ? uri.substring(prefix.length()) : "";
    if (!stopUdpPcmSession(sessionId)) {
        server.send(404, "application/json", "{\"success\":false,\"error\":\"session not active\"}");
        return;
    }
    server.send(200, "application/json", "{\"success\":true}");
}

// ────────────────────────────────────────────
// POST /face
// body: {"face": "calm"|"thinking"|"happy"|"sleepy"}
// → Switch whale face expression
// ────────────────────────────────────────────
static void handleFace() {
    if (!server.hasArg("plain")) {
        // GET: return current face
        String body = "{\"face\":\"";
        body += getCurrentFaceName();
        body += "\"}";
        server.send(200, "application/json", body);
        return;
    }

    JsonDocument doc;
    if (deserializeJson(doc, server.arg("plain")) != DeserializationError::Ok) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"json parse error\"}");
        return;
    }

    const char* face = doc["face"] | "";
    WhaleFace wf;

    if (!whaleFaceFromName(face, &wf)) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"face must be calm/thinking/happy/sleepy/shy/smug/pouty\"}");
        return;
    }

    setWhaleFace(wf);
    Serial.printf("[HTTP] POST /face -> %s\n", face);
    server.send(200, "application/json", "{\"success\":true}");
}

// ────────────────────────────────────────────
// GET /snapshot
// → Capture JPEG from camera and return it
// ────────────────────────────────────────────
static void handleSnapshot() {
    const bool sessionRequired = server.hasArg("session") && server.arg("session") == "1";
    if (sessionRequired && !isCameraSessionActive()) {
        server.send(409, "application/json",
                    "{\"success\":false,\"error\":\"camera session is not active\"}");
        return;
    }

    uint8_t* jpgBuf = nullptr;
    size_t jpgLen = 0;

    if (!captureJpeg(&jpgBuf, &jpgLen, 80)) {
        server.send(500, "application/json", "{\"success\":false,\"error\":\"capture failed\"}");
        return;
    }

    const bool preview = !server.hasArg("preview") || server.arg("preview") != "0";
    if (preview && !showCameraPreview(jpgBuf, jpgLen)) {
        Serial.println("[HTTP] Camera preview unavailable; returning JPEG normally");
    }

    server.send_P(200, "image/jpeg", (const char*)jpgBuf, jpgLen);
    free(jpgBuf);
    Serial.printf("[HTTP] GET /snapshot -> %u bytes JPEG\n", (unsigned)jpgLen);
}

// POST /camera/session {"idle_timeout_ms": 5000}
// DELETE /camera/session
// Keep the GC0308 initialized for a bounded burst of host-side CV frames.
static void handleCameraSessionStart() {
    uint32_t idleTimeoutMs = 5000;
    if (server.hasArg("plain") && !server.arg("plain").isEmpty()) {
        JsonDocument doc;
        if (deserializeJson(doc, server.arg("plain")) != DeserializationError::Ok) {
            server.send(400, "application/json",
                        "{\"success\":false,\"error\":\"json parse error\"}");
            return;
        }
        idleTimeoutMs = doc["idle_timeout_ms"] | idleTimeoutMs;
    }

    if (!startCameraSession(idleTimeoutMs)) {
        server.send(503, "application/json",
                    "{\"success\":false,\"error\":\"camera session start failed\"}");
        return;
    }

    JsonDocument response;
    response["success"] = true;
    response["active"] = true;
    response["idle_timeout_ms"] = cameraSessionIdleTimeoutMs();
    String body;
    serializeJson(response, body);
    server.send(200, "application/json", body);
}

static void handleCameraSessionStop() {
    const bool restored = stopCameraSession();
    server.send(restored ? 200 : 500, "application/json",
                restored
                    ? "{\"success\":true,\"active\":false}"
                    : "{\"success\":false,\"error\":\"touch restore failed\"}");
}

static void handleCameraSessionStatus() {
    JsonDocument response;
    response["success"] = true;
    response["active"] = isCameraSessionActive();
    response["idle_timeout_ms"] = cameraSessionIdleTimeoutMs();
    String body;
    serializeJson(response, body);
    server.send(200, "application/json", body);
}

// GET /env/debug — QMP6988校准字节+原始ADC快照，电脑端验算补偿公式用
static void handleEnvDebug() {
    // 先触发一次读取，保证rawP/rawT快照是新鲜的
    float t, h, pr;
    readEnv(t, h, pr);
    server.send(200, "application/json", envDebugJson());
}

// ────────────────────────────────────────────
// GET /env
// → Temperature, humidity, and barometric pressure from onboard sensors
// ────────────────────────────────────────────
static void handleEnv() {
    if (!isEnvAvailable()) {
        server.send(200, "application/json",
                    "{\"success\":false,\"error\":\"no env sensor detected\"}");
        return;
    }

    float temperature = NAN, humidity = NAN, pressure = NAN;
    bool ok = readEnv(temperature, humidity, pressure);
    if (!ok) {
        server.send(500, "application/json",
                    String("{\"success\":false,\"error\":\"sensor read failed\",\"detail\":\"") + envLastError() + "\"}");
        return;
    }

    JsonDocument doc;
    doc["success"]     = true;
    // Use null in JSON for NAN (sensor absent or read error)
    if (isnan(temperature)) doc["temperature"] = nullptr;
    else                    doc["temperature"] = roundf(temperature * 10.0f) / 10.0f;
    if (isnan(humidity))    doc["humidity"]    = nullptr;
    else                    doc["humidity"]    = roundf(humidity * 10.0f) / 10.0f;
    if (isnan(pressure))    doc["pressure"]    = nullptr;
    else                    doc["pressure"]    = roundf(pressure * 10.0f) / 10.0f;

    JsonObject sensors = doc["sensors"].to<JsonObject>();
    sensors["sht31"]   = isEnvAvailable() && !isnan(humidity);   // humidity only from SHT31
    sensors["qmp6988"] = isEnvAvailable() && !isnan(pressure);   // pressure only from QMP6988

    String body;
    serializeJson(doc, body);
    Serial.printf("[HTTP] GET /env -> t=%.1f h=%.1f p=%.1f\n", temperature, humidity, pressure);
    server.send(200, "application/json", body);
}

// ────────────────────────────────────────────
// 公開関数
// ────────────────────────────────────────────

void initHttpServer() {
    static const char* headerKeys[] = {
        PCM_HEADER_SESSION,
        PCM_HEADER_SEQ,
        PCM_HEADER_FINAL,
        PCM_HEADER_MODE,
    };
    httpServer.collectHeaders(headerKeys, sizeof(headerKeys) / sizeof(headerKeys[0]));
    httpServer.on("/play",         HTTP_POST, handlePlay);
    httpServer.on("/play/pcm",     HTTP_POST, handlePlayPcm, handlePlayPcmRaw);
    httpServer.on("/audio/session", HTTP_POST, handleAudioSessionStart);
    httpServer.on(UriBraces("/audio/session/{}"), HTTP_DELETE, handleAudioSessionStop);
    httpServer.on("/mode",         HTTP_POST, handleMode);
    httpServer.on("/booth",        HTTP_GET, handleBoothStatus);
    httpServer.on("/booth",        HTTP_POST, handleBoothSet);
    httpServer.on("/audio/status", HTTP_GET,  handleAudioStatus);
    httpServer.on("/audio",        HTTP_GET,  handleAudio);
    httpServer.on("/move",         HTTP_POST, handleMove);
    httpServer.on("/home",         HTTP_POST, handleHome);
    httpServer.on("/nod",          HTTP_POST, handleNod);
    httpServer.on("/shake",        HTTP_POST, handleShake);
    httpServer.on("/servo/status", HTTP_GET,  handleServoStatus);
    httpServer.on("/touch/status", HTTP_GET,  handleTouchStatus);
    httpServer.on("/playback/status", HTTP_GET, handlePlaybackStatus);
    httpServer.on("/snapshot",     HTTP_GET,  handleSnapshot);
    httpServer.on("/camera/session", HTTP_POST, handleCameraSessionStart);
    httpServer.on("/camera/session", HTTP_DELETE, handleCameraSessionStop);
    httpServer.on("/camera/status", HTTP_GET, handleCameraSessionStatus);
    httpServer.on("/face",         HTTP_POST, handleFace);
    httpServer.on("/face",         HTTP_GET,  handleFace);
    httpServer.on("/env",          HTTP_GET,  handleEnv);
    httpServer.on("/env/debug",    HTTP_GET,  handleEnvDebug);
    httpServer.begin();
    Serial.println("[HTTP] Server started on port 80");
}

void handleHttpServer() {
    httpServer.handleClient();
}

void executeFirmwareCommand(FirmwareCommand& command) {
    struct Route {
        const char* method;
        const char* path;
        void (*handler)();
    };
    static const Route routes[] = {
        {"GET", "/status", handlePlaybackStatus},
        {"GET", "/playback/status", handlePlaybackStatus},
        {"GET", "/env", handleEnv},
        {"GET", "/face", handleFace},
        {"GET", "/snapshot", handleSnapshot},
        {"GET", "/audio/status", handleAudioStatus},
        {"GET", "/audio", handleAudio},
        {"POST", "/mode", handleMode},
        {"GET", "/booth", handleBoothStatus},
        {"POST", "/booth", handleBoothSet},
        {"POST", "/face", handleFace},
        {"POST", "/move", handleMove},
        {"POST", "/home", handleHome},
        {"POST", "/nod", handleNod},
        {"POST", "/shake", handleShake},
        {"POST", "/play/pcm", handlePlayPcm},
    };
    server.outbound = &command;
    const char* method = command.request["method"] | "";
    const char* path = command.request["path"] | "";
    bool found = false;
    for (const Route& route : routes) {
        if (strcmp(method, route.method) == 0 && strcmp(path, route.path) == 0) {
            route.handler();
            found = true;
            break;
        }
    }
    if (!found) {
        server.send(404, "application/json", "{\"success\":false,\"error\":\"unsupported outbound route\"}");
    }
    server.outbound = nullptr;
}
