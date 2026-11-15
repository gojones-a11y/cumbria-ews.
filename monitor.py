import os
import json
import time
from datetime import datetime, timezone
import requests
import feedparser
from google import genai
from google.genai import types

FEEDS = {
    "Met Office NW": "https://www.metoffice.gov.uk/public/data/PWSCache/WarningsRSS/Region/nw",
    "Sellafield GOV.UK": "https://www.gov.uk/government/collections/sellafield-ltd-incident-reports-and-notices.atom",
    "ONR Nuclear Reg": "https://www.onr.org.uk/news/rss.xml",
    "NCSC Cyber Alerts": "https://www.ncsc.gov.uk/api/1/services/v1/all-rss-feed.xml",
    "ProtectUK Terror/Sec": "https://www.protectuk.police.uk/rss.xml",
    "UKHSA Health/Bio": "https://www.gov.uk/government/organisations/uk-health-security-agency.atom",
    "DEFRA Animal/Bio": "https://www.gov.uk/government/organisations/department-for-environment-food-rural-affairs.atom",
    "Cabinet Office Crisis": "https://www.gov.uk/government/organisations/cabinet-office.atom",
    "DESNZ Energy Security": "https://www.gov.uk/government/organisations/department-for-energy-security-and-net-zero.atom",
    "BGS UK Earthquakes": "http://www.earthquakes.bgs.ac.uk/feeds/MhSeismology.xml"
}

STATE_FILE = "seen_alerts.json"

def fetch_json(url, timeout=20, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "Cumbria-EWS/2.1"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                raise e
            time.sleep(2)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {"seen": data, "recent_log": [], "last_heartbeat": ""}
                return data
        except Exception:
            pass
    return {"seen": [], "recent_log": [], "last_heartbeat": ""}

def save_state(state):
    state["seen"] = state["seen"][-600:]
    state["recent_log"] = state["recent_log"][-25:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_telemetry_and_tripwires():
    context = []
    tripwires = []

    # 1. Sellafield Wind & Cockermouth Weather (with automatic retry)
    url = "https://api.open-meteo.com/v1/forecast?latitude=54.42,54.66&longitude=-3.50,-3.41&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m,surface_pressure&wind_speed_unit=mph"
    try:
        data = fetch_json(url, timeout=20, retries=3)
        sf = data[0].get("current", {})
        ck = data[1].get("current", {})
        speed = sf.get("wind_speed_10m", 0)
        direction = sf.get("wind_direction_10m", 0)
        gusts = ck.get("wind_gusts_10m", 0)
        pressure = ck.get("surface_pressure", 1013)
        eta_mins = round((17.5 / speed) * 60) if speed > 1 else 999
        if 165 <= direction <= 220:
            wind_status = f"BLOWING TOWARD COCKERMOUTH ({speed} mph, ~{eta_mins}m travel time)"
        else:
            wind_status = f"Blowing away from Cockermouth ({speed} mph)"
        context.append(f"Sellafield Wind: {wind_status}\nLocal Weather: Gusts {gusts} mph, Pressure {pressure} hPa")
        if gusts >= 50:
            tripwires.append((f"TRIP-GUST-{datetime.now(timezone.utc).strftime('%Y%m%d%H')}", f"[TELEMETRY TRIPWIRE] Severe wind gusts at CA13 0ZQ: {gusts} mph"))
        if pressure < 970:
            tripwires.append((f"TRIP-PRESS-{datetime.now(timezone.utc).strftime('%Y%m%d%H')}", f"[TELEMETRY TRIPWIRE] Deep low pressure at CA13 0ZQ: {pressure} hPa"))
    except Exception:
        context.append("Weather sensor: Temporarily busy (will re-check in 15 mins)")

    # 2. Cockermouth River Gauges & Hard Flood Tripwires
    stations = {
        "Derwent (Kingfisher)": ("752020", 1.80),
        "Derwent (Ouse Bridge)": ("751110", 2.10),
        "River Cocker": ("751690", 1.80)
    }
    levels = []
    for name, (st_id, threshold) in stations.items():
        try:
            r = fetch_json(f"https://environment.data.gov.uk/flood-monitoring/id/stations/{st_id}/measures", timeout=15, retries=2)
            for m in r.get("items", []):
                val = m.get("latestReading", {}).get("value")
                if val is not None:
                    status = "HIGH" if val >= threshold else "Normal"
                    levels.append(f"{name}: {val:.2f}m ({status})")
                    if val >= threshold:
                        tripwires.append((f"TRIP-RIVER-{st_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H')}", f"[RIVER SURGE TRIPWIRE] {name} has reached {val}m (Flood threshold: {threshold}m)"))
        except Exception:
            pass
    if levels:
        context.append("Rivers: " + " | ".join(levels))

    # 3. NOAA Space Weather / Solar Storm Kp-Index
    try:
        kp_data = fetch_json("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json", timeout=15, retries=2)
        latest_kp = float(kp_data[-1][1])
        kp_status = "STORM WARNING" if latest_kp >= 7.0 else "Calm"
        context.append(f"Space Weather: {kp_status} (Kp {latest_kp})")
        if latest_kp >= 7.0:
            tripwires.append((f"TRIP-KP-{datetime.now(timezone.utc).strftime('%Y%m%d')}", f"[SPACE WEATHER TRIPWIRE] Severe Geomagnetic Storm in progress (Kp {latest_kp}). Risk to rural grid and GPS."))
    except Exception:
        pass

    return "\n".join(context), tripwires

def get_enwl_power_cuts():
    url = "https://electricitynorthwest.opendatasoft.com/api/explore/v2.1/catalog/datasets/sp-enw-live-neop/records?limit=50"
    alerts = []
    try:
        r = fetch_json(url, timeout=15, retries=2)
        for rec in r.get("results", []):
            pc = str(rec.get("postcode", ""))
            volt = str(rec.get("voltagelevel", "LV"))
            if any(sector in pc for sector in ["CA13", "CA14", "CA15"]) or "EHV" in volt or "132" in volt:
                fid = rec.get("fault_id", "unknown")
                status = rec.get("status", "Active")
                off = rec.get("confirmedoff", 0)
                etr = rec.get("etr", "Unknown")
                uid = f"ENWL-{fid}-{status}"
                alerts.append((uid, f"[ENWL Power Cut] Postcode: {pc} | Voltage: {volt} | Customers off: {off} | Status: {status} | Est Restore: {etr}"))
    except Exception:
        pass
    return alerts

def get_cumbria_floods():
    url = "https://environment.data.gov.uk/flood-monitoring/id/floods?county=Cumbria"
    alerts = []
    try:
        r = fetch_json(url, timeout=15, retries=2)
        for item in r.get("items", []):
            sev = item.get("severityLevel", 4)
            if sev <= 3:
                uid = f"EA-{item.get('floodAreaID')}-{sev}"
                alerts.append((uid, f"[EA Flood Severity {sev}] {item.get('description', '')}: {item.get('message', '')[:350]}"))
    except Exception:
        pass
    return alerts

def send_ntfy(topic, title, message, priority="default"):
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": "warning,uk"}
    requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), headers=headers, timeout=15)

