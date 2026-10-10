# Flora

A standalone Raspberry Pi 5 robotic plant companion using OpenAI Realtime
and live light, soil capacitance, temperature, and humidity measurements.
Everything runs from this repo; Humalien and LumaKit are not runtime dependencies.
Voice reasoning is online. Microphone audio reaches OpenAI while `run` is active.
The default model is `gpt-realtime-2.1-mini`, the lower-cost Realtime option.
The default voice is `shimmer`.

## Bench hardware

- Waveshare WM8960 audio HAT: 48 kHz stereo PCM16 capture and playback.
- One 12-pixel GRB NeoPixel ring: 5 V on physical pin 4, ground on pin 6,
  DIN on **GPIO12 / physical pin 32**.
- Default brightness: **5/255 per active channel**, the dim purple test setting.
  Configuration cannot exceed 63/255 (about 25%).
- I2C bus 1 sensors: VEML7700 light at **0x10**, Adafruit STEMMA soil sensor
  (#4026, seesaw) at **0x36**, and SHT41 temperature/humidity at **0x44**.
  The WM8960 audio HAT uses **0x1A**; `UU` at that address means the kernel
  driver owns it and is expected.
- Light sensor sits behind a thin, partly transparent PETG window. The soil
  probe is currently outside soil; placement defaults to `false` in configuration.
- No camera needed.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Add your OpenAI API key to `OPENAI_API_KEY` in `.env`. Keep it out of chat and Git.
Model, voice, software speaker gain, and LED count/brightness are configurable.
The hub temporarily sets WM8960 `Capture` to 23/63 (0 dB) to reduce clipping,
and restores the previous mixer volume on clean shutdown. Adjust
`MIC_CAPTURE_VOLUME` in `.env` if needed. No ALSA state file is written.

```bash
.venv/bin/python -m flora check
.venv/bin/python -m flora sensor-test
.venv/bin/python -m flora led-test
.venv/bin/python -m flora audio-test
.venv/bin/python -m flora voice-test
.venv/bin/python -m flora run
```

`audio-test` measures 3 seconds of live mic input and plays a quiet test tone.
It saves no recording and makes no API calls. `led-test` shows four states,
then clears the ring. Press Ctrl+C to stop any command.
`voice-test` plays one Realtime greeting with the configured model and voice,
sends no microphone input, and then disconnects. It uses the API and is billed.
`sensor-test` prints a single live JSON snapshot from all three sensors, including
units, per-sensor status, UTC measurement timestamps, and placement limitations.
It requires no API key and does not start audio or LEDs. It exits with code 0
when all sensors read successfully, or 1 if any reading is unavailable; healthy
readings remain visible when another sensor fails. No production readings are simulated.

## Sensors and plant status

The installed Adafruit drivers are `adafruit-circuitpython-veml7700`,
`adafruit-circuitpython-seesaw`, and `adafruit-circuitpython-sht4x`, using Blinka
on the Pi. Existing installations can add them without recreating `.env`:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m flora sensor-test
```

`requirements.lock.txt` captures the verified local dependency versions, including
the new drivers. Use it in place of `requirements.txt` for a pinned installation.
I2C must be enabled and the runtime user must have access to `/dev/i2c-1`
(normally through the `i2c` group). Your successful bus scan already confirms
the three devices are connected. Stop older FLORA processes before testing
or restarting with this version, so obsolete battery polling cannot continue.

The Realtime session exposes `get_plant_status` with no arguments. Questions
such as "How's my plant doing?" or "How much light am I getting?" trigger live
reads. The output contains:

| Section | Measurements | Interpretation |
| --- | --- | --- |
| `light` | `lux`, `raw_als` | At the sensor behind PETG; no window transmission correction |
| `soil` | `raw_capacitance` | Raw capacitive counts, never water-content percentage |
| `environment` | `temperature_c`, `relative_humidity_percent` | Air at the SHT41, potentially affected by enclosure/Pi heat |
| `plant`, `calibration`, `watering` | Saved plant details, calibration anchors, recommendation/reason | Pot-specific watering advice once calibrated |

Keep `SOIL_PROBE_IN_SOIL=false` while the probe sits outside a plant. After
installing it in soil, tell FLORA "The probe is now in my plant's soil." It saves
placement in the plant profile without a restart. Alternatively, set
`SOIL_PROBE_IN_SOIL=true` in `.env` and restart. Saved profile placement takes
precedence over the initial environment setting. Placement alone does **not**
calibrate moisture; the workflow below learns a benchmark for the actual pot.

The VEML7700 uses automatic gain/integration adjustment and checks for saturation;
light at the leaves and daily light exposure cannot be inferred from one snapshot.
The PETG window's transmission is unknown, so no multiplier is applied. Compare
readings with and without the window under steady lighting if practical before
calibrating it. SHT41 uses a heater-free, high-precision, CRC-checked measurement.
`persona.md` instructs FLORA to use successful live results, explain unavailable
readings, and avoid invented measurements, trends, or health diagnoses.

Missing drivers, I2C errors, invalid values, and device/CRC errors are reported
per sensor with `status: "unavailable"` and `null` measurements. A failed sensor
is reinitialized and retried once; a later request can recover without restarting
voice. No previous value is substituted for a failed reading. Each sensor hardware
snapshot or soil-sampling request has a five-second timeout. A timed-out I2C worker remains tracked until it finishes,
and further calls return `read_in_progress` instead of starting more workers.

The X1200 battery reader, polling task, configuration, and battery LED override
have been removed. Old `BATTERY_ENABLED` entries in `.env` are ignored. Address
`0x36` is used only by the STEMMA driver; no fuel-gauge register reads remain.

## Memory and plant setup

FLORA stores its own SQLite database at `data/flora.sqlite3`, inside this repo.
It uses Python's built-in `sqlite3`, WAL journaling, bounded lock waits, parameterized
queries, and separate connections per operation. Database work runs in threads
during conversations. `data/` is ignored by Git. It never imports LumaKit or opens
LumaKit's database; LumaKit's timestamped remember/recall/update/delete pattern
informed the local implementation.

Try saying:

- "My name is [your name]. Remember that."
- "My plant is a pothos called Pip. It lives on my desk."
- "What kind of plant do I have?"
- "Do you remember my name?"
- "Remember that I prefer being called [preferred name] instead."
- "Forget the fact about my name."

`remember` upserts a fact under a consistent subject, `recall` retrieves up to 20
matching/recent facts, and `forget` deletes the requested subject. `set_plant_profile`
saves the one current plant's name, species/type, location, potting mix, notes, and
probe placement. Unspecified fields are preserved. `get_plant_status` retrieves
the profile automatically, so FLORA can use the plant type you already told it.
Plant profile, facts, and calibration survive process restarts and voice reconnects.
This is shared memory for one robot, without voice identification or user accounts.
No conversation recordings/transcripts are stored. Inspect it without starting voice:

```bash
.venv/bin/python -m flora memory
```

## Soil calibration with the actual plant

**Wait until the probe is in the actual potting mix.** A reading in air tests the
sensor but is not a useful watering baseline. First tell FLORA the plant's type,
and that its probe is inserted. Keep insertion depth and position consistent.
Then collect two explicitly confirmed real conditions, in either order:

1. **Ready for watering:** manually check that the soil is at the appropriate
   watering point for this plant. Say "I've checked the soil; it's ready for
   watering. Record the watering point." This is `water_now`, not bone-dry soil
   or a universal number for all species.
2. **Watered and drained:** water normally, let excess water drain and readings
   settle, then say "It's watered, drained, and settled. Record the watered point."
   This is `watered`. If a newly bought plant is already wet, the watering-point
   observation may need to wait for its normal drying cycle.

Each capture takes five live soil readings, saves their median/spread and timestamp,
and uses no model-selected numerical anchors. It rejects captures with an outside-soil
probe, missing plant type, failed readings, excessive variation, or indistinct/reversed
anchors. The watered count must exceed the watering-point count by more than the noise
sanity margin. Counts remain counts: this sensor does not provide calibrated volumetric
water content. See [Adafruit's calibration FAQ](https://learn.adafruit.com/adafruit-stemma-soil-sensor-i2c-capacitive-moisture-sensor/faq).

After both anchors are saved, current raw counts below the learned watering point
produce `water`, counts above it produce `wait`, and a small uncertainty band around
it produces `check_soil`. The band uses recorded noise and 5% of the observed span;
it is a noise guard, not a moisture percentage or a universal care target. FLORA can
say "I'd water now" or "No need to water yet" with this measured basis. Missing
readings, incomplete calibration, an outside-soil probe, or unknown plant type produce
`unknown`, so it offers setup or a manual check. No watering actuator is present.

Reset calibration when changing plants, repotting, or moving/reinserting the probe.
Species, potting mix, and saved probe-placement changes automatically clear anchors;
other physical changes require saying "Reset the soil calibration." To capture from
the CLI instead of voice, **invoking the named command asserts that physical condition**:

```bash
.venv/bin/python -m flora calibrate-soil water-now
.venv/bin/python -m flora calibrate-soil watered
.venv/bin/python -m flora calibrate-soil reset
```

These commands require saved plant details/placement (or the environment placement
flag), read only live soil data, and never start audio or use the OpenAI API. No
physical soil calibration has been performed while the probe is outside the pot.

## Raspberry Pi acceptance checks

These checks require the real Pi, sensors, enclosure, audio HAT, and ring:

1. Run `sensor-test` several times. Confirm all three statuses are `ok`, readings
   have timestamps, and the outside-soil probe is identified correctly. Cover
   and uncover the light window and check that lux changes; inspect temperature
   and humidity for plausible values, including after the Pi warms up.
2. Once a plant is installed, save its type and probe placement, and follow the
   calibration workflow above before trusting watering recommendations. Do not immerse
   the probe's electronics or treat the outside-soil reading as a dry-soil baseline.
3. Run `audio-test`, `led-test`, and `voice-test` to confirm microphone, speakers,
   and the four normal ring states still work. Then run FLORA and ask "How's my
   plant doing?", "What's the humidity?", and "Does my plant need water?" Verify
   the spoken values come from live measurements and soil/window limits are respected.
4. Power down, disconnect one sensor, and restart. Check that `sensor-test` shows
   the other readings and exits 1, and voice explains the missing measurement.
   Power down before reconnecting, then confirm reads recover. Also speak another
   question while FLORA is obtaining a reading and confirm audio stays responsive.

Driver/unit tests cannot establish actual bus stability, PETG transmission,
soil calibration, enclosure heat effects, or the model's live spoken behavior.
The real three-sensor CLI and the actual Realtime function-call/continuation flow
have been smoke-tested on this Pi without starting microphone/speaker playback.
Realtime remember/profile tools and recall after reconnect were also verified
using synthetic facts in a temporary database, removed after the test.

Driver references: [VEML7700](https://docs.circuitpython.org/projects/veml7700/en/latest/api.html),
[seesaw](https://docs.circuitpython.org/projects/seesaw/en/latest/api.html),
and [SHT4x](https://docs.circuitpython.org/projects/sht4x/en/latest/api.html).

## Voice behavior

The Pi connects directly to Realtime; no laptop or local hardware WebSocket.
Server speech detection ends your turn automatically. Flora currently has no
clock, reminder, or scheduling capability. Edit `persona.md` to adjust how Flora
speaks.

The microphone is suppressed during actual speaker playback plus a short echo
tail. This prevents self-conversation without an acoustic echo canceller, but
means you **cannot interrupt a spoken reply** in this first version. There is
no wake word yet: speak naturally while running; stop the process to stop capture.
Session failures reconnect with bounded backoff; a new session loses conversation
context. Audio and LEDs are released on shutdown. Credentials and conversation
transcripts are not logged.

LED states:

| State | Ring |
| --- | --- |
| Idle (bench preview) | Solid dim purple |
| Listening | Slow purple pulse |
| Thinking | Purple circle sweep |
| Speaking (actual playback) | Green |
| Connecting | Amber circle sweep |
| Connection error | Dim red |
| Stopped | Off |

The session rests in solid purple idle until it detects speech, then pulses
purple while listening to that turn. Speaking stays green. The ring remains
purple/thinking while a plant-status tool request is in progress.

## Boot service

`deploy/flora.service` is a prepared unit for this Pi. It is not installed or
enabled automatically. After confirming live voice works, installing it requires
a system change outside this repo:

```bash
sudo cp deploy/flora.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flora
journalctl -u flora -f
```

## Development

```bash
.venv/bin/python -m unittest discover -s tests -v
```

`flora/hub.py` coordinates independent microphone, Realtime event, playback,
indicator, and tool-worker tasks. `flora/realtime.py` defines the session/tool
schema and WebSocket transport. `flora/sensors.py` lazily opens one shared bus
and performs on-demand readings in a thread; it survives voice reconnects.
`flora/memory.py` owns the local SQLite schema and persistence;
`flora/tools.py` pairs each function schema with its handler in one explicit
registry used for both Realtime tool definitions and local dispatch. It combines
profile/live readings and derives watering advice from pot-specific calibration.
Completed function calls are queued without awaiting hardware in the event
receiver. The tool worker returns JSON with the matching `call_id`, then requests
one continuation after all outputs. It avoids a competing continuation if a new
voice turn has already started, and is cancelled on session shutdown. Audio PCM,
resampling, echo gating, and normal ring animations retain their working behavior.
Playback buffers 120 ms at the start of each speech burst and feeds audio up to
60 ms ahead on an absolute PCM timeline, preventing cumulative sleep delays from
starving ALSA. Hardware underruns appear as warnings in the console/service log.

Tests mock the hardware only in `tests/` and cover driver interfaces/addresses,
partial failures/recovery, invalid values, timeouts, cancellation, probe placement,
CLI output/exit codes, tool-call outputs/continuations, and audio delivery during
slow tool reads, alongside the existing audio and LED regression checks. Memory
tests use temporary databases to verify restart persistence, corrections/deletion,
literal keyword search, concurrent connections, and calibration invalidation;
watering tests cover missing/noisy/reversed anchors and threshold uncertainty.

The audio resampler is adapted from this machine's Humalien implementation.
ALSA format and hardware buffer settings follow its working hardware node. GPIO wiring,
ring rendering, session coordination, and configuration belong to this project.

API reference: [OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations).
The Pi 5 LED driver dependency uses GPL-2.0-only; retain its license when distributing.
