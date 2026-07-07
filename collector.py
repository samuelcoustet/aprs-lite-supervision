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
RF_RE = re.compile(r"^\[0(L)?\]\s+(.+)$")
IG_RE = re.compile(r"^\[ig\]\s+(.+)$")


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
        if origin == "rf":
            self.state.counts["rf_rx"] += 1
        elif origin == "tx":
            self.state.counts["rf_tx"] += 1
        elif origin == "is":
            self.state.counts["is_rx"] += 1
        if "beacon" in raw_frame.lower() or parsed.get("source") == self.state.config_cache.get("CALLSIGN", ""):
            self.state.counts["beacons"] += 1
        self._emit("new_frame", parsed)

    def _parse_line(self, line: str):
        clean = ANSI_RE.sub("", line.strip())
        if not clean or "[ig>tx]" in clean:
            return
        rf_match = RF_RE.match(clean)
        if rf_match:
            origin = "tx" if rf_match.group(1) else "rf"
            self._handle_frame(rf_match.group(2).strip(), origin)
            return
        ig_match = IG_RE.match(clean)
        if ig_match and ">" in ig_match.group(1):
            self._handle_frame(ig_match.group(1).strip(), "is")

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
