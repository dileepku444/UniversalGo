"""Raylogic MOD2U / MOD4U TCP protocol client - RE8-style architecture.

A single class (RaylogicModDevice) handles both models (MOD2U = 2 channels/
1 pair, MOD4U = 4 channels/2 pairs) - the channel count is decided by the
`model` parameter (const.py DEVICE_MODELS); the rest of the protocol logic
is independent of the pair count.

STATIC / config-based channel setup: a fixed channel count (from the model:
2 or 4), the area taken from the config or from LEGACY_DEFAULT_AREA (0x0C),
and each channel's type (relay/dimmer/fan/curtain/ctc) coming from the
config flow. Relay/Dimmer/Fan/Curtain/CTC are all fully working in this
mode (their command formats are already confirmed).

NOTE: there used to be an "AUTO / BR40" mode here as well (RE8/H81-style
auto-discovery via the `?BR40=` query) - MOD2U/MOD4U/MOD2F devices never
answer this query (confirmed via capture), so this path never worked for
these models and only added extra latency (waiting for the BR40 query) to
every connect(). At the user's request the whole BR40 code path has been
removed - only the static/legacy channel setup is used now, which was
always the path that actually worked.
"""
from __future__ import annotations


import asyncio
import json
import logging
import random
import re
import socket
import struct
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    # SIOCOUTQ (Linux) reports how many bytes in the socket's send buffer
    # are still UN-ACKED - this is our real proof of whether a command
    # reached the device (the device itself sends no ACK - see the COMMAND
    # DELIVERY block in const.py).
    # HA OS / Supervised / Docker are all Linux, but if this ever runs on
    # another platform, the import fails and verification is skipped
    # silently; everything else keeps working as before.
    import fcntl

    _SIOCOUTQ = 0x5411
except ImportError:  # pragma: no cover - non-Linux
    fcntl = None
    _SIOCOUTQ = None

from .const import (
    CONNECT_TIMEOUT,
    CLOSE_TIMEOUT,
    WRITE_TIMEOUT,
    CMD_ADDR_HIGH,
    CMD_CHANNEL_DIRECT,
    RELAY_LEVEL_ON,
    RELAY_LEVEL_OFF,
    DIMMER_LEVEL_ON,
    DIMMER_LEVEL_OFF,
    FAN_LEVEL_OFF,
    FAN_SPEEDS,
    CURTAIN_CMD_ADDR_HIGH,
    CURTAIN_CMD_MOVE,
    CURTAIN_CMD_STOP,
    CURTAIN_DIR_OPEN,
    CURTAIN_DIR_CLOSE,
    CURTAIN_RUN_BYTE_DEFAULT,
    CURTAIN_STOP_TAIL,
    CHANNELS_PER_CURTAIN_SLOT,
    CHANNELS_PER_PAIR,
    DEVICE_MODELS,
    DEFAULT_MODEL,
    CLIENT_SENDER_ID,
    RESYNC_INTERVAL,
    SCENE_FUNC,
    SCENE_AREAS,
    SCENE_MAX,
    CH_TYPE_RELAY,
    CH_TYPE_DIMMER,
    CH_TYPE_FAN,
    CH_TYPE_CURTAIN,
    CH_TYPE_CTC,
    CTC_MODE_SINGLE,
    CTC_MODE_DOUBLE,
    CTC_SINGLE_BRIGHTNESS_ON,
    CTC_SINGLE_BRIGHTNESS_OFF,
    CTC_SINGLE_CT_MIN_LEVEL,
    CTC_SINGLE_CT_MAX_LEVEL,
    CTC_DOUBLE_CONST_BYTE,
    CTC_MIN_KELVIN,
    CTC_MAX_KELVIN,
    CTC_DEFAULT_KELVIN,
    DEFAULT_CHANNEL_COUNT,
    LEGACY_DEFAULT_AREA,
    AREA_MIN,
    AREA_MAX,
    KEEPALIVE_CMD,
    KEEPALIVE_INTERVAL,
    KEEPALIVE_IDLE_THRESHOLD,
    KEEPALIVE_VARIANTS,
    KEEPALIVE_GOOD_SESSION,
    KA_REPLY_MIN_GAP,
    FAST_RECONNECT_MIN_SESSION,
    FAST_RECONNECT_DELAY,
    LISTEN_READ_TIMEOUT,
    INITIAL_DRAIN_QUIET,
    INITIAL_DRAIN_MAX,
    RX_SILENCE_TIMEOUT,
    RECONNECT_BACKOFF_STEPS,
    RECONNECT_SETTLE_MIN,
    RECONNECT_SETTLE_MAX,
    DELIVERY_VERIFY_TIMEOUT,
    DELIVERY_VERIFY_POLL,
    LINK_SUSPECT_SECONDS,
    PENDING_COMMAND_MAX_AGE,
    COMMAND_MAX_ATTEMPTS,
    UNAVAILABLE_GRACE_SECONDS,
    SOFT_RECONNECT_SETTLE_MIN,
    SOFT_RECONNECT_SETTLE_MAX,
)

_LOGGER = logging.getLogger(__name__)

# Old location (up to v1.6.3) - inside the integration folder, which is
# wiped by a HACS update. Now only read for migration (P6).
_STATE_DIR = Path(__file__).parent / "device_state"

# P2 (v1.6.4): line terminator - the confirmed firmware sends "\r", but a
# firmware sending "\n" or "\r\n" must work too. The limit is the asyncio
# StreamReader default (64 KiB) - a longer "line" is garbage -> reconnect.
_LINE_END_RE = re.compile(rb"[\r\n]")
_RX_LINE_LIMIT = 2 ** 16
# P3: frame-type token such as "*AR=" / "+AR40=" (for logging unknown types once)
_FRAME_TYPE_RE = re.compile(r"[*+?][A-Z]{2}\d{0,2}=")
# v1.6.5: only update kelvin from an *AZ= status when the combined output of
# both channels is at least this much (~10% brightness) - below that, byte
# quantization destroys the colour information (at 1% one channel rounds
# to off).
_AZ_MIN_OUTPUT_FOR_KELVIN = 0.10

# SCALE FIX: there used to be no limit on how many devices could attempt
# their TCP connect() at the same time - at HA startup (all config entries
# are set up at roughly the same moment), or when a small hiccup on the
# network/router triggered a resync/reconnect of many devices at once,
# 100 devices tried 100 new TCP connections + initial-burst reads within
# the same second. On a small network (home router/switch) or HA host
# (such as a Raspberry Pi), this burst by itself caused a cascade of
# timeouts/"connection lost" - which looked like each device's own
# connect() failing, while the real cause was simply that everything
# happened AT THE SAME TIME. Now at most `_MAX_CONCURRENT_CONNECTS`
# devices across the whole integration can run their connect handshake
# (TCP open + initial burst read + discovery) concurrently - the rest wait
# their turn (briefly, in a small queue), so the load always stays smooth,
# whether there are 5 devices or 500.
_MAX_CONCURRENT_CONNECTS = 15
_connect_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CONNECTS)


