# Letting Flora control the living-room TV

Status as of 2026-10-18: **blocked on the home network**, not on code. Nothing
in this repo does anything with the TV yet. This note records what we found so
we can pick it up later.

## What's on the network

Scanned from the dev PC with SSDP (UPnP, UDP 1900) and mDNS (Bonjour, UDP 5353)
broadcasts, plus a ping sweep.

| Device (name it announces)     | Model      | Subnet      | Reachable from Flora? |
|--------------------------------|------------|-------------|-----------------------|
| 58in Onn Roku TV (living room) | onn C511X  | 192.168.1.x | no                    |
| 32in onn. Roku TV              | onn 8522X  | 192.168.1.x | no                    |
| Roku Express                   | Roku 3930X | 192.168.1.x | no                    |
| Flora (Pi)                     |            | 10.0.0.x    |                       |
| lumalight (ESP32)              |            | 10.0.0.x    | yes                   |

The Rokus only showed up as announcements relayed by the Linksys Wi-Fi box,
which is the access point the PC and Pi are associated with. Direct
connections to the TVs, and to the Linksys admin page, time out from both the
PC and the Pi.

## Why Flora can't reach the TV

Two subnets. The ARRIS cable gateway hands out 10.0.0.x addresses; the PC and
the Pi are there. The Linksys is in **router mode**, so it runs its own
192.168.1.x subnet behind NAT, and the three Rokus are on that side. Traffic
can leave the Linksys side but nothing on 10.0.0.x can open a connection
inward. Flora can hear the TV announce itself but can't send it a command.

## The fix: put the Linksys in bridge mode

Bridge mode makes the Linksys a plain access point for the ARRIS. Everything
lands on 10.0.0.x. The Wi-Fi name and password stay the same; the TVs just pick
up new 10.0.0.x addresses. Port forwards and parental controls set on the
Linksys are lost (the ARRIS does routing from then on).

It has to be done from the Linksys side of the network, e.g. a phone on the
same Wi-Fi as the TVs:

- **Linksys app:** menu > Network Administration (or Advanced Settings) >
  Internet Settings > Connection Type > **Bridge Mode** > save. It reboots;
  allow 2-3 minutes.
- **Browser:** `http://192.168.1.1` or `http://myrouter.local` > Connectivity >
  Internet Settings > Edit next to "Type of Internet Connection" >
  **Bridge Mode** > Apply.

Verify afterwards from the Pi:

```sh
# find the Rokus again (their IPs will have changed)
python3 - <<'PY'
import socket
m=b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 2\r\nST: roku:ecp\r\n\r\n'
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(3); s.sendto(m,("239.255.255.250",1900))
try:
    while True: d,a=s.recvfrom(4096); print(a[0], [l for l in d.decode().splitlines() if l.startswith(("LOCATION","USN"))])
except socket.timeout: pass
PY
# then, with the 58in's new address:
curl http://<tv-ip>:8060/query/device-info | grep -E "friendly-device-name|power-mode"
```

If `device-info` comes back, the network part is done.

## How control will work once reachable

Roku's **External Control Protocol (ECP)** is a plain HTTP API on port 8060, no
pairing or login. Examples:

```sh
curl -X POST http://<tv-ip>:8060/keypress/PowerOn      # needs Fast TV Start
curl -X POST http://<tv-ip>:8060/keypress/PowerOff
curl -X POST http://<tv-ip>:8060/keypress/VolumeUp      # also VolumeDown, VolumeMute
curl -X POST http://<tv-ip>:8060/keypress/Home
curl -X POST http://<tv-ip>:8060/launch/12              # Netflix; /query/apps lists ids
curl -X POST "http://<tv-ip>:8060/search/browse?keyword=planet+earth"
curl      http://<tv-ip>:8060/query/device-info         # power-mode, name, model
curl      http://<tv-ip>:8060/query/active-app
```

Reference: https://developer.roku.com/docs/developer-program/dev-tools/external-control-api.md

Plan for the code:

- A small `flora/roku.py` module that discovers the TV by its SSDP `roku:ecp`
  announcement and friendly name ("58in Onn Roku TV"), so a changed IP doesn't
  matter, and wraps the handful of ECP calls above.
- Expose it to the Realtime session as a function tool, the same pattern as
  the plant-readings tool: `tv_power(on|off)`, `tv_volume(up|down|mute)`,
  `tv_launch(app)`, `tv_status()`.
- Keep it read-mostly by default; confirm before power/volume changes land in
  a reply so echo or a mis-wake can't flip the TV.

## Caveats

- **Power on over the network only works if Fast TV Start is enabled** on the
  TV (Settings > System > Power). Without it the TV drops off the network when
  off, and only PowerOff/volume/etc. work while it's on.
- Being visible on the network does not mean the TV is on; with Fast TV Start
  it answers in standby too. `power-mode` in `device-info` is the truth
  (`PowerOn`, `DisplayOff`, `Headless`).
- The Pi advertises Cast/AirPlay/Android-TV service types over mDNS, but
  nothing listens on those ports; it's a stale avahi announcement, not a
  problem.
