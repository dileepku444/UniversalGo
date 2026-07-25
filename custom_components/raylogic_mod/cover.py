"""Raylogic MOD2U / MOD4U curtain platform.

Model_Number_Mod4u.txt - REAL MOD4U capture (Area 7, "Curtain Mode / All
Code") - CONFIRMED for BOTH pairs (curtain ek alag frame-shape use karta
hai, formula se derive nahi hua, aur CTC ki tarah ye bhi ek PAIRED mode
hai - jis pair ka ek channel curtain banaya jaaye, wahi pura pair ek
logical curtain entity ban jaata hai). MOD2U par sirf Pair 1 exist karta
hai, jo isi confirmed data se already covered hai.

Entity creation CURTAIN_PAIR_COMMANDS (const.py) se generically derive
hoti hai - jis pair ke liye literal bytes maujood hain, uski entity ban
jaati hai; agar kabhi koi pair "None" reh jaaye (future device variant),
uske liye entity skip ho kar ek Repairs issue ban jaata hai, hardcoded
index par depend nahi karta.

CTC (Double/Single Driver CCT) ab supported hai, lekin ek `light` entity
ke taur par (Colour Temperature control) - dekho light.py -
RaylogicModCtcLight. Yahan cover.py mein sirf isliye reference hai taaki
CTC-type channel ke liye galti se doosri cover entity na ban jaaye.
"""
from __future__ import annotations
import logging

from homeassistant.components.cover import CoverEntity, CoverEntityFeature, CoverDeviceClass
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, CH_TYPE_CURTAIN, CH_TYPE_CTC, CURTAIN_PAIR_COMMANDS
from .protocol import RaylogicModDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    device: RaylogicModDevice = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for ch_num, state in device.channel_states.items():
        if state.get("type") == CH_TYPE_CURTAIN:
            pair_index = device.pair_index_for_channel(ch_num)
            cmd_map = CURTAIN_PAIR_COMMANDS.get(pair_index, {})
            confirmed = all(cmd_map.get(k) for k in ("open", "close", "stop"))
            if confirmed:
                entities.append(RaylogicModCover(hass, entry, device, ch_num, state))
            else:
                _LOGGER.warning(
                    "Raylogic %s %s: channel %d curtain type hai "
                    "(Pair %d), lekin uske curtain bytes abhi confirmed "
                    "nahi hain - entity nahi banai. App se ek baar "
                    "open/close karke log share karo.",
                    device.model_name, device.ip, ch_num, pair_index + 1,
                )
                # NAYA: sirf log par depend nahi karte (log level user ke
                # configuration.yaml me kabhi WARNING se upar cap ho sakta
                # hai aur ye line kabhi dikhti hi nahi) - ek Repairs issue
                # bhi banao jo Settings > Repairs me hamesha dikhega, chahe
                # logger config kuch bhi ho. Isse turant pata chal jaata hai
                # ki "missing entity" ek known/expected gap hai, koi random
                # crash nahi.
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    f"curtain_pair_unconfirmed_{entry.entry_id}_{pair_index}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="curtain_pair_unconfirmed",
                    translation_placeholders={
                        "ip": device.ip,
                        "channel": str(ch_num),
                        "pair": str(pair_index + 1),
                    },
                )
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
        self._attr_unique_id = f"{device.node_id or device.ip}_{model}_ch{ch_num}"
        self._attr_name = f"{model}_{suffix}_area{area}_ch{ch_num}_curtain"
        self._is_closed = not initial_state.get("on", False)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.node_id or self._device.ip)},
            name=f"Raylogic {self._device.model_name} ({self._device.ip})",
            manufacturer="Raylogic",
            model=f"{self._device.model_name} - {self._device.model_desc}",
            sw_version=self._device.fw_version,
        )

    @property
    def available(self):
        return self._device.is_connected

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
        self.async_on_remove(
            self._hass.bus.async_listen(f"{DOMAIN}_state_update", self._on_update)
        )
        self.async_on_remove(
            self._hass.bus.async_listen(f"{DOMAIN}_available", self._on_available)
        )

    @callback
    def _on_update(self, event):
        d = event.data
        if d.get("entry_id") == self._entry.entry_id and d.get("channel") == self._ch_num:
            s = d.get("state", {})
            if "on" in s:
                self._is_closed = not bool(s["on"])
            self.async_write_ha_state()

    @callback
    def _on_available(self, event):
        if event.data.get("entry_id") == self._entry.entry_id:
            self.async_write_ha_state()
