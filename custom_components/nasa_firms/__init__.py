"""The NASA FIRMS Wildfire Monitor integration."""
from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_validation import config_entry_only_config_schema
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_loaded_integration

from .api import CelesTrakClient, FirmsClient, MetNoClient, PlaceIndex
from .const import (
    CONF_MAP_KEY,
    CONF_REGION,
    DATA_PLACES,
    DOMAIN,
    SOURCES_STORAGE_KEY,
    SOURCES_STORAGE_VERSION,
    USER_AGENT,
)
from .coordinator import FirmsCoordinator, NasaFirmsConfigEntry
from .services import async_setup_services

PLATFORMS = [Platform.GEO_LOCATION, Platform.SENSOR]

CONFIG_SCHEMA = config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain's actions.

    Done here rather than in async_setup_entry so the action exists exactly
    once, whatever the number of entries — it resolves the entry from the
    fire entity it is called with.
    """
    async_setup_services(hass)
    return True


@callback
def _async_repair_region(hass: HomeAssistant, entry: NasaFirmsConfigEntry) -> None:
    """Rewrite the one stored region name that never worked.

    `Russia_and_Asia` was offered in the setup dropdown from the first release
    but is not a FIRMS region — the service is called `Russia_Asia`, and the
    wrong name is an HTTP 400 on every single fetch. Anyone who picked it
    cannot repair it themselves any more either, because the dropdown is gone
    and the region follows from the coordinates now.

    Only this one value is touched. A region that is merely an odd choice for
    a location still works, and rewriting those would undo a deliberate pick
    in one of the overlaps.
    """
    if entry.data.get(CONF_REGION) != "Russia_and_Asia":
        return
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_REGION: "Russia_Asia"}
    )




@callback
def _async_orbit_client(hass: HomeAssistant, session) -> CelesTrakClient:
    """One CelesTrak client/cache shared by all FIRMS config entries."""
    return hass.data.setdefault(DOMAIN, {}).setdefault(
        "orbit_client", CelesTrakClient(session)
    )


@callback
def _async_place_index(hass: HomeAssistant) -> PlaceIndex:
    """The place-name index, created once and shared by every entry.

    The table is the largest thing this integration holds in memory, and it is
    the same table whatever an entry is watching — one per Home Assistant, not
    one per entry. Nothing is read from disk here; the index loads itself on
    first use, inside an executor.
    """
    return hass.data.setdefault(DOMAIN, {}).setdefault(DATA_PLACES, PlaceIndex())


async def async_setup_entry(hass: HomeAssistant, entry: NasaFirmsConfigEntry) -> bool:
    """Set up NASA FIRMS from a config entry."""
    _async_repair_region(hass, entry)
    session = async_get_clientsession(hass)
    client = FirmsClient(session, entry.data[CONF_MAP_KEY], entry.data[CONF_REGION])
    weather = MetNoClient(
        session,
        USER_AGENT.format(version=async_get_loaded_integration(hass, DOMAIN).version),
    )
    coordinator = FirmsCoordinator(
        hass, entry, client, weather, _async_place_index(hass), _async_orbit_client(hass, session)
    )
    # Before the first refresh, not after: that refresh already filters, and
    # without the learned history it would republish every known factory for
    # one cycle. A burst of fires that are not fires, right after a restart,
    # is precisely the thing someone opens an issue about.
    await coordinator.async_load_sources()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: NasaFirmsConfigEntry
) -> None:
    """Reload on options change so the coordinator picks up new filters."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: NasaFirmsConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: NasaFirmsConfigEntry) -> None:
    """Delete the learned source history along with the entry.

    The store is keyed per entry and nothing else references it, so removing
    the entry without this would leave a file in .storage that no code ever
    opens again — and it holds the coordinates of everything the entry has
    ever seen burn.
    """
    await Store(
        hass, SOURCES_STORAGE_VERSION, f"{SOURCES_STORAGE_KEY}.{entry.entry_id}"
    ).async_remove()
