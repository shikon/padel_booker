import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

DAYS_AHEAD = 7
TZ_NL = ZoneInfo("Europe/Amsterdam")

VENUE_MAX_COURTS = {
    "padellers_vhv": 6,
    "peakz_beurs": 8,
    "peakz_vijfkamp": 7,
    "david_lloyd_vhv": 4,
}


def time_to_mins(t_str):
    try:
        parts = str(t_str)[:5].split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return -1


def mins_to_time(mins):
    return f"{(mins // 60) % 24:02d}:{mins % 60:02d}"


def parse_price(val):
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        m = re.search(r"(\d+(?:[.,]\d+)?)", val)
        if m:
            return float(m.group(1).replace(",", "."))
    return 0.0


def parse_duration(val):
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        hm = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", val.strip())
        if hm:
            return int(hm.group(1)) * 60 + int(hm.group(2))
        m = re.search(r"(\d+)", val)
        if m:
            return int(m.group(1))
    return 60


def scrape_peakz_location(context, venue_id, location_label, expected_courts):
    """
    Opens Peakz FOYS booking widget, explicitly selects the Eindhoven club
    (Beursgebouw or Vijfkamplaan) so it never pulls Utrecht ('Pon Centre Court'),
    and extracts open slots across DAYS_AHEAD.
    """
    blocks = []
    now_nl = datetime.now(TZ_NL)
    page = context.new_page()
    captured = []

    def on_res(res):
        if res.status == 200 and "json" in res.headers.get("content-type", ""):
            try:
                captured.append((res.url, res.json()))
            except Exception:
                pass

    page.on("response", on_res)

    try:
        page.goto(
            "https://www.peakzpadel.nl/reserveren/court-booking/reservation",
            wait_until="networkidle",
            timeout=35000,
        )
        page.wait_for_timeout(2000)

        # Dismiss Cookiebot banner if present
        try:
            cookie_btn = page.locator("#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll, button:has-text('Alles accepteren')").first
            if cookie_btn.count() > 0 and cookie_btn.is_visible():
                cookie_btn.click(timeout=2000)
                page.wait_for_timeout(1000)
        except Exception:
            pass

        # Wait for any loading modal overlay to disappear
        try:
            page.locator(".modal").first.wait_for(state="hidden", timeout=8000)
        except Exception:
            pass

        # Open location selector if present and click the exact Eindhoven venue
        loc_clicked = False

        # 1. Try vue-multiselect (Peakz FOYS widget uses .multiselect)
        try:
            ms = page.locator(".multiselect").first
            if ms.count() > 0:
                ms.click(force=True, timeout=3000)
                page.wait_for_timeout(500)
                opt = ms.locator(f".multiselect__option:has-text('{location_label}')").first
                if opt.count() > 0:
                    captured.clear()  # Discard initial default-club payloads!
                    opt.dispatch_event("click")
                    loc_clicked = True
                    page.wait_for_timeout(2500)
        except Exception as e:
            print(f"[Peakz] multiselect err for {venue_id}: {e}")

        # 2. Try native <select> dropdowns
        if not loc_clicked:
            try:
                selects = page.locator("select")
                for i in range(selects.count()):
                    sel_el = selects.nth(i)
                    opts = sel_el.locator("option").all_inner_texts()
                    match_opt = next((o for o in opts if location_label.lower() in o.lower()), None)
                    if match_opt:
                        captured.clear()
                        sel_el.select_option(label=match_opt)
                        loc_clicked = True
                        page.wait_for_timeout(2500)
                        break
            except Exception:
                pass

        # 2. Fallback to custom dropdown / combobox
        if not loc_clicked:
            for selector in [
                "text=/Amersfoort -/i",
                "text=/CUPRA Padel Dome/i",
                "[role='combobox']",
                "button:has-text('Locatie')",
                "button:has-text('Utrecht')",
                "[data-testid*='location']",
                f"text=/{location_label}/i",
            ]:
                try:
                    el = page.locator(selector).first
                    if el.count() > 0 and el.is_visible():
                        el.click(timeout=2000)
                        page.wait_for_timeout(1000)
                        target_opt = page.locator(f"text=/{location_label}/i").last
                        if target_opt.count() > 0 and target_opt.is_visible():
                            captured.clear()  # Discard initial Utrecht payloads!
                            target_opt.click(timeout=2000)
                            loc_clicked = True
                            page.wait_for_timeout(2500)
                            break
                except Exception:
                    continue

        print(f"[Peakz] {venue_id} location selected={loc_clicked}, payloads={len(captured)}")
        if not loc_clicked:
            print(f"[Peakz] Warning: could not select {location_label}, skipping to avoid wrong club data")
            return []

        # Robust day picker loop (keep CSS and XPath selectors separate)
        for day_offset in range(1, DAYS_AHEAD):
            target_dt = now_nl + timedelta(days=day_offset)
            date_iso = target_dt.strftime("%Y-%m-%d")
            day_num = str(target_dt.day)

            day_selectors = [
                f"[data-date='{date_iso}']",
                f"[aria-label*='{date_iso}']",
                f"button:text-is('{day_num}')",
                f"td:text-is('{day_num}')",
                f"span:text-is('{day_num}')",
                f"[role='gridcell']:text-is('{day_num}')",
                f"[role='button']:text-is('{day_num}')",
                f".calendar-day:text-is('{day_num}')",
                f"xpath=//button[normalize-space(.)='{day_num}']",
                f"xpath=//*[self::button or self::a or self::td][normalize-space(text())='{day_num}']",
            ]
            for sel in day_selectors:
                try:
                    day_el = page.locator(sel).first
                    if day_el.count() > 0 and day_el.is_visible():
                        day_el.click(force=True, timeout=2000)
                        page.wait_for_timeout(1800)  # Wait for XHR availability request to fire
                        break
                except Exception:
                    continue

        # Parse FOYS availability payloads (rejecting Utrecht 'Pon Centre Court')
        def walk_foys(node, court_name="Indoor Baan", date_str=now_nl.strftime("%Y-%m-%d")):
            if isinstance(node, list):
                for item in node:
                    walk_foys(item, court_name, date_str)
                return
            if not isinstance(node, dict):
                return

            c_name = node.get("name") or node.get("courtName") or node.get("resourceName") or court_name
            if isinstance(c_name, str) and "pon centre" in c_name.lower():
                return  # Skip Utrecht default location

            start_val = node.get("startTime") or node.get("start_time") or node.get("start")
            is_avail = node.get("available", node.get("isAvailable", True))
            if isinstance(start_val, str) and is_avail is True:
                iso_m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", start_val)
                if iso_m:
                    d_s, t_s = iso_m.group(1), iso_m.group(2)
                    try:
                        slot_dt = datetime.strptime(f"{d_s} {t_s}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ_NL)
                        if slot_dt <= now_nl:
                            return
                    except Exception:
                        pass
                    dur = parse_duration(node.get("duration") or 60)
                    if 420 <= time_to_mins(t_s) <= 1380 and dur in (30, 60, 90, 120):
                        blocks.append({
                            "venueId": venue_id,
                            "date": d_s,
                            "time": t_s,
                            "duration": dur,
                            "courtName": str(c_name),
                            "price": parse_price(node.get("price") or node.get("amount")),
                        })

            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk_foys(v, c_name, date_str)

        for _, payload in captured:
            walk_foys(payload)

    except Exception as e:
        print(f"[Peakz] Error for {venue_id}: {e}")
    finally:
        page.close()

    print(f"[Peakz] {venue_id}: extracted {len(blocks)} valid court blocks")
    return blocks


def scrape_padellers_veldhoven(context):
    """
    Scrapes The Padellers Veldhoven on Bookaball by selecting Veldhoven
    and reading actual open slots (never treating occupied bookings as free).
    """
    blocks = []
    now_nl = datetime.now(TZ_NL)
    page = context.new_page()

    try:
        page.goto(
            "https://thepadellers.bookaball.com/nl/bookings/create",
            wait_until="networkidle",
            timeout=35000,
        )
        page.wait_for_timeout(2000)

        # Ensure Veldhoven is selected in club selector
        vhv_opt = page.locator("text=/Veldhoven/i").first
        if vhv_opt.count() > 0 and vhv_opt.is_visible():
            vhv_opt.click(timeout=2000)
            page.wait_for_timeout(2000)

        for day_offset in range(DAYS_AHEAD):
            target_dt = now_nl + timedelta(days=day_offset)
            date_str = target_dt.strftime("%Y-%m-%d")
            if day_offset > 0:
                day_num = str(target_dt.day)
                for sel in [
                    f"[data-date='{date_str}']",
                    f"button:text-is('{day_num}')",
                    f"xpath=//*[normalize-space(text())='{day_num}']",
                ]:
                    try:
                        btn = page.locator(sel).first
                        if btn.count() > 0 and btn.is_visible():
                            btn.click(timeout=1500)
                            page.wait_for_timeout(1500)
                            break
                    except Exception:
                        continue

            # Read clickable free time-slot buttons from the rendered Bookaball grid
            free_slots = page.evaluate("""() => {
                const results = [];
                const candidates = document.querySelectorAll('button, [role="button"], .slot, [data-time]');
                for (const el of candidates) {
                    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                    const cls = (el.className || '').toString().toLowerCase();
                    if (cls.includes('disabled') || cls.includes('booked') || cls.includes('occupied')) continue;
                    const txt = (el.innerText || el.getAttribute('data-time') || '').trim();
                    const m = txt.match(/^([01]\\d|2[0-3]):(00|30)$/);
                    if (m) {
                        results.push({ time: m[0], court: el.getAttribute('data-court') || 'Baan' });
                    }
                }
                return results;
            }""")

            # Count occurrences of each open start time across courts on the grid
            counts_by_time = defaultdict(int)
            for item in free_slots:
                counts_by_time[item["time"]] += 1

            for t_str, count in counts_by_time.items():
                for idx in range(min(count, 6)):
                    blocks.append({
                        "venueId": "padellers_vhv",
                        "date": date_str,
                        "time": t_str,
                        "duration": 60,
                        "courtName": f"Indoor Baan {idx + 1}",
                        "price": 32.0 if 17 <= int(t_str[:2]) <= 21 else 26.0,
                    })

    except Exception as e:
        print(f"[Padellers] Error: {e}")
    finally:
        page.close()

    print(f"[Padellers] Extracted {len(blocks)} open court blocks")
    return blocks


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
        }

        for start_m, (first_dur, first_price) in start_map.items():
            # If a platform gives a 90m or 120m contiguous block, shorter durations also fit
            if first_dur in (60, 90, 120):
                for target_dur in (60, 90, 120):
                    if target_dur <= first_dur:
                        key = (venue_id, date_str, mins_to_time(start_m), target_dur)
                        aggregated[key]["courts"].add(court_name)
                        if first_price > 0:
                            aggregated[key]["prices"].append(round(first_price * (target_dur / first_dur), 2))
            elif first_dur == 30:
                for target_dur in (60, 90, 120):
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
        avg_price = (
            round(sum(info["prices"]) / len(info["prices"]), 2)
            if info["prices"]
            else round(30.0 * (dur / 60), 2)
        )
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
    all_blocks = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            locale="nl-NL",
            timezone_id="Europe/Amsterdam",
        )
        all_blocks.extend(scrape_padellers_veldhoven(context))
        all_blocks.extend(scrape_peakz_location(context, "peakz_beurs", "Beursgebouw", 8))
        all_blocks.extend(scrape_peakz_location(context, "peakz_vijfkamp", "Vijfkamplaan", 7))
        browser.close()

    slots_map = build_slot_map(all_blocks)
    club_status = {
        vid: ("live" if any(k.startswith(f"{vid}_") for k in slots_map) else "portal_only")
        for vid in VENUE_MAX_COURTS
    }
    for vid, st in club_status.items():
        count = sum(1 for k in slots_map if k.startswith(f"{vid}_"))
        print(f"[Summary] {vid}: {count} slots ({st})")

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