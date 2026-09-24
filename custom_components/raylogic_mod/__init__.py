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
import re

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

# BUG FIX (THE SCALE BUG - "connection lost" cascades + HA UI hang jab
# bahut saare (50-100+) devices add kiye jaayein): pehle state/available
# updates hass.bus.async_fire() se GLOBAL event ke taur par bheje jaate
# the ("raylogic_mod_state_update" / "raylogic_mod_available") - koi bhi
# EK device apna keepalive/resync/command bhi fire kare, to HA ke andar
# maujood HAR RAYLOGIC ENTITY (sab devices ke sab channels - switch/
# light/fan/cover) ka listener chalta tha, sirf ye check karne ke liye
# ki "ye event mera hai ya kisi aur device ka" (entry_id/channel match
# karke). 100 devices x ~2-4 channels = ~300-400 entities x 2 listeners
# = ~700-800 listener calls PER SINGLE EVENT - aur events (keepalive,
# resync-har-45s, commands) khud bhi devices ki tadaad ke saath badhte
# hain. Matlab total load ~O(devices^2) tarah badhta hai - isi wajah se
# jitne zyada devices utna zyada "sab kuch slow/atka hua" (webpage load
# na hona) mehsoos hota hai, chahe har individual device ka apna TCP/
# reconnect logic bilkul theek ho.
#
# Fix: ab per-ENTRY (per physical device) scoped dispatcher signal use
# hota hai ("raylogic_mod_<entry_id>_state_update" / "..._available") -
# is signal ko sirf USI device ke entities hi listen karte hain, doosre
# devices ke entities ko is event ka pata hi nahi chalta. Isse per-event
# cost O(1) (us device ke apne channels jitna) ho jaata hai, poore
# integration ke total device-count se independent - 100 devices bhi
# 1 device jaisa hi smooth chalte hain.

from .const import (
    DEFAULT_PORT, DOMAIN, PLATFORMS,
    LEGACY_DEFAULT_AREA,
    CH_TYPE_CTC, CH_TYPE_CURTAIN, CH_TYPE_RELAY, CH_TYPE_DIMMER, CH_TYPE_FAN,
    CTC_MODE_SINGLE,
    DEVICE_MODELS, DEFAULT_MODEL,
    CONF_SCENE_COUNTS, SCENE_AREAS, SCENE_MAX, SCENE_DATA_KEY,
    SIGNAL_AREA_SCENE, merge_scene_maps,
)
from .protocol import RaylogicModDevice
from .discovery import other_integration_hosts

_LOGGER = logging.getLogger(__name__)

# Sirf UI (config entries) se setup hota hai - YAML config nahi.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_RECALL_SCENE = "recall_scene"
RECALL_SCENE_SCHEMA = vol.Schema({
    vol.Required("area"): vol.All(vol.Coerce(int), vol.Range(min=1, max=SCENE_AREAS)),
    vol.Required("scene"): vol.All(vol.Coerce(int), vol.Range(min=1, max=SCENE_MAX)),
})

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


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """raylogic_mod.recall_scene service ek hi baar register karo."""
    hass.data.setdefault(DOMAIN, {})

    async def _recall_scene(call: ServiceCall) -> None:
        await async_recall_area_scene(hass, call.data["area"], call.data["scene"])

    hass.services.async_register(
        DOMAIN, SERVICE_RECALL_SCENE, _recall_scene, schema=RECALL_SCENE_SCHEMA,
    )
    return True


def loaded_devices(hass: HomeAssistant) -> list[RaylogicModDevice]:
    return [
        d for d in hass.data.get(DOMAIN, {}).values()
        if isinstance(d, RaylogicModDevice)
    ]


def current_scene_map(hass: HomeAssistant) -> dict:
    """Saari raylogic_mod entries ke Configure -> scene_counts ka union."""
    return merge_scene_maps(
        e.options.get(CONF_SCENE_COUNTS, "")
        for e in hass.config_entries.async_entries(DOMAIN)
    )


def claim_scene_host(hass: HomeAssistant, entry: ConfigEntry) -> dict | None:
    """Area scenes poori installation ke liye EK hi set - jo entry pehle
    aaye wahi "host". Host entry ke liye merged {area: [scenes]} lautata
    hai, baaki sab ke liye None (duplicate entities nahi bante)."""
    scenes = hass.data.setdefault(SCENE_DATA_KEY, {})
    if scenes.setdefault("host", entry.entry_id) != entry.entry_id:
        return None
    scenes["map"] = current_scene_map(hass)
    return scenes["map"]


