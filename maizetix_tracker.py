#!/usr/bin/env python3
"""MaizeTix price tracker.

Snapshots every upcoming game on maizetix.com and builds an HTML dashboard to
help decide when (and at what price) to sell a ticket.

Storage is plain text so it diffs well in git:
    data/games.json       game metadata
    data/snapshots.csv    one row per game per snapshot (append-only)
    data/latest.json      the current listing ladder + pause notice per game

Usage:
    python3 maizetix_tracker.py collect            # take one snapshot
    python3 maizetix_tracker.py report             # write docs/index.html
    python3 maizetix_tracker.py loop [minutes]     # collect + report every N minutes

Stdlib only - no pip installs needed.
"""
import csv
import html
import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.maizetix.com"
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
GAMES_PATH = DATA / "games.json"
SNAPSHOTS_PATH = DATA / "snapshots.csv"
LATEST_PATH = DATA / "latest.json"
DASHBOARD_PATH = HERE / "docs" / "index.html"
TEMPLATE_PATH = HERE / "dashboard_template.html"
UA = "maizetix-price-tracker/1.0 (personal use; polite polling)"
FIELDS = ["ts", "game_id", "listed", "sold", "lowest", "median_sale", "frozen"]

# python.org builds on macOS ship without CA certs; fall back to the system bundle.
_SSL = ssl.create_default_context(
    cafile="/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").exists() else None)


def http(url, data=None):
    headers = {"User-Agent": UA}
    if data is not None:
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30, context=_SSL) as r:
        return r.read().decode("utf-8", "replace")


def fetch_games():
    body = http(f"{BASE}/api/upcoming_games", {"start_index": 0, "end_index": 100})
    return json.loads(body)["games"]


def _money(s):
    return float(str(s).replace(",", "")) if s not in (None, "", -1) else None


def parse_game_page(page):
    """Pull stats and every listing from a /games/<id> page."""
    def stat(label):
        m = re.search(
            rf'info-stats-header">\s*{label}\s*</div>\s*<div class="info-stats-number">\s*\$([\d,.]+)',
            page)
        return _money(m.group(1)) if m else None

    listed = re.search(r'stats-box">\s*(\d+)\s+listed', page)
    sold = re.search(r'stats-box">\s*(\d+)\s+sold', page)
    notice = re.search(r"(Listings for this game will be paused.*?)</div>", page, re.S)

    listings = []
    tbody = re.search(r"<tbody>(.*?)</tbody>", page, re.S)
    if tbody:
        for row in re.split(r"<tr\b", tbody.group(1))[1:]:
            price = re.search(r"<td>\s*\$([\d,.]+)\s*</td>", row)
            if not price:
                continue
            seat = re.search(r"Sect\.\s*([^|<\s]+)\s*\|\s*Row\s*([^|<\s]+)", row)
            listings.append({"price": _money(price.group(1)),
                             "section": seat.group(1) if seat else None,
                             "row": seat.group(2) if seat else None})
    listings.sort(key=lambda l: l["price"])

    return {
        "listed": int(listed.group(1)) if listed else len(listings),
        "sold": int(sold.group(1)) if sold else None,
        "lowest": stat("Lowest Price"),
        "median_sale": stat("Median Sale"),
        "notice": re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", notice.group(1)))).strip()
                  if notice else None,
        "listings": listings,
    }


def _load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def read_snapshots():
    if not SNAPSHOTS_PATH.exists():
        return []
    with SNAPSHOTS_PATH.open() as f:
        return list(csv.DictReader(f))


def collect():
    DATA.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    games = _load_json(GAMES_PATH, {})
    latest = _load_json(LATEST_PATH, {})
    rows = []
    for g in fetch_games():
        gid = str(g["id"])
        games[gid] = {k: g[k] for k in ("home_team", "away_team", "event_type", "date", "time_set", "stadium")}
        # Games with nothing listed yet have no market to track.
        if g["cheapest_ticket_price"] == -1:
            continue
        try:
            info = parse_game_page(http(f"{BASE}/games/{gid}"))
        except Exception as e:  # one bad page shouldn't lose the whole run
            print(f"  ! game {gid}: {e}", file=sys.stderr)
            continue
        rows.append({"ts": ts, "game_id": gid, "listed": info["listed"], "sold": info["sold"],
                     "lowest": info["lowest"] or _money(g["cheapest_ticket_price"]),
                     "median_sale": info["median_sale"],
                     "frozen": int(g.get("listings_frozen", False))})
        latest[gid] = {"ts": ts, "notice": info["notice"], "ladder": info["listings"]}
        print(f"  {g['away_team']:<18} floor ${info['lowest']}  median sale ${info['median_sale']}"
              f"  {info['listed']} listed / {info['sold']} sold")
        time.sleep(1.5)  # be polite

    GAMES_PATH.write_text(json.dumps(games, indent=1, sort_keys=True) + "\n")
    LATEST_PATH.write_text(json.dumps(latest, separators=(",", ":")) + "\n")
    new_file = not SNAPSHOTS_PATH.exists()
    with SNAPSHOTS_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)
    print(f"[{ts}] snapshot saved for {len(rows)} game(s)")


def report(quiet=False):
    games = _load_json(GAMES_PATH, {})
    latest = _load_json(LATEST_PATH, {})
    num = lambda v, f=float: f(v) if v not in (None, "") else None
    by_game = {}
    for s in read_snapshots():
        by_game.setdefault(s["game_id"], []).append({
            "ts": s["ts"], "listed": num(s["listed"], int), "sold": num(s["sold"], int),
            "lowest": num(s["lowest"]), "median_sale": num(s["median_sale"]),
            "frozen": num(s["frozen"], int)})
    now = datetime.now().strftime("%Y-%m-%d")
    out = []
    for gid, g in sorted(games.items(), key=lambda kv: kv[1]["date"]):
        # Show upcoming games even before anyone lists; drop past games that never had a market.
        if gid not in by_game and g["date"][:10] < now:
            continue
        out.append({**g, "id": int(gid), "snapshots": by_game.get(gid, []),
                    "notice": latest.get(gid, {}).get("notice"),
                    "ladder": latest.get(gid, {}).get("ladder", [])})
    data = json.dumps({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "games": out})
    DASHBOARD_PATH.parent.mkdir(exist_ok=True)
    DASHBOARD_PATH.write_text(
        TEMPLATE_PATH.read_text().replace("/*__DATA__*/null", data.replace("</", "<\\/")))
    if not quiet:
        print(f"wrote {DASHBOARD_PATH} ({len(out)} game(s))")


def loop(minutes):
    while True:
        try:
            collect()
            report(quiet=True)
        except Exception as e:
            print(f"collect failed: {e}", file=sys.stderr)
        time.sleep(minutes * 60)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    if cmd == "collect":
        collect()
    elif cmd == "report":
        report()
    elif cmd == "loop":
        loop(float(sys.argv[2]) if len(sys.argv) > 2 else 15)
    else:
        sys.exit(__doc__)
