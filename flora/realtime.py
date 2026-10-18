import asyncio
import base64
import json
from urllib.parse import quote

from websockets.asyncio.client import connect

from .config import ROOT
from .errors import SessionError, error_code
from .tools import tool_definitions

def session_update(config):
    return {
        "type": "session.update",
        "session": {
            "type": "realtime", "model": config.model,
            "output_modalities": ["audio"],
            "instructions": (ROOT / "persona.md").read_text().strip(),
            "tools": tool_definitions(config.lights_enabled, config.power_off_enabled), "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    # Off: far_field seemed to adapt to the user's voice at the
                    # start of a session and hide speech for 10-40 s after a wake.
                    "noise_reduction": None,
                    "turn_detection": {
                        "type": "semantic_vad", "eagerness": "auto",
                        "create_response": True, "interrupt_response": False,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": config.voice,
                },
            },
        },
    }


class Realtime:
    def __init__(self, config):
        self.config = config
        self.socket = None

    async def open(self):
        self.socket = await connect(
            "wss://api.openai.com/v1/realtime?model=" + quote(self.config.model, safe=""),
            additional_headers={"Authorization": "Bearer " + self.config.api_key},
            open_timeout=20, max_size=4 * 1024 * 1024,
        )
        await self.send(session_update(self.config))
        # Do not start sending microphone audio before configuration is accepted.
        async with asyncio.timeout(20):
            while True:
                event = json.loads(await self.socket.recv())
                if event["type"] == "session.updated":
                    return
                if event["type"] == "error":
                    raise SessionError("Realtime rejected session settings: " + error_code(event.get("error") or {}))

    async def send(self, event):
        try:
            async with asyncio.timeout(10):
                await self.socket.send(json.dumps(event))
        except TimeoutError as exc:
            raise SessionError("Realtime send stalled for 10 seconds") from exc

    async def send_audio(self, audio):
        await self.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(audio).decode("ascii")})

    async def events(self):
        async for message in self.socket:
            yield json.loads(message)

    async def close(self):
        if self.socket:
            await self.socket.close()
