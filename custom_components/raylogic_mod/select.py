"""Har Area ke liye scene dropdown - two-way scene feedback (v1.6.0).

HA Scene entities stateless hote hain (konsa active hai nahi dikha sakte),
isliye har configured Area ke liye ek select: option chuno -> scene recall;
keypad / Raylogic app se scene recall ho -> kisi bhi module par aaya
*AR=000F<area><scene>00 echo is dropdown ko update kar deta hai.
"""
from __future__ import annotations
import logging
import time

from homeassistant.components.select import SelectEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_AREA_SCENE
from . import async_recall_area_scene, claim_scene_host, loaded_devices

_LOGGER = logging.getLogger(__name__)
_NONE = "—"
# Apna scene chunne ke baad itni der tak kisi DOOSRE scene ki stray/purani
# report ko ignore karo (reference me yahi bug tha: HA se scene 2 chuna,
# ~5s baad ek purana echo aakar dropdown ko scene 1 par le gaya).
_OWN_CMD_GUARD = 30.0


def scenes_device_info() -> DeviceInfo:
    """Area scenes kisi ek module ke nahi - apna alag global device."""
    return DeviceInfo(
        identifiers={(DOMAIN, "area_scenes")},
        name="Raylogic MOD Area Scenes",
        manufacturer="Raylogic",
        model="Area Scenes",
    )


async def async_setup_entry(hass, entry, async_add_entities):
    scene_map = claim_scene_host(hass, entry)
    if not scene_map:
        return
    async_add_entities(
        RaylogicModAreaSceneSelect(hass, area, scenes)
        for area, scenes in scene_map.items()
    )


class RaylogicModAreaSceneSelect(SelectEntity):
    _attr_has_entity_name = True

    def __init__(self, hass, area: int, scenes: list[int]):
        self._hass = hass
        self._area = area
        self._attr_unique_id = f"raylogic_mod_area{area}_scene_select"
        self._attr_name = f"Area {area} Scene"
        self._attr_device_info = scenes_device_info()
        self._attr_options = [_NONE] + [f"Scene {s}" for s in scenes]
        cur = next(
            (d.active_scene[area] for d in loaded_devices(hass)
             if area in d.active_scene), None,
        )
        opt = f"Scene {cur}" if cur else _NONE
        self._attr_current_option = opt if opt in self._attr_options else _NONE
        self._last_cmd = 0.0
        self._last_sent: str | None = None

    async def async_select_option(self, option: str) -> None:
        # HA -> Raylogic
        if option == _NONE or option not in self._attr_options:
            return
        self._last_cmd = time.monotonic()
        self._last_sent = option
        await async_recall_area_scene(self._hass, self._area, int(option.split()[-1]))
        self._attr_current_option = option
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(
            self._hass, SIGNAL_AREA_SCENE, self._on_scene))

    @callback
    def _on_scene(self, area: int, scene: int):
        # Raylogic -> HA
        if area != self._area:
            return
        opt = f"Scene {scene}"
        if opt not in self._attr_options or opt == self._attr_current_option:
            return
        if (time.monotonic() - self._last_cmd < _OWN_CMD_GUARD
                and opt != self._last_sent):
            return
        self._attr_current_option = opt
        self.async_write_ha_state()
