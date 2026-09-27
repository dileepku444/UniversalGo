"""Constants for the Raylogic MOD4U integration.

The MOD4U is the same kind of "universal module" as the MOD2U (same
Raylogic GO app, same Select Type screen) - the only difference is that the
MOD4U has 4 physical channels instead of 2, grouped into 2 PAIRS:
  Pair 1 = channel_start + 0, channel_start + 1   (e.g. ch1, ch2)
  Pair 2 = channel_start + 2, channel_start + 3   (e.g. ch3, ch4)
Each channel can independently be a Relay / Dimmer / Fan, BUT Curtain and
CTC are both "paired" modes - when one channel of a pair is set to Curtain
or CTC, the whole pair (both physical channels) is consumed internally by
that ONE logical entity (exactly as for CTC on the MOD2U - Curtain follows
the same rule here, as confirmed by the user).

This file contains two kinds of values:
  1. CONFIRMED  - already verified from the MOD2U capture (the Relay/
     Dimmer/Fan/CTC-pair-1 format is identical for the MOD4U, because at
     the wire-protocol level the MOD4U uses the same *AR=/*AZ= frame shape,
     just with more channels/pairs).
  2. PLACEHOLDER / TODO - the literal Curtain bytes for Pair 2 (channels
     3-4) have not been captured yet. Wherever "TODO CAPTURE" appears, the
     real value must be filled in here once it is known. Until then the
     fallback/warning logic (in protocol.py) uses safe defaults.
"""

DOMAIN = "raylogic_mod"

# Network -------------------------------------------------------------- #
DEFAULT_PORT = 5550
CONNECT_TIMEOUT = 5
RECONNECT_DELAY = 5   # v1.6.7 (P2): 30 -> 5, reconnect quickly once the module is back

# STABILITY FIX (web page/entities flickering "Unavailable"): every
# disconnect (even a 2-second blip, such as the device's own resync or a
# short network hiccup) used to show "Unavailable" in the UI immediately,
# and the next reconnect attempt always happened after a FIXED 30 s - even
# if the device came back straight away. Real logs showed that many
# disconnects recover by themselves within 5-10 seconds (the device was
# briefly busy, or it was our own periodic resync) - but the fixed 30 s
# retry plus the immediate "Unavailable" flag made the UI flicker/show
# Unavailable constantly, although the device was only down briefly.
#
# Fix (in both places): (1) reconnect no longer waits a FIXED 30 s - the
# first retry is short and fast, and the wait grows gradually up to
# RECONNECT_DELAY (RECONNECT_BACKOFF_STEPS below), so short blips recover
# immediately without waiting the full 30 s. (2) Do not show the entity as
# "Unavailable" immediately - allow a short grace window of
# UNAVAILABLE_GRACE_SECONDS; if the reconnect succeeds within that window
# (as it does for short blips), the UI never flickers. Only if connect()
# has not succeeded after that long is the entity shown as "Unavailable" -
# i.e. only for a sustained/genuine outage.
RECONNECT_BACKOFF_STEPS = (1, 2, 4, RECONNECT_DELAY)
UNAVAILABLE_GRACE_SECONDS = 45

# BUG FIX: writer.close() + wait_closed() used to have no upper-bound
# timeout. If a Raylogic device was flaky on the network or did not
# complete the TCP FIN/ACK cleanly (which a cheap embedded device may do),
# wait_closed() could hang without limit - sometimes until the OS's own
# TCP retry timeout (minutes). This was the root cause of the "hangs for a
# while and then comes back" pattern, and the more devices, the more often
# it triggered. Every close operation is now wrapped in this fixed
# CLOSE_TIMEOUT (protocol.py + config_flow.py) - even on timeout the
# socket/writer is discarded/set to None, so the caller is never stuck for
# longer than this.
CLOSE_TIMEOUT = 5

# BUG FIX (second hang source, found separately): every command send is
# followed by `await writer.drain()` to keep the TCP write buffer small -
# but this also had NO timeout. If the device stopped or stopped sending
# TCP ACKs (a "half-dead" socket without any error/FIN), drain() could
# hang until the OS's own TCP retransmit timeout (minutes) - and every
# switch/light/fan/cover turn_on/turn_off awaits this drain() directly, so
# it made the HA service call (for that entity) feel "hung". It is now
# hard-capped by WRITE_TIMEOUT in the same way - as soon as it times out,
# the connection is treated as "lost" and a reconnect cycle is triggered
# (see protocol.py _send_raw); a command is never stuck longer than this.
#
# TUNING NOTE (a real-world regression was found): the first attempt used
# 5 s - far too tight. When a new device connects to the network (e.g.
# adding another Raylogic box), a short transient hiccup on a small/cheap
# home router Wi-Fi (ARP resolution, some packet delay) is completely
# NORMAL and recovers by itself within 1-2 seconds. The tight 5 s limit
# mistook this normal hiccup for "the device has hung" and force-killed
# the connection immediately - so adding a new device also put an
# already-working device into "connection lost", caused more by this FIX
# than by the original bug. Now 15 s - still never allows a genuine
# infinite/multi-minute hang, but no longer mistakes normal network jitter
# for a "hang".
WRITE_TIMEOUT = 15