async def async_recall_area_scene(hass: HomeAssistant, area: int, scene: int) -> int:
    """Area scene har loaded raylogic_mod module par ek SAATH bhejo.

    Modules alag-alag bus par ho sakte hain, isliye har ek ko apni copy
    chahiye. Concurrent (asyncio.gather) bhejna zaroori hai - reference
    integration me sequential bhejne se ~150-350ms ka gap aata tha jisme
    modules active scene par disagree karte aur app me scene flicker hota.
    Recall idempotent hai, isliye same bus par duplicate se state nahi
    bigadti.

    Channel commands jaisa hi: jo module abhi disconnected hai uske liye
    recall _send_addressed() ki queue me jaata hai aur reconnect par replay
    hota hai (PENDING_COMMAND_MAX_AGE se purana ho to drop) - isliye scene
    entities kabhi "Unavailable" nahi dikhate (baaki entities ki tarah)."""
    devs = loaded_devices(hass)
    if not devs:
        raise HomeAssistantError(
            f"Raylogic MOD: koi module loaded nahi hai - area {area} "
            f"scene {scene} recall nahi ho saka."
        )
    offline = [d.ip for d in devs if not d.is_connected]
    if offline:
        _LOGGER.info(
            "Raylogic MOD: scene area=%d scene=%d - %s abhi disconnected, "
            "recall queue me hai (reconnect par jaayega).",
            area, scene, ", ".join(offline),
        )
    results = await asyncio.gather(
        *(d.recall_scene(area, scene) for d in devs), return_exceptions=True,
    )
    for dev, res in zip(devs, results):
        if isinstance(res, Exception):
            _LOGGER.warning(
                "Raylogic %s: scene recall area=%d scene=%d failed: %s",
                dev.ip, area, scene, res,
            )
    # HA ka apna recall bhi select entity me dikhe (099-node frame device
    # echo nahi karta, isliye ye feedback yahin se dena padta hai).
    async_dispatcher_send(hass, SIGNAL_AREA_SCENE, area, scene)
    return len(devs)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # options (naye "Configure" button se) data (initial add se) ke upar
    # priority lete hain - taaki channel type badalne ke baad delete+re-add
    # kiye bina bhi naya config turant effect kare.
    conf = {**entry.data, **entry.options}

    host = conf[CONF_HOST]
    port = conf.get(CONF_PORT, DEFAULT_PORT)
    # F3 (v1.6.1): sirf warning, block nahi - jo setup aaj chal raha hai wo
    # na toote. (Naya add F2 se pehle hi ruk jaata hai; ye un entries ke
    # liye hai jo F2 se pehle bani, ya Configure se host badla gaya.)
    # Ek entry+host ke liye sirf EK baar - device offline ho to HA har
    # ConfigEntryNotReady retry par setup dobara chalata hai (log spam).
    warned = hass.data.setdefault(f"{DOMAIN}_overlap_warned", set())
    overlap_key = (entry.entry_id, str(host).strip())
    if overlap_key not in warned and overlap_key[1] in other_integration_hosts(hass):
        warned.add(overlap_key)
        _LOGGER.warning(
            "Raylogic MOD %s: ye IP 'raylogic' (main) integration me bhi "
            "configured hai - ek hi module par do integrations se do TCP "
            "connections khulenge. Ek integration se is device ko hata do.",
            host,
        )
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
        # P6: learned channels HA ke .storage me - HACS update se safe
        state_dir=hass.config.path(".storage", DOMAIN),
    )

    # D1 (v1.6.3): stable id = entry.unique_id (host_port, add ke waqt fix;
    # Configure se host badle to bhi same) - purane node/ip-based ids yahan
    # migrate hote hain, platforms setup hone se PEHLE.
    device.stable_id = entry.unique_id or entry.entry_id
    _async_migrate_ids(hass, entry, device.stable_id)

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
    _async_purge_stale(hass, entry, device, legacy_area)
    return True


