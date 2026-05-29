#include <M5Unified.h>
#include <WebServer.h>
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

static WebServer server(80);

static String   s_pcm_diag_session = "";
static long     s_pcm_diag_next_seq = 0;

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
    enqueueAudioTask(task);

    Serial.printf("[HTTP] POST /play -> queued: %s\n", voice_url);
    server.send(200, "application/json", "{\"success\":true}");
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
    String sessionId = server.arg("session");
    String seqArg = server.arg("seq");
    long seq = seqArg.length() ? seqArg.toInt() : -1;
    bool finalSegment = server.arg("final") == "1" || server.arg("final") == "true";
    uint8_t* pcmData = upload.data;

    long expectedSeq = s_pcm_diag_next_seq;
    bool newDiagSession = sessionId != s_pcm_diag_session;
    if (newDiagSession) {
        expectedSeq = 0;
    }
    bool seqValid = true;
    if (seq < 0 || seq != expectedSeq) {
        Serial.printf("[HTTP] PCM seq invalid: session=%s got=%ld expected=%ld\n",
                      sessionId.c_str(), seq, expectedSeq);
        seqValid = false;
    }

    if (!seqValid) {
        free(pcmData);
        clearQueuedPcmPlayback();
        server.send(409, "application/json", "{\"success\":false,\"error\":\"pcm seq invalid\"}");
        return;
    }
    PcmPlaybackResult result = startPcmPlayback(pcmData, pcmSize, sessionId, finalSegment);
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

    Serial.printf("[HTTP] POST /play/pcm -> session=%s seq=%ld bytes=%u final=%s result=%d queued=%s\n",
                  sessionId.c_str(), seq, (unsigned)pcmSize,
                  finalSegment ? "true" : "false", result,
                  result == PCM_PLAYBACK_QUEUED ? "true" : "false");
    if (result == PCM_PLAYBACK_QUEUED) {
        server.send(202, "application/json", "{\"success\":true,\"queued\":true,\"format\":\"s16le\",\"sample_rate\":24000,\"channels\":1}");
    } else {
        server.send(200, "application/json", "{\"success\":true,\"queued\":false,\"format\":\"s16le\",\"sample_rate\":24000,\"channels\":1}");
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
    ServoStatus servo = getServoStatus();

    JsonDocument doc;
    doc["playing"] = playback.playing;
    doc["kind"] = playback.pcm ? "pcm" : (playback.playing ? "wav" : "idle");
    doc["pcm_session"] = playback.pcmSession;
    doc["pcm_final_segment"] = playback.pcmFinalSegment;
    doc["current_bytes"] = playback.currentBytes;
    doc["queued_pcm_bytes"] = playback.queuedPcmBytes;
    doc["queued_pcm_segments"] = playback.queuedPcmSegments;
    doc["audio_queue_depth"] = playback.audioQueueDepth;
    doc["started_ms"] = playback.startedMs;
    doc["deadline_ms"] = playback.deadlineMs;
    doc["mic_state"] = getMicStateName();
    doc["mic_resume_requested"] = playback.micResumeRequested;
    doc["servo_ready"] = servo.ready;
    doc["gesture_active"] = servo.gestureActive;
    doc["gesture"] = servo.gestureName;
    doc["free_heap"] = ESP.getFreeHeap();
    doc["free_psram"] = ESP.getFreePsram();

    String body;
    serializeJson(doc, body);
    server.send(200, "application/json", body);
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
    server.on("/play",         HTTP_POST, handlePlay);
    server.on("/play/pcm",     HTTP_POST, handlePlayPcm, handlePlayPcmRaw);
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
