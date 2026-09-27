# Raylogic MOD2U / MOD4U / MOD2F - Home Assistant Integration

> **Unofficial community integration** - not affiliated with or endorsed by Raylogic Control Systems Pvt. Ltd. 'Raylogic' and its logo are trademarks of their owner.

A single integration for all three devices:

- **MOD2U** - 2 physical channels, 1 pair, universal type (Relay/Dimmer/Fan/Curtain/CCT)
- **MOD4U** - 4 physical channels, 2 pairs, universal type (Relay/Dimmer/Fan/Curtain/CCT)
- **MOD2F** - 1 physical channel, **fixed Fan type** (no Select Type option -
  the device is always a Fan; the config only asks for Area + channel number)

RE8-style config-flow architecture (Relay / Dimmer / Fan / Curtain / CCT
per channel, Fan only for the MOD2F). When adding a device, choose MOD2U,
MOD4U or MOD2F from the "Device Model" dropdown - the rest of the config
screen then shows only as many channel fields as that model has (MOD2U = 2,
MOD4U = 4, MOD2F = 1 and no Type dropdown).

Channels come in **PAIRS**:

- **Pair 1** = Channel 1 (+ Channel 2 on MOD4U)
- **Pair 2** = Channel 3 + Channel 4 (MOD4U only)

Relay, Dimmer and Fan can be set **independently** on every channel.
Curtain and CCT (colour temperature) are both **paired** modes - when one
channel of a pair is set to Curtain or CCT, the whole pair (both physical
channels) is consumed internally by a single logical entity (no 2 separate
entities are created).

## Setup

