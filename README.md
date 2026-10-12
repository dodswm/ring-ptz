# Ring Pan-Tilt for Home Assistant

Pan and tilt the **Ring Pan-Tilt Indoor Cam** from Home Assistant.

Home Assistant's built-in Ring integration shows this camera's live view but can't move it. This
custom integration adds:

- **Buttons:** Pan left, Pan right, Tilt up, Tilt down, Home position
- **Sensors:** Pan angle, Tilt angle (updated after each command)
- **Services:** step, continuous move, stop, home, set position and get position
- **A pad card:** tap to step, hold to keep moving until you let go

The buttons and sensors join the camera's existing device from the Ring integration, so they appear
on its device page. The Ring integration isn't required.

> **Unofficial.** This uses Ring's private app protocol, which was reverse-engineered from the Ring
> Android app. Ring doesn't support it and may break it with any app or firmware update. Using it
> may be against Ring's terms of service. Use at your own risk.

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/dodswm/ring-ptz` with type **Integration**.
2. Install **Ring Pan-Tilt** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Ring Pan-Tilt**, then log in with your Ring
   email, password and 2FA code.

The integration logs in as its own Ring client, so the Ring integration stays logged in. It then finds
every Pan-Tilt Indoor Cam (`stickup_cam_mini_ptz_v1`) on the account.

## Card

The card is bundled and loads automatically, so you don't need to add a resource.

```yaml
type: custom:ptz-pad-card
entity: sensor.girls_bedroom_pan_angle   # any Ring Pan-Tilt entity of the camera
show_angles: true                        # "Pan 32° · Tilt -33°" under the buttons
```

| Button | Tap | Hold |
|---|---|---|
| ◀ ▶ | pan one step (about 16°) | pan at about 34°/s until released |
| ▲ ▼ | tilt one step (about 10°) | tilt until released |
| ⌂ | go to the Home Position set in the Ring app | |

Put the card under a `picture-entity` card that shows the Ring live view.

## Services

Each service takes `device_id`, which can be one or more cameras.

| Service | Fields | |
|---|---|---|
| `ring_ptz.step` | `direction`: left / right / up / down | one step |
| `ring_ptz.move` | `direction`, `speed` (0.4–0.63, default 0.5), `duration` (s, default 3) | continuous move that stops after `duration` unless called again |
| `ring_ptz.stop` | | stop a continuous move |
| `ring_ptz.home` | | go to the Home Position |
| `ring_ptz.set_position` | `pan` (about ±177°), `tilt` (about -55…30°) | go to exact angles |
| `ring_ptz.get_position` | | returns `{name: {pan, tilt}}` |
| `ring_ptz.prepare` | | open a session now so the next command is instant |

## How it works

The Ring app sends **JSON-RPC 2.0** commands to the camera as signalling method `rpc` inside a
live-view session. This is the same signalling websocket the Ring integration uses for live view:

```json
{"method": "rpc", "dialog_id": "…", "body": {"doorbot_id": 123, "session_id": "…", "command":
  {"jsonrpc": "2.0", "id": "<uuid>", "method": "PTZ.Pan.Step",
   "params": {"sessionId": "…", "direction": "LEFT", "timestamp": 1760300000000, "version": 1}}}}
```

| Method | Params |
|---|---|
| `PTZ.Pan.Step` / `PTZ.Tilt.Step` | `direction`: `LEFT`/`RIGHT` / `UP`/`DOWN` |
| `PTZ.Pan.Continuous` / `PTZ.Tilt.Continuous` | `direction`, `speed` (0 stops; about 0.3 doesn't move; 0.5 ≈ 34°/s) |
| `PTZ.Set` | `panAngle`, `tiltAngle` |
| `PTZ.Get` | returns `panAngle`, `tiltAngle` |
| `PTZ.Overview`, `PTZ.Calibrate`, `PTZ.AutoTrack.Get` | (not used yet) |

Replies arrive as `body.command = {"destination": "app", "message": <reply>}`. Error replies may come
without an `id`. The camera also sends notifications such as `PTZ.Pan.Halted {reason: LIMIT_REACHED}`.
`PTZ.HomePosition.Return` returns *Method not found* on this camera. Home instead uses `PTZ.Set` with
`ptz_settings.{pan,tilt}_settings.positions.default` from `GET /devices/v1/devices/<id>/settings`.

Implementation notes:

- **No WebRTC.** A receive-only dummy SDP offer is enough for Ring's media server to create the
  session and connect the camera, in about 0.2 s. Ring drops the session about 9 s later ("ICE
  connection failed"). The integration reuses a session for 7 s, so commands after the first take
  0.1–0.4 s. A cold start takes about 1.2 s.
- **A continuous move keeps going if the session drops**, until it reaches the limit. Because of
  this, `move` always auto-stops, and while a move is running a watchdog keeps a fresh session
  ready so the stop goes out immediately.
- **Ring account throttling.** Ring answers `clients_api/session` with HTTP 406 for a while after an
  account creates many sessions. When that happens, the Ring integration can't log in either. This
  integration never calls that endpoint. It uses only the OAuth token, the device list, the
  streaming ticket and the signalling websocket.
- Each command uses a short "on demand" live-view session. These may show up in the Ring app's
  history.

## Credits

Built on [python-ring-doorbell](https://github.com/python-ring-doorbell/python-ring-doorbell), which
provides authentication and the signalling endpoints.
