#!/usr/bin/env python3
"""Read-only sidecar dashboard for aprs-lite."""

from __future__ import annotations

import atexit
import os
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from collector import JournalCollector, RuntimeState, load_env_file, service_status, system_snapshot
from db import SidecarDB

try:
    from flask_socketio import SocketIO

    HAS_SOCKETIO = True
except ImportError:
    HAS_SOCKETIO = False

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = "/home/pi/aprs-sidecar-dashboard/data/sidecar.db"
DEFAULT_CONFIG_PATH = "/opt/aprs-lite/config.env"
DEFAULT_SERVICE_NAME = "aprs-direwolf"
DEFAULT_TUI_SERVICE = "aprs-lite-tui"
DEFAULT_WATCHDOG_SERVICE = "aprs-watchdog"
DEFAULT_JOURNAL_UNIT = "aprs-direwolf"
DEFAULT_PORT = 5080


def load_settings() -> dict:
    env = {
        "DB_PATH": os.environ.get("DB_PATH", DEFAULT_DB_PATH),
        "CONFIG_PATH": os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH),
        "SERVICE_NAME": os.environ.get("SERVICE_NAME", DEFAULT_SERVICE_NAME),
        "TUI_SERVICE": os.environ.get("TUI_SERVICE", DEFAULT_TUI_SERVICE),
        "WATCHDOG_SERVICE": os.environ.get("WATCHDOG_SERVICE", DEFAULT_WATCHDOG_SERVICE),
        "JOURNAL_UNIT": os.environ.get("JOURNAL_UNIT", DEFAULT_JOURNAL_UNIT),
        "DASHBOARD_HOST": os.environ.get("DASHBOARD_HOST", "0.0.0.0"),
        "DASHBOARD_PORT": int(os.environ.get("DASHBOARD_PORT", str(DEFAULT_PORT))),
    }
    config_from_file = load_env_file(Path(env["CONFIG_PATH"]))
    env["LITE_CONFIG"] = config_from_file
    env["CALLSIGN"] = config_from_file.get("CALLSIGN", "APRS-LITE")
    env["COMMENT"] = config_from_file.get("COMMENT", "APRS Lite sidecar dashboard")
    env["LAT"] = config_from_file.get("LAT", "")
    env["LON"] = config_from_file.get("LON", "")
    return env


SETTINGS = load_settings()
DB = SidecarDB(SETTINGS["DB_PATH"])
STATE = RuntimeState(
    service_name=SETTINGS["SERVICE_NAME"],
    config_path=SETTINGS["CONFIG_PATH"],
)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.urandom(24).hex()

if HAS_SOCKETIO:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
else:
    socketio = None

COLLECTOR = JournalCollector(
    db=DB,
    state=STATE,
    socketio=socketio,
    journal_unit=SETTINGS["JOURNAL_UNIT"],
)
COLLECTOR.start()
atexit.register(COLLECTOR.stop)


def reload_settings() -> dict:
    global SETTINGS
    SETTINGS = load_settings()
    STATE.service_name = SETTINGS["SERVICE_NAME"]
    STATE.config_path = SETTINGS["CONFIG_PATH"]
    STATE.refresh_config()
    return SETTINGS


def public_config() -> dict:
    settings = reload_settings()
    config = settings["LITE_CONFIG"]
    return {
        "callsign": config.get("CALLSIGN", "APRS-LITE"),
        "comment": config.get("COMMENT", ""),
        "lat": config.get("LAT", ""),
        "lon": config.get("LON", ""),
        "config_path": settings["CONFIG_PATH"],
        "db_path": settings["DB_PATH"],
        "service_name": settings["SERVICE_NAME"],
        "tui_service": settings["TUI_SERVICE"],
    }


def service_rows() -> list[dict]:
    settings = reload_settings()
    names = [
        settings["SERVICE_NAME"],
        settings["TUI_SERVICE"],
        settings["WATCHDOG_SERVICE"],
    ]
    rows = []
    for name in names:
        rows.append({"name": name, "status": service_status(name)})
    return rows


@app.route("/")
def index():
    return render_template(
        "index.html",
        config=public_config(),
        socketio_enabled=HAS_SOCKETIO,
    )


