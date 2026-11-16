import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

DAYS_AHEAD = 7
TZ_NL = ZoneInfo("Europe/Amsterdam")
VHV_COORDS = (51.4192, 5.4050)

VENUE_MAX_COURTS = {
    "padellers_vhv": 6,
    "peakz_beurs": 8,
    "peakz_vijfkamp": 7,
    "david_lloyd_vhv": 4,
}


def time_to_mins(t_str):
    try:
        h, m = map(int, str(t_str)[:5].split(":"))
        return h * 60 + m
    except Exception:
        return -1


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


def parse_duration(val):
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, dict):
        for k in ("minutes", "value", "duration"):
            if k in val:
                return parse_duration(val[k])
        return 60
    if isinstance(val, str):
        hm = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", val.strip())
        if hm:
            return int(hm.group(1)) * 60 + int(hm.group(2))
        m = re.search(r"(\d+)", val)
        if m:
            return int(m.group(1))
    return 60


def fetch_playtomic_via_browser(context):
    """Fetches Playtomic slots from inside Chromium on playtomic.io to avoid 403 WAF blocks."""
    blocks = []
    page = context.new_page()
    try:
        page.goto("https://playtomic.io/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        tenants_url = (
            f"https://api.app.playtomic.io/v1/tenants"
            f"?coordinate={VHV_COORDS[0]},{VHV_COORDS[1]}&radius=10000&sport_id=PADEL"
        )
        tenants = page.evaluate(
            """async (url) => {
                const res = await fetch(url, { headers: { 'Accept': 'application/json' } });
                if (!res.ok) return { error: res.status };
                return await res.json();
            }""",
            tenants_url,
        )

        if not isinstance(tenants, list):
            print(f"[Playtomic] Tenant lookup response: {tenants}")
            page.close()
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
                avail_url = (
                    f"https://api.app.playtomic.io/v1/availability"
                    f"?tenant_id={tid}&sport_id=PADEL"
                    f"&local_start_min={target_date}T07:00:00"
                    f"&local_start_max={target_date}T23:30:00"
                )
                avail = page.evaluate(
                    """async (url) => {
                        const res = await fetch(url, { headers: { 'Accept': 'application/json' } });
                        if (!res.ok) return [];
                        return await res.json();
                    }""",
                    avail_url,
                )
                if not isinstance(avail, list):
                    continue

                for court in avail:
                    res_id = court.get("resource_id")
                    if res_id not in resources:
                        continue
                    court_name = resources[res_id]
                    for s in court.get("slots", []):
                        raw_start = str(s.get("start_time", ""))[:8]
                        if len(raw_start) == 5:
                            raw_start += ":00"
                        if len(raw_start) != 8:
                            continue
                        dur = parse_duration(s.get("duration"))
                        if dur not in (30, 45, 60, 90, 120):
                            continue
                        utc_dt = datetime.strptime(
                            f"{target_date} {raw_start}", "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=timezone.utc)
                        local_dt = utc_dt.astimezone(TZ_NL)
                        blocks.append({
                            "venueId": "david_lloyd_vhv",
                            "date": local_dt.strftime("%Y-%m-%d"),
                            "time": local_dt.strftime("%H:%M"),
                            "duration": dur,
                            "courtName": court_name,
                            "price": parse_price(s.get("price")),
                        })
    except Exception as e:
        print(f"[Playtomic] Browser fetch error: {e}")
    finally:
        page.close()

    print(f"[Playtomic] Extracted {len(blocks)} raw court blocks")
    return blocks


def extract_widget_blocks(payload, default_venue_id, fallback_date, url_hint=""):
    """Safely extracts available court slots from Bookaball / FOYS JSON payloads."""
    blocks = []
    if any(w in url_hint.lower() for w in ("occupied", "busy", "my-bookings", "translations")):
        return blocks

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

        status_str = str(node.get("status", "")).lower()
        is_avail = node.get("available", node.get("is_available", node.get("isAvailable", True)))
        is_booked = node.get("booked", node.get("is_booked", False))

        start_val = node.get("start_time") or node.get("startTime") or node.get("start") or node.get("from")
        if (
            isinstance(start_val, str)
            and is_avail is not False
            and is_booked is not True
            and status_str not in ("booked", "occupied", "unavailable", "closed")
        ):
            iso_m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", start_val)
            time_m = re.match(r"^(\d{2}:\d{2})", start_val)
            d_str = iso_m.group(1) if iso_m else date
            t_str = iso_m.group(2) if iso_m else (time_m.group(1) if time_m else None)
            dur = parse_duration(node.get("duration") or node.get("duration_minutes"))

            # Ignore opening-hours entries like duration="15:00:00" (900 mins)
            if t_str and 420 <= time_to_mins(t_str) <= 1380 and dur in (30, 45, 60, 90, 120):
                blocks.append({
                    "venueId": venue,
                    "date": d_str,
                    "time": t_str,
                    "duration": dur,
                    "courtName": court,
                    "price": parse_price(node.get("price") or node.get("amount") or node.get("total_price")),
                })

        for v in node.values():
            if isinstance(v, (dict, list)):
                walk(v, venue, court, date)

    try:
        walk(payload)
    except Exception as e:
        print(f"[Widget] Parse warning ({default_venue_id}): {e}")
    return blocks


def scrape_widget_venues(context):
    """Intercepts JSON responses from Bookaball (The Padellers VHV) and FOYS (Peakz EHV)."""
    all_blocks = []
    now_nl = datetime.now(TZ_NL)
    targets = [
        ("padellers_vhv", "https://thepadellers.bookaball.com/nl/bookings/create"),
        ("peakz_beurs", "https://www.peakzpadel.nl/reserveren/court-booking/reservation"),
    ]

    for default_venue, url in targets:
        page = context.new_page()
        captured = []

        def make_handler(store):
            def on_res(res):
                if res.status == 200 and "json" in res.headers.get("content-type", ""):
                    try:
                        store.append((res.url, res.json()))
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
        for resp_url, payload in captured:
            keys_preview = list(payload.keys())[:6] if isinstance(payload, dict) else f"list[{len(payload)}]"
            print(f"  -> {resp_url[:95]} | {keys_preview}")
            all_blocks.extend(
                extract_widget_blocks(payload, default_venue, now_nl.strftime("%Y-%m-%d"), resp_url)
            )
        page.close()

    return all_blocks


def build_slot_map(raw_blocks):
    by_court = defaultdict(list)
    for b in raw_blocks:
        if time_to_mins(b["time"]) >= 0:
            by_court[(b["venueId"], b["date"], b["courtName"])].append(b)

    aggregated = defaultdict(lambda: {"courts": set(), "prices": []})
    for (venue_id, date_str, court_name), blocks in by_court.items():
        blocks.sort(key=lambda x: time_to_mins(x["time"]))
        start_map = {
            time_to_mins(b["time"]): (parse_duration(b.get("duration")), float(b.get("price") or 0))
            for b in blocks
