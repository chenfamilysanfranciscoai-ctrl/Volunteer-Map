#!/usr/bin/env python3
"""
Weekly volunteer-event updater for the Bay Area environmental volunteering map.

What this does, at a high level:
  1. For each source organization, fetch its calendar/event page(s) over plain HTTP
     (no headless browser needed -- see notes per-org below).
  2. Hand the raw text/HTML to Claude with a forced-JSON tool call to pull out
     structured events (name, date, day, start/end time, location, and a
     registration link when one is actually present in the source text).
  3. Geocode any event location that doesn't already have coordinates, using the
     free OpenStreetMap Nominatim API (rate-limited to 1 request/sec per their
     usage policy).
  4. Filter to events between today and LOOKAHEAD_DAYS from now, merge everything
     into events.json in the schema the map already expects, and write the file.

Design notes / honesty about limitations:
  - This was written by Claude based on manually inspecting each org's site once
    (Sept 2026). It has NOT been run end-to-end against live data, because the
    sandbox that wrote it has restricted network egress. Trigger this workflow
    manually once (Actions tab -> "Update volunteer events" -> Run workflow) and
    check the run log / diff before trusting the weekly schedule.
  - If an org redesigns their page, extraction for that org may silently return
    fewer/no events. Each org is wrapped in its own try/except so one broken
    source doesn't take down the whole run -- check the logs periodically.
  - Never invents registration links: if nothing event-specific is found, the
    event is written without a bookingUrl (falls back to the org's orgUrl, if any).
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta

import requests

try:
    import anthropic
except ImportError:
    print("Missing dependency 'anthropic'. pip install -r requirements.txt", file=sys.stderr)
    raise

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "56"))  # ~8 weeks
EVENTS_JSON_PATH = os.environ.get("EVENTS_JSON_PATH", "events.json")
USER_AGENT = "VolunteerMapBot/1.0 (+https://github.com/chenfamilysanfranciscoai-ctrl/Volunteer-Map; contact: chenfamilysanfranciscoai-ctrl)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

HTTP_HEADERS = {"User-Agent": USER_AGENT}

ORGS = [
    {
        "org": "Surfrider Foundation (SF Chapter)",
        "color": "#4a7856",
        "kind": "google_calendar",
        "calendar_page": "https://sf.surfrider.org/calendar",
    },
    {
        "org": "Pacific Beach Coalition",
        "color": "#d9642b",
        "kind": "google_calendar",
        "calendar_page": "https://www.pacificbeachcoalition.org/calendar-2026/",
        # This page also has a static table mapping each recurring cleanup site
        # to its own Google Form sign-up link -- fetched from the same page and
        # handed to Claude alongside the calendar so it can match site name to
        # the right form link.
        "has_site_form_table": True,
    },
    {
        "org": "Save The Bay",
        "color": "#3b6ea5",
        "kind": "html_listing",
        "listing_page": "https://savesfbay.org/calendar/",
        "detail_link_pattern": r'https://savesfbay\.org/event/[a-z0-9\-]+/?',
    },
    {
        "org": "Grassroots Ecology",
        "color": "#8a6d1f",
        "kind": "html_listing",
        "listing_page": "https://www.grassrootsecology.org/event-calendar",
        "detail_link_pattern": r'https://www\.grassrootsecology\.org/event-calendar/\d{4}/\d{2}/\d{2}/[a-z0-9\-]+',
    },
    {
        "org": "San Francisco Baykeeper",
        "color": "#2a8f8f",
        "kind": "no_dated_events",
        "org_url": "https://baykeeper.org/cleanups/",
    },
]

RECORD_EVENTS_TOOL = {
    "name": "record_events",
    "description": "Record the volunteer events extracted from the source text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Short event name/title"},
                        "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                        "start": {"type": "string", "description": "Start time, e.g. '9:00 AM'"},
                        "end": {"type": ["string", "null"], "description": "End time, e.g. '12:00 PM', or null if not stated"},
                        "location": {"type": "string", "description": "Best available location text: venue name and/or street address"},
                        "bookingUrl": {
                            "type": ["string", "null"],
                            "description": (
                                "The direct sign-up/registration URL for THIS SPECIFIC event, if one is "
                                "actually present in the source text (an org's own registration page, an "
                                "Eventbrite/Google Form/SignUpGenius link, etc). Null if no event-specific "
                                "link is present -- never invent or guess one."
                            ),
                        },
                    },
                    "required": ["name", "date", "start", "location"],
                },
            }
        },
        "required": ["events"],
    },
}

EXTRACTION_SYSTEM_PROMPT = """You extract upcoming volunteer event listings from messy website text/HTML/ICS \
content for a Bay Area environmental volunteering map. Rules:
- Only include events dated today ({today}) or later. Ignore past events.
- One event per calendar entry. If a single day lists many separate sites/locations \
(e.g. a coastal cleanup day with multiple named sites), output each site as its own event, \
all sharing that date.
- For "location", give the most specific address or place name available in the text. \
Do not invent an address if none is given -- use whatever place name is present.
- For "bookingUrl", only use a URL that literally appears in the provided text and is specific \
to that event (not the org's generic homepage). If the text includes a table mapping site names \
to sign-up form links, match each event to the right link by site name. If nothing event-specific \
exists, leave bookingUrl null.
- Normalize times to a "H:MM AM/PM" style string. If no end time is stated, use null.
- Call the record_events tool exactly once with everything you found."""


def log(msg):
    print(msg, flush=True)


def http_get(url, **kwargs):
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp.text


def extract_google_calendar_id(page_html):
    """Google Calendar embeds are plain <iframe src="https://calendar.google.com/calendar/embed?src=...">
    tags present in the server-rendered HTML -- no JS execution needed to find them."""
    m = re.search(r'calendar\.google\.com/calendar/embed\?src=([^&"\']+)', page_html)
    if not m:
        return None
    from urllib.parse import unquote

    return unquote(m.group(1))


def fetch_google_calendar_ics(calendar_id):
    from urllib.parse import quote

    ics_url = f"https://calendar.google.com/calendar/ical/{quote(calendar_id)}/public/basic.ics"
    return http_get(ics_url)


def claude_extract_events(client, org_name, source_text, extra_context=""):
    """One Claude call, forced through the record_events tool, returns a list of
    raw event dicts (name/date/start/end/location/bookingUrl) -- no lat/lng yet."""
    today_str = date.today().isoformat()
    system = EXTRACTION_SYSTEM_PROMPT.format(today=today_str)
    # Keep prompts within a sane size; truncate very long source dumps.
    max_chars = 60000
    if len(source_text) > max_chars:
        source_text = source_text[:max_chars]

    user_content = f"Organization: {org_name}\n"
    if extra_context:
        user_content += f"\n{extra_context}\n"
    user_content += f"\n--- SOURCE TEXT ---\n{source_text}\n--- END SOURCE TEXT ---"

    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system,
        tools=[RECORD_EVENTS_TOOL],
        tool_choice={"type": "tool", "name": "record_events"},
        messages=[{"role": "user", "content": user_content}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_events":
            return block.input.get("events", [])
    return []


def compute_day_name(date_str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
    except ValueError:
        return ""


_geocode_cache = {}


def geocode(location_text):
    """Free OpenStreetMap Nominatim geocoding. Rate-limited to 1 req/sec per
    their usage policy (https://operations.osmfoundation.org/policies/nominatim/)."""
    if not location_text:
        return None, None
    key = location_text.strip().lower()
    if key in _geocode_cache:
        return _geocode_cache[key]

    query = location_text
    if "ca" not in query.lower() and "california" not in query.lower():
        query = f"{query}, California"

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1},
            headers=HTTP_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json()
        time.sleep(1)  # be a good citizen of a free shared service
        if results:
            lat, lng = float(results[0]["lat"]), float(results[0]["lon"])
            _geocode_cache[key] = (lat, lng)
            return lat, lng
    except Exception as e:
        log(f"  ! geocoding failed for '{location_text}': {e}")
    _geocode_cache[key] = (None, None)
    return None, None


def slugify_for_log(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def process_google_calendar_org(client, org_cfg):
    page_html = http_get(org_cfg["calendar_page"])
    cal_id = extract_google_calendar_id(page_html)
    if not cal_id:
        log(f"  ! could not find a Google Calendar id on {org_cfg['calendar_page']}")
        return []
    ics_text = fetch_google_calendar_ics(cal_id)

    extra_context = ""
    if org_cfg.get("has_site_form_table"):
        extra_context = (
            "The following is the full page HTML, which (in addition to the calendar) may contain "
            "a table mapping each recurring cleanup/restoration site name to its own volunteer "
            "sign-up form link (e.g. a Google Form). Use it to fill in bookingUrl by matching site "
            "names to the calendar's event locations.\n\n--- PAGE HTML (for site->form matching) ---\n"
            + page_html[:60000]
        )

    return claude_extract_events(client, org_cfg["org"], ics_text, extra_context)


def process_html_listing_org(client, org_cfg):
    listing_html = http_get(org_cfg["listing_page"])
    detail_urls = sorted(set(re.findall(org_cfg["detail_link_pattern"], listing_html)))[:15]
    log(f"  found {len(detail_urls)} detail page(s)")

    combined = f"--- LISTING PAGE ({org_cfg['listing_page']}) ---\n{listing_html[:20000]}\n"
    for url in detail_urls:
        try:
            detail_html = http_get(url)
            combined += f"\n--- DETAIL PAGE ({url}) ---\n{detail_html[:6000]}\n"
        except Exception as e:
            log(f"  ! failed to fetch detail page {url}: {e}")

    return claude_extract_events(client, org_cfg["org"], combined)


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log("ANTHROPIC_API_KEY is not set -- add it as a repo secret (Settings > Secrets and "
            "variables > Actions) so this workflow can call the Claude API.")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    today = date.today()
    cutoff = today + timedelta(days=LOOKAHEAD_DAYS)

    output = []
    for org_cfg in ORGS:
        org_name = org_cfg["org"]
        log(f"== {slugify_for_log(org_name)} ==")
        events = []
        try:
            if org_cfg["kind"] == "google_calendar":
                events = process_google_calendar_org(client, org_cfg)
            elif org_cfg["kind"] == "html_listing":
                events = process_html_listing_org(client, org_cfg)
            elif org_cfg["kind"] == "no_dated_events":
                log("  no dated events by design (self-directed program)")
            else:
                log(f"  ! unknown org kind: {org_cfg['kind']}")
        except Exception as e:
            log(f"  ! failed to process {org_name}: {e}")
            events = []

        cleaned_events = []
        for ev in events:
            try:
                ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
            except (ValueError, KeyError):
                log(f"  ! skipping event with unparsable date: {ev}")
                continue
            if not (today <= ev_date <= cutoff):
                continue

            lat, lng = geocode(ev.get("location", ""))
            if lat is None:
                log(f"  ! could not geocode '{ev.get('location')}' -- skipping this event")
                continue

            cleaned_events.append(
                {
                    "name": ev["name"],
                    "date": ev["date"],
                    "day": compute_day_name(ev["date"]),
                    "start": ev.get("start", ""),
                    "end": ev.get("end"),
                    "lat": round(lat, 4),
                    "lng": round(lng, 4),
                    "location": ev.get("location", ""),
                    **({"bookingUrl": ev["bookingUrl"]} if ev.get("bookingUrl") else {}),
                }
            )

        cleaned_events.sort(key=lambda e: (e["date"], e["start"]))
        log(f"  -> {len(cleaned_events)} upcoming event(s) kept")

        org_entry = {"org": org_name, "color": org_cfg["color"], "events": cleaned_events}
        if org_cfg.get("org_url"):
            org_entry["orgUrl"] = org_cfg["org_url"]
        output.append(org_entry)

    with open(EVENTS_JSON_PATH, "w") as f:
        json.dump(output, f, indent=2)
        f.write("\n")
    log(f"Wrote {EVENTS_JSON_PATH}")


if __name__ == "__main__":
    main()
