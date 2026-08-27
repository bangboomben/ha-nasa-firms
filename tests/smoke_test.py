"""Smoke test for the protocol layer.

`api.py` is the module destined for a standalone PyPI package, so it is the
one part of this integration that can be exercised without Home Assistant.
The test stubs `aiohttp`, loads `api.py` directly, and checks the pure
functions plus the met.no client against a recorded payload — never the
network.

Run it with a plain interpreter, no dependencies, no pytest:

    python tests/smoke_test.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# --- stub aiohttp so api.py imports on a bare interpreter -----------------
_aiohttp = types.ModuleType("aiohttp")


class _ClientError(Exception):
    """Stand-in for aiohttp.ClientError."""


_aiohttp.ClientError = _ClientError
_aiohttp.ClientSession = object
_aiohttp.ClientTimeout = lambda **kwargs: kwargs
sys.modules.setdefault("aiohttp", _aiohttp)

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "firms_api", _ROOT / "custom_components" / "nasa_firms" / "api.py"
)
api = importlib.util.module_from_spec(_spec)
# @dataclass resolves annotations through sys.modules, so register before exec.
sys.modules["firms_api"] = api
_spec.loader.exec_module(api)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "metno_compact.json").read_text())
# The fixture was recorded at 12:59Z on 2026-07-27; its steps run 12:00-16:00Z.
RECORDED_AT = datetime(2026, 7, 27, 12, 59, tzinfo=timezone.utc)
ORBIT_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "orbit_omm.json").read_text())

UTC = timezone.utc
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion."""
    if ok:
        print(f"  ok   {label}")
        return
    FAILURES.append(label)
    print(f"  FAIL {label} {detail}")


def near(a: float | None, b: float, tol: float = 0.01) -> bool:
    """Float comparison that reads well in a check() call."""
    return a is not None and abs(a - b) <= tol


# --- geometry -------------------------------------------------------------
def test_geometry() -> None:
    """bbox, haversine and bearing."""
    print("geometry")
    lat_s, lon_w, lat_n, lon_e = api.bbox_around(60.0, 10.0, 111.0)
    check("bbox spans 1 degree of latitude", near(lat_n - lat_s, 2.0))
    # cos(60 deg) = 0.5, so the box must be twice as wide in longitude.
    check("bbox widens longitude by 1/cos(lat)", near(lon_e - lon_w, 4.0, 0.02))
    check(
        "bbox does not feed degrees to cos()",
        lon_e > lon_w and lat_n > lat_s,
        f"got {(lat_s, lon_w, lat_n, lon_e)}",
    )

    # Cologne -> Thermi, ~1693 km great circle.
    d = api.haversine_km(50.94, 6.96, 40.54, 23.01)
    check("haversine Cologne->Thermi ~1693 km", near(d, 1693, 5), f"got {d:.1f}")
    check("haversine of a point with itself is 0", near(api.haversine_km(1, 2, 1, 2), 0))

    check("bearing due north is 0", near(api.bearing_deg(40, 23, 41, 23), 0, 0.1))
    check("bearing due east is 90", near(api.bearing_deg(40, 23, 40, 24), 90, 0.5))
    check("bearing due south is 180", near(api.bearing_deg(40, 23, 39, 23), 180, 0.1))
    check("cardinal(0) is N", api.cardinal(0) == "N")
    check("cardinal(348.75) wraps to N", api.cardinal(348.75) == "N")
    check("cardinal(315) is NW", api.cardinal(315) == "NW")
    check("cardinal(-90) is W", api.cardinal(-90) == "W")


def test_smoke_offset() -> None:
    """The angle between the smoke's travel and the fire-to-you line."""
    print("smoke offset")
    off = api.smoke_offset
    # Fire NE of you, wind at the fire from the NE: it blows the smoke SW,
    # straight along the line to you.
    check("wind blowing down the line reads 0", near(off(45.0, 45.0), 0.0))
    check("wind blowing away up the line reads 180", near(off(225.0, 45.0), 180.0))
    check("wind across the line reads 90", near(off(135.0, 45.0), 90.0))
    # The wording thresholds sit at 60 and 120 — the boundary must be exact.
    check("the card's towards/past boundary is exact", near(off(105.0, 45.0), 60.0))
    # The 0/360 wrap: a fire almost due north, wind from just past north.
    check("wraps across north", near(off(10.0, 350.0), 20.0))
    check("and symmetrically", near(off(350.0, 10.0), 20.0))
    check("negative bearings normalise", near(off(-90.0, 45.0), 135.0))
    check(
        "every combination lands in 0..180",
        all(
            0.0 <= off(float(w), float(f)) <= 180.0
            for w in range(0, 360, 30)
            for f in range(0, 360, 30)
        ),
    )


# --- FIRMS parsing --------------------------------------------------------
# --- regions --------------------------------------------------------------
def test_regions() -> None:
    """Which FIRMS service serves a point follows from the point."""
    print("regions")
    where = api.region_for
    check("southern France is Europe", where(43.60, 3.90) == "Europe")
    check("Thessaloniki is Europe", where(40.54, 23.01) == "Europe")
    check(
        "California is the contiguous USA",
        where(38.5, -121.5) == "USA_contiguous_and_Hawaii",
    )
    check("Tokyo is Russia_Asia", where(35.7, 139.7) == "Russia_Asia")
    check("Sydney is Australia_NewZealand", where(-33.9, 151.2) == "Australia_NewZealand")
    check(
        "Nairobi is Northern_and_Central_Africa",
        where(-1.3, 36.8) == "Northern_and_Central_Africa",
    )
    # Istanbul lies in Europe and in Russia_Asia at once: 6 degrees inside
    # Europe's eastern edge, 3 inside Russia_Asia's western one.
    check("an overlap goes to whichever the point is deeper inside", where(41.0, 29.0) == "Europe")
    # Greenland falls between Canada's eastern edge and Europe's western one.
    check("an uncovered spot is None, not a guess", where(72.0, -40.0) is None)
    check("open ocean is None too", where(0.0, -150.0) is None)
    check(
        "the name that returned HTTP 400 is gone",
        "Russia_and_Asia" not in api.REGION_BOUNDS,
    )
    check(
        "every extent is a sane box",
        all(
            w < e and s < n and -180 <= w and e <= 180 and -90 <= s and n <= 90
            for w, s, e, n in api.REGION_BOUNDS.values()
        ),
    )


