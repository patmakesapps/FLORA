"""Power off the Pi through one sudoers rule, never general sudo."""
import asyncio
import logging

log = logging.getLogger(__name__)
# Must match deploy/flora-poweroff.sudoers exactly; sudo allows nothing else.
POWEROFF = ("/usr/bin/systemctl", "poweroff")


async def _sudo(*args):
    # -n: fail instead of waiting for a password nobody can type.
    process = await asyncio.create_subprocess_exec(
        "sudo", "-n", *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait()


class Power:
    def __init__(self):
        self.requested = False

    async def permitted(self):
        """True when sudo would run the poweroff command without a password."""
        try:
            return await _sudo("-l", *POWEROFF) == 0
        except OSError:
            return False

    async def power_off(self):
        log.info("Powering off at the user's request")
        if await _sudo(*POWEROFF) != 0:
            self.requested = False
            raise OSError("poweroff was not permitted")
