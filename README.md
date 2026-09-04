# aprs-lite-sidecar-dashboard

Dashboard that runs next to `aprs-lite` without modifying its radio configuration.

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

## Production service

Use Gunicorn with one Eventlet worker. Do not run the Flask/Werkzeug server in
parallel and do not enable both `aprs-dashboard` and
`aprs-lite-sidecar-dashboard`: two services bound to port `5080` can cause
restart loops and starve the Pi audio stack.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aprs-dashboard
curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:5080/login
```

Gunicorn keeps a master process and one worker process; that is one dashboard
instance, not a duplicate service.

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