def test_confidence() -> None:
    """VIIRS letters and MODIS percentages collapse onto one scale."""
    print("confidence")
    check("VIIRS 'h' is high", api.normalize_confidence("h") == "high")
    check("VIIRS 'nominal' is nominal", api.normalize_confidence("nominal") == "nominal")
    check("MODIS 95 is high", api.normalize_confidence(95) == "high")
    check("MODIS 50 is nominal", api.normalize_confidence(50) == "nominal")
    check("MODIS 10 is low", api.normalize_confidence(10) == "low")
    check("numeric string is parsed", api.normalize_confidence("85") == "high")
    check("None stays None", api.normalize_confidence(None) is None)
    check("garbage stays None", api.normalize_confidence("banana") is None)


def test_intensity() -> None:
    """FRP falls into four absolute bands, boundaries included."""
    print("intensity")
    band = api.intensity_for_frp
    check("100 MW is extreme", band(100.0) == "extreme")
    check("above 100 stays extreme", band(4321.0) == "extreme")
    check("50 MW is high", band(50.0) == "high")
    check("just under 100 is high", band(99.9) == "high")
    check("10 MW is moderate", band(10.0) == "moderate")
    check("just under 50 is moderate", band(49.9) == "moderate")
    check("under 10 is low", band(9.9) == "low")
    check("zero is low, not missing", band(0.0) == "low")
    check("no reading is None, not a band", band(None) is None)
    # Every band must have a picture, or a fire silently loses its marker.
    labels = {label for _, label in api.FRP_BANDS}
    check("bands and labels line up", labels == {"extreme", "high", "moderate", "low"})


def test_ignore_zones() -> None:
    """Detections inside a user zone are dropped; broken zones are ignored."""
    print("ignore zones")
    inside = api.in_ignored_zone
    zone = {"latitude": 43.60, "longitude": 3.90, "radius": 1000, "name": "Plant"}
    check("a detection at the centre is inside", inside(43.60, 3.90, [zone]))
    # ~700 m north, still within a 1 km radius.
    check("a detection 700 m away is inside", inside(43.6063, 3.90, [zone]))
    # ~2.2 km north, well outside.
    check("a detection 2 km away is outside", not inside(43.62, 3.90, [zone]))
    check("no zones means nothing is ignored", not inside(43.60, 3.90, []))
    check("None instead of a list is safe", not inside(43.60, 3.90, None))

    far = {"latitude": 0.0, "longitude": 0.0, "radius": 1000, "name": "Elsewhere"}
    check("any matching zone is enough", inside(43.60, 3.90, [far, zone]))

    # A half-written zone must not take an update down or swallow everything.
    broken = [
        {"latitude": 43.60, "name": "no longitude"},
        {"latitude": "x", "longitude": "y", "radius": 1000},
        {"latitude": 43.60, "longitude": 3.90, "radius": None},
    ]
    check("a malformed zone is skipped, not fatal", not inside(43.60, 3.90, broken))
    check("and does not stop a valid one", inside(43.60, 3.90, [*broken, zone]))


def test_persistent_sources() -> None:
    """A learned source is hidden; anything brighter than it still shows.

    The numbers mirror the calibration this rule came from: an industrial cell
    is recognised by a long calendar span, and the brightness test is relative
    to the cell's own history rather than to any megawatt constant.
    """
    print("persistent sources")
    today = date(2026, 7, 31)

    key = api.source_cell_key
    check(
        "neighbouring points share a cell",
        key(43.6000, 3.9000) == key(43.6002, 3.9002),
    )
    check(
        "points 3 km apart do not",
        key(43.6000, 3.9000) != key(43.6300, 3.9000),
    )

    # A week of activity is not a source, however busy.
    young = api.PersistentSources()
    young.record(
        [(43.60, 3.90, 5.0, f"2026-07-{day:02d}") for day in range(20, 28)], today
    )
    check("a week of detections is not a source", not young.is_suppressed(43.60, 3.90, 5.0))

    # Spread the same handful of days across three months instead.
    sources = api.PersistentSources()
    plant_days = ["2026-05-02", "2026-05-20", "2026-06-11", "2026-06-30", "2026-07-24"]
    sources.record([(43.60, 3.90, 10.0, d) for d in plant_days], today)
    check("five days across 60+ is a source", sources.is_suppressed(43.60, 3.90, 10.0))
    check("a routine detection is suppressed", sources.is_suppressed(43.60, 3.90, 25.0))
    # Baseline is 10 MW, so the ceiling sits at 30 MW.
    check("one just over the ceiling is shown", not sources.is_suppressed(43.60, 3.90, 31.0))
    check(
        "the Bordeaux case: 168 MW on a 10 MW source is shown",
        not sources.is_suppressed(43.60, 3.90, 168.5),
    )
    check("a cell with no history is untouched", not sources.is_suppressed(1.0, 1.0, 1.0))

    # One busy day cannot raise the baseline: the day contributes its peak and
    # nothing more, which is what keeps a fire from teaching the cell to hide it.
    loud = api.PersistentSources()
    loud.record(
        [(43.60, 3.90, 10.0, d) for d in plant_days]
        + [(43.60, 3.90, 400.0, "2026-07-26") for _ in range(300)],
        today,
    )
    check("300 detections in one day do not move the median",
          loud.is_suppressed(43.60, 3.90, 25.0))
    check("and that day's own fire still shows",
          not loud.is_suppressed(43.60, 3.90, 400.0))

    # A cell whose readings are all zero must not end up with a zero ceiling.
    quiet = api.PersistentSources()
    quiet.record([(43.60, 3.90, 0.0, d) for d in plant_days], today)
    check("a zero baseline still suppresses its own zeroes",
          quiet.is_suppressed(43.60, 3.90, 0.0))
    check("but not a real fire on top of it",
          not quiet.is_suppressed(43.60, 3.90, 500.0))

    # Ageing out: nothing recorded for long enough and the span collapses.
    aged = api.PersistentSources()
    aged.record([(43.60, 3.90, 10.0, d) for d in plant_days], date(2027, 1, 1))
    check("a source that stopped ages out of the window",
          not aged.is_suppressed(43.60, 3.90, 10.0))

    restored = api.PersistentSources.from_dict(sources.as_dict())
    check("the store round-trips the decision",
          restored.is_suppressed(43.60, 3.90, 10.0)
          and not restored.is_suppressed(43.60, 3.90, 168.5))
    check("a malformed store does not raise",
          api.PersistentSources.from_dict({"bogus": {}, "x:y": {"a": 1}}).tracked_cells
          == 0)
    check("None instead of a store is safe",
          api.PersistentSources.from_dict(None).tracked_cells == 0)
    check("a detection with no date is skipped",
          api.PersistentSources().record([(43.6, 3.9, 5.0, "")], today) is None)