# ------------------------------------------------------------------ #
# MODEL_* = values of the "Device Model" dropdown in config_flow. Each
# model's name/description/default channel count is defined here in one
# place - to add a new model, just add one more entry here; the rest of
# the code (protocol.py) is generic and already works for any channel
# count/pair count.
#
# NOTE: each model used to have a "br40_code" here as well (for
# RE8/H81-style auto-discovery) - MOD2U/MOD4U/MOD2F never answered the
# `?BR40=` query (confirmed), so the whole BR40 code path has been removed
# (from both const.py and protocol.py). The static/legacy channel setup
# (Area + per-channel Type from config_flow) was always the path that
# actually worked.
# ------------------------------------------------------------------ #
MODEL_MOD2U = "mod2u"
MODEL_MOD4U = "mod4u"
MODEL_MOD2F = "mod2f"

DEVICE_MODELS: dict[str, dict] = {
    MODEL_MOD2U: {
        "name": "MOD2U",
        "desc": "Universal 2-Channel Module (Dimmer/Fan/Curtain/Relay/CTC)",
        "channel_count": 2,
    },
    MODEL_MOD4U: {
        "name": "MOD4U",
        "desc": "Universal 4-Channel Module (Dimmer/Fan/Curtain/Relay/CTC)",
        "channel_count": 4,
    },
    MODEL_MOD2F: {
        "name": "MOD2F",
        "desc": "Single-Channel Fan Module (fixed Fan type)",
        "channel_count": 1,
        # Unlike the MOD2U/MOD4U, the MOD2F is not a "universal" module -
        # its single physical channel is always a fan (the Raylogic GO app
        # has no "Select Type" option for it, only "Fan Mode"). The config
        # flow therefore never shows a Channel Type dropdown for it (see
        # _channels_schema_fields) - this fixed value is always used as the
        # type. (The string literal "fan" = CH_TYPE_FAN, defined below - a
        # literal is used here to avoid a forward reference.)
        # CONFIRMED (Model_Number_-_Mod2F.txt + app screenshot, device
        # 192.168.1.36, node 111, Area 04, start address/channel 0x09):
        #   Frame shape exactly as MOD2U/MOD4U: 00 1A <area> <level> <channel>
        #   Off: level=01   Speed1: level=02   Speed2: level=03
        #   Speed3: level=04   Speed4/full: level=05
        # (The same FAN_LEVEL_OFF/FAN_SPEEDS below are reused - the MOD2F
        # needs no separate constants.)
        "fixed_type": "fan",
    },
}
DEFAULT_MODEL = MODEL_MOD4U

# ------------------------------------------------------------------ #
# Area
# Confirmed from the MOD2U capture: the Area byte is simply the area
# number in hex (Area 12 -> 0x0C). The MOD4U uses the same Area scheme (1-16).
# ------------------------------------------------------------------ #
AREA_MIN = 1
AREA_MAX = 16
LEGACY_DEFAULT_AREA = 0x0C  # 12 - default when the user has not entered
                             # an area in the config

# ------------------------------------------------------------------ #
# Command bytes - CONFIRMED (Docklight capture from a MOD2U; the MOD4U
# uses the same wire protocol)
#   <ID>,<Seq>,*AR=<AddrHigh:00><Cmd:1A><Area><Level><Channel>
#   Relay: Level 01=OFF, 02=ON
# ------------------------------------------------------------------ #
CMD_ADDR_HIGH = "00"
CMD_CHANNEL_DIRECT = "1A"
RELAY_LEVEL_ON = "02"
RELAY_LEVEL_OFF = "01"

# ------------------------------------------------------------------ #
# Dimmer - CONFIRMED (Model_Number_Mod2u.txt, vendor capture, Area 12):
#   Ch1 On :  *AR=001A0C0101  -> level=01 (full/on)
#   Ch1 Off:  *AR=001A0CFF01  -> level=FF (off)
#   Ch1 dimming ramp goes FF -> ... -> 69 as brightness increases
#   Ch2 same pattern with channel byte = 02
# Same frame shape as Relay (00 1A <area> <level> <channel>), only the
# level byte meaning is different: 0x01 = full brightness, 0xFF = off,
# values in between are a proprietary dim curve. No exact 256-step table
# was captured, so brightness is mapped linearly between the two
# confirmed endpoints (good enough approximation; refine if a fuller
# capture turns up). Frame shape is per-channel (channel byte = the real
# physical channel number), so this scales to all 4 MOD4U channels
# unchanged - no MOD4U-specific capture needed.
# ------------------------------------------------------------------ #
DIMMER_LEVEL_ON = 0x01
DIMMER_LEVEL_OFF = 0xFF

