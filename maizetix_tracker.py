#!/usr/bin/env python3
"""MaizeTix price tracker.

Snapshots every upcoming game on maizetix.com and builds an HTML dashboard to
help decide when (and at what price) to sell a ticket.

Storage is plain text so it diffs well in git:
    data/games.json            game metadata
    data/snapshots.csv         one row per game per snapshot (append-only)
    data/listing_events.csv    every listing's lifecycle: listed / price_change /
                               removed (append-only; only changes are written)
    data/latest.json           the current listing ladder + pause notice per game
                               (rebuilt every run, not committed)

Seller initials and names are deliberately not recorded: this data is
published, and they say nothing about price.

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
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://www.maizetix.com"
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
GAMES_PATH = DATA / "games.json"
SNAPSHOTS_PATH = DATA / "snapshots.csv"
EVENTS_PATH = DATA / "listing_events.csv"
LATEST_PATH = DATA / "latest.json"
DASHBOARD_PATH = HERE / "docs" / "index.html"
TEMPLATE_PATH = HERE / "dashboard_template.html"
UA = "maizetix-price-tracker/1.0 (personal use; polite polling)"
FIELDS = ["ts", "game_id", "listed", "sold", "lowest", "median_sale", "frozen"]
# event: "existing" (already listed when tracking began), "listed", "price_change", "removed".
# sold_delta: on "removed" rows, how many sales the game logged since the previous
# snapshot - if it's positive, the removal was probably a sale.
EVENT_FIELDS = ["ts", "game_id", "listing_id", "event", "price", "prev_price",
                "section", "row", "seat", "sold_delta"]

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
            seat = re.search(
                r"Sect\.\s*([^|<\s]+)\s*\|\s*Row\s*([^|<\s]+)\s*\|\s*Seat\s*([^|<\s]+)", row)
            lid = re.search(r"/tickets/([0-9A-Za-z]+)", row)
            listings.append({"id": lid.group(1) if lid else None,
                             "price": _money(price.group(1)),
                             "section": seat.group(1) if seat else None,
                             "row": seat.group(2) if seat else None,
                             "seat": seat.group(3) if seat else None})
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


def _read_csv(path):
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def _append_csv(path, fields, rows):
    new_file = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fields)
        if new_file:
            w.writeheader()
        w.writerows(rows)


def replay_events(events):
    """Fold the event log into {listing_id: state} for listings still active."""
    active = {}
    for e in events:
        if e["event"] == "removed":
            active.pop(e["listing_id"], None)
        else:
            state = active.setdefault(e["listing_id"], {
                "game_id": e["game_id"], "first_seen": e["ts"], "first_event": e["event"],
                "first_price": float(e["price"]), "changes": 0,
                "section": e["section"], "row": e["row"], "seat": e["seat"]})
            if e["event"] == "price_change":
                state["changes"] += 1
            state["price"] = float(e["price"])
    return active


def diff_listings(ts, gid, current, active, tracked_game, sold_delta):
    """Events describing how a game's listings changed since the last run."""
    events = []
    seen = set()
    for l in current:
        if not l["id"]:
            continue
        seen.add(l["id"])
        prev = active.get(l["id"])
        base = {"ts": ts, "game_id": gid, "listing_id": l["id"], "price": l["price"],
                "section": l["section"], "row": l["row"], "seat": l["seat"]}
        if prev is None:
            # A game we've never logged: everything was already up before we looked.
            events.append({**base, "event": "listed" if tracked_game else "existing"})
        elif abs(prev["price"] - l["price"]) > 0.001:
            events.append({**base, "event": "price_change", "prev_price": prev["price"]})
    for lid, prev in active.items():
        if prev["game_id"] == gid and lid not in seen:
            events.append({"ts": ts, "game_id": gid, "listing_id": lid, "event": "removed",
                           "price": prev["price"], "section": prev["section"], "row": prev["row"],
                           "seat": prev["seat"], "sold_delta": sold_delta})
    return events


def collect():
    DATA.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    games = _load_json(GAMES_PATH, {})
    latest = _load_json(LATEST_PATH, {})
    past_events = _read_csv(EVENTS_PATH)
    active = replay_events(past_events)
    tracked_games = {e["game_id"] for e in past_events}
    last_sold = {}
    for s in _read_csv(SNAPSHOTS_PATH):
        if s["sold"]:
            last_sold[s["game_id"]] = int(s["sold"])

    rows, events = [], []
    for g in fetch_games():
        gid = str(g["id"])
        games[gid] = {k: g[k] for k in ("home_team", "away_team", "event_type", "date", "time_set", "stadium")}
        has_market = g["cheapest_ticket_price"] != -1
        # Games with nothing listed have no page worth fetching - unless we were
        # tracking listings there, in which case they've all just gone.
        if not has_market and not any(a["game_id"] == gid for a in active.values()):
            continue
        try:
            info = parse_game_page(http(f"{BASE}/games/{gid}"))
        except Exception as e:  # one bad page shouldn't lose the whole run
            print(f"  ! game {gid}: {e}", file=sys.stderr)
            continue
        sold_delta = (info["sold"] - last_sold[gid]
                      if info["sold"] is not None and gid in last_sold else "")
        events += diff_listings(ts, gid, info["listings"], active, gid in tracked_games, sold_delta)
        rows.append({"ts": ts, "game_id": gid, "listed": info["listed"], "sold": info["sold"],
                     "lowest": info["lowest"] or _money(g["cheapest_ticket_price"]),
                     "median_sale": info["median_sale"],
                     "frozen": int(g.get("listings_frozen", False))})
        latest[gid] = {"ts": ts, "notice": info["notice"], "ladder": info["listings"]}
        print(f"  {g['away_team']:<18} floor ${info['lowest']}  median sale ${info['median_sale']}"
              f"  {info['listed']} listed / {info['sold']} sold  ({len(events)} events so far)")
        time.sleep(1.5)  # be polite

    GAMES_PATH.write_text(json.dumps(games, indent=1, sort_keys=True) + "\n")
    LATEST_PATH.write_text(json.dumps(latest, separators=(",", ":")) + "\n")
    _append_csv(SNAPSHOTS_PATH, FIELDS, rows)
    _append_csv(EVENTS_PATH, EVENT_FIELDS, events)
    print(f"[{ts}] snapshot saved for {len(rows)} game(s), {len(events)} listing event(s)")


