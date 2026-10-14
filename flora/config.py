import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    api_key: str
    model: str = "gpt-realtime-2.1-mini"
    voice: str = "shimmer"
    alsa_device: str = "hw:wm8960soundcard,0"
    speaker_gain: float = 1.0
    speaker_volume: int = 121
    mic_capture_volume: int = 23
    led_gpio: int = 12
    led_count: int = 12
    led_max_channel: int = 5
    leds_enabled: bool = True
    soil_probe_in_soil: bool = False
    lights_enabled: bool = False
    power_off_enabled: bool = False
    picovoice_access_key: str = ""
    wake_word_path: Path = ROOT / "wake" / "hey-flora.ppn"
    wake_word_sensitivity: float = 0.5
    conversation_timeout: float = 45.0

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env")
        config = cls(
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            model=os.getenv("REALTIME_MODEL", "gpt-realtime-2.1-mini").strip(),
            voice=os.getenv("REALTIME_VOICE", "shimmer").strip(),
            alsa_device=os.getenv("ALSA_DEVICE", "hw:wm8960soundcard,0"),
            speaker_gain=float(os.getenv("SPEAKER_GAIN", "1.0")),
            speaker_volume=int(os.getenv("SPEAKER_VOLUME", "121")),
            mic_capture_volume=int(os.getenv("MIC_CAPTURE_VOLUME", "23")),
            led_gpio=int(os.getenv("LED_GPIO", "12")),
            led_count=int(os.getenv("LED_COUNT", "12")),
            led_max_channel=int(os.getenv("LED_MAX_CHANNEL", "5")),
            leds_enabled=os.getenv("LEDS_ENABLED", "true").lower() == "true",
            soil_probe_in_soil=os.getenv("SOIL_PROBE_IN_SOIL", "false").lower() == "true",
            lights_enabled=os.getenv("LIGHTS_ENABLED", "false").lower() == "true",
            power_off_enabled=os.getenv("POWER_OFF_ENABLED", "false").lower() == "true",
            picovoice_access_key=os.getenv("PICOVOICE_ACCESS_KEY", "").strip(),
            wake_word_path=ROOT / os.getenv("WAKE_WORD_PATH", "wake/hey-flora.ppn"),
            wake_word_sensitivity=float(os.getenv("WAKE_WORD_SENSITIVITY", "0.5")),
            conversation_timeout=float(os.getenv("CONVERSATION_TIMEOUT", "45")),
        )
        if not 0 <= config.speaker_gain <= 1:
            raise ValueError("SPEAKER_GAIN must be between 0 and 1")
        # WM8960 Speaker: 121 = 0 dB, 127 = +6 dB, one dB per step.
        if not 0 <= config.speaker_volume <= 127:
            raise ValueError("SPEAKER_VOLUME must be between 0 and 127")
        if not 0 <= config.wake_word_sensitivity <= 1:
            raise ValueError("WAKE_WORD_SENSITIVITY must be between 0 and 1")
        if not 10 <= config.conversation_timeout <= 600:
            raise ValueError("CONVERSATION_TIMEOUT must be between 10 and 600 seconds")
        if not 0 <= config.mic_capture_volume <= 63:
            raise ValueError("MIC_CAPTURE_VOLUME must be between 0 and 63")
        if not 1 <= config.led_count <= 128:
            raise ValueError("LED_COUNT must be between 1 and 128")
        if not 0 <= config.led_max_channel <= 63:
            raise ValueError("LED_MAX_CHANNEL must be between 0 and 63 (25% maximum)")
        # This bench's confirmed wiring. Changing pins requires checking conflicts.
        if config.led_gpio != 12:
            raise ValueError("This hub is configured for GPIO12 / physical pin 32")
        return config