# ------------------------------------------------------------------ #
# CTC (Colour Temperature Control / tunable white) - CONFIRMED
# (Model_Number_Mod2u.txt capture, Area 16, matches the user's own Mod
# Settings screenshot: Channel 1 = CTC, "Single Driver" checked).
#
# The Raylogic GO app supports TWO CTC sub-modes, each with its own wire
# format - which one a module uses is a config choice (the Double
# Driver / Single Driver checkboxes in the app), not something the
# device reports on its own, so it's selected in this integration's
# config too (see config_flow.py CTC_MODE_OPTIONS). On the MOD4U this
# sub-mode can be chosen SEPARATELY for EACH PAIR (Pair 1 and Pair 2 can
# each independently be single or double).
#
# 1) SINGLE DRIVER (CW/WW, one physical channel-PAIR - confirmed by the
#    user: configuring a CTC light in the Raylogic GO app actually
#    consumes TWO real physical channels, e.g. channel 3 + channel 4,
#    exactly like any other 2-channel allocation on this device) -
#    identical frame shape to Relay/Dimmer/Fan (00 1A <area> <level>
#    <channel>), and the "channel" byte here IS the real physical
#    channel number of the pair, same as everywhere else in this
#    protocol - NOT a fixed 0x01/0x02 sub-signal id as first assumed.
#    Within the pair:
#      the LOWER physical channel number  -> colour temperature,
#        level 0x0B..0xFF across the 14 captured points (monotonic) -
#        linear approximation between the two confirmed endpoints,
#        same "good enough" approach already used for the Dimmer
#        brightness curve above. Which physical end (warm/cool) 0x0B
#        vs 0xFF corresponds to was NOT recorded in the capture (only
#        consecutive level values, no colour reference) - if warm/cool
#        comes out reversed in practice, just swap
#        CTC_SINGLE_CT_MIN_LEVEL/MAX_LEVEL below.
#      the HIGHER physical channel number -> brightness, level
#        0x01=full .. 0xFF=off (same curve/formula as Dimmer above,
#        reused as-is).
#    This "lower=CT, higher=brightness" rule applies to WHICHEVER pair
#    the CTC channel is in (Pair 1 = channel_start/channel_start+1, or
#    Pair 2 = channel_start+2/channel_start+3) - protocol.py derives the
#    pair from the configured CTC channel's own position, not a fixed
#    channel_start/channel_start+1 assumption (that was a MOD2U-only
#    shortcut since MOD2U only ever HAD one pair).
#
# 2) DOUBLE DRIVER (separate warm+cool physical channels, combined into
#    one *AZ= frame instead of *AR=):
#      Frame (7 bytes after *AZ=):
#          <area><ch_lo><level_lo><ch_hi><level_hi><64><pct>
#
#      BUG FIX (this was the real CCT/CTC bug): <ch_lo> and <ch_hi> used
#      to be treated as FIXED "01"/"02" markers. That misunderstanding
#      arose only because the device the capture was taken from (MOD2U,
#      Area 16) had its CTC pair on PHYSICAL channels 1 and 2 - so a fixed
#      marker and the real channel number looked identical. Throughout the
#      protocol every frame (relay/dimmer/fan/CTC-single) always carries
#      the REAL physical channel number, and the curtain slot byte is also
#      derived from the channel - *AZ= follows the same rule. So on the
#      Area 12 MOD4U, whose CTC pair is on channels 15-16, the frame should
#      have carried 0F/10 but carried 01/02 - the device dropped the frame,
#      which is why the CCT light did not respond at all.
#      protocol.py now derives both bytes from the CTC channel's own pair
#      (lower channel = cool slot, higher = warm slot).
#      warm+cool always summed to 0x100 (256) in every captured colour-
#      temperature-only sweep (fixed 100% brightness); a separate
#      brightness-only sweep (fixed colour) varied that same "cool"
#      byte position from 0xFF(off) down to 0x01(full) as brightness
#      rose 0->100%. Both are consistent with a normal dual-channel
#      cross-fade driver where each channel follows the same
#      0x01=full/0xFF=off convention used everywhere else in this
#      protocol, scaled by both colour-temp position AND overall
#      brightness together. `pct` (last byte) is brightness 0-100
#      (0x00-0x64) and moved in lockstep with the scaled channel bytes
#      in every sample. Only PURE colour-only and PURE brightness-only
#      sweeps were captured (not an arbitrary combined change) - the
#      formula in protocol.py is derived to satisfy both captured
#      sweeps exactly, but a combined brightness+colour capture would
#      help confirm it fully.
#      NOTE: the old "MOD4U limitation" (two double-driver CTC pairs in
#      the same Area could not be told apart) no longer exists - since
#      the frame now carries the real channel numbers, both pairs are
#      cleanly distinguished by their own channel bytes.
# ------------------------------------------------------------------ #
CTC_MODE_SINGLE = "single"
CTC_MODE_DOUBLE = "double"

CTC_SINGLE_BRIGHTNESS_ON = DIMMER_LEVEL_ON      # 0x01, same curve as Dimmer
CTC_SINGLE_BRIGHTNESS_OFF = DIMMER_LEVEL_OFF    # 0xFF
CTC_SINGLE_CT_MIN_LEVEL = 0x0B   # confirmed lowest captured level
CTC_SINGLE_CT_MAX_LEVEL = 0xFF   # confirmed highest captured level

CTC_DOUBLE_CONST_BYTE = 0x64     # constant byte seen in every *AZ= sample

CTC_MIN_KELVIN = 2700   # warmest end of the HA colour-temp slider
CTC_MAX_KELVIN = 6500   # coolest end of the HA colour-temp slider
CTC_DEFAULT_KELVIN = 4000

# ------------------------------------------------------------------ #
# Fan - CONFIRMED (Model_Number_Mod2u.txt, Area 12):
#   Off:     *AR=001A0C0101 (ch1) / ...0102 (ch2) -> level=01
#   Speed 1: level=02   Speed 2: level=03
#   Speed 3: level=04   Speed 4: level=05
# Identical scheme to the Din-Re8 FN4 fan. Per-channel frame (channel
# byte = real physical channel), so this scales to all 4 MOD4U channels
# unchanged.
# ------------------------------------------------------------------ #
FAN_LEVEL_OFF = 0x01
FAN_SPEEDS = {0: 0x01, 25: 0x02, 50: 0x03, 75: 0x04, 100: 0x05}

