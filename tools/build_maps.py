"""Build the route map for every crawl page, in one shared style.

For each crawl: fetch streets and parks from OpenStreetMap (Overpass) and a walking
route between the stops (OSRM foot profile), cache both under tools/cache/, then render
an SVG and write it inline into the page between <!-- map:start --> and <!-- map:end -->.
The SVG is colored with CSS variables from map.css, so it follows light and dark mode.

    python3 tools/build_maps.py            # use cached data when present
    python3 tools/build_maps.py --refresh  # refetch from OSM
"""
import json
import math
import re
import subprocess
import sys
import time
import urllib.parse
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "tools" / "cache"
UA = "csinva-pizza-map-build (https://github.com/csinva/pizza)"

W, H = 640, 400          # every map shares this frame (16:10)
PAD = 48                 # px kept clear around the stops and route
STREET_FONT = 9.5        # matches .street-name in map.css
PIN_FONT = 13            # matches .pin-label in map.css

# Stops in walking order; "extras" get a hollow pin and no route.
CRAWLS = {
    "soho": {
        "page": "soho.html",
        "stops": [
            ("Nolita Pizza", 40.7209123, -73.9961959),
            ("Upside Pizza", 40.7221019, -73.9964217),
            ("Prince Street Pizza", 40.7229906, -73.9945851),
        ],
        "extras": [("Rubirosa", 40.7227225, -73.9961142)],
    },
    "west-village": {
        "page": "west-village.html",
        "stops": [
            ("Bleecker Street Pizza", 40.7321893, -74.0034001),
            ("Joe's Pizza", 40.7305467, -74.0020629),
            ("Mama's TOO!", 40.7332170, -74.0040346),
            ("L'Industrie", 40.7332102, -74.0049520),
        ],
        "extras": [],
    },
    "arthur-ave": {
        "page": "slice-crawl.html",
        "stops": [
            ("Joe's Little Italy", 40.8538929, -73.8841568),
            ("Ivana's", 40.8550232, -73.8882162),
            ("Mezza Luna", 40.8553028, -73.8875914),
            ("Crust & Kitchen", 40.8575816, -73.8860807),
            ("Pugsley", 40.8587496, -73.8848173),
        ],
        "extras": [],
    },
}

STREET_W = {"primary": 9, "secondary": 8, "tertiary": 7, "residential": 5.5, "unclassified": 5.5,
            "living_street": 5, "pedestrian": 4}


# ---------- data ----------

def fetch_json(url, data=None):
    # curl rather than urllib: macOS's system Python can't complete TLS with some OSM servers
    cmd = ["curl", "-sSf", "-m", "90", "-A", UA, url]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    for attempt in range(4):  # public OSM servers rate-limit and drop connections; back off and retry
        r = subprocess.run(cmd, input=data, capture_output=True)
        if r.returncode == 0:
            return json.loads(r.stdout)
        time.sleep(5 * (attempt + 1))
    sys.exit(f"fetch failed ({r.returncode}): {url}\n{r.stderr.decode()}")


def cached(name, refresh, fn):
    path = CACHE / f"{name}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    out = fn()
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, separators=(",", ":")))
    time.sleep(1)
    return out


def overpass(bbox):
    b = ",".join(f"{v:.6f}" for v in bbox)
    q = (f'[out:json][timeout:60];('
         f'way["highway"~"^(primary|secondary|tertiary|residential|unclassified|living_street|pedestrian)$"]({b});'
         f'way["leisure"~"^(park|playground|garden)$"]({b});'
         f'way["landuse"="grass"]({b}););out geom;')
    d = fetch_json("https://overpass-api.de/api/interpreter", urllib.parse.urlencode({"data": q}).encode())
    return [{"tags": el.get("tags", {}), "geom": [[g["lat"], g["lon"]] for g in el["geometry"]]}
            for el in d["elements"] if el.get("geometry")]


def walking_route(stops):
    coords = ";".join(f"{lon},{lat}" for _, lat, lon in stops)
    d = fetch_json(f"https://routing.openstreetmap.de/routed-foot/route/v1/driving/{coords}"
                   "?overview=full&geometries=geojson")
    r = d["routes"][0]
    return {"line": [[lat, lon] for lon, lat in r["geometry"]["coordinates"]],
            "legs": [round(leg["distance"]) for leg in r["legs"]], "distance": round(r["distance"])}


