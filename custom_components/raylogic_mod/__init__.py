"""Raylogic MOD2U / MOD4U integration - RE8-style config-entry architecture.

Ek hi integration (domain: raylogic_mod) dono devices support karta hai -
config_flow mein "Device Model" dropdown se MOD2U (2 channel, 1 pair) ya
MOD4U (4 channel, 2 pairs) choose karo. Relay/Dimmer/Fan har channel
independently set ho sakta hai, lekin Curtain aur CTC dono PAIRED modes
hain - jis pair ka koi ek channel Curtain ya CTC banaya jaaye, wo poora
pair (dono physical channels) ek hi logical entity ke andar consume ho
jaata hai."""
from __future__ import annotations
import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    DEFAULT_PORT, DOMAIN, PLATFORMS,
    LEGACY_DEFAULT_AREA,
    CH_TYPE_CTC, CH_TYPE_CURTAIN, CH_TYPE_RELAY,
    CTC_MODE_SINGLE,
    DEVICE_MODELS, DEFAULT_MODEL,
)
from .protocol import RaylogicModDevice

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE_MODEL = "device_model"
CONF_LEGACY_AREA = "legacy_area"
CONF_CHANNEL_START = "channel_start"
CONF_CH1_TYPE = "channel_1_type"
CONF_CH2_TYPE = "channel_2_type"
CONF_CH3_TYPE = "channel_3_type"
CONF_CH4_TYPE = "channel_4_type"
CONF_CH1_CTC_MODE = "channel_1_ctc_mode"
CONF_CH2_CTC_MODE = "channel_2_ctc_mode"
CONF_CH3_CTC_MODE = "channel_3_ctc_mode"
CONF_CH4_CTC_MODE = "channel_4_ctc_mode"

# Pair layout: (type_conf_key_lo, type_conf_key_hi, ctc_mode_key_lo, ctc_mode_key_hi)
_PAIR_CONF_KEYS = (
    (CONF_CH1_TYPE, CONF_CH2_TYPE, CONF_CH1_CTC_MODE, CONF_CH2_CTC_MODE),
    (CONF_CH3_TYPE, CONF_CH4_TYPE, CONF_CH3_CTC_MODE, CONF_CH4_CTC_MODE),
)


