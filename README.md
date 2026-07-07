# aprs-lite-sidecar-dashboard

Read-only dashboard that runs next to `aprs-lite` without modifying it.

## What it reads

- `journalctl -u aprs-direwolf`
- `/opt/aprs-lite/config.env`
- `systemctl is-active aprs-direwolf`
- system stats sampled locally

## What it does not do

- no config writes
- no service restart/start/stop routes
- no Wi-Fi management
- no direct edits to `/opt/aprs-lite`

## Default paths

- app dir: `/home/pi/aprs-sidecar-dashboard`
- database: `/home/pi/aprs-sidecar-dashboard/data/sidecar.db`
- lite config: `/opt/aprs-lite/config.env`
- dashboard port: `5080`

## Run manually

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
DB_PATH=/home/pi/aprs-sidecar-dashboard/data/sidecar.db \
CONFIG_PATH=/opt/aprs-lite/config.env \
DASHBOARD_PORT=5080 \
./venv/bin/python3 app.py
```

## Suggested deploy layout

```text
/home/pi/aprs-sidecar-dashboard/
  app.py
  collector.py
  db.py
  aprs_parser.py
  requirements.txt
  templates/
  static/
  data/
```

## Notes

- The dashboard keeps its own SQLite history.
- It can bootstrap from the last 200 Direwolf journal lines on first start.
- Live updates use Socket.IO when available, and the page also polls the REST API.
