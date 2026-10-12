"""Ring Pan-Tilt: pan/tilt control for the Ring Pan-Tilt Indoor Cam.

Home Assistant's Ring integration shows the camera but can't move it. This integration speaks the
Ring app's own PTZ protocol (reverse-engineered; see ptz.py) over a short-lived live-view
signalling session, and adds buttons, angle sensors, services and a press-and-hold pad card.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from pathlib import Path
from typing import Any

from ring_doorbell import Auth, AuthenticationError, RingError
import voluptuous as vol

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util.ssl import client_context

from .const import (
    ATTR_DIRECTION,
    ATTR_DURATION,
    ATTR_PAN,
    ATTR_SPEED,
    ATTR_TILT,
    CARD_URL,
    CONF_DEVICES,
    CONF_HARDWARE_ID,
    CONF_TOKEN,
    DOMAIN,
    RING_DOMAIN,
)
from .ptz import DEFAULT_SPEED, DIRECTIONS, USER_AGENT, PtzError, RingPtz, async_discover

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type RingPtzConfigEntry = ConfigEntry[dict[int, PtzCamera]]

# HA 2026: device identifiers are unique per config entry, so sharing the Ring integration's
# identifier no longer merges devices; entities join its device via Entity.device_entry instead.
_PER_ENTRY_IDENTIFIERS = hasattr(dr.DeviceRegistry, "async_get_device_by_identifier")


class PtzCamera:
    """One pan-tilt camera: the PTZ client plus last known angles for the sensors."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, info: dict[str, Any], ptz: RingPtz) -> None:
        self.hass = hass
        self.entry = entry
        self.info = info
        self.ptz = ptz
        self.angles: dict[str, float] = {}
        self._listeners: list[CALLBACK_TYPE] = []
        self._refresh: CALLBACK_TYPE | None = None

    @callback
    def ring_device(self) -> dr.DeviceEntry | None:
        """The Ring integration's device for this camera, if it has registered one."""
        if not (ring_id := self.info.get("device_id")):
            return None
        registry = dr.async_get(self.hass)
        identifier = (RING_DOMAIN, ring_id)
        if _PER_ENTRY_IDENTIFIERS:
            for ring_entry in self.hass.config_entries.async_entries(RING_DOMAIN):
                if device := registry.async_get_device_by_identifier(identifier, ring_entry.entry_id):
                    return device
            return None
        return registry.async_get_device(identifiers={identifier})

    @callback
    def own_device(self) -> dr.DeviceEntry | None:
        """Our own device for this camera (only used when Ring has none)."""
        registry = dr.async_get(self.hass)
        identifier = (DOMAIN, str(self.info["id"]))
        if _PER_ENTRY_IDENTIFIERS:
            return registry.async_get_device_by_identifier(identifier, self.entry.entry_id)
        return registry.async_get_device(identifiers={identifier})

    @callback
    def link_entity(self, entity: Entity) -> None:
        """Put the entity on the Ring integration's camera device, else on our own device."""
        device = self.ring_device()
        if device is None:
            entity._attr_device_info = DeviceInfo(  # noqa: SLF001
                identifiers={(DOMAIN, str(self.info["id"]))},
                manufacturer="Ring", model="Pan-Tilt Indoor Cam", name=self.info["name"],
            )
        elif _PER_ENTRY_IDENTIFIERS:
            entity.device_entry = device  # HA 2026+: how an entity joins another integration's device
        else:
            entity._attr_device_info = DeviceInfo(identifiers={(RING_DOMAIN, self.info["device_id"])})  # noqa: SLF001

    async def run(self, action: Callable[[RingPtz], Awaitable[Any]], refresh: bool = True) -> Any:
        """Run a PTZ action, mapping errors for HA; refresh the angle sensors afterwards."""
        try:
            result = await action(self.ptz)
        except AuthenticationError as err:
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError("Ring login expired; reauthenticate Ring Pan-Tilt") from err
        except (PtzError, RingError, TimeoutError, OSError) as err:
            raise HomeAssistantError(f"Ring pan-tilt command failed: {err}") from err
        if refresh:
            self.schedule_refresh()
        return result

    @callback
    def schedule_refresh(self, delay: float = 2.0) -> None:
        """Read the angles once the camera has (probably) finished moving."""
        if self._refresh:
            self._refresh()

        @callback
        def _fire(_now: Any) -> None:
            self._refresh = None
            self.entry.async_create_background_task(self.hass, self.async_refresh(), "ring_ptz angles")

        self._refresh = async_call_later(self.hass, delay, _fire)

    async def async_refresh(self) -> None:
        if self.ptz.moving:
            self.schedule_refresh()
            return
        try:
            self.set_angles(await self.ptz.get())
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Reading pan/tilt angles failed: %r", err)

    @callback
    def set_angles(self, angles: dict[str, float]) -> None:
        self.angles = angles
        for listener in list(self._listeners):
            listener()

    @callback
    def add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def async_close(self) -> None:
        if self._refresh:
            self._refresh()
        await self.ptz.close()


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the pad card and register the services."""
    integration = await async_get_integration(hass, DOMAIN)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(Path(__file__).parent / "frontend" / "ptz-pad-card.js"), False)]
    )
    add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}")
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RingPtzConfigEntry) -> bool:
    @callback
    def save_token(token: dict[str, Any]) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKEN: token})

    auth = Auth(USER_AGENT, entry.data[CONF_TOKEN], save_token, entry.data[CONF_HARDWARE_ID],
                http_client_session=async_get_clientsession(hass))
    devices = entry.data[CONF_DEVICES]
    try:
        # Device list only (no Ring session): picks up renames and newly added cameras
        if (found := await async_discover(auth)) and found != devices:
            devices = found
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DEVICES: devices})
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed(err) from err
    except (RingError, TimeoutError, OSError) as err:
        raise ConfigEntryNotReady(err) from err

    ssl_context = client_context()
    entry.runtime_data = {d["id"]: PtzCamera(hass, entry, d, RingPtz(auth, d["id"], ssl_context)) for d in devices}

    # Our own device is only a fallback: drop it once the Ring integration has the camera, so its
    # entities are re-created on the Ring device (0.1.0 created a duplicate on HA 2026).
    registry = dr.async_get(hass)
    for camera in entry.runtime_data.values():
        if (ring := camera.ring_device()) and (own := camera.own_device()) and own.id != ring.id:
            registry.async_remove_device(own.id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry) -> bool:
    """Allow deleting our fallback device from the UI."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RingPtzConfigEntry) -> bool:
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        for camera in entry.runtime_data.values():
            await camera.async_close()
    return unloaded


