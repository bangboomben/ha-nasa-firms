"""Aggregate sensors: hotspot count, nearest distance, max FRP."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfLength, UnitOfSpeed
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import cardinal, smoke_offset
from .const import ATTRIBUTION, ATTRIBUTION_WEATHER, DOMAIN
from .coordinator import FirmsCoordinator, FirmsData, NasaFirmsConfigEntry


@dataclass(frozen=True, kw_only=True)
class FirmsSensorDescription(SensorEntityDescription):
    """Sensor description with value/attribute extractors.

    `attributes_fn` takes the coordinator rather than just its data, because the
    nearest-hotspot attributes need the cluster-id -> entity_id register that
    the geo_location entities maintain.
    """

    value_fn: Callable[[FirmsData], Any]
    attributes_fn: Callable[[FirmsCoordinator], dict[str, Any]] | None = None
    # Set where the sensor can carry met.no data, so their CC BY credit shows
    # up exactly while it does.
    uses_weather: bool = False


def _nearest_attributes(coordinator: FirmsCoordinator) -> dict[str, Any]:
    """Pointer to the closest fire, its bearing, and the wind at it.

    The entity id is the useful part: it unlocks every attribute of that fire
    instead of only the fields duplicated here.

    `wind_bearing` is the direction the wind blows *from* at the fire's own
    coordinates, in the same frame as `bearing`, which is what makes the two
    directly comparable — the degrees are what any upwind/downwind template
    needs. `wind_direction` is the same value as a compass point, mirroring
    `bearing`/`direction`, because that is what reads well on a dashboard.

    All of them are plain observations — whether that is good or bad news
    depends on terrain, fuel and how the wind turns next, so the integration
    reports and the user judges.
    """
    clusters = coordinator.data.clusters
    wind = coordinator.data.nearest_wind
    if not clusters:
        return {
            "nearest_entity_id": None,
            "bearing": None,
            "direction": None,
            "wind_bearing": None,
            "wind_direction": None,
            "wind_speed": None,
            "smoke_offset": None,
        }
    nearest = clusters[0]  # coordinator sorts by distance
    has_offset = wind is not None and nearest.bearing is not None
    return {
        "nearest_entity_id": coordinator.entity_ids.get(nearest.id),
        "bearing": nearest.bearing,
        "direction": nearest.direction,
        "wind_bearing": round(wind.bearing) if wind else None,
        "wind_direction": cardinal(wind.bearing) if wind else None,
        "wind_speed": round(wind.speed, 1) if wind else None,
        # The angle between where that wind pushes the smoke and the line
        # from the fire to you — the finished number behind the card's
        # towards/past/away wording, so no template has to carry the
        # arithmetic. 0 = straight at you, 180 = straight away. Geometry,
        # not danger. This sensor keeps its None convention; on the fire
        # entities the same attributes are absent instead.
        "smoke_offset": (
            round(smoke_offset(wind.bearing, nearest.bearing)) if has_offset else None
        ),
    }


def _max_frp_attributes(coordinator: FirmsCoordinator) -> dict[str, Any]:
    """Pointer to the fire behind the maximum — the nearest_entity_id move.

    Asked for in the community thread by the one user running this as
    infrastructure: the sensor said how strong the strongest fire is, but not
    which one it is, so anyone wanting its bearing or confidence had to
    iterate the fires and re-find the maximum themselves. Ties go to the
    nearest of the tied fires, because the clusters arrive sorted by distance
    and max() keeps the first — the same fire the max_frp value comes from.
    """
    strongest = max(
        (c for c in coordinator.data.clusters if c.frp is not None),
        key=lambda c: c.frp,
        default=None,
    )
    if strongest is None:
        return {"strongest_entity_id": None}
    return {"strongest_entity_id": coordinator.entity_ids.get(strongest.id)}


def _satellite_observation_attributes(
    coordinator: FirmsCoordinator,
) -> dict[str, Any]:
    """Orbital facts for the next look and the previous look's identity."""
    observation = coordinator.data.next_observation
    previous = coordinator.data.previous_observation
    if observation is None:
        return {
            "satellite": None,
            "satellite_name": None,
            "norad_id": None,
            "window_start": None,
            "window_end": None,
            "closest_ground_track_km": None,
            "closest_subpoint_latitude": None,
            "closest_subpoint_longitude": None,
            "swath_km": None,
            "previous_observation": previous.closest.isoformat() if previous else None,
            "previous_satellite": previous.satellite if previous else None,
        }
    return {
        "satellite": observation.satellite,
        "satellite_name": observation.satellite_name,
        "norad_id": observation.norad_id,
        "window_start": observation.start.isoformat(),
        "window_end": observation.end.isoformat(),
        "closest_ground_track_km": observation.closest_ground_track_km,
        "closest_subpoint_latitude": observation.closest_subpoint_latitude,
        "closest_subpoint_longitude": observation.closest_subpoint_longitude,
        "swath_km": observation.swath_km,
        "previous_observation": previous.closest.isoformat() if previous else None,
        "previous_satellite": previous.satellite if previous else None,
    }