class RaylogicModDevice:
    """One physical MOD2U or MOD4U module = one TCP connection."""

    def __init__(
        self,
        ip: str,
        port: int,
        model: str = DEFAULT_MODEL,
        legacy_area: int = LEGACY_DEFAULT_AREA,
        legacy_channel_count: int = DEFAULT_CHANNEL_COUNT,
        channel_start: int = 1,
        channel_types: Optional[dict[int, str]] = None,
        channel_ctc_modes: Optional[dict[int, str]] = None,
        state_callback: Optional[Callable] = None,
        state_dir: Optional[str] = None,
    ):
        self.ip = ip
        self.port = port
        self.state_callback = state_callback

        # Which physical device this is (MOD2U or MOD4U) - used for
        # device_info (name/model text). Each model has its own
        # DEVICE_MODELS entry in const.py.
        self._model = model
        _model_info = DEVICE_MODELS.get(model, DEVICE_MODELS[DEFAULT_MODEL])
        self._model_name: str = _model_info["name"]
        self._model_desc: str = _model_info["desc"]

        # Legacy-mode fallback settings (from config_flow or defaults)
        self._legacy_area = legacy_area
        self._legacy_channel_count = legacy_channel_count
        # In many installations this module's first physical channel number
        # is not 1 (it is assigned globally within the Area) - as confirmed
        # in a real capture: manually created "ch1/ch2" did not work because
        # that device's real channels were 3 and 4.
        self._channel_start = max(1, channel_start)
        # The type set for each channel in the Raylogic GO app
        # (relay/dimmer/fan/curtain) - the device does not report it itself,
        # so it comes manually from config_flow. {ch_num: type_str}
        self._channel_types: dict[int, str] = channel_types or {}
        # For CTC channels: 'single' (CW/WW, *AR= frames) or 'double'
        # (warm+cool, *AZ= frames) - comes from config_flow. Ignored for
        # non-CTC channels.
        self._channel_ctc_modes: dict[int, str] = channel_ctc_modes or {}

        # LEARN mode: if no manual area/channel count is given, the device
        # learns Area + Channel by listening to *AR= echoes (from the app/a
        # physical switch) - the same result as the DIN devices' BR40
        # auto-detect, without guessing any unknown byte (only the confirmed
        # relay format is used: 00 1A <area> <level> <channel>).
        self._state_key = f"{ip.replace('.', '_')}_{port}"
        # P6 (v1.6.4): the learned-channels file now lives in HA's
        # /config/.storage/raylogic_mod/ (provided by __init__.py) - the
        # integration folder is replaced by a HACS update, which wiped the
        # file. An old file is copied over on the first load (_load_learned).
        self._state_dir = Path(state_dir) if state_dir else _STATE_DIR
        self._state_file = self._state_dir / f"{self._state_key}_learned.json"
        self._legacy_state_file = _STATE_DIR / f"{self._state_key}_learned.json"
        # P2: our own RX buffer (lines are split on both \r and \n)
        self._rx_buf = b""
        # P3/P4/P5: keys of messages that are logged "only once"
        self._logged_once: set = set()
        # BUG FIX: this used to read from disk synchronously right here in
        # __init__ (the constructor) - but __init__ is called directly from
        # HA's event loop (from async_setup_entry), so this blocking read/
        # write could freeze the whole Home Assistant event loop (not just
        # this integration - all entities/automations/UI) for a while,
        # especially on a slow disk (SD card / Pi) - exactly the "HA
        # hang/stuck" symptom. It is now loaded asynchronously inside
        # connect() (in a thread) - see _ensure_learned_loaded().
        self._learned: dict[int, int] = {}  # {ch_num: area}
        self._learned_loaded = False

        # switch.py registers this - called with (ch_num, initial_state)
        # whenever a NEW channel is learned for the first time, so its entity
        # can be added to HA dynamically right away.
        self.new_channel_callback: Optional[Callable] = None

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._msg_counter = 0
        self._listen_task: Optional[asyncio.Task] = None
        self._ka_task: Optional[asyncio.Task] = None
        self._resync_task: Optional[asyncio.Task] = None
        # STABILITY FIX (v1.5.0) - the real root cause of "connection lost".
        # Every connect()/reconnect() created new _listen/_ka/_resync tasks,
        # but the OLD tasks were never cancelled. An old resync task stayed
        # in its sleep; when it woke up it found the connection alive again
        # and performed a soft reconnect of its own - so 2, 3, 4... resync
        # loops ended up running for the same device. The effect in real
        # logs: despite RESYNC_INTERVAL being 25 s, 94% of resync gaps were
        # UNDER 20 s (some as short as 4.8 s), and 202 TCP sessions were
        # opened in 10 minutes, 174 of them by our own resync.
        #
        # Now every successful connect takes a new "generation" number. All
        # background loops remember their generation and exit immediately as
        # soon as it changes - old tasks have no effect. In addition,
        # _cancel_bg_tasks() cancels them explicitly.
        self._conn_generation = 0
        # When ANYTHING (KA/AR/AZ) was last received from the device - both
        # the passive keepalive and the passive dead-connection detection
        # rely on this (see _keepalive_loop / _listen_loop).
        self._last_rx: float = 0.0
        self._last_tx: float = 0.0
        self._session_started: float = 0.0
        # How long the previous session lasted - this decides fast
        # reconnect (expected ~12 s close vs genuine failure).
        self._last_session_len: float = 0.0
        # KEEPALIVE AUTO-TUNING (v1.5.3): the device drops the connection
        # every ~12 s because it does not recognise our reply to its *KA=
        # (our keepalive was the only frame sent without the "<id>,"
        # prefix). Instead of guessing the right format, the code learns it
        # by trying - see the KEEPALIVE FORMAT block in const.py.
        self._ka_variant = 0
        self._ka_variant_locked = False
        self._ka_best: dict[int, float] = {}
        self._last_ka_reply: float = 0.0
        self._resyncing = False  # prevents duplicates during a soft reconnect
        # BUG FIX (repeated-EOF-storm bug): _resync_loop() and
        # _keepalive_loop() both used to draw their "first-time random
        # stagger" (to spread the load of 100+ devices) afresh EVERY TIME -
        # because every connect()/reconnect() creates a NEW task, and the
        # `random.uniform(...)` inside was new each time too. So after each
        # reconnect the resync wait was sometimes 25 s and sometimes only
        # 1-2 seconds - whenever a short wait came up, the connection was
        # closed and reopened just 1-2 seconds after being established,
        # which confused the device's fragile Wi-Fi/TCP stack into sending
        # an EOF (which is why the log repeatedly showed "the peer closed
        # the connection (EOF)"). The random stagger should only happen the
        # FIRST time in the device instance's whole lifetime (HA startup),
        # not on every reconnect. These two flags (instance-level,
        # persisting across connect() calls) now track that - see
        # _resync_loop() and _keepalive_loop() below.
        self._resync_staggered = False
        self._keepalive_staggered = False
        # BUG FIX: read errors, write errors and the periodic resync could
        # each run their own independent reconnect/connect() - if two ran at
        # the same time (e.g. a real disconnect coinciding with a resync),
        # TWO TCP connections were opened to the device AT ONCE. This small
        # embedded device got confused by that and got stuck - the HA
        # integration had to be reloaded. connect() now only runs inside
        # this lock (one attempt at a time), and the _reconnecting flag
        # prevents duplicate delayed retries from being scheduled.
        self._connect_lock = asyncio.Lock()
        self._reconnecting = False
        self._reconnect_task: Optional[asyncio.Task] = None
        # STABILITY FIX: for short disconnects that recover immediately
        # (such as our own periodic resync, or a 5-10 s network blip), do
        # not show "Unavailable" in the UI immediately - see
        # _mark_unavailable_soon().
        self._unavailable_task: Optional[asyncio.Task] = None
        # UX FIX: if the connection was found down while sending a command
        # (which could even happen during our own short 25 s resync cycle),
        # the command used to be DROPPED silently - only a warning was
        # logged, and the user could not tell from the UI that the on/off
        # command never reached the device. Such commands are now kept in a
        # small queue here, and as soon as the connection is back (within a
        # few seconds), _do_connect() replays/flushes them automatically -
        # the user does not have to press the button again.
        # {cmd: str, at: float} - `at` ensures a very old command is not
        # replayed (see PENDING_COMMAND_MAX_AGE).
        self._pending_commands: list[dict] = []
        # P1 (v1.6.7): prevents "false success" - the channel of every
        # command sent ({full_cmd: ch}) and that channel's LAST CONFIRMED
        # state (from before the command). When the module receives the
        # command (TCP ACK) the snapshot is dropped; when the command is
        # DROPPED (module offline, expiry/overflow/max attempts), the channel
        # is restored to this state and HA is updated.
        self._cmd_channel: dict[str, int] = {}
        self._pre_state: dict[int, dict] = {}
        # P3 (v1.6.7): one outage = one ERROR + one WARNING, the rest DEBUG;
        # one INFO when it comes back (how long it was offline).
        self._outage_since: Optional[float] = None
        self._outage_attempts = 0
        self._MAX_PENDING_COMMANDS = 5
        # COMMAND-DELIVERY FIX (v1.5.2): a write and a close must never run
        # at the same time. Previously _close_writer_safe() from
        # _soft_reconnect/_reconnect could close the socket exactly while
        # _send_raw's data was still in the OS buffer - the command vanished
        # without any error. Both now run inside the same lock.
        self._send_lock = asyncio.Lock()
        # Strong references to running delivery-verification tasks.
        self._verify_tasks: set[asyncio.Task] = set()
        # ON-DEMAND RECONNECT: when an entity is toggled manually or a scene
        # is triggered while the device is disconnected, do not wait for the
        # background _reconnect() loop's backoff (sometimes up to 30 s) -
        # fire a connect() attempt immediately so the command is applied as
        # soon as possible. See _trigger_on_demand_connect() below.
        self._on_demand_task: Optional[asyncio.Task] = None
        # BUG FIX (the big one - device power-cycle "stuck forever" bug):
        # unless _shutdown is True (i.e. disconnect() / HA unload happened
        # explicitly), reconnect will NEVER give up permanently - see
        # _reconnect() below.
        self._shutdown = False

        # Device identity
        self.node_id: Optional[str] = None       # e.g. "101" - the device's own ID
        # v1.6.3 (D1): STABLE base of the entity unique_id / device
        # identifier. It used to be "node_id or ip" - node_id was only known
        # if *KA= arrived within 0.6 s of connecting (an idle module sends it
        # every ~6 s), so the unique_id changed from restart to restart ->
        # "_2" duplicate entities + old orphans. The config entry's unique_id
        # (host_port, fixed when the device is added) is used now - set by
        # __init__.py; this default is only a fallback (same format).
        self.stable_id: str = f"{ip}_{port}"
        self.mac: Optional[str] = None
        self.fw_version: Optional[str] = None

        # The last "run" byte of the curtain frame (travel parameter). The
        # default is CURTAIN_RUN_BYTE_DEFAULT, but as soon as a real curtain
        # echo arrives from the device (operated from the app or a physical
        # switch), the real value for that slot is learned here - so HA
        # sends exactly what the Raylogic GO app sends.
        # {curtain_slot: run_byte}
        self._curtain_run_bytes: dict[int, int] = {}

        # The Area/channel range the device reported about itself in its
        # *KA= frame (self-report) - used to verify the config. See
        # _handle_ka_line() / _verify_against_device_report().
        self.detected_area: Optional[int] = None
        self.detected_channel_start: Optional[int] = None
        self.detected_channel_end: Optional[int] = None
        self._config_mismatch_logged = False

        # channel_states[ch_num] = {"area": int, "type": str, "on": bool, ...}
        self.channel_states: dict[int, dict] = {}

        # v1.6.0: the last recalled scene per Area {area: scene} - updated
        # by keypad/app echoes (*AR=000F..) or HA's own recall. select.py
        # takes its initial value from this.
        self.active_scene: dict[int, int] = {}

        # BUG FIX (v1.5.4, found by the journal test): manual-mode channels
        # are now created RIGHT HERE, in the constructor.
        #
        # They used to be created only inside a SUCCESSFUL connect()
        # (_setup_legacy_channels). So if the device was not reachable at
        # that moment (still booting, Wi-Fi not up yet, or a refused
        # connect), channel_states stayed EMPTY - and set_relay/set_dimmer/
        # set_fan/set_cover all look up `area` first and returned SILENTLY
        # when it was missing. The entity was visible and could be clicked,
        # but the command was never sent - neither queued nor retried. In
        # the test, one such device managed to send 0 of 5 commands.
        #
        # Area and channel types come from config_flow, not from the device
        # - there was never any need to wait for the connection. A command
        # now always at least goes into the queue and reaches the device
        # automatically once the connection is established.
        # (LEARN mode - area 0 - still depends on connect(), because there
        # the channels are learned from the device's own frames.)
        if self._legacy_area and self._legacy_area > 0:
            self._setup_legacy_channels()

    # ------------------------------------------------------------------ #
    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def ip_suffix(self) -> str:
        return self.ip.split(".")[-1]

    @property
    def model(self) -> str:
        return self._model

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_desc(self) -> str:
        return self._model_desc

    @property
    def channel_start(self) -> int:
        return self._channel_start

    def pair_index_for_channel(self, ch_num: int) -> int:
        """Which pair of this DEVICE the channel belongs to (0 = first pair,
        1 = second pair). For display/logging only - the curtain wire byte
        is NOT built from this but from curtain_slot_for_channel()."""
        lo, _hi = self._pair_bounds(ch_num)
        return (lo - self._channel_start) // CHANNELS_PER_PAIR

    def curtain_slot_for_channel(self, ch_num: int) -> int:
        """GLOBAL curtain slot number - this is the third byte of the
        curtain frame (see the Curtain block in const.py).

        In a Raylogic installation channel numbers are allocated globally
        across the whole system, always in blocks of 2, so the slot of any
        pair is derived directly from its lower channel number:

            slot = (lower channel of the pair + 1) // 2

        Examples (verified on the user's real devices):
            ch 23-24 (Area 08, MOD2U)  -> slot 12 (0x0C)
            ch 13-14 (Area 12, MOD4U)  -> slot  7 (0x07)
            ch  3-4  (Area 07, MOD4U)  -> slot  2 (0x02)
            ch  5-6  (Area 07, MOD4U)  -> slot  3 (0x03)

        This is why any device can now be added, in any Area (1-16) and on
        any channel numbers - the correct curtain frame is built
        automatically; nothing needs to be hardcoded."""
        lo, _hi = self._pair_bounds(ch_num)
        return (lo + 1) // CHANNELS_PER_CURTAIN_SLOT

    def curtain_frame(self, ch_num: int, action: str) -> Optional[str]:
        """Build the full curtain hex payload for `action` ('open'/'close'/
        'stop') (the part after *AR=). This is now 100% derived - there are
        no per-pair hardcoded literals."""
        slot = self.curtain_slot_for_channel(ch_num)
        if action == "stop":
            return (
                f"{CURTAIN_CMD_ADDR_HIGH}{CURTAIN_CMD_STOP:02X}"
                f"{slot:02X}{CURTAIN_STOP_TAIL}"
            )
        if action == "open":
            direction = CURTAIN_DIR_OPEN
        elif action == "close":
            direction = CURTAIN_DIR_CLOSE
        else:
            return None
        run = self._curtain_run_bytes.get(slot, CURTAIN_RUN_BYTE_DEFAULT)
        return (
            f"{CURTAIN_CMD_ADDR_HIGH}{CURTAIN_CMD_MOVE:02X}"
            f"{slot:02X}{direction:02X}{run:02X}"
        )

    def _find_curtain_channel(self, slot: int) -> Optional[int]:
        """Find our configured curtain channel from the slot byte of an
        incoming curtain frame (a curtain frame has no Area byte at all, so
        it cannot be matched by area)."""
        for cn, st in self.channel_states.items():
            if st.get("type") != CH_TYPE_CURTAIN:
                continue
            if self.curtain_slot_for_channel(cn) == slot:
                return cn
        return None

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    async def connect(self) -> bool:
        if self._shutdown:
            return False
        async with self._connect_lock:
            if self._connected:
                _LOGGER.debug(
                    "Raylogic %s %s: already connected, skip duplicate "
                    "connect() call.", self._model_name, self.ip,
                )
                return True
            return await self._do_connect()

    async def _do_connect(self) -> bool:
        if not self._learned_loaded:
            # The disk read now happens in a thread (asyncio.to_thread) - the
            # event loop is never blocked, however slow the disk is.
            self._learned = await asyncio.to_thread(self._load_learned)
            self._learned_loaded = True
        # BUG FIX (v1.5.4, caught by the stress test): always close the old
        # socket BEFORE opening a new one.
        #
        # Previously only _soft_reconnect() closed the old socket. All other
        # paths - EOF (listen loop), delivery-verify failure, write error,
        # RX silence - only set `_connected = False` and left the socket as
        # it was. The peer had sent a FIN, but the connection was never
        # closed from OUR side (it stayed half-closed), and on top of that a
        # new connect() opened ANOTHER socket. From the device's point of
        # view there were TWO connections from a single client - exactly
        # the situation in which these small modules get confused and hang
        # or start ignoring commands. The stress test reproduced this on 44
        # out of 100 devices.
        #
        # This is now a central fix in one place - _do_connect never opens a
        # new socket while leaving the old one open.
        await self._close_writer_safe()
        self._reader = None

        async with _connect_semaphore:
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.ip, self.port),
                    timeout=float(CONNECT_TIMEOUT),
                )
                # RESEARCH-BACKED FIX: these Raylogic modules use a cheap
                # ESP8266-class Wi-Fi chip - a chip family known for a
                # well-documented quirk (several reports such as ESP8266
                # Arduino core issue #2552): if Nagle's algorithm is ON for
                # TCP (small packets are briefly buffered/delayed so they
                # can be combined into a larger packet), their small/buggy
                # TCP stack sometimes decides the connection is "closed" on
                # its own and sends an EOF. Setting TCP_NODELAY turns
                # Nagle's algorithm OFF - every small command (such as our
                # *KA=01) is sent immediately without sitting in a buffer,
                # which gives these modules' stack less chance to get
                # confused.
                try:
                    sock = self._writer.get_extra_info("socket")
                    if sock is not None:
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except (OSError, AttributeError) as exc:
                    _LOGGER.debug(
                        "Raylogic %s: could not set TCP_NODELAY (not "
                        "fatal): %s", self.ip, exc,
                    )
                self._connected = True
                self._reconnecting = False
                self._rx_buf = b""          # P2: new socket, new buffer
                self._logged_once.discard("ar40")   # P5: per-session
                # New generation - old background loops (if any are still
                # sitting in their sleep) are invalidated immediately by this
                # and exit on their own.
                self._conn_generation += 1
                gen = self._conn_generation
                now = self._now()
                prev_session = (
                    now - self._session_started if self._session_started else 0.0
                )
                self._session_started = now
                self._last_rx = now
                await self._cancel_bg_tasks()
                if self._outage_since is not None:
                    _LOGGER.info(
                        "Raylogic %s %s: back online - was offline for %.0fs "
                        "(%d reconnect attempts).", self._model_name, self.ip,
                        now - self._outage_since, self._outage_attempts,
                    )
                    self._outage_since = None
                    self._outage_attempts = 0
                if prev_session:
                    _LOGGER.info(
                        "Connected to Raylogic %s at %s (previous session "
                        "lasted %.0fs)", self._model_name, self.ip, prev_session,
                    )
                else:
                    _LOGGER.info(
                        "Connected to Raylogic %s at %s", self._model_name, self.ip,
                    )

                # LATENCY FIX: previously any pending/queued command (such as
                # an on-demand reconnect - the user pressed a button while the
                # device was disconnected) was only sent AFTER
                # _drain_initial_push() (a fixed 2.5 s wait) and the whole
                # channel setup + task creation had completed. Writing and
                # reading on TCP are two completely independent directions -
                # sending a command does not require reading (draining) the
                # device's initial burst. So the command is now sent
                # IMMEDIATELY once TCP connects, without waiting for drain/
                # setup - the real "button press to device" latency of an
                # on-demand reconnect drops from ~2.5-3.5 s (the previous
                # guaranteed minimum) to just the TCP handshake time (often a
                # few hundred milliseconds on a LAN).
                if self._pending_commands:
                    pending, self._pending_commands = self._pending_commands, []
                    fresh = [
                        p for p in pending
                        if (now - p["at"]) <= PENDING_COMMAND_MAX_AGE
                    ]
                    for p in pending:
                        if (now - p["at"]) > PENDING_COMMAND_MAX_AGE:
                            self._cmd_dropped(p["cmd"], f"module was offline for more than {PENDING_COMMAND_MAX_AGE}s")
                    stale = len(pending) - len(fresh)
                    if stale:
                        _LOGGER.debug(
                            "Raylogic %s: dropped %d stale pending command(s) "
                            "(older than %ds).",
                            self.ip, stale, PENDING_COMMAND_MAX_AGE,
                        )
                    if fresh:
                        _LOGGER.info(
                            "Raylogic %s: connection restored - sending %d "
                            "pending command(s) IMMEDIATELY (before drain/"
                            "setup, to reduce latency). The user does not "
                            "need to click again.",
                            self.ip, len(fresh),
                        )
                    # BUG FIX (v1.5.4, caught by the stress test): previously
                    # the whole list was TAKEN OUT of the queue and sent in a
                    # local loop. If any exception occurred in the middle of
                    # that loop (such as the new connection dying
                    # immediately), the loop stopped there and the REMAINING
                    # commands - already removed from the queue - were lost
                    # forever. In the mild-chaos stress test, 5 out of 500
                    # commands were lost this way (queue empty, yet not
                    # delivered).
                    #
                    # The queue now "owns" them: a command is only removed
                    # when it is being sent right away, and if anything goes
                    # wrong in between, the rest stay safely in the queue
                    # (the next connect will send them). The loop is bounded
                    # so that _send_raw's own re-queueing cannot turn it into
                    # an infinite loop.
                    self._pending_commands = fresh + self._pending_commands
                    for _ in range(len(fresh)):
                        if not self._pending_commands:
                            break
                        p = self._pending_commands.pop(0)
                        # verify=True so that the replay is confirmed too,
                        # and queue_on_disconnect=True so that if this new
                        # connection also dies immediately, the command is
                        # re-queued instead of being DROPPED.
                        await self._send_raw(
                            p["cmd"], queue_on_disconnect=True, verify=True,
                            attempt=p.get("attempt", 0),
                        )

                await self._drain_initial_push()

                # BR40 auto-discovery has been removed entirely (at the
                # user's request) - MOD2U/MOD4U/MOD2F never answered the
                # `?BR40=` query, so this path never worked and only added
                # extra latency (the BR40 probe wait) to every connect().
                # The static/legacy channel setup is used directly now -
                # which was always the path that actually worked.
                self._setup_legacy_channels()

                self._listen_task = asyncio.create_task(self._listen_loop(gen))
                self._ka_task = asyncio.create_task(self._keepalive_loop(gen))
                self._resync_task = asyncio.create_task(self._resync_loop(gen))

                self._cancel_unavailable_grace()
                if self.state_callback:
                    self.state_callback(self.ip, None, {"available": True})

                return True

            except Exception as exc:
                # str(exc) can be empty (e.g. a bare ConnectionResetError) -
                # log the type as well, otherwise the log only shows
                # "Failed to connect ...:" with no reason.
                # P3 (v1.6.7): only the first failure of an outage is ERROR, the rest DEBUG
                first = self._outage_since is None
                if first:
                    self._outage_since = self._now()
                self._outage_attempts += 1
                (_LOGGER.error if first else _LOGGER.debug)(
                    "Failed to connect to Raylogic %s: %s%s",
                    self.ip, type(exc).__name__,
                    f" - {exc}" if str(exc) else "",
                )
                self._connected = False
                # BUG FIX: previously the reader/writer were not closed here
                # if an exception occurred after connecting (e.g. in the
                # auto-discovery step) - every 30 s retry leaked a TCP
                # socket, which over time could slow down or hang the whole
                # system through file-descriptor exhaustion on the HA host.
                await self._close_writer_safe()
                self._reader = None
                return False

    @staticmethod
    def _now() -> float:
        return asyncio.get_event_loop().time()

    def _keepalive_frame(self) -> str:
        """The full frame of the keepalive variant currently being tried/
        locked. See the KEEPALIVE FORMAT block in const.py."""
        template = KEEPALIVE_VARIANTS[self._ka_variant % len(KEEPALIVE_VARIANTS)]
        return template.format(
            id=CLIENT_SENDER_ID, seq=self._next_msg(), cmd=KEEPALIVE_CMD,
        )

    def _record_session_result(self, length: float) -> None:
        """When a session ends: remember how long this keepalive variant
        kept the connection alive, and lock it in if it breaks the
        12-second wall."""
        self._last_session_len = length
        if self._ka_variant_locked:
            return
        idx = self._ka_variant % len(KEEPALIVE_VARIANTS)
        self._ka_best[idx] = max(self._ka_best.get(idx, 0.0), length)
        if length >= KEEPALIVE_GOOD_SESSION:
            self._ka_variant_locked = True
            _LOGGER.info(
                "Raylogic %s %s: keepalive format '%s' kept the connection "
                "alive for %.0fs (the device's ~12s auto-disconnect was "
                "broken) - this format will always be used from now on.",
                self._model_name, self.ip, KEEPALIVE_VARIANTS[idx], length,
            )
            return
        # This variant did not work - try the next one on the next connect.
        self._ka_variant += 1
        nxt = self._ka_variant % len(KEEPALIVE_VARIANTS)
        _LOGGER.debug(
            "Raylogic %s: with keepalive format '%s' the session only lasted "
            "%.0fs - trying the next format: '%s'",
            self.ip, KEEPALIVE_VARIANTS[idx], length, KEEPALIVE_VARIANTS[nxt],
        )

    async def _answer_device_keepalive(self) -> None:
        """The device sends its own *KA= every ~6 s and expects a REPLY -
        after two missed replies (6x2 = the same 12 seconds) it drops the
        connection. So we now reply to every device KA immediately (the old
        code sent on a fixed timer, and without the prefix - see const.py).
        It is rate-limited so that we do not spam if the device ever sends
        KAs in a burst."""
        if not self._connected or self._shutdown:
            return
        if self._last_ka_reply and (
            self._now() - self._last_ka_reply
        ) < KA_REPLY_MIN_GAP:
            return
        self._last_ka_reply = self._now()
        await self._send_raw(self._keepalive_frame())

    async def _cancel_bg_tasks(self):
        """Stop the old listen/keepalive/resync tasks.

        STABILITY FIX (v1.5.0): this never used to happen - every reconnect
        created new tasks while the old ones stayed alive in their sleep.
        Later they woke up and did their work again (especially the resync's
        close/reopen of the connection), so multiple resync loops piled up
        on a single device and the connection kept breaking.

        A task does not cancel itself (the soft reconnect runs from inside
        _resync_loop itself) - in that case the _conn_generation guard makes
        the old task exit immediately."""
        current = asyncio.current_task()
        for attr in ("_listen_task", "_ka_task", "_resync_task"):
            task = getattr(self, attr, None)
            if task is not None and task is not current and not task.done():
                task.cancel()
            if task is not current:
                setattr(self, attr, None)

    async def _close_writer_safe(self):
        """BUG FIX: always wrap writer.close() + wait_closed() in
        CLOSE_TIMEOUT. Previously there was no upper bound, so if the
        device did not close the TCP connection cleanly (flaky network /
        cheap embedded device), wait_closed() could hang without any limit
        - this was the root cause of the "connect() hangs for a while
        again" symptom. Now the writer is discarded even on timeout
        (best-effort close), so the caller never waits longer than
        CLOSE_TIMEOUT."""
        if not self._writer:
            return
        # COMMAND-DELIVERY FIX (v1.5.2): close while holding the send lock,
        # so that a command that has just been written is not truncated
        # mid-way. The lock also has a timeout - if the lock is ever stuck
        # for some reason, the close must still happen (otherwise reconnect
        # would hang).
        try:
            await asyncio.wait_for(
                self._send_lock.acquire(), timeout=float(CLOSE_TIMEOUT)
            )
            locked = True
        except (asyncio.TimeoutError, RuntimeError):
            locked = False
        try:
            if not self._writer:
                return
            writer = self._writer
            self._writer = None
            try:
                writer.close()
                await asyncio.wait_for(
                    writer.wait_closed(), timeout=float(CLOSE_TIMEOUT)
                )
            except Exception:
                pass
        finally:
            if locked:
                self._send_lock.release()

    async def disconnect(self):
        # Set this first - so that if a reconnect loop is already running
        # (the device is still down), it stops on its own after its next
        # sleep/attempt and does not keep running forever in the background
        # after an HA unload/reload.
        self._shutdown = True
        self._connected = False
        for task in (
            self._listen_task, self._ka_task, self._resync_task,
            self._reconnect_task, self._unavailable_task,
            self._on_demand_task, *tuple(self._verify_tasks),
        ):
            if task:
                task.cancel()
        await self._close_writer_safe()

    async def _reconnect(self):
        """BUG FIX (THE main "HA stays stuck forever after a device power
        cycle" bug): previously this function retried connect() only ONCE,
        after 30 seconds - if that single attempt failed (e.g. the device
        was still booting, or came onto the network a little later),
        `_reconnecting = False` was reset in `finally` and the function
        simply ended - no further retry was EVER scheduled. Since
        `_send_raw()` (when `_connected` is already False) silently DROPPED
        the command without calling `_schedule_reconnect()` again, the
        integration got permanently stuck in the "disconnected" state -
        even after the device was back and pingable, until someone reloaded
        or restarted HA manually. In the real world a device power cycle
        (off then on) takes more than 30 seconds to boot, so this triggered
        almost every time - exactly the reported symptom: the device back
        up and pingable, but the HA entities unavailable/stuck forever.

        Fix: this is now a proper LOOP - it keeps retrying every 30 seconds
        until connect() succeeds OR the integration is explicitly
        disconnected/unloaded (`_shutdown`), just like the "retrying in
        30s" log message that already existed (except that now it really
        does retry repeatedly)."""
        try:
            # STABILITY FIX: do not fire "available: False" immediately -
            # start a short grace timer (see _mark_unavailable_soon). If the
            # backoff loop below reconnects within the grace window, the UI
            # never shows a flicker.
            self._mark_unavailable_soon()
            attempt = 0
            # FAST RECONNECT (v1.5.3): if the previous session lasted a
            # normal long time, this is the device's expected ~12 s
            # auto-disconnect, not a failure - reconnect immediately.
            # Measured impact: downtime per cycle dropped from ~2.3 s to
            # ~0.1 s, uptime 84% -> 98%.
            fast = self._last_session_len >= FAST_RECONNECT_MIN_SESSION
            while not self._connected and not self._shutdown:
                if fast and attempt == 0:
                    delay = FAST_RECONNECT_DELAY
                    _LOGGER.debug(
                        "Raylogic %s %s: expected auto-disconnect - "
                        "reconnecting immediately (%.2fs).", self._model_name, self.ip, delay,
                    )
                else:
                    delay = RECONNECT_BACKOFF_STEPS[
                        min(attempt, len(RECONNECT_BACKOFF_STEPS) - 1)
                    ]
                    # P3: the reason for the disconnect (read error / send
                    # error / silence / session) has already been logged -
                    # this retry line duplicated that event, hence DEBUG.
                    _LOGGER.debug(
                        "Raylogic %s %s: connection lost, retrying in %ds",
                        self._model_name, self.ip, delay,
                    )
                if self._outage_since is None:
                    self._outage_since = self._now()
                # P1: expire + revert old queued commands even while offline
                # (so HA shows the truth within ~30 s)
                self._expire_pending()
                attempt += 1
                await asyncio.sleep(delay)
                if self._shutdown:
                    return
                if fast and attempt == 1:
                    # Expected close - no settle gap needed (the device
                    # closed it itself, cleanly).
                    try:
                        await self.connect()
                    except Exception as exc:
                        _LOGGER.error(
                            "Raylogic %s %s: error during fast reconnect: %s",
                            self._model_name, self.ip, exc,
                        )
                    fast = False
                    continue
                # STABILITY FIX (v1.5.0): a short random settle gap before
                # reconnecting. Real logs showed "Failed to connect ... the
                # peer closed the connection (EOF)" 5 times - i.e. the module
                # accepted the TCP connection but closed it immediately,
                # because its old socket had not been cleaned up yet. The
                # soft reconnect already had this gap, but this (genuine
                # failure) path did not. Being random also prevents 10
                # devices from stampeding at once.
                await asyncio.sleep(
                    random.uniform(RECONNECT_SETTLE_MIN, RECONNECT_SETTLE_MAX)
                )
                if self._shutdown:
                    return
                # connect() sets _connected itself (True on success) - the
                # loop condition re-checks it automatically and the loop
                # stops right here as soon as it succeeds. On failure no
                # exception should reach this point (_do_connect handles all
                # exceptions internally and returns False) - but an extra
                # safety net is kept so that no unexpected exception can
                # crash this whole retry loop (which would bring back the
                # same "permanently stuck" bug).
                try:
                    await self.connect()
                except Exception as exc:
                    _LOGGER.error(
                        "Raylogic %s %s: unexpected error during "
                        "reconnect attempt (will retry in 30s): %s",
                        self._model_name, self.ip, exc,
                    )
        finally:
            self._reconnecting = False

    def _mark_unavailable_soon(self):
        """Do not show the entity as 'Unavailable' the moment a disconnect
        happens. Start a short UNAVAILABLE_GRACE_SECONDS grace window - if
        connect() succeeds again within that window (as happens with most
        short blips/resync hiccups), no state_callback fires at all - so the
        HA UI never shows an 'Unavailable' flicker. The entity is shown as
        Unavailable only for a genuine, long outage."""
        if self._unavailable_task and not self._unavailable_task.done():
            return  # a grace timer is already pending, do not start another
        self._unavailable_task = asyncio.create_task(self._unavailable_after_grace())

    async def _unavailable_after_grace(self):
        try:
            await asyncio.sleep(UNAVAILABLE_GRACE_SECONDS)
            if not self._connected and self.state_callback:
                self.state_callback(self.ip, None, {"available": False})
        except asyncio.CancelledError:
            pass

    def _cancel_unavailable_grace(self):
        """Called as soon as connect() succeeds - if the grace timer was
        still pending (i.e. 'Unavailable' had not been shown in the UI yet),
        cancel it so it does not fire late and wrongly show an
        already-reconnected device as unavailable."""
        if self._unavailable_task and not self._unavailable_task.done():
            self._unavailable_task.cancel()
        self._unavailable_task = None

    def _schedule_reconnect(self):
        """Whenever a reconnect is needed - read error, write error or
        resync failure - always go through this, so that only ONE reconnect
        loop runs at a time (2 TCP connections opened to the device at once
        confused the device itself and it hung; HA had to be reloaded).

        BUG FIX: previously `_reconnecting = True` was only set INSIDE the
        `_reconnect()` coroutine - but a newly created asyncio task does not
        run immediately (it waits until the event loop gets a turn). If
        `_schedule_reconnect()` was called again within the same
        synchronous stack (in quick succession, e.g. in a connect-failure
        cascade), the old task had not even started yet - the flag still
        read False, and DUPLICATE reconnect tasks were created. The flag is
        now set right here, synchronously, BEFORE the task is created."""
        if self._shutdown:
            return
        if not self._reconnecting:
            self._reconnecting = True
            self._reconnect_task = asyncio.create_task(self._reconnect())

    def _trigger_on_demand_connect(self):
        """The user toggled an entity, or a scene was triggered, while the
        device was disconnected - for this we should not wait for the
        background `_reconnect()` loop's own backoff schedule (which, in a
        genuine outage, can grow from 1 s up to 15 s/30 s), otherwise the
        command could stay "queued" on a minute scale.

        This function immediately fires a separate best-effort `connect()`
        attempt (a fire-and-forget task) - this is safe because:
          - `connect()` itself runs inside `_connect_lock`, so this and the
            background `_reconnect()` loop will never open two TCP
            connections at the same time (whichever takes the lock first
            wins; the other then sees `_connected` already True and becomes
            a no-op immediately).
          - `_do_connect()` is also gated by the module-level
            `_connect_semaphore`, so even in a 100+ device installation many
            simultaneous on-demand attempts do not create a "thundering
            herd" - the same existing global concurrency limit applies
            here too.
        If this attempt fails (the device really is down right now), that
        is no problem - the background `_reconnect()` loop is already
        running (it was started by `_schedule_reconnect()` when the
        connection broke) and will continue its normal backoff retry.
        """
        if self._connected or self._shutdown:
            return
        if self._on_demand_task and not self._on_demand_task.done():
            return  # an on-demand attempt is already pending, do not start another
        self._on_demand_task = asyncio.create_task(self._on_demand_connect())

    async def _on_demand_connect(self):
        try:
            await self.connect()
        except Exception as exc:
            # connect()/_do_connect() handle all exceptions internally and
            # return False - but an extra safety net is kept so that this
            # fire-and-forget task never produces an unhandled warning such
            # as "Task exception was never retrieved". The background
            # _reconnect() loop will keep retrying.
            _LOGGER.debug(
                "Raylogic %s: on-demand reconnect attempt failed (the normal "
                "background retry loop keeps running): %s", self.ip, exc,
            )

    async def _resync_loop(self, gen: int):
        """Closes and reopens the connection every RESYNC_INTERVAL seconds -
        the same effect as reopening the App, so that any change made from
        the Raylogic App (which is not live-broadcast) is reflected in HA
        within a few seconds. Commands sent from HA (which are applied
        optimistically and instantly) are not disturbed by this.

        SCALE FIX: previously the first sleep was also exactly
        RESYNC_INTERVAL (a fixed 45 s) - which meant that if many devices
        (50-100+) were added/connected shortly after HA startup, their
        resync cycles became EXACTLY synchronized, and every ~45 s ALL
        devices closed and reopened their TCP connections AT ONCE
        ("thundering herd") - on a small host (such as a Raspberry Pi) this
        could cause a CPU/network spike that made HA unresponsive for a
        while. So the first cycle's wait is RANDOM (between 0 and
        RESYNC_INTERVAL, different per device) so that the load of 100+
        devices is spread across the whole 45-second window instead of a
        single moment.

        BUG FIX (repeated-EOF-storm bug): previously this "random first
        time" wait was drawn fresh EVERY time - because this function
        itself restarts as a NEW task after every new connect()/
        soft_reconnect() (see _do_connect), and the `random.uniform(...)`
        call was inside it. So the first cycle was fine, but EVERY resync
        AFTER it was random too - sometimes 25 s, but sometimes only 1-2
        seconds. Whenever that short wait came up, a connection that had
        just been established was deliberately closed and reopened only 1-2
        seconds later - which confused the device's fragile Wi-Fi/TCP stack
        into sending an EOF (the "the peer closed the connection (EOF)"
        that kept appearing in the log was caused by this). The random
        stagger should only happen in this device instance's FIRST resync
        cycle (HA startup jitter); every cycle after that should be the
        full, fixed RESYNC_INTERVAL - `self._resync_staggered`
        (instance-level, persisting across all reconnects) now guarantees
        exactly that."""
        if not RESYNC_INTERVAL:
            # v1.5.4: resync is disabled (the reason is documented in
            # const.py) - the device pushes its own state live, and the
            # connection is now stable, so deliberately breaking the
            # connection has no benefit, only downtime.
            return
        if not self._resync_staggered:
            self._resync_staggered = True
            wait = random.uniform(0, RESYNC_INTERVAL)
        else:
            # A little jitter on every cycle, so that 10+ devices do not
            # synchronize their resyncs to the same moment and stampede.
            wait = RESYNC_INTERVAL * random.uniform(0.85, 1.15)
        while self._connected and not self._resyncing and gen == self._conn_generation:
            await asyncio.sleep(wait)
            # STABILITY FIX (v1.5.0): this generation check is the missing
            # guard that caused old resync tasks (which stayed alive even
            # after a reconnect) to break the connection over and over. The
            # old session's task now simply ends quietly here.
            if not self._connected or self._resyncing or gen != self._conn_generation:
                return
            _LOGGER.debug(
                "Raylogic %s: periodic resync (a soft reconnect, like "
                "reopening the App) so that changes made in the App are synced too.",
                self.ip,
            )
            await self._soft_reconnect()
            return  # the new connect() starts its own fresh resync loop

    async def _soft_reconnect(self):
        """Close the old socket and call connect() again immediately - this
        time WITHOUT firing an 'available: False' event (the gap is very
        short; the HA UI should not flicker unless the reconnect really
        fails)."""
        self._resyncing = True
        for task in (self._listen_task, self._ka_task):
            if task and task is not asyncio.current_task():
                task.cancel()
        await self._close_writer_safe()
        self._connected = False
        self._resyncing = False
        # STABILITY FIX: when a new connect() was attempted IMMEDIATELY
        # after closing the old socket, many devices rejected/EOF'd the new
        # connection before they had cleaned up their old socket. A short
        # "breathing" gap (random per device, so that devices do not get
        # synchronized) gives the device time to clean up.
        await asyncio.sleep(random.uniform(SOFT_RECONNECT_SETTLE_MIN, SOFT_RECONNECT_SETTLE_MAX))
        ok = await self.connect()
        if not ok:
            _LOGGER.warning(
                "Raylogic %s: periodic resync failed, the normal reconnect "
                "cycle will take over.", self.ip,
            )
            self._mark_unavailable_soon()
            self._schedule_reconnect()

    # ------------------------------------------------------------------ #
    # I/O
    # ------------------------------------------------------------------ #
    def _next_msg(self) -> str:
        self._msg_counter = (self._msg_counter % 999) + 1
        return f"{self._msg_counter:03d}"

    # ---------------- P1 (v1.6.7): confirmed-state tracking ---------------- #
    def _cmd_delivered(self, cmd: str) -> None:
        """The module received the command - drop that channel's pre-state
        snapshot, but only when no other command for that channel is still
        in flight."""
        ch = self._cmd_channel.pop(cmd, None)
        if ch is not None and ch not in self._cmd_channel.values():
            self._pre_state.pop(ch, None)

    def _cmd_dropped(self, cmd: str, reason: str) -> None:
        """The command will never be delivered - remove the "false success":
        restore the channel to its last confirmed state and update the HA
        entity."""
        ch = self._cmd_channel.pop(cmd, None)
        if ch is None or ch in self._cmd_channel.values():
            return
        prev = self._pre_state.pop(ch, None)
        if prev is None:
            return
        self.channel_states[ch] = dict(prev)
        _LOGGER.warning(
            "Raylogic %s %s: the command for channel %d did not reach the "
            "device (%s) - the state in HA has been restored to the real "
            "(last confirmed) state, so no 'false success' is shown.",
            self._model_name, self.ip, ch, reason,
        )
        if self.state_callback:
            self.state_callback(self.ip, ch, self.channel_states[ch])

    def _expire_pending(self) -> None:
        """Drop + revert queued commands older than PENDING_COMMAND_MAX_AGE.
        This also runs while the module is offline (from the reconnect
        loop), so HA shows the truth within ~30 s instead of waiting for the
        module to come back."""
        now = self._now()
        keep = []
        for p in self._pending_commands:
            if (now - p["at"]) > PENDING_COMMAND_MAX_AGE:
                self._cmd_dropped(p["cmd"], f"module was offline for {PENDING_COMMAND_MAX_AGE}s")
            else:
                keep.append(p)
        self._pending_commands = keep

    def _queue_command(self, cmd: str, attempt: int = 0) -> None:
        """Put a command into the replay queue (bounded + timestamped).

        BUG FIX (v1.5.4, caught by the stress test): putting a command into
        the queue is only useful if something also FLUSHES it - and the
        flush only happens inside a successful connect(). Previously some
        paths (such as the "connection is down" branch of `_send_raw`) only
        called `_trigger_on_demand_connect()`, which makes a single
        best-effort attempt and goes quiet if it fails. If the background
        `_reconnect()` loop was not running at that moment either (e.g. it
        had just exited after a successful connect), the command sat in the
        queue forever - the user's click was wasted, with no error. In the
        mild-chaos stress test, 5 out of 500 commands were lost this way.

        Every queue add now also guarantees that the reconnect loop is
        running. `_schedule_reconnect()` is idempotent (it returns
        immediately if one is already running)."""
        self._pending_commands.append(
            {"cmd": cmd, "at": self._now(), "attempt": attempt}
        )
        if len(self._pending_commands) > self._MAX_PENDING_COMMANDS:
            old = self._pending_commands.pop(0)
            self._cmd_dropped(old["cmd"], "queue full")
        if not self._connected and not self._shutdown:
            self._schedule_reconnect()

    async def _resend_unconfirmed(self, cmd: str, attempt: int) -> None:
        """Delivery of the command to the device was NOT confirmed - send
        it again.

        BUG FIX (v1.5.4, caught by the stress test): previously, if a
        reconnect happened in the middle of verification (the generation
        changed), _verify_delivery returned silently - assuming "the
        reconnect path will take care of it". But the command written on
        that old socket was not in any queue, so it was lost FOREVER. In
        the stress test, 89 out of 500 commands disappeared this way (queue
        empty, delivery stuck at 411).

        Such a command is now sent again on the new connection. All
        commands are idempotent (absolute levels, not toggles), so resending
        is safe. The COMMAND_MAX_ATTEMPTS cap prevents infinite retries."""
        if self._shutdown:
            return
        if attempt + 1 >= COMMAND_MAX_ATTEMPTS:
            self._cmd_dropped(cmd, f"{COMMAND_MAX_ATTEMPTS} attempts failed")
            _LOGGER.error(
                "Raylogic %s %s: the command could not reach the device even "
                "after %d attempts - giving up: '%s'",
                self._model_name, self.ip, COMMAND_MAX_ATTEMPTS, cmd,
            )
            return
        _LOGGER.info(
            "Raylogic %s: delivery of the command was not confirmed (the "
            "connection changed) - resending it on the new connection "
            "(attempt %d/%d): '%s'",
            self.ip, attempt + 2, COMMAND_MAX_ATTEMPTS, cmd,
        )
        await self._send_raw(
            cmd, queue_on_disconnect=True, verify=True, attempt=attempt + 1,
        )

    @staticmethod
    def _unacked_bytes(sock) -> Optional[int]:
        """How many bytes in the socket's send queue have not yet been
        ACKed by the device's TCP stack. 0 = everything reached the device.
        None = this check is not available on this platform (Linux only)."""
        if fcntl is None or sock is None:
            return None
        try:
            raw = fcntl.ioctl(sock.fileno(), _SIOCOUTQ, struct.pack("I", 0))
            return struct.unpack("I", raw)[0]
        except (OSError, ValueError, AttributeError):
            return None

    async def _verify_delivery(
        self, cmd: str, gen: int, sock, attempt: int = 0,
    ) -> None:
        """Confirm in the background that the command really reached the
        device. The device itself sends no ACK, but its TCP stack does -
        SIOCOUTQ reaching 0 means the data was received.

        This does not block the entity click (it runs in a separate task),
        so the UI response stays just as immediate."""
        try:
            await self._verify_delivery_inner(cmd, gen, sock, attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # safety net - the background task must
            _LOGGER.debug(      # never raise an unhandled exception
                "Raylogic %s: error during delivery verification (ignored): %s",
                self.ip, exc,
            )

    async def _verify_delivery_inner(
        self, cmd: str, gen: int, sock, attempt: int = 0,
    ) -> None:
        deadline = self._now() + DELIVERY_VERIFY_TIMEOUT
        last_pending = None
        while self._now() < deadline:
            if gen != self._conn_generation:
                # A reconnect has happened and delivery of this command was
                # NOT confirmed - the data written on the old socket was
                # probably lost. Send it again on the new connection.
                await self._resend_unconfirmed(cmd, attempt)
                return
            pending = self._unacked_bytes(sock)
            if pending is None:
                self._cmd_delivered(cmd)
                return  # not supported on this platform, skip verification
            if pending == 0:
                self._cmd_delivered(cmd)
                _LOGGER.debug(
                    "Raylogic %s: command reached the device (TCP ACK): %s",
                    self.ip, cmd,
                )
                return
            # FALSE-POSITIVE GUARD: if the queue is shrinking, data IS being
            # sent - the link is just a little slow/busy (possible in a
            # 100+ device setup). In that case push the deadline out a bit;
            # only consider it "dead" when there is no progress at all.
            if last_pending is not None and pending < last_pending:
                deadline = self._now() + DELIVERY_VERIFY_TIMEOUT
            last_pending = pending
            await asyncio.sleep(DELIVERY_VERIFY_POLL)

        if self._shutdown:
            return
        if gen != self._conn_generation:
            await self._resend_unconfirmed(cmd, attempt)
            return
        # Timeout - the device did not ACK the data even at the TCP level,
        # i.e. the socket really is dead. This is exactly the case in which
        # the command used to be lost silently and the user had to click
        # again.
        _LOGGER.warning(
            "Raylogic %s %s: the command did not reach the device within "
            "%.1fs (no TCP ACK) - treating the socket as dead, reconnecting "
            "and RESENDING the command AUTOMATICALLY; there is no need to "
            "click again: '%s'",
            self._model_name, self.ip, DELIVERY_VERIFY_TIMEOUT, cmd,
        )
        self._connected = False
        self._queue_command(cmd, attempt)
        self._mark_unavailable_soon()
        self._schedule_reconnect()
        self._trigger_on_demand_connect()

    async def _send_raw(
        self, cmd: str, queue_on_disconnect: bool = False,
        verify: bool = False, attempt: int = 0,
    ):
        # PRE-FLIGHT CHECK (v1.5.2): the device normally sends its own frame
        # every 6-12 s. If there has been complete silence for longer than
        # LINK_SUSPECT_SECONDS, the socket is very likely dead - rather than
        # losing the command by writing to it, queue it and refresh the
        # connection first. (The listen loop also does its own
        # RX_SILENCE_TIMEOUT check, but that triggers a bit later - the
        # user's click should not wait for it.)
        if (
            verify
            and self._connected
            and self._last_rx
            and (self._now() - self._last_rx) > LINK_SUSPECT_SECONDS
        ):
            _LOGGER.warning(
                "Raylogic %s %s: the device has been silent for %.0fs - "
                "refreshing the connection before sending the command (the "
                "command is safe in the queue and will be sent as soon as "
                "the connection is re-established).",
                self._model_name, self.ip, self._now() - self._last_rx,
            )
            self._connected = False
            self._schedule_reconnect()

        if not self._connected or not self._writer:
            if queue_on_disconnect:
                # Bounded queue (max N) so that memory does not build up
                # needlessly if the user toggles very quickly.
                self._queue_command(cmd, attempt)
                _LOGGER.info(
                    "Raylogic %s: the command could not be sent right now "
                    "(connection down) - it has been queued and will be sent "
                    "automatically once reconnected: '%s'", self.ip, cmd,
                )
                # ON-DEMAND FIX: do not just queue and wait for the
                # background backoff cycle - this is a REAL control command
                # (the user toggled something or a scene was triggered), so
                # also fire a connect() attempt immediately so the command is
                # applied as soon as possible.
                self._trigger_on_demand_connect()
            else:
                _LOGGER.warning(
                    "Raylogic %s: command DROPPED because the connection is "
                    "not active right now (connected=%s) - '%s' could not be "
                    "sent. The connection to the device is probably being "
                    "re-established (reconnect cycle) - try again shortly.",
                    self.ip, self._connected, cmd,
                )
            return
        try:
            # Write + drain now happen inside _send_lock, so that
            # _close_writer_safe() cannot close the socket in between and
            # truncate the command we have just written.
            async with self._send_lock:
                if not self._connected or not self._writer:
                    self._queue_command(cmd, attempt)
                    self._trigger_on_demand_connect()
                    return
                writer = self._writer
                gen = self._conn_generation
                sock = writer.get_extra_info("socket")
                writer.write((cmd + "\r").encode())
                await asyncio.wait_for(writer.drain(), timeout=float(WRITE_TIMEOUT))
                self._last_tx = self._now()
            _LOGGER.debug("TX %s: %s", self.ip, cmd)
            if verify:
                # Confirm delivery in the background - without blocking the
                # UI. Keeping a reference to the task is essential: without
                # a strong reference asyncio may garbage-collect the task
                # (Python's well-known fire-and-forget pitfall) - and the
                # verification would silently disappear.
                task = asyncio.create_task(
                    self._verify_delivery(cmd, gen, sock, attempt)
                )
                self._verify_tasks.add(task)
                task.add_done_callback(self._verify_tasks.discard)
        except asyncio.TimeoutError:
            _LOGGER.error(
                "Raylogic %s: command send timed out after %ss (socket "
                "half-dead / device not ACKing) - marking disconnected "
                "and reconnecting.", self.ip, WRITE_TIMEOUT,
            )
            self._connected = False
            if verify:
                # v1.5.2: previously the command was simply lost here. It is
                # now queued and replayed automatically after reconnecting.
                self._queue_command(cmd, attempt)
                self._trigger_on_demand_connect()
            self._schedule_reconnect()
        except Exception as exc:
            _LOGGER.error("Send error to Raylogic %s: %s", self.ip, exc)
            self._connected = False
            if verify:
                self._queue_command(cmd, attempt)
                self._trigger_on_demand_connect()
            self._schedule_reconnect()

    async def _send_addressed(self, cmd: str, ch: Optional[int] = None):
        """CONFIRMED from real Docklight capture (device connected DIRECTLY,
        192.168.1.34:5550): wire traffic ALWAYS carries a "<id>,<seq>,"
        prefix before *AR=/+AR40= - the official PDF's bare "*AR=...\\r"
        examples are only the logical payload, not the real wire format.

        Two mistakes were made earlier:
          1. The prefix was removed entirely (based on the PDF examples) -
             wrong, real traffic does carry the prefix.
          2. The device's own broadcast id (which appears as e.g. "109" in
             *KA=/+AR40= lines) was taken to be our sender id - wrong, that
             is the device/hub's OWN identity, not ours. Real working client
             commands (such as "099,155,*AR=001A040203") use a DIFFERENT id
             - that is CLIENT_SENDER_ID.
        """
        full = f"{CLIENT_SENDER_ID},{self._next_msg()},{cmd}"
        if ch is not None:
            # P1: the state BEFORE the command = the last confirmed state
            # (unless an undelivered command already exists; in that case
            # the older snapshot is kept)
            if ch not in self._pre_state:
                self._pre_state[ch] = dict(self.channel_states.get(ch, {}))
            self._cmd_channel[full] = ch
        await self._send_raw(
            full,
            queue_on_disconnect=True,
            # v1.5.2: this is a REAL user command (entity click / scene) -
            # confirm its delivery via TCP ACK and resend it automatically
            # if it does not arrive. The keepalive is sent without this flag.
            verify=True,
        )

    async def _read_line(self, timeout: float = 2.0) -> Optional[str]:
        try:
            line = await asyncio.wait_for(self._read_frame(), timeout=timeout)
            # Anything received from the device = the connection is
            # definitely alive. Both the passive keepalive and the passive
            # dead-connection detection rely on this timestamp (see
            # _keepalive_loop / _listen_loop).
            self._last_rx = self._now()
            return line
        except asyncio.TimeoutError:
            return None
        except asyncio.IncompleteReadError as exc:
            # ROOT CAUSE (the webpage/HA UI hung when a second device was
            # added or when any device closed TCP from its side): this used
            # to be treated as "harmless", just like a timeout (it simply
            # returned None and never touched self._connected). After EOF,
            # readuntil() ALWAYS raises IncompleteReadError immediately
            # (zero delay, without any wait) - so _listen_loop's
            # `while self._connected:` loop saw the None and tried the next
            # read straight away, which hit EOF immediately again, creating
            # a zero-delay tight loop that CPU-spun and blocked the whole
            # HA event loop (not just this device - the entire HA webpage/UI
            # hung until a restart). EOF is now treated as a real disconnect
            # (ConnectionError is raised) so the caller immediately moves
            # to the disconnected state and the normal 30 s reconnect cycle
            # is triggered - a tight loop is no longer possible.
            raise ConnectionError(
                f"Raylogic {self.ip}: the peer closed the connection (EOF)"
            ) from exc
        except Exception as exc:
            _LOGGER.error("Read error from Raylogic %s: %s", self.ip, exc)
            self._connected = False
            self._schedule_reconnect()
            return None

    async def _read_frame(self) -> str:
        """P2 (v1.6.4): the next non-empty line - "\r", "\n" and "\r\n" all
        work. Previously there was only readuntil(b"\r"): not a single frame
        from "\n"-only firmware was parsed (confirmed in the simulator - a
        reconnect every ~80 s, the device useless). The buffer lives on the
        instance, so no data is lost even if _read_line's timeout cancels in
        the middle. EOF -> IncompleteReadError (same EOF handling as
        before), 64 KiB without a terminator -> ValueError (the old "Read
        error" -> reconnect)."""
        while True:
            m = _LINE_END_RE.search(self._rx_buf)
            if m:
                raw = self._rx_buf[:m.start()]
                self._rx_buf = self._rx_buf[m.end():]
                if len(raw) > _RX_LINE_LIMIT:
                    raise ValueError("Separator is found, but chunk is longer than limit")
                text = raw.decode(errors="replace").strip()
                if text:
                    return text
                continue    # the empty line between "\r\n"
            if len(self._rx_buf) > _RX_LINE_LIMIT:
                self._rx_buf = b""
                raise ValueError("Line longer than limit without terminator")
            chunk = await self._reader.read(4096)
            if not chunk:
                raise asyncio.IncompleteReadError(self._rx_buf, None)
            self._rx_buf += chunk

    def _log_once(self, key, level: int, msg: str, *args) -> None:
        """P3/P4/P5: do not log the same thing for every frame - only once."""
        if key in self._logged_once:
            return
        self._logged_once.add(key)
        _LOGGER.log(level, msg, *args)

    async def _drain_initial_push(self):
        """Whatever initial burst the device sends as soon as a new
        connection is established (the same probably happens when the App
        connects, which is why the App gets the correct status on reopen) -
        previously we only read the FIRST line and discarded the rest. Now
        every line that arrives within a short time (2.5 s) is processed by
        _dispatch_line - if it contains per-channel *AR= state, that is now
        reflected in channel_states (state_callback fires as well, so HA
        entities update immediately)."""
        # SCALE FIX (v1.5.2): previously each read's timeout was the whole
        # remaining window (up to 2.5 s) - which meant every device sat for
        # about 2.5 seconds on connect even after its burst had ended. In a
        # 100+ device setup (where connects run in batches via the
        # semaphore) this directly turned into a minute-scale delay at HA
        # startup. The end of the burst is now detected by a SHORT quiet
        # gap (INITIAL_QUIET), and the overall cap is still 2.5 s - the data
        # received stays exactly the same, just without the needless wait.
        end_time = asyncio.get_event_loop().time() + INITIAL_DRAIN_MAX
        first = True
        while asyncio.get_event_loop().time() < end_time:
            remaining = max(0.05, end_time - asyncio.get_event_loop().time())
            line = await self._read_line(
                timeout=min(INITIAL_DRAIN_QUIET, remaining)
            )
            if not line:
                break
            if first and "*KA=" in line:
                self._handle_ka_line(line)
            else:
                self._dispatch_line(line)
            first = False

    def _handle_ka_line(self, line: str):
        """The device/hub sends the *KA= line ITSELF to broadcast its own
        identity (e.g. "109,*KA=31-...") - this is NOT our sender id; it is
        only stored for reference/logging. Outgoing commands use
        CLIENT_SENDER_ID (confirmed "099")."""
        try:
            candidate = line.split(",")[0].strip()
            if candidate.isdigit():
                self.node_id = candidate
        except Exception:
            pass
        self._parse_ka_identity(line)
        # v1.5.3: reply to the device's keepalive IMMEDIATELY - otherwise it
        # drops the connection after 2 KAs (~12 s). Fire-and-forget, because
        # _handle_ka_line is called from a sync context.
        if self._connected and not self._shutdown:
            task = asyncio.create_task(self._answer_device_keepalive())
            self._verify_tasks.add(task)
            task.add_done_callback(self._verify_tasks.discard)

    def _parse_ka_identity(self, line: str) -> None:
        """In the *KA= payload the device reports its own AREA and its own
        CHANNEL RANGE. Decoding (verified against the logs of 10 real
        devices):

            *KA=<xx>-<ctr:3><n:1><AREA:2h><01><0><START:2h><END:2h>0000

        Examples:
            *KA=21-05421001001020000 -> area 0x10=16, ch 0x01-0x02 (1-2)
            *KA=11-04710C0100D100000 -> area 0x0C=12, ch 0x0D-0x10 (13-16)
            *KA=11-04820801017180000 -> area 0x08= 8, ch 0x17-0x18 (23-24)

        We do NOT use this to CHANGE the config (devices that are already
        working correctly must not be touched) - we only VERIFY it and emit
        a clear warning. If a wrong Area or First Channel Number was entered
        when adding a new device, the log now shows it immediately, with the
        exact correct value - no guessing needed."""
        try:
            idx = line.find("*KA=")
            if idx == -1:
                return
            payload = line[idx + 4:].strip()
            if "-" not in payload:
                return
            body = payload.split("-", 1)[1]
            if len(body) < 17 or body[6:8] != "01":
                return
            area = int(body[4:6], 16)
            start = int(body[9:11], 16)
            end = int(body[11:13], 16)
        except (ValueError, IndexError):
            return

        if not (AREA_MIN <= area <= AREA_MAX):
            return
        if not (1 <= start <= end <= 255):
            return

        self.detected_area = area
        self.detected_channel_start = start
        self.detected_channel_end = end

        if self._config_mismatch_logged:
            return
        if not self._legacy_area or self._legacy_area <= 0:
            return  # LEARN mode - nothing to verify here

        problems = []
        if area != self._legacy_area:
            problems.append(
                f"Area: the config says {self._legacy_area}, the device "
                f"reports {area}"
            )
        if start != self._channel_start:
            problems.append(
                f"First Channel Number: the config says {self._channel_start}, "
                f"the device reports {start}"
            )
        detected_count = end - start + 1
        if detected_count != self._legacy_channel_count:
            problems.append(
                f"Channel count: the config (model {self._model_name}) "
                f"assumes {self._legacy_channel_count} channels, the device "
                f"reports {detected_count} ({start}-{end}) - the wrong "
                f"Device Model may have been selected"
            )
        if problems:
            self._config_mismatch_logged = True
            _LOGGER.warning(
                "Raylogic %s %s: CONFIG MISMATCH - the device reports "
                "something different about itself. %s. Open HA -> Settings "
                "-> Devices -> 'Configure' on this device and enter the "
                "correct values, otherwise commands will go to the wrong "
                "address and the device will silently ignore them.",
                self._model_name, self.ip, "; ".join(problems),
            )
        else:
            _LOGGER.debug(
                "Raylogic %s: config matches the device's self-report "
                "(area=%d, channels %d-%d).",
                self.ip, area, start, end,
            )

    def _setup_legacy_channels(self):
        """If the user entered an Area manually in config_flow (0 means
        'auto/learn', Relay only), create the channels right away - each
        channel with the type selected in the config (relay/dimmer/fan/
        curtain). Otherwise (LEARN mode) nothing is created until a real
        *AR= frame arrives (the channel has to be toggled once from the
        app/switch) - LEARN only works for Relay channels.

        NOTE: this function also runs after a periodic resync
        (_soft_reconnect) - so if a channel ALREADY exists (from the
        previous session), its on/brightness/percentage state is kept
        as-is and only area/type are refreshed. Otherwise every resync
        would make the light/switch wrongly flicker OFF in HA (even though
        the device had not actually changed - the fresh *AR= sent by
        _drain_initial_push right after the new connect() provides the
        real update)."""
        if self._legacy_area and self._legacy_area > 0:
            area = max(AREA_MIN, min(AREA_MAX, self._legacy_area))
            start = self._channel_start
            for ch_num in range(start, start + self._legacy_channel_count):
                # NOTE: CTC and Curtain are both PAIRED modes - when one
                # channel of a pair becomes one of these, the other
                # physical channel deliberately has no key in this dict
                # (see _resolve_channel_types in __init__.py), so that no
                # separate/duplicate entity is created for it.
                if ch_num not in self._channel_types:
                    continue
                ch_type = self._channel_types.get(ch_num, CH_TYPE_RELAY)
                existing = self.channel_states.get(ch_num, {})
                self.channel_states[ch_num] = {
                    "area": area,
                    "type": ch_type,
                    "on": existing.get("on", False),
                    "brightness": existing.get("brightness", 0),
                    "percentage": existing.get("percentage", 0),
                    "ctc_mode": self._channel_ctc_modes.get(ch_num, CTC_MODE_SINGLE),
                    "color_temp_kelvin": existing.get("color_temp_kelvin", CTC_DEFAULT_KELVIN),
                    "learned": False,
                }
            _LOGGER.info(
                "Raylogic %s: manual mode - area=%d, channels %d-%d "
                "ready (types: %s).", self.ip, area, start,
                start + self._legacy_channel_count - 1,
                {k: v.get("type") for k, v in self.channel_states.items()},
            )
        else:
            # LEARN mode: restore the Relay channels learned in the previous
            # session immediately (from disk); new channels will come from
            # *AR= frames. Only the relay type works here.
            for ch_num, area in self._learned.items():
                existing = self.channel_states.get(ch_num, {})
                self.channel_states[ch_num] = {
                    "area": area,
                    "type": CH_TYPE_RELAY,
                    "on": existing.get("on", False),
                    "learned": True,
                }
            _LOGGER.info(
                "Raylogic %s: LEARN mode - %d channel(s) restored from "
                "previous session. For a new channel, switch that channel "
                "ON/OFF once from the Raylogic GO app or a physical switch - "
                "HA will detect it and create the entity automatically.",
                self.ip, len(self._learned),
            )

    # ------------------------------------------------------------------ #
    # Learned-channel persistence
    # ------------------------------------------------------------------ #
    def _load_learned(self) -> dict[int, int]:
        # P6: if the file is not in the new location but exists in the old
        # one (integration folder) -> copy it over once. The old file is
        # left in place (a HACS update removes it anyway), not deleted.
        source = self._state_file
        if not source.exists() and self._legacy_state_file.exists():
            source = self._legacy_state_file
        try:
            data = json.loads(source.read_text())
            learned = {int(k): int(v) for k, v in data.items()}
        except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError):
            return {}
        if source is not self._state_file:
            _LOGGER.info(
                "Raylogic %s: moved learned channels from the old location "
                "to %s (they will no longer be wiped by a HACS update).",
                self.ip, self._state_file,
            )
            self._write_learned_file({str(k): v for k, v in learned.items()})
        return learned

    def _write_learned_file(self, snapshot: dict) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(snapshot))
        except OSError as err:
            _LOGGER.warning("Raylogic %s: failed to save learned state: %s", self.ip, err)

    def _save_learned(self) -> None:
        # BUG FIX: previously a synchronous write_text() happened right
        # here, called from _handle_ar() (the listen_loop task), i.e. from
        # HA's event-loop thread itself - on a slow disk the whole of HA
        # could freeze for a while. It is now written in a background
        # thread (fire-and-forget), so the event loop is never blocked.
        snapshot = {str(k): v for k, v in self._learned.items()}
        asyncio.create_task(asyncio.to_thread(self._write_learned_file, snapshot))

    def _learn_channel(self, ch_num: int, area: int) -> bool:
        """Record a new channel. Returns True if this channel was seen for
        the first time (i.e. an entity should be created)."""
        is_new = ch_num not in self.channel_states
        if is_new or self.channel_states[ch_num].get("area") != area:
            self.channel_states[ch_num] = {
                "area": area, "type": CH_TYPE_RELAY, "on": False, "learned": True,
            }
            self._learned[ch_num] = area
            self._save_learned()
            _LOGGER.info(
                "Raylogic %s: LEARNED new channel %d in area %d "
                "(from a real *AR= frame).", self.ip, ch_num, area,
            )
        return is_new

    # ------------------------------------------------------------------ #
    # Control - Relay (CONFIRMED format, works in both auto & legacy mode)
    # ------------------------------------------------------------------ #
    async def set_relay(self, ch_num: int, on: bool):
        state = self.channel_states.get(ch_num, {})
        area = state.get("area")
        if not area:
            _LOGGER.error(
                "Raylogic %s: the Area of channel %d is not known yet "
                "(LEARN mode, and this channel has not been learned yet) "
                "- the command cannot be sent. First switch this channel "
                "ON/OFF once from the Raylogic GO app or a physical switch.",
                self.ip, ch_num,
            )
            return
        level = RELAY_LEVEL_ON if on else RELAY_LEVEL_OFF
        cmd_hex = f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}{level}{ch_num:02X}"
        await self._send_addressed(f"*AR={cmd_hex}", ch=ch_num)
        self.channel_states.setdefault(ch_num, {}).update({"on": on})
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Control - Dimmer (CONFIRMED format, Model_Number_Mod2u.txt capture)
    #   Frame: 00 1A <area> <level> <channel>
    #   level: 0x01 = full brightness, 0xFF = off, in-between = dim curve.
    #   Linear approximation: brightness (0-255) -> level.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _snap_to_off(brightness: Optional[int]) -> int:
        """User-requested behaviour: when the HA slider is all the way down
        (1%), turn the light fully OFF instead of leaving it "on but barely
        lit". HA's brightness scale is 0-255, and 1% is about 2-3 - this
        whole near-zero range is treated as 0 (= OFF). Returns 0 if the
        light should effectively be off, otherwise the original
        brightness."""
        if not brightness:
            return 0
        if round(brightness * 100 / 255) <= 1:
            return 0
        return brightness

    async def set_dimmer(self, ch_num: int, brightness: Optional[int]):
        """brightness: 0-255 (HA scale), or None/0 = off."""
        state = self.channel_states.get(ch_num, {})
        area = state.get("area")
        if not area:
            _LOGGER.error(
                "Raylogic %s: the Area of channel %d is not known - "
                "the dimmer command cannot be sent.", self.ip, ch_num,
            )
            return
        brightness = self._snap_to_off(brightness)
        if not brightness:
            level = DIMMER_LEVEL_OFF
        else:
            brightness = max(1, min(255, brightness))
            # 255 (full) -> level 0x01; the level increases as it dims.
            # Only go up to 254 - 255 (0xFF) is reserved for OFF, otherwise
            # the dimmest "on" brightness would accidentally become an OFF
            # command.
            level = max(DIMMER_LEVEL_ON, min(254, 256 - brightness))
        cmd_hex = f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}{level:02X}{ch_num:02X}"
        await self._send_addressed(f"*AR={cmd_hex}", ch=ch_num)
        self.channel_states.setdefault(ch_num, {}).update(
            {"on": bool(brightness), "brightness": brightness or 0}
        )
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Control - CTC (Colour Temperature Control / tunable white)
    # See the CTC comment block in const.py for the full wire-format
    # explanation of both sub-modes (single/double driver).
    # ------------------------------------------------------------------ #
    def _pair_bounds(self, ch_num: int) -> tuple[int, int]:
        """Which PAIR this channel (ch_num) belongs to (the 2 pairs of a
        MOD4U: channel_start/+1 and channel_start+2/+3) - returns the (lo,
        hi) physical channel numbers of that pair.

        BUG FIX (MOD2U -> MOD4U generalization): previously CTC always
        assumed a HARDCODED (channel_start, channel_start+1) - which was
        fine on the MOD2U because it only has one pair, but on the MOD4U
        this BROKE CTC on the 2nd pair (channel_start+2/+3) (the 1st pair's
        channels were used, while the real hardware was something else).
        The pair is now derived from the configured channel's own position
        and works correctly for any pair."""
        offset = ch_num - self._channel_start
        pair_index = offset // CHANNELS_PER_PAIR
        lo = self._channel_start + pair_index * CHANNELS_PER_PAIR
        return lo, lo + 1

    def _ctc_single_wire_channels(self, ch_num: int) -> tuple[int, int]:
        """Single-driver CTC uses a physical channel PAIR (such as channel 3
        + channel 4), not a fixed 0x01/0x02 sub-signal id (as was wrongly
        assumed earlier, based on a single-channel capture only).
        User-confirmed rule: of the TWO physical channels of the pair, the
        lower number is colour temperature and the higher number is
        brightness.

        `ch_num` here is this CTC entity's own (primary/configured)
        physical channel number - its PAIR (not always the device's first
        pair) is derived from it, so that on a MOD4U both CTC Pair 1
        (channel_start/+1) and CTC Pair 2 (channel_start+2/+3) work
        independently, each with its own correct physical channels.

        Returns: (ct_channel, brightness_channel)."""
        lo, hi = self._pair_bounds(ch_num)
        return lo, hi

    async def set_ctc(
        self, ch_num: int,
        brightness: Optional[int] = None,
        color_temp_kelvin: Optional[int] = None,
    ):
        """brightness: 0-255 or None (unchanged). color_temp_kelvin: None
        (unchanged) or a Kelvin value from the HA slider. Only the params
        that are not None are actually changed - in single-driver mode these
        are two independent *AR= frames (exactly as in the capture); in
        double-driver mode both always go into one combined *AZ= frame
        (because in that format the warm/cool bytes are encoded together -
        they cannot be sent separately)."""
        state = self.channel_states.get(ch_num, {})
        area = state.get("area")
        if not area:
            _LOGGER.error(
                "Raylogic %s: the Area of channel %d (CTC) is not "
                "known - the command cannot be sent.", self.ip, ch_num,
            )
            return
        mode = state.get("ctc_mode", CTC_MODE_SINGLE)
        if brightness is not None:
            brightness = self._snap_to_off(brightness)

        if mode == CTC_MODE_DOUBLE:
            eff_brightness = (
                brightness if brightness is not None
                else state.get("brightness", 255)
            )
            eff_kelvin = (
                color_temp_kelvin if color_temp_kelvin is not None
                else state.get("color_temp_kelvin", CTC_DEFAULT_KELVIN)
            )
            await self._send_ctc_double(ch_num, area, eff_brightness, eff_kelvin)
        else:
            ct_channel, brightness_channel = self._ctc_single_wire_channels(ch_num)
            eff_brightness = state.get("brightness", 255)
            eff_kelvin = state.get("color_temp_kelvin", CTC_DEFAULT_KELVIN)
            if brightness is not None:
                eff_brightness = brightness
                level = (
                    CTC_SINGLE_BRIGHTNESS_OFF if not brightness
                    else max(CTC_SINGLE_BRIGHTNESS_ON, min(254, 256 - brightness))
                )
                cmd_hex = (
                    f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}"
                    f"{level:02X}{brightness_channel:02X}"
                )
                await self._send_addressed(f"*AR={cmd_hex}", ch=ch_num)
            if color_temp_kelvin is not None:
                eff_kelvin = color_temp_kelvin
                level = self._kelvin_to_single_ct_level(color_temp_kelvin)
                cmd_hex = (
                    f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}"
                    f"{level:02X}{ct_channel:02X}"
                )
                await self._send_addressed(f"*AR={cmd_hex}", ch=ch_num)

        self.channel_states.setdefault(ch_num, {}).update({
            "on": bool(eff_brightness),
            "brightness": eff_brightness or 0,
            "color_temp_kelvin": eff_kelvin,
        })
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    async def _send_ctc_double(
        self, ch_num: int, area: int, brightness: Optional[int], kelvin: int,
    ):
        """Double-driver frame - warm/cool cross-fade formula, derived to
        match both captured sweeps exactly (see const.py comment).

        Wire: <area><ch_lo><level_lo><ch_hi><level_hi><const><pct>

        BUG FIX (CCT/CTC did not work on any other channel): previously the
        channel bytes here were HARDCODED "01" and "02". The mistake went
        unnoticed because the MOD2U the capture was taken from had its CTC
        pair on physical channels 1 and 2. On the Area 12 MOD4U the CTC pair
        is on channels 15-16, so 0F/10 must go into the frame - sending
        01/02 made the device drop the frame, which is why the CCT light did
        not respond at all. Both channel bytes are now derived from this CTC
        channel's own pair.

        Wiring (confirmed by the user on real hardware): the LOWER physical
        channel of the pair = WHITE/cool driver, the HIGHER channel =
        YELLOW/warm driver - so cool goes into level_lo and warm into
        level_hi."""
        cool_channel, warm_channel = self._pair_bounds(ch_num)
        kelvin = max(CTC_MIN_KELVIN, min(CTC_MAX_KELVIN, kelvin))
        warm_frac = 1 - (kelvin - CTC_MIN_KELVIN) / (CTC_MAX_KELVIN - CTC_MIN_KELVIN)
        brightness = max(0, min(255, brightness or 0))
        bright_frac = brightness / 255
        warm_on = warm_frac * bright_frac
        cool_on = (1 - warm_frac) * bright_frac
        warm_level = 0xFF if warm_on <= 0 else max(1, min(255, round(256 - warm_on * 255)))
        cool_level = 0xFF if cool_on <= 0 else max(1, min(255, round(256 - cool_on * 255)))
        pct = round(bright_frac * 100)
        # lower physical channel = white/cool, higher = yellow/warm
        cmd_hex = (
            f"{area:02X}{cool_channel:02X}{cool_level:02X}"
            f"{warm_channel:02X}{warm_level:02X}"
            f"{CTC_DOUBLE_CONST_BYTE:02X}{pct:02X}"
        )
        _LOGGER.debug(
            "Raylogic %s: CTC(double) ch%d -> cool ch%d=0x%02X, warm "
            "ch%d=0x%02X, pct=%d (area %d) - *AZ=%s",
            self.ip, ch_num, cool_channel, cool_level, warm_channel,
            warm_level, pct, area, cmd_hex,
        )
        await self._send_addressed(f"*AZ={cmd_hex}", ch=ch_num)

    def _kelvin_to_single_ct_level(self, kelvin: int) -> int:
        kelvin = max(CTC_MIN_KELVIN, min(CTC_MAX_KELVIN, kelvin))
        frac = (kelvin - CTC_MIN_KELVIN) / (CTC_MAX_KELVIN - CTC_MIN_KELVIN)
        level = round(CTC_SINGLE_CT_MIN_LEVEL + frac * (CTC_SINGLE_CT_MAX_LEVEL - CTC_SINGLE_CT_MIN_LEVEL))
        return max(CTC_SINGLE_CT_MIN_LEVEL, min(CTC_SINGLE_CT_MAX_LEVEL, level))

    def _single_ct_level_to_kelvin(self, level: int) -> int:
        level = max(CTC_SINGLE_CT_MIN_LEVEL, min(CTC_SINGLE_CT_MAX_LEVEL, level))
        frac = (level - CTC_SINGLE_CT_MIN_LEVEL) / (CTC_SINGLE_CT_MAX_LEVEL - CTC_SINGLE_CT_MIN_LEVEL)
        return round(CTC_MIN_KELVIN + frac * (CTC_MAX_KELVIN - CTC_MIN_KELVIN))

    @staticmethod
    def _double_levels_to_kelvin(cool_level: int, warm_level: int) -> Optional[int]:
        """Colour temperature from the cool + warm level bytes of *AZ=
        (v1.6.5).

        The encoder (_send_ctc_double) scales each channel by both colour
        AND brightness: output = share x brightness, byte = 256 - output x
        255, 0xFF = off. Previously kelvin was derived from the WARM byte
        only - which was wrong as soon as brightness was below 100%
        (2700K @50% -> 4593K), and the next brightness-only command sent
        that wrong kelvin and changed the light's colour (confirmed with
        the simulator). Now the output of both channels is computed and
        their RATIO is used - brightness cancels out. 0xFF = exactly 0
        output (previously full cool read as 6485K, now 6500K). Both off,
        or so dim that the colour cannot be decoded -> None: the caller
        keeps the previous kelvin."""
        def out(level: int) -> float:
            return 0.0 if level >= 0xFF else (256 - max(1, level)) / 255
        cool, warm = out(cool_level), out(warm_level)
        # At very low brightness the bytes carry no colour information at
        # all (at 1% one channel rounds to "off") - storing a guess there
        # would make the next command change the colour. Below 10% total
        # output return None: the caller keeps the previous (correct) kelvin.
        if cool + warm < _AZ_MIN_OUTPUT_FOR_KELVIN:
            return None
        warm_frac = warm / (cool + warm)
        return round(CTC_MAX_KELVIN - warm_frac * (CTC_MAX_KELVIN - CTC_MIN_KELVIN))

    def _find_ctc_channel(
        self, area: int, mode: str, wire_channel: Optional[int] = None
    ) -> Optional[int]:
        """The configured CTC channel (if any) matching this Area and this
        sub-mode (single/double) - for CTC, incoming frames have to be
        matched BEFORE the normal ch_num-based lookup, because the wire
        'channel' byte here is a sub-signal (brightness/colour), not a
        physical channel.

        BUG FIX (MOD2U -> MOD4U generalization): on the MOD2U only one CTC
        pair was possible, so an area+mode match was enough. The MOD4U can
        have 2 pairs - if both are CTC (single mode) in the SAME Area, the
        old code always returned the FIRST match, which mixed up the data
        of both pairs (an incoming Pair 2 frame wrongly updated the Pair 1
        entity, or vice versa). Now, if `wire_channel` is given (always
        available for single mode, because that frame carries the real
        physical channel number), only the channel matching that
        wire_channel's own PAIR is returned - both pairs are cleanly
        disambiguated, even in the same Area."""
        candidates = [
            (cn, st) for cn, st in self.channel_states.items()
            if (
                st.get("type") == CH_TYPE_CTC
                and st.get("ctc_mode", CTC_MODE_SINGLE) == mode
                and st.get("area") == area
            )
        ]
        if not candidates:
            return None
        if wire_channel is None:
            return candidates[0][0]
        # Multiple CTC candidates (both pairs in the same Area) - pick the
        # one that matches wire_channel's pair.
        target_lo, target_hi = self._pair_bounds(wire_channel)
        for cn, _st in candidates:
            cn_lo, cn_hi = self._pair_bounds(cn)
            if (cn_lo, cn_hi) == (target_lo, target_hi):
                return cn
        # Fallback: if there is only ONE candidate, use it even if
        # wire_channel does not match its pair. This is a safety net for
        # older firmware/frame variants that send a fixed 01/02 marker
        # instead of the channel byte - state sync keeps working in that
        # case too.
        if len(candidates) == 1:
            return candidates[0][0]
        return None

    def _apply_ctc_single_update(self, ch_num: int, wire_channel: int, level: int):
        st = self.channel_states.setdefault(ch_num, {})
        _ct_channel, brightness_channel = self._ctc_single_wire_channels(ch_num)
        if wire_channel == brightness_channel:
            if level == CTC_SINGLE_BRIGHTNESS_OFF:
                st.update({"on": False, "brightness": 0})
            else:
                st.update({"on": True, "brightness": max(1, min(255, 256 - level))})
        else:  # ct_channel
            st["color_temp_kelvin"] = self._single_ct_level_to_kelvin(level)
        if self.state_callback:
            self.state_callback(self.ip, ch_num, st)

    def _handle_az(self, line: str):
        """*AZ= = double-driver CTC frame (cool+warm combined). Format:
        <area><ch_lo><cool_level><ch_hi><warm_level><64><pct> (7 bytes,
        see const.py) - the lower physical channel of the pair = white/
        cool, the higher = yellow/warm (the same wiring _send_ctc_double
        sends).

        BUG FIX: previously this parser returned as soon as
        `b[1] != 0x01 or b[3] != 0x02`, i.e. incoming frames for any CTC
        pair other than channels 1-2 (such as 15-16) were silently
        discarded - the CCT light's state in HA never synced. Any
        consecutive channel pair is now accepted."""
        try:
            idx = line.find("*AZ=")
            if idx == -1:
                return
            hex_part = "".join(line[idx + 4:].split())[:14]    # P3
            if len(hex_part) < 14:
                self._log_once(
                    ("short", "*AZ="), logging.INFO,
                    "Raylogic %s: ignored a short *AZ= frame: %s", self.ip, line[:120],
                )
                return
            b = bytes.fromhex(hex_part)
            if len(b) < 7:
                return
            # ch_lo/ch_hi are always a consecutive physical pair.
            if b[3] != b[1] + 1:
                return
            area = b[0]
            cool_level, warm_level = b[2], b[4]
            pct = b[6]
            ch_num = self._find_ctc_channel(
                area, CTC_MODE_DOUBLE, wire_channel=b[1],
            )
            if ch_num is None:
                return
            brightness = max(0, min(255, round(pct * 255 / 100)))
            st = self.channel_states.setdefault(ch_num, {})
            st.update({"on": brightness > 0, "brightness": brightness})
            kelvin = self._double_levels_to_kelvin(cool_level, warm_level)
            if kelvin is not None:      # off frame: keep the previous colour
                st["color_temp_kelvin"] = kelvin
            if self.state_callback:
                self.state_callback(self.ip, ch_num, st)
        except Exception as exc:
            _LOGGER.debug("Raylogic AZ parse error '%s': %s", line, exc)
            self._log_once(
                ("bad", "*AZ="), logging.INFO,
                "Raylogic %s: could not parse an *AZ= frame, ignored: %s (%s)",
                self.ip, line[:120], exc,
            )

    # ------------------------------------------------------------------ #
    # Control - Fan (CONFIRMED format, Model_Number_Mod2u.txt capture)
    #   Frame: 00 1A <area> <level> <channel>
    #   level: 0x01=off, 0x02=speed1(25%), 0x03=speed2(50%),
    #          0x04=speed3(75%), 0x05=speed4/full(100%)
    # ------------------------------------------------------------------ #
    async def set_fan(self, ch_num: int, percentage: int):
        """percentage: 0, 25, 50, 75, 100 (HA fan speed steps)."""
        state = self.channel_states.get(ch_num, {})
        area = state.get("area")
        if not area:
            _LOGGER.error(
                "Raylogic %s: the Area of channel %d is not known - "
                "the fan command cannot be sent.", self.ip, ch_num,
            )
            return
        # Take the nearest confirmed step (0/25/50/75/100)
        step = min(FAN_SPEEDS.keys(), key=lambda k: abs(k - percentage))
        level = FAN_SPEEDS[step]
        cmd_hex = f"{CMD_ADDR_HIGH}{CMD_CHANNEL_DIRECT}{area:02X}{level:02X}{ch_num:02X}"
        await self._send_addressed(f"*AR={cmd_hex}", ch=ch_num)
        self.channel_states.setdefault(ch_num, {}).update(
            {"on": step > 0, "percentage": step}
        )
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Control - Curtain
    #
    # BUG FIX (curtains did not work on any other Area/channel): previously
    # there were 6 HARDCODED literal command strings here, taken from a
    # capture of a single device (Area 7, channels 3-6) and sent unchanged
    # to every device. The third byte of the curtain frame is a GLOBAL
    # curtain slot - channels 23-24 in Area 08 needed 0x0C, but 0x02 was
    # always sent, so the device silently ignored the frame (no error at
    # all, just the "nothing happens" symptom). The whole frame is now
    # derived from the channel - see curtain_slot_for_channel() /
    # curtain_frame() and the Curtain block in const.py.
    # ------------------------------------------------------------------ #
    async def set_cover(self, ch_num: int, action: str):
        """action: 'open' | 'close' | 'stop'."""
        pair_lo, _pair_hi = self._pair_bounds(ch_num)
        pair_index = (pair_lo - self._channel_start) // CHANNELS_PER_PAIR
        slot = self.curtain_slot_for_channel(ch_num)
        cmd_hex = self.curtain_frame(ch_num, action)
        if not cmd_hex:
            _LOGGER.warning(
                "Raylogic %s %s: unknown curtain action for channel %d "
                "'%s' - nothing was sent.",
                self._model_name, self.ip, ch_num, action,
            )
            return
        _LOGGER.info(
            "Raylogic %s %s: curtain '%s' -> channel %d (device Pair %d, "
            "physical ch %d-%d, global curtain slot %d/0x%02X, "
            "connected=%s) - sending *AR=%s",
            self._model_name, self.ip, action, ch_num, pair_index + 1,
            pair_lo, pair_lo + 1, slot, slot, self._connected, cmd_hex,
        )
        await self._send_addressed(f"*AR={cmd_hex}", ch=ch_num)
        if action != "stop":
            self.channel_states.setdefault(ch_num, {}).update(
                {"on": action == "open", "moving": True}
            )
            if self.state_callback:
                self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Area scenes (v1.6.0)
    # ------------------------------------------------------------------ #
    async def recall_scene(self, area: int, scene: int):
        """Recall a Raylogic GO app area scene: *AR=000F<area><scene>00.

        This is a bus broadcast - all fixtures in that Area react, not just
        this module's channels. _send_addressed() sends it under
        CLIENT_SENDER_ID (099, the app/keypad node) and, like all other
        commands, it gets TCP-ACK verification + queue/replay on disconnect
        (a recall is idempotent, so resending is safe)."""
        if not (1 <= area <= SCENE_AREAS and 1 <= scene <= SCENE_MAX):
            _LOGGER.warning(
                "Raylogic %s %s: invalid scene recall area=%s scene=%s - "
                "nothing was sent.", self._model_name, self.ip, area, scene,
            )
            return
        cmd_hex = f"00{SCENE_FUNC:02X}{area:02X}{scene:02X}00"
        _LOGGER.debug(
            "Raylogic %s: recall_scene area=%d scene=%d -> *AR=%s",
            self.ip, area, scene, cmd_hex,
        )
        await self._send_addressed(f"*AR={cmd_hex}")
        self.active_scene[area] = scene

    # ------------------------------------------------------------------ #
    # Background loops
    # ------------------------------------------------------------------ #
    async def _listen_loop(self, gen: int):
        while self._connected and gen == self._conn_generation:
            try:
                line = await self._read_line(timeout=float(LISTEN_READ_TIMEOUT))
            except ConnectionError as exc:
                # The peer closed the connection (EOF) - a real disconnect,
                # not a tight loop. Properly mark it disconnected + trigger
                # the normal reconnect cycle, and exit the loop.
                if gen != self._conn_generation:
                    return  # this task belongs to an old session, exit quietly
                session = self._now() - self._session_started
                self._record_session_result(session)
                if session >= FAST_RECONNECT_MIN_SESSION:
                    # The device's normal/expected auto-disconnect - no need
                    # to make noise about it as an error, just reconnect
                    # immediately (see FAST RECONNECT in const.py).
                    _LOGGER.debug(
                        "Raylogic %s %s: the device closed the connection "
                        "after %.0fs (its normal behaviour) - reconnecting "
                        "immediately.",
                        self._model_name, self.ip, session,
                    )
                else:
                    _LOGGER.warning(
                        "Raylogic %s %s: %s (the session only lasted %.0fs) - "
                        "marked as disconnected, triggering a reconnect.",
                        self._model_name, self.ip, exc, session,
                    )
                self._connected = False
                self._mark_unavailable_soon()
                self._schedule_reconnect()
                return
            if gen != self._conn_generation:
                return
            if line:
                self._dispatch_line(line)
                continue

            # Read timeout - nothing arrived from the device in this window.
            # STABILITY FIX (v1.5.0): previously nothing happened here, the
            # loop simply went back to reading. A "half-dead" socket (where
            # neither EOF nor data arrives) was only detected when the next
            # WRITE failed - so a dead connection could keep showing as
            # "connected" on a minute scale.
            # Detection is now PASSIVE: the device itself sends its *KA=
            # every 6-12 s, so silence for this long = the connection is
            # dead. This is faster and does not disturb the module either
            # (no extra writes).
            idle = self._now() - self._last_rx
            if idle >= RX_SILENCE_TIMEOUT:
                _LOGGER.warning(
                    "Raylogic %s %s: no frame received from the device for "
                    "%.0fs (normally one arrives every 6-12s) - treating the "
                    "connection as dead and reconnecting.",
                    self._model_name, self.ip, idle,
                )
                self._last_session_len = self._now() - self._session_started
                self._connected = False
                self._mark_unavailable_soon()
                self._schedule_reconnect()
                return

    async def _keepalive_loop(self, gen: int):
        # SCALE FIX: the first keepalive also gets a small random delay, so
        # that the keepalive writes of many devices (100+) do not become
        # EXACTLY synchronized (a small fix - KEEPALIVE_INTERVAL is short,
        # so the effect is small too, but it is the same principle as the
        # resync jitter).
        #
        # BUG FIX: same as _resync_loop() - this random stagger used to be
        # drawn fresh for the new task on EVERY reconnect, not only at HA
        # startup. Now a dedicated instance-level flag
        # (`self._keepalive_staggered`, just like `self._resync_staggered`)
        # ensures the random delay is only applied in this device
        # instance's FIRST keepalive cycle - all later reconnects simply
        # wait the normal KEEPALIVE_INTERVAL.
        #
        # STABILITY FIX (v1.5.0) - the keepalive is now PASSIVE. From real
        # logs: the device itself sends its *KA= frame every 6-12 seconds
        # (293 RX frames in 10 minutes), while we were piling 125 of our own
        # *KA=01 writes on top of that. For these cheap ESP8266-class
        # modules every extra write is an extra risk (their TCP stack is
        # weak) - and that write gave us no new information, because proof
        # of liveness was already coming from the device's own frames.
        #
        # We now only write when NOTHING at all has arrived from the device
        # for KEEPALIVE_INTERVAL. Under normal conditions our write count
        # drops to practically 0.
        if not self._keepalive_staggered:
            self._keepalive_staggered = True
            await asyncio.sleep(random.uniform(0, KEEPALIVE_INTERVAL))
        while self._connected and gen == self._conn_generation:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if not self._connected or gen != self._conn_generation:
                return

            # BUG FIX (caught by testing): the decision to lock a variant
            # used to be made ONLY on disconnect - but if the variant really
            # works, the connection never breaks, so the lock never happened
            # and on the next reconnect the code moved away from the working
            # format. The running session is now checked here as well.
            if not self._ka_variant_locked:
                live = self._now() - self._session_started
                if live >= KEEPALIVE_GOOD_SESSION:
                    idx = self._ka_variant % len(KEEPALIVE_VARIANTS)
                    self._ka_variant_locked = True
                    _LOGGER.info(
                        "Raylogic %s %s: with keepalive format '%s' the "
                        "connection has stayed alive continuously for %.0fs "
                        "(the device's ~12s auto-disconnect was broken) - "
                        "this format will always be used from now on.",
                        self._model_name, self.ip, KEEPALIVE_VARIANTS[idx], live,
                    )

            # v1.5.3: the real keepalive is now the REPLY to the device's
            # own *KA= (_answer_device_keepalive) - that is what the device
            # needs. This loop is only a fallback: if for some reason the
            # device has not sent a KA and we have not written anything for
            # a while either, send a keepalive ourselves.
            since_tx = self._now() - self._last_tx if self._last_tx else 1e9
            idle = self._now() - self._last_rx
            if since_tx < KEEPALIVE_IDLE_THRESHOLD and idle < KEEPALIVE_IDLE_THRESHOLD:
                continue
            _LOGGER.debug(
                "Raylogic %s: no traffic for %.0fs - sending a fallback "
                "keepalive.", self.ip, min(idle, since_tx),
            )
            self._last_tx = self._now()
            await self._send_raw(self._keepalive_frame())

    def _dispatch_line(self, line: str):
        _LOGGER.debug("RX %s: %s", self.ip, line)
        if "*KA=" in line:
            self._handle_ka_line(line)
        elif "+AR40=" in line:
            # NEWLY FOUND (typo/gap fix, this time for VISIBILITY only): this
            # is a SEPARATE periodic frame that the MOD4U sends on its own
            # (~19-20 s). Its byte layout has not been decoded yet - it is
            # only shown clearly in the RX log so that 3-4 samples can be
            # collected and its real structure decoded (the next concrete
            # step, if it is ever needed).
            # P5 (v1.6.4): it arrives about once every ~20 s - an INFO log
            # every time was noisy with many devices. Now one sample per
            # connection session is logged at INFO, the rest at DEBUG.
            self._log_once(
                "ar40", logging.INFO,
                "Raylogic %s: received a +AR40= status heartbeat (not "
                "decoded yet; it will not be logged again in this session) "
                "- raw: %s", self.ip, line,
            )
            _LOGGER.debug("Raylogic %s: +AR40= raw: %s", self.ip, line)
        elif "*AZ=" in line:
            self._handle_az(line)
        elif "*AR=" in line:
            self._handle_ar(line)
        else:
            # P3: unknown frame type - ignored (as before), but each type is
            # shown once at INFO so that new firmware can be noticed.
            m = _FRAME_TYPE_RE.search(line)
            kind = m.group(0) if m else "?"
            self._log_once(
                ("unknown", kind), logging.INFO,
                "Raylogic %s: ignored unknown frame type %s (only the first "
                "sample of this type is logged): %s", self.ip, kind, line[:120],
            )

    def _decode_level(self, ch_type: str, level: int) -> dict:
        """Decode the level byte of an incoming *AR= according to the
        channel TYPE. Previously this always used the Relay check
        (level==0x02) - so the status of Dimmer/Fan channels (and the
        Dimmer's brightness) was never updated correctly, even when the
        device sent the correct frame."""
        if ch_type == CH_TYPE_DIMMER:
            if level == DIMMER_LEVEL_OFF:
                return {"on": False, "brightness": 0}
            brightness = max(1, min(255, 256 - level))
            return {"on": True, "brightness": brightness}
        if ch_type == CH_TYPE_FAN:
            if level == FAN_LEVEL_OFF:
                return {"on": False, "percentage": 0}
            step = next(
                (pct for pct, lvl in FAN_SPEEDS.items() if lvl == level), None
            )
            if step is None:
                step = min(FAN_SPEEDS, key=lambda p: abs(FAN_SPEEDS[p] - level))
            return {"on": step > 0, "percentage": step}
        # Relay (default). P4 (v1.6.4): only the confirmed levels - 01 = OFF,
        # 02 = ON. Previously EVERYTHING other than 02 was treated as OFF, so
        # a firmware with a different ON level would wrongly show the relay
        # as OFF. The state now does NOT change on an unknown level (one
        # warning).
        if level == int(RELAY_LEVEL_ON, 16):
            return {"on": True}
        if level == int(RELAY_LEVEL_OFF, 16):
            return {"on": False}
        self._log_once(
            ("relay_level", level), logging.WARNING,
            "Raylogic %s: received unknown level 0x%02X for a relay (only "
            "01=OFF / 02=ON are confirmed) - state not changed. This may be "
            "new firmware; please include a sample of this level in an "
            "issue.", self.ip, level,
        )
        return {}

    def _handle_curtain_frame(self, b: bytes):
        """Curtain echo (from the app or a physical switch) - frame:
              00 27 <slot> <dir> <run>    (open/close)
              00 26 <slot> 00   00        (stop)

        It does two things:
          1. Syncs the state of the HA cover entity (previously these frames
             were not parsed at all).
          2. LEARNS the <run> byte (the curtain's travel parameter) - if the
             app operates it with a different value, HA will send that same
             value from then on instead of the hardcoded default."""
        slot = b[2]
        ch_num = self._find_curtain_channel(slot)
        if ch_num is None:
            _LOGGER.debug(
                "Raylogic %s: received a curtain frame for slot %d (0x%02X), "
                "but this device has no configured curtain channel for that "
                "slot - ignored.", self.ip, slot, slot,
            )
            return
        if b[1] == CURTAIN_CMD_STOP:
            _LOGGER.debug(
                "Raylogic %s: curtain STOP echo (slot %d, channel %d).",
                self.ip, slot, ch_num,
            )
            return
        direction = b[3]
        run = b[4]
        if run and self._curtain_run_bytes.get(slot) != run:
            self._curtain_run_bytes[slot] = run
            _LOGGER.info(
                "Raylogic %s: learned the 'run' byte of curtain slot %d from "
                "the device: 0x%02X (HA will now send the same value).",
                self.ip, slot, run,
            )
        if direction not in (CURTAIN_DIR_OPEN, CURTAIN_DIR_CLOSE):
            return
        st = self.channel_states.setdefault(ch_num, {})
        st.update({"on": direction == CURTAIN_DIR_OPEN, "moving": True})
        if self.state_callback:
            self.state_callback(self.ip, ch_num, st)

    def _handle_ar(self, line: str):
        """*AR= echo from the mobile app or another node - for real-time
        sync. Format: <ID>,<Seq>,*AR=00 1A <area> <level> <channel>

        NOTE: on the wire this line may also carry a prefix such as
        "001,086," (Docklight's or another client's own format) - we simply
        extract the hex after "*AR="; the prefix makes no difference.
        """
        try:
            idx = line.find("*AR=")
            if idx == -1:
                return
            # P3 (v1.6.4): "*AR= 001A.." / "00 1A 0C .." must work too -
            # previously the fixed 10-char slice failed silently as soon as
            # a space appeared.
            hex_part = "".join(line[idx + 4:].split())[:10]
            if len(hex_part) < 10:
                self._log_once(
                    ("short", "*AR="), logging.INFO,
                    "Raylogic %s: ignored a short *AR= frame: %s", self.ip, line[:120],
                )
                return
            b = bytes.fromhex(hex_part)
            if len(b) < 5:
                return

            # v1.6.0: area-scene recall echo (keypad / app / another node):
            #   00 0F <area> <scene> 00  -> two-way feedback for the select entity.
            # Previously this frame was silently discarded by the
            # `b[1] != 0x1A` check below. It has no effect on channel state
            # (after a scene, channels are updated by their own
            # *AR=001A.. frames).
            if b[0] == 0x00 and b[1] == SCENE_FUNC:
                area, scene = b[2], b[3]
                if 1 <= area <= SCENE_AREAS and 1 <= scene <= SCENE_MAX:
                    self.active_scene[area] = scene
                    if self.state_callback:
                        self.state_callback(self.ip, f"scene_{area}", {"scene": scene})
                return

            # NEW: curtain frames (cmd 0x27 = open/close, 0x26 = stop) used
            # to be discarded right here by the `b[1] != 0x1A` check - i.e.
            # the state of a curtain operated from the app/physical switch
            # was never reflected in HA. A curtain frame has no Area byte at
            # all, so matching is done by the global curtain slot.
            if b[1] in (CURTAIN_CMD_MOVE, CURTAIN_CMD_STOP):
                self._handle_curtain_frame(b)
                return

            if b[1] != 0x1A:
                return
            area = b[2]
            level = b[3]
            ch_num = b[4]

            # CTC (single-driver) special case: here the "channel" byte
            # (ch_num) IS the real physical channel number (such as 3 or 4)
            # - NOT a fixed 0x01/0x02 sub-signal id (the earlier mistake,
            # assumed from a single-channel capture only). Within the pair,
            # the lower number = colour temperature and the higher number =
            # brightness (see the const.py comment). This is why it must be
            # handled BEFORE the normal ch_num-based lookup, otherwise it
            # could wrongly corrupt the state of another normal channel
            # (whose real ch_num is 1 or 2).
            ctc_ch = self._find_ctc_channel(area, CTC_MODE_SINGLE, wire_channel=ch_num)
            if ctc_ch is not None:
                ct_channel, brightness_channel = self._ctc_single_wire_channels(ctc_ch)
                if ch_num in (ct_channel, brightness_channel):
                    self._apply_ctc_single_update(ctc_ch, ch_num, level)
                    return

            # Manual mode (Area configured, >0): only accept frames for THAT
            # area, and only update the state of channels that are already
            # configured - do not create new "phantom" channels automatically
            # (this is why ch3/ch4 were once wrongly created on a 2-channel
            # MOD2U, from another device's/area's traffic).
            if self._legacy_area and self._legacy_area > 0:
                if area != self._legacy_area:
                    return
                if ch_num not in self.channel_states:
                    _LOGGER.debug(
                        "Raylogic %s: received an *AR= frame for area=%d "
                        "ch=%d, but this channel is not in the manual config "
                        "- ignored (it may belong to another device).",
                        self.ip, area, ch_num,
                    )
                    return
                ch_type = self.channel_states[ch_num].get("type", CH_TYPE_RELAY)
                self.channel_states[ch_num].update(self._decode_level(ch_type, level))
                if self.state_callback:
                    self.state_callback(self.ip, ch_num, self.channel_states[ch_num])
                return

            # LEARN mode (Area=0): when a new channel is discovered, create
            # its entity dynamically - this is the original intended
            # behaviour. LEARN only works for Relay (a new channel is always
            # created as a relay), hence the direct Relay decode here.
            is_new = self._learn_channel(ch_num, area)
            self.channel_states[ch_num].update(self._decode_level(CH_TYPE_RELAY, level))  # P4

            if is_new and self.new_channel_callback:
                self.new_channel_callback(ch_num, self.channel_states[ch_num])

            if self.state_callback:
                self.state_callback(self.ip, ch_num, self.channel_states[ch_num])
        except Exception as exc:
            _LOGGER.debug("Raylogic AR parse error '%s': %s", line, exc)
            self._log_once(
                ("bad", "*AR="), logging.INFO,
                "Raylogic %s: could not parse an *AR= frame, ignored (only "
                "the first occurrence of this kind is logged): %s (%s)", self.ip, line[:120], exc,
            )
