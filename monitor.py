import os
import json
import requests
import feedparser
from google import genai
from google.genai import types

FEEDS = {
    "Met Office NW": "https://www.metoffice.gov.uk/public/data/PWSCache/WarningsRSS/Region/nw",
    "Sellafield GOV.UK": "https://www.gov.uk/government/collections/sellafield-ltd-incident-reports-and-notices.atom",
    "ONR Nuclear Reg": "https://www.onr.org.uk/news/rss.xml",
    "NCSC Cyber Alerts": "https://www.ncsc.gov.uk/api/1/services/v1/all-rss-feed.xml",
    "ProtectUK Security": "https://www.protectuk.police.uk/rss.xml"
}

STATE_FILE = "seen_alerts.json"

def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen(seen_set):
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen_set)[-500:], f, indent=2)

def get_telemetry_context():
    url = "https://api.open-meteo.com/v1/forecast?latitude=54.42,54.66&longitude=-3.50,-3.41&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m,surface_pressure&wind_speed_unit=mph"
    context = []
    try:
        data = requests.get(url, timeout=10).json()
        sf = data[0].get("current", {})
        ck = data[1].get("current", {})
        speed = sf.get("wind_speed_10m", 0)
        direction = sf.get("wind_direction_10m", 0)
        eta_mins = round((17.5 / speed) * 60) if speed > 1 else 999
        downwind = "YES - BLOWING TOWARD CA13 0ZQ" if 165 <= direction <= 220 else "No (blowing away from CA13 0ZQ)"
        context.append(
            f"Sellafield Wind: {speed} mph from {direction}° | Downwind to CA13 0ZQ: {downwind} | "
            f"17.5-mile Plume ETA: ~{eta_mins} mins. "
            f"CA13 0ZQ Gusts: {ck.get('wind_gusts_10m', 0)} mph, Pressure: {ck.get('surface_pressure', 1013)} hPa."
        )
    except Exception as e:
        context.append(f"Weather telemetry error: {e}")

    stations = {"Derwent (Kingfisher)": "752020", "Derwent (Ouse Bridge)": "751110", "River Cocker": "751690"}
    levels = []
    for name, st_id in stations.items():
        try:
            r = requests.get(f"https://environment.data.gov.uk/flood-monitoring/id/stations/{st_id}/measures", timeout=8).json()
            for m in r.get("items", []):
                val = m.get("latestReading", {}).get("value")
                if val is not None:
                    levels.append(f"{name}: {val}m")
        except Exception:
            pass
    if levels:
        context.append("Cockermouth Rivers: " + " | ".join(levels))
    return "\n".join(context)

def get_enwl_power_cuts():
    url = "https://electricitynorthwest.opendatasoft.com/api/explore/v2.1/catalog/datasets/sp-enw-live-neop/records?limit=50"
    alerts = []
    try:
        r = requests.get(url, timeout=10).json()
        for rec in r.get("results", []):
            pc = str(rec.get("postcode", ""))
            if any(sector in pc for sector in ["CA13", "CA14", "CA15"]):
                fid = rec.get("fault_id", "unknown")
                status = rec.get("status", "Active")
                volt = rec.get("voltagelevel", "LV")
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
        r = requests.get(url, timeout=10).json()
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
    requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), headers=headers, timeout=10)

def main():
    seen = load_seen()
    first_run = len(seen) == 0
    new_entries = []

    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                uid = entry.get("id") or entry.get("link") or entry.get("title")
                if uid and uid not in seen:
                    new_entries.append((uid, f"[{source}] {entry.get('title', '')} - {entry.get('summary', '')[:350]}"))
        except Exception:
            pass

    for uid, text in get_cumbria_floods() + get_enwl_power_cuts():
        if uid not in seen:
            new_entries.append((uid, text))

    telemetry = get_telemetry_context()
    ntfy_topic = os.environ["NTFY_TOPIC"]

    if first_run:
        send_ntfy(
            ntfy_topic,
            "CA13 0ZQ EWS Online",
            f"Your West Cumbria Early Warning System is active!\n\n{telemetry}",
            priority="urgent"
        )

    if not new_entries:
        return

    with open("prompt.txt", "r") as f:
        system_prompt = f.read()

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    for uid, alert_text in new_entries:
        seen.add(uid)
        payload = f"LIVE LOCAL TELEMETRY:\n{telemetry}\n\nINCOMING EVENT:\n{alert_text}"
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
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
                    result.get("title", "CA13 0ZQ Alert"),
                    result.get("message", alert_text),
                    result.get("priority", "default")
                )
        except Exception as e:
            print(f"Error analyzing {uid}: {e}")

    save_seen(seen)

if __name__ == "__main__":
    main()
          
