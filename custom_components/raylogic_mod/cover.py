"""Raylogic MOD2U / MOD4U curtain platform.

A curtain uses a different frame shape (command 0x27/0x26 instead of 0x1A)
and, like CCT, is a PAIRED mode: when one channel of a pair is configured as
a curtain, the whole pair becomes a single logical curtain entity.

The curtain wire frame is now DERIVED entirely from the channel number
(protocol.py -> curtain_slot_for_channel / curtain_frame, and the Curtain
block in const.py). Previously every pair needed hardcoded literal bytes,
so curtains only worked on the one device that had been captured. Now an
entity is created for every curtain channel, whatever Area (1-16) the
device is in.

CCT (single/double driver CCT) is supported, but as a `light` entity
(colour temperature control) - see light.py, RaylogicModCctLight. It is
referenced here only so that a CCT-type channel never accidentally gets an
additional cover entity.
"""
from __future__ import annotations
import logging

from homeassistant.components.cover import CoverEntity, CoverEntityFeature, CoverDeviceClass
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, CH_TYPE_CURTAIN, CH_TYPE_CCT
from .protocol import RaylogicModDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    device: RaylogicModDevice = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for ch_num, state in device.channel_states.items():
        if state.get("type") == CH_TYPE_CURTAIN:
            # BUG FIX: there used to be a gate here checking whether
            # hardcoded curtain bytes for this pair existed in const.py.
            # Literal bytes existed for only 2 pairs, so no curtain entity
            # was created from the third pair onwards. The curtain frame is
            # now derived entirely from the channel number (protocol.py's
            # curtain_slot_for_channel/curtain_frame), so an entity can
            # always be created for every curtain channel, whatever Area
            # (1-16) the device is in and whatever its channel numbers are.
            _LOGGER.debug(
                "Raylogic %s %s: curtain channel %d -> device pair %d, "
                "global curtain slot %d (frame *AR=%s).",
                device.model_name, device.ip, ch_num,
                device.pair_index_for_channel(ch_num) + 1,
                device.curtain_slot_for_channel(ch_num),
                device.curtain_frame(ch_num, "open"),
            )
            entities.append(RaylogicModCover(hass, entry, device, ch_num, state))
        elif state.get("type") == CH_TYPE_CCT:
            _LOGGER.debug(
                "Raylogic %s %s: channel %d is of type CCT - no entity is "
                "created on this (cover) platform; see the 'light' platform "
                "(RaylogicModCctLight).", device.model_name, device.ip, ch_num,
            )
    if entities:
        _LOGGER.info(
            "Setting up %d %s curtain channel(s) on %s",
            len(entities), device.model_name, device.ip,
        )
        async_add_entities(entities)


class RaylogicModCover(CoverEntity, RestoreEntity):
    _attr_has_entity_name = False
    _attr_device_class = CoverDeviceClass.CURTAIN
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )

    def __init__(self, hass, entry, device: RaylogicModDevice, ch_num, initial_state):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._ch_num = ch_num
        suffix = device.ip_suffix
        area = initial_state.get("area", 0)
        model = device.model
        self._attr_unique_id = f"{device.stable_id}_{model}_ch{ch_num}"
        self._attr_name = f"{model}_{suffix}_area{area}_ch{ch_num}_curtain"
        self._is_closed = not initial_state.get("on", False)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.stable_id)},
            name=f"Raylogic {self._device.model_name} ({self._device.ip})",
            manufacturer="Raylogic",
            model=f"{self._device.model_name} - {self._device.model_desc}",
            sw_version=self._device.fw_version,
        )

    @property
    def available(self):
        # UX FIX (explicit user requirement): the dashboard must never
        # show "Unavailable" (greyed out), even while the device is
        # disconnecting/reconnecting in the background. The entity
        # always keeps showing its last known state (on/off/brightness/
        # etc.). The protocol layer handles disconnects silently in the
        # background (fast reconnect + command queue-and-replay), so the
        # availability signal is no longer used for UI visibility.
        return True

    @property
    def is_closed(self):
        return self._is_closed

    async def async_open_cover(self, **kwargs):
        await self._device.set_cover(self._ch_num, "open")
        self._is_closed = False
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs):
        await self._device.set_cover(self._ch_num, "close")
        self._is_closed = True
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs):
        await self._device.set_cover(self._ch_num, "stop")

    async def async_added_to_hass(self):
        # SCALE FIX: entry-scoped dispatcher signal instead of a global
        # hass.bus event - see __init__.py's _handle_state_update comment.
        self.async_on_remove(
            async_dispatcher_connect(
                self._hass,
                f"{DOMAIN}_{self._entry.entry_id}_state_update",
                self._on_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self._hass,
                f"{DOMAIN}_{self._entry.entry_id}_available",
                self._on_available,
            )
        )

        # F1 (v1.7.0): the module never reports its state on its own and
        # answers no state query, so after an HA restart/reload the last
        # known state is restored (a real frame that already arrived wins).
        last = await self.async_get_last_state()
        if last is not None and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            if self._device.restore_channel(self._ch_num, self._restore_payload(last)):
                self._on_update(self._ch_num, self._device.channel_states[self._ch_num])

    @staticmethod
    def _restore_payload(last) -> dict:
        return {"on": last.state in ("open", "opening")}

    @callback
    def _on_update(self, ch_num, state):
        if ch_num != self._ch_num:
            return
        if "on" in state:
            self._is_closed = not bool(state["on"])
        self.async_write_ha_state()

    @callback
    def _on_available(self, _available):
        self.async_write_ha_state()
