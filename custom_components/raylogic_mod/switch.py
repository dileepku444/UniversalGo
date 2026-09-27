"""Raylogic MOD2U / MOD4U relay/switch platform.

Every channel configured as type 'Relay' in the config flow (or learned as
a relay in LEARN mode) becomes a switch entity on this platform.
"""
from __future__ import annotations
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, CH_TYPE_RELAY
from .protocol import RaylogicModDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    device: RaylogicModDevice = hass.data[DOMAIN][entry.entry_id]
    entities = [
        RaylogicModSwitch(hass, entry, device, ch_num, state)
        for ch_num, state in device.channel_states.items()
        if state.get("type") == CH_TYPE_RELAY
    ]
    if entities:
        _LOGGER.info("Setting up %d relay channel(s) on %s", len(entities), device.ip)
        async_add_entities(entities)
    else:
        _LOGGER.info(
            "Raylogic %s: no channel has been learned yet (LEARN mode). "
            "Switch each channel ON/OFF once from the Raylogic GO app or a "
            "physical switch - its entity will be created automatically.",
            device.ip,
        )

    # In LEARN mode new channels are learned at runtime - whenever that
    # happens, protocol.py calls this callback so the entity can be added
    # DYNAMICALLY (without restarting Home Assistant).
    def _on_new_channel(ch_num, state):
        async_add_entities([RaylogicModSwitch(hass, entry, device, ch_num, state)])

    device.new_channel_callback = _on_new_channel


class RaylogicModSwitch(SwitchEntity, RestoreEntity):
    _attr_has_entity_name = False
    _attr_entity_registry_enabled_default = True

    def __init__(self, hass, entry, device: RaylogicModDevice, ch_num, initial_state):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._ch_num = ch_num
        suffix = device.ip_suffix
        area = initial_state.get("area", 0)
        self._attr_unique_id = f"{device.stable_id}_{device.model}_ch{ch_num}"
        self._attr_name = f"{device.model}_{suffix}_area{area}_ch{ch_num}"
        self._is_on = initial_state.get("on", False)

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
    def is_on(self):
        return self._is_on

    async def async_turn_on(self, **kwargs):
        await self._device.set_relay(self._ch_num, True)
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._device.set_relay(self._ch_num, False)
        self._is_on = False
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        # SCALE FIX: this used to listen to a GLOBAL hass.bus event (fired
        # for every entity of every device); it now uses a dispatcher signal
        # scoped to this device (entry_id), so updates from other devices
        # never reach this entity, no matter how many devices (100+) are
        # added to Home Assistant.
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
        return {"on": last.state == "on"}

    @callback
    def _on_update(self, ch_num, state):
        if ch_num == self._ch_num and "on" in state:
            self._is_on = bool(state["on"])
            self.async_write_ha_state()

    @callback
    def _on_available(self, _available):
        self.async_write_ha_state()
