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

## Food: first deployment

The Food tab needs a catalogue, which is imported once inside the running container. It needs
outbound internet (fdc.nal.usda.gov, static.openfoodfacts.org); the running app only reaches out
for live barcode lookups and to MordorCook.

If your compose file lives outside the checkout (as in the example above), give it the Food
variables too — compose only passes what the file names. The values come from a `.env` next to that
compose file (or `--env-file`); every one has a working default:

```yaml
    environment:
      MORDORCOOK_URL: "${MORDORCOOK_URL:-http://100.108.145.60:8105}"
      FOOD_OFF_LIVE: "${FOOD_OFF_LIVE:-true}"
      FOOD_AI_ENABLED: "${FOOD_AI_ENABLED:-false}"
      ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY:-}"
      FOOD_AI_MODEL: "${FOOD_AI_MODEL:-claude-haiku-4-5}"
```

Then pull and rebuild as usual, open the Food tab and tap **Download food database**. That is all —
the import runs on the server in the background and the tab shows its progress. By hand instead:

```bash
git -C /srv/gymtrack/app pull --ff-only
docker compose -f /srv/compose/gymtrack.yml up -d --build

# basic foods from USDA (a few seconds after the download)
docker compose -f /srv/compose/gymtrack.yml exec gymtrack python scripts/import_basic.py --build
# Open Food Facts products sold here (10–30 min, one core, flat memory)
docker compose -f /srv/compose/gymtrack.yml exec gymtrack python scripts/import_off.py
```

Both write to `data/foods.db`, which you can delete and re-import at any time. Existing training data
is not touched: the Food tables are added to `gymtrack.db` next to the old ones on first start.

To run the OFF import with lower priority next to other services, prefix it with `nice`:
`… exec gymtrack nice -n 19 python scripts/import_off.py`.

## Live barcode scanning (HTTPS)

Browsers only open a live camera on a secure page. Over `http://100.108.145.60:8101` the Scan button
therefore takes a photo instead. Tailscale can put GymTrack on HTTPS with a real certificate, still
reachable only inside your tailnet:

```bash
sudo tailscale serve --bg 8101
tailscale serve status        # prints the https://<server>.<tailnet>.ts.net address
```

Open that address on the phone (and add it to the home screen again). The old `http://` address keeps
working with photo scanning. If HTTPS is not enabled for your tailnet yet, the command says where
to turn it on in the Tailscale admin console. `sudo tailscale serve --https=443 off` undoes it.

## Backups

Your data is `data/gymtrack.db`; `data/foods.db` is the re-importable catalogue and needs no backup.
There is also an endpoint that dumps everything you entered, Food included, as JSON:

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
