#!/usr/bin/env python3
"""
EPG Generator
=============
Downloads an M3U playlist, filters by categories, parses event names/dates/times
from channel names (handling multiple provider formats), and writes a timed XMLTV EPG.xml.
Each event gets a proper start/stop block plus a follow-up "Event ended" block.
Runs unattended via GitHub Actions.

Supported channel name formats
-------------------------------
  UEFA Conference League: AEK Larnaca vs Crystal Palace @ Mar 19 1:00 PM :Paramount+  01
  US: ESPN+ PPV 1 - SEATTLE KRAKEN VS. NASHVILLE PREDATORS | Fri 20 Mar 01:00 | 8K EXCLUSIVE
  NHL | 03 - 8:30pm Avalanche @ Blackhawks
  ATP 1000 Tennis Miami Day #2 Court 7 @ Mar 19 10:00 AM :TSN+  05
  MLS | Event 1 San Jose Earthquakes vs. Seattle Sounders FC // UK Sun 15 Mar 10:45pm // ET Sun 15 Mar 6:45pm
"""

import re
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

# ── Configuration ─────────────────────────────────────────────────────────────

M3U_URL              = os.environ.get("M3U_URL", "")
EPG_CATEGORIES_RAW   = os.environ.get("EPG_CATEGORIES", "paramount+")
TZ_OFFSET_HOURS      = float(os.environ.get("TZ_OFFSET_HOURS", "-7"))   # PDT = -7, PST = -8
DEFAULT_DURATION_MIN = int(os.environ.get("DEFAULT_DURATION_MIN", "120"))
ENDED_DURATION_MIN   = int(os.environ.get("ENDED_DURATION_MIN", "180"))
ENDED_MESSAGE        = os.environ.get("ENDED_MESSAGE", "Event ended")
OUTPUT_FILE          = os.environ.get("EPG_OUTPUT", "EPG.xml")

# ── Constants ─────────────────────────────────────────────────────────────────

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5,  "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Named timezone offsets used by providers in channel names
# PDT/ET/UK blocks — we prefer PDT, fall back to ET, then UK
TZ_BLOCK_OFFSETS = {
    "pt":   -7,  "pdt":  -7,  "pst":  -8,
    "mt":   -6,  "mdt":  -6,  "mst":  -7,
    "ct":   -5,  "cdt":  -5,  "cst":  -6,
    "et":   -4,  "edt":  -4,  "est":  -5,
    "gmt":   0,  "utc":   0,
    "bst":   1,  "uk":    1,   "gb":   1,
    "cet":   1,  "cest":  2,
}

# Priority order: prefer the timezone closest to the user's own
# PDT first, then other North American zones, then UK/EU last
TZ_PRIORITY = ["pdt", "pt", "pst", "mdt", "mt", "mst", "cdt", "ct", "cst",
               "edt", "et", "est", "utc", "gmt", "bst", "uk", "gb", "cet", "cest"]

# Broadcasters — detected when appearing after : or | at end of name
BROADCASTERS = [
    "paramount+", "paramount plus",
    "tsn+", "tsn", "sportsnet", "sportsnet+",
    "espn+", "espn",
    "sky sports", "bein sports", "bein",
    "tnt sports", "tnt",
    "dazn", "fs1", "fs2",
    "fox sports", "nbc sports", "cbs sports",
    "peacock", "hbo max", "max",
    "apple tv+", "apple tv",
    "fubo", "sling", "hulu",
    "amazon", "prime video",
    "discovery+", "eurosport",
    "bt sport", "canal+", "digi sport",
    "rds", "tvA sports",
]

# Regex: broadcaster at end of name after : or |
BROADCASTER_RE = re.compile(
    r'[:\|]\s*(' + '|'.join(re.escape(b) for b in sorted(BROADCASTERS, key=len, reverse=True)) + r')\s*$',
    re.IGNORECASE,
)

# Regex: country/region prefix at start — "US:", "UK:", "CA:", etc.
COUNTRY_PREFIX_RE = re.compile(r'^\s*[A-Z]{2,3}:\s*', re.IGNORECASE)

# Regex: league prefix before a pipe — "NHL |", "MLS |", "ATP |" etc.
LEAGUE_PREFIX_RE = re.compile(r'^([A-Z0-9 +]{2,20})\s*\|\s*', re.IGNORECASE)