def test_clustering() -> None:
    """Detections of the same fire collapse into one cluster."""
    print("clustering")
    h = api.FirmsHotspot
    # Two detections 300 m apart plus one 5 km away.
    points = [
        (h(latitude=43.600, longitude=3.900, satellite="noaa20", frp=12.0, confidence="nominal"), 5.0),
        (h(latitude=43.602, longitude=3.901, satellite="snpp", frp=30.0, confidence="high"), 5.1),
        (h(latitude=43.650, longitude=3.900, satellite="noaa20", frp=4.0, confidence="low"), 9.0),
    ]
    clusters = api.cluster_hotspots(points, 1.0, (43.5, 3.9))
    check("three detections become two fires", len(clusters) == 2, f"got {len(clusters)}")
    first = clusters[0]
    check("merged cluster counts both detections", first.detections == 2)
    check("merged cluster keeps both satellites", first.satellites == ["noaa20", "snpp"])
    check("merged cluster keeps the strongest FRP", near(first.frp, 30.0))
    check("merged cluster keeps the best confidence", first.confidence == "high")
    check("clusters are sorted by distance", clusters[0].distance_km <= clusters[1].distance_km)
    check("cluster carries a bearing", first.bearing is not None)
    check("cluster carries a compass direction", first.direction in api.CARDINALS)

    # Two fires that round to the same 0.01 degree id must stay distinguishable.
    same_id = [
        (h(latitude=44.0000, longitude=4.0000, satellite="noaa20", frp=9.0), 1.0),
        (h(latitude=44.0010, longitude=4.0010, satellite="noaa20", frp=8.0), 1.2),
    ]
    collided = api.cluster_hotspots(same_id, 0.05, (44.0, 4.0))
    check("colliding ids stay unique", len({c.id for c in collided}) == len(collided))

    check("no detections means no clusters", api.cluster_hotspots([], 1.0, (0, 0)) == [])


def test_id_carry_over() -> None:
    """A fire keeps its id while its centroid drifts."""
    print("id carry-over")
    h = api.FirmsHotspot
    origin = (43.5, 3.9)

    def one(lat: float, lon: float, frp: float = 10.0, dist: float = 5.0):
        return [(h(latitude=lat, longitude=lon, satellite="noaa20", frp=frp), dist)]

    # 43.604 rounds to 43.60, 43.606 to 43.61: the exact grid line that used to
    # destroy the entity and build a new one.
    first = api.cluster_hotspots(one(43.604, 3.900), 1.0, origin)
    drifted = api.cluster_hotspots(one(43.606, 3.900), 1.0, origin, first)
    check("the drifting centroid would change the raw id",
          api.cluster_hotspots(one(43.606, 3.900), 1.0, origin)[0].id != first[0].id)
    check("but the id is carried over instead", drifted[0].id == first[0].id,
          f"{first[0].id} -> {drifted[0].id}")

    # Far enough away that it is a different fire, not the same one moving.
    moved_far = api.cluster_hotspots(one(43.900, 3.900), 1.0, origin, first)
    check("a fire beyond the radius gets a fresh id", moved_far[0].id != first[0].id)

    # Two neighbours ~1.1 km apart: each must keep its own id, not swap.
    pair = [
        (h(latitude=44.000, longitude=4.000, satellite="noaa20", frp=20.0), 5.0),
        (h(latitude=44.010, longitude=4.000, satellite="noaa20", frp=10.0), 6.0),
    ]
    base = api.cluster_hotspots(pair, 0.6, origin)
    check("two neighbours are two fires", len(base) == 2)
    nudged = [
        (h(latitude=44.001, longitude=4.000, satellite="noaa20", frp=20.0), 5.0),
        (h(latitude=44.011, longitude=4.000, satellite="noaa20", frp=10.0), 6.0),
    ]
    after = api.cluster_hotspots(nudged, 0.6, origin, base)
    by_lat = {round(c.latitude, 3): c.id for c in after}
    check("the near neighbour keeps its own id", by_lat[44.001] == base[0].id)
    check("the far neighbour keeps its own id", by_lat[44.011] == base[1].id)
    check("no id is handed out twice", len({c.id for c in after}) == 2)

    # A newcomer next to a survivor must not inherit anything.
    grown = api.cluster_hotspots(
        one(43.604, 3.900) + [(h(latitude=43.700, longitude=3.900, satellite="snpp", frp=5.0), 7.0)],
        1.0, origin, first,
    )
    check("the survivor keeps its id", first[0].id in {c.id for c in grown})
    check("the newcomer gets its own", len({c.id for c in grown}) == 2)

    # An id belonging to a fire that went out must not be reused.
    gone = api.cluster_hotspots(one(43.700, 3.900, dist=7.0), 1.0, origin, first)
    check("a fire that went out does not donate its id", gone[0].id != first[0].id)

    # Without a previous cycle nothing changes -- the pre-0.4.0 behaviour.
    check("no previous set means ids are derived as before",
          api.cluster_hotspots(one(43.606, 3.900), 1.0, origin)[0].id == "43.61/3.90")

    # Carried ids win over freshly derived ones when they collide.
    collide_before = api.cluster_hotspots(one(44.0000, 4.0000), 1.0, origin)
    collide_after = api.cluster_hotspots(
        [
            (h(latitude=44.0010, longitude=4.0000, satellite="noaa20", frp=20.0), 5.0),
            (h(latitude=44.0040, longitude=4.0000, satellite="snpp", frp=1.0), 9.0),
        ],
        0.2, origin, collide_before,
    )
    check("a carried id is never renamed",
          collide_before[0].id in {c.id for c in collide_after})
    check("the colliding newcomer is the one suffixed",
          len({c.id for c in collide_after}) == 2)