def _cameras_for(hass: HomeAssistant, call: ServiceCall) -> list[PtzCamera]:
    """Resolve the call's device_id(s) to loaded cameras."""
    loaded = [cam for entry in hass.config_entries.async_loaded_entries(DOMAIN) for cam in entry.runtime_data.values()]
    cameras: list[PtzCamera] = []
    for device_id in call.data[ATTR_DEVICE_ID]:
        # The camera's entities live on the Ring integration's device, or on our fallback device
        found = [cam for cam in loaded
                 if device_id in {d.id for d in (cam.ring_device(), cam.own_device()) if d}]
        if not found:
            raise ServiceValidationError(f"{device_id} is not a Ring pan-tilt camera")
        cameras.extend(found)
    return cameras


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    device = {vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string])}
    direction = vol.In(list(DIRECTIONS))
    actions: dict[str, tuple[dict, Callable[[ServiceCall], Callable[[RingPtz], Awaitable[Any]]]]] = {
        "step": ({vol.Required(ATTR_DIRECTION): direction},
                 lambda call: lambda ptz: ptz.step(call.data[ATTR_DIRECTION])),
        "move": ({vol.Required(ATTR_DIRECTION): direction,
                  vol.Optional(ATTR_SPEED, default=DEFAULT_SPEED): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=0.63)),
                  vol.Optional(ATTR_DURATION, default=3.0): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=10))},
                 lambda call: lambda ptz: ptz.move(call.data[ATTR_DIRECTION], call.data[ATTR_SPEED], call.data[ATTR_DURATION])),
        "stop": ({}, lambda call: lambda ptz: ptz.stop()),
        "home": ({}, lambda call: lambda ptz: ptz.home()),
        "prepare": ({}, lambda call: lambda ptz: ptz.prepare()),
        "set_position": ({vol.Required(ATTR_PAN): vol.Coerce(float), vol.Required(ATTR_TILT): vol.Coerce(float)},
                         lambda call: lambda ptz: ptz.set(call.data[ATTR_PAN], call.data[ATTR_TILT])),
    }
    for name, (fields, make_action) in actions.items():
        async def handle(call: ServiceCall, make_action=make_action, name=name) -> None:
            for camera in _cameras_for(hass, call):
                await camera.run(make_action(call), refresh=name not in ("prepare", "move"))

        hass.services.async_register(DOMAIN, name, handle, vol.Schema({**device, **fields}))

    async def handle_get_position(call: ServiceCall) -> dict[str, Any]:
        result = {}
        for camera in _cameras_for(hass, call):
            angles = await camera.run(lambda ptz: ptz.get(), refresh=False)
            camera.set_angles(angles)
            result[camera.info["name"]] = {"pan": angles.get("panAngle"), "tilt": angles.get("tiltAngle")}
        return result

    hass.services.async_register(DOMAIN, "get_position", handle_get_position, vol.Schema(device),
                                 supports_response=SupportsResponse.ONLY)