# ---------- geometry ----------

class Frame:
    """Equirectangular projection fitted so the given points sit inside the W x H frame."""

    def __init__(self, pts):
        lat0 = sum(p[0] for p in pts) / len(pts)
        self.kx = math.cos(math.radians(lat0))
        xs = [p[1] * self.kx for p in pts]
        ys = [-p[0] for p in pts]
        span = max((max(xs) - min(xs)) / (W - 2 * PAD), (max(ys) - min(ys)) / (H - 2 * PAD), 0.0035 / W)
        self.s = 1 / span
        self.cx, self.cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        self.m_per_px = span * 111320  # 1 deg latitude ~ 111.32 km

    def xy(self, lat, lon):
        return (W / 2 + (lon * self.kx - self.cx) * self.s, H / 2 + (-lat - self.cy) * self.s)

    def bbox(self, margin=60):
        lon = lambda x: ((x - W / 2) / self.s + self.cx) / self.kx
        lat = lambda y: -((y - H / 2) / self.s + self.cy)
        return lat(H + margin), lon(-margin), lat(-margin), lon(W + margin)  # s, w, n, e


def path_d(pts):
    return "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)


def seg_len(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def resample(pts, step):
    """Points every `step` px along a polyline, each with its distance from the start."""
    out, run = [(0.0, pts[0])], 0.0
    nxt = step
    for a, b in zip(pts, pts[1:]):
        L = seg_len(a, b)
        while L and run + L >= nxt:
            t = (nxt - run) / L
            out.append((nxt, (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)))
            nxt += step
        run += L
    return out, run


def inside_runs(pts, margin):
    """Consecutive stretches of a polyline that stay inside the frame."""
    runs, cur = [], []
    for p in pts:
        if margin < p[0] < W - margin and margin < p[1] < H - margin:
            cur.append(p)
        else:
            if len(cur) > 1:
                runs.append(cur)
            cur = []
    if len(cur) > 1:
        runs.append(cur)
    return runs


def dist_to_seg(p, a, b):
    L2 = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2
    t = 0 if not L2 else max(0, min(1, ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / L2))
    return math.hypot(p[0] - a[0] - t * (b[0] - a[0]), p[1] - a[1] - t * (b[1] - a[1]))


def near_line(p, pts, d):
    return any(dist_to_seg(p, a, b) < d for a, b in zip(pts, pts[1:]))


def boxes_hit(a, b, gap=3):
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1])


def abbreviate(name):
    for long, short in (("Street", "St"), ("Avenue", "Ave"), ("East ", "E "), ("West ", "W "), ("South", "S"),
                        ("North", "N"), ("Place", "Pl"), ("Boulevard", "Blvd")):
        name = name.replace(long, short)
    return name


# ---------- labels ----------

def place_pin_labels(points, names, obstacles):
    """Put each stop's name on the first side of its pin that clears the frame, pins and other labels."""
    placed = []
    sides = ((22, 4.5, "start"), (-22, 4.5, "end"), (0, -21, "middle"), (0, 30, "middle"),
             (16, -15, "start"), (-16, -15, "end"), (16, 25, "start"), (-16, 25, "end"))
    for i, ((x, y), name) in enumerate(zip(points, names)):
        tw, th = len(name) * PIN_FONT * 0.56, 14
        choice = None
        for dx, dy, anchor in sides:
            lx, ly = x + dx, y + dy
            left = lx if anchor == "start" else lx - tw if anchor == "end" else lx - tw / 2
            box = (left, ly - th + 3, left + tw, ly + 3)
            on_map = box[0] > 6 and box[2] < W - 40 and box[1] > 6 and box[3] < H - 24
            others = [b for j, b in enumerate(obstacles) if j != i] + [p[3] for p in placed]
            if on_map and not any(boxes_hit(box, b) for b in others):
                choice = (lx, ly, anchor, box)
                break
        placed.append(choice or (x + 19, y + 4.5, "start", (x + 19, y - 7, x + 19 + tw, y + 7)))
    return placed


