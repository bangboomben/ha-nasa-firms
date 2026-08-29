# v0.9.1 — the sky comes back sooner

Text for the GitHub release.

## Release body

Three fixes to the satellite observation times from v0.9.0. Nothing is renamed,
nothing needs reconfiguring, and fire data is untouched.

### Fixed

- **A short CelesTrak outage no longer costs a whole day.** Any failed request
  used to mean a 24-hour wait. Now 429, 500, 502, 503 and 504 wait about an
  hour; 403, 404 and anything unrecognised still wait a day; a `Retry-After`
  header decides in between. Their policy still holds — a failed request is
  never repeated on the update cycle.
- **One repair notice, not one per config entry.** The orbital elements are
  fetched once and shared, so an outage was never about a single location.
  Two entries used to mean the same warning twice.
- **A timed-out request says what happened.** It logged `CelesTrak request
  failed:` with nothing after the colon.

Nothing to clean up when you update: the old notices do not survive a restart,
and updating requires one.

Fire data courtesy of NASA FIRMS. Wind data from MET Norway and place names
from GeoNames, both used under CC BY 4.0.