# Regex: quality/resolution tags
QUALITY_RE = re.compile(
    r'\b(8K|4K|FHD|HD|SD|UHD|EXCLUSIVE|PPV(?!\s*\d))\b', re.IGNORECASE
)

# Regex: trailing channel/stream number (1-2 digits, possibly with leading spaces)
TRAILING_NUM_RE = re.compile(r'\s+\d{1,2}$')

# Regex: time — "8:30pm", "1:00 PM", "13:00", "@10:00", "22h30"
# @ only treated as time prefix when directly followed by digits (not team names like "Avalanche @ Blackhawks")
TIME_RE = re.compile(
    r'(?:(?:@\s*)(?=\d)|(?<![A-Za-z@]))(\d{1,2})(?:h|:|\.)(\d{2})\s*(am|pm)?(?!\d)', re.IGNORECASE
)

# Regex: day-of-week prefix before a date — "Fri", "Sun", etc.
DOW_RE = re.compile(
    r'\b(mon|tue|wed|thu|fri|sat|sun)\b', re.IGNORECASE
)

# Regex: // TZ Day Date Time blocks used by some providers
# e.g. "// ET Sun 15 Mar 6:45pm" or "// UK Sun 15 Mar 10:45pm"
TZ_BLOCK_RE = re.compile(
    r'//\s*([A-Z]{2,4})\s+(?:mon|tue|wed|thu|fri|sat|sun)?\s*'
    r'(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{1,2}[:\.]?\d{2}\s*(?:am|pm)?)',
    re.IGNORECASE,
)

# Regex: pipe-delimited schedule blocks "| Fri 20 Mar 01:00 | 8K EXCLUSIVE"
PIPE_SCHEDULE_RE = re.compile(
    r'\s*\|\s*(?:mon|tue|wed|thu|fri|sat|sun)?\s*\d{0,2}\s*[A-Za-z]{0,9}\s*\d{0,2}[:\.]?\d{0,2}\s*(?:am|pm)?\s*\|.*$',
    re.IGNORECASE,
)

# ── M3U: fetch and parse ──────────────────────────────────────────────────────

