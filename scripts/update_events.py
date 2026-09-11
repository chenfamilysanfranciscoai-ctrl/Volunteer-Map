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
  2026-09-10  The Playwright fallback landed but Pacific Beach Coalition was
              STILL coming back with 0 events. Root-caused for real this time
              via an interactive browser session against the live pages:
                - extract_google_calendar_id()'s regex required `src=` to be
                  the literal first query parameter right after
                  `calendar/embed?`. Pacific Beach Coalition's embed code puts
                  it 8th (after height/wkst/bgcolor/ctz/showCalendars/
                  showTitle/showNav), so the id was never found, on either the
                  plain fetch OR the Playwright-rendered fetch -- this was
                  never actually a JS-rendering problem for this org. Rewrote
                  it to parse the full query string with urllib.parse so
                  parameter order doesn't matter.
                - Separately, even with the id correctly extracted, Pacific
                  Beach Coalition's calendar has public *embedding* enabled
                  but not the public *iCal export* -- its
                  /public/basic.ics URL 404s even though the embed itself
                  shows real events (confirmed by loading both directly).
                  Added render_google_calendar_agenda() as a fallback: when
                  the ICS fetch fails, render the same public calendar's own
                  agenda-mode view (?mode=AGENDA) with Playwright and hand its
                  plain-text event listing to Claude instead. Confirmed this
                  view lists every upcoming Pacific Beach Coalition event out
                  past our 56-day lookahead window.
                - Grassroots Ecology is still returning 0 events and was NOT
                  resolved this round -- its listing page renders as plain
                  server HTML with matching detail links when spot-checked,
                  so the cause isn't obvious yet. Added explicit logging of
                  the fetched HTML size and the number of detail links found
                  in process_html_listing_org() so the next real run's logs
                  pin down whether the regex is finding 0 detail URLs, or
                  finding them but Claude still isn't extracting events from
                  the combined text.
  2026-09-10b The above logging paid off immediately -- run #3's log showed
              exactly what was still wrong for both orgs:
                - Grassroots Ecology: the plain fetch pulled down 488KB of
                  real HTML and STILL matched 0 detail links, on both the
                  plain and Playwright-rendered fetch (489KB, same 0). Not a
                  JS problem after all -- confirmed via the live page's actual
                  href attributes that this site's event links are relative
                  paths ("/event-calendar/2026/09/09/..."), never prefixed
                  with the domain, so a pattern anchored to
                  "https://www.grassrootsecology.org/..." could never match.
                  Changed the pattern to match the relative form and added
                  _find_detail_urls(), which resolves whatever the pattern
                  finds against the listing page's own URL (urljoin) -- a
                  no-op for an org with absolute links like Save The Bay, but
                  required for one with relative links like this.
                - Pacific Beach Coalition: the iCal-unavailable fallback DID
                  trigger correctly and rendered the agenda view, but Claude
                  extracted 0 events from whatever text came back -- with no
                  visibility into what that text actually was. Added logging
                  of the rendered text's length and first 200 characters, plus
                  a defensive click-past-the-consent-dialog step (a completely
                  fresh, cookie-less Playwright browser -- which is what every
                  CI run is -- can get served Google's "Before you continue"
                  interstitial instead of the calendar itself; this wasn't
                  reproducible by hand since an already-logged-in interactive
                  browser doesn't see it). Next run's log will show directly
                  whether this was the cause.
  2026-09-10c Paul asked for thoroughness over speed after round 2 still came
              back with both orgs empty. Run #4's actual log (now readable
              thanks to the logging added in round 2) explained both:
                - Pacific Beach Coalition: the agenda view DID render real
                  content (confirmed: no consent wall, correct page) but only
                  5390 characters of it -- the header plus one event -- because
                  a fixed 2-second pause after "networkidle" wasn't enough for
                  Google Calendar's agenda list to finish populating via its
                  own follow-up JS/XHR calls. Replaced the fixed pause with
                  polling page.inner_text("body") until its length stops
                  growing (checked every second, up to ~20s), and switched to
                  a normal desktop Chrome user-agent + viewport for this
                  specific fetch (the generic bot UA is fine for plain HTML
                  fetches elsewhere, but there's no reason to risk Google's JS
                  treating an unrecognized UA differently for its own app).
                - Grassroots Ecology: the link-matching fix from round 2
                  worked (15 matches, 15 detail pages fetched, Claude
                  extracted 7 real events) -- but all 7 were silently dropped
                  by main()'s date-range filter with no log line, because that
                  check was a bare `continue` with no logging. Root cause:
                  sorted(set(urls))[:15] sorts URLs alphabetically, which for
                  this org's date-stamped URLs is also chronological, and the
                  listing page mixes past and future events together -- so the
                  "first 15" were mostly already-past events, not the next 15
                  upcoming ones. Fixed _find_detail_urls() to parse the date
                  embedded in each URL, drop anything already in the past, and
                  sort what's left soonest-first before capping to 15. Also
                  made the date-range skip in main() always log the event name
                  and why it was dropped, so this class of "everything silently
                  vanished" never has to be re-diagnosed blind again.
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
        # This site's own event links are relative (e.g.
        # href="/event-calendar/2026/09/09/volunteer-at-russian-ridge"), never
        # prefixed with the domain -- confirmed by inspecting the real page's
        # raw href attributes. A pattern anchored to "https://www..." never
        # matched anything, which is why this org always came back with 0
        # events regardless of Playwright. process_html_listing_org() resolves
        # whatever this matches against listing_page, so a relative pattern
        # here is enough (and still works fine for an org whose links happen
        # to be absolute, like Save The Bay below).
        "detail_link_pattern": r'/event-calendar/\d{4}/\d{2}/\d{2}/[a-z0-9\-]+',
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
    """Google Calendar embeds are plain <iframe src="https://calendar.google.com/calendar/embed?...">
    tags present in the server-rendered HTML -- no JS execution needed to find them.

    Don't assume `src` is any particular parameter in the query string -- Google's
    own embed code doesn't put it first for every org (confirmed: Pacific Beach
    Coalition's embed URL is `...embed?height=...&wkst=...&bgcolor=...&ctz=...&
    showCalendars=...&showTitle=...&showNav=...&src=<id>&color=...`, with `src`
    8th). Grab the whole query string after `embed?` and parse it properly
    instead of anchoring a regex to `?src=`."""
    m = re.search(r'calendar\.google\.com/calendar/embed\?([^"\'<>\s]+)', page_html)
    if not m:
        return None
    import html
    from urllib.parse import parse_qs, unquote

    # Server-rendered HTML represents the "&" between query params as the
    # entity "&amp;" inside an attribute value -- unescape that (and
    # unquote() any %-encoding) before splitting into individual params, or
    # everything after the first param gets swallowed into one bogus key.
    query_string = html.unescape(unquote(m.group(1)))
    qs = parse_qs(query_string)
    src_values = qs.get("src")
    if not src_values:
        return None
    return src_values[0]


