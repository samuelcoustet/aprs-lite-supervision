#!/usr/bin/env python3
"""Sidecar dashboard for aprs-lite."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

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

_SECRET_FILE = Path("/home/pi/aprs-sidecar-dashboard/data/.secret_key")

def _load_secret_key() -> str:
    try:
        if _SECRET_FILE.exists():
            return _SECRET_FILE.read_text().strip()
    except Exception:
        pass
    key = os.urandom(24).hex()
    try:
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(key)
        _SECRET_FILE.chmod(0o600)
    except Exception:
        pass
    return key

app.config["SECRET_KEY"] = _load_secret_key()

# ── Auth helpers ──────────────────────────────────────────────────────────────

AUTH_FILE = Path("/home/pi/aprs-sidecar-dashboard/data/auth.json")
DEFAULT_PASSWORD = "aprs"


def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def _load_auth() -> dict:
    try:
        return json.loads(AUTH_FILE.read_text())
    except Exception:
        return {"password_hash": _hash_pw(DEFAULT_PASSWORD)}


def _save_auth(data: dict):
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(data))


def _check_password(pw: str) -> bool:
    return _load_auth().get("password_hash") == _hash_pw(pw)


@app.before_request
def require_login():
    if request.path.startswith("/static") or request.path.startswith("/login"):
        return
    if not session.get("logged_in"):
        return redirect(url_for("login"))

if HAS_SOCKETIO:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
else:
    socketio = None

# ── Connected clients tracking ────────────────────────────────────────────────
_connected_clients: dict[str, str] = {}  # sid → ip
_clients_lock = threading.Lock()

def _on_chat_event(event_type: str, msg_id: str, parsed: dict):
    """Callback wired to JournalCollector for ACK/REJ/inbound messages."""
    cfg = load_settings().get("LITE_CONFIG", {})
    own = cfg.get("CALLSIGN", "").upper()
    src = (parsed.get("source") or "").upper()
    if event_type == "ack":
        # ACK sent by dst station back to us — src=their callsign, msg_id=our msg_no
        DB.ack_chat(src, msg_id)
        socketio.emit("msg_acked", {"src": src, "msg_no": msg_id})
    elif event_type == "msg":
        # Inbound message addressed to our callsign
        dst = (parsed.get("msg_to") or "").strip().upper()
        if dst == own:
            text = parsed.get("comment") or parsed.get("msg_text") or ""
            DB.insert_chat("in", src, own, text, msg_id)
            socketio.emit("chat_in", {
                "src": src, "dst": own, "text": text, "msg_no": msg_id,
                "timestamp": parsed.get("timestamp", ""),
            })


COLLECTOR = JournalCollector(
    db=DB,
    state=STATE,
    socketio=socketio,
    journal_unit=SETTINGS["JOURNAL_UNIT"],
    on_msg_event=_on_chat_event,
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


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if _check_password(pw):
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Mot de passe incorrect"
    return """<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<title>APRS Lite — Connexion</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'JetBrains Mono',monospace;background:#0a0e14;color:#e8edf3;display:flex;align-items:center;justify-content:center;height:100vh}