@app.route("/api/frames")
def api_frames():
    return jsonify(
        DB.get_frames(
            n=int(request.args.get("n", 50)),
            offset=int(request.args.get("offset", 0)),
            source=request.args.get("source"),
            data_type=request.args.get("type"),
            since=request.args.get("since"),
            until=request.args.get("until"),
            origin=request.args.get("origin"),
        )
    )


@app.route("/api/positions")
def api_positions():
    since_hours = request.args.get("since_hours")
    return jsonify(
        DB.get_positions(
            since_hours=float(since_hours) if since_hours is not None else None
        )
    )


@app.route("/api/stations")
def api_stations():
    return jsonify(DB.get_stations())


@app.route("/api/stats")
def api_stats():
    summary = COLLECTOR.system_summary()
    stats = DB.get_stats()
    latest_telemetry = DB.get_latest_telemetry()
    snapshot = summary or system_snapshot(SETTINGS["SERVICE_NAME"])
    stats.update(
        {
            "uptime_seconds": snapshot.get("uptime_seconds", 0),
            "cpu_temp": latest_telemetry.get("cpu_temp", snapshot.get("cpu_temp")),
            "cpu_usage": latest_telemetry.get("cpu_usage", snapshot.get("cpu_usage")),
            "ram_usage": latest_telemetry.get("ram_usage", snapshot.get("ram_usage")),
            "disk_usage": latest_telemetry.get("disk_usage", snapshot.get("disk_usage")),
            "load_avg_1m": latest_telemetry.get("load_avg_1m", snapshot.get("load_avg_1m")),
            "direwolf_status": snapshot.get("direwolf_status"),
            "hostname": snapshot.get("hostname"),
            "primary_ip": snapshot.get("primary_ip"),
            "last_error": snapshot.get("last_error"),
            "counts": snapshot.get("counts", {}),
            "last_frame": snapshot.get("last_frame", ""),
            "last_frame_time_runtime": snapshot.get("last_frame_time"),
        }
    )
    return jsonify(stats)


@app.route("/api/telemetry")
def api_telemetry():
    return jsonify(DB.get_telemetry(hours=int(request.args.get("hours", 24))))


@app.route("/api/export/frames.csv")
def api_export_frames():
    csv_data = DB.export_frames_csv(
        since=request.args.get("since"),
        until=request.args.get("until"),
        source=request.args.get("source"),
        data_type=request.args.get("type"),
        origin=request.args.get("origin"),
    )
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=aprs_lite_frames.csv"},
    )


@app.route("/api/export/telemetry.csv")
def api_export_telemetry():
    csv_data = DB.export_telemetry_csv(hours=int(request.args.get("hours", 24)))
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=aprs_lite_telemetry.csv"},
    )


@app.route("/api/system/services")
def api_system_services():
    return jsonify(service_rows())


@app.route("/api/system/summary")
def api_system_summary():
    summary = COLLECTOR.system_summary()
    summary["services"] = service_rows()
    summary["config"] = public_config()
    summary["db"] = {
        "path": SETTINGS["DB_PATH"],
        "stats": DB.get_stats(),
    }
    return jsonify(summary)


@app.route("/api/system/config")
def api_system_config():
    return jsonify(public_config())


@app.route("/api/system/config/raw")
def api_system_config_raw():
    reload_settings()
    return Response(STATE.config_raw, mimetype="text/plain; charset=utf-8")


if socketio is not None:
    @socketio.on("connect")
    def ws_connect():
        for frame in reversed(DB.get_frames(n=30)):
            socketio.emit("new_frame", frame)
        latest = DB.get_latest_telemetry()
        if latest:
            socketio.emit("telemetry", latest)


def main():
    if socketio is not None:
        socketio.run(
            app,
            host=SETTINGS["DASHBOARD_HOST"],
            port=SETTINGS["DASHBOARD_PORT"],
            allow_unsafe_werkzeug=True,
        )
    else:
        app.run(
            host=SETTINGS["DASHBOARD_HOST"],
            port=SETTINGS["DASHBOARD_PORT"],
            threaded=True,
        )


if __name__ == "__main__":
    main()
