"""Raylogic MOD2U / MOD4U dimmer platform.

Model_Number_Mod2u.txt capture se confirmed: 00 1A <area> <level> <channel>,
level 0x01=full, 0xFF=off, in-between=dim curve (linear approximation).
"""
from __future__ import annotations
import logging

from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    DOMAIN, CH_TYPE_DIMMER, CH_TYPE_CTC,
    CTC_MIN_KELVIN, CTC_MAX_KELVIN, CTC_DEFAULT_KELVIN,
)
from .protocol import RaylogicModDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    device: RaylogicModDevice = hass.data[DOMAIN][entry.entry_id]
    entities = [
        RaylogicModLight(hass, entry, device, ch_num, state)
        for ch_num, state in device.channel_states.items()
        if state.get("type") == CH_TYPE_DIMMER
    ]
    entities += [
        RaylogicModCtcLight(hass, entry, device, ch_num, state)
        for ch_num, state in device.channel_states.items()
        if state.get("type") == CH_TYPE_CTC
    ]
    if entities:
        _LOGGER.info(
            "Setting up %d light channel(s) (dimmer+CTC) on %s",
            len(entities), device.ip,
        )
        async_add_entities(entities)


class RaylogicModLight(LightEntity):
    _attr_has_entity_name = False
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, hass, entry, device: RaylogicModDevice, ch_num, initial_state):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._ch_num = ch_num
        suffix = device.ip_suffix
        area = initial_state.get("area", 0)
        self._attr_unique_id = f"{device.stable_id}_{device.model}_ch{ch_num}"
        self._attr_name = f"{device.model}_{suffix}_area{area}_ch{ch_num}_dimmer"
        self._is_on = initial_state.get("on", False)
        self._brightness = initial_state.get("brightness", 0)

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
    def brightness(self):
        return self._brightness

    async def async_turn_on(self, **kwargs):
        brightness = kwargs.get("brightness", self._brightness or 255)
        await self._device.set_dimmer(self._ch_num, brightness)
        # State device.channel_states se lo (na ki yahan diye gaye raw
        # brightness se) - set_dimmer andar 1% ya usse kam brightness ko
        # "snap to OFF" karta hai (user ke kehne par: slider ekdum neeche
        # jaaye to light poori tarah OFF ho, "on but barely lit" nahi),
        # aur pehle wala 0-brightness "ON" mismatch bhi isi tarah avoid
        # hota hai - dono jagah alag-alag logic likhne se woh drift kar
        # sakte the.
        st = self._device.channel_states.get(self._ch_num, {})
        self._is_on = bool(st.get("on", False))
        self._brightness = st.get("brightness", 0)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._device.set_dimmer(self._ch_num, 0)
        self._is_on = False
        self._brightness = 0
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
        if "brightness" in state:
            self._brightness = state["brightness"]
        self.async_write_ha_state()

    @callback
    def _on_available(self, _available):
        self.async_write_ha_state()


class RaylogicModCtcLight(LightEntity):
    """CTC (tunable-white / colour-temperature) channel.

    Supports BOTH sub-modes captured in Model_Number_Mod2u.txt (Area 16):
    'single' driver (CW/WW, one physical channel-pair, *AR= frames - this
    is the mode in the user's own Mod Settings screenshot) and 'double'
    driver (separate warm+cool channels, one combined *AZ= frame). Which
    one applies is chosen per-channel in config_flow.py (see const.py's
    CTC comment block for the full wire-format writeup and confidence
    notes - the double-driver formula in particular is derived rather
    than a literal captured table, since only pure-brightness and
    pure-colour sweeps were captured, not a combined change).
    """

    _attr_has_entity_name = False
    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = CTC_MIN_KELVIN
    _attr_max_color_temp_kelvin = CTC_MAX_KELVIN

    def __init__(self, hass, entry, device: RaylogicModDevice, ch_num, initial_state):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._ch_num = ch_num
        suffix = device.ip_suffix
        area = initial_state.get("area", 0)
        self._attr_unique_id = f"{device.stable_id}_{device.model}_ch{ch_num}_ctc"
        self._attr_name = f"{device.model}_{suffix}_area{area}_ch{ch_num}_ctc"
        self._is_on = initial_state.get("on", False)
        self._brightness = initial_state.get("brightness", 0)
        self._kelvin = initial_state.get("color_temp_kelvin", CTC_DEFAULT_KELVIN)

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
    def brightness(self):
        return self._brightness

    @property
    def color_temp_kelvin(self):
        return self._kelvin

    async def async_turn_on(self, **kwargs):
        brightness = kwargs.get("brightness")
        kelvin = kwargs.get("color_temp_kelvin")
        # Sirf turn_on (bina brightness diye) call ho to full brightness
        # par ON karo - jaisa regular dimmer light bhi karta hai.
        if brightness is None and not self._is_on:
            brightness = self._brightness or 255
        await self._device.set_ctc(
            self._ch_num, brightness=brightness, color_temp_kelvin=kelvin,
        )
        # State device.channel_states se lo - set_ctc andar 1% (ya kam)
        # brightness ko "snap to OFF" karta hai (slider ekdum neeche jaaye
        # to light poori tarah OFF ho jaaye), aur brightness=0 wala
        # ON/OFF mismatch bhi isi se sahi reflect hota hai. Kelvin-only
        # calls (brightness=None) mein device ka "on" waisa hi rehta hai
        # jaisa pehle tha, isliye wahan bhi wahi se lo.
        st = self._device.channel_states.get(self._ch_num, {})
        self._is_on = bool(st.get("on", False))
        self._brightness = st.get("brightness", 0)
        if kelvin is not None:
            self._kelvin = kelvin
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._device.set_ctc(self._ch_num, brightness=0)
        self._is_on = False
        self._brightness = 0
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
        if "brightness" in state:
            self._brightness = state["brightness"]
        if "color_temp_kelvin" in state:
            self._kelvin = state["color_temp_kelvin"]
        self.async_write_ha_state()

    @callback
    def _on_available(self, _available):
        self.async_write_ha_state()