.box{background:#111820;border:1px solid #1e2a3a;border-radius:10px;padding:36px 40px;width:340px}
h1{font-size:18px;color:#00ff88;margin-bottom:24px;letter-spacing:2px;font-family:'Exo 2',sans-serif}
label{font-size:11px;color:#8899aa;display:block;margin-bottom:4px}
input{width:100%;background:#0a0e14;border:1px solid #1e2a3a;border-radius:4px;color:#e8edf3;font-size:13px;padding:8px 10px;outline:none;font-family:inherit;margin-bottom:16px}
input:focus{border-color:#00aaff}
button{width:100%;padding:9px;border:1px solid #00ff88;border-radius:4px;background:rgba(0,255,136,.08);color:#00ff88;font-size:13px;cursor:pointer;font-family:inherit}
button:hover{background:rgba(0,255,136,.18)}
.err{color:#ff4466;font-size:11px;margin-top:10px;text-align:center}
</style></head><body>
<div class="box">
  <h1>APRS LITE SIDECAR</h1>
  <form method="post">
    <label>Mot de passe</label>
    <input type="password" name="password" autofocus placeholder="••••">
    <button type="submit">Connexion</button>
    """ + (f'<div class="err">{error}</div>' if error else "") + """
  </form>
</div></body></html>"""


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/auth/password", methods=["POST"])
def api_auth_password():
    data = request.get_json(force=True) or {}
    old = data.get("old_password", "")
    new = data.get("new_password", "")
    if not _check_password(old):
        return jsonify({"ok": False, "error": "Ancien mot de passe incorrect"}), 403
    if len(new) < 4:
        return jsonify({"ok": False, "error": "Nouveau mot de passe trop court (min 4 caractères)"}), 400
    _save_auth({"password_hash": _hash_pw(new)})
    return jsonify({"ok": True})


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


@app.route("/api/system/network")
def api_system_network():
    from collector import get_network_info
    return jsonify(get_network_info())


@app.route("/api/system/clock")
def api_system_clock():
    from collector import get_clock_info
    return jsonify(get_clock_info())


@app.route("/api/weather")
def api_weather():
    frames = DB.get_frames(n=50, data_type="weather")
    return jsonify(frames)


@app.route("/api/system/monitor")
def api_system_monitor():
    import json as _json
    journal_unit = SETTINGS["JOURNAL_UNIT"]

    _ansi_only = __import__("re").compile(r"^(\x1b\[[0-9;]*m)+$")

    def generate():
        try:
            env = {
                "SYSTEMD_COLORS": "1",
                "TERM": "xterm-256color",
                "PATH": "/usr/bin:/bin",
                "HOME": "/root",
            }
            proc = __import__("subprocess").Popen(
                ["journalctl", "-u", journal_unit, "-f", "--output=cat", "-n", "80", "--no-pager"],
                stdout=__import__("subprocess").PIPE,
                stderr=__import__("subprocess").DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            # Direwolf outputs the ANSI color code on its own line before [ig>tx] data.
            # Buffer color-only lines and prepend them to the next data line.
            pending = ""
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                if _ansi_only.match(line):
                    pending = line  # hold color prefix for next line
                    continue
                combined = pending + line
                pending = ""
                try:
                    yield f"data: {_json.dumps(combined)}\n\n"
                except GeneratorExit:
                    proc.terminate()
                    return
            proc.terminate()
        except Exception as exc:
            yield f"data: {_json.dumps('Erreur: ' + str(exc))}\n\n"

    return app.response_class(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/api/system/pi")
def api_system_pi():
    import platform
    from collector import get_pi_info
    return jsonify(get_pi_info())


@app.route("/api/system/audio")
def api_system_audio():
    from collector import get_audio_info
    return jsonify(get_audio_info())


# ── Action endpoints ──────────────────────────────────────────────────────────

@app.route("/api/config/save", methods=["POST"])
def api_config_save():
    from collector import save_config_bulk, EDITABLE_KEYS
    data = request.get_json(force=True) or {}
    filtered = {k: v for k, v in data.items() if k in EDITABLE_KEYS}
    if not filtered:
        return jsonify({"ok": False, "error": "Aucune clé valide"}), 400
    ok, err = save_config_bulk(filtered)
    if ok:
        reload_settings()
    return jsonify({"ok": ok, "error": err})


@app.route("/api/direwolf/restart", methods=["POST"])
def api_direwolf_restart():
    from collector import restart_direwolf
    ok, err = restart_direwolf()
    return jsonify({"ok": ok, "error": err})


@app.route("/api/aioc/detect", methods=["POST"])
def api_aioc_detect():
    from collector import detect_aioc_full
    return jsonify(detect_aioc_full())


# ── APRS messaging ────────────────────────────────────────────────────────────

def _retry_worker():
    """Background thread: RF→IS retry after 30s, fail after 90s."""
    import time
    while True:
        time.sleep(10)
        try:
            cfg = load_settings().get("LITE_CONFIG", {})
            src = cfg.get("CALLSIGN", "N0CALL").upper()
            server = cfg.get("IGSERVER", "euro.aprs2.net")
            passcode = cfg.get("PASSCODE", "0")
            for msg in DB.get_pending_retries(rf_timeout_s=30, is_timeout_s=90):
                row_id = msg["id"]
                if msg["_action"] == "retry_is":
                    packet_is = _encode_aprs_message(src, msg["dst"], msg["text"], msg["msg_no"], via="is")
                    ok, _ = _send_aprs_is(packet_is, server, 14580, src, passcode)
                    if ok:
                        DB.set_chat_status(row_id, "retry_is", via="is")
                        socketio.emit("chat_status", {"id": row_id, "msg_no": msg["msg_no"],
                                                      "dst": msg["dst"], "status": "retry_is", "via": "is"})
                elif msg["_action"] == "failed":
                    DB.set_chat_status(row_id, "failed")
                    socketio.emit("chat_status", {"id": row_id, "msg_no": msg["msg_no"],
                                                  "dst": msg["dst"], "status": "failed"})
        except Exception:
            pass

import socket as _socket
import threading as _threading

_retry_thread = _threading.Thread(target=_retry_worker, daemon=True)
_retry_thread.start()

_msg_counter = 0
_msg_counter_lock = _threading.Lock()

def _next_msg_no() -> str:
    global _msg_counter
    with _msg_counter_lock:
        _msg_counter = (_msg_counter % 999) + 1
        return str(_msg_counter)

def _encode_aprs_message(src: str, dst: str, text: str, msg_no: str, via: str = "is") -> str:
    dst_pad = dst.upper().ljust(9)
    path = "WIDE1-1" if via == "rf" else "TCPIP*"
    dest = "APRS" if via == "rf" else "APNW01"
    return f"{src.upper()}>{dest},{path}::{dst_pad}:{text}{{{msg_no}"

def _send_rf(packet: str) -> tuple[bool, str]:
    from collector import send_kiss_packet
    return send_kiss_packet(packet)

def _encode_aprs_ack(src: str, dst: str, msg_no: str) -> str:
    dst_pad = dst.upper().ljust(9)
    return f"{src.upper()}>APNW01,TCPIP*::{dst_pad}:ack{msg_no}"

def _send_aprs_is(packet: str, server: str, port: int, callsign: str, passcode: str) -> tuple[bool, str]:
    try:
        with _socket.create_connection((server, port), timeout=10) as s:
            banner = s.recv(512).decode("utf-8", errors="replace")
            login = f"user {callsign} pass {passcode} vers aprs-lite-supervision 1.0\r\n"
            s.sendall(login.encode())
            s.recv(256)
            s.sendall((packet + "\r\n").encode())
        return True, ""
    except Exception as e:
        return False, str(e)


@app.route("/api/chat/messages")
def api_chat_messages():
    if not session.get("logged_in"):
        return jsonify([])
    cfg = reload_settings()["LITE_CONFIG"]
    own = cfg.get("CALLSIGN", "").upper()
    # outbound from our DB
    out_msgs = [dict(m, _type="out") for m in DB.get_chat(100)]
    # inbound: frames table where data_type=message and msg_to=own
    with DB._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM frames WHERE data_type IN ('message','message_ack') ORDER BY id DESC LIMIT 200"
        ).fetchall()
    in_msgs = []
    import re as _re
    _msg_to_re = _re.compile(r'^:([A-Z0-9\-]{1,9})\s*:', _re.IGNORECASE)
    for r in rows:
        rd = dict(r)
        src = (rd.get("source") or "").upper()
        # extract addressee from raw: after first ':' in info field
        raw = rd.get("raw") or ""
        msg_to = ""
        try:
            info_start = raw.index(">")
            colon = raw.index(":", info_start)
            m = _msg_to_re.match(raw[colon+1:])
            if m:
                msg_to = m.group(1).strip().upper()
        except (ValueError, AttributeError):
            pass
        if msg_to == own or src == own:
            rd["msg_to"] = msg_to
            in_msgs.append(rd)
    return jsonify({"own": own, "outbound": out_msgs, "inbound": in_msgs})


@app.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Non authentifié"}), 401
    data = request.get_json(force=True)
    dst = (data.get("dst") or "").strip().upper()
    text = (data.get("text") or "").strip()[:67]
    if not dst or not text:
        return jsonify({"ok": False, "error": "dst et text requis"}), 400
    cfg = reload_settings()["LITE_CONFIG"]
    src = cfg.get("CALLSIGN", "N0CALL").upper()
    server = cfg.get("IGSERVER", "euro.aprs2.net")
    passcode = cfg.get("PASSCODE", "0")
    msg_no = _next_msg_no()
    from datetime import datetime, timezone as _tz

    # 1. RF via Direwolf/kissutil (prioritaire)
    packet_rf = _encode_aprs_message(src, dst, text, msg_no, via="rf")
    ok, err = _send_rf(packet_rf)
    via = "rf"

    # 2. Fallback APRS-IS si KISS inaccessible
    if not ok:
        packet_is = _encode_aprs_message(src, dst, text, msg_no, via="is")
        ok, err = _send_aprs_is(packet_is, server, 14580, src, passcode)
        via = "is"

    packet = packet_rf if via == "rf" else packet_is
    if ok:
        row_id = DB.insert_chat("out", src, dst, text, msg_no, via=via)
        socketio.emit("chat_out", {
            "id": row_id, "src": src, "dst": dst, "text": text, "msg_no": msg_no, "via": via,
            "timestamp": datetime.now(_tz.utc).isoformat(),
        })
    return jsonify({"ok": ok, "error": err, "packet": packet, "msg_no": msg_no, "via": via})


@app.route("/api/beacon/send", methods=["POST"])
def api_beacon_send():
    from collector import send_kiss_packet, _make_beacon_packet
    cfg = reload_settings()["LITE_CONFIG"]
    packet = _make_beacon_packet(cfg)
    ok, err = send_kiss_packet(packet)
    return jsonify({"ok": ok, "error": err, "packet": packet})


@app.route("/api/weather/send", methods=["POST"])
def api_weather_send():
    from collector import send_kiss_packet, make_weather_packet, get_sensor_data, _fetch_openmeteo
    cfg = reload_settings()["LITE_CONFIG"]
    callsign = cfg.get("CALLSIGN", "N0CALL")
    try:
        lat = float(cfg.get("LAT", "0"))
        lon = float(cfg.get("LON", "0"))
    except ValueError:
        return jsonify({"ok": False, "error": "LAT/LON invalides dans config"}), 400
    sensor = get_sensor_data()
    om = _fetch_openmeteo()
    if sensor.get("ok"):
        # Hybride : valeurs locales + vent/pluie Open-Meteo
        data = dict(sensor)
        data["wind_speed"]     = om.get("wind_speed")
        data["wind_dir"]       = om.get("wind_dir")
        data["rain_1h"]        = om.get("rain_1h")
        data["weather_source"] = "hybrid"
    elif om:
        # Capteur absent : tout Open-Meteo
        data = {
            "temperature":    om["temperature"],
            "humidity":       om["humidity"],
            "pressure":       om["pressure"],
            "wind_speed":     om.get("wind_speed"),
            "wind_dir":       om.get("wind_dir"),
            "rain_1h":        om.get("rain_1h"),
            "weather_source": "open-meteo",
        }
    else:
        return jsonify({"ok": False, "error": "Capteur absent et Open-Meteo indisponible"}), 503
    packet = make_weather_packet(callsign, lat, lon, data)
    ok, err = send_kiss_packet(packet)
    return jsonify({"ok": ok, "error": err, "packet": packet})


@app.route("/api/sensor")
def api_sensor():
    from collector import get_sensor_data, get_box_sensor_data, _fetch_openmeteo
    outdoor = get_sensor_data()
    if not outdoor.get("ok"):
        om = _fetch_openmeteo()
        if om:
            outdoor = {
                "ok": True,
                "source": "open-meteo",
                "temperature": om.get("temperature"),
                "humidity":    om.get("humidity"),
                "pressure":    om.get("pressure"),
                "wind_speed":  om.get("wind_speed"),
                "wind_dir":    om.get("wind_dir"),
                "rain_1h":     om.get("rain_1h"),
                "weather_code": om.get("weather_code"),
                "chip":        "Open-Meteo",
            }
    box = get_box_sensor_data()
    return jsonify({"outdoor": outdoor, "box": box})


@app.route("/api/wifi/scan", methods=["POST"])
def api_wifi_scan():
    from collector import wifi_scan, wifi_current
    return jsonify({"networks": wifi_scan(), "current": wifi_current()})


@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    from collector import wifi_connect
    data = request.get_json(force=True) or {}
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "").strip()
    if not ssid:
        return jsonify({"ok": False, "error": "SSID requis"}), 400
    ok, msg = wifi_connect(ssid, password)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/wifi/disconnect", methods=["POST"])
def api_wifi_disconnect():
    from collector import wifi_disconnect
    ok, msg = wifi_disconnect()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/clock/sync", methods=["POST"])
def api_clock_sync():
    from collector import ntp_sync
    ok, msg = ntp_sync()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/clock/set", methods=["POST"])
def api_clock_set():
    from collector import clock_set
    data = request.get_json(force=True) or {}
    value = data.get("datetime", "").strip()
    ok, msg = clock_set(value)
    return jsonify({"ok": ok, "message": msg})


# ── direwolf.conf ──────────────────────────────────────────────────────────────

@app.route("/api/system/direwolf")
def api_direwolf_conf():
    from collector import parse_direwolf_conf
    return jsonify(parse_direwolf_conf())


@app.route("/api/system/direwolf/raw", methods=["GET", "POST"])
def api_direwolf_conf_raw():
    from collector import write_direwolf_conf_raw, DIREWOLF_CONF
    if request.method == "GET":
        try:
            return Response(DIREWOLF_CONF.read_text(encoding="utf-8", errors="replace"), mimetype="text/plain; charset=utf-8")
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    data = request.get_json(force=True) or {}
    content = data.get("content", "")
    ok, err = write_direwolf_conf_raw(content)
    if ok:
        from collector import restart_direwolf
        restart_direwolf()
    return jsonify({"ok": ok, "error": err})


# ── config.env raw write ───────────────────────────────────────────────────────

@app.route("/api/config/raw", methods=["POST"])
def api_config_raw():
    data = request.get_json(force=True) or {}
    content = data.get("content", "")
    try:
        Path(SETTINGS["CONFIG_PATH"]).write_text(content, encoding="utf-8")
        reload_settings()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── VU-mètre AIOC (SSE) ───────────────────────────────────────────────────────

@app.route("/api/audio/levels")
def api_audio_levels():
    from collector import stream_audio_levels
    return app.response_class(
        stream_audio_levels(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ── OTA Update ────────────────────────────────────────────────────────────────

@app.route("/api/system/update/upload", methods=["POST"])
def api_update_upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "Aucun fichier"}), 400
    dest = Path("/tmp/ota_update.deb")
    f.save(str(dest))
    try:
        r = subprocess.run(["dpkg-deb", "-I", str(dest)], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return jsonify({"ok": False, "error": r.stderr.strip() or "Fichier .deb invalide"}), 400
        return jsonify({"ok": True, "info": r.stdout.strip()})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "dpkg-deb non disponible"}), 500


@app.route("/api/system/update/install")
def api_update_install():
    import json as _json

    def generate():
        try:
            proc = subprocess.Popen(
                ["sudo", "dpkg", "-i", "/tmp/ota_update.deb"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in proc.stdout:
                yield f"data: {_json.dumps(line.rstrip())}\n\n"
            proc.wait()
            yield f"data: {_json.dumps('EXIT:' + str(proc.returncode))}\n\n"
        except Exception as exc:
            yield f"data: {_json.dumps('Erreur: ' + str(exc))}\n\n"

    return app.response_class(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ── Reboot Pi ─────────────────────────────────────────────────────────────────

@app.route("/api/system/reboot", methods=["POST"])
def api_system_reboot():
    threading.Timer(1.5, lambda: subprocess.run(["sudo", "reboot"])).start()
    return jsonify({"ok": True, "message": "Redémarrage dans 1.5s…"})


# ── Service start/stop/restart ────────────────────────────────────────────────

SERVICE_WHITELIST = {"aprs-direwolf", "aprs-lite-tui", "aprs-watchdog", "aprs-sidecar-dashboard"}


@app.route("/api/system/service/<name>/<action>", methods=["POST"])
def api_service_action(name, action):
    if name not in SERVICE_WHITELIST:
        return jsonify({"ok": False, "error": f"Service '{name}' non autorisé"}), 403
    if action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "error": f"Action '{action}' invalide"}), 400
    try:
        r = subprocess.run(
            ["sudo", "/bin/systemctl", action, name],
            capture_output=True, text=True, timeout=20,
        )
        return jsonify({"ok": r.returncode == 0, "error": r.stderr.strip()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── System events SSE (watchdog log) ─────────────────────────────────────────

@app.route("/api/system/events")
def api_system_events():
    from collector import stream_system_events
    return app.response_class(
        stream_system_events(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ── Watchdog log ──────────────────────────────────────────────────────────────

@app.route("/api/system/watchdog")
def api_system_watchdog():
    import re as _re
    n = int(request.args.get("n", 200))
    log_path = Path("/opt/aprs-lite/logs/watchdog.log")
    if not log_path.exists():
        return jsonify([])
    try:
        r = subprocess.run(["tail", "-n", str(n), str(log_path)], capture_output=True, text=True, timeout=5)
        lines = r.stdout.splitlines()
        pat = _re.compile(r'^(\S+\s+\S+)\s+(\w+)\s+(.*)')
        result = []
        for line in lines:
            m = pat.match(line)
            if m:
                result.append({"ts": m.group(1), "level": m.group(2), "message": m.group(3)})
            elif line.strip():
                result.append({"ts": "", "level": "INFO", "message": line})
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── aprs.fi status ───────────────────────────────────────────────────────────

@app.route("/api/aprsfi")
def api_aprsfi():
    from collector import get_aprsfi_status
    cfg = SETTINGS["LITE_CONFIG"]
    callsign = cfg.get("CALLSIGN", "")
    apikey = cfg.get("APRSFI_KEY", "")
    return jsonify(get_aprsfi_status(callsign, apikey))


# ── CSV log history ───────────────────────────────────────────────────────────

@app.route("/api/logs/dates")
def api_logs_dates():
    from collector import read_log_dates
    return jsonify(read_log_dates())


@app.route("/api/logs/frames")
def api_logs_frames():
    import csv as _csv, io as _io
    from collector import read_log_frames
    date_str = request.args.get("date", "")
    fmt = request.args.get("format", "json")
    cfg = SETTINGS["LITE_CONFIG"]
    try:
        own_lat = float(cfg.get("LAT", ""))
        own_lon = float(cfg.get("LON", ""))
    except Exception:
        own_lat = own_lon = None
    frames = read_log_frames(date_str, own_lat, own_lon)
    if fmt == "csv":
        if not frames:
            return Response("", mimetype="text/csv")
        out = _io.StringIO()
        w = _csv.DictWriter(out, fieldnames=frames[0].keys())
        w.writeheader(); w.writerows(frames)
        return Response(
            out.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=aprs_{date_str}.csv"},
        )
    return jsonify(frames)


# ── Analyse (stats agrégées) ─────────────────────────────────────────────────

_analyse_cache: dict[str, tuple[float, dict]] = {}
_ANALYSE_CACHE_TTL = 60  # seconds

@app.route("/api/analyse")
def api_analyse():
    import time as _t
    hours = request.args.get("hours", "1")
    since_hours = float(hours) if hours and hours != "all" else None
    cache_key = str(since_hours)
    cached = _analyse_cache.get(cache_key)
    if cached and _t.time() - cached[0] < _ANALYSE_CACHE_TTL:
        return jsonify(cached[1])
    result = DB.get_analyse(since_hours=since_hours)
    cfg = SETTINGS["LITE_CONFIG"]
    try:
        own_lat = float(cfg.get("LAT", ""))
        own_lon = float(cfg.get("LON", ""))
        from collector import haversine
        for st in result["stations"]:
            lat, lon = st.get("last_lat"), st.get("last_lon")
            if lat is not None and lon is not None:
                d, b = haversine(own_lat, own_lon, lat, lon)
                st["distance_km"] = d
                st["bearing_deg"] = b
    except Exception:
        pass
    _analyse_cache[cache_key] = (_t.time(), result)
    return jsonify(result)


# ── RF Coverage ──────────────────────────────────────────────────────────────

import threading as _cov_thr
_cov_proc = [None]
_cov_lock = _cov_thr.Lock()
_COV_PNG  = os.path.join(os.path.dirname(__file__), "coverage_cache.png")
_COV_META = _COV_PNG + ".json"

@app.route("/api/coverage/status")
def api_coverage_status():
    import json as _json
    with _cov_lock:
        running = _cov_proc[0] is not None and _cov_proc[0].poll() is None
    if running:
        return jsonify({"status": "running"})
    if os.path.exists(_COV_META):
        try:
            meta = _json.loads(open(_COV_META).read())
            meta["status"] = "done"
            return jsonify(meta)
        except Exception:
            pass
    return jsonify({"status": "idle"})

@app.route("/api/coverage/generate", methods=["POST"])
def api_coverage_generate():
    import subprocess, json as _json
    data = request.get_json(silent=True) or {}
    radius_km = float(data.get("radius_km", 20))
    grid_size  = int(data.get("grid_size",  128))
    height_m   = float(data.get("height_m", 15))
    with _cov_lock:
        if _cov_proc[0] is not None and _cov_proc[0].poll() is None:
            return jsonify({"status": "already_running"})
        cfg = SETTINGS["LITE_CONFIG"]
        lat = float(cfg.get("LAT", 0))
        lon = float(cfg.get("LON", 0))
        cmd = [
            "python3",
            os.path.join(os.path.dirname(__file__), "coverage_gen.py"),
            str(lat), str(lon),
            "--height", str(height_m),
            "--radius", str(radius_km),
            "--grid",   str(grid_size),
            "--out",    _COV_PNG,
        ]
        _cov_proc[0] = subprocess.Popen(cmd, cwd=os.path.dirname(__file__))
    return jsonify({"status": "started"})

@app.route("/api/coverage/image")
def api_coverage_image():
    from flask import send_file
    if os.path.exists(_COV_PNG):
        return send_file(_COV_PNG, mimetype="image/png")
    return ("", 404)


# ── Connected clients ─────────────────────────────────────────────────────────

@app.route("/api/system/clients")
def api_system_clients():
    with _clients_lock:
        clients = list(_connected_clients.values())
    # deduplicate IPs
    unique = list(dict.fromkeys(clients))
    return jsonify({"count": len(unique), "ips": unique})


# ── Export CSV preview ────────────────────────────────────────────────────────

@app.route("/api/export/frames/preview")
def api_export_frames_preview():
    n = int(request.args.get("n", 20))
    frames = DB.get_frames(
        n=n,
        source=request.args.get("source"),
        data_type=request.args.get("type"),
        since=request.args.get("since"),
        until=request.args.get("until"),
        origin=request.args.get("origin"),
    )
    headers = ["id", "timestamp", "source", "destination", "path", "data_type", "origin", "lat", "lon", "comment", "raw"]
    rows = [[f.get(h) for h in headers] for f in frames]
    return jsonify({"headers": headers, "rows": rows})


# ── Backup / Restore ─────────────────────────────────────────────────────────

BACKUP_DIR = Path("/home/pi/aprs-sidecar-dashboard/backups")

@app.route("/api/backup/create", methods=["POST"])
def api_backup_create():
    import tarfile, datetime
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        archive = BACKUP_DIR / f"backup_{ts}.tar.gz"
        sources = [
            "/opt/aprs-lite/config.env",
            "/opt/aprs-lite/direwolf.conf",
            "/home/pi/aprs-sidecar-dashboard/data",
        ]
        with tarfile.open(str(archive), "w:gz") as tar:
            for src in sources:
                p = Path(src)
                if p.exists():
                    tar.add(str(p), arcname=p.name if p.is_file() else p.name)
        return jsonify({"ok": True, "file": archive.name, "size": archive.stat().st_size})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/backup/list")
def api_backup_list():
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(BACKUP_DIR.glob("backup_*.tar.gz"), reverse=True)
        return jsonify([
            {"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime}
            for f in files
        ])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backup/download/<name>")
def api_backup_download(name):
    import re as _re
    from flask import send_file
    if not _re.match(r'^backup_\d{8}_\d{6}\.tar\.gz$', name):
        return jsonify({"error": "Nom invalide"}), 400
    f = BACKUP_DIR / name
    if not f.exists():
        return jsonify({"error": "Fichier introuvable"}), 404
    return send_file(str(f), as_attachment=True, download_name=name)


@app.route("/api/backup/restore/<name>", methods=["POST"])
def api_backup_restore(name):
    import re as _re, tarfile, shutil
    if not _re.match(r'^backup_\d{8}_\d{6}\.tar\.gz$', name):
        return jsonify({"error": "Nom invalide"}), 400
    f = BACKUP_DIR / name
    if not f.exists():
        return jsonify({"error": "Fichier introuvable"}), 404
    try:
        restored = []
        with tarfile.open(str(f), "r:gz") as tar:
            for member in tar.getmembers():
                if member.name == "config.env":
                    tar.extract(member, path="/opt/aprs-lite", filter="data")
                    restored.append("config.env")
                elif member.name == "direwolf.conf":
                    tar.extract(member, path="/opt/aprs-lite", filter="data")
                    restored.append("direwolf.conf")
        return jsonify({"ok": True, "restored": restored})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/backup/delete/<name>", methods=["POST"])
def api_backup_delete(name):
    import re as _re
    if not _re.match(r'^backup_\d{8}_\d{6}\.tar\.gz$', name):
        return jsonify({"error": "Nom invalide"}), 400
    f = BACKUP_DIR / name
    if not f.exists():
        return jsonify({"error": "Fichier introuvable"}), 404
    f.unlink()
    return jsonify({"ok": True})


# ── Terminal WebSocket (PTY) ──────────────────────────────────────────────────
if socketio is not None:
    import pty, fcntl, struct, termios, select as _select, signal as _signal

    _terminal_fd = None
    _terminal_pid = None

    def _set_winsize(fd, rows, cols):
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def _pty_read_loop():
        global _terminal_fd, _terminal_pid
        while _terminal_fd:
            try:
                r, _, _ = _select.select([_terminal_fd], [], [], 0.1)
                if r:
                    data = os.read(_terminal_fd, 4096)
                    if data:
                        socketio.emit("terminal_output", {"data": data.decode("utf-8", errors="replace")})
            except (OSError, IOError):
                break
        _terminal_fd = None
        _terminal_pid = None
        socketio.emit("terminal_exit", {})

    def _pty_spawn(user="pi"):
        global _terminal_fd, _terminal_pid
        homes = {"pi": "/home/pi", "root": "/root"}
        home = homes.get(user, "/home/pi")
        cmd = ["bash", "-i"] if user == "pi" else ["sudo", "-u", user, "-H", "bash", "-i"]
        exe = cmd[0]
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(exe, cmd, {
                "TERM": "xterm-256color",
                "HOME": home,
                "USER": user,
                "LOGNAME": user,
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            })
        else:
            _terminal_fd = fd
            _terminal_pid = pid
            __import__("threading").Thread(target=_pty_read_loop, daemon=True).start()

    def _pty_kill():
        global _terminal_fd, _terminal_pid
        if _terminal_pid:
            try:
                os.kill(_terminal_pid, _signal.SIGKILL)
                os.waitpid(_terminal_pid, os.WNOHANG)
            except Exception:
                pass
            _terminal_pid = None
        if _terminal_fd:
            try:
                os.close(_terminal_fd)
            except Exception:
                pass
            _terminal_fd = None

    @socketio.on("terminal_start")
    def ws_terminal_start(data=None):
        global _terminal_pid
        if _terminal_pid:
            return
        _pty_spawn((data or {}).get("user", "pi"))

    @socketio.on("terminal_input")
    def ws_terminal_input(data):
        global _terminal_fd
        if _terminal_fd:
            try:
                os.write(_terminal_fd, data["data"].encode("utf-8"))
            except OSError:
                pass

    @socketio.on("terminal_resize")
    def ws_terminal_resize(data):
        global _terminal_fd
        if _terminal_fd:
            _set_winsize(_terminal_fd, data.get("rows", 24), data.get("cols", 80))

    @socketio.on("terminal_restart")
    def ws_terminal_restart(data=None):
        _pty_kill()
        socketio.emit("terminal_exit", {})
        __import__("time").sleep(0.2)
        _pty_spawn((data or {}).get("user", "pi"))

    @socketio.on("terminal_stop")
    def ws_terminal_stop():
        _pty_kill()
        socketio.emit("terminal_exit", {})


if socketio is not None:
    @socketio.on("connect")
    def ws_connect(auth=None):
        try:
            sid = request.sid
            ip = request.environ.get("HTTP_X_FORWARDED_FOR", request.environ.get("REMOTE_ADDR", "?"))
            with _clients_lock:
                _connected_clients[sid] = ip.split(",")[0].strip()
        except Exception:
            pass
        for frame in reversed(DB.get_frames(n=30)):
            socketio.emit("new_frame", frame)
        latest = DB.get_latest_telemetry()
        if latest:
            socketio.emit("telemetry", latest)

    @socketio.on("disconnect")
    def ws_disconnect():
        try:
            with _clients_lock:
                _connected_clients.pop(request.sid, None)
        except Exception:
            pass


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
