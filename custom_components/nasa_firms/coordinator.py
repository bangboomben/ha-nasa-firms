"""Update coordinator: fetch all satellites, filter, deduplicate."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_RADIUS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CONFIDENCE_RANK,
    WINDOW_24H,
    CelesTrakClient,
    FirmsAuthError,
    FirmsClient,
    FirmsCluster,
    MetNoClient,
    ObservationSchedule,
    OrbitError,
    OrbitHTTPError,
    PersistentSources,
    PlaceDataError,
    PlaceIndex,
    PlaceMatch,
    SatelliteObservation,
    WeatherError,
    WindObservation,
    bbox_around,
    cluster_hotspots,
    haversine_km,
    in_ignored_zone,
)
from .const import (
    CLUSTER_RADIUS_KM,
    CONF_AUTO_IGNORE,
    CONF_IGNORE_ZONES,
    CONF_MIN_CONFIDENCE,
    CONF_MIN_FRP,
    CONF_SATELLITES,
    CONF_WIND_FIRES,
    CONF_WINDOW,
    DEFAULT_AUTO_IGNORE,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_FRP,
    DEFAULT_RADIUS_M,
    DEFAULT_SATELLITES,
    DEFAULT_WIND_FIRES,
    DOMAIN,
    FETCH_COUNT,
    MAX_WIND_FIRES,
    SOURCES_STORAGE_KEY,
    SOURCES_STORAGE_VERSION,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

type NasaFirmsConfigEntry = ConfigEntry[FirmsCoordinator]


@dataclass
class FirmsData:
    """Result of one update cycle."""

    clusters: list[FirmsCluster] = field(default_factory=list)
    clusters_by_id: dict[str, FirmsCluster] = field(default_factory=dict)
    raw_detections: int = 0
    per_satellite: dict[str, int] = field(default_factory=dict)
    satellite_errors: dict[str, str] = field(default_factory=dict)
    # At least one satellite came back at the FETCH_COUNT ceiling, so the feed
    # was cut off and fires are missing. A log warning is not enough for this:
    # every number the integration shows is then too low, and nothing about a
    # too-low fire count looks wrong.
    truncated: bool = False
    # Detections dropped by the user's ignore zones. Surfaced so a zone can be
    # seen working — a silent filter on fire data would be worse than none.
    ignored_detections: int = 0
    # Detections dropped because they sat on a learned persistent source and
    # looked like its usual output. Counted separately from the manual zones
    # for the same reason those are counted at all: this one decides on its
    # own, so it has to be even easier to see doing it.
    auto_ignored_detections: int = 0
    # Wind at each fire's own coordinates, keyed by cluster id, for the N
    # nearest fires only. A fire ranked beyond that budget is simply absent —
    # not None — and so is any fire whose lookup failed.
    wind: dict[str, WindObservation] = field(default_factory=dict)
    # Nearest populated place per fire, keyed by cluster id. Every fire gets
    # one — the lookup is local and costs nothing per fire after the first —
    # so absence here means the dataset could not be read, nothing else.
    places: dict[str, PlaceMatch] = field(default_factory=dict)
    # Immediately previous/next configured satellite observation opportunity.
    # Supplemental only: prediction failures never invalidate FIRMS fire data.
    observation_schedule: ObservationSchedule = field(default_factory=ObservationSchedule)

    @property
    def nearest_wind(self) -> WindObservation | None:
        """Wind at the closest fire — the shape every consumer grew up with."""
        if not self.clusters:
            return None
        return self.wind.get(self.clusters[0].id)

    @property
    def nearest_km(self) -> float | None:
        """Distance to the closest fire, or None when there is none."""
        return self.clusters[0].distance_km if self.clusters else None

    @property
    def max_frp(self) -> float | None:
        """Strongest fire radiative power across all clusters."""
        frps = [c.frp for c in self.clusters if c.frp is not None]
        return max(frps) if frps else None

    @property
    def next_observation(self) -> SatelliteObservation | None:
        """Next configured satellite observation opportunity."""
        return self.observation_schedule.next

    @property
    def previous_observation(self) -> SatelliteObservation | None:
        """Most recent configured satellite observation opportunity."""
        return self.observation_schedule.previous



class FirmsCoordinator(DataUpdateCoordinator[FirmsData]):
    """Poll FIRMS for all configured satellites and merge the result."""

    config_entry: NasaFirmsConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NasaFirmsConfigEntry,
        client: FirmsClient,
        weather: MetNoClient,
        places: PlaceIndex,
        orbits: CelesTrakClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.weather = weather
        # Shared across entries — see const.DATA_PLACES.
        self.places = places
        self.orbits = orbits
        self._orbits_failing = False
        self._orbit_http_status: int | None = None
        self._orbit_issue_reported: bool | None = None
        # Latched so a weather outage is reported once, not every 15 minutes.
        self._weather_failing = False
        # Same latch for the place dataset — but this one never clears. The
        # dataset can only fail by being missing or corrupt, which is a broken
        # install rather than a passing outage, and the index itself gives up
        # permanently after the first failure. Reinstalling and restarting is
        # the fix, and the restart is what resets this.
        self._places_failing = False
        # None rather than False, so the first cycle after a restart always
        # syncs the repair issue — otherwise one raised before the restart
        # would outlive the condition that raised it.
        self._truncation_reported: bool | None = None
        cfg = {**entry.data, **entry.options}
        self.latitude: float = cfg[CONF_LATITUDE]
        self.longitude: float = cfg[CONF_LONGITUDE]
        self.radius_km: float = cfg.get(CONF_RADIUS, DEFAULT_RADIUS_M) / 1000
        self.satellites: list[str] = cfg.get(CONF_SATELLITES, DEFAULT_SATELLITES)
        self.window: str = cfg.get(CONF_WINDOW, WINDOW_24H)
        self.min_confidence: str = cfg.get(CONF_MIN_CONFIDENCE, DEFAULT_MIN_CONFIDENCE)
        self.min_frp: float = cfg.get(CONF_MIN_FRP, DEFAULT_MIN_FRP)
        # int() because the number selector hands back a float, and a float
        # cannot slice a list. Clamped against the cap rather than trusted:
        # the budget promise in the docs must hold even for a hand-edited
        # entry, since every installation shares one met.no User-Agent.
        self.wind_fires: int = min(
            int(cfg.get(CONF_WIND_FIRES, DEFAULT_WIND_FIRES)), MAX_WIND_FIRES
        )
        self.auto_ignore: bool = cfg.get(CONF_AUTO_IGNORE, DEFAULT_AUTO_IGNORE)
        # The per-cell history behind the automatic ignores. Recorded on every
        # cycle whatever `auto_ignore` says, and only *consulted* when it is
        # on: a source needs 60 days to be recognised, so building the history
        # only after the switch is flipped would mean two months of nothing
        # happening. This way the feature works the day it is enabled.
        self._sources = PersistentSources()
        self._sources_store: Store = Store(
            hass, SOURCES_STORAGE_VERSION, f"{SOURCES_STORAGE_KEY}.{entry.entry_id}"
        )
        self._bbox = bbox_around(self.latitude, self.longitude, self.radius_km)
        # Identifies which entry a fire belongs to. Every entry publishes its
        # fires under the same `nasa_firms` geo_location source, so a map card
        # fed by `geo_location_sources` mixes them — and since each fire's state
        # is its distance from *its own* entry's origin, a foreign fire shows a
        # believable but wrong number. Same form as the entry title.
        self.origin = f"{self.latitude:.2f}/{self.longitude:.2f}"
        # cluster id -> entity_id, filled in by the geo_location entities once
        # Home Assistant has assigned them. Lets the aggregate sensors point at
        # the actual fire entity instead of guessing its slug.
        self.entity_ids: dict[str, str] = {}
        # Last cycle's clusters, so a fire keeps its id — and with it its
        # entity and its history — while its centroid drifts. Memory only:
        # after a restart the entities are rebuilt anyway, and every fire that
        # has not drifted since gets the same id from its coordinates.
        self._previous_clusters: list[FirmsCluster] = []

    async def async_load_sources(self) -> None:
        """Restore the learned source history before the first refresh.

        Called from async_setup_entry rather than lazily: the very first cycle
        after a restart already filters, and without the history it would show
        every factory again for one cycle — a burst of fires that are not
        fires, right after a restart, is exactly the failure people report.
        """
        self._sources = PersistentSources.from_dict(
            await self._sources_store.async_load()
        )

    async def _async_update_data(self) -> FirmsData:
        results = await asyncio.gather(
            *(
                self.client.fetch(sat, self.window, self._bbox, FETCH_COUNT)
                for sat in self.satellites
            ),
            return_exceptions=True,
        )
        data = FirmsData()
        hotspots = []
        for sat, result in zip(self.satellites, results):
            if isinstance(result, FirmsAuthError):
                raise ConfigEntryAuthFailed from result
            if isinstance(result, BaseException):
                data.satellite_errors[sat] = str(result)
                _LOGGER.warning("FIRMS fetch failed for %s: %s", sat, result)
                continue
            if len(result) >= FETCH_COUNT:
                data.truncated = True
                _LOGGER.warning(
                    "FIRMS returned the maximum of %s features for %s — "
                    "results may be truncated",
                    FETCH_COUNT,
                    sat,
                )
            data.per_satellite[sat] = len(result)
            hotspots.extend(result)
        if not data.per_satellite and data.satellite_errors:
            raise UpdateFailed(f"All FIRMS fetches failed: {data.satellite_errors}")
        self._async_sync_truncation_issue(data.truncated)

        # Client-side filtering: exact radius (the bbox is a superset),
        # ignore zones, minimum confidence, minimum fire radiative power.
        # Read per cycle rather than cached in __init__: an options edit does
        # reload the entry (see __init__.py), but reading them here means this
        # holds regardless of how the zones got there.
        zones = self.config_entry.options.get(CONF_IGNORE_ZONES) or []
        in_radius: list = []
        for h in hotspots:
            dist = haversine_km(self.latitude, self.longitude, h.latitude, h.longitude)
            if dist <= self.radius_km:
                in_radius.append((h, dist))

        # Learn from everything inside the radius, before any filter runs. A
        # baseline computed from an already-filtered sample would sit too high
        # and suppress more than it should, which is the one direction this
        # must not fail in. Outside the radius is skipped: those detections can
        # never be shown, so their history would be dead weight in the store.
        self._sources.record(
            [
                (h.latitude, h.longitude, h.frp, (h.acq_datetime or "")[:10])
                for h, _ in in_radius
            ],
            dt_util.utcnow().date(),
        )
        self._sources_store.async_delay_save(self._sources.as_dict, 60)

        within: list = []
        ignored = 0
        auto_ignored = 0
        for h, dist in in_radius:
            if in_ignored_zone(h.latitude, h.longitude, zones):
                ignored += 1
                continue
            if self.auto_ignore and self._sources.is_suppressed(
                h.latitude, h.longitude, h.frp
            ):
                auto_ignored += 1
                continue
            if (
                self.min_confidence != "any"
                and CONFIDENCE_RANK.get(h.confidence or "nominal", 1)
                < CONFIDENCE_RANK[self.min_confidence]
            ):
                continue
            if self.min_frp and (h.frp or 0.0) < self.min_frp:
                continue
            within.append((h, dist))

        data.raw_detections = len(within)
        data.ignored_detections = ignored
        data.auto_ignored_detections = auto_ignored
        data.clusters = cluster_hotspots(
            within,
            CLUSTER_RADIUS_KM,
            (self.latitude, self.longitude),
            self._previous_clusters,
        )
        data.clusters_by_id = {c.id: c for c in data.clusters}
        # Only reached when the fetch succeeded — a failed cycle raises above
        # and leaves the previous set intact, so a single outage does not
        # renumber every fire.
        self._previous_clusters = data.clusters
        if data.clusters:
            data.places = await self._async_places(data.clusters)
            data.wind = await self._async_wind(data.clusters)
        data.observation_schedule = await self._async_observation_schedule()
        return data

    async def _async_observation_schedule(self) -> ObservationSchedule:
        """Predict previous/next configured coverage without blocking fire data."""
        try:
            schedule = await self.orbits.schedule(
                self.latitude, self.longitude, self.satellites
            )
        except OrbitHTTPError as err:
            self._orbit_http_status = err.status
            self._async_sync_orbit_issue(err.status)
            if not self._orbits_failing:
                self._orbits_failing = True
                _LOGGER.warning(
                    "Satellite observation times unavailable, continuing without them: %s",
                    err,
                )
            return ObservationSchedule()
        except OrbitError as err:
            self._orbit_http_status = None
            if not self._orbits_failing:
                self._orbits_failing = True
                _LOGGER.warning(
                    "Satellite observation times unavailable, continuing without them: %s",
                    err,
                )
            return ObservationSchedule()
        self._orbit_http_status = None
        self._async_sync_orbit_issue(None)
        if self._orbits_failing:
            _LOGGER.info("Satellite observation-time prediction recovered")
        self._orbits_failing = False
        return schedule

    @property
    def orbits_failing(self) -> bool:
        """Whether VIIRS observation prediction failed this cycle."""
        return self._orbits_failing

    @property
    def weather_failing(self) -> bool:
        """Whether the wind lookup is currently backed off. For diagnostics."""
        return self._weather_failing

    @property
    def places_failing(self) -> bool:
        """Whether the place dataset could not be read. For diagnostics."""
        return self._places_failing

    @property
    def sources(self) -> PersistentSources:
        """The learned persistent-source history. For diagnostics.

        No manual reset is offered, because the history expires on its own: a
        source that stops radiating stops refreshing its days, and once the
        retention window has carried enough of them away its span drops back
        under the threshold and it is simply no longer a known source. A plant
        that shuts down therefore fades out by itself, and in the meantime it
        has nothing left to suppress anyway.
        """
        return self._sources

    @callback
    def _async_sync_orbit_issue(self, status: int | None) -> None:
        """Surface CelesTrak HTTP failures once, without retrying them."""
        active = status is not None
        if active == self._orbit_issue_reported:
            return
        self._orbit_issue_reported = active
        issue_id = f"orbit_http_{self.config_entry.entry_id}"
        if not active:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="orbit_http",
            translation_placeholders={
                "name": self.config_entry.title,
                "status": str(status),
            },
        )

    @callback
    def _async_sync_truncation_issue(self, truncated: bool) -> None:
        """Put the truncation flag somewhere a person will actually see it.

        `truncated` means FIRMS cut the response off at its cap, so every
        number this entry produces is too low — the fire count, the distance to
        the nearest one, the strongest FRP. Nothing about a too-low fire count
        looks wrong, and an attribute plus a log line is not where anyone
        looks. The repairs dashboard is.
        """
        if truncated == self._truncation_reported:
            return
        self._truncation_reported = truncated
        issue_id = f"truncated_{self.config_entry.entry_id}"
        if not truncated:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="truncated",
            translation_placeholders={
                "name": self.config_entry.title,
                "cap": str(FETCH_COUNT),
            },
        )

    async def _async_places(
        self, clusters: list[FirmsCluster]
    ) -> dict[str, PlaceMatch]:
        """Nearest populated place for every fire, off the event loop.

        One executor hop for the whole cycle rather than one per fire: the
        first call also parses ~170k rows out of the bundled dataset, and
        every later one is a dict hit for a fire that has not moved.

        Never raises. A missing or corrupt dataset costs the place names and
        leaves the fire data untouched, exactly as a weather outage does.
        """
        try:
            return await self.hass.async_add_executor_job(self._lookup_places, clusters)
        except PlaceDataError as err:
            if not self._places_failing:
                self._places_failing = True
                _LOGGER.warning(
                    "Place names unavailable, continuing without them: %s", err
                )
            return {}

    def _lookup_places(self, clusters: list[FirmsCluster]) -> dict[str, PlaceMatch]:
        """Blocking half of _async_places. Runs in an executor thread.

        Pure: the flag lives on the event loop side, because the index reports
        an unreadable file exactly once and answers None quietly ever after —
        clearing the flag on a later empty cycle would report a broken install
        as healthy.
        """
        found: dict[str, PlaceMatch] = {}
        for cluster in clusters:
            match = self.places.nearest(cluster.latitude, cluster.longitude)
            if match is not None:
                found[cluster.id] = match
        return found

    async def _async_wind(
        self, clusters: list[FirmsCluster]
    ) -> dict[str, WindObservation]:
        """Wind at the N nearest fires — a busy box holds hundreds of them.

        Sequential on purpose: nothing here is latency-critical, and a burst
        of parallel requests is exactly what a rate limit is for. The first
        failure ends the cycle's lookups — on a throttle the client is backed
        off anyway, and a met.no that just failed does not need four more
        tries in the same second. Readings collected before the failure are
        kept.

        Never raises: the fire data is the product, so a weather outage costs
        wind attributes and nothing else.
        """
        winds: dict[str, WindObservation] = {}
        for cluster in clusters[: self.wind_fires]:
            try:
                wind = await self.weather.wind_at(cluster.latitude, cluster.longitude)
            except WeatherError as err:
                if not self._weather_failing:
                    self._weather_failing = True
                    _LOGGER.warning(
                        "Wind lookup failed, continuing without wind attributes: %s",
                        err,
                    )
                else:
                    _LOGGER.debug("Wind lookup still failing: %s", err)
                return winds
            if wind is not None:
                winds[cluster.id] = wind
        self._weather_failing = False
        return winds
