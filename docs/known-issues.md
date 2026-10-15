# Known issues

Problems we've diagnosed but not fixed yet. Newest first.

## Flora answers her own replies after long uptime

**Seen:** 2026-10-15. Flora kept responding to her own speech. Unplugging her
and plugging her back in fixed it. The logs were lost on reboot (the journal
isn't persistent), so this is the best-fitting cause, not a confirmed one.

**Why it happens.** The HAT has no echo canceller. The only protection is the
mic gate in `flora/hub.py` (`Session.microphone`): while `playback.speaking`,
the mic is replaced with silence, plus a 0.3 s tail (`Playback.until` in
`flora/audio.py`). That tail is timed from when audio is *written to aplay*,
assuming it reaches the speaker about 60 ms later.

That assumption drifts:

- `Playback.run` paces writes on the system clock and feeds silence
  continuously, even in standby. The WM8960 clock runs a little slow
  (roughly 50 ppm), so audio slowly piles up in aplay's stdin pipe.
- The pipe holds up to 64 KiB, about 340 ms. Measured on 2026-10-15: 100 ms
  backed up in that pipe after about 35 minutes of uptime.
- After about 2 hours with no stall, the pipe is full and the speaker lags by
  about 420 ms (pipe plus aplay's 80 ms buffer). The 0.3 s gate opens
  before she has finished talking, so the end of each reply reaches Realtime
  and semantic VAD treats it as the user speaking.
- A reboot empties the pipe. The short event-loop stall at each wake drains
  some of the backlog but not all of it, so whether it happens depends on how
  long she's been in standby.
- Volume level 9 with replies peaking near full scale makes the leak loud.

**Fix plan (not done):**

1. Cap the backlog: shrink aplay's stdin pipe to its minimum
   (`fcntl F_SETPIPE_SZ`, about 4 KiB, or 20 ms) so `drain()` blocks and
   playback follows the card's clock.
2. Raise the gate tail from 0.3 s to about 0.5 s as an extra margin.
3. Add visibility: log the pipe backlog in the "Audio health" line, and make
   the journal persistent (`Storage=persistent`) so logs survive a reboot.
4. Optional: arecord uses its defaults (500 ms buffer, 125 ms periods). Set
   `--buffer-time`/`--period-time` the way aplay does.

**To check the backlog live** (read-only, no sound), compare FIONREAD on
`/proc/<aplay pid>/fd/0` with the `delay` line in
`/proc/asound/card*/pcm0p/sub0/status`.