# ---------------------------------------------------------------------- #
# D1 / D2 (v1.6.3): entity registry hygiene
# ---------------------------------------------------------------------- #
_OLD_UID_RE = re.compile(
    r"^(?P<base>.+)_(?P<model>mod2u|mod4u|mod2f)_ch(?P<rest>\d+(?:_ctc)?)$"
)
_SCENE_DEVICE_ID = "area_scenes"

# channel type -> (entity domain, unique_id suffix) - platforms ka exact mirror
_TYPE_TO_ENTITY = {
    CH_TYPE_RELAY: ("switch", ""),
    CH_TYPE_DIMMER: ("light", ""),
    CH_TYPE_CTC: ("light", "_ctc"),
    CH_TYPE_FAN: ("fan", ""),
    CH_TYPE_CURTAIN: ("cover", ""),
}


def _keeper_rank(ent, group) -> tuple:
    """Duplicate group me original kaun hai: HA do entities ka same
    entity_id hone par doosre ko "<id>_2", "_3"... deta hai - jo entity kisi
    doosre candidate ke entity_id + "_N" hai wo pakka duplicate hai. (Sirf
    created_at par bharosa nahi: delete+re-add par HA purana tombstone
    uske purane created_at ke saath restore kar deta hai.)"""
    is_dup = any(
        other is not ent
        and re.fullmatch(re.escape(other.entity_id) + r"_\d+", ent.entity_id)
        for other in group
    )
    created = getattr(ent, "created_at", None)
    return (is_dup, created is None, created or 0, ent.entity_id)


def _async_migrate_ids(hass: HomeAssistant, entry: ConfigEntry, stable: str) -> None:
    """Purane "<node>_<model>_chN" / "<ip>_<model>_chN" unique_ids ko
    "<stable>_<model>_chN" par le jao. Ek channel ke do versions (node aur
    ip, jinme se ek "_2" ban gaya tha) mile to ORIGINAL (sabse pehle bana)
    entity_id rakho - dashboards/automations wahi use karte hain - aur
    duplicate hata do. Device registry ke purane identifiers bhi ek hi
    stable device me merge hote hain."""
    ent_reg = er.async_get(hass)
    groups: dict[tuple, list] = {}
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if ent.platform != DOMAIN:
            continue
        m = _OLD_UID_RE.match(ent.unique_id)
        if not m:
            continue
        new_uid = f"{stable}_{m['model']}_ch{m['rest']}"
        groups.setdefault((ent.domain, new_uid), []).append(ent)

    kept_devices: dict[str, int] = {}
    for (_domain, new_uid), ents in groups.items():
        keeper = next((e for e in ents if e.unique_id == new_uid), None)
        if keeper is None:
            keeper = min(ents, key=lambda e: _keeper_rank(e, ents))
            ent_reg.async_update_entity(keeper.entity_id, new_unique_id=new_uid)
            _LOGGER.info(
                "Raylogic MOD: %s unique_id %s -> %s",
                keeper.entity_id, keeper.unique_id, new_uid,
            )
        for dup in ents:
            if dup.entity_id != keeper.entity_id:
                _LOGGER.warning(
                    "Raylogic MOD: duplicate entity %s hata rahe hain (%s ka "
                    "hi doosra copy tha)", dup.entity_id, keeper.entity_id,
                )
                ent_reg.async_remove(dup.entity_id)
        if keeper.device_id:
            kept_devices[keeper.device_id] = kept_devices.get(keeper.device_id, 0) + 1

    # Device registry: (DOMAIN, node/ip) wale purane devices -> ek stable device
    dev_reg = dr.async_get(hass)
    devices = [
        d for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
        if any(i[0] == DOMAIN and i[1] != _SCENE_DEVICE_ID for i in d.identifiers)
    ]
    if not devices:
        return
    keeper_dev = next(
        (d for d in devices if (DOMAIN, stable) in d.identifiers), None,
    )
    if keeper_dev is None:
        # Jis device par rakhi gayi (original) entities hain wahi original
        # device hai - device-based automations isi ke device_id se jude
        # hote hain. Tie ho to sabse pehle bana.
        keeper_dev = min(devices, key=lambda d: (
            -kept_devices.get(d.id, 0),
            getattr(d, "created_at", None) is None,
            getattr(d, "created_at", None) or 0,
        ))
        dev_reg.async_update_device(keeper_dev.id, new_identifiers={(DOMAIN, stable)})
    for dev in devices:
        if dev.id == keeper_dev.id:
            continue
        # Pehle entities ko keeper device par shift karo, tabhi purana device
        # hatao (device hatne par uski entities bhi hat jaati hain).
        for ent in er.async_entries_for_device(ent_reg, dev.id, include_disabled_entities=True):
            ent_reg.async_update_entity(ent.entity_id, device_id=keeper_dev.id)
        dev_reg.async_remove_device(dev.id)