def main():
    state = load_state()
    seen = set(state["seen"])
    new_entries = []

    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:4]:
                uid = entry.get("id") or entry.get("link") or entry.get("title")
                if uid and uid not in seen:
                    new_entries.append((uid, f"[{source}] {entry.get('title', '')} - {entry.get('summary', '')[:300]}"))
        except Exception:
            pass

    telemetry, tripwires = get_telemetry_and_tripwires()

    for uid, text in get_cumbria_floods() + get_enwl_power_cuts() + tripwires:
        if uid not in seen:
            new_entries.append((uid, text))

    ntfy_topic = os.environ["NTFY_TOPIC"]
    now_utc = datetime.now(timezone.utc)
    week_id = now_utc.strftime("%Y-W%W")

    # Sunday Morning Weekly Heartbeat (After 08:00 UTC on Sundays)
    if now_utc.weekday() == 6 and now_utc.hour >= 8 and state.get("last_heartbeat") != week_id:
        send_ntfy(
            ntfy_topic,
            "CA13 0ZQ Weekly EWS Check-In",
            f"All UK feeds and Cockermouth sensors are active and watching.\n\n{telemetry}",
            priority="default"
        )
        state["last_heartbeat"] = week_id

    if not new_entries:
        state["seen"] = list(seen)
        save_state(state)
        return

    with open("prompt.txt", "r") as f:
        system_prompt = f.read()

    batch_text = "\n".join([text for _, text in new_entries])
    recent_context = "\n".join(state.get("recent_log", [])[-10:]) or "None"

    for uid, text in new_entries:
        seen.add(uid)
        state["recent_log"].append(text[:200])

    payload = (
        f"LIVE LOCAL TELEMETRY:\n{telemetry}\n\n"
        f"RECENT 24H CONTEXT:\n{recent_context}\n\n"
        f"NEW INCOMING EVENTS TO ANALYZE:\n{batch_text}"
    )

    try:
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=payload,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
            ),
        )
        result = json.loads(response.text)
        if result.get("should_alert"):
            send_ntfy(
                ntfy_topic,
                result.get("title", "CA13 0ZQ Threat Alert"),
                result.get("message", batch_text),
                result.get("priority", "default")
            )
    except Exception as e:
        print(f"Compound analysis error: {e}")

    state["seen"] = list(seen)
    save_state(state)

if __name__ == "__main__":
    main()
