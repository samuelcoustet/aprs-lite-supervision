#!/usr/bin/env python3
"""Read-only collectors for aprs-lite sidecar dashboard."""

from __future__ import annotations

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
RF_RE = re.compile(r"^\[(\d+)(L)?(H)?\]\s+(.+)$")
IG_RE = re.compile(r"^\[ig\]\s+(.+)$")
IGTX_RE = re.compile(r"^\[ig>tx\]\s+(.+)$")


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


def system_snapshot(service_name: str) -> dict:
    return {
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
    ):
        self.db = db
        self.state = state
        self.socketio = socketio
        self.journal_unit = journal_unit
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