# ------------------------------------------------------------------ #
# Curtain - now DERIVED from a formula (there used to be 6 hardcoded
# literal strings - THAT was the real curtain bug).
#
# A curtain uses a DIFFERENT frame shape from Relay/Dimmer/Fan (command
# byte 0x27 = open/close, 0x26 = stop, instead of 0x1A), and it has NO
# Area byte at all:
#
#     00 27 <curtain_slot> <direction> <run>      (open / close)
#     00 26 <curtain_slot> 00 00                  (stop)
#
# <curtain_slot> = the GLOBAL curtain/pair index, NOT the area or a
# per-device pair number. In a Raylogic installation channel numbers are
# allocated globally across the whole system, always in blocks of 2
# (device 1 -> ch 1-2, device 2 -> ch 3-6, even a 1-channel MOD2F takes a
# whole block 7-8, the next 9-10, ... and so on up to 23-24). Therefore:
#
#     curtain_slot = (lower channel number of the pair + 1) // 2
#
# CONFIRMED - real hardware echoes (from the user's own HA debug log, while
# the curtain was operated from the Raylogic GO app):
#   MOD2U node 115, Area 08, channels 23-24  -> *AR=00270C020A
#        (0x0C = 12 = (23+1)//2)   [close], *AR=00270C010A [open]
#   MOD4U node 111, Area 12, channels 13-16  -> *AR=002707020A
#        (0x07 =  7 = (13+1)//2)   [close], *AR=002707010A [open]
#   The old Model_Number_Mod4u.txt capture (Area 7, channels 3-6) ->
#        0x02 = (3+1)//2 and 0x03 = (5+1)//2 - the same formula, they were
#        just the slots of that one device. Treating them as "Pair 1 /
#        Pair 2" and sending them to EVERY device was the bug: the curtain
#        on ch 23-24 in Area 8 needed slot 12 but slot 2 was being sent -
#        the device silently ignores such a frame (hence the "nothing
#        happens" symptom, with no error either).
#
# <run> = the last byte. Real app traffic (both devices) uses 0x0A; the
# old Area-7 capture had 0x05 - so this is the curtain's configured
# travel/run parameter, not a fixed constant. The default is 0x0A (as on
# the user's real hardware), and protocol.py also LEARNS this byte
# automatically from curtain echoes sent by the device, so HA sends
# whatever value the app uses.
# ------------------------------------------------------------------ #
CURTAIN_CMD_ADDR_HIGH = "00"
CURTAIN_CMD_MOVE = 0x27      # open/close
CURTAIN_CMD_STOP = 0x26      # stop
CURTAIN_DIR_OPEN = 0x01
CURTAIN_DIR_CLOSE = 0x02
CURTAIN_RUN_BYTE_DEFAULT = 0x0A
CURTAIN_STOP_TAIL = "0000"

# Each curtain slot covers 2 physical channels (one pair).
CHANNELS_PER_CURTAIN_SLOT = 2

# ------------------------------------------------------------------ #
# +AR40= channel-type map - CONFIRMED (Model_Number_Mod2u.txt).
# This is a CONFIGURATION frame the Raylogic GO app sends to the module
# to SET each channel's mode (relay/dimmer/fan/curtain) - it is not a
# readback/query the module answers on its own, so it can't be used for
# live auto-detection the way RE8's +BR40= can. It's kept here for
# reference/documentation and for a future "set channel mode from HA"
# service, and to interpret the byte if it's ever seen echoed back.
# The MOD4U's own +AR40= layout (how ch_count/records grow for 4
# channels) has not been captured - the shape below is the MOD2U's
# (2-channel) layout, for reference only.
#   Bytes (12 total, after +AR40=): 01 01 <ch_count> <ch1_type> <ch1_sub>
#   <ch2_type> <ch2_sub> 00 00 00 FF FF
#   ch_type: 00=relay 01=dimmer 02=fan 03=curtain
# ------------------------------------------------------------------ #
AR40_TYPE_RELAY = 0x00
AR40_TYPE_DIMMER = 0x01
AR40_TYPE_FAN = 0x02
AR40_TYPE_CURTAIN = 0x03

# ------------------------------------------------------------------ #
# Channel types (per-channel, as seen in "Select Type" screen)
# These types come from config_flow (the user's manual selection) - see
# _resolve_channel_types (__init__.py). The device does not broadcast its
# own channel type (BR40 auto-discovery has been removed - see the
# protocol.py header note).
# ------------------------------------------------------------------ #
CH_TYPE_DIMMER = "dimmer"
CH_TYPE_FAN = "fan"
CH_TYPE_CURTAIN = "curtain"
CH_TYPE_RELAY = "relay"
CH_TYPE_CTC = "ctc"
CH_TYPE_EMPTY = "empty"

CHANNELS_PER_PAIR = 2
DEFAULT_CHANNEL_COUNT = 4  # fallback only, if the model info is unavailable for some reason
# NOTE: this fixed value used to be the channel-count default for the whole
# integration (when the integration supported only the MOD4U). The actual
# channel count now comes from DEVICE_MODELS[model]["channel_count"] (via
# the "Device Model" dropdown in config_flow): 2 (MOD2U) or 4 (MOD4U)
# depending on the model. PAIR_COUNT is now derived per model as well -
# see _pair_count(model) in config_flow.py.

