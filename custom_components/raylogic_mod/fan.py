"""Raylogic MOD2U / MOD4U fan platform.

Model_Number_Mod2u.txt capture se confirmed: 00 1A <area> <level> <channel>,
level 0x01=off, 0x02..0x05 = speed 1-4 (25/50/75/100%).
"""
from __future__ import annotations
import logging

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, CH_TYPE_FAN
from .protocol import RaylogicModDevice

_LOGGER = logging.getLogger(__name__)

SPEED_STEPS = [0, 25, 50, 75, 100]


async def async_setup_entry(hass, entry, async_add_entities):
    device: RaylogicModDevice = hass.data[DOMAIN][entry.entry_id]
    entities = [
        RaylogicModFan(hass, entry, device, ch_num, state)
        for ch_num, state in device.channel_states.items()
        if state.get("type") == CH_TYPE_FAN
    ]
    if entities:
        _LOGGER.info("Setting up %d fan channel(s) on %s", len(entities), device.ip)
        async_add_entities(entities)


class RaylogicModFan(FanEntity):
    _attr_has_entity_name = False
    # HA (2024.8+) ab TURN_ON/TURN_OFF explicitly declare karwata hai, warna
    # UI ka toggle "does not support action fan.turn_on" error deta hai -
    # chahe async_turn_on/async_turn_off already implement kiye ho.
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_speed_count = 4

    def __init__(self, hass, entry, device: RaylogicModDevice, ch_num, initial_state):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._ch_num = ch_num
        suffix = device.ip_suffix
        area = initial_state.get("area", 0)
        self._attr_unique_id = f"{device.stable_id}_{device.model}_ch{ch_num}"
        self._attr_name = f"{device.model}_{suffix}_area{area}_ch{ch_num}_fan"
        self._is_on = initial_state.get("on", False)
        self._percentage = initial_state.get("percentage", 0)

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
        # UX FIX: user ne explicitly maanga - dashboard par kabhi
        # bhi "Unavailable" (grey) nahi dikhna chahiye, chahe device
        # background mein disconnect/reconnect ho raha ho. Entity
        # hamesha apni last-known state (On/Off/brightness/etc.)
        # dikhati rahegi. Underlying protocol layer disconnects
        # ko khud silently/background mein handle karta hai (fast
        # reconnect + command-queue-and-replay) - is availability
        # signal ko sirf UI-visibility ke liye use nahi karte ab.
        return True

    @property
    def is_on(self):
        return self._is_on

    @property
    def percentage(self):
        return self._percentage

    async def async_set_percentage(self, percentage: int):
        step = min(SPEED_STEPS, key=lambda s: abs(s - percentage))
        await self._device.set_fan(self._ch_num, step)
        self._percentage = step
        self._is_on = step > 0
        self.async_write_ha_state()

    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs):
        await self.async_set_percentage(percentage or self._percentage or 100)

    async def async_turn_off(self, **kwargs):
        await self._device.set_fan(self._ch_num, 0)
        self._is_on = False
        self._percentage = 0
        self.async_write_ha_state()

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

    @callback
    def _on_update(self, ch_num, state):
        if ch_num != self._ch_num:
            return
        if "on" in state:
            self._is_on = bool(state["on"])
        if "percentage" in state:
            self._percentage = state["percentage"]
        self.async_write_ha_state()

    @callback
    def _on_available(self, _available):
        self.async_write_ha_state()
