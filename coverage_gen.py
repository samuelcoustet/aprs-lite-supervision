#!/usr/bin/env python3
"""
RF Coverage generator using SRTM elevation data.
Pure Python stdlib — no PIL, numpy, or splat required.
Memory-efficient: raw HGT bytes accessed via struct.unpack_from.

Model: knife-edge Fresnel diffraction (ITU-R P.526).
For APRS on 144.800 MHz, a relay at 1650m AMSL has ~30 dB link budget
margin, so moderate diffraction (ν < NU_THRESHOLD) is tolerated.
"""
import argparse, struct, zlib, math, urllib.request, gzip, os, time

SRTM_BUCKET  = "https://elevation-tiles-prod.s3.amazonaws.com/skadi"
EARTH_R_KM   = 6371.0
K_FACTOR     = 4.0 / 3.0          # effective Earth radius for standard atmosphere
LAMBDA_M     = 300.0 / 144.800    # ~2.074 m at 144.800 MHz
NU_THRESHOLD = 1.0                 # max Fresnel ν allowed (~12 dB diffraction loss)
                                   # increase to extend coverage in shadowed areas


# ── SRTM tile ─────────────────────────────────────────────────────────────────

def _tile_name(lat, lon):
    la = int(math.floor(lat))
    lo = int(math.floor(lon))
    ns = 'N' if la >= 0 else 'S'
    ew = 'E' if lo >= 0 else 'W'
    return f"{ns}{abs(la):02d}{ew}{abs(lo):03d}"