def place_street_labels(slug, streets, route_pts, obstacles):
    """Label the streets the route walks first, then the longest others.

    A label goes on a straight, in-frame stretch of its street, clear of pins, stop names and
    earlier labels; on a street the route uses it sits just beside the dotted line.
    """
    by_name = {}
    for el in streets:
        name = el["tags"].get("name")
        if name:
            by_name.setdefault(name, []).extend(inside_runs(el["px"], 10))
    route_samples = [p for _, p in resample(route_pts, 4)[0]]

    def on_route(runs):
        return sum(1 for p in route_samples if any(near_line(p, r, 5) for r in runs))

    def length(runs):
        return sum(sum(seg_len(a, b) for a, b in zip(r, r[1:])) for r in runs)

    order = sorted(by_name.items(), key=lambda kv: (-on_route(kv[1]), -length(kv[1])))
    taken, out = [], []
    for name, runs in order:
        if len(out) >= 9:
            break
        label = abbreviate(name).upper()
        tw = len(label) * (STREET_FONT * 0.6 + 1.2)
        routed = on_route(runs) > 0
        best = None
        for run in sorted(runs, key=lambda r: -length([r])):
            if run[0][0] > run[-1][0]:  # read left to right
                run = run[::-1]
            samples, total = resample(run, 5)
            if total < tw + 16:
                continue
            for frac in (0.5, 0.35, 0.65, 0.2, 0.8):
                c = total * frac
                span = [p for d, p in samples if c - tw / 2 - 4 <= d <= c + tw / 2 + 4]
                if c - tw / 2 < 4 or c + tw / 2 > total - 4 or len(span) < 3:
                    continue
                a0 = math.atan2(span[1][1] - span[0][1], span[1][0] - span[0][0])
                a1 = math.atan2(span[-1][1] - span[-2][1], span[-1][0] - span[-2][0])
                if abs((a1 - a0 + math.pi) % (2 * math.pi) - math.pi) > 0.35:
                    continue  # skip bends
                boxes = [(x - 6, y - 6, x + 6, y + 6) for x, y in span]
                if any(boxes_hit(b, o, 0) for b in boxes for o in obstacles + taken):
                    continue
                best = (run, c, boxes)
                break
            if best:
                break
        if not best:
            continue
        run, c, boxes = best
        taken += boxes
        pid = f"st-{slug}-{len(out)}"
        dy = -10 if routed else 3.3  # beside the route line, or centred in the street
        out.append(f'<path id="{pid}" d="{path_d(run)}" fill="none"/>'
                   f'<text class="street-name" dy="{dy}"><textPath href="#{pid}" startOffset="{c:.0f}" '
                   f'text-anchor="middle">{escape(label)}</textPath></text>')
    return out


# ---------- render ----------

