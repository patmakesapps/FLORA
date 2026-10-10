import asyncio
import base64
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from flora.config import Config
from flora.hub import Session
from flora.memory import Memory
from flora.sensors import Sensors


def function_call(call_id="call_1", name="get_plant_status", arguments="{}"):
    return {"type": "function_call", "name": name, "call_id": call_id, "arguments": arguments}


def completed(*calls, status="completed"):
    return {"type": "response.done", "response": {"status": status, "output": list(calls)}}


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self):
        status = Sensors()._empty_status()
        status["soil"].update(status="ok", raw_capacitance=456, error=None)
        self.sensors = SimpleNamespace(get_plant_status=AsyncMock(return_value=status))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.session = Session(Config(api_key="test"), SimpleNamespace(), self.sensors,
                               Memory(directory.name + "/flora.sqlite3"))
        self.session.client.send = AsyncMock()
        self.tasks = []

    async def asyncTearDown(self):
        for task in getattr(self, "tasks", []):
            task.cancel()
        await asyncio.gather(*getattr(self, "tasks", []), return_exceptions=True)

    async def process(self, events, expected_messages):
        async def stream():
            for event in events:
                yield event
        self.session.client.events = stream
        with self.assertRaises(ConnectionError):
            await self.session.events()
        sent = asyncio.Event()
        async def send(event):
            if self.session.client.send.await_count >= expected_messages:
                sent.set()
        self.session.client.send.side_effect = send
        self.tasks.append(asyncio.create_task(self.session.tools()))
        await asyncio.wait_for(sent.wait(), 1)
        return [call.args[0] for call in self.session.client.send.await_args_list]

    async def test_output_matches_call_id_and_continues_with_real_status_payload(self):
        self.make_session()
        # The live model may include a brief preamble before the function call.
        messages = await self.process([completed(
            {"type": "message", "content": [{"type": "output_text", "text": "Let me check."}]},
            function_call(),
        )], 2)
        self.assertEqual(messages[0]["type"], "conversation.item.create")
        item = messages[0]["item"]
        self.assertEqual(item["type"], "function_call_output")
        self.assertEqual(item["call_id"], "call_1")
        result = json.loads(item["output"])
        self.assertEqual(result["soil"]["raw_capacitance"], 456)
        self.assertFalse(result["soil"]["probe_in_soil"])
        self.assertIsNone(result["light"]["lux"])
        self.assertEqual(messages[1], {"type": "response.create"})
        self.sensors.get_plant_status.assert_awaited_once_with()

    async def test_all_calls_have_outputs_before_one_continuation_and_duplicates_are_ignored(self):
        self.make_session()
        event = completed(function_call(), function_call("call_2"))
        messages = await self.process([event, event], 3)
        self.assertEqual([message["item"]["call_id"] for message in messages[:2]], ["call_1", "call_2"])
        self.assertEqual(messages[2], {"type": "response.create"})
        self.assertEqual(self.sensors.get_plant_status.await_count, 2)

    async def test_invalid_json_extra_arguments_and_unknown_tools_return_errors(self):
        self.make_session()
        messages = await self.process([completed(
            function_call(arguments="{"),
            function_call("call_2", arguments='{"percent":true}'),
            function_call("call_3", name="water_plant"),
        )], 4)
        errors = [json.loads(message["item"]["output"])["error"] for message in messages[:3]]
        self.assertEqual(errors, ["invalid_arguments", "invalid_arguments", "unknown_tool"])
        self.sensors.get_plant_status.assert_not_awaited()

    async def test_cancelled_incomplete_calls_and_argument_events_are_not_executed(self):
        self.make_session()
        async def stream():
            yield {**function_call(), "type": "response.function_call_arguments.done"}
            yield completed(function_call(), status="cancelled")
            yield completed(function_call(), status="incomplete")
        self.session.client.events = stream
        with self.assertRaises(ConnectionError):
            await self.session.events()
        self.assertTrue(self.session.tool_queue.empty())
        self.sensors.get_plant_status.assert_not_awaited()

    async def test_new_speech_avoids_continuation_before_response_created_arrives(self):
        self.make_session()
        messages = await self.process([
            completed(function_call()), {"type": "input_audio_buffer.speech_started"},
        ], 1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["item"]["type"], "function_call_output")
        self.assertTrue(self.session.hearing)

    async def test_sensor_wait_does_not_stop_incoming_audio_and_new_turn_avoids_competing_reply(self):
        self.make_session()
        read_started, release_read, audio_received, hold_stream = [asyncio.Event() for _ in range(4)]
        result = self.sensors.get_plant_status.return_value
        async def slow_read():
            read_started.set()
            await release_read.wait()
            return result
        self.sensors.get_plant_status.side_effect = slow_read
        self.session.output_adapter = SimpleNamespace(convert=lambda pcm: pcm)
        async def stream():
            yield completed(function_call())
            await read_started.wait()
            yield {"type": "response.created"}
            # A fresh response resets the resampler; use PCM at its native rate.
            yield {"type": "response.output_audio.delta", "delta": base64.b64encode(bytes(4800)).decode()}
            audio_received.set()
            await hold_stream.wait()
        self.session.client.events = stream
        self.tasks = [asyncio.create_task(self.session.events()), asyncio.create_task(self.session.tools())]
        await asyncio.wait_for(audio_received.wait(), 1)
        self.assertGreater(self.session.playback.buffered, 0)
        self.session.client.send.assert_not_awaited()
        output_sent = asyncio.Event()
        self.session.client.send.side_effect = lambda event: output_sent.set()
        release_read.set()
        await asyncio.wait_for(output_sent.wait(), 1)
        self.assertEqual(self.session.client.send.await_count, 1)
        self.assertEqual(self.session.client.send.await_args.args[0]["item"]["type"], "function_call_output")


if __name__ == "__main__":
    unittest.main()
