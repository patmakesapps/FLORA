# Flora

A standalone Raspberry Pi 5 desk voice companion using OpenAI Realtime.
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

## Voice behavior

The Pi connects directly to Realtime; no laptop or local hardware WebSocket.
Server speech detection ends your turn automatically. A clock tool supplies the
real local/UTC time. Edit `persona.md` to adjust how Flora speaks.

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
purple while listening to that turn. Below **15%** battery charge, idle turns
red. Speaking stays green and active listening stays purple.
The X1200 gauge is read every 5 seconds through I2C bus 1 at address 0x36.
If the gauge is unavailable, battery charge is unknown; no low-charge value
is invented. This feature only changes the ring color.
`led-test` also previews red idle using a simulated 14% reading.

Gauge reference: [Suptronics X120x monitoring code](https://github.com/suptronics/x120x/blob/main/qtx120x.py).

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

The audio resampler is adapted from this machine's Humalien implementation.
ALSA format and buffer timing follow its working hardware node. GPIO wiring,
ring rendering, session coordination, and configuration belong to this project.

API reference: [OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations).
The Pi 5 LED driver dependency uses GPL-2.0-only; retain its license when distributing.
