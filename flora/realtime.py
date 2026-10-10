import base64
import json
from urllib.parse import quote

from websockets.asyncio.client import connect

from .config import ROOT
from .tools import tool_definitions

def session_update(config):
    return {
        "type": "session.update",
        "session": {
            "type": "realtime", "model": config.model,
            "output_modalities": ["audio"],
            "instructions": (ROOT / "persona.md").read_text().strip(),
            "tools": tool_definitions(), "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "noise_reduction": {"type": "far_field"},
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
        import asyncio
        async with asyncio.timeout(20):
            while True:
                event = json.loads(await self.socket.recv())
                if event["type"] == "session.updated":
                    return
                if event["type"] == "error":
                    raise RuntimeError("Realtime rejected session settings: " + event.get("error", {}).get("code", "unknown"))

    async def send(self, event):
        await self.socket.send(json.dumps(event))

    async def send_audio(self, audio):
        await self.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(audio).decode("ascii")})

    async def events(self):
        async for message in self.socket:
            yield json.loads(message)

    async def close(self):
        if self.socket:
            await self.socket.close()
