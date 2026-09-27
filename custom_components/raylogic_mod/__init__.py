"""Raylogic MOD2U / MOD4U integration - RE8-style config-entry architecture.

A single integration (domain: raylogic_mod) supports both devices - choose
MOD2U (2 channels, 1 pair) or MOD4U (4 channels, 2 pairs) from the "Device
Model" dropdown in the config flow. Relay/Dimmer/Fan can be set per channel
independently, but Curtain and CCT are both PAIRED modes: when one channel
of a pair is set to Curtain or CCT, the whole pair (both physical channels)
is consumed by a single logical entity."""
from __future__ import annotations
import asyncio
import logging
import re

import voluptuous as vol

from datetime import timedelta

from homeassistant.config_entries import (
    ConfigEntry, ConfigEntryState, SOURCE_IGNORE, SOURCE_INTEGRATION_DISCOVERY,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval

# BUG FIX (THE SCALE BUG - "connection lost" cascades and a hanging HA UI
# when many (50-100+) devices are added): state/available updates used to
# be sent as GLOBAL events via hass.bus.async_fire()
# ("raylogic_mod_state_update" / "raylogic_mod_available"). Whenever ANY
# single device fired a keepalive/resync/command event, the listener of
# EVERY Raylogic entity in HA (all channels of all devices - switch/light/
# fan/cover) ran, only to check "is this event mine or another device's?"
# (by matching entry_id/channel). 100 devices x ~2-4 channels = ~300-400
# entities x 2 listeners = ~700-800 listener calls PER SINGLE EVENT - and
# the number of events (keepalive, resync every 45 s, commands) grows with
# the device count as well. The total load therefore grew roughly as
# O(devices^2), which is why "everything is slow/stuck" (web pages not
# loading) got worse with more devices, even though each device's own
# TCP/reconnect logic was fine.
#
# Fix: a dispatcher signal scoped per ENTRY (per physical device) is now
# used ("raylogic_mod_<entry_id>_state_update" / "..._available"). Only the
# entities of THAT device listen to it; entities of other devices never see
# the event. The per-event cost becomes O(1) (the device's own channels),
# independent of the integration's total device count - 100 devices run as
# smoothly as 1.

from .const import (
    DEFAULT_PORT, DOMAIN, PLATFORMS,
    LEGACY_DEFAULT_AREA,
    CH_TYPE_CCT, CH_TYPE_CURTAIN, CH_TYPE_RELAY, CH_TYPE_DIMMER, CH_TYPE_FAN,
    CCT_MODE_SINGLE, LEGACY_CCT_UID_SUFFIX, normalize_legacy_cct,
    DEVICE_MODELS, DEFAULT_MODEL,
    CONF_SCENE_COUNTS, SCENE_AREAS, SCENE_MAX, SCENE_DATA_KEY,
    AUTO_SCAN_FIRST_DELAY, AUTO_SCAN_INTERVAL, AUTO_SCAN_MAX_SUBNETS, CONF_AUTO_DISCOVERY,
    SIGNAL_AREA_SCENE, merge_scene_maps,
)
from .protocol import RaylogicModDevice
from . import discovery
from .discovery import other_integration_hosts

_LOGGER = logging.getLogger(__name__)

# Set up from the UI (config entries) only - no YAML configuration.
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
CONF_CH1_CCT_MODE = "channel_1_cct_mode"
CONF_CH2_CCT_MODE = "channel_2_cct_mode"
CONF_CH3_CCT_MODE = "channel_3_cct_mode"
CONF_CH4_CCT_MODE = "channel_4_cct_mode"

# Pair layout: (type_conf_key_lo, type_conf_key_hi, cct_mode_key_lo, cct_mode_key_hi)
_PAIR_CONF_KEYS = (
    (CONF_CH1_TYPE, CONF_CH2_TYPE, CONF_CH1_CCT_MODE, CONF_CH2_CCT_MODE),
    (CONF_CH3_TYPE, CONF_CH4_TYPE, CONF_CH3_CCT_MODE, CONF_CH4_CCT_MODE),
)


def _resolve_channel_types(
    conf: dict, channel_start: int, channel_count: int, fixed_type: str | None = None,
) -> tuple[dict[int, str], dict[int, str]]:
    """Resolve the config (channel_1_type..channel_N_type) into the physical
    channel_types / channel_cct_modes dicts. Only pairs up to this model's
    `channel_count` (MOD2U=2, MOD4U=4) are processed; anything beyond that
    (e.g. channel_3/4_type accidentally saved on a MOD2U, such as data left
    over after changing the model) is ignored.

    For each pair (lo, hi):
      - if lo's type is 'cct' or 'curtain' -> only the lo entry is created
        (paired entity) and hi is IGNORED (it gets no entity of its own -
        otherwise two separate entities would conflict on the same physical
        hardware).
      - else if hi's type is 'cct' or 'curtain' -> only the hi entry
        (same reason, symmetric case).
      - otherwise (both normal: relay/dimmer/fan) -> each channel gets its
        own entity independently.

    fixed_type models (e.g. MOD2F - a single fixed fan channel): there is no
    "Select Type" choice and no PAIRING (running the pair-of-2 logic for a
    single channel would create a phantom "channel_start+1" that does not
    physically exist). This is therefore a simple, separate branch: every
    physical channel from channel_start up to channel_count gets the fixed
    type, with no CCT mode (a fan does not need one).
    """
    if fixed_type:
        channel_types = {
            channel_start + i: fixed_type for i in range(channel_count)
        }
        return channel_types, {}

    channel_types: dict[int, str] = {}
    channel_cct_modes: dict[int, str] = {}
    pair_count = max(1, channel_count // 2)

    for pair_index, (lo_key, hi_key, lo_cct_key, hi_cct_key) in enumerate(_PAIR_CONF_KEYS):
        if pair_index >= pair_count:
            break
        phys_lo = channel_start + pair_index * 2
        phys_hi = phys_lo + 1
        type_lo = conf.get(lo_key, CH_TYPE_RELAY)
        type_hi = conf.get(hi_key, CH_TYPE_RELAY)

        if type_lo in (CH_TYPE_CCT, CH_TYPE_CURTAIN):
            channel_types[phys_lo] = type_lo
            if type_lo == CH_TYPE_CCT:
                channel_cct_modes[phys_lo] = conf.get(lo_cct_key, CCT_MODE_SINGLE)
        elif type_hi in (CH_TYPE_CCT, CH_TYPE_CURTAIN):
            channel_types[phys_hi] = type_hi
            if type_hi == CH_TYPE_CCT:
                channel_cct_modes[phys_hi] = conf.get(hi_cct_key, CCT_MODE_SINGLE)
        else:
            channel_types[phys_lo] = type_lo
            channel_types[phys_hi] = type_hi

    return channel_types, channel_cct_modes


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the raylogic_mod.recall_scene service once."""
    hass.data.setdefault(DOMAIN, {})

    async def _recall_scene(call: ServiceCall) -> None:
        await async_recall_area_scene(hass, call.data["area"], call.data["scene"])

    hass.services.async_register(
        DOMAIN, SERVICE_RECALL_SCENE, _recall_scene, schema=RECALL_SCENE_SCHEMA,
    )
    _async_start_auto_discovery(hass)
    return True


# ---------------------------------------------------------------------- #
# Automatic discovery (v1.6.6)
# Pattern adopted from the reference "raylogic" integration's AUTO_SCAN
# (nothing was changed there): the first scan runs after
# AUTO_SCAN_FIRST_DELAY, then every AUTO_SCAN_INTERVAL. Every new module
# becomes a "Discovered" card (integration_discovery flow) - the user only
# presses "Add".
# ---------------------------------------------------------------------- #
_AUTO_SCAN_KEY = f"{DOMAIN}_auto_scan"


@callback
def _async_start_auto_discovery(hass: HomeAssistant) -> None:
    if hass.data.get(_AUTO_SCAN_KEY):
        return
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    async def _run(_now=None) -> None:
        await async_auto_discovery_scan(hass)

    unsubs = [
        async_track_time_interval(
            hass, _run, timedelta(seconds=AUTO_SCAN_INTERVAL),
            name="raylogic_mod auto discovery",
        ),
        async_call_later(hass, AUTO_SCAN_FIRST_DELAY, _run),
    ]

    @callback
    def _stop(_event=None) -> None:
        for unsub in unsubs:
            unsub()
        unsubs.clear()
        hass.data.pop(_AUTO_SCAN_KEY, None)

    hass.data[_AUTO_SCAN_KEY] = {"stop": _stop, "running": False, "last": None}
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)


def auto_discovery_enabled(hass: HomeAssistant) -> bool:
    """One integration-wide setting: if "Auto-discover" is turned off in the
    Configure dialog of any device, the background scan is disabled."""
    entries = [
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.source != SOURCE_IGNORE
    ]
    return bool(entries) and all(
        e.options.get(CONF_AUTO_DISCOVERY, True) is not False for e in entries
    )


async def async_auto_discovery_scan(hass: HomeAssistant) -> int:
    """One background scan. Returns how many NEW "Discovered" cards were
    created. Configured (raylogic_mod + raylogic) and ignored hosts are never
    touched; Home Assistant itself aborts duplicate cards (same unique_id)."""
    state = hass.data.get(_AUTO_SCAN_KEY)
    if state is None or state["running"] or not auto_discovery_enabled(hass):
        return 0
    state["running"] = True
    try:
        subnets = await discovery.async_auto_subnets(hass, AUTO_SCAN_MAX_SUBNETS)
        skip = discovery.configured_hosts(hass) | discovery.ignored_hosts(hass)
        hits = await discovery.async_scan(subnets, skip=skip)
        started = 0
        for hit in hits:
            try:
                res = await hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": SOURCE_INTEGRATION_DISCOVERY},
                    data=hit,
                )
                if res.get("type") != "abort":
                    started += 1
            except Exception:  # one bad hit must not stop the others
                _LOGGER.debug("discovery flow for %s failed", hit.get("host"), exc_info=True)
        state["last"] = {"subnets": subnets, "hits": len(hits), "new": started}
        _LOGGER.debug(
            "Raylogic MOD auto-discovery: subnets %s, %d module(s) answered, %d new card(s)",
            subnets, len(hits), started,
        )
        if started:
            _LOGGER.info(
                "Raylogic MOD auto-discovery: found %d new module(s) - press Add "
                "on the 'Discovered' card in Settings > Devices & services: %s",
                started, ", ".join(discovery.describe(h) for h in hits),
            )
        return started
    except Exception:
        _LOGGER.debug("Raylogic MOD auto-discovery scan failed", exc_info=True)
        return 0
    finally:
        state["running"] = False


def loaded_devices(hass: HomeAssistant) -> list[RaylogicModDevice]:
    return [
        d for d in hass.data.get(DOMAIN, {}).values()
        if isinstance(d, RaylogicModDevice)
    ]


def current_scene_map(hass: HomeAssistant) -> dict:
    """Union of the Configure -> scene_counts settings of all raylogic_mod entries."""
    return merge_scene_maps(
        e.options.get(CONF_SCENE_COUNTS, "")
        for e in hass.config_entries.async_entries(DOMAIN)
    )


def claim_scene_host(hass: HomeAssistant, entry: ConfigEntry) -> dict | None:
    """Area scenes are ONE set for the whole installation - the entry that
    comes first becomes the "host". Returns the merged {area: [scenes]} for
    the host entry and None for every other entry (so no duplicate entities
    are created)."""
    scenes = hass.data.setdefault(SCENE_DATA_KEY, {})
    if scenes.setdefault("host", entry.entry_id) != entry.entry_id:
        return None
    scenes["map"] = current_scene_map(hass)
    return scenes["map"]


async def async_recall_area_scene(hass: HomeAssistant, area: int, scene: int) -> int:
    """Send an area scene recall to every loaded raylogic_mod module AT ONCE.

    Modules may be on separate buses, so each needs its own copy. Sending
    concurrently (asyncio.gather) matters: in the reference integration,
    sending sequentially left a ~150-350 ms gap during which modules
    disagreed on the active scene and the scene flickered in the app. A
    recall is idempotent, so a duplicate on the same bus does not corrupt
    the state.

    Same as for channel commands: for a module that is currently
    disconnected, the recall goes into the _send_addressed() queue and is
    replayed on reconnect (dropped if older than PENDING_COMMAND_MAX_AGE) -
    which is why scene entities never show "Unavailable" (like the other
    entities)."""
    devs = loaded_devices(hass)
    if not devs:
        raise HomeAssistantError(
            f"Raylogic MOD: no module is loaded - area {area} "
            f"scene {scene} could not be recalled."
        )
    offline = [d.ip for d in devs if not d.is_connected]
    if offline:
        _LOGGER.info(
            "Raylogic MOD: scene area=%d scene=%d - %s currently disconnected, "
            "recall queued (it will be sent on reconnect).",
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
    # Show HA's own recall in the select entity too (the device does not
    # echo a node-099 frame, so this feedback has to be given here).
    async_dispatcher_send(hass, SIGNAL_AREA_SCENE, area, scene)
    return len(devs)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """v1.7.1: config entry 1.1 -> 1.2 - the old "ctc" spellings in data and
    options become "cct". Runs once, before async_setup_entry."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        hass.config_entries.async_update_entry(
            entry,
            data=normalize_legacy_cct(dict(entry.data)),
            options=normalize_legacy_cct(dict(entry.options)),
            minor_version=2,
        )
        _LOGGER.info(
            "Raylogic MOD: config entry '%s' migrated to 1.2 (CTC -> CCT)",
            entry.title,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # options (from the "Configure" button) take priority over data (from
    # the initial add), so a changed channel type takes effect immediately
    # without deleting and re-adding the device.
    conf = normalize_legacy_cct({**entry.data, **entry.options})

    host = conf[CONF_HOST]
    port = conf.get(CONF_PORT, DEFAULT_PORT)
    # F3 (v1.6.1): warning only, no block - a setup that works today must not
    # break. (A new add is already stopped by F2; this covers entries created
    # before F2, or a host changed via Configure.)
    # Only ONCE per entry+host - while a device is offline, HA re-runs setup
    # on every ConfigEntryNotReady retry (log spam).
    warned = hass.data.setdefault(f"{DOMAIN}_overlap_warned", set())
    overlap_key = (entry.entry_id, str(host).strip())
    if overlap_key not in warned and overlap_key[1] in other_integration_hosts(hass):
        warned.add(overlap_key)
        _LOGGER.warning(
            "Raylogic MOD %s: this IP is also configured in the 'raylogic' "
            "(main) integration - two integrations will open two TCP "
            "connections to one module. Remove the device from one of them.",
            host,
        )
    model = conf.get(CONF_DEVICE_MODEL, DEFAULT_MODEL)
    model_info = DEVICE_MODELS.get(model, DEVICE_MODELS[DEFAULT_MODEL])
    channel_count = model_info["channel_count"]
    # 0 = relay-only auto/learn mode - no Area entered manually
    legacy_area = conf.get(CONF_LEGACY_AREA, LEGACY_DEFAULT_AREA)
    # In many installations channel numbering does not start at 1 (it is
    # assigned globally within the Area) - this device's first channel number.
    channel_start = conf.get(CONF_CHANNEL_START, 1)
    # The type set in the Raylogic GO app (relay/dimmer/fan/curtain/cct) -
    # keys are now the actual physical channel numbers (starting at
    # channel_start), resolved up to the model's channel_count.
    channel_types, channel_cct_modes = _resolve_channel_types(
        conf, channel_start, channel_count, fixed_type=model_info.get("fixed_type"),
    )

    device = RaylogicModDevice(
        ip=host, port=port,
        model=model,
        legacy_area=legacy_area,
        legacy_channel_count=channel_count,
        channel_start=channel_start,
        channel_types=channel_types,
        channel_cct_modes=channel_cct_modes,
        state_callback=lambda ip, ch, state: _handle_state_update(
            hass, entry.entry_id, ip, ch, state
        ),
        # P6: learned channels are kept in HA's .storage - safe across HACS updates
        state_dir=hass.config.path(".storage", DOMAIN),
    )

    # D1 (v1.6.3): stable id = entry.unique_id (host_port, fixed when the
    # device is added; unchanged if the host is changed via Configure) - old
    # node/ip-based ids are migrated here, BEFORE the platforms are set up.
    device.stable_id = entry.unique_id or entry.entry_id
    _async_migrate_ids(hass, entry, device.stable_id)

    connected = await device.connect()
    if not connected:
        # BUG FIX: this used to `return False` - HA then treated the entry as
        # "setup failed" outright, with no proper exponential-backoff retry.
        # Raising ConfigEntryNotReady makes HA retry by itself at intervals
        # (e.g. when a device joins the network a little late after booting)
        # - startup does not feel "stuck", and the integration recovers on
        # its own once the device appears, without a manual reload. (This is
        # separate from the other "stuck forever after a power cycle" bug,
        # which is now fixed - see _reconnect() in protocol.py.)
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
    r"^(?P<base>.+)_(?P<model>mod2u|mod4u|mod2f)_ch(?P<num>\d+)(?P<cct>_cct|_ctc)?$"
)
_SCENE_DEVICE_ID = "area_scenes"

# channel type -> (entity domain, unique_id suffix) - exact mirror of the platforms
_TYPE_TO_ENTITY = {
    CH_TYPE_RELAY: ("switch", ""),
    CH_TYPE_DIMMER: ("light", ""),
    CH_TYPE_CCT: ("light", "_cct"),
    CH_TYPE_FAN: ("fan", ""),
    CH_TYPE_CURTAIN: ("cover", ""),
}


def _keeper_rank(ent, group) -> tuple:
    """Which entity of a duplicate group is the original: when two entities
    would get the same entity_id, HA gives the second one "<id>_2", "_3"...,
    so an entity whose id is another candidate's entity_id + "_N" is
    definitely the duplicate. (created_at alone is not reliable: after a
    delete + re-add HA restores the old tombstone with its old created_at.)"""
    is_dup = any(
        other is not ent
        and re.fullmatch(re.escape(other.entity_id) + r"_\d+", ent.entity_id)
        for other in group
    )
    created = getattr(ent, "created_at", None)
    return (is_dup, created is None, created or 0, ent.entity_id)


def _async_migrate_ids(hass: HomeAssistant, entry: ConfigEntry, stable: str) -> None:
    """Move old "<node>_<model>_chN" / "<ip>_<model>_chN" unique_ids to
    "<stable>_<model>_chN". If two versions of a channel exist (node and ip,
    one of which became "_2"), keep the ORIGINAL (earliest created)
    entity_id - dashboards/automations use that one - and remove the
    duplicate. Old device registry identifiers are merged into one stable
    device as well."""
    ent_reg = er.async_get(hass)
    groups: dict[tuple, list] = {}
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if ent.platform != DOMAIN:
            continue
        m = _OLD_UID_RE.match(ent.unique_id)
        if not m:
            continue
        # v1.7.1: an old "_ctc" suffix becomes "_cct" in place - same entity,
        # same entity_id (dashboards keep working), no duplicate created.
        suffix = "_cct" if m["cct"] in ("_cct", LEGACY_CCT_UID_SUFFIX) else ""
        new_uid = f"{stable}_{m['model']}_ch{m['num']}{suffix}"
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
                    "Raylogic MOD: removing duplicate entity %s (it was a second "
                    "copy of %s)", dup.entity_id, keeper.entity_id,
                )
                ent_reg.async_remove(dup.entity_id)
        if keeper.device_id:
            kept_devices[keeper.device_id] = kept_devices.get(keeper.device_id, 0) + 1

    # Device registry: old (DOMAIN, node/ip) devices -> one stable device
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
        # The device that holds the kept (original) entities is the original
        # device - device-based automations are bound to its device_id. On a
        # tie, the earliest created one wins.
        keeper_dev = min(devices, key=lambda d: (
            -kept_devices.get(d.id, 0),
            getattr(d, "created_at", None) is None,
            getattr(d, "created_at", None) or 0,
        ))
        dev_reg.async_update_device(keeper_dev.id, new_identifiers={(DOMAIN, stable)})
    for dev in devices:
        if dev.id == keeper_dev.id:
            continue
        # Move the entities to the keeper device first, then remove the old
        # device (removing a device also removes its entities).
        for ent in er.async_entries_for_device(ent_reg, dev.id, include_disabled_entities=True):
            ent_reg.async_update_entity(ent.entity_id, device_id=keeper_dev.id)
        dev_reg.async_remove_device(dev.id)


