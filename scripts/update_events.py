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
    (Sept 2026). Run #1 surfaced real bugs (see CHANGELOG at the bottom of this
    docstring) which are now fixed as best as they could be verified from a
    sandbox with no direct network access to these sites -- Claude validated the
    underlying fixes using a separate interactive browser tool, but could not run
    THIS script live end-to-end. Treat every manual "Run workflow" as a real test,
    not a formality: read the log, read the diff.
  - Several of these sites render their calendar/event links via client-side
    JavaScript, so a plain HTTP GET sometimes returns HTML that doesn't yet
    contain the data (no headless browser executes the page's JS). Each such
    fetch has a Playwright-rendered fallback that only kicks in when the cheap
    plain-HTTP path finds nothing -- see render_with_playwright().
  - If an org redesigns their page, extraction for that org may silently return
    fewer/no events. Each org is wrapped in its own try/except so one broken
    source doesn't take down the whole run -- check the logs periodically.
  - Never invents registration links: if nothing event-specific is found, the
    event is written without a bookingUrl (falls back to the org's orgUrl, if any).
    Every bookingUrl is also verified with a real HTTP request before being kept
    (see verify_url()) -- a link that 404s/times out is dropped, not published.

CHANGELOG:
  2026-09-09  Initial version.
  2026-09-16  After run #1 came back with Pacific Beach Coalition and Grassroots
              Ecology both finding 0 events, and Save The Bay's 2 events failing
              to geocode:
                - Added a Playwright-rendered fallback for both the Google
                  Calendar embed lookup and the HTML-listing detail-link scan,
                  used only when the plain-HTTP pass finds nothing. (Confirmed
                  via an interactive browser session that Pacific Beach
                  Coalition's calendar iframe -- and presumably Grassroots
                  Ecology's event links -- exist in the rendered DOM; a plain
                  fetch alone wasn't enough for at least one of these two.)
                - Added LOCATION_OVERRIDES for known recurring sites plus a
                  looser retry query, to fix Nominatim geocoding failures on
                  full venue names like "MLK Regional Shoreline, Oakland".
                - Added verify_url() to check every extracted bookingUrl
                  actually resolves (< 400 status after redirects) before it's
                  written to events.json.
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

# A few recurring sites where free-text geocoding of the venue name alone is
# known to fail or land on the wrong spot (e.g. Nominatim doesn't know "MLK" as
# an abbreviation). Add to this as new mismatches show up in the logs -- match
# is a case-insensitive substring check against the extracted location text.
LOCATION_OVERRIDES = {
    "mlk": (37.7433, -122.1975),  # MLK Jr. Regional Shoreline, Oakland
    "eden landing": (37.6155, -122.1085),  # Eden Landing Ecological Reserve, Hayward
    "ravenswood": (37.4784, -122.1997),  # Ravenswood / Cooley Landing, East Palo Alto
    "radio road": (37.6262, -122.1114),  # Radio Road Marsh, Redwood City
}

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


def render_with_playwright(url):
    """Fallback for pages that build their calendar/event links with client-side
    JavaScript, so a plain requests.get() sees an empty shell. Only called when
    the cheap plain-HTTP path finds nothing -- this is slower (spins up a real
    headless Chromium) and isn't needed for every org."""
    from playwright.sync_api import sync_playwright

    log(f"  (falling back to a rendered browser fetch for {url})")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, timeout=45000, wait_until="networkidle")
            # Give any late-loading widgets (e.g. a Google Calendar iframe) a
            # moment past "networkidle" to finish painting.
            page.wait_for_timeout(2000)
            return page.content()
        finally:
            browser.close()


def verify_url(url, timeout=10):
    """Confirm a booking link actually resolves before we publish it. A 2xx/3xx
    (after redirects) counts as good; anything else, or a request that errors
    out entirely, means we drop the link rather than publish something dead."""
    try:
        resp = requests.head(
            url, headers=HTTP_HEADERS, timeout=timeout, allow_redirects=True
        )
        if resp.status_code >= 400:
            # Some servers don't implement HEAD properly -- retry with GET
            # before giving up on an otherwise-plausible link.
            resp = requests.get(
                url, headers=HTTP_HEADERS, timeout=timeout, allow_redirects=True, stream=True
            )
            resp.close()
        return resp.status_code < 400
    except requests.RequestException as e:
        log(f"  ! bookingUrl failed verification, dropping it: {url} ({e})")
        return False


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


def _nominatim_lookup(query):
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": query, "format": "json", "limit": 1, "countrycodes": "us"},
        headers=HTTP_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json()
    time.sleep(1)  # be a good citizen of a free shared service -- max 1 req/sec
    if results:
        return float(results[0]["lat"]), float(results[0]["lon"])
    return None


def geocode(location_text):
    """Resolve a location string to (lat, lng). Order of attempts:
      1. LOCATION_OVERRIDES -- known recurring sites we've manually verified.
      2. Nominatim (OpenStreetMap) on the full location text, with ", USA"
         appended if no state/country is already present.
      3. Nominatim again on a loosened version of the query (drop a leading
         venue name, keep just the "<City>, CA" tail) -- full venue names like
         "MLK Regional Shoreline, Oakland" sometimes confuse free-text search
         even though the city+state alone resolves fine.
    Returns (None, None) if nothing worked, so the caller can skip the event
    rather than plot a wrong or fabricated pin."""
    if not location_text:
        return None, None
    key = location_text.strip().lower()
    if key in _geocode_cache:
        return _geocode_cache[key]

    for needle, coords in LOCATION_OVERRIDES.items():
        if needle in key:
            _geocode_cache[key] = coords
            return coords

    query = location_text
    if "ca" not in query.lower() and "california" not in query.lower() and "usa" not in query.lower():
        query = f"{query}, USA"

    try:
        result = _nominatim_lookup(query)
        if result:
            _geocode_cache[key] = result
            return result
    except Exception as e:
        log(f"  ! geocoding failed for '{location_text}': {e}")

    # Loosen the query: keep only the last couple of comma-separated segments
    # (typically "<city>, <state>"), which is more likely to be recognized.
    parts = [p.strip() for p in location_text.split(",") if p.strip()]
    if len(parts) > 1:
        loose_query = ", ".join(parts[-2:]) + ", USA"
        if loose_query.lower() != query.lower():
            try:
                result = _nominatim_lookup(loose_query)
                if result:
                    log(f"  (geocoded '{location_text}' via loosened query '{loose_query}')")
                    _geocode_cache[key] = result
                    return result
            except Exception as e:
                log(f"  ! loosened geocoding also failed for '{location_text}': {e}")

    _geocode_cache[key] = (None, None)
    return None, None


def slugify_for_log(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def process_google_calendar_org(client, org_cfg):
    page_html = http_get(org_cfg["calendar_page"])
    cal_id = extract_google_calendar_id(page_html)
    if not cal_id:
        # The iframe is probably injected by client-side JS on this site rather
        # than present in the raw server HTML (confirmed to be the case for at
        # least one org this way) -- render it with a real browser and retry.
        page_html = render_with_playwright(org_cfg["calendar_page"])
        cal_id = extract_google_calendar_id(page_html)
    if not cal_id:
        log(f"  ! could not find a Google Calendar id on {org_cfg['calendar_page']} "
            f"even after a rendered-browser fetch -- the page structure may have "
            f"changed more substantially and needs a human look")
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

    if not detail_urls:
        # As with the calendar embeds, this listing may render its event links
        # via client-side JS -- retry with a rendered fetch before giving up.
        listing_html = render_with_playwright(org_cfg["listing_page"])
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

            booking_url = ev.get("bookingUrl")
            if booking_url and not verify_url(booking_url):
                booking_url = None  # dropped, not published -- see verify_url()

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
                    **({"bookingUrl": booking_url} if booking_url else {}),
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
