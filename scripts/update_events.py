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
  2026-09-10c The site owner asked for thoroughness over speed after round 2 still came
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
  2026-09-11  Run #6 (the first run with all of the above fixes applied)
              crashed the whole script: `Claude extracted 1071 raw event(s)`
              for Grassroots Ecology, then a TypeError ("string indices must
              be integers") once the cleaning loop hit one of them, because
              those 1071 "events" were plain strings, not the objects
              record_events' schema requires -- and that loop only caught
              ValueError/KeyError, not TypeError, so it went unhandled and
              killed main() before events.json was ever written. That threw
              away Surfrider's and Save The Bay's perfectly good results for
              the run too, not just Grassroots Ecology's bad ones -- a single
              malformed record from one org shouldn't be able to do that.
              Added two layers of defense: claude_extract_events() now drops
              any non-object entries right at the source (logging how many),
              and the cleaning loop in main() now skips a non-dict event
              outright and wraps the rest of the per-event work in a broad
              try/except, so nothing short of the whole process dying can
              stop events.json from being written with whatever good data was
              actually collected.
  2026-09-11b The site owner re-ran before this fix had actually been pasted in, so run
              #7 hit the identical crash again on the identical line -- but
              its log was still useful: with the earlier UA/stabilize fix,
              Pacific Beach Coalition's agenda view now genuinely works
              (30 raw events extracted, 13 kept), which resolves the "PBC
              extracts nothing" thread from before. What's left is a
              geocoding gap: 8 of those PBC events failed to geocode, and
              looking at the failing addresses, several are subtly corrupted
              by the extraction step -- e.g. "Pacifica State Beach, 1416 9th
              St, California 95814, United States" carries a Sacramento zip
              code that has nothing to do with Pacifica. The single "last 2
              comma-separated segments" fallback couldn't recover from that
              (it produced "California 95814, United States", dropping the
              city entirely). Verified live against Nominatim's real API
              (not simulated) that every one of these failing addresses'
              *venue name alone* -- "Sharp Park Beach, CA", "Pacifica State
              Beach, CA", "Montara Beach, CA", "Thornton State Beach, CA" --
              resolves correctly to the real place, since named parks/beaches
              are already in OpenStreetMap under their own name regardless of
              what's wrong with the rest of the address. Rewrote geocode()
              around a general, ordered list of fallback query shapes (full
              address -> venue name alone -> last 3 segments -> last 2
              segments) instead of a single hardcoded loosening, verified the
              exact addresses from run #7's log now resolve to their real,
              live-checked coordinates, and re-ran a full mocked dry run of
              main() end to end (every known bad-data shape from every past
              run, all at once) confirming nothing crashes and every
              previously-working path (LOCATION_OVERRIDES, already-good
              addresses) is unaffected.
  2026-09-17  The site owner reported three things after a week of runs (#8 manual,
              #9 scheduled) had gone by since the last fix: Pacific Beach
              Coalition empty again, "dates aren't right on a lot of the
              links", and a Save The Bay Hayward event dated Oct 3rd linking
              to the same page as the MLK Coastal Cleanup event. Read run
              #9's actual log (not a guess) to root-cause each:
                - Pacific Beach Coalition: the agenda-view render is now
                  failing with "Page.goto: Timeout 45000ms exceeded" while
                  waiting for "networkidle" -- it worked in run #7 (30 raw
                  events) with the exact same code, so this isn't broken
                  logic, it's an unreliable wait condition: Google
                  Calendar's agenda view apparently keeps some background
                  connection open, so "no network activity for 500ms" can
                  simply never happen even once the page has fully loaded.
                  Switched render_google_calendar_agenda()'s page.goto() to
                  wait_until="domcontentloaded" instead (the existing
                  poll-until-the-text-stops-growing loop right after it
                  already handles waiting for the JS-rendered list itself,
                  so it doesn't depend on networkidle at all), bumped the
                  timeout to 60s for margin, and added one retry with a
                  fresh page before giving up.
                - "Dates aren't right" / the Save The Bay Oct 3rd + duplicate
                  link report: does NOT match what's actually in the live
                  events.json (2 Save The Bay events, both 9/19, two
                  different bookingUrls) or what run #9's log shows it wrote.
                  index.html's fetch('events.json') had no cache-busting at
                  all, so a browser (or the GitHub Pages CDN) serving a
                  stale copy is the far more likely explanation than a data
                  bug -- added a timestamp query param and cache: 'no-store'
                  so a visitor always gets the current file.
                - Also found, independently, while reading run #9's log: a
                  genuinely wrong pin. "1400 Broadway St, Redwood City, CA
                  94063" (a real Grassroots Ecology event) geocoded to
                  (34.0228, -118.4839) -- Los Angeles, not Redwood City. The
                  log showed why: the full address returns zero Nominatim
                  results (live-verified, even with ", USA" appended), so it
                  fell through to the "venue name only" fallback, which for
                  THIS address is just the bare street number+name with no
                  city ("1400 Broadway St, CA, USA") -- and that resolves,
                  successfully but wrongly, to a different Broadway St in
                  Santa Monica. A wrong-but-successful geocode is worse than
                  a failed one, since nothing downstream flags it. Live
                  Nominatim checks confirmed "Redwood City, CA 94063, USA"
                  (the last-2-segments fallback) resolves correctly.
                  Reordered _build_geocode_attempts() so the
                  city-preserving fallbacks (last 3 / last 2 segments) are
                  tried before "venue name only", and skip "venue name only"
                  entirely whenever the first comma-segment starts with a
                  digit -- a real venue name never does, and a bare street
                  number+name is too ambiguous to geocode without its city.
                  Re-verified this doesn't regress the run #7 PBC addresses
                  (their venue names don't start with digits, so they still
                  get tried, just after the now-earlier fallbacks that would
                  fail for them anyway) and re-ran a full mocked dry run of
                  main() end to end before shipping.
  2026-09-18  The site owner re-ran with the above fixes applied and reported three
              more things: Grassroots Ecology events still have no
              "I will help!" link at all (they pointed at
              grassrootsecology.org/calendar -> click a date -> click an
              event -> lands on a page like .../event-calendar/2026/09/19/
              coastal-cleanup-day-redwood-city, and asked for exactly that);
              the Save The Bay Hayward event's date/link still looked wrong;
              and some Sept 19 events seemed to be missing from the map
              entirely.
                - Checked the live map data directly: as of this run, all 5
                  of Grassroots Ecology's actual Sept 19 events (and both
                  Sept 20 ones) ARE present and correctly geocoded -- the
                  "missing events" report was almost certainly from an
                  earlier state (before this run, or before a browser
                  refresh); nothing further to fix there.
                - Root-caused the missing Grassroots Ecology bookingUrls:
                  process_html_listing_org() fetched each event's own detail
                  page but blended ALL of them into one combined block of
                  text and asked Claude to extract every event AND match
                  each one back to the right source URL in a single call.
                  That inference apparently works for Save The Bay but
                  wasn't landing for Grassroots Ecology. Rewrote it to call
                  Claude once PER detail page instead (confirmed live: each
                  event has its own page, e.g. exactly the Redwood City URL
                  the site owner linked, with its own "Register Here" button), then
                  set that event's bookingUrl to the page's own URL
                  programmatically afterward -- not inferred by the model at
                  all, so it can't be wrong. This is also just what the site owner
                  described wanting: the button now goes to that same
                  "volunteer tab" they click through to by hand. Applies to
                  both html_listing orgs (Save The Bay too), so both are now
                  equally reliable instead of one working by luck.
                - Investigated the Hayward date claim directly: the map's
                  data (from a run several days earlier) says Sept 19; the
                  live savesfbay.org page for that same event now says
                  October 3. Checked the OTHER Save The Bay event (MLK
                  Regional Shoreline) as a control -- it still correctly
                  says Sept 19, unchanged -- so this isn't a systemic
                  scraping bug or a caching issue, it's that Save The Bay
                  itself changed/rescheduled specifically the Hayward
                  event's date sometime after our last scrape ran. Nothing
                  in our pipeline was wrong at the time it ran; the fix is
                  just to re-run and pick up their current listing (which
                  this changelog's other fixes make worth doing anyway).
                  The "brings me to the same page as the coastal MLK one"
                  part: both event pages legitimately list each other as
                  related events in their own footer ("Invasive Plant Pull
                  at Ravenswood...", "Habitat Restoration at Eden Landing...")
                  -- that's savesfbay.org's own related-events nav, not a
                  bug in our bookingUrl.
                - Re-ran a full mocked dry run of main() end to end
                  (including the new per-detail-page extraction path) before
                  sending, confirming bookingUrl is set correctly per event
                  and nothing crashes.
  2026-09-18b The site owner clarified the "missing Sept 19 events" from earlier the
              same day were about Pacific Beach Coalition specifically, not
              Grassroots Ecology -- and indeed, run #10 (the first real run
              with the domcontentloaded fix from earlier today) still came
              back with 0 PBC events. Read that run's actual log: this time
              there was no timeout at all -- "agenda view rendered 4841
              chars of text after waiting for it to stabilize", the exact
              same char count as run #7's log, which is confirmed (from
              that same run's own log) to have extracted 30 real events from
              what was the same real calendar content. Live-reloaded the
              actual PBC agenda URL directly in a browser just now and
              confirmed it currently renders ~4900 chars of real event text
              (Calera Creek, Esplanade, Foster City, Montara Beach, Mussel
              Rock, ...), not an empty/header-only page. So the page was
              almost certainly rendered correctly both times, and Claude's
              own extraction call came back with 0 events on this one run
              for no code-level reason -- nothing in claude_extract_events()
              or its caller changed between run #7 and run #10. Rather than
              chase a single non-reproducible bad response further, added a
              pragmatic safety net: claude_extract_events() now retries once
              (a fresh, independent API call) whenever the FIRST attempt
              returns 0 events from a substantial amount of source text
              (>=800 chars -- short/empty pages still correctly return 0
              without wasting a retry), and logs 500 chars of the source
              text (not just 200) whenever that happens, so if this ever
              turns out to be a *repeatable* content/prompt problem rather
              than a one-off miss, the next log has enough in it to diagnose
              without this much run-archaeology. Verified with dedicated
              unit tests (recovers on a one-off miss, doesn't retry for
              short text, retries exactly once and gives up on a persistent
              failure) plus a full mocked dry run of main() end to end.
  2026-09-18c The 09-18b retry fix shipped, but run #11 showed Pacific Beach
              Coalition STILL returning 0 events -- and this time the log
              (with the new 500-char preview) proved the retry ALSO failed
              on the exact same real content: a real time, "Calera Creek
              Habitat Restoration", and a real maps.app.goo.gl link were all
              plainly present in the source text handed to Claude on both
              calls. That ruled out "one-off stochastic miss" for good.
              Pacific Beach Coalition turned out to be the ONLY org
              configured with has_site_form_table -- which appended up to
              60,000 characters of the calendar page's raw, unrendered HTML
              (mostly nav/footer/script boilerplate) as extra_context, so
              Claude could try to match each event to its own sign-up form
              link straight from the whole page. That was the one
              structural difference between this org and every other org in
              the same run, all of which extracted correctly. Removed the
              feature entirely (the agenda text alone already carries a
              per-event link when the source page has one). Verified with a
              unit test confirming extra_context is now always empty for
              this org, plus a full mocked dry run. Checked GitHub Actions
              directly afterward: run #12 (with this fix) succeeded with 25
              real, correctly-geocoded Pacific Beach Coalition events, live
              on the map (screenshot-verified).
  2026-09-18d The site owner asked for Pacific Beach Coalition's per-site direct
              Google Form links specifically, since that's what makes this
              org's data special. Re-added the site->form matching, but
              entirely in code this time, AFTER extraction, instead of
              handing Claude the raw page (which is what broke extraction
              in 09-18c): _extract_site_form_links() pulls (site name, form
              URL) pairs straight out of the calendar page's own small HTML
              table (confirmed live: a plain, un-rendered <table> with one
              row per site -- 11 rows on a real fetch, all 11 containing a
              form link), and _match_site_form_link() scores each extracted
              event against those rows by shared, inverse-document-
              frequency-weighted words (so a word nearly every row has, like
              "cleanup" or "beach", barely counts, while a rare place name
              like "foster" or "mussel" counts heavily), refusing to guess
              when the top two candidates are within a hair of each other.
              A first version still produced one false positive under
              testing against real data: "Pacifica State Beach Cleanup" (a
              real event with no table row) won a clear, non-tied match
              against "Linda Mar State Beach Cleanup" purely off generic
              shared words ("pacifica", "state", "beach", "cleanup") that
              most of the table's rows share. Fixed with a second "anchor
              gate" check: even a non-tied winner must also share at least
              one genuinely RARE word (appearing in only a couple of the
              table's own rows) with the event, using a stricter stopword
              set that also strips those generic descriptive words --
              rejecting outright (never falling back to a lower-ranked
              candidate) when it doesn't. Verified against the real 11-row
              table plus real event names/locations pulled from a live
              events.json: 16 test cases covering every real site (including
              disambiguating "Linda Mar State Beach Cleanup" from the
              separate "Linda Mar Habitat Restoration" at the same spot) and
              five real non-matching events that must return None (Pacifica
              State Beach Cleanup, Thornton Vista Cleanup and Habitat
              Restoration, Calera Creek Habitat Restoration, CA Coastal
              Cleanup Day, PBC General Meeting) -- all pass. Also re-verified
              the underlying table parser against the real, live PBC
              calendar page HTML (not just a reconstruction), confirming 11
              real rows with the exact same site names used in testing.
              Finished with a full mocked dry run of process_google_calendar_org()
              confirming: extra_context stays empty (doesn't regress
              09-18c), exactly one Claude call is made, real events get
              matched to their real form links, and events with no table row
              are correctly left without one rather than guessing.
  2026-09-18e The site owner walked through the Pacific Beach Coalition calendar by
              hand (main page -> click a date -> click an event -> "Register
              online to volunteer") and gave a real, working form URL --
              which immediately showed the whole 09-18d approach above,
              while real and thoroughly tested against the data it was
              built on, was solving the wrong problem. That real link
              turned out to belong to "Calera Creek Habitat Restoration",
              an event that was NEVER in the 11-row site->form table on the
              calendar page (confirmed: the table-matching tests correctly
              returned None for it) -- because the real registration link
              was never in that table at all. It's embedded directly in
              each individual Google Calendar EVENT's own description,
              which only becomes visible by clicking that specific event.
              Investigated live (Chrome DevTools-style, via the browser
              tools) what actually powers that per-event popup: the
              calendar's own embed widget calls Google's public Calendar
              API v3 (`events.list`) directly from the browser, using an
              API key baked into Google's own client-side JS -- the exact
              same request anyone can make with their own free API key (no
              OAuth, no billing required -- confirmed against Google's
              current docs). That response contains the FULL event data
              already, including each event's own description with its
              real registration link, and confirmed the same is true for
              Surfrider's calendar too (the site owner asked whether this would help
              there as well -- it does, same mechanism, same fix).
                Replaced the whole site-form-table/word-matching approach
              (_extract_site_form_links, _match_site_form_link, and the two
              stopword sets around it -- all now deleted) with a direct
              call to that same official API
              (fetch_google_calendar_events_via_api(), using a new optional
              GOOGLE_CALENDAR_API_KEY secret), which gives exact structured
              data straight from Google for both "google_calendar" orgs: no
              more AI extraction, no more guessing at dates/times/locations
              from scraped text, and each event's own real registration
              link pulled directly out of its description
              (_extract_registration_link_from_description) rather than
              approximated after the fact. Along the way, confirmed live
              that the register link inside a real description is
              sometimes wrapped in Google's own "https://www.google.com/
              url?q=..." redirect shim (even in the raw API response, not
              just the rendered widget) -- decoded directly
              (_decode_google_url_shim) rather than depending on that
              redirect service behaving the same way for a plain
              server-side request. Also confirmed live that the register
              link's visible text is sometimes wrapped in a NESTED tag
              (e.g. "<a ...><b>REGISTER HERE</b></a>", even triple-nested
              for one Surfrider event) -- a naive "no nested tags" regex
              would silently miss these entirely, so
              _extract_registration_link_from_description() captures
              everything up to the closing </a> and strips inner tags from
              the captured text afterward instead. Separately confirmed
              live that a few PBC events (Foster City Cleanup, Mussel Rock
              Beach Cleanup, Linda Mar Habitat Restoration, CA Coastal
              Cleanup Day) have no location field and no "Where to Meet:"
              text in their description either -- previously they still
              showed up on the map because Claude's own extraction
              inferred a reasonable location from the event name; the pure
              structured-API path has no such inference, so without a
              fallback these would have silently disappeared from the map,
              a real regression. Added a small named-site fallback list
              (_PBC_EVENT_NAME_LOCATION_FALLBACKS) for exactly these
              already-known recurring events, which still goes through the
              normal geocode() fallback chain rather than a hardcoded
              coordinate.
                Kept the whole older ICS/agenda-view/AI-extraction pipeline
              in place as an automatic fallback -- both when
              GOOGLE_CALENDAR_API_KEY isn't set yet and when the API call
              itself fails for any reason (bad key, quota, network) -- so
              this can't leave either org silently empty. Verified with 15
              unit tests covering the link-decoding, nested-tag extraction,
              location fallback chain, and event-shape conversion against
              real structures confirmed live for both orgs, plus a full
              mocked dry run of process_google_calendar_org() (API path for
              both orgs, including the exact Thornton Vista event the old
              table-matching approach could never link) and of main()
              end-to-end, and a fallback test confirming the older pipeline
              still kicks in correctly both when the key is unset and when
              the API call itself errors.
2026-09-19  GOOGLE_CALENDAR_API_KEY got set up and this pipeline finally got
              a real production run -- confirming Surfrider worked exactly as
              designed (5 events fetched straight from the API, real
              registration links, no AI extraction), but Pacific Beach
              Coalition was STILL falling back to the older pipeline every
              time, now failing differently: not "key missing" but a genuine
              "404 Client Error: Not Found" from
              www.googleapis.com/calendar/v3/calendars/.../events, using the
              exact calendar id extract_google_calendar_id() had pulled from
              the page.
                Root-caused live rather than guessed: pulled Pacific Beach
              Coalition's actual calendar page HTML directly and confirmed
              its embed iframe's `src` value is NOT a plain calendar id like
              Surfrider's -- it's base64 ("cGlja2l0dXBwYWNpZmljYUBnbWFpbC5j
              b20"). extract_google_calendar_id() was using that raw base64
              string AS the calendar id, which is why every fetch 404'd: no
              calendar is literally named that string. Decoded it
              (base64 -> "pickituppacifica@gmail.com") and verified LIVE, in
              a real browser, that a direct call to this exact same
              production endpoint --
              https://www.googleapis.com/calendar/v3/calendars/pickituppacifica%40gmail.com/events
              -- returns a real 200 with real event data for that decoded
              id, proving definitively that the calendar's public sharing
              was never the problem; the raw, un-decoded src value was.
                Added _resolve_calendar_id(): if the embed's `src` doesn't
              already contain "@" (a real calendar id always does), try
              base64-decoding it (padding it back out first, since it's
              routinely stored unpadded in HTML/URLs -- confirmed true for
              this exact id, 35 chars, not a multiple of 4) and use the
              decoded value only if THAT contains "@" too -- otherwise the
              original string is kept untouched, so a future org with a
              plain but coincidentally base64-charset-safe id can't get
              mangled by this. extract_google_calendar_id() now runs every
              extracted id through this before returning it, so both the
              plain-id case (Surfrider) and the base64 case (Pacific Beach
              Coalition) resolve to the correct real id automatically, with
              no per-org configuration needed.
                Also used this same production run's log to confirm two
              Surfrider events (both recurring "Monthly Chapter Meeting"
              instances) get correctly dropped for having no location data
              anywhere -- no location field, no "Where to Meet:" text, and
              (checked directly against the real API response) nothing
              location-like anywhere in the description either. This is not
              a regression from the API path: the older AI-extraction
              pipeline hit the exact same wall on this org's own page text
              in the past (its earlier log line literally read "could not
              geocode 'Meeting location provided in registration link'" --
              i.e. even Claude could only find a placeholder saying the
              location was inside the registration form, not an actual
              address). Nothing to fix here -- a real event with no
              discoverable location correctly gets skipped rather than
              plotted with a guessed or wrong pin, on both pipelines alike.
                Verified with 4 new unit tests covering _resolve_calendar_id
              directly (decodes the real confirmed PBC id, leaves a plain id
              alone, leaves non-calendar-shaped base64 alone rather than
              guessing, and handles the padding-stripped form same as the
              real HTML has it) -- 19 unit tests total now, all passing --
              plus a full mocked dry run of main() end-to-end confirming
              nothing else regressed.
"""

import base64
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
# Optional: a free Google Cloud API key with the Calendar API enabled (no
# billing required -- see the CHANGELOG entry below dated 2026-09-18e, and
# the repo secret GOOGLE_CALENDAR_API_KEY it's read from in the workflow).
# When set, Surfrider's and
# Pacific Beach Coalition's calendars (both "google_calendar" orgs) are read
# straight from Google's own official Calendar API instead of the older
# ICS/agenda-view + AI-extraction pipeline -- exact structured data and each
# event's own real registration link, no guessing. When unset, those two
# orgs silently fall back to the older pipeline so the script still works.
GOOGLE_CALENDAR_API_KEY = os.environ.get("GOOGLE_CALENDAR_API_KEY")
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
    return _resolve_calendar_id(src_values[0])


def _resolve_calendar_id(raw_id):
    """The `src` value from a Google Calendar embed is usually already the
    real calendar id (e.g. "xxxx@group.calendar.google.com" -- confirmed
    this is exactly what Surfrider's embed uses, and it works as-is).

    2026-09-19: root-caused, with a real live test, why Pacific Beach
    Coalition's API fetch was 404ing on every run even after
    GOOGLE_CALENDAR_API_KEY was correctly set: its embed's `src` isn't a
    plain id at all, it's base64 -- "cGlja2l0dXBwYWNpZmljYUBnbWFpbC5jb20",
    confirmed live to be exactly the base64 encoding of
    "pickituppacifica@gmail.com". Our fetch was asking the API for a
    calendar literally NAMED that base64 string, which of course doesn't
    exist (404) -- meanwhile the calendar IS genuinely public: a direct
    call to this script's own endpoint,
    https://www.googleapis.com/calendar/v3/calendars/pickituppacifica%40gmail.com/events,
    was verified live (in a browser, using a real key) to return 200 with
    real event data, proving the base64 id -- not the calendar's public
    sharing settings -- was the entire problem.

    A plain id always contains "@" already, so only attempt to decode when
    it doesn't, and only trust the decoded result if IT contains "@" too
    (a real calendar id always does) -- otherwise silently keep the
    original string. This avoids mangling some future org's plain id that
    happens to only use base64-safe characters (unlikely, but cheap to
    guard against) while still recovering the real id for orgs like this
    one."""
    if "@" in raw_id:
        return raw_id
    try:
        padded = raw_id + "=" * (-len(raw_id) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
    except Exception:
        return raw_id
    if "@" in decoded:
        log(f"  (this calendar's embed uses a base64-encoded id -- decoded it to the real calendar id)")
        return decoded
    return raw_id


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

    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    url = f"https://calendar.google.com/calendar/embed?src={quote(calendar_id)}&mode=AGENDA"
    log("  (this calendar's public iCal export is unavailable -- rendering its agenda view instead)")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            # Run #9's log showed this failing outright with "Page.goto:
            # Timeout 45000ms exceeded" waiting for "networkidle" -- Google
            # Calendar's agenda view keeps some background connection alive
            # (long-poll/analytics/etc.), so "networkidle" (no network
            # activity for 500ms) can simply never be reached, even though
            # the page itself has fully rendered. Run #7's own log proves the
            # page loads fine well within 45s when it isn't waiting on that:
            # it captured 4841 stable chars of agenda text. Switch to
            # "domcontentloaded" (fires as soon as the DOM itself is parsed,
            # regardless of any lingering background network activity) and
            # lean on the polling-until-stable loop below -- which already
            # exists specifically to wait for the JS-rendered agenda list to
            # finish filling in -- to determine when the page is actually
            # ready, instead of an unreliable network-idle signal.
            #
            # Belt-and-suspenders: also retry the whole load once (fresh
            # page) if it still times out, since a CI runner's network can
            # just be having a bad moment -- one retry is cheap next to a
            # whole org silently coming back empty for a week.
            last_error = None
            for attempt in (1, 2):
                page = browser.new_page(
                    # A generic bot user-agent is fine for a plain HTML
                    # fetch, but Google Calendar's own front end is a full
                    # JS app that reads the user-agent -- give it a normal
                    # desktop Chrome UA and viewport so it renders the same
                    # rich agenda list a real visitor would get, not a
                    # degraded/minimal fallback.
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1400, "height": 1000},
                )
                try:
                    page.goto(url, timeout=60000, wait_until="domcontentloaded")
                except PlaywrightTimeoutError as e:
                    last_error = e
                    log(f"  ! agenda view load attempt {attempt} timed out: {e}")
                    page.close()
                    if attempt == 2:
                        raise
                    continue
                # A completely fresh, cookie-less browser (which is what a
                # CI runner always is) sometimes gets Google's "Before you
                # continue" consent interstitial instead of the calendar
                # itself. Click past it if present -- harmless no-op when
                # it isn't.
                for label in ["Accept all", "I agree", "Accept"]:
                    try:
                        btn = page.get_by_role("button", name=label, exact=False)
                        if btn.count() > 0:
                            btn.first.click(timeout=3000)
                            page.wait_for_timeout(1500)
                            break
                    except Exception:
                        pass
                # The agenda list itself renders progressively via JS/XHR
                # after the page loads -- run #4 confirmed this is real: the
                # page loaded (no consent wall, no error), but a single
                # fixed 2s pause after load only ever captured the header
                # plus the very first entry (5390 chars) instead of the
                # multi-week list a human sees. Poll until the visible text
                # stops growing instead of guessing a fixed delay.
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
            # Unreachable (the loop above always returns or raises), but
            # keeps this function's control flow obviously exhaustive.
            raise last_error
        finally:
            browser.close()


def _claude_extract_events_once(client, org_name, source_text, extra_context=""):
    """A single extraction attempt -- see claude_extract_events() for the
    retry wrapper around this."""
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
            raw = block.input.get("events", [])
            if not isinstance(raw, list):
                log(f"  ! record_events tool call's 'events' field wasn't a list (got "
                    f"{type(raw).__name__}) -- treating as no events extracted")
                return []
            # Run #6: for Grassroots Ecology specifically, this came back as
            # 1071 plain strings instead of event objects -- the schema
            # requires objects, but a forced tool call isn't a hard
            # guarantee Claude always honors every field's declared type.
            # Filter those out here, at the source, rather than letting bad
            # entries travel downstream into date-parsing/geocoding.
            valid = [ev for ev in raw if isinstance(ev, dict)]
            if len(valid) != len(raw):
                log(f"  ! record_events returned {len(raw) - len(valid)} non-object entr"
                    f"{'y' if len(raw) - len(valid) == 1 else 'ies'} out of {len(raw)} -- dropped, kept {len(valid)}")
            return valid
    return []


# Source text shorter than this is plausibly just a header/empty-state with
# genuinely nothing to extract -- not worth a retry. Above it, a 0-event
# result is suspicious enough to be worth a second, independent attempt.
_EXTRACTION_RETRY_MIN_SOURCE_CHARS = 800


def claude_extract_events(client, org_name, source_text, extra_context=""):
    """One (or, if it looks suspicious, two) Claude call(s), forced through
    the record_events tool, returning a list of raw event dicts
    (name/date/start/end/location/bookingUrl) -- no lat/lng yet.

    2026-09-18: run #10's log showed Pacific Beach Coalition's agenda view
    rendering fine -- 4841 chars, the exact same size as run #7's, which DID
    extract 30 events from what was confirmed to be the same real calendar
    content -- yet this run's extraction came back with 0 raw events. Since
    nothing in this function or its caller changed between those two runs,
    and the source text was substantively the same real content both times,
    the most likely explanation is a one-off miss in the model's own
    response for that call, not a structural bug -- these are inherently a
    little stochastic, and a forced tool call can occasionally come back
    genuinely (if incorrectly) empty. Rather than trying to root-cause a
    single non-reproducible bad response further, treat "0 events from a
    substantial amount of source text" as suspicious enough to retry once,
    the same way the PBC page-load timeout gets one retry -- cheap
    insurance against a whole org silently coming back empty for a week
    over what was probably just a bad roll. Also log more than the previous
    200-char preview when this happens, so a *repeat* failure (a real
    content/prompt problem, not a fluke) is diagnosable from the log alone
    next time instead of requiring this level of run-log archaeology again.
    """
    events = _claude_extract_events_once(client, org_name, source_text, extra_context)
    if not events and len(source_text) >= _EXTRACTION_RETRY_MIN_SOURCE_CHARS:
        log(f"  ! extraction returned 0 events from {len(source_text)} chars of source text "
            f"for {org_name} -- retrying once in case that was a one-off miss "
            f"(source text starts: {source_text[:500]!r})")
        events = _claude_extract_events_once(client, org_name, source_text, extra_context)
        if not events:
            log(f"  ! retry also returned 0 events for {org_name} -- likely a real "
                f"content/prompt issue this time, not a fluke")
    return events


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


def _build_geocode_attempts(location_text):
    """Build an ordered list of (label, query) attempts to try against
    Nominatim for a single location string. Most-specific first, broadest
    last -- stop at the first one that resolves.

    Run #7's log showed exactly why a single "loosen to last 2 segments"
    fallback isn't enough: for
    "Pacifica State Beach, 1416 9th St, California 95814, United States"
    (a genuinely corrupted address -- 95814 is a Sacramento zip code, not
    Pacifica's -- an artifact of how the source text got extracted), the
    last-2-segments fallback produces "California 95814, United States",
    which drops the city entirely and still fails. Verified live against
    Nominatim's own API that the fix isn't a smarter loosening of THIS
    address -- it's using the venue name alone: "Pacifica State Beach, CA"
    resolves immediately to the actual beach. Same result for "Montara State
    Beach, CA" (another of that run's failures). Named parks/beaches are
    already in OpenStreetMap's database under their own name, so when the
    surrounding address text is noisy or wrong, the venue name by itself is
    often *more* reliable than trying to fix the address around it."""
    attempts = []
    full = location_text
    def with_usa(s):
        # Append ", USA" only if the string doesn't already end in something
        # that names the country -- joining segments that already include a
        # trailing "USA" segment (common once the source address is fully
        # qualified) was otherwise producing queries like "...CA 94015, USA,
        # USA". Harmless to Nominatim either way, but noisy and worth doing
        # properly.
        return s if "usa" in s.lower() else f"{s}, USA"

    if not any(tok in full.lower() for tok in ("ca", "california", "usa")):
        full = with_usa(full)
    attempts.append(("full text", full))

    parts = [p.strip() for p in location_text.split(",") if p.strip()]

    if len(parts) > 1:
        # Last 3 segments -- catches "<street>, <city>, <state> <zip>, USA"
        # shapes where the last-2 fallback below would drop the city and
        # keep only the state/zip.
        if len(parts) > 2:
            attempts.append(("last 3 segments", with_usa(", ".join(parts[-3:]))))

        # Last 2 segments -- typically "<city>, <state>". Tried before
        # "venue name only" below: this still keeps the city, which matters
        # a lot -- see the Redwood City incident in the note below.
        attempts.append(("last 2 segments", with_usa(", ".join(parts[-2:]))))

        # Venue name alone (first segment) + state -- catches corrupted or
        # overly-specific addresses where the place name itself is what's
        # actually in the map database (e.g. "Pacifica State Beach, CA").
        # Run #9's log caught why this can't be tried before the
        # city-preserving fallbacks above, and why it must be skipped
        # entirely when the first segment is a numbered street address
        # rather than an actual place name: for "1400 Broadway St, Redwood
        # City, CA 94063", "1400 Broadway St, CA, USA" doesn't fail -- it
        # resolves to a DIFFERENT, wrong Broadway St in Santa Monica/LA
        # (34.0228, -118.4839), silently. A wrong-but-successful geocode is
        # worse than a failed one, since nothing downstream flags it. Live
        # Nominatim checks confirmed: the full address (even with ", USA"
        # appended) returns zero results for that address, but "Redwood
        # City, CA 94063, USA" (the last-2-segments attempt) correctly
        # resolves to Redwood City. So: only offer "venue name only" when
        # the first segment doesn't start with a digit -- a real venue name
        # never does, and a bare street number+name is too ambiguous to
        # geocode without its city.
        if not re.match(r"^\d", parts[0]):
            attempts.append(("venue name only", f"{parts[0]}, CA, USA"))

    # De-duplicate (case-insensitive) while preserving order -- short
    # addresses can make several of the above identical.
    seen = set()
    deduped = []
    for label, q in attempts:
        qkey = q.lower()
        if qkey not in seen:
            seen.add(qkey)
            deduped.append((label, q))
    return deduped


def geocode(location_text):
    """Resolve a location string to (lat, lng). Order of attempts:
      1. LOCATION_OVERRIDES -- known recurring sites we've manually verified.
      2. A series of Nominatim (OpenStreetMap) queries built by
         _build_geocode_attempts(), from most-specific (the full address) to
         broadest (city + state) -- see that function's docstring for why
         more than one fallback shape is needed.
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

    for label, query in _build_geocode_attempts(location_text):
        try:
            result = _nominatim_lookup(query)
        except Exception as e:
            log(f"  ! geocoding attempt ({label}) failed for '{location_text}': {e}")
            continue
        if result:
            if label != "full text":
                log(f"  (geocoded '{location_text}' via {label}: '{query}')")
            _geocode_cache[key] = result
            return result

    # Don't log the overall failure here -- the caller (main()) already logs
    # "could not geocode '<location>' -- skipping this event" for every
    # event this returns (None, None) for; logging it again here would just
    # duplicate that line.
    _geocode_cache[key] = (None, None)
    return None, None


def slugify_for_log(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


# 2026-09-18e: the site-name -> form-link word-matching approach that used
# to live here (_extract_site_form_links / _match_site_form_link, plus the
# _SITE_FORM_STOPWORDS/_SITE_ANCHOR_STOPWORDS machinery around it) is gone.
# The site owner walked through the calendar by hand and showed that PBC's real
# registration links don't come from the small site->form table on the
# calendar page at all -- they're embedded directly in each individual
# Google Calendar EVENT's own description (click an event, there's a
# "Register online to volunteer" link right there), which the table-matching
# approach never had access to and could only approximate by fuzzy word
# overlap. That approximation was real work (see the CHANGELOG entries this
# replaces) but it was solving the wrong problem: matching against a
# generic ~11-row table of recurring sites, when the actual authoritative,
# per-EVENT link was sitting one click away the whole time -- confirmed
# live for "Calera Creek Habitat Restoration" (an event that was never in
# that table and always correctly came back with no link) and matches the
# exact real form URL the site owner copied out by hand. See
# _extract_registration_link_from_description() and
# fetch_google_calendar_events_via_api() below for the real mechanism.


def _decode_google_url_shim(href):
    """Google Calendar's own rich-text description editor wraps a pasted
    link in its own https://www.google.com/url?q=<real-url>&... redirect
    shim -- confirmed live that this is baked into the raw description text
    Google's API itself returns, not just something the embed widget adds
    at render time. Decode straight to the real target instead of depending
    on that redirect service behaving the same way for a plain server-side
    request with no browser session behind it."""
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(href)
    if parsed.netloc in ("www.google.com", "google.com") and parsed.path == "/url":
        qs = parse_qs(parsed.query)
        q = qs.get("q")
        if q:
            return q[0]
    return href


_REGISTRATION_LINK_KEYWORDS = re.compile(r"\b(register|registration|sign[\s-]?up|rsvp|volunteer)\b", re.I)
_KNOWN_SIGNUP_DOMAINS = re.compile(
    r"(docs\.google\.com/forms|forms\.gle|eventbrite\.com|signupgenius\.com|surveymonkey\.com)", re.I
)


def _extract_registration_link_from_description(description_html):
    """Find the real, event-specific sign-up link inside a Google Calendar
    event's own description HTML, if one is there.

    Live-confirmed (Pacific Beach Coalition, Surfrider) that every org here
    wraps its registration link in a "friendly" anchor like
    '<a href="...">Register online to volunteer</a>' or
    '<a href="..."><b>REGISTER HERE</b></a>' -- note the second one nests a
    <b> tag INSIDE the <a>, so a naive "<a[^>]*>([^<]*)</a>" pattern (which
    stops at the first '<') never matches it at all; this one captures
    everything up to the closing </a> non-greedily and strips inner tags
    from the captured text afterward instead. Scores each link in the
    description by whether its visible text looks like a registration
    prompt and/or its target is a known sign-up-form domain, and returns
    the best-scoring one -- or None if nothing scores, same "never invent a
    link" principle as the rest of this pipeline."""
    if not description_html:
        return None
    import html as html_module

    candidates = []
    for m in re.finditer(r'<a\s+[^>]*href="([^"]*)"[^>]*>(.*?)</a>', description_html, re.I | re.S):
        href, inner_html = m.group(1), m.group(2)
        text = re.sub(r"<[^>]+>", " ", inner_html)
        text = html_module.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        target = _decode_google_url_shim(html_module.unescape(href))
        score = 0
        if _REGISTRATION_LINK_KEYWORDS.search(text):
            score += 2
        if _KNOWN_SIGNUP_DOMAINS.search(target):
            score += 1
        if score > 0:
            candidates.append((score, target))
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    return candidates[0][1]


# A few Pacific Beach Coalition events whose Google Calendar entry has no
# usable location text at all -- confirmed live via the Calendar API: the
# structured `location` field is empty (or, for one event, a bare
# maps.app.goo.gl link with no address text, handled separately below by
# reading the description's own "Where to Meet:" line first), and there's
# no "Where to Meet:" fallback in the description either. These are
# recurring, named sites we already know the general area of from this
# org's own site -- falls back to this ONLY after both the API location
# field and the description's "Where to Meet:" line come up empty, and
# still goes through the normal geocode() fallback chain like any other
# location string (not a hardcoded coordinate), so a bad guess here still
# can't silently plot a wrong pin the way LOCATION_OVERRIDES could.
_PBC_EVENT_NAME_LOCATION_FALLBACKS = [
    (re.compile(r"foster city", re.I), "Foster City, CA"),
    (re.compile(r"mussel rock", re.I), "Mussel Rock, Daly City, CA"),
    (re.compile(r"linda mar", re.I), "Linda Mar, Pacifica, CA"),
    (re.compile(r"coastal cleanup day", re.I), "Pacifica, CA"),
]

_WHERE_TO_MEET_RE = re.compile(r"Where to Meet:\s*([^<\n]+)", re.I)


def _pick_event_location(item):
    """Best available location text for one Calendar API event item.

    Order of attempts:
      1. The structured `location` field, if it's real text (not empty,
         not itself a bare URL -- confirmed live that Pacific Beach
         Coalition sometimes puts a maps.app.goo.gl short link there
         instead of an address, e.g. for "Calera Creek Habitat
         Restoration", which geocode() obviously can't do anything with).
      2. A "Where to Meet: ..." line inside the event's own description,
         if present (confirmed live: that's exactly where Calera Creek's
         real address-like text actually lives, since its `location` field
         is just the maps link above).
      3. A small set of known-recurring-site name fallbacks for the
         handful of events that have neither (see
         _PBC_EVENT_NAME_LOCATION_FALLBACKS above)."""
    loc = (item.get("location") or "").strip()
    if loc and not re.match(r"^https?://", loc):
        return loc

    description = item.get("description") or ""
    m = _WHERE_TO_MEET_RE.search(description)
    if m:
        import html as html_module

        return html_module.unescape(m.group(1)).strip()

    name = item.get("summary") or ""
    for pattern, fallback_loc in _PBC_EVENT_NAME_LOCATION_FALLBACKS:
        if pattern.search(name):
            return fallback_loc

    return loc  # possibly still empty or a bare URL -- geocode() will cleanly skip it, same as any event with no usable location today


def _format_ampm(dt):
    """'9:00 AM' style -- matches the "H:MM AM/PM" convention the rest of
    this pipeline already uses (previously produced by Claude's own
    extraction), so output looks identical whichever path an event came
    through."""
    return dt.strftime("%I:%M %p").lstrip("0")


def _google_calendar_item_to_raw_event(item):
    """Convert one Calendar API v3 event item into this script's normal raw
    event dict shape (the same shape claude_extract_events() produces), so
    everything downstream in main() -- date filtering, geocoding, bookingUrl
    verification -- works unchanged regardless of which path an event came
    through."""
    if item.get("status") == "cancelled":
        return None
    name = (item.get("summary") or "").strip()
    if not name:
        return None

    start = item.get("start") or {}
    end = item.get("end") or {}
    if "date" in start:
        # All-day event (e.g. "CA Coastal Cleanup Day") -- no specific time.
        date_str = start["date"]
        start_str = "All day"
        end_str = None
    elif "dateTime" in start:
        start_dt = datetime.fromisoformat(start["dateTime"])
        date_str = start_dt.strftime("%Y-%m-%d")
        start_str = _format_ampm(start_dt)
        end_str = None
        if "dateTime" in end:
            end_str = _format_ampm(datetime.fromisoformat(end["dateTime"]))
    else:
        return None

    return {
        "name": name,
        "date": date_str,
        "start": start_str,
        "end": end_str,
        "location": _pick_event_location(item),
        "bookingUrl": _extract_registration_link_from_description(item.get("description")),
    }


GOOGLE_CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"


def fetch_google_calendar_events_via_api(calendar_id, time_min, time_max):
    """Pull this calendar's events directly from Google's own public
    Calendar API v3 (read-only, via GOOGLE_CALENDAR_API_KEY) instead of
    scraping the embed widget's rendered HTML or its ICS/agenda-view
    fallbacks. Structured JSON, not text a model has to interpret -- exact
    dates/times/locations, and each event's own real registration link
    straight from its description field (see
    _extract_registration_link_from_description), instead of trying to
    reconstruct it after the fact from a table or free text. This is the
    officially documented public endpoint (developers.google.com/workspace/
    calendar/api/v3/reference/events/list) -- requires the calendar's own
    sharing settings to have "Make available to public" / "See all event
    details" turned on, which both Surfrider's and Pacific Beach
    Coalition's calendars already do (confirmed live: that's the same
    setting that lets their public embed widgets show full event details
    to anonymous visitors today)."""
    from urllib.parse import quote

    url = f"{GOOGLE_CALENDAR_API_BASE}/calendars/{quote(calendar_id, safe='')}/events"
    params = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "timeZone": "America/Los_Angeles",
        "maxResults": 250,
        "timeMin": time_min,
        "timeMax": time_max,
        "key": GOOGLE_CALENDAR_API_KEY,
    }
    resp = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json().get("items", [])


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

    if GOOGLE_CALENDAR_API_KEY:
        try:
            today = date.today()
            # Wrapped a day wider on each side than we actually need -- main()
            # re-filters to the exact date range afterward anyway, this just
            # guards against a UTC/Pacific timezone boundary off-by-one.
            time_min = (today - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
            time_max = (today + timedelta(days=LOOKAHEAD_DAYS + 1)).strftime("%Y-%m-%dT00:00:00Z")
            items = fetch_google_calendar_events_via_api(cal_id, time_min, time_max)
            raw_events = [ev for item in items if (ev := _google_calendar_item_to_raw_event(item))]
            with_links = sum(1 for ev in raw_events if ev.get("bookingUrl"))
            log(f"  fetched {len(raw_events)} event(s) directly from the Google Calendar API "
                f"({with_links} with their own real registration link) -- no AI extraction needed")
            return raw_events
        except Exception as e:
            log(f"  ! Google Calendar API fetch failed ({e}) -- falling back to the "
                f"older AI-extraction pipeline for this run")
            # falls through to the pipeline below
    else:
        log("  (GOOGLE_CALENDAR_API_KEY not set -- using the older AI-extraction pipeline; "
            "add that secret for exact structured data straight from Google instead, see the "
            "2026-09-18e CHANGELOG entry above)")
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

    raw_events = claude_extract_events(client, org_cfg["org"], source_text)
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

    if not detail_urls:
        # No detail pages at all -- fall back to extracting straight from the
        # listing page's own text so a broken detail-link pattern degrades to
        # "no bookingUrl" rather than "no events".
        raw_events = claude_extract_events(
            client, org_cfg["org"], f"--- LISTING PAGE ({org_cfg['listing_page']}) ---\n{listing_html[:20000]}\n"
        )
        log(f"  Claude extracted {len(raw_events)} raw event(s) from the listing page alone "
            f"(before date filtering/geocoding)")
        return raw_events

    # One Claude call PER detail page rather than one call across all of them
    # combined. The site owner reported (2026-09-18) that Grassroots Ecology's events
    # were coming through with no bookingUrl at all, unlike Save The Bay's --
    # the difference: when every detail page's text is blended into a single
    # blob and Claude is asked to extract every event at once, matching each
    # event back to the one URL among several that it actually came from is
    # an inference Claude has to get right on its own, and evidently wasn't
    # for this org. Processing one page at a time removes that inference
    # entirely: whatever event(s) come out of THIS call can only have come
    # from THIS url, so bookingUrl is set programmatically afterward, not
    # trusted from the model's own output. This also directly delivers what
    # the site owner asked for -- each event's "I will help!" button goes to that
    # event's own page on the org's site (e.g.
    # https://www.grassrootsecology.org/event-calendar/2026/09/19/coastal-
    # cleanup-day-redwood-city), the same "volunteer tab" they described
    # clicking through to by hand.
    raw_events = []
    for url in detail_urls:
        try:
            detail_html = http_get(url)
        except Exception as e:
            log(f"  ! failed to fetch detail page {url}: {e}")
            continue
        source_text = (
            f"--- LISTING PAGE ({org_cfg['listing_page']}), for date/context only ---\n"
            f"{listing_html[:8000]}\n"
            f"\n--- DETAIL PAGE ({url}) -- this describes exactly ONE event ---\n"
            f"{detail_html[:8000]}\n"
        )
        try:
            page_events = claude_extract_events(client, org_cfg["org"], source_text)
        except Exception as e:
            log(f"  ! Claude extraction failed for detail page {url}: {e}")
            continue
        for ev in page_events:
            if isinstance(ev, dict):
                ev["bookingUrl"] = url
        raw_events.extend(page_events)

    log(f"  Claude extracted {len(raw_events)} raw event(s) from {len(detail_urls)} detail page(s), "
        f"one call per page (before date filtering/geocoding)")
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
            # Run #6 crashed the ENTIRE script here: Claude's tool response for
            # Grassroots Ecology came back with `events` containing 1071
            # entries that were plain strings, not event objects (some
            # degenerate/repetitive echo of the source text rather than a
            # real extraction -- the *why* wasn't fully diagnosable from the
            # log, but the shape of the bad data was clear: `ev["date"]`
            # threw TypeError, a class this loop didn't catch). Because that
            # was outside any try/except, it killed the whole run before
            # events.json was ever written -- throwing away Surfrider and
            # Save The Bay's perfectly good results along with it, not just
            # Grassroots Ecology's bad ones. Two layers of defense now: skip
            # anything that isn't actually an event object, and never let a
            # single malformed record from one org take down every org.
            if not isinstance(ev, dict):
                log(f"  ! skipping malformed (non-object) event entry: {ev!r:.100}")
                continue
            try:
                try:
                    ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
                except (ValueError, KeyError, TypeError):
                    log(f"  ! skipping event with unparsable date: {ev}")
                    continue
                if not (today <= ev_date <= cutoff):
                    # This used to be a silent `continue` -- when it's every
                    # single extracted event (as happened for Grassroots
                    # Ecology in run #4, all 7 dropped here with zero
                    # explanation in the log), there was no way to tell this
                    # apart from a geocoding problem or Claude finding
                    # nothing at all. Always log why.
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
            except Exception as e:
                # Belt-and-suspenders: whatever this is, one bad record must
                # never take the whole run down with it.
                log(f"  ! unexpected error cleaning event {ev!r:.200} -- skipping it: {e}")
                continue

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