def fetch_google_calendar_ics(calendar_id):
    from urllib.parse import quote

    ics_url = f"https://calendar.google.com/calendar/ical/{quote(calendar_id)}/public/basic.ics"
    return http_get(ics_url)


def render_google_calendar_agenda(calendar_id):
    """Fallback for calendars where public *embedding* is enabled but public
    *iCal export* isn't -- their /public/basic.ics 404s even though the embed
    itself shows real events. Confirmed to be the case for Pacific Beach
    Coalition's calendar specifically (loaded both URLs directly: the embed
    shows a full month of real events, the .ics URL 404s).

    Google's own agenda-mode view of the same public embed lists every event
    as plain text -- date, time, title, location -- which Claude can parse
    just as well as an ICS feed, and it's driven by the same public sharing
    setting the embed already relies on, so it works anywhere the embed does."""
    from urllib.parse import quote
    from playwright.sync_api import sync_playwright

    url = f"https://calendar.google.com/calendar/embed?src={quote(calendar_id)}&mode=AGENDA"
    log("  (this calendar's public iCal export is unavailable -- rendering its agenda view instead)")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            # A generic bot user-agent is fine for a plain HTML fetch, but
            # Google Calendar's own front end is a full JS app that reads the
            # user-agent -- give it a normal desktop Chrome UA and a normal
            # desktop viewport so it renders the same rich agenda list a real
            # visitor would get, not a degraded/minimal fallback.
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1400, "height": 1000},
            )
            page.goto(url, timeout=45000, wait_until="networkidle")
            # A completely fresh, cookie-less browser (which is what a CI
            # runner always is) sometimes gets Google's "Before you continue"
            # consent interstitial instead of the calendar itself. Click past
            # it if present -- harmless no-op when it isn't.
            for label in ["Accept all", "I agree", "Accept"]:
                try:
                    btn = page.get_by_role("button", name=label, exact=False)
                    if btn.count() > 0:
                        btn.first.click(timeout=3000)
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    pass
            # The agenda list itself renders progressively via JS/XHR after
            # "networkidle" -- run #4 confirmed this is real: the page loaded
            # (no consent wall, no error), but a single fixed 2s pause after
            # load only ever captured the header plus the very first entry
            # (5390 chars) instead of the multi-week list a human sees. Poll
            # until the visible text stops growing instead of guessing a
            # fixed delay.
            previous_len = -1
            stable_checks = 0
            for _ in range(20):  # up to ~20s total
                page.wait_for_timeout(1000)
                current_text = page.inner_text("body")
                if len(current_text) == previous_len:
                    stable_checks += 1
                    if stable_checks >= 2:
                        break
                else:
                    stable_checks = 0
                previous_len = len(current_text)
            body_text = page.inner_text("body")
            log(f"  agenda view rendered {len(body_text)} chars of text after waiting for it to "
                f"stabilize (first 200: {body_text[:200]!r})")
            return body_text
        finally:
            browser.close()


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
    try:
        source_text = fetch_google_calendar_ics(cal_id)
    except requests.RequestException as e:
        # Some calendars have public *embedding* enabled without public *iCal
        # export* enabled -- those are two separate sharing settings in Google
        # Calendar, and a site owner can easily turn on the first without the
        # second. Confirmed this is exactly what's happening for Pacific Beach
        # Coalition: the embed shows real events, but /public/basic.ics 404s.
        log(f"  ! iCal export failed for this calendar ({e}) -- falling back to its agenda view")
        source_text = render_google_calendar_agenda(cal_id)

    extra_context = ""
    if org_cfg.get("has_site_form_table"):
        extra_context = (
            "The following is the full page HTML, which (in addition to the calendar) may contain "
            "a table mapping each recurring cleanup/restoration site name to its own volunteer "
            "sign-up form link (e.g. a Google Form). Use it to fill in bookingUrl by matching site "
            "names to the calendar's event locations.\n\n--- PAGE HTML (for site->form matching) ---\n"
            + page_html[:60000]
        )

    raw_events = claude_extract_events(client, org_cfg["org"], source_text, extra_context)
    log(f"  Claude extracted {len(raw_events)} raw event(s) (before date filtering/geocoding)")
    return raw_events


