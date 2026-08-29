# CLAUDE.md — ha-nasa-firms

Home Assistant custom integration `nasa_firms`: NASA FIRMS satellite wildfire
detections as native HA entities. Public repo — everything committed here is
published.

## Goal

Public release via HACS (custom repo → default store), long-term a Home
Assistant Core submission. Donation-funded ("coffee tip jar"), never paid.

## Architecture rules

- **`custom_components/nasa_firms/api.py` must stay free of Home Assistant
  imports.** It is the protocol layer (WFS client, parsing, haversine, bbox,
  clustering) that gets extracted to a PyPI package for the Core submission —
  Core requires protocol logic in a published library. Everything else is
  thin HA glue (coordinator, config flow, two platforms).
- Model after the existing Core geo-feed family (`nsw_rural_fire_service_feed`,
  `qld_bushfire_feed`, `geo_json_events`) — same patterns, feed-specific value.
- The MAP_KEY lives in config entry data only. Never put it in entry titles,
  logs, code, docs, or screenshots.
- Brand icon ships in `custom_components/nasa_firms/brand/` (icon.png 256,
  icon@2x.png 512) — the HA 2026.3+ mechanism. The home-assistant/brands repo
  no longer accepts custom-integration icons (our PR #10856 was auto-closed);
  do not resubmit there.

## Origin & feedback backlog

Grew out of a zero-custom-code `geo_json_events` setup, documented in
https://community.home-assistant.io/t/wildfire-monitoring-with-nasa-firms-live-fire-map-proximity-alerts-zero-custom-components/1016485
Thread feedback drives the roadmap:

- BBOX `cos(radians(lat))` footgun → solved (radius + map picker)
- Confidence/FRP invisible to `geo_json_events` → solved (filters + attributes)
- 5-min polling vs. NASA's 15-min refresh → solved (`UPDATE_INTERVAL`)
- Persistent heat sources (factories) cause false alarms → **open, v0.2 lead
  candidate**: auto-ignore locations detected across many consecutive days,
  plus manual ignore zones (coordinate + radius) in the options flow. Raised
  independently by two users (posts #4/#5, #7) — the strongest differentiator
  left over the plain YAML setup.
- Map markers carry no intensity signal → **open, and the mechanism is now
  proven**: colour by FRP. A user solved it with Node-RED + nathan.gs map-card
  + auto-entities + z-index layering (post #7); we should manage it natively.
  The `entity_picture` route was verified against the frontend source while
  building v0.3.0 and **works** — see the marker note below. Colouring is
  therefore a small step: swap the one constant in `geo_location.py` for a
  handful of pre-built pictures picked by FRP. What is *not* settled and needs
  a maintainer decision first: the thresholds. Fallback/companion: a plain
  `severity` attribute (low/moderate/high/extreme) so card-mod and
  auto-entities users can style without parsing raw FRP.
- **Map card marker rendering (verified 2026-07-27, frontend `dev`).** The card
  reads an entity's `icon` only when the entity carries `label_mode: icon`, and
  that can only be set on entities listed one by one. Anything arriving through
  `geo_location_sources` is a bare entity id, so it can never carry it — those
  markers fall back to `entity_picture`, and without one to the first letters of
  the friendly name ("Wildfire hotspot 43.6/3.9" rendered as "Wh4"). Confirmed
  in `ha-map.ts`/`ha-entity-marker.ts`: the picture wins over the name and is
  applied as a CSS `background-image` in a 48px circle. Hence the flame shipped
  in v0.3.0 as a base64 SVG data URI (not percent-encoded — it lands in an
  unquoted `url()`), full-bleed 24x24, one shared module constant. `_attr_icon`
  stays for the entity list and more-info dialog, which do use it.
- **Map card clustering (verified 2026-07-29, re-checked 2026-08-27, frontend
  `dev`).** The core card
  merges markers within 40 px into one disc carrying a count
  (`markerClusterGroup({maxClusterRadius: 40})` in `ha-map.ts`), and it is the
  default: `this._clusterMarkers = this._config.cluster ?? true`
  (`hui-map-card.ts`). At the `default_zoom: 8` the README recommends, 40 px is
  ~18 km at 43.6° N — so in a busy area the intensity colours of v0.4.0 collapse
  into one `--primary-color` bubble, exactly where they matter most. `cluster:
  false` turns it off and exists only from **HA 2025.6** (frontend #25429);
  clustering itself landed in 2025.3 (#24244), so 2025.3–2025.5 got it with no
  way out — no longer our problem, `hacs.json` has required 2025.6.0 since
  v0.5.0. **Since some 2026.x release the card also carries a toggle button**
  (`_toggleClusterMarkers`, shown whenever more than one entity is on the
  map), so a user can switch grouping off without touching YAML — which makes
  it a one-tap diagnostic when someone reports odd marker behaviour. The built-in **Map panel**
  renders `<ha-map>` without `clusterMarkers`, i.e. at the component default
  `true`, and offers no setting — it always clusters. With clustering off, draw
  order is Leaflet's: `DecoratedMarker` extends `Marker` and nothing in the
  frontend sets `zIndexOffset`, so z-index follows the marker's y pixel
  position — **the southernmost fire draws on top, never the strongest**.
  Neither is reachable from the integration; both are documentation.
- **Stale markers are not a removal bug (verified 2026-08-27, frontend `dev`).**
  Chased for the community thread (RedKing, posts #49/#51: eight hotspots but
  eleven markers, cured by restarting Fully Kiosk). The removal path is sound
  at all three links, so **do not go looking for a data-side bug here again**:
  `_drawEntities` in `ha-map.ts` clears every marker, every zone **and the
  whole cluster group** (`this._mapCluster.remove()`) before redrawing;
  `hui-map-card.shouldUpdate` returns true on any `hass.states` change while
  `geo_location_sources` is set; and `willUpdate` rebuilds the entity array
  behind a `deepEqual`, and that new array identity is what
  `changedProps.has("entities")` picks up to trigger the redraw. A vanished
  fire does reach the map. What is left is a rendering asymmetry: sensor
  values are Lit DOM updates, while Leaflet only repaints when driven — which
  is why the component carries a `ResizeObserver` on `invalidateSize` and a
  `visibilitychange` listener at all. Unproven from here; it needs the
  reporter to say whether a plain desktop browser goes stale too.
  Incidental drift worth knowing: `ha-map` now takes `_states` from a Lit
  context (`@consume`) rather than a `hass` property, so the July notes above
  describe an older shape in that one respect.
- **Dark mode does not touch our markers (verified 2026-07-29).** `ha-map.ts`
  defines `--map-filter: invert(0.9) hue-rotate(170deg) …` on `#map.forced-dark`
  but applies it to `.leaflet-tile-pane` only. Markers sit in
  `.leaflet-marker-pane`, a sibling pane, so the band colours render as
  authored. pyspilf's `theme_mode: light` workaround (post #30) is a property of
  his third-party card — do not copy it into our docs.
- ~~The configured radius has no upper bound~~ → **DONE across v0.4.0/v0.5.0**:
  500 km cap in the config flow, `truncated` attribute, and a Repairs notice
  when FIRMS caps the response anyway.
- **One map card mixes all config entries** → documented, not fixed. `nasa_firms`
  is a single source name for every entry, so a card using
  `geo_location_sources` plots them all together — two distant entries give a
  useless zoomed-out map. The core card cannot filter geo_location by entry;
  the README says so and points at per-entity cards as the way out.
- ~~The nearest-hotspot sensor carries only the distance~~ → **DONE in v0.2.0**
  (post #12). Ships `nearest_entity_id`, `bearing` and `direction`; `latitude`
  /`longitude` deliberately not duplicated, they are reachable through the
  entity id. Note the startup race fixed along the way: entity ids only exist
  after HA adds the entities, so the geo_location platform nudges the
  coordinator listeners ~2 s after adding — without that the sensor renders
  before any id exists and the attribute stays `None` for a full refresh cycle.
  That nudge shipped with a bug fixed in v0.3.0: the `async_call_later` target
  needs `@callback`, otherwise HA runs it in an executor thread and every state
  write behind it raises `async_write_ha_state from a thread other than the
  event loop`. Any future `async_call_later`/`async_track_*` target here needs
  the same decorator.
- **Attributes stay non-configurable** (maintainer decision, 2026-07-28). A
  multi-select "which attributes do you want" in the config flow was considered
  and rejected: attributes are invisible until looked for and cost nothing,
  optional ones force an "if you enabled this" caveat onto every documented
  template, and HA convention reserves configuration for things that change
  behaviour or cost (poll interval, filters, satellite count = API calls) —
  Core review would flag it. Recorder growth is the one real concern and users
  can already exclude attributes via `recorder:`. The answer to "is this getting
  bloated?" is to add fewer attributes, not to make them switchable.
- ~~Wind direction at the fire~~ → **DONE in v0.3.0** (post #14). Ships
  `wind_bearing`, `wind_direction` and `wind_speed` on the nearest sensor, from
  met.no Locationforecast, one request per 15-min cycle for the nearest cluster
  only. Degrees *and* compass point on purpose, mirroring `bearing`/`direction`:
  the degrees are what the upwind/downwind template needs, the compass point is
  what goes on a dashboard — dropping the degrees would break the feature's
  actual purpose.
  **Design line held and not up for revision:** facts only, no risk score, no
  "downwind, you are fine" — wind turns and slope/fuel matter as much. The
  upwind/downwind maths lives in the README as a user template, with its limits
  spelled out.
  **Where exactly that line sits (maintainer decision, 2026-07-27).** The README
  dashboard card was rewritten to explain itself — a bare `nominal` or `1.19 MW`
  means nothing to a first-time user — and its wind sentence now says the smoke
  is pushed *towards* / *sideways to* / *away from* you instead of printing an
  angle. That is deliberately as far as the wording goes: it describes the
  current wind vector in words, the caveat sits on the next line, and nowhere
  does the card say "safe", score the risk or forecast where the smoke will end
  up. "Away from you" reads close to reassurance and was weighed on exactly that
  point before shipping. Anything beyond it — a verdict, a colour-coded threat
  level, a "no action needed" — stays out. The angle-based phrasing is kept in
  the README recipe for anyone who wants no interpretation at all.
  met.no's ToS shapes `MetNoClient`: identifying User-Agent with
  the repo as contact, `Expires` respected before any refetch,
  `If-Modified-Since` for cheap 304s, coordinates rounded to 2 decimals (~1 km,
  well inside their 4-decimal cap, and it keeps the cache warm as the
  representative pixel jitters). 403/429 back off for an hour. Attribution is
  CC BY 4.0 and only shown while wind data is actually present.
- Superseded history of the wind decision (kept so it is not re-litigated):
  First ruled out because the obvious implementation uses `wind_bearing` from
  the user's own `weather.*` entity, i.e. the wind at *their* house applied to a
  fire tens of km away — meaningless in mountains. Maintainer then raised the
  better framing: query the wind **at the fire's coordinates** (met.no
  Locationforecast takes arbitrary lat/lon, no API key), which removes that
  objection entirely. Remaining concerns: one API call per fire (fine at 5
  hotspots, ugly at 100, plus met.no rate limits and User-Agent rules), and the
  bigger one — a dashboard saying "downwind, you're fine" is a safety claim
  satellite data cannot back, since wind shifts and slope/fuel matter as much.
  Asked pyspilf directly in the thread for his read before deciding. Either way
  `bearing` ships first as the enabler.
- ~~Two config entries share one `geo_location` source~~ → **answered in
  v0.4.0** with the `origin` attribute (entry coordinates on every fire) so
  `auto-entities` can filter; the trap and the recipe are in
  `docs/dashboard.md`. Per-entry source strings stay rejected — they would
  break every documented `nasa_firms` card.
- ~~Cluster IDs die when the centroid drifts across a 0.01° boundary~~ →
  **DONE in v0.4.0**: ids are carried forward by matching against the previous
  cycle, nearest-first, no id handed out twice. Memory only — a restart
  rebuilds them from coordinates, which is the gap 0.7.0's persisted state
  closes.

**Evaluated and rejected:** Gridware/GridScope pole sensors as an extra alert
source (posts #8/#10). B2B hardware sold to utilities, no public developer
API, deployments concentrated in the US; the Spanish user's own utility
confirmed no API access. Not a data channel we can consume — do not revisit
without new evidence.

**Sequencing (maintainer decision, 2026-07-27):** the three nearest-hotspot
attributes shipped as v0.2.0 because a user was waiting on them to retire his
own workaround. Everything else still waits for real installation feedback
rather than forum enthusiasm.

**Wind shipped as v0.3.0** — brief kept in `docs/TASK-wind-at-fire.md` as the
record of why it looks the way it does. It introduced the **first external data
source** into the integration, so it sets the precedent for the next one: the
client lives in `api.py` with its own error type, the coordinator swallows its
failures, and the fire data never depends on it. Any future second source
follows that shape.

**Relevance shipped as v0.6.0** (2026-07-30, live-tested on both entries;
decision record in `docs/v0.6.0-candidates.md`): wind for the N nearest fires
(`wind_fires`, default 3, cap 5 — the cap is met.no budget arithmetic, not a
technical limit), `smoke_offset` on every fire with a reading (**absent, not
None**, beyond the budget — presence is the filter), the `get_wind` action
(response-only, raises instead of returning nulls), `strongest_entity_id` on
the max-FRP sensor, and timeout messages that name themselves (str() of a
TimeoutError is empty — both clients caught it as a blank message). Two
decisions not to re-litigate: the met.no rate-limit backoff is
**client-global** on purpose, because a throttled client walking on to the
next fire is exactly what earns the permanent ban; and there is **no "most
intense" sensor entity** — one entity per ranking would triple the same
attributes, the pointer is the pattern.

## Conventions

- Code, comments, commit messages, README: English. Conversation with the
  maintainer: German.
- No AI co-author trailers in commit messages (maintainer decision,
  2026-07-27). AI assistance is disclosed once, in the README's
  "Credits & disclaimer" section.
- **Editorial rule (all channels — README, forum, Ko-fi, release notes):**
  never use active disasters as urgency marketing. The personal
  vacation-home-in-Greece story is the anchor; ongoing fires may be mentioned
  factually (e.g. as test validation), never as a sales hook, never with
  dramatic disaster imagery. Wherever the tip jar appears, keep the
  wildfire-relief pointer (IFRC / local Red Cross) next to it.
- Pure logic changes need a run of the smoke test: `python tests/smoke_test.py`.
  No dependencies and no pytest — it stubs `aiohttp`, loads `api.py` directly
  and exercises bbox/haversine/clustering plus the met.no client against a
  recorded payload in `tests/fixtures/`. It lives in the repo since v0.3.0;
  earlier versions kept it out of tree, which is why older notes say to ask.
- **Repair notices do not survive a restart (verified 2026-08-29, live
  instance).** Everything this integration raises is non-persistent, and HA
  keeps only a stub for those in `.storage/repairs.issue_registry` — loaded
  back as inactive, never shown again. The file is a graveyard by design: 162
  entries on the live box, most of them long-dead HACS `restart_required`
  notices. So **do not write migration code that deletes an old issue id**;
  v0.9.1 had one and it was measurably dead, because changing the integration
  requires a restart and the restart already cleared the notice. Checking the
  registry file proves nothing either way — ask HA itself over the WebSocket
  (`repairs/list_issues`), which is what showed the two notices were gone.
- Runtime verification happens on the maintainer's live HA instance before
  any release; this repo has no HA test harness yet (planned with the Core
  prep: pytest-homeassistant-custom-component).

## Release checklist

1. Bump `version` in `manifest.json`
2. Smoke test green, hassfest + HACS actions green
3. Tag `vX.Y.Z` + GitHub release (HACS installs from releases)
4. Update the forum thread for user-visible changes
