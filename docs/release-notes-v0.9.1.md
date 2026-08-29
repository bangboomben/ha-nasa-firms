# v0.9.1 — the sky comes back sooner

Text for the GitHub release.

## Release body

Three fixes to the satellite observation times that shipped in v0.9.0. Nothing
is renamed, nothing needs reconfiguring, and fire data is untouched throughout.

### An outage that ended in minutes kept the times missing for a day

- CelesTrak was briefly unavailable twice in two days on the maintainer's
  instance — HTTP 503 both times, answering normally again within the half
  hour.
- v0.9.0 treated every failed request alike: a full 24-hour cooldown before
  trying again. So the observation times stayed missing all day, and the
  repair notice sat there with them, long after the outage had ended.
- The wait now depends on what the answer meant. **429, 500, 502, 503, 504** —
  their service is busy, and that passes: one hour. **403, 404, or any status
  not recognised** — the refusal is aimed at this client, and asking again soon
  changes nothing: still a full day.
- If CelesTrak sends a `Retry-After`, that decides, clamped between those two
  bounds — a five-second header cannot turn this into a retry loop, and a
  week-long one cannot park the observation times indefinitely.
- Their usage policy is still respected: a failed request is never repeated on
  the update cycle.

### One notice, not one per config entry

- The orbital elements are fetched once and shared by every entry, so an
  outage is never about a single location. v0.9.0 raised a separate warning for
  each one anyway — two entries meant the same notice twice, five meant it five
  times.
- It is now a single notice, and its text says that it covers every location.
- Nothing to clean up when you update: these notices do not survive a restart,
  and updating the integration requires one.

### A timeout that said nothing

- A timed-out request logged `CelesTrak request failed:` with nothing after the
  colon — `str()` of a `TimeoutError` is empty. It now names both the timeout
  and the limit it hit.

### Also

The separation the feature was built around held throughout: across every one
of these outages, FIRMS fire data kept updating exactly as normal. Only the
previous and next observation were missing.

Fire data courtesy of NASA FIRMS. Wind data from MET Norway and place names
from GeoNames, both used under CC BY 4.0.