def render(slug, cfg, refresh):
    stops, extras = cfg["stops"], cfg["extras"]
    route = cached(f"{slug}-route", refresh, lambda: walking_route(stops))
    frame = Frame([(lat, lon) for _, lat, lon in stops + extras] + [tuple(p) for p in route["line"]])
    osm = cached(f"{slug}-osm", refresh, lambda: overpass(frame.bbox()))
    for el in osm:
        el["px"] = [frame.xy(*p) for p in el["geom"]]
    streets = sorted((el for el in osm if "highway" in el["tags"]),
                     key=lambda el: STREET_W.get(el["tags"]["highway"], 5))
    parks = [el for el in osm if "highway" not in el["tags"]]
    route_pts = [frame.xy(lat, lon) for lat, lon in route["line"]]
    names = [n for n, _, _ in stops + extras]
    pins = [frame.xy(lat, lon) for _, lat, lon in stops + extras]

    pin_boxes = [(x - 15, y - 15, x + 15, y + 15) for x, y in pins]
    chrome = [(0, H - 26, 200, H), (W - 200, H - 22, W, H), (W - 40, 0, W, 50)]
    pin_labels = place_pin_labels(pins, names, pin_boxes)
    street_labels = place_street_labels(slug, streets, route_pts, pin_boxes + [p[3] for p in pin_labels] + chrome)

    out = [f'<svg class="route-map" viewBox="0 0 {W} {H}" role="img" aria-labelledby="map-title-{slug}">',
           f'<title id="map-title-{slug}">Walking route map: {escape(", then ".join(n for n, _, _ in stops))}</title>',
           f'<rect width="{W}" height="{H}" fill="var(--map-bg)"/>']
    out += [f'<path d="{path_d(el["px"])}Z" fill="var(--map-park)"/>' for el in parks]
    out += [f'<path d="{path_d(el["px"])}" fill="none" stroke="var(--map-street)" '
            f'stroke-width="{STREET_W.get(el["tags"]["highway"], 5)}" stroke-linecap="round" stroke-linejoin="round"/>'
            for el in streets]
    out += street_labels

    # Route: a soft casing, then the dotted sauce line
    out.append(f'<path d="{path_d(route_pts)}" fill="none" stroke="var(--map-bg)" stroke-width="8" '
               'stroke-linecap="round" stroke-linejoin="round" opacity=".7"/>')
    out.append(f'<path d="{path_d(route_pts)}" fill="none" stroke="var(--sauce)" stroke-width="3.5" '
               'stroke-dasharray="1 7" stroke-linecap="round" stroke-linejoin="round"/>')

    for i, ((x, y), (lx, ly, anchor, _)) in enumerate(zip(pins, pin_labels)):
        if i >= len(stops):
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="11" fill="var(--map-bg)" stroke="var(--sauce)" stroke-width="2.5"/>'
                       f'<text class="pin-num extra" x="{x:.1f}" y="{y + 7.5:.1f}">*</text>')
        else:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="13" fill="var(--sauce)" stroke="var(--map-bg)" stroke-width="3"/>'
                       f'<text class="pin-num" x="{x:.1f}" y="{y + 5:.1f}">{i + 1}</text>')
        out.append(f'<text class="pin-label" x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}">{escape(names[i])}</text>')

    # Scale bar (a round number of meters near 90 px), north arrow, attribution
    meters = min((50, 100, 200, 250, 500, 1000), key=lambda m: abs(m - frame.m_per_px * 90))
    px = meters / frame.m_per_px
    out.append(f'<g class="map-chrome"><path d="M14 {H - 16} v5 h{px:.1f} v-5" fill="none" stroke="var(--map-ink)" stroke-width="1.5"/>'
               f'<text x="{14 + px + 6:.1f}" y="{H - 11}">{meters} m</text>'
               f'<text x="{W - 22}" y="25" text-anchor="middle">N</text>'
               f'<path d="M{W - 22} 30 l-5 12 l5 -3 l5 3 z" fill="var(--map-ink)"/>'
               f'<text x="{W - 8}" y="{H - 8}" text-anchor="end" class="attrib">© OpenStreetMap contributors</text></g>')
    out.append("</svg>")

    miles = route["distance"] / 1609.34
    minutes = round(route["distance"] / 80)  # ~80 m per minute on foot
    caption = f'Walking route: <b>{miles:.1f} mi</b>, about <b>{minutes} min</b> on foot between stops.'
    if extras:
        caption += " Hollow pins are optional stops."
    caption += ' Map data © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors.'
    legs = ",".join(map(str, route["legs"]))
    block = (f'<!-- map:start (generated by tools/build_maps.py, do not edit by hand) -->\n'
             f'<figure class="map" data-legs="{legs}">\n{"".join(out)}\n<figcaption>{caption}</figcaption>\n</figure>\n'
             f'<!-- map:end -->')

    page = ROOT / cfg["page"]
    html, n = re.subn(r"<!-- map:start.*?<!-- map:end -->", lambda _: block, page.read_text(), flags=re.S)
    if n != 1:
        sys.exit(f"{cfg['page']}: expected one map:start/map:end block, found {n}")
    page.write_text(html)
    print(f"{cfg['page']}: {len(streets)} streets, {len(street_labels)} labels, route {route['distance']} m, legs {route['legs']}")


if __name__ == "__main__":
    refresh = "--refresh" in sys.argv
    for slug, cfg in CRAWLS.items():
        render(slug, cfg, refresh)
