#!/usr/bin/env python3
"""
mod-aprs-core/aprs_parser.py — Décodeur de trames APRS complet.
Gère : positions (standard + compressed), MIC-E, messages (avec ack/rej),
status, weather, objets, items, telemetry.
"""

import re
import math
from datetime import datetime, timezone


def parse_aprs_frame(raw: str) -> dict:
    """Parse une trame APRS brute en dict structuré."""
    result = {
        "raw": raw.strip(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "",
        "destination": "",
        "path": "",
        "data_type": "unknown",
        "lat": None,
        "lon": None,
        "symbol": "",
        "comment": "",
        "speed": None,
        "course": None,
        "altitude": None,
        # Champs message
        "msg_to": None,
        "msg_text": None,
        "msg_no": None,
        "msg_ack": None,
        "msg_rej": None,
        # Champs météo
        "wx_wind_dir": None,
        "wx_wind_speed": None,
        "wx_gust": None,
        "wx_temp": None,
        "wx_rain_1h": None,
        "wx_rain_24h": None,
        "wx_humidity": None,
        "wx_pressure": None,
    }

    # Header: SOURCE>DEST,PATH:payload
    m = re.match(
        r'^([A-Za-z0-9\-]{1,9})>([A-Za-z0-9\-]{1,9})'
        r'((?:,[A-Za-z0-9\-\*]{1,9})*):(.+)$',
        raw.strip(), re.DOTALL,
    )
    if not m:
        return result

    result["source"] = m.group(1).upper()
    result["destination"] = m.group(2).upper()
    result["path"] = m.group(3).lstrip(",")
    payload = m.group(4)

    if not payload:
        return result

    c = payload[0]

    # --- Position sans timestamp ---
    if c in ('!', '='):
        result["data_type"] = "position"
        _parse_position_field(payload[1:], result)

    # --- Position avec timestamp ---
    elif c in ('/', '@'):
        result["data_type"] = "position_ts"
        if len(payload) > 8:
            _parse_position_field(payload[8:], result)

    # --- MIC-E ---
    elif c in ('`', "'", '\x1c', '\x1d'):
        result["data_type"] = "mic-e"
        _parse_mice(result["destination"], payload[1:], result)

    # --- Message ---
    elif c == ':':
        _parse_message(payload[1:], result)

    # --- Status ---
    elif c == '>':
        result["data_type"] = "status"
        result["comment"] = payload[1:].strip()

    # --- Objet ---
    elif c == ';':
        result["data_type"] = "object"
        _parse_object(payload[1:], result)

    # --- Item ---
    elif c == ')':
        result["data_type"] = "item"
        result["comment"] = payload[1:]

    # --- Weather (positionless) ---
    elif c == '_':
        result["data_type"] = "weather"
        _parse_weather(payload[1:], result)

    # --- Telemetry ---
    elif c == 'T':
        result["data_type"] = "telemetry"
        result["comment"] = payload

    # --- Raw GPS (NMEA) ---
    elif c == '$':
        result["data_type"] = "nmea"
        result["comment"] = payload

    # --- User-defined ---
    elif c == '{':
        result["data_type"] = "user-defined"
        result["comment"] = payload

    # --- Inconnu ---
    else:
        result["comment"] = payload

    return result


# =============================================================
# Position standard et compressée
# =============================================================

def _parse_position_field(data: str, result: dict):
    """Détecte et parse position standard ou compressée."""
    if not data:
        return

    # Position standard : ddmm.hhN/dddmm.hhWs
    m = re.match(
        r'^(\d{4}\.\d{2})([NS])(.)(\d{5}\.\d{2})([EW])(.)(.*)$',
        data, re.DOTALL,
    )
    if m:
        _parse_position_standard(m, result)
        return

    # Position compressée : 13 chars, commence par /\[A-z]
    if len(data) >= 13 and data[0] in ('/\\') or (len(data) >= 13 and 33 <= ord(data[0]) <= 126):
        _parse_position_compressed(data, result)
        return

    result["comment"] = data


def _parse_position_standard(m, result: dict):
    lat_str, lat_dir, sym_table, lon_str, lon_dir, sym_code, rest = m.groups()

    lat_deg = int(lat_str[:2])
    lat_min = float(lat_str[2:])
    result["lat"] = round(lat_deg + lat_min / 60.0, 6)
    if lat_dir == 'S':
        result["lat"] = -result["lat"]

    lon_deg = int(lon_str[:3])
    lon_min = float(lon_str[3:])
    result["lon"] = round(lon_deg + lon_min / 60.0, 6)
    if lon_dir == 'W':
        result["lon"] = -result["lon"]

    result["symbol"] = sym_table + sym_code

    # Extension CSE/SPD
    ext = re.match(r'^(\d{3})/(\d{3})(.*)$', rest, re.DOTALL)
    if ext:
        cse = int(ext.group(1))
        spd = int(ext.group(2))
        if cse > 0 or spd > 0:
            result["course"] = cse
            result["speed"] = spd
        rest = ext.group(3)

    # Altitude /A=xxxxxx
    alt = re.search(r'/A=(-?\d{6})', rest)
    if alt:
        result["altitude"] = int(alt.group(1))

    # Météo intégrée dans la position
    if rest and rest[0] == '_':
        _parse_weather(rest[1:], result)
        result["data_type"] = "weather"
    else:
        result["comment"] = rest.strip()


def _parse_position_compressed(data: str, result: dict):
    """Parse une position APRS compressée (base-91)."""
    try:
        sym_table = data[0]
        lat_enc = data[1:5]
        lon_enc = data[5:9]
        sym_code = data[9]
        cs_enc = data[10:12] if len(data) >= 12 else "  "
        t_byte = ord(data[12]) - 33 if len(data) >= 13 else 0

        # Décodage base-91
        lat_val = 0
        for ch in lat_enc:
            lat_val = lat_val * 91 + (ord(ch) - 33)
        result["lat"] = round(90.0 - lat_val / 380926.0, 6)

        lon_val = 0
        for ch in lon_enc:
            lon_val = lon_val * 91 + (ord(ch) - 33)
        result["lon"] = round(-180.0 + lon_val / 190463.0, 6)

        result["symbol"] = sym_table + sym_code

        # Course/Speed ou Altitude selon le type byte
        c1 = ord(cs_enc[0]) - 33
        c2 = ord(cs_enc[1]) - 33

        if t_byte & 0x18 == 0x10:  # NMEA source
            if c1 >= 0 and c1 <= 89:
                result["course"] = c1 * 4
                result["speed"] = round(1.08 ** c2 - 1, 1)
        elif t_byte & 0x18 == 0x00:  # Compressed altitude
            if c1 >= 0 and c2 >= 0:
                result["altitude"] = round(1.002 ** (c1 * 91 + c2))

        rest = data[13:] if len(data) > 13 else ""
        result["comment"] = rest.strip()

    except (IndexError, ValueError):
        result["comment"] = data


# =============================================================
# MIC-E
# =============================================================

def _parse_mice(dest: str, info: str, result: dict):
    """Parse MIC-E (destination field + info field encoding)."""
    try:
        if len(dest) < 6 or len(info) < 8:
            result["comment"] = f"MIC-E (trop court)"
            return

        # Latitude depuis le champ destination
        lat_digits = ""
        lat_msg_bits = []
        mice_table = {
            '0': ('0', 0), '1': ('1', 0), '2': ('2', 0),
            '3': ('3', 0), '4': ('4', 0), '5': ('5', 0),
            '6': ('6', 0), '7': ('7', 0), '8': ('8', 0),
            '9': ('9', 0),
            'A': ('0', 1), 'B': ('1', 1), 'C': ('2', 1),
            'D': ('3', 1), 'E': ('4', 1), 'F': ('5', 1),
            'G': ('6', 1), 'H': ('7', 1), 'I': ('8', 1),
            'J': ('9', 1),
            'K': ('0', 1), 'L': ('0', 0),
            'P': ('0', 1), 'Q': ('1', 1), 'R': ('2', 1),
            'S': ('3', 1), 'T': ('4', 1), 'U': ('5', 1),
            'V': ('6', 1), 'W': ('7', 1), 'X': ('8', 1),
            'Y': ('9', 1), 'Z': ('0', 1),
        }

        for i in range(6):
            ch = dest[i]
            if ch in mice_table:
                digit, msg_bit = mice_table[ch]
                lat_digits += digit
                lat_msg_bits.append(msg_bit)
            else:
                result["comment"] = f"MIC-E (dest invalide)"
                return

        # Latitude
        lat_deg = int(lat_digits[0:2])
        lat_min = float(lat_digits[2:4] + "." + lat_digits[4:6])
        lat = lat_deg + lat_min / 60.0

        # N/S depuis bit 3
        if lat_msg_bits[3] == 0:
            lat = -lat

        # Longitude offset depuis bit 4
        lon_offset = 100 if lat_msg_bits[4] == 1 else 0

        # E/W depuis bit 5
        lon_west = lat_msg_bits[5] == 0

        # Longitude depuis info field
        lon_deg = (ord(info[0]) - 28) + lon_offset
        if 180 <= lon_deg <= 189:
            lon_deg -= 80
        elif 190 <= lon_deg <= 199:
            lon_deg -= 190

        lon_min = (ord(info[1]) - 28)
        if lon_min >= 60:
            lon_min -= 60

        lon_hun = (ord(info[2]) - 28)
        lon = lon_deg + (lon_min + lon_hun / 100.0) / 60.0

        if lon_west:
            lon = -lon

        result["lat"] = round(lat, 6)
        result["lon"] = round(lon, 6)

        # Speed/Course
        sp = (ord(info[3]) - 28) * 10
        dc = ord(info[4]) - 28
        sp += dc // 10
        cse = (dc % 10) * 100 + (ord(info[5]) - 28)

        if sp >= 0 and sp <= 800:
            result["speed"] = sp
        if 0 <= cse <= 360:
            result["course"] = cse

        # Symbol
        result["symbol"] = info[7] + info[6] if len(info) >= 8 else ""

        # Texte restant
        rest = info[8:] if len(info) > 8 else ""

        # Altitude dans le texte MIC-E
        alt_m = re.search(r'(.)}', rest)
        if alt_m:
            idx = rest.index(alt_m.group(0))
            alt_chars = rest[max(0, idx-3):idx+1]
            # ... altitude MIC-E base-91

        result["comment"] = rest.strip()

    except (IndexError, ValueError, TypeError) as e:
        result["comment"] = f"MIC-E (erreur: {e})"


# =============================================================
# Messages
# =============================================================

def _parse_message(data: str, result: dict):
    """Parse les messages APRS (:to_call :message{no)."""
    # Format: ADDRESSEE :message{nn  (addressee padded to 9 chars)
    if len(data) < 10:
        result["data_type"] = "message"
        result["comment"] = data
        return

    to_call = data[:9].strip()
    msg_body = data[10:] if len(data) > 10 else ""

    # ACK
    ack_m = re.match(r'^ack(\w+)$', msg_body)
    if ack_m:
        result["data_type"] = "message_ack"
        result["msg_to"] = to_call
        result["msg_ack"] = ack_m.group(1)
        return

    # REJ
    rej_m = re.match(r'^rej(\w+)$', msg_body)
    if rej_m:
        result["data_type"] = "message_rej"
        result["msg_to"] = to_call
        result["msg_rej"] = rej_m.group(1)
        return

    # Message normal avec numéro optionnel
    msg_m = re.match(r'^(.*?)(?:\{(\w{1,5}))?$', msg_body, re.DOTALL)
    if msg_m:
        result["data_type"] = "message"
        result["msg_to"] = to_call
        result["msg_text"] = msg_m.group(1)
        result["msg_no"] = msg_m.group(2) or ""

        # Query APRS ?
        if result["msg_text"].startswith("?"):
            result["data_type"] = "query"
    else:
        result["data_type"] = "message"
        result["msg_to"] = to_call
        result["msg_text"] = msg_body

    result["comment"] = f"→{to_call}: {result.get('msg_text', '')}"


# =============================================================
# Objets
# =============================================================

def _parse_object(data: str, result: dict):
    """Parse un objet APRS (;name_____*ddhhmmz...)."""
    if len(data) < 18:
        result["comment"] = data
        return

    obj_name = data[:9].strip()
    alive = data[9]  # '*' = live, '_' = killed
    rest = data[10:]

    result["comment"] = f"{'OBJ' if alive == '*' else 'DEL'}: {obj_name}"

    # Le reste est un timestamp + position
    if len(rest) > 7:
        _parse_position_field(rest[7:], result)


# =============================================================
# Météo
# =============================================================

def _parse_weather(data: str, result: dict):
    """Parse les données météo APRS."""
    result["data_type"] = "weather"

    # Wind direction
    m = re.search(r'(\d{3})/(\d{3})', data)
    if m:
        result["wx_wind_dir"] = int(m.group(1))
        result["wx_wind_speed"] = int(m.group(2))

    # Gust
    m = re.search(r'g(\d{3})', data)
    if m:
        result["wx_gust"] = int(m.group(1))

    # Temperature (Fahrenheit)
    m = re.search(r't(-?\d{2,3})', data)
    if m:
        f = int(m.group(1))
        result["wx_temp"] = round((f - 32) * 5 / 9, 1)

    # Rain last hour (1/100 inch)
    m = re.search(r'r(\d{3})', data)
    if m:
        result["wx_rain_1h"] = round(int(m.group(1)) * 0.254, 1)

    # Rain 24h
    m = re.search(r'p(\d{3})', data)
    if m:
        result["wx_rain_24h"] = round(int(m.group(1)) * 0.254, 1)

    # Humidity
    m = re.search(r'h(\d{2})', data)
    if m:
        h = int(m.group(1))
        result["wx_humidity"] = 100 if h == 0 else h

    # Barometric pressure (1/10 mbar)
    m = re.search(r'b(\d{5})', data)
    if m:
        result["wx_pressure"] = round(int(m.group(1)) / 10.0, 1)

    result["comment"] = data.strip()
