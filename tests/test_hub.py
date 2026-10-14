import asyncio
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

from flora.audio import Audio, Playback, apply_gain, BYTES_PER_SECOND, PLAYBACK_PREROLL
from flora.audio_adapter import PiToModelAudio, ModelToPiAudio
from flora.errors import SessionError, error_code, failure_reason
from flora.config import Config
from flora.errors import AudioError
from flora.hub import Session, indicator_state, until_done
from flora.leds import animation_frame
from flora.power import Power
from flora.volume import Volume
from flora.tools import Tools, tool_definitions
from flora.waking import WakeWord, wake_word_ready
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

    def test_startup_spin_is_purple_and_standby_is_dark(self):
        for elapsed in (0, 0.2, 1, 2.5):
            frame = animation_frame("connecting", 12, 5, elapsed)
            pixels = [frame[i:i + 3] for i in range(0, len(frame), 3)]
            # GRB order: no green, red equal to blue, on every lit pixel.
            self.assertTrue(any(any(pixel) for pixel in pixels))
            self.assertTrue(all(pixel[0] == 0 and pixel[1] == pixel[2] for pixel in pixels))
        self.assertEqual(animation_frame("standby", 12, 5, 3), bytes(36))

    def test_all_animations_respect_brightness_and_sweep_moves(self):
        for state in ("idle", "listening", "thinking", "speaking", "connecting", "standby", "error", "off"):
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
    async def test_playback_keeps_idle_stream_running_and_preserves_speech(self):
        clock = 10.0
        chunks, writes_at = [], []
        source = np.full(48000 * 2 * 2 + 100, 1234, dtype="<i2").tobytes()
        playback = None
        speech_start = None
        speech_end = None

        async def write(chunk):
            nonlocal clock
            writes_at.append(clock)
            chunks.append(chunk)
            clock += 0.004

        async def sleep(delay):
            nonlocal clock, speech_start, speech_end
            clock += delay + 0.001
            if speech_start is None and clock >= 10.5:
                self.assertFalse(playback.speaking)  # Idle silence leaves mic open.
                speech_start = clock
                playback.push(source[:1000])
                playback.push(source[1000:])
            if speech_start is not None and not playback.buffered and speech_end is None:
                speech_end = clock
                self.assertTrue(playback.speaking)  # Acoustic tail still gated.
            if speech_end is not None and clock > speech_end + 0.5:
                self.assertFalse(playback.speaking)
                raise asyncio.CancelledError
            self.assertLess(clock, 15)  # Catch a stuck buffer or gate.

        playback = Playback(SimpleNamespace(write=write))
        with patch("flora.audio.asyncio.get_running_loop", return_value=SimpleNamespace(time=lambda: clock)), \
                patch("flora.audio.asyncio.sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await playback.run()
        speech = b"".join(chunk for chunk in chunks if any(chunk))
        self.assertEqual(speech[:len(source)], source)
        self.assertEqual(speech[len(source):], bytes(len(speech) - len(source)))
        self.assertEqual(playback.buffered, 0)
        first_speech = next(t for t, chunk in zip(writes_at, chunks) if any(chunk))
        self.assertGreaterEqual(first_speech, speech_start + PLAYBACK_PREROLL)
        self.assertLess(speech_end - speech_start, len(source) / BYTES_PER_SECOND + PLAYBACK_PREROLL + 0.05)
        self.assertLess(max(b - a for a, b in zip(writes_at, writes_at[1:])), 0.025)

    async def test_playback_preroll_collects_delayed_packets_and_plays_short_replies(self):
        chunks = []
        clock = 10.0
        added = False
        playback = Playback(SimpleNamespace(write=AsyncMock(side_effect=lambda chunk: chunks.append(chunk))))
        playback.push(bytes([1, 2]) * 100)
        async def sleep(delay):
            nonlocal clock, added
            clock += delay + 0.001
            if clock >= 10.06 and not added:
                self.assertTrue(playback.speaking)
                self.assertFalse(any(any(chunk) for chunk in chunks))
                playback.push(bytes([3, 4]) * 100)
                added = True
            if not playback.buffered:
                raise asyncio.CancelledError
            self.assertLess(clock, 11)
        with patch("flora.audio.asyncio.get_running_loop", return_value=SimpleNamespace(time=lambda: clock)), \
                patch("flora.audio.asyncio.sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await playback.run()
        speech = b"".join(chunk for chunk in chunks if any(chunk))
        self.assertEqual(speech[:400], bytes([1, 2]) * 100 + bytes([3, 4]) * 100)
        self.assertEqual(speech[400:], bytes(3840 - 400))

    async def test_stalled_audio_pipes_trigger_recovery(self):
        async def stalled(*args):
            await asyncio.Event().wait()
        audio = Audio("unused", 0.25)
        audio.microphone = SimpleNamespace(stdout=SimpleNamespace(readexactly=stalled))
        audio.speaker = SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=Mock(), drain=stalled))
        with patch("flora.audio.AUDIO_IO_TIMEOUT", 0.01):
            with self.assertRaisesRegex(SessionError, "Microphone stalled"):
                await audio.read()
            with self.assertRaisesRegex(SessionError, "Speaker stalled"):
                await audio.write(bytes(3840))

    async def test_failed_speaker_ends_conversation_with_audio_error(self):
        async def failed():
            raise AudioError("Speaker stalled for 5 seconds")
        speaker = asyncio.create_task(failed())
        cancelled = asyncio.Event()
        async def conversation():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with self.assertRaisesRegex(AudioError, "Speaker stalled"):
            await asyncio.wait_for(until_done(speaker, conversation()), 1)
        self.assertTrue(cancelled.is_set())

    def conversation(self, **kwargs):
        session = Session(Config(api_key="test"), SimpleNamespace(state="standby"), SimpleNamespace(), **kwargs)
        session.client.open = AsyncMock()
        session.client.close = AsyncMock()
        session.input_adapter = SimpleNamespace(convert=lambda data: data)
        return session

    async def test_speech_while_connecting_is_sent_after_session_is_configured(self):
        session = self.conversation()
        connected = asyncio.Event()
        sent = []
        chunks = [bytes([1, 2]) * 1920, bytes([3, 4]) * 1920, bytes([5, 6]) * 1920]
        async def read():
            if len(chunks) == 1:
                await connected.wait()
            if chunks:
                return chunks.pop(0)
            await asyncio.Event().wait()
        async def open_():
            while len(chunks) > 1:
                await asyncio.sleep(0)
            connected.set()
        async def blocked():
            await asyncio.Event().wait()
            yield {}
        session.audio.read = read
        session.client.open = AsyncMock(side_effect=open_)
        session.client.events = blocked
        session.client.send_audio = AsyncMock(side_effect=lambda data: sent.append(data))
        task = asyncio.create_task(session.run())
        try:
            for _ in range(200):
                if sent:
                    break
                await asyncio.sleep(0.005)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(sent[0], bytes([1, 2]) * 1920 + bytes([3, 4]) * 1920 + bytes([5, 6]) * 1920)
        session.client.close.assert_awaited_once()

    async def test_conversation_ends_after_silence_but_not_while_speaking(self):
        clock = 0.0
        session = self.conversation(idle_timeout=45)
        async def sleep(delay):
            nonlocal clock
            clock += delay
            session.hearing = clock < 20  # User talks for the first 20 seconds.
        with patch("flora.hub.asyncio.get_running_loop", return_value=SimpleNamespace(time=lambda: clock)), \
                patch("flora.hub.asyncio.sleep", side_effect=sleep):
            await session.watch()
        self.assertGreaterEqual(clock, 64.5)  # 45 s after the last speech at 19.5 s.
        self.assertLess(clock, 65.5)

    async def test_power_off_waits_for_the_goodbye_to_finish(self):
        power = Power()
        power.requested = True
        clock = 0.0
        session = self.conversation(power=power)
        powered_at = None
        async def power_off():
            nonlocal powered_at
            powered_at = clock
        power.power_off = power_off
        async def sleep(delay):
            nonlocal clock
            clock += delay
            session.responding = clock < 5
        with patch("flora.hub.asyncio.get_running_loop", return_value=SimpleNamespace(time=lambda: clock)), \
                patch("flora.hub.asyncio.sleep", side_effect=sleep):
            await session.watch()
        self.assertEqual(powered_at, 5.0)

    async def test_power_tool_needs_confirmation_and_permission(self):
        power = Power()
        tools = Tools(None, memory=object(), power=power)
        self.assertEqual(await tools.execute("power_off", {"confirmed": False}), {"status": "not_confirmed"})
        self.assertFalse(power.requested)
        power.permitted = AsyncMock(return_value=False)
        self.assertEqual(await tools.execute("power_off", {"confirmed": True}), {"error": "power_off_not_permitted"})
        self.assertFalse(power.requested)
        power.permitted = AsyncMock(return_value=True)
        self.assertEqual(await tools.execute("power_off", {"confirmed": True}),
                         {"status": "powering_off_after_reply"})
        self.assertTrue(power.requested)
        self.assertEqual(await Tools(None, memory=object()).execute("power_off", {"confirmed": True}),
                         {"error": "unknown_tool"})
        self.assertNotIn("power_off", {tool["name"] for tool in tool_definitions()})
        self.assertIn("power_off", {tool["name"] for tool in tool_definitions(power_off_enabled=True)})

    async def test_power_off_runs_only_the_sudoers_command(self):
        calls = []
        async def exec_(*args, **kwargs):
            calls.append(args)
            return SimpleNamespace(wait=AsyncMock(return_value=0))
        with patch("flora.power.asyncio.create_subprocess_exec", side_effect=exec_):
            self.assertTrue(await Power().permitted())
            await Power().power_off()
        self.assertEqual(calls, [("sudo", "-n", "-l", "/usr/bin/systemctl", "poweroff"),
                                 ("sudo", "-n", "/usr/bin/systemctl", "poweroff")])

    async def test_speaker_mixer_is_lowered_before_opening_and_restored(self):
        audio = Audio("unused", 1.0, 23, 115)
        mixer = {"Capture": "40,40", "Speaker": "127,127"}
        order = []
        async def run_mixer(*args):
            if args[0] == "sget":
                kind = "Capture" if args[1] == "Capture" else "Playback"
                left, right = mixer[args[1]].split(",")
                return f"  Front Left: {kind} {left} [x]\n  Front Right: {kind} {right} [x]\n"
            order.append(args[1])
            mixer[args[1]] = args[2] if "," in args[2] else f"{args[2]},{args[2]}"
            return ""
        audio._mixer = run_mixer
        async def start(command, **kwargs):
            order.append(command[0])
            return SimpleNamespace(returncode=None)
        audio._start = start
        with patch("flora.audio.asyncio.sleep", AsyncMock()):
            await audio.start()
        self.assertEqual(mixer, {"Capture": "23,23", "Speaker": "115,115"})
        self.assertLess(order.index("Speaker"), order.index("aplay"))
        audio.microphone = audio.speaker = None
        await audio.close()
        self.assertEqual(mixer, {"Capture": "40,40", "Speaker": "127,127"})

    async def test_volume_is_capped_in_code_and_remembered(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "volume.json"
            volume = Volume(1.0, path)
            audio = SimpleNamespace(gain=None)
            volume.attach(audio)
            self.assertAlmostEqual(audio.gain, 10 ** (-6 / 20))  # Starts at level 8.
            tools = Tools(None, memory=object(), volume=volume)
            for _ in range(3):
                result = await tools.execute("set_volume", {"change": "up"})
            self.assertEqual((result["level"], result["at_maximum"]), (10, True))
            self.assertEqual(audio.gain, 1.0)  # As loud as allowed.
            result = await tools.execute("set_volume", {"level": 4})
            self.assertAlmostEqual(audio.gain, 10 ** (-18 / 20))
            self.assertEqual(await tools.execute("set_volume", {"level": 11}), {"error": "invalid_arguments"})
            self.assertEqual(await tools.execute("set_volume", {"level": 3, "change": "up"}),
                             {"error": "invalid_arguments"})
            await tools.execute("set_volume", {"change": "down"})
            self.assertEqual(Volume(1.0, path).level, 3)  # Survives a restart.
            self.assertEqual((await tools.execute("set_volume", {}))["level"], 3)
        self.assertEqual(await Tools(None, memory=object()).execute("set_volume", {"level": 5}),
                         {"error": "unknown_tool"})

    async def test_wake_word_scores_80ms_frames_until_threshold(self):
        frames = []
        scores = iter([0.05, 0.1, 0.19, 0.8])  # Default threshold 0.2.
        model = SimpleNamespace(predict=lambda frame: frames.append(len(frame)) or {"hey_flora": next(scores)},
                                reset=Mock())
        wake = WakeWord(Config(api_key="test"))
        wake._model, wake._name = model, "hey_flora"
        audio = SimpleNamespace(read=AsyncMock(return_value=bytes(3840)))
        await asyncio.wait_for(wake.wait(audio), 1)
        self.assertEqual(frames, [1280] * 4)  # 16 kHz, after the 48 kHz stereo downsample.
        model.reset.assert_called_once()

    def test_wake_word_needs_its_model(self):
        missing = Config(api_key="test", wake_word_path=Path("missing.onnx"))
        self.assertIn("missing.onnx", wake_word_ready(missing))
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "hey_flora.onnx"
            model.write_bytes(b"model")
            self.assertIsNone(wake_word_ready(Config(api_key="test", wake_word_path=model)))

    async def test_failed_response_exposes_code_but_not_message(self):
        session = Session(Config(api_key="test"), SimpleNamespace())
        async def events():
            yield {"type": "response.done", "response": {"status": "failed", "status_details": {
                "error": {"code": "server_error", "message": "private conversation"}}}}
        session.client.events = events
        with self.assertRaises(SessionError) as caught:
            await session.events()
        self.assertEqual(failure_reason(caught.exception), "Realtime response failed: server_error")
        self.assertEqual(failure_reason(RuntimeError("private credential")), "RuntimeError")
        self.assertEqual(error_code({"code": "private conversation\n"}), "unknown")

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
        session._connected = True
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

    async def test_health_recovers_stalled_response_but_not_idle_or_playback(self):
        for waiting, speaking, hearing, should_fail in (
            (True, False, False, True), (False, False, False, False),
            (True, True, False, False), (True, False, True, False),
        ):
            session = Session(Config(api_key="test"), SimpleNamespace())
            session._response_progress = 1.0 if waiting else None
            session.hearing = hearing
            if speaking:
                session.playback.push(bytes(3840))
            calls = 0
            async def sleep(delay):
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise asyncio.CancelledError
            with patch("flora.hub.asyncio.get_running_loop", return_value=SimpleNamespace(time=lambda: 100.0)), \
                    patch("flora.hub.asyncio.sleep", side_effect=sleep):
                if should_fail:
                    with self.assertRaisesRegex(SessionError, "Response stalled"):
                        await session.health()
                else:
                    with self.assertRaises(asyncio.CancelledError):
                        await session.health()

    async def test_health_reports_mic_before_gating_and_sent_silence(self):
        session = Session(Config(api_key="test"), SimpleNamespace())
        session.audio.read = AsyncMock(side_effect=[np.full(1920, -2000, dtype="<i2").tobytes(), asyncio.CancelledError()])
        session.input_adapter = SimpleNamespace(convert=lambda audio: audio)
        session.client.send_audio = AsyncMock()
        session._connected = True
        session.playback.until = asyncio.get_running_loop().time() + 10
        with self.assertRaises(asyncio.CancelledError):
            await session.microphone()
        self.assertEqual(session._mic_peak, 2000)
        self.assertEqual(session._gated_bytes, 3840)
        self.assertEqual(session._sent_bytes, 3840)
        self.assertEqual(session.client.send_audio.await_args.args[0], bytes(3840))

    async def test_completed_response_clears_watchdog(self):
        session = Session(Config(api_key="test"), SimpleNamespace())
        async def events():
            yield {"type": "response.created"}
            self.assertIsNotNone(session._response_progress)
            yield {"type": "response.done", "response": {"status": "completed"}}
        session.client.events = events
        with self.assertRaises(ConnectionError):
            await session.events()
        self.assertIsNone(session._response_progress)

    async def test_stalled_network_send_has_safe_error(self):
        from flora.realtime import Realtime
        client = Realtime(Config(api_key="test"))
        client.socket = SimpleNamespace(send=AsyncMock(side_effect=TimeoutError))
        with self.assertRaisesRegex(SessionError, "Realtime send stalled"):
            await client.send_audio(bytes(100))

    async def test_session_schema_uses_current_audio_format_without_key(self):
        event = session_update(Config(api_key="never-send-in-session"))
        tools = {tool["name"]: tool for tool in event["session"]["tools"]}
        self.assertEqual(set(tools), {
            "get_plant_status", "set_plant_profile", "calibrate_soil", "remember", "recall", "forget",
            "get_current_time", "get_observation_history", "set_volume",
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
