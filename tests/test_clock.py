import os
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from flora.tools import Tools


@unittest.skipUnless(hasattr(time, "tzset"), "time.tzset is POSIX-only; the Pi runs these")
class ClockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"TZ": "America/New_York"})
        environment.start()
        time.tzset()
        self.addCleanup(time.tzset)
        self.addCleanup(environment.stop)
        # A clock lookup must work without sensor access or database operations.
        self.tools = Tools(None, memory=object())

    async def test_local_date_rollover_and_utc_describe_the_same_instant(self):
        instant = datetime(2026, 1, 1, 2, 3, 4, tzinfo=timezone.utc)
        with patch("flora.tools.datetime", wraps=datetime) as clock:
            clock.now.return_value = instant
            result = await self.tools.execute("get_current_time", {})
            clock.now.assert_called_once_with(timezone.utc)
        self.assertEqual(result["local_datetime"], "2025-12-31T21:03:04-05:00")
        self.assertEqual(result["date"], "2025-12-31")
        self.assertEqual(result["time"], "21:03:04")
        self.assertEqual(result["weekday"], "Wednesday")
        self.assertEqual(result["timezone_abbreviation"], "EST")
        self.assertEqual(result["source"], "raspberry_pi_system_clock")
        self.assertEqual(datetime.fromisoformat(result["local_datetime"]),
                         datetime.fromisoformat(result["utc_datetime"]))

    async def test_each_call_reads_a_fresh_clock_and_uses_the_current_dst_offset(self):
        instants = [datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
                    datetime(2026, 7, 1, 12, tzinfo=timezone.utc)]
        with patch("flora.tools.datetime", wraps=datetime) as clock:
            clock.now.side_effect = instants
            winter = await self.tools.execute("get_current_time", {})
            summer = await self.tools.execute("get_current_time", {})
        self.assertEqual(winter["time"], "07:00:00")
        self.assertEqual(summer["time"], "08:00:00")
        self.assertEqual(summer["timezone_abbreviation"], "EDT")
        self.assertTrue(summer["local_datetime"].endswith("-04:00"))

    async def test_clock_rejects_unadvertised_arguments(self):
        result = await self.tools.execute("get_current_time", {"timezone": "made-up"})
        self.assertEqual(result, {"error": "invalid_arguments"})


if __name__ == "__main__":
    unittest.main()
