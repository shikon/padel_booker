import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from playwright.sync_api import sync_playwright

DAYS_AHEAD = 7
TZ_NL = ZoneInfo("Europe/Amsterdam")
VHV_COORDS = (51.4192, 5.4050)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}

VENUE_MAX_COURTS = {
    "padellers_vhv": 6,
    "peakz_beurs": 8,
    "peakz_vijfkamp": 7,
    "david_lloyd_vhv": 4,
}


def time_to_mins(t_str):
    h, m = map(int, t_str[:5].split(":"))
    return h * 60 + m


def mins_to_time(mins):
    return f"{(mins // 60) % 24:02d}:{mins % 60:02d}"


def parse_price(price_val):
    if isinstance(price_val, (int, float)):
        return float(price_val)
    if isinstance(price_val, str):
        m = re.search(r"(\d+(?:[.,]\d+)?)", price_val)
        if m:
            return float(m.group(1).replace(",", "."))
    return 0.0


def fetch_playtomic_david_lloyd():
    """Fetches real slots from Playtomic for David Lloyd Eindhoven (located in Veldhoven)."""
    blocks = []
    try:
        r = requests.get(
            "https://api.app.playtomic.io/v1/tenants",
            params={
                "coordinate": f"{VHV_COORDS[0]},{VHV_COORDS[1]}",
                "radius": 10000,
                "sport_id": "PADEL",
            },
            headers=HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        tenants = r.json()
    except Exception as e:
        print(f"[Playtomic] Tenant lookup failed: {e}")
        return blocks

    now_nl = datetime.now(TZ_NL)
    for t in tenants:
        name = t.get("tenant_name", "")
        if "david lloyd" not in name.lower():
            continue

        tid = t.get("tenant_id")
        resources = {
            res["resource_id"]: res.get("name", "DL Indoor Padel")
            for res in t.get("resources", [])
            if res.get("sport_id") == "PADEL"
        }
        print(f"[Playtomic] Matched '{name}' ({tid}) with {len(resources)} courts")

        for day_offset in range(DAYS_AHEAD):
            target_date = (now_nl + timedelta(days=day_offset)).date()
            try:
                av = requests.get(
                    "https://api.app.playtomic.io/v1/availability",
                    params={
                        "tenant_id": tid,
                        "sport_id": "PADEL",
                        "local_start_min": f"{target_date}T07:00:00",
                        "local_start_max": f"{target_date}T23:30:00",
                    },
                    headers=HEADERS,
                    timeout=15,
                )
                if av.status_code != 200:
                    continue
                for court in av.json():
                    res_id = court.get("resource_id")
                    if res_id not in resources:
                        continue
                    court_name = resources[res_id]
                    for s in court.get("slots", []):
                        raw_start = s.get("start_time", "")[:8]
                        if len(raw_start) < 5:
                            continue
                        utc_dt = datetime.strptime(
                            f"{target_date} {raw_start}", "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=timezone.utc)
                        local_dt = utc_dt.astimezone(TZ_NL)
                        blocks.append({
                            "venueId": "david_lloyd_vhv",
                            "date": local_dt.strftime("%Y-%m-%d"),
                            "time": local_dt.strftime("%H:%M"),
                            "duration": int(s.get("duration", 60)),
                            "courtName": court_name,
                            "price": parse_price(s.get("price")),
                        })
            except Exception as e:
                print(f"[Playtomic] Error on {target_date}: {e}")

    print(f"[Playtomic] Extracted {len(blocks)} raw court blocks")
    return blocks


def extract_widget_blocks(payload, default_venue_id, fallback_date):
    """Recursively extracts available time slots from captured Bookaball / FOYS JSON payloads."""
    blocks = []

    def walk(node, venue=default_venue_id, court="Indoor Court", date=fallback_date):
        if isinstance(node, list):
            for item in node:
                walk(item, venue, court, date)
            return
        if not isinstance(node, dict):
            return

        blob = " ".join(str(v).lower() for v in node.values() if isinstance(v, (str, int)))
        if "beurs" in blob:
            venue = "peakz_beurs"
        elif "vijfkamp" in blob:
            venue = "peakz_vijfkamp"
        elif "veldhoven" in blob:
            venue = "padellers_vhv"

        for k in ("court_name", "courtName", "resource_name", "resourceName", "name", "title"):
            val = node.get(k)
            if isinstance(val, str) and any(w in val.lower() for w in ("baan", "court", "padel")):
                court = val
                break

        is_avail = node.get("available", node.get("is_available", node.get("isAvailable", True)))
        start_val = node.get("start_time") or node.get("startTime") or node.get("start") or node.get("from")
        if isinstance(start_val, str) and is_avail is not False:
            iso_m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", start_val)
            time_m = re.match(r"^(\d{2}:\d{2})", start_val)
            d_str = iso_m.group(1) if iso_m else date
            t_str = iso_m.group(2) if iso_m else (time_m.group(1) if time_m else None)
            if t_str:
                blocks.append({
                    "venueId": venue,
                    "date": d_str,
                    "time": t_str,
                    "duration": int(node.get("duration") or node.get("duration_minutes") or 60),
                    "courtName": court,
                    "price": parse_price(node.get("price") or node.get("amount") or node.get("total_price")),
                })

        for v in node.values():
            if isinstance(v, (dict, list)):
                walk(v, venue, court, date)

    walk(payload)
    return blocks


def scrape_widget_venues():
    """Intercepts JSON responses from Bookaball (The Padellers VHV) and FOYS (Peakz EHV)."""
    all_blocks = []
    now_nl = datetime.now(TZ_NL)
    targets = [
        ("padellers_vhv", "https://thepadellers.bookaball.com/nl/bookings/create"),
        ("peakz_beurs", "https://www.peakzpadel.nl/reserveren/court-booking/reservation"),
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="nl-NL", timezone_id="Europe/Amsterdam")

        for default_venue, url in targets:
            page = context.new_page()
            captured = []

            def make_handler(store):
                def on_res(res):
                    if res.status == 200 and "json" in res.headers.get("content-type", ""):
                        try:
                            store.append(res.json())
                        except Exception:
                            pass
                return on_res

            page.on("response", make_handler(captured))
            try:
                page.goto(url, wait_until="networkidle", timeout=35000)
                page.wait_for_timeout(3000)
                for day_offset in range(1, DAYS_AHEAD):
                    day_num = str((now_nl + timedelta(days=day_offset)).day)
                    btn = page.locator(f"button:has-text('{day_num}')").first
                    if btn.count() > 0 and btn.is_visible():
                        try:
                            btn.click(timeout=1500)
                            page.wait_for_timeout(1200)
                        except Exception:
                            pass
            except Exception as e:
                print(f"[Widget] Navigation warning for {default_venue}: {e}")

            print(f"[Widget] Captured {len(captured)} JSON responses for {default_venue}")
            for payload in captured:
                all_blocks.extend(extract_widget_blocks(payload, default_venue, now_nl.strftime("%Y-%m-%d")))
            page.close()

        browser.close()
    return all_blocks


def build_slot_map(raw_blocks):
    by_court = defaultdict(list)
    for b in raw_blocks:
        by_court[(b["venueId"], b["date"], b["courtName"])].append(b)

    aggregated = defaultdict(lambda: {"courts": set(), "prices": []})
    for (venue_id, date_str, court_name), blocks in by_court.items():
        blocks.sort(key=lambda x: time_to_mins(x["time"]))
        start_map = {time_to_mins(b["time"]): (int(b.get("duration") or 30), float(b.get("price") or 0)) for b in blocks}

        for start_m, (first_dur, first_price) in start_map.items():
            for target_dur in (60, 90, 120):
                if first_dur == target_dur:
                    key = (venue_id, date_str, mins_to_time(start_m), target_dur)
                    aggregated[key]["courts"].add(court_name)
                    if first_price > 0:
                        aggregated[key]["prices"].append(first_price)
                elif first_dur == 30:
                    steps = target_dur // 30
                    total_p = 0.0
                    ok = True
                    for s in range(steps):
                        if (start_m + s * 30) not in start_map:
                            ok = False
                            break
                        total_p += start_map[start_m + s * 30][1]
                    if ok:
                        key = (venue_id, date_str, mins_to_time(start_m), target_dur)
                        aggregated[key]["courts"].add(court_name)
                        if total_p > 0:
                            aggregated[key]["prices"].append(total_p)

    slots_out = {}
    for (venue_id, date_str, time_str, dur), info in aggregated.items():
        courts = sorted(info["courts"])
        open_count = len(courts)
        if open_count == 0:
            continue
        max_c = max(VENUE_MAX_COURTS.get(venue_id, open_count), open_count)
        avg_price = round(sum(info["prices"]) / len(info["prices"]), 2) if info["prices"] else round(30.0 * (dur / 60), 2)
        slots_out[f"{venue_id}_{date_str}_{time_str}_{dur}"] = {
            "openCourts": open_count,
            "maxCourts": max_c,
            "totalPrice": avg_price,
            "perPlayerPrice": round(avg_price / 4, 2),
            "isPeak": 17 <= int(time_str[:2]) <= 21,
            "availableCourtNames": courts,
            "supported": True,
        }
    return slots_out


def main():
    slots_map = build_slot_map(fetch_playtomic_david_lloyd() + scrape_widget_venues())
    club_status = {
        vid: ("live" if any(k.startswith(f"{vid}_") for k in slots_map) else "portal_only")
        for vid in VENUE_MAX_COURTS
    }
    output = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "GitHub Actions Live Sync",
        "clubStatus": club_status,
        "slots": slots_map,
    }
    with open("availability.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(slots_map)} slot keys to availability.json")


if __name__ == "__main__":
    main()
