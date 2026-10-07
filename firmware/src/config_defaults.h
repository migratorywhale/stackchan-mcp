#ifndef STACKCHAN_CONFIG_DEFAULTS_H
#define STACKCHAN_CONFIG_DEFAULTS_H

// Public, non-secret defaults used by CI and first-time builds.
// Local deployments may override any value from firmware/src/config.h.

#ifndef WIFI_NETWORK_COUNT
#define WIFI_NETWORK_COUNT 2
#endif

#ifndef WIFI_SSID_0
#define WIFI_SSID_0 "your-ssid"
#endif
#ifndef WIFI_PASSWORD_0
#define WIFI_PASSWORD_0 "your-password"
#endif
#ifndef WIFI_SSID_1
#define WIFI_SSID_1 "your-hotspot-ssid"
#endif
#ifndef WIFI_PASSWORD_1
#define WIFI_PASSWORD_1 "your-hotspot-password"
#endif

#ifndef SPEAKER_VOLUME
#define SPEAKER_VOLUME 200
#endif

#ifndef MIC_SAMPLE_RATE
#define MIC_SAMPLE_RATE 16000
#endif
#ifndef MIC_MAX_RECORD_SECONDS
#define MIC_MAX_RECORD_SECONDS 8
#endif
#ifndef MIC_TRIGGER_RMS
#define MIC_TRIGGER_RMS 0.0095f
#endif
#ifndef MIC_TRIGGER_HOLD_MS
#define MIC_TRIGGER_HOLD_MS 280
#endif
#ifndef MIC_SILENCE_RMS
#define MIC_SILENCE_RMS 0.0020f
#endif
#ifndef MIC_SILENCE_HOLD_MS
#define MIC_SILENCE_HOLD_MS 1500
#endif
#ifndef MIC_FRAME_SAMPLES
#define MIC_FRAME_SAMPLES 1600
#endif
#ifndef MIC_MIN_VALID_SAMPLES
#define MIC_MIN_VALID_SAMPLES 5200
#endif
#ifndef MIC_VOICE_CONFIRM_RMS
#define MIC_VOICE_CONFIRM_RMS 0.004f
#endif
#ifndef PRE_TRIGGER_BUFFER_SAMPLES
#define PRE_TRIGGER_BUFFER_SAMPLES 4800
#endif
#ifndef MIC_MAGNIFICATION
#define MIC_MAGNIFICATION 2
#endif
#ifndef MIC_NOISE_FILTER_LEVEL
#define MIC_NOISE_FILTER_LEVEL 1
#endif

#ifndef NOTIFICATION_CHECK_INTERVAL
#define NOTIFICATION_CHECK_INTERVAL 60000
#endif

#ifndef HTTP_TIMEOUT_CHAT
#define HTTP_TIMEOUT_CHAT 30000
#endif
#ifndef HTTP_TIMEOUT_SHORT
#define HTTP_TIMEOUT_SHORT 5000
#endif

#ifndef DISPLAY_BRIGHTNESS
#define DISPLAY_BRIGHTNESS 70
#endif

// Touch: when set to 1, the firmware still detects taps, holds and swipes and
// still publishes them on /touch/status, but it no longer changes the face or
// moves the servos on its own. The host agent becomes the only thing that
// decides what a gesture means. Default 0 keeps the built-in petting reaction.
// Override from firmware/src/config.h for a given build.
#ifndef TOUCH_AGENT_DECIDES
#define TOUCH_AGENT_DECIDES 0
#endif

#endif