SENSORS: tuple[FirmsSensorDescription, ...] = (
    FirmsSensorDescription(
        key="hotspot_count",
        translation_key="hotspot_count",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fire",
        value_fn=lambda d: len(d.clusters),
        attributes_fn=lambda c: {
            "raw_detections": c.data.raw_detections,
            "per_satellite": c.data.per_satellite,
            "satellite_errors": c.data.satellite_errors,
            "truncated": c.data.truncated,
            "ignored_detections": c.data.ignored_detections,
            # Next to the manual zones' count on purpose: this filter picks its
            # own targets, so the number it drops belongs where someone will
            # trip over it, not only in a diagnostics download.
            "auto_ignored_detections": c.data.auto_ignored_detections,
        },
    ),
    FirmsSensorDescription(
        key="nearest_distance",
        translation_key="nearest_distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.nearest_km,
        attributes_fn=_nearest_attributes,
        uses_weather=True,
    ),
    # The same reading as the `wind_speed` attribute above, as an entity.
    # A bare number in the states view has no unit, and on this integration the
    # `km` of the distance sensor sits directly under it — a user in the
    # community thread read 4.4 m/s as 4.4 km/h and filed it as a bug against
    # a card showing 16 km/h for the same wind. As an entity Home Assistant
    # prints the unit itself and converts it to whatever the instance or the
    # user prefers, so the ambiguity cannot recur. The attribute stays: every
    # template this project has published reads it, and it is the raw value
    # that any calculation wants.
    FirmsSensorDescription(
        key="nearest_wind_speed",
        translation_key="nearest_wind_speed",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.nearest_wind.speed if d.nearest_wind else None,
        uses_weather=True,
    ),
    FirmsSensorDescription(
        key="max_frp",
        translation_key="max_frp",
        native_unit_of_measurement="MW",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heat-wave",
        suggested_display_precision=1,
        value_fn=lambda d: d.max_frp,
        attributes_fn=_max_frp_attributes,
    ),
    FirmsSensorDescription(
        key="satellite_observation",
        translation_key="satellite_observation",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:satellite-variant",
        value_fn=lambda d: d.next_observation.closest if d.next_observation else None,
        attributes_fn=_satellite_observation_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NasaFirmsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the aggregate sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        FirmsSensor(coordinator, entry, description) for description in SENSORS
    )


class FirmsSensor(CoordinatorEntity[FirmsCoordinator], SensorEntity):
    """One aggregate value derived from the current fire clusters."""

    _attr_has_entity_name = True
    entity_description: FirmsSensorDescription

    def __init__(
        self,
        coordinator: FirmsCoordinator,
        entry: NasaFirmsConfigEntry,
        description: FirmsSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="NASA FIRMS",
            model="Active fire data (VIIRS/MODIS)",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def attribution(self) -> str:
        """Credit met.no as well, but only while we are showing their data."""
        if self.entity_description.uses_weather and self.coordinator.data.nearest_wind:
            return f"{ATTRIBUTION}. {ATTRIBUTION_WEATHER}"
        return ATTRIBUTION

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return diagnostic attributes where defined."""
        if self.entity_description.attributes_fn:
            return self.entity_description.attributes_fn(self.coordinator)
        return None
