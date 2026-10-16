import logging
import asyncio
import os
from contextlib import suppress

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import discovery
from homeassistant.setup import async_setup_component
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]
CARD_RESOURCE_URL = "/china_telecom/ctm-telecom-card.js"
LEGACY_CARD_RESOURCE_URL = "/local/ctm-telecom-card.js"
CARD_RESOURCE_PREFIXES = (CARD_RESOURCE_URL, LEGACY_CARD_RESOURCE_URL)

# Home Assistant sets up every config entry of a domain concurrently, so the
# "check flag -> await -> set flag" pattern used below raced: with two phone
# numbers both entries passed the check, both called
# hass.http.async_register_static_paths() for the same url_path, and the loser
# died with
#     RuntimeError: Added route will never be executed, method GET is already
#     registered
# which failed the whole config entry (every sensor of that number went
# unavailable). A reload then worked, because the first entry had meanwhile set
# the flag. Serialise the bootstrap and claim the flag before the first await.
_CARD_SETUP_LOCK = asyncio.Lock()

# Version of this local patch, surfaced on the device page so it is obvious
# whether the patched code is running.
PATCH_VERSION = "1.1.7+local.1"


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the China Telecom component from configuration.yaml."""
    if DOMAIN in config:
        for entry_config in config[DOMAIN]:
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": SOURCE_IMPORT},
                    data=entry_config,
                )
            )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up China Telecom integration from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    try:
        await async_register_lovelace_resource(hass)
    except Exception:  # noqa: BLE001
        # The Lovelace card is cosmetic. Never let it take the sensors down.
        _LOGGER.warning(
            "Failed to register the CTM Telecom Lovelace card; continuing without it",
            exc_info=True,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        domain_data = hass.data.get(DOMAIN) or {}
        domain_data.pop(entry.entry_id, None)
        (domain_data.get("login_attempts") or {}).pop(entry.entry_id, None)

    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload China Telecom when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_register_lovelace_resource(hass: HomeAssistant) -> None:
    """Register the CTM Telecom card as a Lovelace resource."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    async with _CARD_SETUP_LOCK:
        if domain_data.get("card_resource_registered"):
            return
        # Claim the slot before any await so a concurrently setting up entry
        # cannot start the same registration work.
        domain_data["card_resource_registered"] = True
        try:
            await async_setup_component(hass, "lovelace", {})
            await async_register_card_static_path(hass)
            await async_register_card_resource(hass)
        except Exception:
            domain_data["card_resource_registered"] = False
            raise


async def async_register_card_resource(hass: HomeAssistant) -> None:
    """Make the card resource known to the frontend."""
    version = await hass.async_add_executor_job(_card_resource_version, hass)
    resource_url = f"{CARD_RESOURCE_URL}?v={version}"

    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        _LOGGER.warning("Can not access Lovelace data, injecting card resource only")
        add_extra_js_url(hass, resource_url)
        return

    resources = lovelace.resources if hasattr(lovelace, "resources") else lovelace.get("resources")
    if resources is None:
        _LOGGER.warning("Can not access Lovelace resources, injecting card resource only")
        add_extra_js_url(hass, resource_url)
        return

    with suppress(Exception):
        if hasattr(resources, "async_get_info"):
            await resources.async_get_info()
        elif not resources.loaded:
            await resources.async_load()

    for item in resources.async_items():
        if not item.get("url", "").startswith(CARD_RESOURCE_PREFIXES):
            continue

        if item["url"] != resource_url and isinstance(resources, ResourceStorageCollection):
            await resources.async_update_item(
                item["id"], {"res_type": "module", "url": resource_url}
            )
            _LOGGER.info("Updated CTM Telecom card resource: %s", resource_url)
        return

    if isinstance(resources, ResourceStorageCollection):
        await resources.async_create_item({"res_type": "module", "url": resource_url})
        _LOGGER.info("Registered CTM Telecom card resource: %s", resource_url)
    else:
        add_extra_js_url(hass, resource_url)
        _LOGGER.info("Injected CTM Telecom card resource: %s", resource_url)


async def async_register_card_static_path(hass: HomeAssistant) -> None:
    """Expose the bundled frontend card from the integration directory."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("card_static_path_registered"):
        return

    domain_data["card_static_path_registered"] = True
    try:
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    CARD_RESOURCE_URL,
                    hass.config.path("custom_components", DOMAIN, "www", "ctm-telecom-card.js"),
                    True,
                )
            ]
        )
    except RuntimeError as err:
        # Another code path already owns this route; the card still works.
        _LOGGER.debug("CTM Telecom card static path already registered: %s", err)
    except Exception:
        domain_data["card_static_path_registered"] = False
        raise


def _card_resource_version(hass: HomeAssistant) -> str:
    """Return a cache-busting version for the frontend card."""
    card_path = hass.config.path("custom_components", DOMAIN, "www", "ctm-telecom-card.js")
    with suppress(OSError):
        return str(int(os.path.getmtime(card_path)))
    return "1"
