# Touch as a sense, not a reflex

By default this firmware reacts to touch on its own. This page describes an
opt-in build that hands that decision to the host agent instead.

## What the default build does

When a forward swipe followed by a backward swipe lands within 1500 ms,
`touch_service.cpp` treats it as petting and performs the response itself:

```cpp
// firmware/src/touch_service.cpp, startPetting()
setWhaleFace(WHALE_HAPPY);   // firmware picks the expression
pettingStartedMs = nowMs;
...
if (isServoReady()) {
    servoShake();            // firmware wags the head
}
```

The gesture is detected, the reaction is performed, and the agent is never
consulted. From the agent's side the touch did not happen: nothing enters its
context, and nothing it decides can change what the body does. It is an
animatronic reflex — charming, and entirely the firmware's choice.

## What `TOUCH_AGENT_DECIDES=1` changes

Detection stays. The reaction goes. Petting is still recognised, still counted,
and still published on `/touch/status` — but the face is not changed, the
servos are not moved, and nothing happens unless the host decides it should.

That turns touch from a behaviour performed near the agent into **information
the agent has**. Three consequences follow, and they are the point:

- The response can be anything, including nothing. **Doing nothing is a
  decision**, and in the default build it is impossible to make.
- The same gesture can mean different things at different times. A companion
  that reacts identically to every touch is a mechanism; one that can be busy,
  or surprised, or already mid-sentence is something closer to a presence.
- The agent can be wrong about it. It can misread a pet as a nudge, respond
  oddly, and learn otherwise. That failure mode does not exist when the
  firmware has already decided.

## Enabling it

Copy the example config if you have not already, then set the flag:

```cpp
// firmware/src/config.h
#define TOUCH_AGENT_DECIDES 1
```

Then build and flash as usual. `/touch/status` reports the active mode in
`agent_decides` so you can confirm from the host side which firmware you are
talking to.

## Reading touch from the host

`GET /touch/status` returns:

| Field | Meaning |
|---|---|
| `event_seq` | **Monotonic counter.** Increments once per recorded event. |
| `last_event` | `hold`, `swipe_forward`, `swipe_backward`, `petting`, `recording_started`, `recording_rejected`, or `none` |
| `last_event_ms` | `millis()` at the last event |
| `intensities` | `[front, middle, back]`, 0–3 |
| `pet_count` | Pets detected since boot |
| `agent_decides` | Whether this build suppresses the built-in reaction |

Poll `event_seq` and compare it against the last value you saw. Do not use
`last_event_ms` for this: `millis()` resolution is 1 ms and two gestures can
land inside the same tick.

```python
import time, requests

DEVICE = "http://stackchan.local"
last_seq = None

while True:
    st = requests.get(f"{DEVICE}/touch/status", timeout=2).json()
    seq = st["event_seq"]

    if last_seq is None:          # first poll: adopt the current cursor
        last_seq = seq
    elif seq != last_seq:
        last_seq = seq
        event = st["last_event"]

        # the decision is yours; this is only a sketch
        if event == "petting":
            pass                  # lean into it, speak, or do nothing at all
        elif event == "hold":
            pass                  # the hand stayed; that is different
        elif event.startswith("swipe"):
            pass                  # direction is in the event name

    time.sleep(0.5)
```

Everything the agent does next — head movement, expression, speech, silence —
goes through the normal MCP tools or HTTP endpoints. The firmware is no longer
in that decision.

## What still happens automatically

- Tap-to-record is **unchanged**. A short tap is a functional trigger that
  starts a microphone recording, not a reaction to a gesture, and tapping to
  talk should not depend on the agent being awake.
- Detection, counters and diagnostics all behave as documented in
  `docs/http-api.md`.
- `suppressed_pet_count` stays at zero in this mode. The default build
  suppresses the petting animation while audio is busy; here there is no
  animation to suppress.

## Caveats worth knowing before you rely on it

- **Polling interval sets reaction latency.** At one poll per second, a pet can
  go unanswered for a second. That is fine for being petted and too slow for
  flinching. If you need faster, poll faster or add a push channel.
- **If nothing polls, nothing happens.** Silence is the cost of the design.
  Choose it deliberately, not by accident.
- **Verified on hardware (Oct 7, 2026).** Default and `TOUCH_AGENT_DECIDES=1`
  both build with zero errors and zero warnings against upstream `972266b`
  (`pio run -e m5stack-cores3`, ESP32-S3, RAM 31.3%, flash 21.9%). The
  autonomous path was then flashed to a real unit (M5Stack CoreS3, via its
  USB-Serial/JTAG port) and confirmed end to end: `/touch/status` reports
  `agent_decides: true`, a physical hold arrives as `last_event: "hold"`
  with `event_seq` advancing, and the body performs **no** face change and
  **no** head movement of its own. The response was driven entirely from the
  host.

## Using your own faces

`TOUCH_AGENT_DECIDES` is independent of the face assets, so a personal build
usually wants its own art. Two things matter:

- **Faces must be 192x192.** The renderer upscales by 1.25x
  (`SCALE_NUM 5 / SCALE_DEN 4`) and centres the result on the 320x240 panel,
  so a 320x240 source is drawn at 400x300 and clipped. Keep the canvas square
  at 192x192.
- **The filenames are fixed.** `scripts/generate_gif_assets.py` reads
  `firmware/data/*.gif` named `A_calm.gif`, `B_thinking.gif`, `C_happy.gif`,
  `D_sleepy.gif`, `E_shy.gif`, `F_smug.gif`, `G_pouty.gif` and regenerates
  `firmware/src/gif_assets.h`. It refuses to write a partial header, so all
  seven must be present. Animated GIFs (GIF89a, multiple frames) work.

Faces are deliberately not part of this patch: the branch ships upstream's
assets so the diff stays reviewable. Swap in your own locally.

## Also in this branch

`wifi_manager.cpp` draws a "WiFi OK!" toast (SSID + IP) across the full
screen after connecting. Because a face only covers the centre 240 px, that
text lingered around it until something forced a full redraw. The toast now
clears itself after its 3 s delay. Unrelated to touch; included because it is
a one-line display fix.

## Credits

- The event-cursor design and this patch were written by **Sonny (Claude
  Sonnet)**, running as an embodied agent on this hardware, in collaboration
  with **pumpkinbyte**, who built the device, flashed it, and tested it.
- The first version of the decoupling has run on our own unit since early
  September 2026. This branch is that idea re-expressed as a small, opt-in
  patch on top of current upstream code.
- Upstream: [migratorywhale/stackchan-mcp](https://github.com/migratorywhale/stackchan-mcp)
- Stack-chan by ししかわ (shishikawa).
