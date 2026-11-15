import json
from datetime import datetime, timedelta, timezone
import requests
from playwright.sync_api import sync_playwright

DAYS_AHEAD = 7
VHV_COORDS = (51.4192, 5.4050)  # Veldhoven center

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}


def fetch_playtomic_nearby():
    """Queries Playtomic API for indoor padel clubs within 10 km of Veldhoven."""
    slots = []
    clubs = []
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
        print(f"Playtomic tenants lookup failed: {e}")
        return clubs, slots

    today = datetime.now(timezone.utc).date()

    for t in tenants:
        tid = t.get("tenant_id")
        name = t.get("tenant_name", "Playtomic Club")
        resources = {
            res["resource_id"]: res
            for res in t.get("resources", [])
            if res.get("sport_id") == "PADEL"
            and res.get("properties", {}).get("resource_type") == "indoor"
        }
        if not resources:
            continue

        clubs.append({
            "id": f"playtomic-{tid}",
            "name": name,
            "platform": "Playtomic",
            "bookingUrl": f"https://playtomic.io/tenant/{tid}",
        })

        for day_offset in range(DAYS_AHEAD):
            target_date = today + timedelta(days=day_offset)
            start_min = f"{target_date}T07:00:00"
            start_max = f"{target_date}T23:30:00"
            try:
                av = requests.get(
                    "https://api.app.playtomic.io/v1/availability",
                    params={
                        "tenant_id": tid,
                        "sport_id": "PADEL",
                        "local_start_min": start_min,
                        "local_start_max": start_max,
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
                    court_name = resources[res_id].get("name", "Indoor Court")
                    for s in court.get("slots", []):
                        slots.append({
                            "clubId": f"playtomic-{tid}",
                            "clubName": name,
                            "courtName": court_name,
                            "date": str(target_date),
                            "time": s.get("start_time", "")[:5],
                            "duration": s.get("duration", 60),
                            "price": s.get("price", ""),
                            "indoor": True,
                            "bookingUrl": f"https://playtomic.io/tenant/{tid}",
                        })
            except Exception as e:
                print(f"Playtomic availability error for {name} on {target_date}: {e}")

    return clubs, slots


def capture_widget_json(url, club_id, club_name, platform):
    """Loads FOYS / Bookaball booking widgets headlessly and intercepts JSON responses."""
    captured = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            def handle_response(response):
                if "json" in response.headers.get("content-type", ""):
                    try:
                        data = response.json()
                        captured.append({"url": response.url, "data": data})
                    except Exception:
                        pass

            page.on("response", handle_response)
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
            browser.close()
    except Exception as e:
        print(f"Widget capture warning for {club_name}: {e}")

    return {
        "id": club_id,
        "name": club_name,
        "platform": platform,
        "bookingUrl": url,
        "rawResponsesCount": len(captured),
    }


def main():
    pt_clubs, pt_slots = fetch_playtomic_nearby()

    widget_clubs = [
        capture_widget_json(
            "https://www.peakzpadel.nl/reserveren/court-booking/reservation",
            "peakz-ehv",
            "Peakz Padel Eindhoven (Vijfkamplaan / Beursgebouw)",
            "FOYS",
        ),
        capture_widget_json(
            "https://thepadellers.bookaball.com/",
            "padellers-vhv",
            "The Padellers Veldhoven",
            "Bookaball",
        ),
    ]

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "clubs": pt_clubs + widget_clubs,
        "slots": pt_slots,
    }

    with open("availability.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(pt_slots)} slots across {len(output['clubs'])} clubs.")


if __name__ == "__main__":
    main()