# --- met.no parsing -------------------------------------------------------
def test_parse_wind() -> None:
    """Wind is read from the forecast step closest to the moment asked."""
    print("parse_wind")
    wind = api.parse_wind(FIXTURE, RECORDED_AT)
    check("wind is found in the recorded payload", wind is not None)
    # 12:59 is one minute from the 13:00 step and 59 from the 12:00 one.
    check("closest step wins, not timeseries[0]", wind.time == "2026-07-27T13:00:00Z")
    check("wind bearing comes from wind_from_direction", near(wind.bearing, 333.7))
    check("wind speed comes through in m/s", near(wind.speed, 5.0))

    # The sensor renders this bearing as a compass point next to the degrees.
    check("wind bearing maps onto a compass point", api.cardinal(wind.bearing) == "NNW")

    earlier = api.parse_wind(FIXTURE, datetime(2026, 7, 27, 12, 5, tzinfo=UTC))
    check("earlier ask picks the earlier step", earlier.time == "2026-07-27T12:00:00Z")

    stale = api.parse_wind(FIXTURE, RECORDED_AT + timedelta(days=2))
    check("a forecast that no longer covers now returns None", stale is None)

    check("empty payload returns None", api.parse_wind({}, RECORDED_AT) is None)
    check("garbage payload returns None", api.parse_wind({"properties": 7}, RECORDED_AT) is None)
    check(
        "a step without wind returns None",
        api.parse_wind(
            {"properties": {"timeseries": [{"time": "2026-07-27T13:00:00Z", "data": {}}]}},
            RECORDED_AT,
        )
        is None,
    )
    check(
        "an unparsable timestamp is skipped, not fatal",
        api.parse_wind(
            {
                "properties": {
                    "timeseries": [
                        {"time": "not a time", "data": {}},
                        FIXTURE["properties"]["timeseries"][1],
                    ]
                }
            },
            RECORDED_AT,
        )
        is not None,
    )


