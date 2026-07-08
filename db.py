#!/usr/bin/env python3
"""SQLite storage for the aprs-lite sidecar dashboard."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
DEDUP_WINDOW_SECONDS = 600

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    destination TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    data_type TEXT NOT NULL DEFAULT 'unknown',
    lat REAL,
    lon REAL,
    symbol TEXT DEFAULT '',
    speed REAL,
    course REAL,
    altitude REAL,
    comment TEXT DEFAULT '',
    raw TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'rf',
    raw_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_frames_timestamp ON frames(timestamp);
CREATE INDEX IF NOT EXISTS idx_frames_source ON frames(source);
CREATE INDEX IF NOT EXISTS idx_frames_origin ON frames(origin);
CREATE INDEX IF NOT EXISTS idx_frames_type ON frames(data_type);
CREATE INDEX IF NOT EXISTS idx_frames_hash ON frames(raw_hash);
CREATE TABLE IF NOT EXISTS stations (
    callsign TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_lat REAL,
    last_lon REAL,
    last_symbol TEXT DEFAULT '',
    last_speed REAL,
    last_course REAL,
    last_altitude REAL,
    last_comment TEXT DEFAULT '',
    frame_count INTEGER NOT NULL DEFAULT 0,
    last_data_type TEXT NOT NULL DEFAULT 'unknown',
    last_origin TEXT NOT NULL DEFAULT 'rf'
);
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    cpu_temp REAL,
    cpu_usage REAL,
    ram_usage REAL,
    disk_usage REAL,
    direwolf_status TEXT DEFAULT '',
    load_avg_1m REAL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp ON telemetry(timestamp);
"""


class SidecarDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            row = conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )

    def insert_frame(self, frame: dict) -> int:
        now = frame.get("timestamp") or datetime.now(timezone.utc).isoformat()
        origin = frame.get("origin", "rf")
        raw = frame.get("raw", "")
        raw_hash = hashlib.sha1(f"{origin}|{raw}".encode("utf-8")).hexdigest()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=DEDUP_WINDOW_SECONDS)
        ).isoformat()

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM frames WHERE raw_hash=? AND timestamp>=? "
                "ORDER BY id DESC LIMIT 1",
                (raw_hash, cutoff),
            ).fetchone()
            if existing:
                return -1

            cur = conn.execute(
                """
                INSERT INTO frames (
                    timestamp, source, destination, path, data_type,
                    lat, lon, symbol, speed, course, altitude,
                    comment, raw, origin, raw_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    frame.get("source", ""),
                    frame.get("destination", ""),
                    frame.get("path", ""),
                    frame.get("data_type", "unknown"),
                    frame.get("lat"),
                    frame.get("lon"),
                    frame.get("symbol", ""),
                    frame.get("speed"),
                    frame.get("course"),
                    frame.get("altitude"),
                    frame.get("comment", ""),
                    raw,
                    origin,
                    raw_hash,
                ),
            )
            if frame.get("source"):
                self._upsert_station(conn, frame, now)
            return cur.lastrowid

    def _upsert_station(self, conn, frame: dict, now: str):
        conn.execute(
            """
            INSERT INTO stations (
                callsign, first_seen, last_seen, last_lat, last_lon,
                last_symbol, last_speed, last_course, last_altitude,
                last_comment, frame_count, last_data_type, last_origin
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(callsign) DO UPDATE SET
                last_seen=excluded.last_seen,
                last_lat=COALESCE(excluded.last_lat, last_lat),
                last_lon=COALESCE(excluded.last_lon, last_lon),
                last_symbol=CASE
                    WHEN excluded.last_symbol != '' THEN excluded.last_symbol
                    ELSE last_symbol
                END,
                last_speed=COALESCE(excluded.last_speed, last_speed),
                last_course=COALESCE(excluded.last_course, last_course),
                last_altitude=COALESCE(excluded.last_altitude, last_altitude),
                last_comment=CASE
                    WHEN excluded.last_comment != '' THEN excluded.last_comment
                    ELSE last_comment
                END,
                frame_count=frame_count + 1,
                last_data_type=excluded.last_data_type,
                last_origin=excluded.last_origin
            """,
            (
                frame.get("source", ""),
                now,
                now,
                frame.get("lat"),
                frame.get("lon"),
                frame.get("symbol", ""),
                frame.get("speed"),
                frame.get("course"),
                frame.get("altitude"),
                frame.get("comment", ""),
                frame.get("data_type", "unknown"),
                frame.get("origin", "rf"),
            ),
        )

    def insert_telemetry(self, telemetry: dict):
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telemetry (
                    timestamp, cpu_temp, cpu_usage, ram_usage,
                    disk_usage, direwolf_status, load_avg_1m
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telemetry.get("timestamp")
                    or datetime.now(timezone.utc).isoformat(),
                    telemetry.get("cpu_temp"),
                    telemetry.get("cpu_usage"),
                    telemetry.get("ram_usage"),
                    telemetry.get("disk_usage"),
                    telemetry.get("direwolf_status", ""),
                    telemetry.get("load_avg_1m"),
                ),
            )

    def get_frames(
        self,
        n: int = 50,
        offset: int = 0,
        source: str | None = None,
        data_type: str | None = None,
        since: str | None = None,
        until: str | None = None,
        origin: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM frames WHERE 1=1"
        params: list[object] = []
        if source:
            query += " AND source=?"
            params.append(source)
        if data_type:
            query += " AND data_type=?"
            params.append(data_type)
        if since:
            query += " AND timestamp>=?"
            params.append(since)
        if until:
            query += " AND timestamp<=?"
            params.append(until)
        if origin:
            query += " AND origin=?"
            params.append(origin)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([n, offset])
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def get_positions(self, since_hours: float | None = None) -> list[dict]:
        query = """
            SELECT callsign, last_seen, last_lat AS lat, last_lon AS lon,
                   last_symbol AS symbol, last_speed AS speed,
                   last_course AS course, last_altitude AS altitude,
                   last_comment AS comment, last_origin AS origin
            FROM stations
            WHERE last_lat IS NOT NULL AND last_lon IS NOT NULL
        """
        params: list[object] = []
        if since_hours is not None:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=since_hours)
            ).isoformat()
            query += " AND last_seen>=?"
            params.append(cutoff)
        query += " ORDER BY last_seen DESC"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def get_stations(self) -> list[dict]:
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM stations ORDER BY last_seen DESC"
                ).fetchall()
            ]

    def get_telemetry(self, hours: int = 24) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM telemetry WHERE timestamp>=? ORDER BY timestamp",
                    (cutoff,),
                ).fetchall()
            ]

    def get_latest_telemetry(self) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM telemetry ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else {}

    def get_stats(self) -> dict:
        with self._connect() as conn:
            frames_total = conn.execute(
                "SELECT COUNT(*) FROM frames"
            ).fetchone()[0]
            stations_total = conn.execute(
                "SELECT COUNT(*) FROM stations"
            ).fetchone()[0]
            last_frame_time = conn.execute(
                "SELECT timestamp FROM frames ORDER BY id DESC LIMIT 1"
            ).fetchone()
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).isoformat()
            frames_per_hour = conn.execute(
                "SELECT COUNT(*) FROM frames WHERE timestamp>=?",
                (cutoff,),
            ).fetchone()[0]
            origin_rows = conn.execute(
                "SELECT origin, COUNT(*) AS count FROM frames GROUP BY origin"
            ).fetchall()

        db_size_bytes = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        disk = shutil.disk_usage(str(Path(self.db_path).anchor or "/"))
        origins = {row["origin"]: row["count"] for row in origin_rows}
        return {
            "frames_total": frames_total,
            "stations_total": stations_total,
            "last_frame_time": last_frame_time["timestamp"] if last_frame_time else None,
            "frames_per_hour": frames_per_hour,
            "db_size_bytes": db_size_bytes,
            "db_free_bytes": disk.free,
            "origins": origins,
        }

    def get_analyse(self, since_hours: float | None = None) -> dict:
        """Aggregate stats for the Analyse tab (time-filtered from live DB)."""
        if since_hours is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
            tc = "AND f.timestamp >= ?"
            p: list[object] = [cutoff]
        else:
            tc = ""
            p = []

        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM frames f WHERE 1=1 {tc}", p
            ).fetchone()[0]

            origins_rows = conn.execute(
                f"SELECT origin, COUNT(*) AS cnt FROM frames f WHERE 1=1 {tc} GROUP BY origin",
                p,
            ).fetchall()

            stations_rows = conn.execute(
                f"""
                SELECT
                    f.source AS callsign,
                    COUNT(*) AS frame_count,
                    MAX(f.timestamp) AS last_seen,
                    MAX(CASE WHEN f.origin = 'rf' THEN 1 ELSE 0 END) AS has_direct,
                    MAX(CASE WHEN f.origin = 'rf_digi' THEN 1 ELSE 0 END) AS has_digi,
                    MAX(CASE WHEN f.speed IS NOT NULL AND f.speed > 0 THEN 1 ELSE 0 END) AS is_mobile,
                    s.last_lat, s.last_lon, s.last_symbol, s.last_comment, s.last_origin
                FROM frames f
                LEFT JOIN stations s ON s.callsign = f.source
                WHERE f.source != '' {tc}
                GROUP BY f.source
                ORDER BY last_seen DESC
                """,
                p,
            ).fetchall()

        return {
            "total_frames": total,
            "origins": {row["origin"]: row["cnt"] for row in origins_rows},
            "stations": [dict(row) for row in stations_rows],
        }

    def export_frames_csv(
        self,
        since: str | None = None,
        until: str | None = None,
        source: str | None = None,
        data_type: str | None = None,
        origin: str | None = None,
    ) -> str:
        frames = self.get_frames(
            n=999999,
            since=since,
            until=until,
            source=source,
            data_type=data_type,
            origin=origin,
        )
        if not frames:
            return ""
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=frames[0].keys())
        writer.writeheader()
        writer.writerows(frames)
        return output.getvalue()

    def export_telemetry_csv(self, hours: int = 24) -> str:
        telemetry = self.get_telemetry(hours=hours)
        if not telemetry:
            return ""
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=telemetry[0].keys())
        writer.writeheader()
        writer.writerows(telemetry)
        return output.getvalue()
