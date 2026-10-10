import asyncio
import base64
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np

from flora.audio import Audio, Playback, apply_gain, BYTES_PER_SECOND, PLAYBACK_PREROLL
from flora.audio_adapter import PiToModelAudio, ModelToPiAudio
from flora.config import Config
from flora.hub import Session, indicator_state
from flora.leds import animation_frame
from flora.realtime import session_update


class AudioTests(unittest.TestCase):
    def test_resampling_keeps_odd_split_alignment_and_duration(self):
        source = np.full(48000 * 2, 1000, dtype="<i2").tobytes()
        adapter = PiToModelAudio()
        result = adapter.convert(source[:1001]) + adapter.convert(source[1001:], final=True)
        self.assertEqual(len(result), 24000 * 2)
        np.testing.assert_allclose(np.frombuffer(result, dtype="<i2")[100:-100], 1000, atol=2)

    def test_model_audio_plays_same_signal_on_both_channels(self):
        source = np.full(24000, 1000, dtype="<i2").tobytes()
        result = ModelToPiAudio().convert(source, final=True)
        samples = np.frombuffer(result, dtype="<i2").reshape(-1, 2)
        self.assertEqual(len(samples), 48000)
        np.testing.assert_array_equal(samples[:, 0], samples[:, 1])

    def test_software_volume(self):
        source = np.array([-30000, 30000], dtype="<i2").tobytes()
        np.testing.assert_array_equal(np.frombuffer(apply_gain(source, 0.25), dtype="<i2"), [-7500, 7500])


class IndicatorTests(unittest.TestCase):
    def test_bench_colors(self):
        purple = bytes([0, 5, 5]) * 12
        self.assertEqual(animation_frame("idle", 12, 5, 0), purple)
        self.assertEqual(animation_frame("speaking", 12, 5, 0), bytes([5, 0, 0]) * 12)

    def test_all_animations_respect_brightness_and_sweep_moves(self):
        for state in ("idle", "listening", "thinking", "speaking", "connecting", "error", "off"):
            for elapsed in (0, 0.2, 3, 6):
                frame = animation_frame(state, 12, 5, elapsed)
                self.assertEqual(len(frame), 36)
                self.assertLessEqual(max(frame), 5)
        self.assertNotEqual(animation_frame("thinking", 12, 5, 0), animation_frame("thinking", 12, 5, 0.2))
        self.assertNotEqual(animation_frame("listening", 12, 5, 0), animation_frame("listening", 12, 5, 3))

    def test_green_tracks_actual_playback_after_generation_finishes(self):
        self.assertEqual(indicator_state(True, False, False), "speaking")
        self.assertEqual(indicator_state(False, True, False), "thinking")
        self.assertEqual(indicator_state(False, False, True), "listening")
        self.assertEqual(indicator_state(False, False, False), "idle")

class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_playback_compensates_write_and_scheduling_delays_without_losing_pcm(self):
        clock = 10.0
        chunks = []
        writes_at = []
        source = np.arange(48000 * 2 * 2, dtype="<i2").tobytes()

        async def write(chunk):
            nonlocal clock
            writes_at.append(clock)
            chunks.append(chunk)
            clock += 0.004  # Simulate costly writes.

        async def sleep(delay):
            nonlocal clock
            clock += delay + 0.001  # Each scheduling wakeup arrives late.
            if sum(map(len, chunks)) == len(source):
                raise asyncio.CancelledError

        playback = Playback(SimpleNamespace(write=write))
        # Network/resampler packets need not end on a 20 ms boundary.
        playback.push(source[:1000])
        playback.push(source[1000:])
        with patch("flora.audio.asyncio.get_running_loop", return_value=SimpleNamespace(time=lambda: clock)), \
                patch("flora.audio.asyncio.sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await playback.run()
            self.assertTrue(playback.speaking)  # Still gate the acoustic tail.
        self.assertEqual(b"".join(chunks), source)
        self.assertEqual(playback.buffered, 0)
        duration = len(source) / BYTES_PER_SECOND
        self.assertGreaterEqual(writes_at[0], 10 + PLAYBACK_PREROLL)
        self.assertLess(clock - 10, duration + PLAYBACK_PREROLL + 0.025)
        self.assertAlmostEqual(playback.until, 10 + PLAYBACK_PREROLL + 0.001 + duration + 0.3)

    async def test_playback_preroll_collects_delayed_packets_and_plays_short_replies(self):
        chunks = []
        playback = Playback(SimpleNamespace(write=AsyncMock(side_effect=lambda chunk: chunks.append(chunk))))
        playback.push(bytes([1, 2]) * 100)
        async def sleep(delay):
            if delay == PLAYBACK_PREROLL:
                self.assertTrue(playback.speaking)
                self.assertFalse(chunks)
                playback.push(bytes([3, 4]) * 100)
            elif not playback.buffered:
                raise asyncio.CancelledError
        with patch("flora.audio.asyncio.sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await playback.run()
        self.assertEqual(b"".join(chunks), bytes([1, 2]) * 100 + bytes([3, 4]) * 100)

    async def test_shutdown_drains_a_full_unread_capture_pipe(self):
        audio = Audio("unused", 0.25)
        audio.microphone = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import sys,time; sys.stdout.buffer.write(b'x' * 2000000); sys.stdout.flush(); time.sleep(30)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.sleep(0.15)
        await asyncio.wait_for(audio.close(), 3)
        self.assertIsNotNone(audio.microphone.returncode)

    async def test_mic_does_not_send_speaker_echo(self):
        session = Session(Config(api_key="test"), SimpleNamespace())
        session.audio.read = AsyncMock(side_effect=[bytes([1, 2]) * 1920, asyncio.CancelledError()])
        session.input_adapter = SimpleNamespace(convert=lambda audio: audio)
        session.client.send_audio = AsyncMock()
        session.playback.until = asyncio.get_running_loop().time() + 10
        with self.assertRaises(asyncio.CancelledError):
            await session.microphone()
        self.assertEqual(session.client.send_audio.await_args.args[0], bytes(3840))

    async def test_streamed_response_is_flushed_at_audio_done(self):
        session = Session(Config(api_key="test"), SimpleNamespace())
        pcm = np.full(24000, 500, dtype="<i2").tobytes()
        events = [
            {"type": "response.created"},
            {"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()},
            {"type": "response.output_audio.done"},
            {"type": "response.done", "response": {"status": "completed", "output": []}},
        ]
        async def stream():
            for event in events:
                yield event
        session.client.events = stream
        with self.assertRaises(ConnectionError):
            await session.events()
        self.assertEqual(session.playback.buffered, 48000 * 4)
        self.assertFalse(session.responding)
        self.assertTrue(session.playback.speaking)

    async def test_session_schema_uses_current_audio_format_without_key(self):
        event = session_update(Config(api_key="never-send-in-session"))
        tools = {tool["name"]: tool for tool in event["session"]["tools"]}
        self.assertEqual(set(tools), {
            "get_plant_status", "set_plant_profile", "calibrate_soil", "remember", "recall", "forget",
            "get_current_time",
        })
        tool = tools["get_plant_status"]
        self.assertEqual(tool["name"], "get_plant_status")
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["parameters"], {
            "type": "object", "properties": {}, "required": [], "additionalProperties": False,
        })
        self.assertEqual(event["session"]["tool_choice"], "auto")
        self.assertNotIn("never-send-in-session", json.dumps(event))
        self.assertEqual(event["session"]["audio"]["input"]["format"], {"type": "audio/pcm", "rate": 24000})


if __name__ == "__main__":
    unittest.main()