# "scene" + "select" (v1.6.0): Raylogic GO app AREA scenes. They are global
# to the whole installation (not tied to one module), so only ONE entry
# (the "scene host") creates them - see scene.py / select.py.
PLATFORMS = ["switch", "light", "fan", "cover", "scene", "select"]

KEEPALIVE_CMD = "*KA=01"

# ------------------------------------------------------------------ #
# KEEPALIVE FORMAT (v1.5.3) - the cure for "the device drops the
# connection by itself every 12 seconds"
#
# The v1.5.2 logs showed a very clear pattern:
#   - 301 of 301 sessions were closed by the device after EXACTLY ~12.3
#     seconds (12 s/14 s/15 s, identical on every device).
#   - This had happened before too (the old v1.4.2 log had 1881 sessions,
#     all ending with EOF at 12.0 s) - but back then our own buggy resync
#     broke the connection at ~13 s anyway, so this device-side timer never
#     became visible.
#   - The device itself sends its *KA= about every 6 seconds. 6 x 2 = 12.
#     Meaning: the device expects a REPLY to every KA, and if two KAs go
#     unanswered it considers the client dead and drops the connection.
#
# Here is how our old keepalive went out:
#     TX 192.168.120.101: *KA=01                  <-- WITHOUT prefix
#     TX 192.168.120.101: 099,001,*AR=001A100101  <-- command, with prefix
#     RX 192.168.120.101: 106,*KA=21-0542100...   <-- device, with prefix
# On this wire EVERY frame carries an "<id>," or "<id>,<seq>," prefix.
# Only our keepalive went out without a prefix - most likely the device
# discarded it as malformed, so as far as it was concerned we never
# replied at all.
#
# Since we have no capture of the app, instead of guessing the exact format
# the code LEARNS it: each new connection tries one variant and measures
# how long the session lasts. The variant that breaks through the
# 12-second wall (session > KEEPALIVE_GOOD_SESSION) is locked in
# permanently. The device reconnects every 12 s, so this tuning completes
# by itself within a few minutes.
#
# Even if no variant works it is not a problem - FAST RECONNECT (below)
# reduces the downtime from ~2.3 s to milliseconds, so the user will not
# notice.
# ------------------------------------------------------------------ #
# {seq} is filled in automatically. Order = most likely first.
KEEPALIVE_VARIANTS = (
    "{id},{seq},*KA=01",   # like our command frames (id + seq)
    "{id},*KA=01",         # like the device's own KA frame (id only)
    "*KA=01",              # old behaviour (no prefix) - fallback
)
# A session lasting this long = this variant breaks the 12 s wall.
KEEPALIVE_GOOD_SESSION = 25

# Minimum gap between replies to the device's *KA= - for anti-spam only
# (in case the device ever sends KAs in a burst). The device's own KA
# interval is ~6 s, so with 1 s every KA is easily answered. NOTE: this
# deliberately does NOT depend on "did we send anything else recently" -
# the device specifically needs a reply to its KA; any other traffic is
# not enough.
KA_REPLY_MIN_GAP = 1.0

# ------------------------------------------------------------------ #
# FAST RECONNECT (v1.5.3)
#
# If the device closing at ~12 s is simply its design, treating it as an
# "error" is wrong. In v1.5.2 every EOF was followed by a 1 s backoff +
# 0.5-2.0 s settle = ~2.3 s downtime per cycle, i.e. the connection was
# down 16% of the time (measured). A click landing in those 16% had to
# wait for the reconnect.
#
# Now: if the session lasted a NORMAL length (longer than
# FAST_RECONNECT_MIN_SESSION), it is an expected close - reconnect
# immediately (FAST_RECONNECT_DELAY), with no backoff/settle. Downtime
# drops from ~2.3 s to ~0.1-0.3 s (uptime from ~84% to ~98%).
#
# On a genuine failure (very short session, or connect itself failing) the
# old backoff + settle still applies - otherwise HA would hammer a device
# that really is down.
# ------------------------------------------------------------------ #
FAST_RECONNECT_MIN_SESSION = 8
FAST_RECONNECT_DELAY = 0.05

# ------------------------------------------------------------------ #
# STABILITY (v1.5.0) - the real cure for "connection lost"
#
# What the real logs (10 devices, 9.8 minutes) clearly showed:
#   - 202 TCP connections were opened, 174 (86%) of them by OUR OWN
#     periodic resync. Only 26 were genuine device-side EOFs.
#   - All 26 of those EOFs came WITHIN 30 SECONDS of connecting, median
#     session only 12 seconds. So the device can handle long sessions - it
#     simply never got the chance.
#   - Every device sends its own *KA= frame unprompted every 6-12 seconds
#     (293 RX vs our 125 TX). So the answer to "is the connection alive?"
#     comes from the device for FREE.
#
# Hence the strategy is now reversed: STOP TOUCHING the connection.
#   1. The keepalive is now PASSIVE - we only write when nothing at all
#      has arrived from the device (up to KEEPALIVE_INTERVAL). Under normal
#      conditions our write count drops to 0.
#   2. A "dead" connection is now detected by READING, not by WRITING - it
#      is considered dead only if the device's own heartbeat has not
#      arrived within RX_SILENCE_TIMEOUT. This is faster and does not
#      disturb the module's TCP stack.
#   3. The periodic resync is now only a distant safety net (RESYNC_INTERVAL
#      below), because the device itself broadcasts changes made from the
#      app/switch live as *AR= frames - there is no need to break the
#      connection to get the state.
#   4. On every new connect the old background tasks are CANCELLED (see
#      _cancel_bg_tasks + _conn_generation) - previously every reconnect
#      created a NEW resync task without stopping the old one; they piled
#      up and the 25 s interval effectively became 5-13 s (94% of resync
#      gaps in the logs were under 20 s).
# ------------------------------------------------------------------ #

