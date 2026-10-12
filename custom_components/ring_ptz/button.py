"""Buttons: one step left/right/up/down, and go to the Home Position."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PtzCamera, RingPtzConfigEntry

BUTTONS = [  # (key, direction or None for home)
    ("pan_left", "left"),
    ("pan_right", "right"),
    ("tilt_up", "up"),
    ("tilt_down", "down"),
    ("home", None),
]


async def async_setup_entry(hass: HomeAssistant, entry: RingPtzConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(PtzButton(cam, key, direction) for cam in entry.runtime_data.values() for key, direction in BUTTONS)


class PtzButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, camera: PtzCamera, key: str, direction: str | None) -> None:
        self._camera = camera
        self._direction = direction
        self._attr_translation_key = key
        self._attr_unique_id = f"{camera.info['id']}_{key}"
        camera.link_entity(self)

    async def async_press(self) -> None:
        if self._direction:
            await self._camera.run(lambda ptz: ptz.step(self._direction))
        else:
            await self._camera.run(lambda ptz: ptz.home())
