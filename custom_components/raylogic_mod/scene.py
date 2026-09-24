"""Raylogic GO app ke area scenes - HA Scene platform (v1.6.0).

Area scenes bus-wide hain (kisi ek MOD2U/MOD4U/MOD2F ke channels tak
seemit nahi), isliye poori installation ke liye EK hi set banta hai, "scene
host" entry par (dekho __init__.claim_scene_host). Konse area/scene banenge
ye kisi bhi device ke Configure -> "Area scenes" field se aata hai.
"""
from __future__ import annotations
import logging
from typing import Any

from homeassistant.components.scene import Scene

from . import async_recall_area_scene, claim_scene_host
from .select import scenes_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    scene_map = claim_scene_host(hass, entry)
    if not scene_map:
        return
    entities = [
        RaylogicModAreaScene(hass, area, scene)
        for area, scenes in scene_map.items()
        for scene in scenes
    ]
    _LOGGER.info("Raylogic MOD: %d area scenes set up", len(entities))
    async_add_entities(entities)


class RaylogicModAreaScene(Scene):
    _attr_has_entity_name = True

    def __init__(self, hass, area: int, scene: int):
        self._hass = hass
        self._area = area
        self._scene = scene
        # Address-based aur host-independent: host entry badle to bhi same.
        self._attr_unique_id = f"raylogic_mod_area{area}_scene{scene}"
        self._attr_name = f"Area {area} Scene {scene}"
        self._attr_device_info = scenes_device_info()
        self._attr_extra_state_attributes = {"area": area, "scene": scene}

    # available override nahi - baaki raylogic_mod entities ki tarah kabhi
    # "Unavailable" nahi; offline module ke liye recall queue ho jaata hai.

    async def async_activate(self, **kwargs: Any) -> None:
        await async_recall_area_scene(self._hass, self._area, self._scene)
