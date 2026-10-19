# stereo-hub

A small HTTP service and web remote for the **Stereo Hub** that ships with
Buchardt Audio A500 / A700 speakers: a Platin (Hansong) WiSA transmitter built
on the Libre Wireless LibreSync platform. It talks to the hub over your local
network, with no vendor app and no cloud.

The hub protocol is handled by [aiolibresync](https://github.com/drsound/aiolibresync).
This service holds one connection open, keeps the hub's state current from its
own push messages, and exposes that over HTTP:

- a phone-friendly web page at `/` with power, volume and input
- a JSON API at `/api/...` (interactive docs at `/docs`)
- a one-shot URL for a Flic button (or anything else that can fetch a URL)
  that turns the hub on and switches input

> The same hub is sold under other brands too: Platin, System Audio, Econik,
> Triangle and others. It will probably work with them, but it has only been
> written against the Buchardt / Platin Stereo Hub.

## Run it

### Docker (recommended)

```bash
docker run -d --name stereo-hub -p 8080:8080 \
  -e STEREO_HUB_HOST=192.168.1.50 \
  --restart unless-stopped \
  ghcr.io/theboyknowsclass/stereo-hub:latest
```

Or with the included [compose.yml](compose.yml): copy
[.env.example](.env.example) to `.env`, set your hub's address (and a port, if
8080 is taken), then:

```bash
docker compose up -d
```

Then open `http://<docker-host>:8080/`.

### Finding the hub's address

Your router's list of connected devices is the easiest place to look. The hub
usually shows up under the name you gave it in the vendor's app, or as
"LibreWireless". If your network is split into VLANs, the hub may be on a
different subnet from your PC. The service only needs to be able to reach it.

If the page says **hub offline (address)**, the service can't reach that
address on TCP 50006 and 7777. `docker logs stereo-hub` shows each connect
attempt.

Images are built for `linux/amd64` and `linux/arm64` on every push to `main`
(`:latest`, `:sha-<short>`) and on `v*` tags (`:1.2.3`).

### Locally

```bash
python -m venv .venv
.venv/bin/pip install -e .
STEREO_HUB_HOST=192.168.1.50 .venv/bin/stereo-hub
```

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `STEREO_HUB_HOST` | *(discover)* | The hub's IP address, e.g. `192.168.1.50`. Strongly recommended: give the hub a DHCP reservation and set this. The service logs whether it connected; `docker logs` shows it. |
| `STEREO_HUB_PORT` | `8080` | Host port in [compose.yml](compose.yml). |
| `PORT` | `8080` | Port the service listens on inside the container. |
| `BIND_HOST` | `0.0.0.0` | Address the service listens on. |
| `LOG_LEVEL` | `info` | |

If `STEREO_HUB_HOST` is unset, the service looks for a hub with an SSDP search
at startup and uses the first one it finds. Multicast does not cross Docker's
default bridge network, so discovery inside a container needs
`network_mode: host`. Setting the address avoids the problem.

The hub needs TCP ports **50006** and **7777** reachable from wherever this
service runs.

## Flic button

Add an **Internet Request** action to the button, in the Flic app or on a Flic
Hub, with method `GET` (or `POST`) and a URL like:

```
http://<docker-host>:8080/api/actions/on/HDMI
```

This turns the hub on, if it isn't already, and switches to the input named
`HDMI`. Input names are matched case-insensitively against the hub's own list,
which `GET /api/state` or the web page shows. The input's index works too.
Leave the input off (`/api/actions/on`) to only turn the hub on.

The request is sent by whatever runs the Flic action. For the phone app that
means the phone has to be on your home network.

## API

| Method | Path | Body | |
| --- | --- | --- | --- |
| `GET` | `/api/state` | | Current state |
| `PUT` | `/api/power` | `{"on": true}` | |
| `PUT` | `/api/volume` | `{"level": 30}` | 0–100 |
| `PUT` | `/api/source` | `{"index": 1}` or `{"name": "HDMI"}` | |
| `POST` | `/api/media/{play,pause,stop,next,previous}` | | Streaming input only |
| `GET`/`POST` | `/api/actions/on[/{input}]` | | Power on and switch input, for buttons |
| `GET` | `/api/events` | | Server-sent events: the state on connect and on every change |
| `GET` | `/healthz` | | Liveness, plus whether the hub is connected |

Every write waits for the hub to confirm the change and returns the new state.
If the hub can't be reached you get `503`, and if it doesn't confirm in time
you get `504`.

Things the hub does that may surprise you (see aiolibresync's README for the
details):

- "Off" ends the playback session. Turning the hub back on doesn't resume it.
- Switching away from the Streaming input ends a Chromecast session.
- Mute can be read but not set, so the API has no mute control.

## Security

There is no authentication. Anyone who can reach the port can turn your
stereo on and change the volume. Keep it on your LAN, or put it behind
something that authenticates.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The tests run the API against a fake hub, so they need no hardware.

## License

MIT
