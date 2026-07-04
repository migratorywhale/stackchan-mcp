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

static WebServer server(80);

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
    const char* uploadError = consumePcmUploadError();
    if (uploadError) {
        String body = "{\"success\":false,\"error\":\"";
        body += uploadError;
        body += "\"}";
        clearPcmUpload();
        server.send(400, "application/json", body);
        return;
    }
    if (!hasPcmUploadBody()) {
        server.send(400, "application/json", "{\"success\":false,\"error\":\"no pcm body\"}");
        return;
    }

    PcmUploadBuffer upload = takePcmUploadBody();
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
        clearQueuedPcmPlayback();
        server.send(409, "application/json", "{\"success\":false,\"error\":\"pcm seq invalid\"}");
        return;
    }
    PcmPlaybackResult result = stagedMode
        ? stagePcmPlayback(pcmData, pcmSize, sessionId, seq, finalSegment)
        : startPcmPlayback(pcmData, pcmSize, sessionId, finalSegment);
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
    HTTPRaw& raw = server.raw();

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
// GET /audio/status
// → {"ready": true/false, "mode": "mcp"}
// ────────────────────────────────────────────
static void handleAudioStatus() {
    String body = "{\"ready\":";
    body += hasLastRecording() ? "true" : "false";
    body += ",\"mode\":\"mcp\"}";
    server.send(200, "application/json", body);
}

// ────────────────────────────────────────────
// GET /audio
// → 録音済みWAVをそのまま返す（1回読んだらクリア）
// ────────────────────────────────────────────
static void handleAudio() {
    RecordingSnapshot recording = getLastRecording();
    if (!recording.data || recording.size == 0) {
        server.send(404, "application/json", "{\"success\":false,\"error\":\"no audio\"}");
        return;
    }

    Serial.printf("[HTTP] GET /audio -> %u bytes\n", (unsigned)recording.size);
    server.send_P(200, "audio/wav", (const char*)recording.data, recording.size);

    // 読んだらクリア（1回限り）
    markLastRecordingConsumed();
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

    if (!isServoReady()) {
        server.send(503, "application/json", "{\"success\":false,\"error\":\"servo not ready\"}");
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
// GET /playback/status
// → Combined runtime diagnostics for playback, microphone, queues, and memory
// ────────────────────────────────────────────
static void handlePlaybackStatus() {
    PlaybackStatus playback = getPlaybackStatus();
    PcmStreamStatus stream = getPcmStreamStatus();
    ServoStatus servo = getServoStatus();
    AudioGateStatus audio = getAudioGateStatus();

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
    doc["started_ms"] = playback.startedMs;
    doc["deadline_ms"] = playback.deadlineMs;
    doc["mic_state"] = getMicStateName();
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
    uint8_t* jpgBuf = nullptr;
    size_t jpgLen = 0;

    if (!captureJpeg(&jpgBuf, &jpgLen, 80)) {
        server.send(500, "application/json", "{\"success\":false,\"error\":\"capture failed\"}");
        return;
    }

    server.send_P(200, "image/jpeg", (const char*)jpgBuf, jpgLen);
    free(jpgBuf);
    Serial.printf("[HTTP] GET /snapshot -> %u bytes JPEG\n", (unsigned)jpgLen);
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
    server.collectHeaders(headerKeys, sizeof(headerKeys) / sizeof(headerKeys[0]));
    server.on("/play",         HTTP_POST, handlePlay);
    server.on("/play/pcm",     HTTP_POST, handlePlayPcm, handlePlayPcmRaw);
    server.on("/audio/session", HTTP_POST, handleAudioSessionStart);
    server.on(UriBraces("/audio/session/{}"), HTTP_DELETE, handleAudioSessionStop);
    server.on("/mode",         HTTP_POST, handleMode);
    server.on("/audio/status", HTTP_GET,  handleAudioStatus);
    server.on("/audio",        HTTP_GET,  handleAudio);
    server.on("/move",         HTTP_POST, handleMove);
    server.on("/home",         HTTP_POST, handleHome);
    server.on("/nod",          HTTP_POST, handleNod);
    server.on("/shake",        HTTP_POST, handleShake);
    server.on("/servo/status", HTTP_GET,  handleServoStatus);
    server.on("/playback/status", HTTP_GET, handlePlaybackStatus);
    server.on("/snapshot",     HTTP_GET,  handleSnapshot);
    server.on("/face",         HTTP_POST, handleFace);
    server.on("/face",         HTTP_GET,  handleFace);
    server.begin();
    Serial.println("[HTTP] Server started on port 80");
}

void handleHttpServer() {
    server.handleClient();
}