def _async_purge_stale(
    hass: HomeAssistant, entry: ConfigEntry, device: RaylogicModDevice, legacy_area: int,
) -> None:
    """Is entry ki wo registry entities hatao jo ab bante hi nahi (Configure
    se channel type / First Channel badla, ya scene hataya) - warna wo
    hamesha "unavailable" orphan reh jaati hain. Channels config se aate
    hain (device se discover nahi hote) isliye expected set deterministic
    hai. LEARN mode (Area 0) me channels dheere-dheere seekhe jaate hain -
    wahan kuch nahi hataate."""
    if not legacy_area or legacy_area <= 0:
        return
    expected: set[tuple[str, str]] = set()
    for ch_num, st in device.channel_states.items():
        spec = _TYPE_TO_ENTITY.get(st.get("type"))
        if spec:
            expected.add((spec[0], f"{device.stable_id}_{device.model}_ch{ch_num}{spec[1]}"))
    scenes = hass.data.get(SCENE_DATA_KEY, {})
    if scenes.get("host") == entry.entry_id:
        for area, nums in (scenes.get("map") or {}).items():
            expected.add(("select", f"raylogic_mod_area{area}_scene_select"))
            for num in nums:
                expected.add(("scene", f"raylogic_mod_area{area}_scene{num}"))
    ent_reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if ent.platform == DOMAIN and (ent.domain, ent.unique_id) not in expected:
            _LOGGER.info(
                "Raylogic MOD: stale entity %s (%s) hata rahe hain - ab config "
                "me nahi hai", ent.entity_id, ent.unique_id,
            )
            ent_reg.async_remove(ent.entity_id)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options flow se save hote hi poora entry reload karo."""
    await hass.config_entries.async_reload(entry.entry_id)
    # Scene config kisi bhi device par set ho sakti hai, lekin scene/select
    # entities sirf "scene host" entry par bante hain - agar merged map
    # badla aur host koi DOOSRA entry hai, to use bhi reload karo taaki
    # naye scenes ban jaayein / hate hue hat jaayein.
    scenes = hass.data.get(SCENE_DATA_KEY, {})
    host_id = scenes.get("host")
    if (
        host_id and host_id != entry.entry_id
        and scenes.get("map") != current_scene_map(hass)
    ):
        await hass.config_entries.async_reload(host_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    device: RaylogicModDevice = hass.data[DOMAIN].get(entry.entry_id)
    if device:
        await device.disconnect()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        scenes = hass.data.get(SCENE_DATA_KEY, {})
        if scenes.get("host") == entry.entry_id:
            scenes.pop("host", None)
            scenes.pop("map", None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Scene host wala device DELETE hua to area scenes kisi doosre loaded
    device par wapas bana do (warna restart tak gayab rehte). Normal reload
    me ye nahi chalta - wahan wahi entry dobara host ban jaati hai."""
    if hass.data.get(SCENE_DATA_KEY, {}).get("host"):
        return
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id != entry.entry_id and other.state is ConfigEntryState.LOADED:
            hass.async_create_task(hass.config_entries.async_reload(other.entry_id))
            return


def _handle_state_update(hass, entry_id, ip, ch, state):
    if "available" in state:
        async_dispatcher_send(
            hass, f"{DOMAIN}_{entry_id}_available", state["available"]
        )
        return
    if isinstance(ch, str) and ch.startswith("scene_"):
        # Area-scene echo (protocol._handle_ar) - sirf scene selectors ke
        # liye; channel entities tak nahi jaata.
        async_dispatcher_send(
            hass, SIGNAL_AREA_SCENE, int(ch[len("scene_"):]), state["scene"],
        )
        return
    async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_state_update", ch, state)
    async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_available", True)