1. Copy `custom_components/raylogic_mod` into your HA `config/custom_components/`.
2. Restart Home Assistant.
3. Settings -> Devices & Services -> Add Integration -> "Raylogic MOD2U / MOD4U / MOD2F".
4. Step 1: IP Address, Port (default 5550) and **Device Model** (MOD2U/MOD4U/MOD2F).
5. Step 2 (MOD2U/MOD4U):
   - **Area** - the Area shown on the device's Mod Settings screen (e.g.
     12, or 16 for a CCT pair). Only use `0` when ALL channels are Relay -
     HA learns the Area by itself the first time a channel is toggled from
     the app/switch. Dimmer/Fan/Curtain/CCT require the Area to be entered
     manually.
   - **Channel 1-N Type** - what you set on the "Select Type" screen of
     the Raylogic GO app: `relay`, `dimmer`, `fan`, `curtain` or `cct`.
     The device does not report its own type, so it has to be entered here
     once.
   - **CCT / Curtain (paired types)**: setting Channel 1 (or 3) Type to
     `cct`/`curtain` makes the Channel 2 (or 4) Type field of that pair
     automatically ignored - a single entity is created, not two
     conflicting ones. For CCT, also select the Driver Mode (Single/Double,
     as shown on the app's Mod Settings screen) - independently per pair.
5. Step 2 (MOD2F): only 2 fields - **Area** (as shown in the device app,
   e.g. 4) and **First Channel Number** (the same as the device app's
   "Start Address", e.g. `9`). No Type dropdown - Fan is always fixed. Area
   `0` does not work here (Fan does not support Learn mode, only Relay
   does) - the integration blocks this if Area is left at 0.

## Bug fixes in this version (merged from both files)

1. **THE MAIN FIX - HA permanently "stuck" after a device power cycle**:
   previously a connection-loss retry happened only **ONCE** (after 30
   seconds) - if that single attempt failed (e.g. the device was still
   rebooting, which is perfectly normal during a real-world power cycle),
   the integration stayed disconnected **forever**, even after the device
   came back up and kept answering pings. `_reconnect()` is now a proper
   loop - as soon as the device is reachable again (on the next 30 s retry
   cycle) it reconnects automatically, with no need to restart/reload HA.
2. **CCT pair-derivation bug** (MOD4U): a CCT channel's pair was always
   assumed to be `(channel_start, channel_start+1)` - fine on the MOD2U
   (only one pair), but because of this the CCT on the MOD4U's 2nd pair
   (Channel 3-4) used the wrong physical channels. Each CCT entity now
   derives its own pair.
3. **CCT same-Area disambiguation** (MOD4U): if 2 Single-Driver CCT pairs
   are configured in the same Area, incoming frames are now matched to the
   correct pair (by the real channel number on the wire) instead of simply
   returning the first match.
4. **Curtain - both pairs confirmed** (MOD4U): from a real MOD4U capture
   (Area 7), the curtain open/close/stop bytes are now confirmed for both
   Pair 1 **and** Pair 2 (previously only Pair 1 was, and even that was
   guessed from a MOD2U capture that turned out to be wrong - the real
   MOD4U byte layout is different: pair-marker byte 0x02/0x03).
5. Event-loop-blocking disk I/O (learned-channel save/load) was moved to a
   background thread - the whole of HA no longer freezes, even on slow
   SD-card/Pi storage.
6. Duplicate/overlapping TCP connections (when a read error, a write error
   and the periodic resync trigger at the same time) are now prevented by a
   connect lock - the device itself used to get confused and hang.
7. **BR40 log spam fix**: the "BR40 auto-discovery failed" WARNING used to
   reappear on EVERY periodic resync (~45 s), forever (even when the device
   was working perfectly) - because none of MOD2U/MOD4U/MOD2F has a
   `br40_code` set, and these devices never answer the `?BR40=` query with
   `+BR40=` (they only send an unsolicited `+AR40=` heartbeat). This
   message now appears as a WARNING only once per connection lifetime, and
   at DEBUG after that.
8. **MOD2F support added**: a new "single-channel, fixed Fan type" model.
   The wire protocol is the same as MOD2U/MOD4U (`00 1A <area> <level> <channel>`,
   level 01=off/02-05=speed 1-4), so `protocol.py` needed no new command
   code at all - only a new model entry in `const.py` and a small special
   case in `config_flow.py`/`__init__.py` ("do not show a Type dropdown for
   this model, the type is always Fan").
9. **THE STARTUP-SLOW ROOT CAUSE - duplicate config entries for the same
   device**: the config flow's `unique_id` used to be non-deterministic -
   if the device's `*KA=` line arrived within the 5-second validation
   window while adding it, the `unique_id` became a MAC-like string,
   otherwise just the raw host string. Because of this, if the same
   physical device (same IP:port) was ever added again (or migrated from
   the old standalone MOD2U/MOD4U integration), the two attempts could end
   up with DIFFERENT `unique_id`s - so duplicate detection
   (`_abort_if_unique_id_configured`) never caught the duplicate, and TWO
   config entries were created for the same IP. If the device only accepts
   one TCP client at a time (as appears to be the case), both entries
   "fight" each other for the connection - this was the real reason HA
   startup was slow/flaky. The `unique_id` is now always built
   deterministically from `host:port` only - a second "Add" of the same
   IP:port always aborts immediately with "already configured".

   **IMPORTANT**: this fix only prevents new duplicates from being created
   - if 2 entries already exist for the same IP, find them yourself under
   Settings -> Devices & Services (e.g. "Raylogic MOD2U 192.168.1.34"
   appears twice) and delete the extra one.

## MOD2F

A dedicated single-channel Fan module - the Raylogic GO app has no "Select
Type" screen for it; the module is always a Fan. So the HA config flow does
not show a Channel Type dropdown for this model either, just:

- **Area** - as shown in the "Area" field of the device app (e.g. `4`).
- **First Channel Number** - the same as the device app's "Start Address"
  (e.g. `9`, the value used as the channel byte in the *AR= frame).

Area `0` (auto-learn) does not work on the MOD2F (it is for Relay devices
only) - the config flow blocks this combination by itself.

## Learn mode (Relay only, Area = 0)

If the Area is left at `0` and the channel is a Relay, HA passively listens
for `*AR=` frames (when you toggle from the app/a physical switch), learns
the Area and creates the entity immediately (no restart needed). This does
NOT work for Dimmer/Fan/Curtain/CCT - they require the Area to be entered
manually.

## Repairs

If a pair's curtain bytes are not confirmed (in a future device variant),
the entity is not created and an issue appears under Settings > Repairs
with the exact steps for capturing and sharing the data.