# How long the listen loop waits on one read. Keeping it short is fine -
# it only decides how quickly silence is checked; it generates no network
# traffic.
LISTEN_READ_TIMEOUT = 10

# As soon as a new connection is established, the device sends its initial
# burst (that is where HA gets fresh state). Whether the burst has ended is
# detected by a short quiet gap - the overall cap is INITIAL_DRAIN_MAX.
# (Before v1.5.2 every connect waited a fixed 2.5 s, which added
# minute-scale delays to startup on setups with 100+ devices.)
INITIAL_DRAIN_QUIET = 0.6
INITIAL_DRAIN_MAX = 2.5

# Our own *KA=01 write is sent ONLY if NOTHING at all has arrived from the
# device for this long. The device's own heartbeat is 6-12 s, so 30 s
# (2.5x margin) is used - under normal conditions our write count stays 0,
# and an occasional late heartbeat does not create useless traffic.
# This is separate from KEEPALIVE_INTERVAL: interval = how often to CHECK,
# threshold = after how much silence to actually WRITE.
KEEPALIVE_IDLE_THRESHOLD = 30

# The device itself sends its *KA= every 6-12 s. If NOTHING at all arrives
# for this long, treat the connection as dead (~6 missed heartbeats - this
# margin keeps a busy device or some Wi-Fi jitter from being mistaken for
# "dead").
RX_SILENCE_TIMEOUT = 75

# A short random gap before every reconnect attempt - two benefits:
#   (a) the module gets time to clean up its old socket
#       (without it the new connect immediately got an EOF - the logs had
#        5 "Failed to connect ... EOF" for exactly this reason),
#   (b) 10 devices do not stampede at the same moment.
RECONNECT_SETTLE_MIN = 0.5
RECONNECT_SETTLE_MAX = 2.0

# ------------------------------------------------------------------ #
# COMMAND DELIVERY (v1.5.2) - the "device reacts on the first click" fix
#
# Problem (from real logs): a Raylogic device sends no ACK for a command
# (in log1, 454 TX commands went out and only 11 RX came back - and those
# 11 were the app's own broadcasts, not replies to our commands). And
# `writer.write() + drain()` "succeeds" SILENTLY over TCP even on a
# half-dead socket - drain() only tells us the data left the OS buffer,
# not that it reached the device. So HA never learned that a command was
# lost - the user had to click a second or third time.
#
# Of the 460 commands in log1, 337 (73%) landed at a moment when the
# connection was either closing or had only just been established, and 159
# (35%) within just 1.5 seconds of that - plenty of opportunity to be
# dropped.
#
# Fix: the device sends no ACK, but TCP does. On Linux the SIOCOUTQ ioctl
# tells how many bytes in the socket's send queue are still UN-ACKED.
# After sending a command we watch this queue in the background (without
# blocking the UI):
#   - queue reaches 0      -> the device's TCP stack has received the
#                              data; the command definitely arrived.
#   - not 0 by the timeout -> the socket really is dead. HA then marks the
#                              connection dead itself, queues the command,
#                              reconnects immediately and re-sends the
#                              command as soon as it is reconnected. The
#                              user does NOT need to click again.
# This verification runs in a background task, so the entity's click
# response stays immediate (no UI lag).
#
# NOTE: SIOCOUTQ is Linux-specific (HA OS/Docker/Supervised are all
# Linux). On any other platform this check is skipped automatically - all
# the other fixes still work there.
# ------------------------------------------------------------------ #
DELIVERY_VERIFY_TIMEOUT = 1.2
DELIVERY_VERIFY_POLL = 0.05

# Sanity check BEFORE sending a command: the device normally sends a frame
# every 6-12 s. If it has been completely silent for this long, treat the
# socket as "suspect" - rather than writing to it and losing the command,
# queue it and refresh the connection first.
LINK_SUSPECT_SECONDS = 30

# Do not replay a queued command that has become this old - by then the
# user has probably done something else, and replaying an old command
# would be confusing.
PENDING_COMMAND_MAX_AGE = 30

# A command is sent at most this many times (first attempt + retries). All
# commands are IDEMPOTENT (they send an absolute level, not a toggle) -
# relay ON, dimmer 60%, curtain open - so re-sending them is completely
# safe. The cap keeps HA from retrying forever against a device that is
# really down.
COMMAND_MAX_ATTEMPTS = 4