def fetch_m3u(url: str) -> str:
    if not url:
        raise ValueError(
            "M3U_URL is not set.\n"
            "Repo → Settings → Secrets → Actions → New secret\n"
            "  Name : M3U_URL\n"
            "  Value: your full playlist URL"
        )
    print(f"  Fetching playlist...")
    req = urllib.request.Request(url, headers={"User-Agent": "EPGGenerator/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    print(f"  Downloaded {len(data):,} bytes.")
    return data


def parse_m3u(content: str) -> list[dict]:
    channels, lines, i = [], content.splitlines(), 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            info = {"name": "", "group": "", "tvg_id": "", "url": ""}
            for pattern, key in [
                (r'tvg-id="([^"]*)"',      "tvg_id"),
                (r'group-title="([^"]*)"', "group"),
                (r'tvg-name="([^"]*)"',    "name"),
            ]:
                m = re.search(pattern, line, re.IGNORECASE)
                if m:
                    info[key] = m.group(1)
            m = re.search(r',(.+)$', line)
            if m:
                info["name"] = m.group(1).strip()
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i < len(lines) and not lines[i].startswith("#"):
                info["url"] = lines[i].strip()
            channels.append(info)
        i += 1
    print(f"  Parsed {len(channels)} total channels.")
    return channels


def filter_by_categories(channels: list[dict], categories: list[str]) -> list[dict]:
    results = []
    for ch in channels:
        for cat in categories:
            if cat in ch["group"].lower() or cat in ch["name"].lower():
                ch = dict(ch)
                ch["matched_category"] = cat
                results.append(ch)
                break
    return results


# ── Time / date helpers ───────────────────────────────────────────────────────

def parse_time_str(time_str: str) -> tuple[int, int] | None:
    """Parse a time string like '8:30pm', '13:00', '10:00 AM' into (hour, minute)."""
    m = TIME_RE.search(time_str)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    mer = (m.group(3) or "").lower()
    if mer == "pm" and hour < 12:
        hour += 12
    if mer == "am" and hour == 12:
        hour = 0
    return (hour, minute)


def parse_date_str(text: str) -> tuple[int, int, int] | None:
    """Try to extract (year, month, day) from a text fragment.
    Tries named-month patterns first to avoid time digits being mistaken for dates."""

    # yyyy-mm-dd
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', text)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # "Mar 19 2026", "19 Mar 2026", "Fri 20 Mar 01:00" (with optional DOW prefix)
    # Strip day-of-week first so "Fri 20 Mar" becomes "20 Mar"
    # Use explicit month name list so "Court", "Day", "Event" etc. never match
    MONTH_NAMES_RE = re.compile(
        r'\b(january|february|march|april|may|june|july|august|september|october|november|december'
        r'|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b',
        re.IGNORECASE
    )
    text_nodow = re.sub(r'\b(?:mon|tue|wed|thu|fri|sat|sun)\b\s*', '', text, flags=re.IGNORECASE)
    # month-name first: "Mar 19" — but reject if the digit is immediately followed by : (it's a time)
    m = re.search(
        r'\b(january|february|march|april|may|june|july|august|september|october|november|december'
        r'|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})(?!\s*:)(?:\s+(\d{4}))?\b',
        text_nodow, re.IGNORECASE
    )
    if m:
        mo = MONTHS.get(m.group(1).lower()[:3])
        if mo:
            y = int(m.group(3)) if m.group(3) else datetime.now().year
            d = int(m.group(2))
            if 1 <= d <= 31:
                return (y, mo, d)
    # digit-first: "19 Mar"
    m = re.search(
        r'\b(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december'
        r'|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)(?:\s+(\d{4}))?\b',
        text_nodow, re.IGNORECASE
    )
    if m:
        mo = MONTHS.get(m.group(2).lower()[:3])
        if mo:
            y = int(m.group(3)) if m.group(3) else datetime.now().year
            d = int(m.group(1))
            if 1 <= d <= 31:
                return (y, mo, d)

    # dd/mm/yyyy or dd/mm (numeric only — lowest priority, skip if looks like a time)
    # Require a slash separator (not dash, to avoid matching time like 20 Mar 01:00 as 20-01)
    m = re.search(r'(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?', text)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else datetime.now().year
        if y < 100: y += 2000
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return (y, mo, d)

    return None


# ── Multi-format name parser ──────────────────────────────────────────────────

def extract_tz_blocks(raw: str) -> tuple[str, tuple | None, float | None]:
    """
    Handle the // TZ Day Date Time // TZ Day Date Time format.
    Returns (cleaned_name, date_tuple, tz_offset_hours) using the preferred timezone.

    Preference order: PDT > other PT > MT > CT > ET > UTC/GMT > UK/BST > CET
    """
    blocks = TZ_BLOCK_RE.findall(raw)
    if not blocks:
        return raw, None, None

    # Remove all // blocks from the name
    clean = re.sub(r'\s*//.*$', '', raw, flags=re.DOTALL).strip()

    # Score each block by timezone preference
    best_score = 999
    best_date  = None
    best_offset = None

    for tz_label, day_str, month_str, time_str in blocks:
        tz_key = tz_label.lower()
        score  = TZ_PRIORITY.index(tz_key) if tz_key in TZ_PRIORITY else 999
        offset = TZ_BLOCK_OFFSETS.get(tz_key)

        if offset is None:
            continue

        mo = MONTHS.get(month_str.lower()[:3])
        if not mo:
            continue

        y = datetime.now().year
        try:
            date_tuple = (y, mo, int(day_str))
        except ValueError:
            continue

        if score < best_score:
            best_score  = score
            best_date   = date_tuple
            best_offset = offset

    return clean, best_date, best_offset


def extract_pipe_schedule(raw: str) -> tuple[str, str]:
    """
    Handle pipe-delimited schedule blocks:
      "US: ESPN+ PPV 1 - NAME | Fri 20 Mar 01:00 | 8K EXCLUSIVE"
    Returns (event_name_part, schedule_part).
    Splits on the first pipe whose content looks like schedule info
    (contains a time, day-of-week, or recognisable date).
    Pipes that are purely quality/label noise are also consumed but not returned.
    """
    parts = re.split(r'\s*\|\s*', raw)
    if len(parts) <= 1:
        return raw, ""

    # Find the first pipe segment that contains schedule info
    first_schedule_idx = None
    for i, p in enumerate(parts[1:], start=1):
        if TIME_RE.search(p) or DOW_RE.search(p) or parse_date_str(p):
            first_schedule_idx = i
            break

    if first_schedule_idx is None:
        # No schedule info found in any pipe segment — name is everything before last pipe
        return parts[0].strip(), ""

    event_part = parts[0].strip()
    schedule_parts = []
    for p in parts[first_schedule_idx:]:
        if TIME_RE.search(p) or DOW_RE.search(p) or parse_date_str(p):
            schedule_parts.append(p.strip())

    return event_part, " ".join(schedule_parts)


def clean_event_name(name: str) -> str:
    """
    Clean up an event name after date/time/broadcaster info has been removed.
    Strips: country prefixes, league prefixes, quality tags, trailing numbers,
            broadcaster names at start, event/game number prefixes like "03 -".
    Does NOT strip: PPV N, Event N, Court N, Day #N, @ between teams.
    """
    t = name

    # Country prefix: "US:", "UK:", "CA:" at start
    t = COUNTRY_PREFIX_RE.sub('', t)

    # League prefix: "NHL |", "MLS |" at start
    t = LEAGUE_PREFIX_RE.sub('', t)

    # Broadcaster name at start of title followed by separator: "ESPN+ -", "TSN+ -"
    broadcaster_start_re = re.compile(
        r'^(' + '|'.join(re.escape(b) for b in sorted(BROADCASTERS, key=len, reverse=True)) + r')\s*[-|:]+\s*',
        re.IGNORECASE
    )
    t = broadcaster_start_re.sub('', t)

    # Event/game number prefix at start: "03 -", "1 -" (standalone number before dash)
    t = re.sub(r'^\d{1,2}\s*-\s*', '', t)

    # Quality/resolution tags
    t = QUALITY_RE.sub('', t)

    # Trailing stream number
    t = TRAILING_NUM_RE.sub('', t)

    # Clean up separators and whitespace
    t = re.sub(r'\s*-\s*$', '', t)
    t = re.sub(r'^\s*[-|:]+\s*', '', t)
    t = re.sub(r'\s{2,}', ' ', t)
    t = t.strip()

    return t or name.strip()


def parse_channel_name(raw: str, idx: int, user_tz_offset: timedelta, category: str) -> dict:
    """
    Parse a channel name into an EPG event dict.
    Handles four provider formats:
      1. Simple: "Event name @ Date Time :Source  N"
      2. Pipe-delimited: "Name | Day Date Time | Quality"
      3. TZ-block: "Name // UK Day Date Time // ET Day Date Time"
      4. Mixed combinations of the above
    """

    working  = raw.strip()
    source   = ""
    date_tuple  = None
    time_hm     = None   # (hour, minute)
    tz_offset   = user_tz_offset   # may be overridden by named TZ block

    # ── Step 1: strip trailing channel number ─────────────────────────────────
    working = TRAILING_NUM_RE.sub('', working)

    # ── Step 2: extract broadcaster tag at end ────────────────────────────────
    src_m = BROADCASTER_RE.search(working)
    if src_m:
        source  = src_m.group(1).strip()
        working = working[:src_m.start()].strip()

    # ── Step 3: handle // TZ-block format ────────────────────────────────────
    if '//' in working:
        working, tz_date, tz_off = extract_tz_blocks(working)
        if tz_date:
            date_tuple = tz_date
        if tz_off is not None:
            # Convert named TZ offset to a timedelta for later UTC conversion
            tz_offset = timedelta(hours=tz_off)
        # Extract time from the preferred block's time string
        tz_blocks = TZ_BLOCK_RE.findall(raw)
        best_score  = 999
        best_time   = None
        for tz_label, _, _, time_str in tz_blocks:
            tz_key = tz_label.lower()
            score  = TZ_PRIORITY.index(tz_key) if tz_key in TZ_PRIORITY else 999
            if score < best_score and TZ_BLOCK_OFFSETS.get(tz_key) is not None:
                best_score = score
                best_time  = parse_time_str(time_str)
        if best_time:
            time_hm = best_time

    # ── Step 4: handle pipe-delimited schedule blocks ─────────────────────────
    schedule_part_saved = ""
    if '|' in working:
        event_part, schedule_part = extract_pipe_schedule(working)
        if schedule_part:
            # Pull date and time out of the schedule segment
            if not date_tuple:
                date_tuple = parse_date_str(schedule_part)
            if not time_hm:
                time_hm = parse_time_str(schedule_part)
            schedule_part_saved = schedule_part
            working = event_part
        # If no schedule segment found, keep working as-is

    # ── Step 5: extract date from remaining working string (fallback to raw) ──
    if not date_tuple:
        for candidate in (working, raw):
            date_tuple = parse_date_str(candidate)
            if date_tuple:
                break

    # ── Step 6: extract time — search working string, then full raw as fallback ─
    if not time_hm:
        for candidate in (working, raw):
            tm = TIME_RE.search(candidate)
            if tm:
                t = parse_time_str(tm.group(0))
                if t:
                    time_hm = t
                    break

    # ── Step 7: strip date/time tokens from the working name ─────────────────
    # If working is very short (just a league tag like "NHL") and there's a schedule
    # part that contains team names after the time, use that for the title instead
    name_source = working
    if schedule_part_saved and len(working.strip()) <= 6:
        # Extract match name from schedule part: strip leading game number and time
        match_name = re.sub(r'^\d{1,2}\s*[-–]\s*', '', schedule_part_saved)
        match_name = re.sub(r'(?:@\s*)?\d{1,2}(?:h|:|\.)(\d{2})\s*(?:am|pm)?(?!\w)', '', match_name, flags=re.IGNORECASE)
        match_name = DOW_RE.sub('', match_name)
        match_name = match_name.strip()
        if len(match_name) > len(working.strip()):
            name_source = match_name

    name_clean = name_source
    name_clean = re.sub(r'\d{4}-\d{2}-\d{2}', '', name_clean)
    name_clean = re.sub(r'\b[A-Za-z]{3,9}\s+\d{1,2}(?:\s+\d{4})?\b', '', name_clean)
    name_clean = re.sub(r'\b\d{1,2}\s+[A-Za-z]{3,9}(?:\s+\d{4})?\b', '', name_clean)
    name_clean = re.sub(r'\d{1,2}[\/\-]\d{1,2}(?:[\/\-]\d{2,4})?', '', name_clean)
    name_clean = re.sub(r'(?:@\s*)?\d{1,2}(?:h|:|\.)(\d{2})\s*(?:am|pm)?(?!\w)', '', name_clean, flags=re.IGNORECASE)
    name_clean = DOW_RE.sub('', name_clean)

    # ── Step 8: apply cosmetic title cleanup ──────────────────────────────────
    title = clean_event_name(name_clean)

    # ── Step 9: build UTC start datetime ─────────────────────────────────────
    # If we have a time but no date, default to today — better than skipping
    if time_hm and not date_tuple:
        today = datetime.now()
        date_tuple = (today.year, today.month, today.day)

    confidence = "ok" if (date_tuple and time_hm) else ("warn" if (date_tuple or time_hm) else "err")
    start_utc  = None

    if date_tuple and time_hm:
        try:
            local_dt  = datetime(date_tuple[0], date_tuple[1], date_tuple[2], time_hm[0], time_hm[1])
            start_utc = local_dt - tz_offset
        except ValueError:
            confidence = "err"

    date_str = (
        f"{date_tuple[0]:04d}-{date_tuple[1]:02d}-{date_tuple[2]:02d}"
        if date_tuple else datetime.now().strftime("%Y-%m-%d")
    )

    if not source:
        source = category.title()

    return {
        "ch_id":      f"ch{idx:03d}",
        "title":      title,
        "source":     source,
        "category":   category,
        "date_str":   date_str,
        "start_utc":  start_utc,
        "duration":   DEFAULT_DURATION_MIN,
        "confidence": confidence,
        "raw":        raw,
    }


# ── XMLTV builder ─────────────────────────────────────────────────────────────

def fmt_xmltv(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S") + " +0000"


def build_xmltv(events: list[dict]) -> str:
    tv = Element("tv", attrib={"generator-info-name": "EPG Generator (GitHub Actions)"})

    seen: set[str] = set()
    for ev in events:
        if ev["ch_id"] not in seen:
            seen.add(ev["ch_id"])
            ch_el = SubElement(tv, "channel", id=ev["ch_id"])
            SubElement(ch_el, "display-name").text = ev["title"][:60]
            if ev["source"]:
                SubElement(ch_el, "display-name").text = ev["source"]

    for ev in events:
        if not ev["start_utc"]:
            print(f"  [SKIP ] No time — skipping: {ev['raw'][:65]}")
            continue

        stop_utc   = ev["start_utc"] + timedelta(minutes=ev["duration"])
        ended_stop = stop_utc        + timedelta(minutes=ENDED_DURATION_MIN)

        # Main event block
        prog = SubElement(tv, "programme", attrib={
            "start":   fmt_xmltv(ev["start_utc"]),
            "stop":    fmt_xmltv(stop_utc),
            "channel": ev["ch_id"],
        })
        SubElement(prog, "title",    lang="en").text = ev["title"]
        SubElement(prog, "desc",     lang="en").text = ev["title"] + (f" — {ev['source']}" if ev["source"] else "")
        SubElement(prog, "category", lang="en").text = "Sports"

        # Event ended block
        ended = SubElement(tv, "programme", attrib={
            "start":   fmt_xmltv(stop_utc),
            "stop":    fmt_xmltv(ended_stop),
            "channel": ev["ch_id"],
        })
        SubElement(ended, "title",    lang="en").text = ENDED_MESSAGE
        SubElement(ended, "desc",     lang="en").text = f"{ev['title']} has finished."
        SubElement(ended, "category", lang="en").text = "Sports"

    raw_xml = tostring(tv, encoding="unicode")
    return minidom.parseString(raw_xml).toprettyxml(indent="  ", encoding=None)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    categories = [c.strip().lower() for c in EPG_CATEGORIES_RAW.split(",") if c.strip()]

    print("=" * 60)
    print("EPG Generator")
    print(f"  Categories  : {', '.join(categories)}")
    print(f"  Timezone    : UTC{TZ_OFFSET_HOURS:+.1f}")
    print(f"  Event dur   : {DEFAULT_DURATION_MIN} min")
    print(f"  Ended block : {ENDED_DURATION_MIN} min → '{ENDED_MESSAGE}'")
    print(f"  Output      : {OUTPUT_FILE}")
    print("=" * 60)

    user_tz = timedelta(hours=TZ_OFFSET_HOURS)

    try:
        content = fetch_m3u(M3U_URL)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)

    all_channels = parse_m3u(content)
    if not all_channels:
        print("ERROR: No channels found in playlist.")
        sys.exit(1)

    matched = filter_by_categories(all_channels, categories)
    print(f"\n  Matched {len(matched)} channels across {len(categories)} "
          f"{'category' if len(categories) == 1 else 'categories'}.")

    if not matched:
        print(f"\nWARNING: No channels matched. Check EPG_CATEGORIES matches")
        print(f"  group-title values in your M3U. Current: {categories}")

    print("\nParsing channel names:")
    events = []
    for idx, ch in enumerate(matched, start=1):
        cat = ch.get("matched_category", categories[0])
        ev  = parse_channel_name(ch["name"], idx, user_tz, cat)
        if ch.get("tvg_id"):
            ev["ch_id"] = ch["tvg_id"]
        events.append(ev)
        time_str = ev["start_utc"].strftime("%Y-%m-%d %H:%M UTC") if ev["start_utc"] else "no time found"
        print(f"  [{ev['confidence'].upper():4}] {ev['title'][:52]}")
        print(f"         @ {time_str}")

    ok      = sum(1 for e in events if e["confidence"] == "ok")
    warn    = sum(1 for e in events if e["confidence"] == "warn")
    err     = sum(1 for e in events if e["confidence"] == "err")
    skipped = sum(1 for e in events if not e["start_utc"])
    print(f"\nSummary: {ok} OK  |  {warn} review  |  {err} no data  |  {skipped} skipped")

    xml_str = build_xmltv(events)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(xml_str)
    print(f"\nWritten: {OUTPUT_FILE} ({len(xml_str)/1024:.1f} KB)")
    print("Done.")


if __name__ == "__main__":
    main()