_URL_DATE_RE = re.compile(r'/(20\d{2})/(\d{2})/(\d{2})/')


def _find_detail_urls(listing_page, listing_html, pattern):
    """Resolve whatever the pattern matches against the listing page's own URL --
    a no-op for an already-absolute match (e.g. Save The Bay), but required for
    a site like Grassroots Ecology whose event links are relative paths.

    A listing page often keeps past events linked alongside upcoming ones (run
    #4's log showed Grassroots Ecology's 15 matches were the 15
    *chronologically earliest* event pages on the whole page -- since the date
    is embedded in the URL, sorting the raw strings put a bunch of already-past
    events ahead of upcoming ones, and every single one of the 7 events Claude
    did extract got silently dropped by main()'s today<=date<=cutoff check).
    When a URL embeds a YYYY/MM/DD date, drop anything clearly in the past and
    sort what's left soonest-first before capping to 15, so the cap actually
    keeps the *next* 15 events rather than the *earliest-ever-linked* 15. Falls
    back to plain alphabetical sort for URLs with no embeddable date (e.g. Save
    The Bay's slug-only event URLs)."""
    from urllib.parse import urljoin

    matches = re.findall(pattern, listing_html)
    resolved = {urljoin(listing_page, m) for m in matches}

    today_str = date.today().isoformat().replace("-", "")  # e.g. "20260910"

    def sort_key(u):
        m = _URL_DATE_RE.search(u)
        if not m:
            return (1, u)  # no date in the URL -- sort after dated ones, alphabetically
        y, mo, d = m.groups()
        return (0, f"{y}{mo}{d}")

    def is_past(u):
        m = _URL_DATE_RE.search(u)
        if not m:
            return False  # can't tell -- don't drop it
        y, mo, d = m.groups()
        return f"{y}{mo}{d}" < today_str

    future_or_unknown = [u for u in resolved if not is_past(u)]
    dropped = len(resolved) - len(future_or_unknown)
    if dropped:
        log(f"  dropped {dropped} detail link(s) whose URL date is already in the past")
    return sorted(future_or_unknown, key=sort_key)[:15]


def process_html_listing_org(client, org_cfg):
    listing_html = http_get(org_cfg["listing_page"])
    log(f"  fetched listing page: {len(listing_html)} chars (plain HTTP)")
    detail_urls = _find_detail_urls(org_cfg["listing_page"], listing_html, org_cfg["detail_link_pattern"])
    log(f"  detail-link regex found {len(detail_urls)} match(es) in the plain fetch")

    if not detail_urls:
        # As with the calendar embeds, this listing may render its event links
        # via client-side JS -- retry with a rendered fetch before giving up.
        listing_html = render_with_playwright(org_cfg["listing_page"])
        log(f"  re-fetched via Playwright: {len(listing_html)} chars")
        detail_urls = _find_detail_urls(org_cfg["listing_page"], listing_html, org_cfg["detail_link_pattern"])
        log(f"  detail-link regex found {len(detail_urls)} match(es) after rendering")

    log(f"  found {len(detail_urls)} detail page(s)")

    combined = f"--- LISTING PAGE ({org_cfg['listing_page']}) ---\n{listing_html[:20000]}\n"
    for url in detail_urls:
        try:
            detail_html = http_get(url)
            combined += f"\n--- DETAIL PAGE ({url}) ---\n{detail_html[:6000]}\n"
        except Exception as e:
            log(f"  ! failed to fetch detail page {url}: {e}")

    raw_events = claude_extract_events(client, org_cfg["org"], combined)
    log(f"  Claude extracted {len(raw_events)} raw event(s) from {len(combined)} chars of source text "
        f"(before date filtering/geocoding)")
    return raw_events


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
                # This used to be a silent `continue` -- when it's every single
                # extracted event (as happened for Grassroots Ecology in run
                # #4, all 7 dropped here with zero explanation in the log),
                # there was no way to tell this apart from a geocoding problem
                # or Claude finding nothing at all. Always log why.
                reason = "before today" if ev_date < today else f"beyond the {LOOKAHEAD_DAYS}-day lookahead"
                log(f"  ! skipping '{ev.get('name')}' on {ev['date']} -- {reason}")
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
