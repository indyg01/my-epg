#!/usr/bin/env python3
import re
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

M3U_URL = os.environ.get("M3U_URL", "")
OUTPUT_FILE = "epg.xml"

# set this to -8 for PST or -7 for PDT
LOCAL_OFFSET_HOURS = -8

DEFAULT_DURATION_MIN = 120
ENDED_DURATION_MIN = 180


MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
}

# Example supported formats:
# "Serie A: Cagliari vs Napoli @ Mar 20 3:20 PM :Paramount+ 01"
# "English Football League: Preston North End vs Stoke City @ Mar 20 5:50 PM :Paramount+ 04"
RX_TITLE_TIME = re.compile(
    r"^(.*?)\s*@\s*([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE
)

RX_SLOT = re.compile(r":\s*Paramount\+\s*(\d{1,3})\s*$", re.IGNORECASE)


def fetch_m3u(url: str) -> str:
    if not url:
        raise ValueError("Set M3U_URL first.")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def parse_m3u(content: str) -> list[dict]:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    channels = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF"):
            group = ""
            tvg_name = ""

            m = re.search(r'group-title="([^"]*)"', line, re.IGNORECASE)
            if m:
                group = m.group(1).strip()

            m = re.search(r'tvg-name="([^"]*)"', line, re.IGNORECASE)
            if m:
                tvg_name = m.group(1).strip()

            if not tvg_name and "," in line:
                tvg_name = line.split(",", 1)[1].strip()

            url = ""
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                url = lines[i + 1].strip()
                i += 1

            channels.append({
                "group": group,
                "name": tvg_name,
                "url": url,
            })
        i += 1

    return channels


def parse_paramount_name(name: str) -> dict | None:
    slot_m = RX_SLOT.search(name)
    if not slot_m:
        return None

    slot_num = int(slot_m.group(1))
    ch_id = f"paramount.{slot_num:03d}"

    time_m = RX_TITLE_TIME.search(name)
    if not time_m:
        return None

    raw_title = time_m.group(1).strip()
    mon = time_m.group(2).lower()
    day = int(time_m.group(3))
    hour = int(time_m.group(4))
    minute = int(time_m.group(5))
    ampm = time_m.group(6).upper()

    month = MONTHS.get(mon)
    if not month:
        return None

    if ampm == "PM" and hour != 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0

    now = datetime.now()
    year = now.year

    # Assume provider time is Eastern Time.
    # March in your examples is usually EDT = UTC-4.
    # If you need EST dates too, we can add smarter switching later.
    eastern_offset = timezone(timedelta(hours=-4))
    local_offset = timezone(timedelta(hours=LOCAL_OFFSET_HOURS))

    start_et = datetime(year, month, day, hour, minute, tzinfo=eastern_offset)

    # rollover for next year if needed
    if start_et < datetime.now(eastern_offset) - timedelta(days=30):
        start_et = datetime(year + 1, month, day, hour, minute, tzinfo=eastern_offset)

    start_local = start_et.astimezone(local_offset)
    start_utc = start_local.astimezone(timezone.utc)

    return {
        "ch_id": ch_id,
        "display_name": f"Paramount+ {slot_num:03d}",
        "title": raw_title,
        "start_utc": start_utc,
        "duration_min": DEFAULT_DURATION_MIN,
    }


def xmltv_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def build_xml(events: list[dict]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv>'
    ]

    seen = set()
    for ev in events:
        if ev["ch_id"] in seen:
            continue
        seen.add(ev["ch_id"])
        lines.append(f'  <channel id="{escape(ev["ch_id"])}">')
        lines.append(f'    <display-name>{escape(ev["display_name"])}</display-name>')
        lines.append(f'  </channel>')

    for ev in events:
        start = ev["start_utc"]
        stop = start + timedelta(minutes=ev["duration_min"])
        ended_stop = stop + timedelta(minutes=ENDED_DURATION_MIN)

        lines.append(
            f'  <programme start="{xmltv_time(start)}" stop="{xmltv_time(stop)}" channel="{escape(ev["ch_id"])}">'
        )
        lines.append(f'    <title>{escape(ev["title"])}</title>')
        lines.append(f'    <desc>{escape(ev["title"])} — Paramount+</desc>')
        lines.append(f'    <category>Sports</category>')
        lines.append(f'  </programme>')

        lines.append(
            f'  <programme start="{xmltv_time(stop)}" stop="{xmltv_time(ended_stop)}" channel="{escape(ev["ch_id"])}">'
        )
        lines.append(f'    <title>Event ended</title>')
        lines.append(f'    <desc>{escape(ev["title"])} has finished.</desc>')
        lines.append(f'    <category>Sports</category>')
        lines.append(f'  </programme>')

    lines.append('</tv>')
    return "\n".join(lines) + "\n"


def main():
    content = fetch_m3u(M3U_URL)
    channels = parse_m3u(content)

    paramount = [c for c in channels if c["group"].strip().lower() == "paramount+"]
    print(f"Found {len(paramount)} PARAMOUNT+ channels")

    events = []
    for ch in paramount:
        parsed = parse_paramount_name(ch["name"])
        if not parsed:
            print(f"SKIP: {ch['name']}")
            continue
        events.append(parsed)

    print(f"Parsed {len(events)} events")

    xml = build_xml(events)
    with open(OUTPUT_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(xml)

    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