def _resolve_channel_types(
    conf: dict, channel_start: int, channel_count: int, fixed_type: str | None = None,
) -> tuple[dict[int, str], dict[int, str]]:
    """Config (channel_1_type..channel_N_type) ko physical channel_types /
    channel_ctc_modes dicts mein resolve karo - sirf is model ke
    `channel_count` (MOD2U=2, MOD4U=4) tak ke pairs process hote hain,
    baaki (agar MOD2U par galti se channel_3/4_type bhi save ho jaayein,
    jaise model badalne par purana data reh gaya ho) ignore ho jaate hain.

    Har pair (lo, hi) ke liye:
      - agar lo ka type 'ctc' ya 'curtain' hai -> sirf lo entry banti hai
        (paired entity), hi ko IGNORE kiya jaata hai (uski apni entity
        nahi banti - warna dono channels ke liye do alag entity bante jo
        ek hi physical hardware par conflict karte).
      - warna agar hi ka type 'ctc' ya 'curtain' hai -> sirf hi entry
        (same reason, symmetric case).
      - warna (dono normal: relay/dimmer/fan) -> dono independently apni
        apni entity paate hain.

    fixed_type models (jaise MOD2F - single fixed Fan channel): koi
    "Select Type" choice hi nahi hoti, aur koi PAIRING bhi nahi hoti (1
    channel ke liye pair-of-2 logic chalane se ek phantom "channel_start+1"
    ban jaata jo physically exist hi nahi karta) - is liye ye ek seedha,
    alag branch hai: channel_start se channel_count tak har physical
    channel ko wahi fixed type de do, koi CTC mode nahi (Fan ko zaroorat
    nahi).
    """
    if fixed_type:
        channel_types = {
            channel_start + i: fixed_type for i in range(channel_count)
        }
        return channel_types, {}

    channel_types: dict[int, str] = {}
    channel_ctc_modes: dict[int, str] = {}
    pair_count = max(1, channel_count // 2)

    for pair_index, (lo_key, hi_key, lo_ctc_key, hi_ctc_key) in enumerate(_PAIR_CONF_KEYS):
        if pair_index >= pair_count:
            break
        phys_lo = channel_start + pair_index * 2
        phys_hi = phys_lo + 1
        type_lo = conf.get(lo_key, CH_TYPE_RELAY)
        type_hi = conf.get(hi_key, CH_TYPE_RELAY)

        if type_lo in (CH_TYPE_CTC, CH_TYPE_CURTAIN):
            channel_types[phys_lo] = type_lo
            if type_lo == CH_TYPE_CTC:
                channel_ctc_modes[phys_lo] = conf.get(lo_ctc_key, CTC_MODE_SINGLE)
        elif type_hi in (CH_TYPE_CTC, CH_TYPE_CURTAIN):
            channel_types[phys_hi] = type_hi
            if type_hi == CH_TYPE_CTC:
                channel_ctc_modes[phys_hi] = conf.get(hi_ctc_key, CTC_MODE_SINGLE)
        else:
            channel_types[phys_lo] = type_lo
            channel_types[phys_hi] = type_hi

    return channel_types, channel_ctc_modes


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # options (naye "Configure" button se) data (initial add se) ke upar
    # priority lete hain - taaki channel type badalne ke baad delete+re-add
    # kiye bina bhi naya config turant effect kare.
    conf = {**entry.data, **entry.options}

    host = conf[CONF_HOST]
    port = conf.get(CONF_PORT, DEFAULT_PORT)
    model = conf.get(CONF_DEVICE_MODEL, DEFAULT_MODEL)
    model_info = DEVICE_MODELS.get(model, DEVICE_MODELS[DEFAULT_MODEL])
    channel_count = model_info["channel_count"]
    # 0 = relay-only auto/learn mode - Area manually diya gaya nahi hai
    legacy_area = conf.get(CONF_LEGACY_AREA, LEGACY_DEFAULT_AREA)
    # Kai installations mein channel numbering 1 se shuru nahi hoti (Area ke
    # andar globally assign hoti hai) - is device ka pehla channel number.
    channel_start = conf.get(CONF_CHANNEL_START, 1)
    # Raylogic GO app mein jo type set kiya gaya hai (relay/dimmer/fan/
    # curtain/ctc) - keys ab actual physical channel numbers hain
    # (channel_start se shuru), model ke channel_count tak resolve hoti hai.
    channel_types, channel_ctc_modes = _resolve_channel_types(
        conf, channel_start, channel_count, fixed_type=model_info.get("fixed_type"),
    )

    device = RaylogicModDevice(
        ip=host, port=port,
        model=model,
        legacy_area=legacy_area,
        legacy_channel_count=channel_count,
        channel_start=channel_start,
        channel_types=channel_types,
        channel_ctc_modes=channel_ctc_modes,
        state_callback=lambda ip, ch, state: _handle_state_update(
            hass, entry.entry_id, ip, ch, state
        ),
    )

    connected = await device.connect()
    if not connected:
        # BUG FIX: pehle yahan `return False` tha - HA isse entry ko seedha
        # "setup failed" maan leta tha, proper exponential-backoff retry
        # nahi hoti thi. ConfigEntryNotReady raise karne se HA khud isse
        # thodi thodi der mein retry karta rehta hai (jaisa device boot ke
        # time thoda der se network par aaye) - startup "stuck" jaisa feel
        # nahi hota, aur baad mein device aane par integration khud theek
        # ho jaati hai, manual reload ki zaroorat nahi padti. (Ye alag hai
        # us doosre "power-cycle ke baad hamesha ke liye atak jaana" bug
        # se, jo AB fix ho chuka hai - dekho protocol.py ka _reconnect().)
        raise ConfigEntryNotReady(
            f"Could not connect to Raylogic {model_info['name']} at {host}:{port}"
        )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = device

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options flow se save hote hi poora entry reload karo."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    device: RaylogicModDevice = hass.data[DOMAIN].get(entry.entry_id)
    if device:
        await device.disconnect()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


def _handle_state_update(hass, entry_id, ip, ch, state):
    if "available" in state:
        hass.bus.async_fire(
            f"{DOMAIN}_available",
            {"entry_id": entry_id, "available": state["available"]},
        )
        return
    hass.bus.async_fire(
        f"{DOMAIN}_state_update",
        {"entry_id": entry_id, "ip": ip, "channel": ch, "state": state},
    )
    hass.bus.async_fire(
        f"{DOMAIN}_available",
        {"entry_id": entry_id, "available": True},
    )
