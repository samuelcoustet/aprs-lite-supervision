#!/usr/bin/env python3
"""Collectors and messaging helpers for aprs-lite sidecar dashboard."""

from __future__ import annotations

def _get_altitude_m():
    try:
        with open("/opt/aprs-lite/config.env") as f:
            for ln in f:
                if ln.startswith("ALTITUDE_M"):
                    return float(ln.split("=",1)[1].strip().strip('"').strip("'"))
    except Exception: pass
    return 0.0

def _sea_level_pressure(p_hpa, alt_m, t_c):
    """Convert station pressure to sea-level (ISA barometric)."""
    if p_hpa is None or not alt_m: return p_hpa
    try:
        t = float(t_c) if t_c is not None else 15.0
        a = float(alt_m)
        return round(p_hpa * (1 - (0.0065 * a) / (t + 0.0065 * a + 273.15)) ** -5.257, 1)
    except Exception:
        return p_hpa


import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from aprs_parser import parse_aprs_frame
from db import SidecarDB

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\033\[[0-9;]*m")
# [0] rf rx, [0L] local tx, [0H] heard via digi — any channel number
RF_RE = re.compile(r"^\[(\d+)(?:\.\d+)?(L)?(H)?\]\s+(.+)$")
IG_RE = re.compile(r"^\[ig\]\s+(.+)$")
IGTX_RE = re.compile(r"^\[ig>tx\]\s+(.+)$")

_APRS_IS_IGSERVER = Path("/opt/aprs-lite/direwolf.conf")

def _aprs_is_server() -> str:
    try:
        m = re.search(r'^IGSERVER\s+(\S+)', _APRS_IS_IGSERVER.read_text(), re.MULTILINE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "euro.aprs2.net"

def encode_aprs_message(addressee: str, text: str, msg_id: str) -> str:
    return f":{addressee.upper().ljust(9)[:9]}:{text[:67]}{{{msg_id}"

def encode_aprs_ack(addressee: str, msg_id: str) -> str:
    return f":{addressee.upper().ljust(9)[:9]}:ack{msg_id}"

def send_aprs_is(info: str, config: dict) -> tuple[bool, str]:
    """Envoie un info-field APRS via TCP direct à APRS-IS.

    config doit contenir CALLSIGN et PASSCODE.
    """
    callsign = config.get("CALLSIGN", "N0CALL").strip()
    passcode = config.get("PASSCODE", "0").strip()
    server   = _aprs_is_server()
    packet   = f"{callsign}>APRS,TCPIP*:{info}"
    try:
        with socket.create_connection((server, 14580), timeout=10) as s:
            f = s.makefile("rb")
            f.readline()  # bannière
            s.sendall(f"user {callsign} pass {passcode} vers aprs-lite-dashboard 1.0.8\r\n".encode())
            f.readline()  # réponse login
            s.sendall(f"{packet}\r\n".encode())
            time.sleep(0.5)
        return True, ""
    except Exception as e:
        return False, str(e)


def load_env_file(path: Path) -> dict:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def service_status(name: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip() or "inactive"
    except Exception:
        return "unknown"


def get_cpu_temp() -> float | None:
    path = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(path.read_text().strip()) / 1000.0, 1)
    except Exception:
        return None


def get_cpu_usage() -> float | None:
    try:
        load1 = os.getloadavg()[0]
        cpus = os.cpu_count() or 1
        return round((load1 / cpus) * 100.0, 1)
    except Exception:
        return None


def get_ram_usage() -> float | None:
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
        values = {}
        for line in lines:
            key, _, value = line.partition(":")
            values[key] = int(value.strip().split()[0])
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        if total <= 0:
            return None
        used = total - available
        return round((used * 100.0) / total, 1)
    except Exception:
        return None


def get_disk_usage(path: str = "/") -> float | None:
    try:
        usage = shutil.disk_usage(path)
        return round((usage.used * 100.0) / usage.total, 1)
    except Exception:
        return None


def get_uptime_seconds() -> int:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:
        return 0


def get_primary_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_network_info() -> dict:
    info: dict = {"hostname": socket.gethostname(), "primary_ip": get_primary_ip(), "interfaces": [], "wifi": {}}
    try:
        r = subprocess.run(["ip", "-j", "addr", "show"], capture_output=True, text=True, timeout=5)
        ifaces = json.loads(r.stdout)
        for iface in ifaces:
            name = iface.get("ifname", "")
            flags = iface.get("flags", [])
            addrs = [a["local"] for a in iface.get("addr_info", []) if a.get("family") in ("inet", "inet6")]
            info["interfaces"].append({"name": name, "state": iface.get("operstate", "?"), "addrs": addrs, "flags": flags})
    except Exception:
        pass
    try:
        r = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=3)
        ssid = r.stdout.strip()
        if ssid:
            info["wifi"]["ssid"] = ssid
        r2 = subprocess.run(["iwgetid", "-a"], capture_output=True, text=True, timeout=3)
        bssid = r2.stdout.strip().split()[-1] if r2.stdout.strip() else ""
        if bssid:
            info["wifi"]["bssid"] = bssid
    except Exception:
        pass
    try:
        r = subprocess.run(["iw", "dev", "wlan0", "link"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("signal:"):
                info["wifi"]["signal_dbm"] = line.split(":")[1].strip().split()[0]
            elif line.startswith("tx bitrate:"):
                info["wifi"]["tx_bitrate"] = line.split(":", 1)[1].strip()
    except Exception:
        pass
    return info


def get_clock_info() -> dict:
    info: dict = {"now": datetime.now(timezone.utc).isoformat(), "uptime_seconds": get_uptime_seconds(), "timezone": "?", "ntp_sync": False, "ntp_service": "?"}
    try:
        r = subprocess.run(
            ["timedatectl", "show", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.splitlines():
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip()
            if k == "Timezone":
                info["timezone"] = v
            elif k == "NTPSynchronized":
                info["ntp_sync"] = v == "yes"
            elif k == "NTP":
                info["ntp_enabled"] = v == "yes"
            elif k == "LocalRTC":
                info["local_rtc"] = v == "yes"
    except Exception:
        pass
    try:
        r = subprocess.run(["timedatectl", "show-timesync", "--no-pager", "-p", "ServerName,Poll,Leap"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip()
            if k == "ServerName":
                info["ntp_server"] = v
            elif k == "Poll":
                info["ntp_poll_s"] = v
    except Exception:
        pass
    return info


# ── Haversine ────────────────────────────────────────────────────────────────

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Return (distance_km, bearing_deg) from point 1 to point 2."""
    import math
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    dist = R * 2 * math.asin(math.sqrt(a))
    y = math.sin(dλ) * math.cos(φ2)
    x = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(dλ)
    brg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return round(dist, 2), round(brg, 1)


# ── Direwolf version ──────────────────────────────────────────────────────────

def get_direwolf_version() -> str:
    try:
        r = subprocess.run(
            ["direwolf", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        output = (r.stdout + r.stderr)
        for line in output.splitlines():
            if "Dire Wolf version" in line or "direwolf version" in line.lower():
                return line.strip()
        return output.splitlines()[0].strip() if output.strip() else "inconnu"
    except Exception:
        return "inconnu"


# ── aprs.fi status ────────────────────────────────────────────────────────────

def get_aprsfi_status(callsign: str, apikey: str = "") -> dict:
    """Query aprs.fi API for a callsign. Returns status dict."""
    import urllib.request, urllib.parse
    if not callsign:
        return {"error": "Indicatif non configuré"}
    if not apikey:
        return {"error": "APRSFI_KEY non configurée dans config.env"}
    try:
        params = urllib.parse.urlencode({"name": callsign, "what": "loc", "apikey": apikey, "format": "json"})
        url = f"https://api.aprs.fi/api/get?{params}"
        ctx = __import__("ssl").create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = __import__("ssl").CERT_NONE
        with urllib.request.urlopen(url, timeout=8, context=ctx) as resp:
            data = json.loads(resp.read().decode())
        if data.get("result") != "ok":
            return {"error": data.get("description", "Erreur API")}
        entries = data.get("entries", [])
        if not entries:
            return {"error": f"{callsign} non trouvé sur aprs.fi"}
        e = entries[0]
        now = time.time()
        age_s = int(now - float(e.get("lasttime", now)))
        path = e.get("path", "")
        via = "RF" if "qAR" in path or "WIDE" in path else ("APRS-IS" if "qAC" in path else path)
        return {
            "callsign": e.get("name"),
            "last_time": e.get("lasttime"),
            "age_s": age_s,
            "age_str": _fmt_age(age_s),
            "lat": e.get("lat"),
            "lng": e.get("lng"),
            "comment": e.get("comment", ""),
            "path": path,
            "via": via,
            "symbol": e.get("symbol", ""),
            "srccall": e.get("srccall", ""),
            "running": age_s < 7200,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _fmt_age(s: int) -> str:
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}min {s%60}s"
    if s < 86400:
        return f"{s//3600}h {(s%3600)//60}min"
    return f"{s//86400}j {(s%86400)//3600}h"


# ── CSV log reader ────────────────────────────────────────────────────────────

LOG_DIR = Path("/opt/aprs-lite/logs")
_LOG_HEADERS = ["chan","utime","isotime","source","heard","level","error","dti",
                "name","symbol","latitude","longitude","speed","course","altitude",
                "frequency","offset","tone","system","status","telemetry","comment"]


def read_log_dates() -> list[str]:
    """Return sorted list of available log dates (YYYY-MM-DD)."""
    try:
        dates = []
        for f in LOG_DIR.glob("????-??-??.log"):
            dates.append(f.stem)
        return sorted(dates, reverse=True)
    except Exception:
        return []


def read_log_frames(date_str: str, own_lat: float | None = None, own_lon: float | None = None) -> list[dict]:
    """Parse a YYYY-MM-DD Direwolf CSV log into a list of frame dicts."""
    import csv as _csv, re as _re
    date_re = _re.compile(r'^\d{4}-\d{2}-\d{2}$')
    if not date_re.match(date_str):
        return []
    log_file = LOG_DIR / f"{date_str}.log"
    if not log_file.exists():
        return []
    rows = []
    try:
        with log_file.open(encoding="utf-8", errors="replace") as fh:
            reader = _csv.DictReader(fh)
            for row in reader:
                try:
                    lat = float(row["latitude"]) if row.get("latitude") else None
                    lon = float(row["longitude"]) if row.get("longitude") else None
                    dist = brg = None
                    if lat is not None and lon is not None and own_lat is not None and own_lon is not None:
                        dist, brg = haversine(own_lat, own_lon, lat, lon)
                    # heard == source → direct; otherwise via digi
                    origin = "rf" if row.get("source") == row.get("heard") else "rf_digi"
                    rows.append({
                        "timestamp": row.get("isotime", ""),
                        "source": row.get("source") or row.get("name", ""),
                        "heard_via": row.get("heard", ""),
                        "level": row.get("level", ""),
                        "symbol": row.get("symbol", ""),
                        "lat": lat, "lon": lon,
                        "speed": row.get("speed") or None,
                        "course": row.get("course") or None,
                        "altitude": row.get("altitude") or None,
                        "comment": row.get("comment", ""),
                        "system": row.get("system", ""),
                        "origin": origin,
                        "distance_km": dist,
                        "bearing_deg": brg,
                    })
                except Exception:
                    continue
    except Exception:
        pass
    return rows


def get_pi_info() -> dict:
    import platform
    info: dict = {}
    try:
        info["model"] = Path("/proc/device-tree/model").read_text().strip().rstrip("\x00")
    except Exception:
        info["model"] = "Raspberry Pi (inconnu)"
    try:
        r = subprocess.run(["lsb_release", "-ds"], capture_output=True, text=True, timeout=3)
        info["os"] = r.stdout.strip()
    except Exception:
        info["os"] = platform.platform()
    info["kernel"] = platform.release()
    info["arch"] = platform.machine()
    info["hostname"] = socket.gethostname()
    info["direwolf_version"] = get_direwolf_version()
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
        mem = {l.split(":")[0]: int(l.split()[1]) for l in lines if ":" in l and len(l.split()) >= 2}
        info["mem_total_mb"] = round(mem.get("MemTotal", 0) / 1024)
        info["mem_free_mb"] = round(mem.get("MemAvailable", 0) / 1024)
        info["mem_used_mb"] = info["mem_total_mb"] - info["mem_free_mb"]
    except Exception:
        info["mem_total_mb"] = info["mem_free_mb"] = info["mem_used_mb"] = None
    info["cpu_temp"] = get_cpu_temp()
    info["uptime_seconds"] = get_uptime_seconds()
    try:
        st = os.statvfs("/")
        info["disk_total_gb"] = round(st.f_blocks * st.f_frsize / 1e9, 1)
        info["disk_free_gb"] = round(st.f_bavail * st.f_frsize / 1e9, 1)
        info["disk_used_gb"] = round(info["disk_total_gb"] - info["disk_free_gb"], 1)
    except Exception:
        info["disk_total_gb"] = info["disk_free_gb"] = info["disk_used_gb"] = None
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3)
        val = r.stdout.strip().replace("throttled=", "")
        throttled = int(val, 16)
        info["throttled"] = bool(throttled)
        info["throttled_hex"] = val
        flags = []
        if throttled & 0x1: flags.append("under-voltage")
        if throttled & 0x2: flags.append("arm-freq-capped")
        if throttled & 0x4: flags.append("currently-throttled")
        if throttled & 0x10000: flags.append("under-voltage-occurred")
        if throttled & 0x20000: flags.append("arm-freq-capped-occurred")
        if throttled & 0x40000: flags.append("throttling-occurred")
        info["throttle_flags"] = flags
    except Exception:
        info["throttled"] = None
    try:
        r = subprocess.run(["vcgencmd", "measure_clock", "arm"], capture_output=True, text=True, timeout=3)
        val = r.stdout.strip().split("=")[-1]
        info["arm_freq_mhz"] = round(int(val) / 1e6)
    except Exception:
        info["arm_freq_mhz"] = None
    try:
        r = subprocess.run(["vcgencmd", "get_mem", "gpu"], capture_output=True, text=True, timeout=3)
        info["gpu_mem_mb"] = r.stdout.strip().replace("gpu=", "").replace("M", "")
    except Exception:
        info["gpu_mem_mb"] = None
    return info


def get_audio_info() -> dict:
    info: dict = {"playback": [], "capture": [], "aioc": None}
    try:
        r = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if line.startswith("card "):
                info["playback"].append(line.strip())
                if "All-In-One" in line or "aioc" in line.lower() or "AIOC" in line:
                    info["aioc"] = {"playback_line": line.strip()}
    except Exception:
        pass
    try:
        r = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if line.startswith("card "):
                info["capture"].append(line.strip())
                if "All-In-One" in line or "aioc" in line.lower() or "AIOC" in line:
                    if info["aioc"] is None:
                        info["aioc"] = {}
                    info["aioc"]["capture_line"] = line.strip()
                    m = __import__("re").search(r"card (\d+):", line)
                    if m:
                        info["aioc"]["hw"] = f"hw:{m.group(1)},0"
    except Exception:
        pass
    try:
        r = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if "All-In-One" in line or "aioc" in line.lower() or "C-Media" in line:
                info["usb_device"] = line.strip()
                break
    except Exception:
        pass
    return info


# ── Config write ─────────────────────────────────────────────────────────────

CONFIG_PATH = Path("/opt/aprs-lite/config.env")
DIREWOLF_CONF = Path("/opt/aprs-lite/direwolf.conf")

EDITABLE_KEYS = {
    "CALLSIGN", "PASSCODE", "LAT", "LON", "COMMENT",
    "TXDELAY", "ADEVICE", "PTT", "ARATE",
    "IGSERVER", "IGFILTER_KM",
    "LOG_DIR", "LOG_RETENTION_DAYS",
    "REBOOT_DAY", "REBOOT_HOUR",
    "SENSOR_ENABLED", "SENSOR_INTERVAL", "WX_ENABLED",
}


def save_config_key(key: str, value: str) -> bool:
    """Write a single key=value to config.env (in-place replace)."""
    if key not in EDITABLE_KEYS:
        return False
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        pattern = re.compile(rf'^({re.escape(key)}\s*=\s*).*', re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(rf'\g<1>"{value}"', text)
        else:
            text += f'\n{key}="{value}"\n'
        CONFIG_PATH.write_text(text, encoding="utf-8")
        return True
    except Exception:
        return False


def save_config_bulk(data: dict) -> tuple[bool, str]:
    """Write multiple keys at once; returns (ok, error_msg)."""
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        for key, value in data.items():
            if key not in EDITABLE_KEYS:
                continue
            pattern = re.compile(rf'^({re.escape(key)}\s*=\s*).*', re.MULTILINE)
            if pattern.search(text):
                text = pattern.sub(rf'\g<1>"{value}"', text)
            else:
                text += f'\n{key}="{value}"\n'
        CONFIG_PATH.write_text(text, encoding="utf-8")
        return True, ""
    except Exception as e:
        return False, str(e)


# ── Direwolf / service actions ────────────────────────────────────────────────

def restart_direwolf() -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["sudo", "/bin/systemctl", "restart", "aprs-direwolf"],
            capture_output=True, text=True, timeout=20
        )
        return r.returncode == 0, r.stderr.strip()
    except Exception as e:
        return False, str(e)


def detect_aioc_full() -> dict:
    try:
        import sys
        sys.path.insert(0, "/opt/aprs-lite")
        from aioc_detect import full_detect
        return full_detect()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Beacon / packet sending ───────────────────────────────────────────────────

def _kiss_port_open() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 8001), timeout=2)
        s.close()
        return True
    except Exception:
        return False


def _make_beacon_packet(config: dict) -> str:
    callsign = config.get("CALLSIGN", "N0CALL")
    comment = config.get("COMMENT", "APRS Relay")
    try:
        lat = float(config.get("LAT", "0"))
        lon = float(config.get("LON", "0"))
        ld, lm = int(abs(lat)), (abs(lat) % 1) * 60
        od, om = int(abs(lon)), (abs(lon) % 1) * 60
        ls = f"{ld:02d}{lm:05.2f}{'N' if lat >= 0 else 'S'}"
        os_ = f"{od:03d}{om:05.2f}{'E' if lon >= 0 else 'W'}"
        return f"{callsign}>APNW01,WIDE1-1:!{ls}/{os_}r {comment}"
    except Exception:
        return f"{callsign}>APNW01,WIDE1-1:!0000.00N/00000.00W# {comment}"


def make_weather_packet(callsign: str, lat: float, lon: float, data: dict) -> str:
    ld, lm = int(abs(lat)), (abs(lat) % 1) * 60
    od, om_deg = int(abs(lon)), (abs(lon) % 1) * 60
    ls = f"{ld:02d}{lm:05.2f}{'N' if lat >= 0 else 'S'}"
    os_ = f"{od:03d}{om_deg:05.2f}{'E' if lon >= 0 else 'W'}"
    tf = round(data["temperature"] * 9 / 5 + 32)
    hh = int(data["humidity"]) % 100
    bp = min(99999, round(data["pressure"] * 10))
    # Vent : direction (ddd) et vitesse en nœuds (sss), rafale (ggg)
    wdir = data.get("wind_dir")
    wspd = data.get("wind_speed")  # km/h → knots
    if wdir is not None and wspd is not None:
        wdir_s = f"{int(wdir):03d}"
        wspd_kn = max(0, round(wspd / 1.852))
        wind_s = f"{wdir_s}/{wspd_kn:03d}g{wspd_kn:03d}"
    else:
        wind_s = ".../...g..."
    # Pluie sur 1h en centièmes de pouce
    rain = data.get("rain_1h", 0) or 0
    rain_hundredths = max(0, round(rain / 25.4 * 100))
    wx = f"{wind_s}t{tf:03d}r{rain_hundredths:03d}h{hh:02d}b{bp:05d}"
    extras = []
    if data.get("iaq", -1) >= 0:
        extras.append(f"IAQ={data['iaq']:.0f}/{data.get('iaq_accuracy', 0)}")
    if data.get("co2_eq", -1) > 0:
        extras.append(f"CO2={data['co2_eq']:.0f}ppm")
    if data.get("voc_eq", -1) > 0:
        extras.append(f"VOC={data['voc_eq']:.2f}ppm")
    src = data.get("weather_source", "")
    if src == "open-meteo":
        extras.append("src=OM")
    elif src == "hybrid":
        extras.append("src=HYB")
    if extras:
        wx += " " + " ".join(extras)
    elif data.get("gas"):
        wx += f" Gas:{data['gas']}ohm"
    return f"{callsign}>APNW01,WIDE1-1:!{ls}/{os_}_{wx}"


def send_kiss_packet(packet: str) -> tuple[bool, str]:
    """Send an APRS packet via kissutil to Direwolf KISS port 8001."""
    if not _kiss_port_open():
        return False, "Port KISS 8001 inaccessible"
    try:
        r = subprocess.run(
            ["kissutil", "-h", "127.0.0.1", "-p", "8001"],
            input=packet + "\n",
            capture_output=True, text=True, timeout=5
        )
        return (True, "") if r.returncode == 0 else (False, r.stderr.strip() or "kissutil erreur")
    except FileNotFoundError:
        return False, "kissutil non installé"
    except Exception as e:
        return False, str(e)


# ── WiFi ─────────────────────────────────────────────────────────────────────

def wifi_scan() -> list[str]:
    try:
        r = subprocess.run(
            ["sudo", "/usr/sbin/iwlist", "wlan0", "scan"],
            capture_output=True, text=True, timeout=15
        )
        return sorted(set(m.group(1) for m in re.finditer(r'ESSID:"(.+?)"', r.stdout)))
    except Exception:
        return []


def wifi_current() -> str:
    try:
        r = subprocess.run(["iwgetid", "wlan0", "--raw"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""


def wifi_connect(ssid: str, password: str) -> tuple[bool, str]:
    conf = (
        'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n'
        'update_config=1\ncountry=FR\n\n'
        f'network={{\n    ssid="{ssid}"\n    psk="{password}"\n}}\n'
    )
    try:
        subprocess.run(
            ["sudo", "/usr/bin/tee", "/etc/wpa_supplicant/wpa_supplicant.conf"],
            input=conf, text=True, capture_output=True, timeout=5
        )
        subprocess.run(["sudo", "/sbin/wpa_cli", "-i", "wlan0", "reconfigure"],
                       capture_output=True, timeout=10)
        for _ in range(15):
            time.sleep(1)
            if wifi_current() == ssid:
                return True, f"Connecté à {ssid}"
        return False, "Échec — SSID/MDP incorrect ?"
    except Exception as e:
        return False, str(e)


def wifi_disconnect() -> tuple[bool, str]:
    try:
        subprocess.run(["sudo", "/sbin/ip", "link", "set", "wlan0", "down"], timeout=5)
        time.sleep(1)
        subprocess.run(["sudo", "/sbin/ip", "link", "set", "wlan0", "up"], timeout=5)
        return True, "Déconnecté"
    except Exception as e:
        return False, str(e)


# ── Clock / NTP ───────────────────────────────────────────────────────────────

def ntp_sync() -> tuple[bool, str]:
    try:
        subprocess.run(
            ["sudo", "/bin/systemctl", "restart", "systemd-timesyncd"],
            timeout=10, capture_output=True
        )
        return True, "NTP relancé"
    except Exception as e:
        return False, str(e)


def clock_set(value: str) -> tuple[bool, str]:
    if not re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$', value):
        return False, "Format invalide (attendu : YYYY-MM-DD HH:MM:SS)"
    try:
        subprocess.run(["sudo", "/bin/date", "-s", value], capture_output=True, timeout=5)
        return True, f"Heure réglée : {value}"
    except Exception as e:
        return False, str(e)


# ── BME280/BME68x sensor ──────────────────────────────────────────────────────

def _read_sensor_at(addr: int) -> dict:
    """Lit un BME280 à l'adresse I2C donnée. Retourne dict ou {ok:False}."""
    try:
        import sys
        sys.path.insert(0, "/opt/aprs-lite")
        from bme_sensor import BME280, BME68X_ID, BME280_ID, CHIP_ID_REG
        import smbus2
        bus = smbus2.SMBus(1)
        try:
            cid = bus.read_byte_data(addr, CHIP_ID_REG)
        except OSError:
            bus.close()
            return {"ok": False, "error": f"Aucun capteur à 0x{addr:02x}"}
        bus.close()
        if cid == BME280_ID:
            sensor = BME280(addr)
            data = sensor.read()
            sensor.close()
            try:
                _alt = _get_altitude_m()
                if data and data.get("pressure") and _alt:
                    data["pressure_raw"] = data["pressure"]
                    data["pressure"] = _sea_level_pressure(data["pressure"], _alt, data.get("temperature"))
            except Exception: pass
            data["ok"] = True
            data["chip"] = "BME280"
            return data
        return {"ok": False, "error": f"chip_id inconnu 0x{cid:02x} à 0x{addr:02x}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_sensor_data() -> dict:
    """Capteur météo principal (0x76, SDO→GND)."""
    return _read_sensor_at(0x76)


def get_box_sensor_data() -> dict:
    """Capteur boîtier (0x77, SDO→VCC)."""
    return _read_sensor_at(0x77)


# ── direwolf.conf ────────────────────────────────────────────────────────────

DIREWOLF_CONF_KEYS = {
    "MYCALL", "MODEM", "PTT", "TXDELAY", "SLOTTIME", "PERSIST",
    "DWAIT", "TXTAIL", "DIGIPEAT", "DEDUPE", "IGSERVER", "IGLOGIN",
    "IGFILTER", "PBEACON",
}


def parse_direwolf_conf() -> dict:
    result: dict = {}
    try:
        text = DIREWOLF_CONF.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) >= 1:
                key = parts[0].upper()
                value = parts[1] if len(parts) > 1 else ""
                if key in DIREWOLF_CONF_KEYS:
                    result[key] = value
    except Exception:
        pass
    return result


def write_direwolf_conf_raw(content: str) -> tuple[bool, str]:
    try:
        DIREWOLF_CONF.write_text(content, encoding="utf-8")
        return True, ""
    except Exception as e:
        return False, str(e)


# ── Audio level streaming ──────────────────────────────────────────────────

def stream_audio_levels():
    import struct as _struct
    import json as _json
    try:
        proc = subprocess.Popen(
            ["arecord", "-D", "plughw:AllInOneCable,0", "-f", "S16_LE", "-r", "48000", "-c", "1", "-q"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        yield 'data: {"error": "arecord non disponible"}\n\n'
        return
    buf_size = 4096
    buf = b""
    last_emit = time.time()
    try:
        while True:
            chunk = proc.stdout.read(buf_size)
            if not chunk:
                break
            buf += chunk
            now = time.time()
            if now - last_emit >= 0.2:
                samples = _struct.unpack(f"<{len(buf)//2}h", buf[:len(buf)//2*2])
                if samples:
                    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
                    db = 20 * __import__("math").log10(rms / 32768.0 + 1e-9)
                    yield f'data: {_json.dumps({"rms": round(rms, 2), "db": round(db, 2)})}\n\n'
                buf = b""
                last_emit = now
    except GeneratorExit:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


# ── System events streaming (watchdog log) ────────────────────────────────

WATCHDOG_LOG = Path("/opt/aprs-lite/logs/watchdog.log")
_LOG_RE = re.compile(r'^(\S+\s+\S+)\s+(\w+)\s+(.*)')


def stream_system_events():
    import json as _json
    if not WATCHDOG_LOG.exists():
        yield f'data: {_json.dumps({"error": "watchdog.log introuvable"})}\n\n'
        time.sleep(5)
        return
    try:
        proc = subprocess.Popen(
            ["tail", "-f", str(WATCHDOG_LOG)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            m = _LOG_RE.match(line)
            if m:
                event = {"ts": m.group(1), "level": m.group(2), "message": m.group(3)}
            else:
                event = {"ts": "", "level": "INFO", "message": line}
            try:
                yield f'data: {_json.dumps(event)}\n\n'
            except GeneratorExit:
                proc.terminate()
                return
        proc.terminate()
    except Exception as exc:
        yield f'data: {_json.dumps({"error": str(exc)})}\n\n'


# ── Open-Meteo fallback (La Pierre Saint-Martin 42.97°N -0.78°E, ~1650m) ──
_om_cache: dict = {}  # {cache_key: {"ts": float, "data": dict}}
_OM_TTL = 600  # 10 minutes


def _fetch_openmeteo(lat=None, lon=None, elev=None) -> dict:
    """Retourne temp/humidity/pressure/wind/rain depuis Open-Meteo, cache 10 min."""
    import urllib.request as _ur
    _lat  = lat  if lat  is not None else 42.97
    _lon  = lon  if lon  is not None else -0.78
    _elev = elev if elev is not None else 1650
    cache_key = f"{_lat:.3f},{_lon:.3f}"
    # Use per-location cache slot
    if cache_key not in _om_cache:
        _om_cache[cache_key] = {"ts": 0, "data": {}}
    slot = _om_cache[cache_key]
    now = time.time()
    if now - slot["ts"] < _OM_TTL and slot["data"]:
        return slot["data"]
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={_lat}&longitude={_lon}&elevation={int(_elev)}"
            "&current=temperature_2m,relative_humidity_2m,surface_pressure,"
            "wind_speed_10m,wind_direction_10m,precipitation,snowfall,weather_code"
            "&wind_speed_unit=kmh&timezone=Europe%2FParis"
        )
        with _ur.urlopen(url, timeout=8) as resp:
            raw = json.loads(resp.read())
        cur = raw.get("current", {})
        data = {
            "temperature": round(cur.get("temperature_2m", 0), 1),
            "humidity":    round(cur.get("relative_humidity_2m", 0)),
            "pressure":    round(cur.get("surface_pressure", 0), 1),
            "wind_speed":  round(cur.get("wind_speed_10m", 0), 1),
            "wind_dir":    round(cur.get("wind_direction_10m", 0)),
            "rain_1h":     round(cur.get("precipitation", 0), 1),
            "snow_1h":     round(cur.get("snowfall", 0), 1),
            "weather_code": cur.get("weather_code", 0),
        }
        slot["ts"] = now
        slot["data"] = data
        return data
    except Exception:
        return {}


def system_snapshot(service_name: str) -> dict:
    snap: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cpu_temp": get_cpu_temp(),
        "cpu_usage": get_cpu_usage(),
        "ram_usage": get_ram_usage(),
        "disk_usage": get_disk_usage("/"),
        "direwolf_status": service_status(service_name),
        "load_avg_1m": round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None,
        "uptime_seconds": get_uptime_seconds(),
        "hostname": socket.gethostname(),
        "primary_ip": get_primary_ip(),
    }
    bme = get_sensor_data()
    om = _fetch_openmeteo()
    if bme.get("ok"):
        # Capteur physique présent : valeurs locales + vent/pluie Open-Meteo
        snap["bme_temp"]       = bme.get("temperature")
        snap["bme_humidity"]   = bme.get("humidity")
        snap["bme_pressure"]   = bme.get("pressure")
        snap["wind_speed"]     = om.get("wind_speed")
        snap["wind_dir"]       = om.get("wind_dir")
        snap["rain_1h"]        = om.get("rain_1h")
        snap["snow_1h"]        = om.get("snow_1h")
        snap["weather_source"] = "hybrid"
    elif om:
        # Capteur absent : tout depuis Open-Meteo
        snap["bme_temp"]       = om.get("temperature")
        snap["bme_humidity"]   = om.get("humidity")
        snap["bme_pressure"]   = om.get("pressure")
        snap["wind_speed"]     = om.get("wind_speed")
        snap["wind_dir"]       = om.get("wind_dir")
        snap["rain_1h"]        = om.get("rain_1h")
        snap["snow_1h"]        = om.get("snow_1h")
        snap["weather_source"] = "open-meteo"
    # Second capteur désactivé (0x77 absent)
    # box = get_box_sensor_data()
    # if box.get("ok"):
    #     snap["box_temp"]     = box.get("temperature")
    #     snap["box_humidity"] = box.get("humidity")
    #     snap["box_pressure"] = box.get("pressure")
    return snap


@dataclass
class RuntimeState:
    service_name: str
    config_path: str
    counts: dict = field(
        default_factory=lambda: {"rf_rx": 0, "rf_tx": 0, "is_rx": 0, "beacons": 0}
    )
    last_frame: str = ""
    last_frame_time: str | None = None
    last_error: str | None = None
    latest_snapshot: dict = field(default_factory=dict)
    config_cache: dict = field(default_factory=dict)
    config_raw: str = ""

    def refresh_config(self):
        path = Path(self.config_path)
        self.config_cache = load_env_file(path)
        try:
            self.config_raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            self.config_raw = ""


class JournalCollector:
    def __init__(
        self,
        db: SidecarDB,
        state: RuntimeState,
        socketio=None,
        journal_unit: str = "aprs-direwolf",
        on_msg_event=None,
    ):
        self.db = db
        self.state = state
        self.socketio = socketio
        self.journal_unit = journal_unit
        self._on_msg_event = on_msg_event  # callable(type, msg_id, parsed)
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def start(self):
        self.state.refresh_config()
        self._threads = [
            threading.Thread(target=self._bootstrap_recent_frames, daemon=True),
            threading.Thread(target=self._follow_journal, daemon=True),
            threading.Thread(target=self._sample_system_loop, daemon=True),
            threading.Thread(target=self._config_refresh_loop, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self):
        self._stop.set()

    def _emit(self, event: str, payload: dict):
        if self.socketio is not None:
            self.socketio.emit(event, payload)

    def _handle_frame(self, raw_frame: str, origin: str):
        parsed = parse_aprs_frame(raw_frame)
        parsed["origin"] = origin
        # Haversine distance/bearing from own station
        try:
            own_lat = float(self.state.config_cache.get("LAT", ""))
            own_lon = float(self.state.config_cache.get("LON", ""))
            frame_lat = parsed.get("lat")
            frame_lon = parsed.get("lon")
            if frame_lat is not None and frame_lon is not None:
                dist, brg = haversine(own_lat, own_lon, frame_lat, frame_lon)
                parsed["distance_km"] = dist
                parsed["bearing_deg"] = brg
        except Exception:
            pass
        row_id = self.db.insert_frame(parsed)
        if row_id == -1:
            return
        parsed["id"] = row_id
        self.state.last_frame = raw_frame[:120]
        self.state.last_frame_time = parsed.get("timestamp")
        if origin in ("rf", "rf_digi"):
            self.state.counts["rf_rx"] += 1
        elif origin == "tx":
            self.state.counts["rf_tx"] += 1
        elif origin in ("is", "igtx"):
            self.state.counts["is_rx"] += 1
        if "beacon" in raw_frame.lower() or parsed.get("source") == self.state.config_cache.get("CALLSIGN", ""):
            self.state.counts["beacons"] += 1
        self._emit("new_frame", parsed)

        # Callback chat : ACK / REJ / message entrant
        if self._on_msg_event:
            dtype = parsed.get("data_type")
            try:
                if dtype == "message_ack":
                    self._on_msg_event("ack", parsed.get("msg_ack") or "", parsed)
                elif dtype == "message_rej":
                    self._on_msg_event("rej", parsed.get("msg_rej") or "", parsed)
                elif dtype == "message":
                    self._on_msg_event("msg", parsed.get("msg_no") or "", parsed)
            except Exception:
                pass

    def _parse_line(self, line: str):
        clean = ANSI_RE.sub("", line.strip())
        if not clean:
            return
        rf_match = RF_RE.match(clean)
        if rf_match:
            # group(2)="L" → tx, group(3)="H" → heard via digi, else rf
            if rf_match.group(2):
                origin = "tx"
            elif rf_match.group(3):
                origin = "rf_digi"
            else:
                origin = "rf"
            self._handle_frame(rf_match.group(4).strip(), origin)
            return
        ig_match = IG_RE.match(clean)
        if ig_match and ">" in ig_match.group(1):
            self._handle_frame(ig_match.group(1).strip(), "is")
            return
        igtx_match = IGTX_RE.match(clean)
        if igtx_match and ">" in igtx_match.group(1):
            self._handle_frame(igtx_match.group(1).strip(), "igtx")

    def _bootstrap_recent_frames(self):
        try:
            if self.db.get_stats()["frames_total"] > 0:
                return
            result = subprocess.run(
                [
                    "journalctl",
                    "-u",
                    self.journal_unit,
                    "--output=cat",
                    "-n",
                    "200",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
            )
            for line in result.stdout.splitlines():
                self._parse_line(line)
        except Exception as exc:
            self.state.last_error = f"bootstrap_recent_frames: {exc}"

    def _follow_journal(self):
        while not self._stop.is_set():
            try:
                proc = subprocess.Popen(
                    [
                        "journalctl",
                        "-u",
                        self.journal_unit,
                        "-f",
                        "--output=cat",
                        "-n",
                        "0",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    if self._stop.is_set():
                        break
                    self._parse_line(line)
                proc.terminate()
            except Exception as exc:
                self.state.last_error = f"follow_journal: {exc}"
            time.sleep(3)

    def _sample_system_loop(self):
        while not self._stop.is_set():
            snapshot = system_snapshot(self.state.service_name)
            self.state.latest_snapshot = snapshot
            self.db.insert_telemetry(snapshot)
            self._emit("telemetry", snapshot)
            time.sleep(60)

    def _config_refresh_loop(self):
        while not self._stop.is_set():
            try:
                self.state.refresh_config()
            except Exception as exc:
                self.state.last_error = f"refresh_config: {exc}"
            time.sleep(30)

    def system_summary(self) -> dict:
        snapshot = dict(self.state.latest_snapshot or system_snapshot(self.state.service_name))
        snapshot["service_name"] = self.state.service_name
        snapshot["counts"] = dict(self.state.counts)
        snapshot["last_frame"] = self.state.last_frame
        snapshot["last_frame_time"] = self.state.last_frame_time
        snapshot["last_error"] = self.state.last_error
        snapshot["config_path"] = self.state.config_path
        return snapshot