# ------------------------------------------------------------------ #
# Client sender-ID - CONFIRMED from real Docklight capture (device
# 192.168.1.34:5550, connected DIRECTLY, no TCP-HUB machine in between).
# Real traffic shows TWO different identities on the wire:
#   "109,...,*KA=..." / "109,...,+AR40=..." -> the MODULE/HUB's OWN
#     identity broadcasting its status. NOT to be reused as our sender id
#     (device ignores/loops commands that claim to be from itself).
#   "099,155,*AR=001A040203" (and 099,158 / 099,159 / 099,160...) -> a
#     CLIENT session's real, working *AR= commands (mobile app's own
#     session sending real accepted commands). This is genuinely honored
#     by the device, so we mirror it for our own outgoing commands.
# Official PDF's bare "*AR=...\r" (no prefix) examples are the LOGICAL
# payload only - real wire traffic always carries this <id>,<seq>, prefix.
# ------------------------------------------------------------------ #
CLIENT_SENDER_ID = "099"
# F1 (v1.6.1) - co-existence with the "raylogic" (main/DIN) integration:
# the main integration sends normal commands as "003" and only scene
# recalls as "099"; this integration sends everything as "099". This is
# NOT a clash:
#   - The sender ID is not a reply address - each integration has its own
#     TCP connection to each module, and the device returns frames on that
#     same connection.
#   - The main integration's ack matching only looks at echoes from node
#     "003"; we never send "003", and we do not use echo acks at all (TCP
#     SIOCOUTQ verification).
#   - Each other's "099" *AR= frames are keypad/app activity for both -
#     which is exactly the intended two-way sync.
# Do NOT change this to a value such as "098" unless confirmed by a capture
# on MOD hardware: the firmware treats node IDs differently (queries are
# only answered for 003, *BS= only on a 099 recall) - with an unknown ID a
# MOD module may silently ignore every command.
# DIAGNOSTIC NOTE: the official Raylogic app connects to the device only
# briefly (while the app is open), not 24/7 - which is why the app never
# shows the disconnect problem. HA, by contrast, stays connected to ALL
# devices ALL the time and pings every KEEPALIVE_INTERVAL seconds - for the
# limited TCP stack of these small/cheap Wi-Fi modules, this "always-on
# connection + frequent pings" load exceeds what their firmware was
# designed for. The interval was raised from 5 s to 15 s to reduce
# fleet-wide protocol chatter (10 devices, from every 5 s to every 15 s)
# without losing responsiveness (HA's optimistic update + real-time *AR=
# listener already update the UI immediately; this keepalive only checks
# whether the connection is alive).
KEEPALIVE_INTERVAL = 15

# ------------------------------------------------------------------ #
# Periodic resync (soft-reconnect)
#
# Confirmed via a real-world test: the device reports its CORRECT,
# up-to-date state only in the initial burst of a NEW connection (which is
# why the Raylogic app shows the correct status after being closed and
# reopened, even for a change made from HA). The device does NOT
# broadcast state changes of any channel (relay or dimmer) live to other
# sessions that are already connected.
#
# HA therefore closes and reopens its own connection periodically (in the
# background, with no interruption to entities/commands) - exactly the
# same effect as reopening the app - so that both directions (a change made
# in the app shows in HA, and a change made in HA shows in the app) sync
# within a few seconds, without relying on a live push.
# ------------------------------------------------------------------ #
# STABILITY FIX: real-world logs showed that this periodic resync was
# itself the largest source of the disconnect cycle - every 45 s EVERY
# device closed and reopened its TCP connection, and many small/cheap
# embedded modules could not handle this repeated close/reopen promptly
# (they closed the new connection as soon as it opened - "the peer closed
# the connection (EOF)"), so the device kept showing "Unavailable". The
# interval was raised from 45 s to 180 s (3 minutes) - a change made in the
# app still syncs to HA within a few minutes, while the close/reopen load
# on the device dropped ~4x, which proved much more stable in practice.
# DIAGNOSTIC NOTE (from real logs): the devices were disconnecting by
# themselves / from the network side every ~30-40 s - more often than our
# resync interval (180 s), so our resync is NOT the MAIN source of the real
# disconnects. The interval was therefore raised to 600 s (10 min) so the
# resync itself cannot cause any extra churn - and if disconnects kept
# arriving at the same rate, that would confirm they are purely
# network/device-side (Wi-Fi/router/power) and cannot be stopped by a
# code fix.
# SOFTWARE-HUB STRATEGY (without buying any hardware): the official
# Raylogic app connects to the device only BRIEFLY - never 24/7 - and shows
# no disconnect problem (confirmed by the user). HA did the opposite: it
# kept one connection alive 24/7, which the small/cheap Wi-Fi chip of these
# modules could not handle, so the module broke it by itself in an
# UNCONTROLLED way (random EOF).
#
# Fix: instead of buying external hardware such as a HUB-1, HA now follows
# the same short-session pattern as the app - every RESYNC_INTERVAL seconds
# it closes and reopens the connection itself, in a CONTROLLED way (see
# _resync_loop/_soft_reconnect) - so the device never sees a connection
# alive long enough for its stack to get "confused" and send an EOF by
# itself. The interval was lowered from 600 s (a rare touch-up) to 25 s,
# so HA always cycles gracefully BEFORE the device's tolerance limit
# (~30-40 s, as seen in real logs). The grace period
# (UNAVAILABLE_GRACE_SECONDS) again keeps the UI from flickering - this
# cycling must remain invisible in the background.
#
# FINAL FIX (v1.5.0): the "short-session" theory above turned out to be
# WRONG in real logs. The data says:
#   - The device handles sessions of 30 s+ perfectly well. All 26 EOFs
#     came WITHIN 30 SECONDS of connecting (median session 12 s) - so the
#     device was not BREAKING on long sessions, it simply never GOT a long
#     session, because we were breaking the connection ourselves every
#     25 s (and, due to duplicate loops, in practice every 5-13 s).
#   - The device DOES broadcast its state live (operating a curtain from
#     the app delivered an *AR= frame on HA's running connection) - so the
#     original justification for the resync was wrong.
# The resync is therefore now only a distant safety net (15 minutes);
# normal state sync happens through the live push. The _conn_generation
# guard was added at the same time so old/duplicate resync loops never run.
#
# FINAL (v1.5.4): the periodic resync is now OFF (0 = off).
#
# The v1.5.3 real log confirmed all of this:
#   - Device-side EOFs: down from 301 to 0. Sessions lasted up to 545
#     seconds (previously all died at 12 s) - the keepalive fix really
#     broke through the 12-second wall.
#   - In that log the ONLY remaining source of broken connections was our
#     own resync: 9 resyncs, each leaving the device DOWN for ~1.8 s (max
#     2.6 s). A click landing in that window waited in the queue - this was
#     the only remaining "lag".
#   - The resync is no longer needed: the device itself broadcasts changes
#     made from the app/switch live as *AR= frames, and the connection is
#     now stable at the TCP level (TCP guarantees that no frame is lost
#     while connected). If the connection ever really breaks, the device's
#     initial burst after reconnecting provides fresh state anyway.
# Hence 0 = completely off. Any non-zero value re-enables it (the code
# still honours that value).
RESYNC_INTERVAL = 0

