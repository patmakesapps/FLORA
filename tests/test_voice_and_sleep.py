import asyncio
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np

from flora.config import Config
from flora.hub import Session
from flora.realtime import session_update


def turn(voiced_ms):
    session = Session(Config(api_key="test"), SimpleNamespace(),
                      voice_check=SimpleNamespace(voiced_ms=lambda start, end: voiced_ms))
    speech = ("item", 1000)
    stopped = {"item_id": "item", "audio_end_ms": 3000}
    return session._accept(speech, stopped)


class VoiceAndSleepTests(unittest.IsolatedAsyncioTestCase):
    def test_typing_is_dropped_but_speech_is_answered(self):
        self.assertFalse(turn(90))   # Loud enough for the server, but not a voice.
        self.assertTrue(turn(600))

    @unittest.skipUnless(importlib.util.find_spec("openwakeword"), "openwakeword not installed")
    def test_silero_hears_no_voice_in_clicks(self):
        from flora.voice_check import VoiceCheck
        check = VoiceCheck()
        clicks = np.zeros(24000, dtype="<i2")
        clicks[::2400] = 20000  # Ten sharp clicks in a second.
        check.feed(clicks.tobytes(), 0)
        self.assertEqual(check.voiced_ms(0, 1000), 0)

    def test_sleep_tool_only_with_a_wake_word(self):
        names = lambda event: {tool["name"] for tool in event["session"]["tools"]}
        self.assertNotIn("go_to_sleep", names(session_update(Config(api_key="test"))))
        self.assertIn("go_to_sleep", names(session_update(Config(api_key="test"), sleep_enabled=True)))
        self.assertIsNone(Session(Config(api_key="test"), SimpleNamespace()).sleep)

    async def test_sleep_waits_for_the_goodbye_then_ends_the_conversation(self):
        session = Session(Config(api_key="test"), SimpleNamespace(), idle_timeout=45)
        self.assertEqual(await session.tool_runner.execute("go_to_sleep", {}),
                         {"status": "sleeping_after_reply"})
        session.responding = True
        watcher = asyncio.create_task(session.watch())
        await asyncio.sleep(0.6)
        self.assertFalse(watcher.done())  # Still saying goodbye.
        session.responding = False
        await asyncio.wait_for(watcher, 2)


if __name__ == "__main__":
    unittest.main()
