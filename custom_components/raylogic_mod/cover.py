"""Raylogic MOD2U / MOD4U curtain platform.

Curtain ek alag frame-shape use karta hai (cmd 0x27/0x26, na ki 0x1A) aur
CTC ki tarah ek PAIRED mode hai - jis pair ka ek channel curtain banaya
jaaye, wahi poora pair ek logical curtain entity ban jaata hai.

Curtain ka wire frame ab poori tarah channel number se DERIVE hota hai
(protocol.py -> curtain_slot_for_channel / curtain_frame, aur const.py ka
Curtain block) - pehle yahan har pair ke liye hardcoded literal bytes
chahiye hote the, jiski wajah se curtain sirf usi ek device par chalti
thi jiska capture liya gaya tha. Ab har curtain channel ki entity banti
hai, chahe device kisi bhi Area (1-16) mein ho.

CTC (Double/Single Driver CCT) ab supported hai, lekin ek `light` entity
ke taur par (Colour Temperature control) - dekho light.py -
RaylogicModCtcLight. Yahan cover.py mein sirf isliye reference hai taaki
CTC-type channel ke liye galti se doosri cover entity na ban jaaye.
"""
from __future__ import annotations
import logging

from homeassistant.components.cover import CoverEntity, CoverEntityFeature, CoverDeviceClass
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, CH_TYPE_CURTAIN, CH_TYPE_CTC
from .protocol import RaylogicModDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    device: RaylogicModDevice = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for ch_num, state in device.channel_states.items():
        if state.get("type") == CH_TYPE_CURTAIN:
            # BUG FIX: pehle yahan ek "kya is pair ke hardcoded curtain
            # bytes const.py mein maujood hain?" wala gate tha - sirf 2
            # pairs ke liye literal bytes the, isliye teesre pair se aage
            # ki curtain entity banti hi nahi thi. Ab curtain frame poori
            # tarah channel number se derive hota hai (protocol.py ka
            # curtain_slot_for_channel/curtain_frame), isliye har curtain
            # channel ki entity hamesha ban sakti hai - chahe device kisi
            # bhi Area (1-16) mein ho aur uske channel numbers kuch bhi
            # hon.
            _LOGGER.debug(
                "Raylogic %s %s: curtain channel %d -> device pair %d, "
                "global curtain slot %d (frame *AR=%s).",
                device.model_name, device.ip, ch_num,
                device.pair_index_for_channel(ch_num) + 1,
                device.curtain_slot_for_channel(ch_num),
                device.curtain_frame(ch_num, "open"),
            )
            entities.append(RaylogicModCover(hass, entry, device, ch_num, state))
        elif state.get("type") == CH_TYPE_CTC:
            _LOGGER.debug(
                "Raylogic %s %s: channel %d CTC type hai - is platform "
                "(cover) mein entity nahi banti, dekho 'light' platform "
                "(RaylogicModCtcLight).", device.model_name, device.ip, ch_num,
            )
    if entities:
        _LOGGER.info(
            "Setting up %d %s curtain channel(s) on %s",
            len(entities), device.model_name, device.ip,
        )
        async_add_entities(entities)


class RaylogicModCover(CoverEntity):
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