# STABILITY FIX: during a resync the old socket used to be closed and a new
# connect() attempted IMMEDIATELY (0 second wait) - many devices do not
# accept a connection again that quickly (they need some time to fully
# clean up their old socket), so the new connect() also failed immediately
# with an EOF. Now, after closing, a short "breathing" gap is inserted
# (~1-2 s, random per device so that not all devices synchronise), giving
# the device a chance to clean up its old socket.
SOFT_RECONNECT_SETTLE_MIN = 1.0
SOFT_RECONNECT_SETTLE_MAX = 2.5


# ------------------------------------------------------------------ #
# Area scenes (v1.6.0) - Raylogic GO app area scenes
# Wire format (confirmed from live captures made for the reference
# "raylogic" integration): *AR=000F <area> <scene> 00 - this is a BUS
# BROADCAST; every fixture in that Area reacts. When a scene is recalled
# from a keypad/the app, this same frame is echoed to the other nodes -
# which gives the select entity its two-way feedback. The recall is sent
# under CLIENT_SENDER_ID (099), the app/keypad node (the reference
# integration also recalls scenes from this node so the scene shows as
# "selected" in the app).
# ------------------------------------------------------------------ #
SCENE_FUNC = 0x0F
SCENE_AREAS = 16          # areas 1..16 (same as AREA_MIN..AREA_MAX)
SCENE_MAX = 64            # scene number 1..64
CONF_SCENE_COUNTS = "scene_counts"

# Which entry the scene/select entities were created on, and from which
# merged map - hass.data[SCENE_DATA_KEY] = {"host": entry_id, "map": {...}}.
# A separate key is used so that hass.data[DOMAIN] only ever contains
# RaylogicModDevice objects (older code reads them by entry_id).
SCENE_DATA_KEY = f"{DOMAIN}_scenes"

# Global (entry-independent) dispatcher signal. Scene feedback can come
# from any module (whichever hears that bus frame), so it is not
# per-entry - but only scene/select entities listen to it (as many as
# areas x scenes), not channel entities, so the scale fix (per-entry
# signals) is unaffected.
SIGNAL_AREA_SCENE = f"{DOMAIN}_area_scene"


def parse_scene_map(text) -> dict:
    """"area:scene,scene; area:scene" -> {area: [scenes]}.

    Example: "12:1,2,3; 5:1,4" = scenes 1,2,3 in Area 12 and 1,4 in Area 5.
    Empty text = no scenes. Invalid tokens are skipped silently, ranges
    are clamped, and it never raises - a typo can never break setup."""
    result: dict[int, list] = {}
    for block in str(text or "").replace("\n", ";").split(";"):
        area_str, sep, scenes_str = block.strip().partition(":")
        if not sep:
            continue
        try:
            area = int(area_str.strip())
        except ValueError:
            continue
        if not 1 <= area <= SCENE_AREAS:
            continue
        scenes = set()
        for tok in scenes_str.split(","):
            try:
                num = int(tok.strip())
            except ValueError:
                continue
            if 1 <= num <= SCENE_MAX:
                scenes.add(num)
        if scenes:
            result.setdefault(area, [])
            result[area] = sorted(set(result[area]) | scenes)
    return result


def merge_scene_maps(option_values) -> dict:
    """Union of the scene maps of several devices - area scenes are global,
    so the scene config can be entered in the Configure dialog of ANY one
    device."""
    merged: dict[int, set] = {}
    for text in option_values:
        for area, scenes in parse_scene_map(text).items():
            merged.setdefault(area, set()).update(scenes)
    return {area: sorted(scenes) for area, scenes in sorted(merged.items())}


# ------------------------------------------------------------------ #
# Automatic discovery (v1.6.6) - background LAN scan
# The AUTO_SCAN pattern of the reference "raylogic" integration (only read,
# nothing was changed there): the first scan runs shortly after HA starts,
# then at a fixed interval. Every new module becomes a "Discovered" card in HA.
# ------------------------------------------------------------------ #
AUTO_SCAN_FIRST_DELAY = 60        # s - does not slow down HA startup
AUTO_SCAN_INTERVAL = 1800         # s - 30 min (the user's choice)
AUTO_SCAN_MAX_SUBNETS = 4         # max subnets per scan (~1000 hosts)
CONF_AUTO_DISCOVERY = "auto_discovery"