# --- clients --------------------------------------------------------------
class FakeResponse:
    """Just enough of an aiohttp response for the client."""

    def __init__(self, status: int, body: str = "", headers: dict | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body


class FakeSession:
    """Hands out canned responses and records what was asked for."""

    def __init__(self, *responses) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        if not self._responses:
            raise AssertionError("client made more requests than the test expected")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


BODY = json.dumps(FIXTURE)
UA = "ha-nasa-firms/0.3.0 github.com/bangboomben/ha-nasa-firms"


def ok_response(
    expires: str = "Mon, 27 Jul 2026 13:29:37 GMT",
    last_modified: str = "Mon, 27 Jul 2026 12:59:07 GMT",
) -> FakeResponse:
    """A 200 with the caching headers met.no really sends."""
    return FakeResponse(
        200,
        BODY,
        {"Expires": expires, "Last-Modified": last_modified},
    )


async def test_firms_client_failures() -> None:
    """FIRMS failure paths carry a message a person can act on.

    satellite_errors publishes these strings verbatim, and the timeout case
    is the one that actually happens: three real occurrences in a week on the
    live instance, every one reading "FIRMS request failed: " with nothing
    after the colon, because str() of a TimeoutError is empty.
    """
    print("FIRMS client: failures")
    bbox = (43.0, 3.0, 44.0, 4.0)

    session = FakeSession(TimeoutError())
    client = api.FirmsClient(session, "key", "Europe")
    try:
        await client.fetch("noaa20", api.WINDOW_24H, bbox)
        check("timeout raises FirmsError", False, "no exception")
    except api.FirmsError as err:
        check("timeout raises FirmsError", True)
        check("and the message says so", "timed out" in str(err), f"got {err!r}")
    # Only rejections hop hosts; an outage must not double the wait.
    check("a timeout does not try the second host", len(session.calls) == 1)

    client = api.FirmsClient(
        FakeSession(_ClientError("connection reset")), "key", "Europe"
    )
    try:
        await client.fetch("noaa20", api.WINDOW_24H, bbox)
        check("connection error raises FirmsError", False, "no exception")
    except api.FirmsError as err:
        check("connection error raises FirmsError", True)
        check("keeping the error's own text", "connection reset" in str(err))

    client = api.FirmsClient(FakeSession(FakeResponse(500, "boom", {})), "key", "Europe")
    try:
        await client.fetch("noaa20", api.WINDOW_24H, bbox)
        check("HTTP 500 raises FirmsError", False, "no exception")
    except api.FirmsError as err:
        check("HTTP 500 raises FirmsError", True)
        check("naming the status", "500" in str(err))

    # FirmsAuthError subclasses FirmsError, so getting the plain kind here
    # would silently skip the reauth flow. A rejection is only final once
    # both hosts said no — the fallback itself is test_firms_host_fallback.
    client = api.FirmsClient(
        FakeSession(FakeResponse(403, "denied", {}), FakeResponse(403, "denied", {})),
        "key",
        "Europe",
    )
    try:
        await client.fetch("noaa20", api.WINDOW_24H, bbox)
        check("HTTP 403 from both hosts raises FirmsAuthError", False, "no exception")
    except api.FirmsAuthError as err:
        check("HTTP 403 from both hosts raises FirmsAuthError", True)
        check(
            "naming both hosts and NASA's answer",
            "firms.modaps" in str(err)
            and "firms2.modaps" in str(err)
            and "denied" in str(err),
            f"got {err!r}",
        )
    except api.FirmsError as err:
        check("HTTP 403 from both hosts raises FirmsAuthError", False, f"got FirmsError: {err}")


async def test_firms_host_fallback() -> None:
    """A MAP_KEY rejected by one FIRMS host is retried on the other.

    The two FIRMS hostnames do not accept the same keys (issue #2): a key
    can fetch data on firms2 while firms answers HTTP 403, and the other way
    round. The client must ask the second host before giving up, and then
    keep using the one that said yes.
    """
    print("FIRMS client: host fallback")
    bbox = (43.0, 3.0, 44.0, 4.0)
    empty = json.dumps({"type": "FeatureCollection", "features": []})
    rejected = "MAP_KEY is invalid or your have exceeded your transaction/time limit."

    session = FakeSession(
        FakeResponse(403, rejected, {}),
        FakeResponse(200, empty, {}),
        FakeResponse(200, empty, {}),
    )
    client = api.FirmsClient(session, "key", "Europe")
    result = await client.fetch("noaa20", api.WINDOW_24H, bbox)
    check("a key only the second host knows still fetches", result == [])
    check(
        "the first ask goes to firms",
        session.calls[0]["url"].startswith("https://firms.modaps"),
        f"got {session.calls[0]['url']}",
    )
    check(
        "the rejection is retried on firms2",
        session.calls[1]["url"].startswith("https://firms2.modaps"),
        f"got {session.calls[1]['url']}",
    )

    await client.fetch("noaa20", api.WINDOW_24H, bbox)
    check("the accepting host is remembered", len(session.calls) == 3)
    check(
        "and asked first from then on",
        session.calls[2]["url"].startswith("https://firms2.modaps"),
        f"got {session.calls[2]['url']}",
    )

    # The HTML "invalid MAP_KEY" page is a rejection too, not a parse error.
    session = FakeSession(
        FakeResponse(200, "<html>Invalid MAP_KEY. Register at ...</html>", {}),
        FakeResponse(200, empty, {}),
    )
    client = api.FirmsClient(session, "key", "Europe")
    result = await client.fetch("noaa20", api.WINDOW_24H, bbox)
    check("an HTML rejection also falls through to the second host", result == [])
    check("with exactly one retry", len(session.calls) == 2)


async def test_client_request() -> None:
    """The outgoing request honours the terms of service."""
    print("met.no client: request shape")
    session = FakeSession(ok_response())
    client = api.MetNoClient(session, UA)
    wind = await client.wind_at(43.6012, 3.8987, RECORDED_AT)
    check("wind comes back", wind is not None)
    call = session.calls[0]
    check("hits the compact endpoint", call["url"] == api.METNO_URL)
    check("identifies itself", call["headers"].get("User-Agent") == UA)
    check(
        "no If-Modified-Since on the first request",
        "If-Modified-Since" not in call["headers"],
    )
    check(
        "coordinates are rounded to ~1 km",
        call["params"] == {"lat": "43.6000", "lon": "3.9000"},
        f"got {call['params']}",
    )
    check(
        "coordinates stay within met.no's 4-decimal limit",
        all(len(v.split(".")[1]) <= 4 for v in call["params"].values()),
    )


async def test_client_caching() -> None:
    """Expires is respected, then If-Modified-Since keeps the refresh cheap."""
    print("met.no client: caching")
    session = FakeSession(ok_response(), FakeResponse(304, "", {}))
    client = api.MetNoClient(session, UA)
    await client.wind_at(43.60, 3.90, RECORDED_AT)

    # Same fire, 15 minutes later: still inside the Expires window.
    await client.wind_at(43.60, 3.90, RECORDED_AT + timedelta(minutes=15))
    check("no second request before Expires", len(session.calls) == 1, f"{len(session.calls)} calls")

    # Past Expires: ask again, but conditionally.
    wind = await client.wind_at(43.60, 3.90, RECORDED_AT + timedelta(minutes=45))
    check("refetches once Expires has passed", len(session.calls) == 2)
    check(
        "echoes the stored Last-Modified verbatim",
        session.calls[1]["headers"].get("If-Modified-Since")
        == "Mon, 27 Jul 2026 12:59:07 GMT",
    )
    check("a 304 keeps serving the cached forecast", wind is not None)

    # A second fire is its own request with its own cache entry — it must
    # neither reuse the first fire's forecast nor destroy it.
    session2 = FakeSession(ok_response(), ok_response())
    client2 = api.MetNoClient(session2, UA)
    await client2.wind_at(43.60, 3.90, RECORDED_AT)
    await client2.wind_at(40.54, 23.01, RECORDED_AT)
    check("a second fire is a fresh request", len(session2.calls) == 2)
    check(
        "without the first fire's If-Modified-Since",
        "If-Modified-Since" not in session2.calls[1]["headers"],
    )
    # Returning to either fire inside its Expires window costs nothing —
    # the alternation that made a one-slot cache miss on every call.
    await client2.wind_at(43.60, 3.90, RECORDED_AT + timedelta(minutes=15))
    await client2.wind_at(40.54, 23.01, RECORDED_AT + timedelta(minutes=15))
    check("alternating between fires hits both caches", len(session2.calls) == 2)

    # Past Expires, each fire refreshes with its own Last-Modified.
    session3 = FakeSession(
        ok_response(last_modified="Mon, 27 Jul 2026 12:59:07 GMT"),
        ok_response(last_modified="Mon, 27 Jul 2026 12:41:00 GMT"),
        FakeResponse(304, "", {}),
        FakeResponse(304, "", {}),
    )
    client3 = api.MetNoClient(session3, UA)
    await client3.wind_at(43.60, 3.90, RECORDED_AT)
    await client3.wind_at(40.54, 23.01, RECORDED_AT)
    later = RECORDED_AT + timedelta(minutes=45)
    await client3.wind_at(43.60, 3.90, later)
    await client3.wind_at(40.54, 23.01, later)
    check(
        "each fire refreshes with its own Last-Modified",
        session3.calls[2]["headers"].get("If-Modified-Since")
        == "Mon, 27 Jul 2026 12:59:07 GMT"
        and session3.calls[3]["headers"].get("If-Modified-Since")
        == "Mon, 27 Jul 2026 12:41:00 GMT",
    )


async def test_client_cache_bounds() -> None:
    """The cache is bounded, and recency decides who falls out."""
    print("met.no client: cache bounds")
    size = api.WEATHER_CACHE_SIZE
    session = FakeSession(*[ok_response() for _ in range(size + 2)])
    client = api.MetNoClient(session, UA)

    # Fill every slot with distinct coordinates.
    for i in range(size):
        await client.wind_at(40.0 + i, 3.90, RECORDED_AT)
    check("filling the cache is one request per point", len(session.calls) == size)

    # Touch the oldest entry so it becomes the newest...
    await client.wind_at(40.0, 3.90, RECORDED_AT + timedelta(minutes=5))
    check("a cache hit costs no request", len(session.calls) == size)

    # ...then overflow the cache by one.
    await client.wind_at(40.0 + size, 3.90, RECORDED_AT + timedelta(minutes=5))
    check("one past the cap is a fresh request", len(session.calls) == size + 1)

    # The touched entry survived the eviction; the untouched oldest did not.
    await client.wind_at(40.0, 3.90, RECORDED_AT + timedelta(minutes=6))
    check("a hit refreshes recency, so the touched point survives",
          len(session.calls) == size + 1)
    await client.wind_at(41.0, 3.90, RECORDED_AT + timedelta(minutes=6))
    check("the least-recently-used point is the one evicted",
          len(session.calls) == size + 2)


async def test_client_backoff_is_global() -> None:
    """One throttle silences every coordinate, not just the one that hit it."""
    print("met.no client: backoff covers every coordinate")
    session = FakeSession(ok_response(), FakeResponse(429, "slow down", {}), ok_response())
    client = api.MetNoClient(session, UA)
    await client.wind_at(43.60, 3.90, RECORDED_AT)
    try:
        await client.wind_at(40.54, 23.01, RECORDED_AT)
        check("the throttle still raises WeatherError", False, "no exception")
    except api.WeatherError:
        check("the throttle still raises WeatherError", True)

    # A coordinate the client has never seen must not tempt it into a request.
    wind = await client.wind_at(45.00, 7.00, RECORDED_AT + timedelta(minutes=5))
    check("a throttle on one fire silences all of them", len(session.calls) == 2)
    check("an unknown fire reports no wind meanwhile", wind is None)

    # What was cached before the throttle is still served from memory.
    wind = await client.wind_at(43.60, 3.90, RECORDED_AT + timedelta(minutes=5))
    check("a fire cached before the throttle keeps its wind", wind is not None)
    check("still without a request", len(session.calls) == 2)

    # Once the hour is up, requests resume.
    after = RECORDED_AT + api.WEATHER_RATE_LIMIT_BACKOFF + timedelta(minutes=1)
    wind = await client.wind_at(45.00, 7.00, after)
    check("requests resume after the backoff", len(session.calls) == 3)
    check("and the wind comes back", wind is not None)


async def test_client_failures() -> None:
    """Failure paths raise WeatherError and never hammer met.no."""
    print("met.no client: failures")
    session = FakeSession(FakeResponse(429, "slow down", {}))
    client = api.MetNoClient(session, UA)
    try:
        await client.wind_at(43.60, 3.90, RECORDED_AT)
        check("429 raises WeatherError", False, "no exception")
    except api.WeatherError:
        check("429 raises WeatherError", True)
    except Exception as err:  # noqa: BLE001 - the test is the assertion
        check("429 raises WeatherError", False, f"got {type(err).__name__}")

    # Throttled means throttled: nothing goes out for an hour.
    wind = await client.wind_at(43.60, 3.90, RECORDED_AT + timedelta(minutes=15))
    check("backs off after a 429", len(session.calls) == 1, f"{len(session.calls)} calls")
    check("and reports no wind meanwhile", wind is None)

    for label, canned in (
        ("connection error", _ClientError("boom")),
        ("timeout", TimeoutError()),
        ("HTTP 500", FakeResponse(500, "server error", {})),
        ("non-JSON body", FakeResponse(200, "<html>nope</html>", {})),
    ):
        client = api.MetNoClient(FakeSession(canned), UA)
        try:
            await client.wind_at(43.60, 3.90, RECORDED_AT)
            check(f"{label} raises WeatherError", False, "no exception")
        except api.WeatherError:
            check(f"{label} raises WeatherError", True)
        except Exception as err:  # noqa: BLE001 - the test is the assertion
            check(f"{label} raises WeatherError", False, f"got {type(err).__name__}")

    # The same blank-message trap as on the FIRMS side.
    client = api.MetNoClient(FakeSession(TimeoutError()), UA)
    try:
        await client.wind_at(43.60, 3.90, RECORDED_AT)
        check("a timeout names itself", False, "no exception")
    except api.WeatherError as err:
        check("a timeout names itself", "timed out" in str(err), f"got {err!r}")

    check(
        "WeatherError is not a FirmsError",
        not issubclass(api.WeatherError, api.FirmsError),
    )


def test_place_index() -> None:
    """The bundled place dataset, and the banded search over it."""
    print("place names")
    index = api.PlaceIndex()
    index.load()
    check("dataset loads", index.loaded)
    check("dataset holds six figures of places", len(index) > 100_000, f"got {len(index)}")

    # Sorted by latitude is not decoration: the search bisects on it.
    lats = index._lats
    check(
        "dataset is sorted by latitude",
        all(lats[i] <= lats[i + 1] for i in range(0, len(lats) - 1, 97)),
    )

    # The reference entry. Montpellier sits ~2.3 km from 43.60/3.90.
    match = index.nearest(43.60, 3.90)
    check("reference entry resolves to Montpellier", match is not None and match.name == "Montpellier", f"got {match}")
    check("reference entry knows its country", match is not None and match.country == "FR")
    check("reference distance ~2.3 km", match is not None and near(match.distance_km, 2.3, 0.3), f"got {match}")

    # Names are held as one UTF-8 blob plus offsets, so the one thing that can
    # go wrong is slicing a multi-byte character in half. Thérmi carries an
    # accent, and the structural checks below cover the rest of the table.
    accented = index.nearest(40.54, 23.01)
    check(
        "a non-ASCII name survives the blob round-trip",
        accented is not None and accented.name == "Thérmi",
        f"got {accented}",
    )
    check(
        "offsets end exactly at the end of the blob",
        index._offsets[-1] == len(index._names),
        f"{index._offsets[-1]} vs {len(index._names)}",
    )
    check(
        "there is one offset per place, plus the closing one",
        len(index._offsets) == len(index) + 1,
        f"{len(index._offsets)} vs {len(index) + 1}",
    )
    decoded = 0
    for i in range(0, len(index), 331):  # a spread-out sample, not a hot loop
        try:
            name = index._place_at(i, 0.0).name
        except UnicodeDecodeError:
            check("every sampled name decodes", False, f"place {i} broke")
            break
        if name:
            decoded += 1
    else:
        check("every sampled name decodes and is non-empty", decoded > 400, f"got {decoded}")

    # Sparse country: the answer is legitimately far away and must say so
    # rather than being hidden or capped.
    outback = index.nearest(-25.34, 131.03)
    check("outback resolves to Yulara", outback is not None and outback.name == "Yulara", f"got {outback}")
    check("outback distance is ~12 km", outback is not None and near(outback.distance_km, 11.8, 1.0), f"got {outback}")

    # Empty ocean: every band expands and the last one spans the globe, so
    # there is always an answer, however remote.
    pacific = index.nearest(0.0, -140.0)
    check("empty ocean still resolves", pacific is not None, f"got {pacific}")
    check("empty ocean is honestly far", pacific is not None and pacific.distance_km > 500, f"got {pacific}")

    # The band search must agree with brute force, including where the naive
    # implementations break: across the antimeridian and at the pole.
    for label, lat, lon in (
        ("antimeridian", -16.5, 179.9),
        ("north pole", 89.9, 0.0),
        ("southern France", 43.60, 3.90),
    ):
        best_index, best_km = -1, 1e18
        for i in range(len(index._lats)):
            km = api.haversine_km(lat, lon, index._lats[i], index._lons[i])
            if km < best_km:
                best_index, best_km = i, km
        expected = index._place_at(best_index, best_km)
        got = index.nearest(lat, lon)
        check(
            f"banded search matches a full scan at the {label}",
            got is not None and got.name == expected.name,
            f"got {got}, full scan says {expected.name} at {best_km:.1f} km",
        )

    # Repeat lookups come out of the cache, and the cache is bounded.
    before = len(index._cache)
    index.nearest(43.60, 3.90)
    check("a repeat lookup adds no cache entry", len(index._cache) == before)
    for i in range(api.PLACE_CACHE_SIZE + 50):
        index.nearest(10.0 + i * 0.01, 10.0)
    check(
        "cache stays bounded",
        len(index._cache) <= api.PLACE_CACHE_SIZE,
        f"got {len(index._cache)}",
    )


def test_place_index_concurrent_load() -> None:
    """Two config entries load the shared index at the same moment.

    This is not hypothetical: entries set up concurrently, each reaches the
    index from its own executor thread, and the very first cycle of both runs
    before anything is loaded. An earlier version marked the index as failed
    for the duration of the load, so whichever entry arrived second saw a
    permanently-failed index and went the whole session without place names —
    which is exactly how it behaved on a live two-entry instance.
    """
    print("place names: concurrent load")
    import threading

    index = api.PlaceIndex()
    start = threading.Barrier(4)
    results: list[object] = [None] * 4
    errors: list[BaseException] = []

    def worker(slot: int) -> None:
        try:
            start.wait(timeout=30)
            results[slot] = index.nearest(43.60, 3.90)
        except BaseException as err:  # noqa: BLE001 - the test is the assertion
            errors.append(err)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    check("no thread raised", not errors, f"got {errors[:1]}")
    check("the index ends up loaded", index.loaded)
    check(
        "every caller gets the answer, not just the first",
        all(r is not None and r.name == "Montpellier" for r in results),
        f"got {results}",
    )


def test_place_index_failures() -> None:
    """A broken dataset costs place names and nothing else."""
    print("place names: failure modes")
    missing = api.PlaceIndex(Path(__file__).parent / "no-such-file.csv.gz")
    try:
        missing.load()
        check("a missing dataset raises PlaceDataError", False, "no exception")
    except api.PlaceDataError as err:
        check("a missing dataset raises PlaceDataError", True)
        check("the error names the file", "no-such-file" in str(err), f"got {err!r}")

    # Latched: the second call is silent, so a broken install does not warn
    # every fifteen minutes for as long as Home Assistant runs.
    try:
        missing.load()
        check("a second load stays silent", True)
    except api.PlaceDataError:
        check("a second load stays silent", False, "raised again")
    check("a failed index reports itself unloaded", not missing.loaded)
    check("a failed index resolves nothing", missing.nearest(43.6, 3.9) is None)

    # An interrupted HACS download is the realistic corruption, and it must
    # not escape as something the update cycle would treat as a fire failure.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "places.csv.gz"
        broken.write_bytes(b"\x1f\x8b\x08\x00 this is not a gzip stream at all")
        index = api.PlaceIndex(broken)
        try:
            index.load()
            check("a corrupt dataset raises PlaceDataError", False, "no exception")
        except api.PlaceDataError:
            check("a corrupt dataset raises PlaceDataError", True)
        except Exception as err:  # noqa: BLE001 - the test is the assertion
            check(
                "a corrupt dataset raises PlaceDataError",
                False,
                f"leaked {type(err).__name__}",
            )
        check("a corrupt dataset resolves nothing", index.nearest(43.6, 3.9) is None)

        # Truncated mid-stream: valid header, cut body.
        good = api.PLACES_FILE.read_bytes()
        half = Path(tmp) / "half.csv.gz"
        half.write_bytes(good[: len(good) // 2])
        try:
            api.PlaceIndex(half).load()
            check("a truncated dataset raises PlaceDataError", False, "no exception")
        except api.PlaceDataError:
            check("a truncated dataset raises PlaceDataError", True)
        except Exception as err:  # noqa: BLE001 - the test is the assertion
            check(
                "a truncated dataset raises PlaceDataError",
                False,
                f"leaked {type(err).__name__}",
            )

    check(
        "PlaceDataError is not a FirmsError",
        not issubclass(api.PlaceDataError, api.FirmsError),
    )



# --- satellite observation prediction -----------------------------------
class OrbitResponse:
    """Async context-manager response for the CelesTrak client."""

    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._body = json.dumps(payload) if not isinstance(payload, str) else payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def text(self) -> str:
        return self._body


class OrbitSession:
    """Canned synchronous get() returning async context managers."""

    def __init__(self, *responses: OrbitResponse) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("orbit client made more requests than expected")
        return self.responses.pop(0)


def test_orbit_parsing_and_drift() -> None:
    """OMM parsing and short-horizon drift against a recorded SGP4 vector."""
    print("satellite orbits: parsing and drift")
    elements = api._parse_omm_json(ORBIT_FIXTURE)
    check("OMM epoch is UTC", elements.epoch.tzinfo is not None)
    check("OMM eccentricity parsed", near(elements.ecc, 0.0007417, 1e-9))
    check("OMM mean motion positive", elements.n > 0.0)

    for label, broken in (
        ("missing field", {k: v for k, v in ORBIT_FIXTURE.items() if k != "MEAN_MOTION"}),
        ("bad number", {**ORBIT_FIXTURE, "MEAN_MOTION": "banana"}),
        ("bad eccentricity", {**ORBIT_FIXTURE, "ECCENTRICITY": 1.2}),
    ):
        try:
            api._parse_omm_json(broken)
            check(f"malformed OMM: {label}", False, "no exception")
        except api.OrbitError:
            check(f"malformed OMM: {label}", True)

    # Reference SGP4 TEME vector for the recorded ISS element set at
    # 2019-12-09 20:42:00Z. The lightweight propagator is intentionally not
    # SGP4, but at this ~4 h horizon it should stay comfortably within 15 km.
    # This assertion catches applying J2 mean-anomaly drift a second time.
    when = datetime(2019, 12, 9, 20, 42, tzinfo=UTC)
    got = api._kepler_eci(elements, when)
    reference = (-6088.92, -936.13, -2866.44)
    drift_km = sum((a - b) ** 2 for a, b in zip(got, reference)) ** 0.5
    check("4 h propagation drift stays under 15 km", drift_km < 15.0, f"got {drift_km:.1f} km")


def test_configured_spacecraft() -> None:
    """Configured FIRMS sources expand to the right physical spacecraft."""
    print("satellite orbits: configured spacecraft")
    expand = api.CelesTrakClient._configured_spacecraft
    default = expand(["noaa20", "noaa21", "snpp"])
    check("default sources map to three spacecraft", [s.key for s in default] == ["noaa20", "noaa21", "snpp"])
    modis = expand(["modis"])
    check("MODIS expands to Terra and Aqua", [s.key for s in modis] == ["terra", "aqua"])
    deduped = expand(["noaa20", "noaa20", "modis"])
    check("duplicate sources do not duplicate spacecraft", len({s.norad_id for s in deduped}) == len(deduped))
    check("unknown source is ignored", expand(["unknown"]) == [])


async def test_orbit_client_policy() -> None:
    """Two-hour cache and non-200 stop/cooldown without network access."""
    print("satellite orbits: CelesTrak policy")
    sat = api.ORBIT_SATELLITES["noaa20"][0]
    payload = [ORBIT_FIXTURE]

    session = OrbitSession(OrbitResponse(200, payload), OrbitResponse(200, payload))
    client = api.CelesTrakClient(session)
    first = await client._elements(sat)
    second = await client._elements(sat)
    check("two-hour cache avoids a second download", len(session.calls) == 1)
    check("cached elements are reused", first == second)
    check("FORMAT=JSON is explicit", "FORMAT=JSON" in session.calls[0])
    check("celestrak.org domain is used", session.calls[0].startswith("https://celestrak.org/"))

    # Age the cache beyond the policy TTL: exactly one new request is allowed.
    client._element_cache[sat.norad_id] = (
        datetime.now(UTC) - api.ORBIT_ELEMENT_TTL - timedelta(seconds=1),
        first,
    )
    await client._elements(sat)
    check("expired cache fetches once", len(session.calls) == 2)

    blocked_session = OrbitSession(OrbitResponse(503, "busy"), OrbitResponse(200, payload))
    blocked = api.CelesTrakClient(blocked_session)
    try:
        await blocked._elements(sat)
        check("non-200 raises OrbitHTTPError", False, "no exception")
    except api.OrbitHTTPError as err:
        check("non-200 raises OrbitHTTPError", err.status == 503)
    try:
        await blocked._elements(sat)
        check("blocked request still reports status", False, "no exception")
    except api.OrbitHTTPError as err:
        check("blocked request still reports status", err.status == 503)
    check("non-200 is not retried during cooldown", len(blocked_session.calls) == 1)

    # Simulate the full 24-hour cooldown elapsing without sleeping.
    blocked._http_blocked_until = datetime.now(UTC) - timedelta(seconds=1)
    await blocked._elements(sat)
    check("a fresh attempt is allowed after 24 h cooldown", len(blocked_session.calls) == 2)


def main() -> int:
    """Run everything and report."""
    test_geometry()
    test_smoke_offset()
    test_regions()
    test_confidence()
    test_intensity()
    test_ignore_zones()
    test_persistent_sources()
    test_clustering()
    test_id_carry_over()
    test_parse_wind()
    test_place_index()
    test_place_index_concurrent_load()
    test_place_index_failures()
    test_orbit_parsing_and_drift()
    test_configured_spacecraft()
    asyncio.run(test_firms_client_failures())
    asyncio.run(test_firms_host_fallback())
    asyncio.run(test_client_request())
    asyncio.run(test_client_caching())
    asyncio.run(test_client_cache_bounds())
    asyncio.run(test_client_backoff_is_global())
    asyncio.run(test_client_failures())
    asyncio.run(test_orbit_client_policy())
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