def _download_tile(name, cache_dir="/tmp/srtm_cache"):
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, name + ".hgt")
    if os.path.exists(cache) and os.path.getsize(cache) > 100000:
        print(f"  SRTM cached: {name}", flush=True)
        with open(cache, 'rb') as f:
            return f.read()
    url = f"{SRTM_BUCKET}/{name[:3]}/{name}.hgt.gz"
    print(f"  SRTM download: {url}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "aprs-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        gz = r.read()
    raw = gzip.decompress(gz)
    with open(cache, 'wb') as f:
        f.write(raw)
    return raw


class HGTTile:
    """
    Memory-efficient SRTM tile: keeps raw bytes, reads via struct.unpack_from.
    ~2.9 MB vs ~40 MB Python tuple for a 1201×1201 tile.
    """
    def __init__(self, raw, sw_lat, sw_lon):
        self.raw    = raw
        self.sw_lat = sw_lat
        self.sw_lon = sw_lon
        self.n      = int(round(math.sqrt(len(raw) // 2)))  # 1201 for SRTM3

    def _h(self, ri, ci):
        r   = (self.n - 1) - ri   # HGT stored N→S
        off = (r * self.n + ci) * 2
        v   = struct.unpack_from('>h', self.raw, off)[0]
        return 0.0 if v == -32768 else float(v)

    def elevation(self, lat, lon):
        row = (lat - self.sw_lat) * (self.n - 1)
        col = (lon - self.sw_lon) * (self.n - 1)
        row = max(0.0, min(self.n - 1.001, row))
        col = max(0.0, min(self.n - 1.001, col))
        ri, ci = int(row), int(col)
        fr, fc = row - ri, col - ci
        return (self._h(ri,   ci)   * (1-fr) * (1-fc)
                + self._h(ri+1, ci) * fr     * (1-fc)
                + self._h(ri, ci+1) * (1-fr) * fc
                + self._h(ri+1,ci+1)* fr     * fc)


class TileCache:
    def __init__(self):
        self._tiles = {}

    def elev(self, lat, lon):
        key = _tile_name(lat, lon)
        if key not in self._tiles:
            try:
                raw = _download_tile(key)
                self._tiles[key] = HGTTile(raw, int(math.floor(lat)), int(math.floor(lon)))
            except Exception as e:
                print(f"  WARNING: tile {key} failed: {e}", flush=True)
                self._tiles[key] = None
        t = self._tiles[key]
        return 0.0 if t is None else t.elevation(lat, lon)


# ── Geometry ──────────────────────────────────────────────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    R    = EARTH_R_KM
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a    = (math.sin(dLat/2)**2
            + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
            * math.sin(dLon/2)**2)
    return R * 2 * math.asin(math.sqrt(max(0.0, a)))


def earth_bulge_m(d_km):
    """Earth curvature correction (m) at distance d_km from one end."""
    return (d_km * 1000) ** 2 / (2 * K_FACTOR * EARTH_R_KM * 1000)


def fresnel_nu(excess_h_m, d1_km, d2_km):
    """
    Fresnel-Kirchhoff diffraction parameter ν (ITU-R P.526-15).
    excess_h_m > 0 means the obstacle is above the LOS line (blocking).
    excess_h_m < 0 means clearance (free space).
    """
    d1 = max(d1_km, 0.001) * 1000
    d2 = max(d2_km, 0.001) * 1000
    return excess_h_m * math.sqrt(2.0 * (d1 + d2) / (LAMBDA_M * d1 * d2))


def worst_nu(tc, tx_lat, tx_lon, tx_h_amsl, rx_lat, rx_lon, rx_agl=2.0, samples=40):
    """
    Walk the path, return the worst (highest) Fresnel ν found.
    ν < 0   → clear LOS
    ν = 0   → obstacle exactly on LOS (6 dB extra loss)
    ν = 1   → ~12 dB diffraction loss
    ν = 1.5 → ~15 dB diffraction loss
    """
    rx_elev   = tc.elev(rx_lat, rx_lon)
    rx_h      = rx_elev + rx_agl
    total_km  = haversine_km(tx_lat, tx_lon, rx_lat, rx_lon)
    if total_km < 0.05:
        return -999.0

    nu_max = -999.0
    for i in range(1, samples):
        t       = i / samples
        s_lat   = tx_lat + t * (rx_lat - tx_lat)
        s_lon   = tx_lon + t * (rx_lon - tx_lon)
        terrain = tc.elev(s_lat, s_lon)
        d_tx    = total_km * t
        d_rx    = total_km * (1 - t)
        los_h   = tx_h_amsl + t * (rx_h - tx_h_amsl)
        bulge   = earth_bulge_m(min(d_tx, d_rx))
        excess  = (terrain + bulge) - los_h   # + = blocking, - = clearance
        nu      = fresnel_nu(excess, d_tx, d_rx)
        if nu > nu_max:
            nu_max = nu
    return nu_max


# ── PNG writer ────────────────────────────────────────────────────────────────

def write_png(width, height, rgba_bytes):
    def chunk(name, data):
        buf = name + data
        return (struct.pack('>I', len(data)) + buf
                + struct.pack('>I', zlib.crc32(buf) & 0xffffffff))
    raw_rows = b''
    stride   = width * 4
    for row in range(height):
        raw_rows += b'\x00' + rgba_bytes[row*stride:(row+1)*stride]
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw_rows, 6))
            + chunk(b'IEND', b''))


# ── Coverage pixel colour ─────────────────────────────────────────────────────

def nu_to_rgba(nu, nu_threshold):
    """
    Map ν to RGBA:
      ν < -0.7  → solid green  (full LOS)
      -0.7..0   → green fading to yellow-green (slight clearance)
      0..ν_thr  → yellow-green fading to orange (diffraction zone, still covered)
    """
    if nu < -0.7:
        # Full LOS: solid green
        return (0, 200, 60, 217)
    elif nu < 0.0:
        # Near-LOS: slight fade, still bright green
        t = (nu + 0.7) / 0.7   # 0→1 as ν goes -0.7→0
        g = int(200 - t * 20)
        return (0, g, 60, 217)
    else:
        # Diffraction zone (0 ≤ ν < nu_threshold)
        t = nu / nu_threshold   # 0→1
        r = int(t * 180)
        g = int(200 - t * 80)
        a = int(217 - t * 50)
        return (r, g, 60, a)


# ── Main ──────────────────────────────────────────────────────────────────────

def generate_coverage(lat, lon, height_m=15.0, radius_km=20.0,
                      grid_size=128, outfile=None, nu_threshold=NU_THRESHOLD):
    if outfile is None:
        outfile = "/tmp/coverage_cache.png"

    dlat  = radius_km / 111.0
    dlon  = radius_km / (111.0 * math.cos(math.radians(lat)))
    south, north = lat - dlat, lat + dlat
    west,  east  = lon - dlon, lon + dlon

    tc       = TileCache()
    tx_elev  = tc.elev(lat, lon)
    tx_h     = tx_elev + height_m
    print(f"  Relay {lat:.4f}N {lon:.4f}E  elev={tx_elev:.0f}m  ant={height_m}m  AMSL={tx_h:.0f}m", flush=True)
    print(f"  Model: knife-edge Fresnel ν<{nu_threshold}  λ={LAMBDA_M:.2f}m  r={radius_km}km  grid={grid_size}", flush=True)

    for la, lo in [(south, west), (south, east), (north, west), (north, east)]:
        tc.elev(la, lo)
    print(f"  Tiles loaded. Starting {grid_size}×{grid_size} viewshed…", flush=True)

    rgba = bytearray(grid_size * grid_size * 4)
    t0   = time.time()

    for row in range(grid_size):
        p_lat = north - (row / (grid_size - 1)) * (north - south)
        for col in range(grid_size):
            p_lon = west + (col / (grid_size - 1)) * (east - west)
            if haversine_km(lat, lon, p_lat, p_lon) > radius_km:
                continue
            nu = worst_nu(tc, lat, lon, tx_h, p_lat, p_lon)
            if nu < nu_threshold:
                r, g, b, a = nu_to_rgba(nu, nu_threshold)
                idx          = (row * grid_size + col) * 4
                rgba[idx]    = r
                rgba[idx+1]  = g
                rgba[idx+2]  = b
                rgba[idx+3]  = a

        if (row + 1) % 16 == 0:
            pct = (row + 1) / grid_size * 100
            print(f"  {pct:.0f}%  {time.time()-t0:.0f}s", flush=True)

    png = write_png(grid_size, grid_size, bytes(rgba))
    with open(outfile, 'wb') as f:
        f.write(png)
    elapsed = time.time() - t0
    print(f"  Done {elapsed:.0f}s → {outfile} ({len(png)}B)", flush=True)

    import json
    meta = {"bounds": [south, west, north, east], "lat": lat, "lon": lon,
            "height_m": height_m, "radius_km": radius_km, "grid": grid_size,
            "elapsed_s": round(elapsed, 1)}
    with open(outfile + ".json", 'w') as f:
        json.dump(meta, f)
    return [south, west, north, east]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="RF coverage generator (APRS 144.800 MHz)")
    ap.add_argument("lat",             type=float,           help="Relay latitude")
    ap.add_argument("lon",             type=float,           help="Relay longitude")
    ap.add_argument("--height", "-H",  type=float, default=15.0,  dest="height_m",   help="Antenna height AGL (m)")
    ap.add_argument("--radius", "-r",  type=float, default=20.0,  dest="radius_km",  help="Coverage radius (km)")
    ap.add_argument("--grid",   "-g",  type=int,   default=128,   dest="grid_size",  help="Grid resolution")
    ap.add_argument("--out",    "-o",  type=str,   default=None,  dest="outfile",    help="Output PNG path")
    ap.add_argument("--nu",     "-n",  type=float, default=NU_THRESHOLD, dest="nu",  help="Max Fresnel ν threshold")
    args = ap.parse_args()

    bounds = generate_coverage(
        args.lat, args.lon,
        height_m=args.height_m,
        radius_km=args.radius_km,
        grid_size=args.grid_size,
        outfile=args.outfile,
        nu_threshold=args.nu,
    )
    print("Bounds:", bounds)