def likely_sales(events):
    """Estimate individual sale prices from the event log.

    When listings vanish during an interval in which the game's sold counter went
    up by N, the N cheapest of them most likely sold (buyers take the cheap end).
    """
    by_run = {}
    for e in events:
        if e["event"] == "removed" and e["sold_delta"] not in ("", None):
            by_run.setdefault((e["ts"], e["game_id"]), []).append(e)
    sales = []
    for (ts, gid), removed in by_run.items():
        n = int(removed[0]["sold_delta"])
        for e in sorted(removed, key=lambda e: float(e["price"]))[:max(n, 0)]:
            sales.append({"ts": ts, "game_id": gid, "price": float(e["price"]),
                          "section": e["section"], "row": e["row"], "listing_id": e["listing_id"]})
    return sorted(sales, key=lambda s: s["ts"])


def listing_histories(events, sales):
    """Per game, every listing's full timeline: start, price changes, end, outcome.

    start_exact is False for listings already up when tracking began, so their
    time on market is a lower bound. Outcome for removed listings: "sold" (likely
    sale), "withdrawn" (no sales that interval), or "unclear" (sales happened but
    other removals better explain them).
    """
    sold_ids = {x["listing_id"] for x in sales}
    by_id = {}
    for e in events:
        h = by_id.get(e["listing_id"])
        if h is None or (e["event"] in ("listed", "existing") and h["end"]):
            # First sighting, or a relisting under the same ID after removal.
            h = by_id[e["listing_id"]] = {
                "id": e["listing_id"], "game_id": e["game_id"], "section": e["section"],
                "row": e["row"], "seat": e["seat"], "start": e["ts"],
                "start_exact": e["event"] == "listed", "end": None, "outcome": None, "prices": []}
        if e["event"] == "removed":
            h["end"] = e["ts"]
            h["outcome"] = ("sold" if e["listing_id"] in sold_ids
                            else "withdrawn" if e["sold_delta"] in ("0", 0) else "unclear")
        else:
            h["prices"].append([e["ts"], float(e["price"])])
    out = {}
    for h in by_id.values():
        out.setdefault(h.pop("game_id"), []).append(h)
    return out


def report(quiet=False):
    games = _load_json(GAMES_PATH, {})
    latest = _load_json(LATEST_PATH, {})
    events = _read_csv(EVENTS_PATH)
    active = replay_events(events)
    sales = likely_sales(events)
    histories = listing_histories(events, sales)
    now = datetime.now(timezone.utc)
    since_3h = (now - timedelta(hours=3)).isoformat(timespec="seconds")

    num = lambda v, f=float: f(v) if v not in (None, "") else None
    by_game = {}
    for s in _read_csv(SNAPSHOTS_PATH):
        by_game.setdefault(s["game_id"], []).append({
            "ts": s["ts"], "listed": num(s["listed"], int), "sold": num(s["sold"], int),
            "lowest": num(s["lowest"]), "median_sale": num(s["median_sale"]),
            "frozen": num(s["frozen"], int)})

    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for gid, g in sorted(games.items(), key=lambda kv: kv[1]["date"]):
        # Show upcoming games even before anyone lists; drop past games that never had a market.
        if gid not in by_game and g["date"][:10] < today:
            continue
        ladder = latest.get(gid, {}).get("ladder", [])
        for l in ladder:  # enrich with history from the event log
            st = active.get(l.get("id"))
            if st:
                l.update(first_seen=st["first_seen"], since_start=st["first_event"] == "existing",
                         first_price=st["first_price"], changes=st["changes"])
        recent = [e for e in events if e["game_id"] == gid and e["ts"] >= since_3h
                  and e["event"] != "existing"]
        game_sales = [s for s in sales if s["game_id"] == gid]
        out.append({**g, "id": int(gid), "snapshots": by_game.get(gid, []),
                    "notice": latest.get(gid, {}).get("notice"),
                    "ladder": ladder,
                    "activity": {
                        "listed": sum(e["event"] == "listed" for e in recent),
                        "price_cuts": sum(e["event"] == "price_change" and float(e["price"]) < float(e["prev_price"])
                                          for e in recent),
                        "price_raises": sum(e["event"] == "price_change" and float(e["price"]) > float(e["prev_price"])
                                            for e in recent),
                        "removed": sum(e["event"] == "removed" for e in recent),
                    },
                    "sales": game_sales[-100:],
                    "history": histories.get(gid, []),
                    "sales_median_3h": statistics.median([s["price"] for s in game_sales if s["ts"] >= since_3h])
                                       if any(s["ts"] >= since_3h for s in game_sales) else None,
                    "tracking_since": min((e["ts"] for e in events if e["game_id"] == gid), default=None)})
    data = json.dumps({"generated": now.isoformat(timespec="seconds"), "games": out})
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
