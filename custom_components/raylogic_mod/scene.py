"""Raylogic GO app area scenes exposed as the Home Assistant Scene platform (v1.6.0).

Area scenes are bus-wide (not limited to the channels of a single
MOD2U/MOD4U/MOD2F), so exactly ONE set is created for the whole installation,
on the "scene host" entry (see __init__.claim_scene_host). Which areas and
scenes are created comes from the "Area scenes" field in the Configure dialog
of any device.
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
        # Address-based and host-independent: stays the same if the host entry changes.
        self._attr_unique_id = f"raylogic_mod_area{area}_scene{scene}"
        self._attr_name = f"Area {area} Scene {scene}"
        self._attr_device_info = scenes_device_info()
        self._attr_extra_state_attributes = {"area": area, "scene": scene}

    # No `available` override: like the other raylogic_mod entities this never
    # shows "Unavailable"; a recall for an offline module is queued instead.

    async def async_activate(self, **kwargs: Any) -> None:
        await async_recall_area_scene(self._hass, self._area, self._scene)
