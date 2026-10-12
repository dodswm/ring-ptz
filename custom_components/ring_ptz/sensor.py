"""Pan and tilt angle sensors (updated after each command; no polling, which would open Ring sessions)."""
from __future__ import annotations

from homeassistant.components.sensor import RestoreSensor
from homeassistant.const import DEGREE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PtzCamera, RingPtzConfigEntry

SENSORS = [("pan", "panAngle"), ("tilt", "tiltAngle")]


async def async_setup_entry(hass: HomeAssistant, entry: RingPtzConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(PtzAngleSensor(cam, key, field) for cam in entry.runtime_data.values() for key, field in SENSORS)


class PtzAngleSensor(RestoreSensor):
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = DEGREE
    _attr_suggested_display_precision = 0
    _attr_should_poll = False

    def __init__(self, camera: PtzCamera, key: str, field: str) -> None:
        self._camera = camera
        self._field = field
        self._attr_translation_key = key
        self._attr_unique_id = f"{camera.info['id']}_{key}_angle"
        self._attr_device_info = camera.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value
        self.async_on_remove(self._camera.add_listener(self._updated))

    @callback
    def _updated(self) -> None:
        if (value := self._camera.angles.get(self._field)) is not None:
            self._attr_native_value = value
            self.async_write_ha_state()