def _async_purge_stale(
    hass: HomeAssistant, entry: ConfigEntry, device: RaylogicModDevice, legacy_area: int,
) -> None:
    """Remove this entry's registry entities that are no longer created (a
    channel type / First Channel changed via Configure, or a scene removed) -
    otherwise they remain as permanently "unavailable" orphans. Channels come
    from the config (they are not discovered from the device), so the
    expected set is deterministic. In LEARN mode (Area 0) channels are
    learned gradually - nothing is removed there."""
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
                "Raylogic MOD: removing stale entity %s (%s) - no longer in "
                "the config", ent.entity_id, ent.unique_id,
            )
            ent_reg.async_remove(ent.entity_id)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the whole entry as soon as the options flow is saved."""
    await hass.config_entries.async_reload(entry.entry_id)
    # The scene config can be set on any device, but scene/select entities
    # are only created on the "scene host" entry - if the merged map changed
    # and the host is a DIFFERENT entry, reload it too so new scenes are
    # created and removed ones disappear.
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
    """If the scene host device is DELETED, recreate the area scenes on
    another loaded device (otherwise they would be missing until a restart).
    This does not run on a normal reload - there the same entry becomes the
    host again."""
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
        # Area-scene echo (protocol._handle_ar) - for the scene selectors only;
        # it is not sent to channel entities.
        async_dispatcher_send(
            hass, SIGNAL_AREA_SCENE, int(ch[len("scene_"):]), state["scene"],
        )
        return
    async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_state_update", ch, state)
    async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_available", True)
