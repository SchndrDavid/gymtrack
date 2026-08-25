# Deployment

GymTrack has no authentication. It assumes it is running on a network only you can reach. Everything
below is written with that in mind.

## On a home server

```bash
sudo mkdir -p /srv/gymtrack/app /srv/gymtrack/data
sudo chown -R "$USER:$USER" /srv/gymtrack
git clone https://github.com/SchndrDavid/gymtrack.git /srv/gymtrack/app
mkdir -p /srv/gymtrack/data
docker compose -f /srv/gymtrack/app/docker-compose.yml up -d --build
```

If you keep compose files in one place rather than next to the code, point `build:` at the checkout
and the volume at a data directory outside it:

```yaml
name: gymtrack

services:
  gymtrack:
    build: /srv/gymtrack/app
    image: gymtrack
    container_name: gymtrack
    restart: unless-stopped
    user: "1000:1000"
    ports:
      - "8101:8000"
    volumes:
      - /srv/gymtrack/data:/data
```

Updating is a pull and a rebuild. The frontend lives inside the image, so a rebuild is required even
for a one-line change to `index.html`:

```bash
git -C /srv/gymtrack/app pull
docker compose -f /srv/gymtrack/app/docker-compose.yml up -d --build
```

## Backups

The database is a single file, and there is an endpoint that dumps everything as JSON:

```bash
curl -s localhost:8101/api/export > "gymtrack-$(date +%F).json"
```

A weekly cron entry is enough:

```cron
0 4 * * 1 curl -s localhost:8101/api/export > /srv/backups/gymtrack-$(date +\%F).json
```

## Reaching it from a phone

**Over a VPN mesh (Tailscale, Netbird, ZeroTier).** Nothing to configure — the container binds
`0.0.0.0`, so it answers on the mesh address. This is the intended setup.

**On a custom domain, mesh-only.** Public DNS can point at a private address. An `A` record for
`gym.example.com` pointing at your Tailscale IP resolves for everyone but only *connects* for devices
on your tailnet. You get a memorable name without exposing anything. The URL keeps its port
(`http://gym.example.com:8101`) and stays plain HTTP unless you terminate TLS yourself.

**Publicly, through a tunnel.** A tunnel daemon such as Cloudflare Tunnel dials out from the server,
so it works behind CGNAT or DS-Lite where port forwarding is impossible, and gives you HTTPS on your
own domain. Put an access policy in front of it — the app has no login of its own, and an open
endpoint is an open database.

**GitHub Pages will not work.** Pages serves static files only. The frontend would load and then fail
every API call, because there is no Python process and no database behind it.
