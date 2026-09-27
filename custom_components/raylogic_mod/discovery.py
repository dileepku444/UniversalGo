"""LAN discovery for Raylogic MOD2U / MOD4U / MOD2F modules (v1.6.0).

Raylogic modules do not implement any standard discovery (mDNS/SSDP/
broadcast) - confirmed by live captures made for the reference "raylogic"
integration. However, every module listens on TCP 5550 and sends its own
"*KA=" identity frame as soon as a client connects. So a short TCP connect
to every host of the subnet is made, and any host that sends a Raylogic
frame is a "hit".

A MOD module's *KA= payload also reports its Area and channel range (the
same decoding used by protocol._parse_ka_identity), so a scan hit also
suggests the model (2 ch = MOD2U, 4 ch = MOD4U, 1 ch = MOD2F), the Area and
the First Channel Number.

Safety: hosts that are already configured in Home Assistant (in raylogic_mod
OR in the reference "raylogic" integration) are never touched by the scan,
so it can never interfere with a live connection. The scan runs when the
user chooses Add device -> Scan network, and (since v1.6.6) as the periodic
background auto-discovery started from __init__.py.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket

from .const import (
    AREA_MAX, AREA_MIN, CLOSE_TIMEOUT, DEFAULT_PORT, DEVICE_MODELS,
)

_LOGGER = logging.getLogger(__name__)

# Only a genuine Raylogic frame counts as a hit: "<node>,*KA=..." (keepalive,
# no message number) or something like "<node>,<msg>,*AR=...". Any other
# appliance listening on 5550 is ignored.
_FRAME_RE = re.compile(r"^(\d{1,3}),(?:(\d+),)?([*+][A-Z]{2}\d{0,2}=)(.*)$")

CONNECT_TIMEOUT = 1.5     # a LAN host answers a SYN well within this
# An idle module sends *KA= about every 6 s - wait slightly longer than that;
# reading stops immediately once a KA arrives.
BANNER_TIMEOUT = 7.5
MAX_CONCURRENCY = 64      # 254 hosts in ~8 s
MAX_HOSTS = 1024          # never scan more than a /22, even by mistake

_COUNT_TO_MODEL = {
    info["channel_count"]: key for key, info in DEVICE_MODELS.items()
}


def decode_mod_ka(payload: str) -> dict | None:
    """MOD *KA= payload -> {"area", "start", "end"} (or None).

        *KA=<xx>-<ctr:3><n:1><AREA:2h><01><0><START:2h><END:2h>0000
        e.g. *KA=21-05421001001020000 -> area 16, channels 1-2 (MOD2U)

    Same layout and validation as protocol._parse_ka_identity."""
    if "-" not in payload:
        return None
    body = payload.split("-", 1)[1].strip()
    try:
        if len(body) < 17 or body[6:8] != "01":
            return None
        area = int(body[4:6], 16)
        start = int(body[9:11], 16)
        end = int(body[11:13], 16)
    except (ValueError, IndexError):
        return None
    if not (AREA_MIN <= area <= AREA_MAX and 1 <= start <= end <= 255):
        return None
    return {"area": area, "start": start, "end": end}


def parse_banner(lines: list[str]) -> dict | None:
    """First frames sent by a module after connecting -> hit dict, or None
    if none of them is a Raylogic frame."""
    info: dict = {"node": None, "area": None, "channel_start": None,
                  "channel_count": None, "model": None}
    hit = False
    for raw in lines:
        m = _FRAME_RE.match(raw.strip())
        if not m:
            continue
        hit = True
        node, _msg, cmd, payload = m.groups()
        info["node"] = info["node"] or node
        if cmd == "*KA=" and info["area"] is None:
            ka = decode_mod_ka(payload)
            if ka:
                info["area"] = ka["area"]
                info["channel_start"] = ka["start"]
                info["channel_count"] = ka["end"] - ka["start"] + 1
                info["model"] = _COUNT_TO_MODEL.get(info["channel_count"])
    return info if hit else None


async def async_probe_host(host: str, port: int = DEFAULT_PORT,
                           connect_timeout: float = CONNECT_TIMEOUT,
                           banner_timeout: float = BANNER_TIMEOUT) -> dict | None:
    """Connect once, read what the module sends, close. None = not a Raylogic module."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), connect_timeout)
    except Exception:
        return None
    lines: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + banner_timeout
    buf = b""
    try:
        while len(lines) < 12 and len(buf) < 4096:
            # "\r", "\n" or "\r\n" - same as protocol._read_frame (v1.6.4)
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()               # incomplete line
            new = [p.decode(errors="replace").strip() for p in parts]
            lines.extend(x for x in new if x)
            if any("*KA=" in x for x in new):
                break   # identity frame received
            left = deadline - loop.time()
            if left <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(1024), left)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
    finally:
        # Closing is hard-capped as well (like config_flow.validate_connection)
        # so the scan never hangs if the device does not send a FIN.
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), float(CLOSE_TIMEOUT))
        except Exception:
            pass
    info = parse_banner(lines)
    if info is None:
        return None
    info.update({"host": host, "port": port})
    return info


def hosts_in(subnets: list[str]) -> list[str]:
    """CIDR strings -> host addresses (deduplicated, capped, IPv4 only)."""
    out: list[str] = []
    seen: set[str] = set()
    for cidr in subnets:
        try:
            net = ipaddress.ip_network(str(cidr).strip(), strict=False)
        except ValueError:
            continue
        if net.version != 4 or net.num_addresses > MAX_HOSTS + 2:
            continue
        hosts = list(net.hosts()) or [net.network_address]
        for ip in hosts:
            s = str(ip)
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _fallback_subnet() -> list[str]:
    """The /24 of the default-route interface (a UDP 'connect' sends no
    packet, it only selects the source address)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 53))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return [str(ipaddress.ip_network(f"{ip}/24", strict=False))]
    except Exception:
        return []


async def async_local_subnets(hass) -> list[str]:
    """CIDR of every enabled IPv4 adapter in Home Assistant (/24 for very large LANs)."""
    subnets: list[str] = []
    try:
        from homeassistant.components.network import async_get_adapters
        for adapter in await async_get_adapters(hass):
            if not adapter.get("enabled"):
                continue
            for ipv4 in adapter.get("ipv4", []):
                addr = ipv4.get("address", "")
                prefix = int(ipv4.get("network_prefix", 24) or 24)
                if not addr or addr.startswith("127."):
                    continue
                if prefix < 22:
                    prefix = 24
                net = str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))
                if net not in subnets:
                    subnets.append(net)
    except Exception:
        pass
    if not subnets:
        subnets = await hass.async_add_executor_job(_fallback_subnet)
    return subnets


OTHER_DOMAIN = "raylogic"   # main/DIN integration - read only


def _entry_hosts(hass, domain: str) -> set[str]:
    hosts: set[str] = set()
    for e in hass.config_entries.async_entries(domain):
        h = {**e.data, **e.options}.get("host")
        if h:
            hosts.add(str(h).strip())
    return hosts


def other_integration_hosts(hass) -> set[str]:
    """IPs configured in the 'raylogic' (main) integration. If the same module
    were configured in both integrations, two TCP connections would be opened
    to one device."""
    return _entry_hosts(hass, OTHER_DOMAIN)


def configured_hosts(hass) -> set[str]:
    """IPs already configured in Home Assistant, in raylogic_mod AND in
    'raylogic'. The scan never connects to them."""
    return _entry_hosts(hass, "raylogic_mod") | other_integration_hosts(hass)


def ignored_hosts(hass) -> set[str]:
    """When the user presses "Ignore" on a "Discovered" card, an entry with
    source=ignore remains (without data). The host is taken from its
    unique_id ("<host>_<port>") so the auto-scan never touches it again."""
    hosts: set[str] = set()
    for e in hass.config_entries.async_entries("raylogic_mod", include_ignore=True):
        if e.source == "ignore" and e.unique_id and "_" in e.unique_id:
            hosts.add(e.unique_id.rsplit("_", 1)[0])
    return hosts


async def async_auto_subnets(hass, max_subnets: int = 4) -> list[str]:
    """Subnets for the background scan - the user does not have to type anything:
      1. Home Assistant's own enabled IPv4 adapters (as in async_local_subnets)
      2. the /24 of every configured module (raylogic_mod + raylogic), which
         also covers modules on another VLAN that Home Assistant routes to
    Duplicates removed, at most `max_subnets`, total hosts <= MAX_HOSTS."""
    subnets = list(await async_local_subnets(hass))
    for h in sorted(configured_hosts(hass)):
        try:
            ip = ipaddress.ip_address(h)
        except ValueError:
            continue    # hostname - skip
        if ip.version != 4 or ip.is_loopback:
            continue
        net = str(ipaddress.ip_network(f"{h}/24", strict=False))
        if net not in subnets:
            subnets.append(net)
    out: list[str] = []
    total = 0
    for net in subnets:
        n = len(hosts_in([net]))
        if not n or len(out) >= max_subnets or total + n > MAX_HOSTS:
            continue
        out.append(net)
        total += n
    return out


async def async_scan(subnets: list[str], *, port: int = DEFAULT_PORT,
                     skip: set[str] | None = None,
                     concurrency: int = MAX_CONCURRENCY) -> list[dict]:
    """Probe every host of the subnets; returns the Raylogic hits in IP order."""
    skip = skip or set()
    targets = [h for h in hosts_in(subnets) if h not in skip]
    sem = asyncio.Semaphore(concurrency)

    async def one(host: str) -> dict | None:
        async with sem:
            return await async_probe_host(host, port)

    results = await asyncio.gather(*(one(h) for h in targets),
                                   return_exceptions=True)
    hits = [r for r in results if isinstance(r, dict)]
    hits.sort(key=lambda r: ipaddress.ip_address(r["host"]))
    _LOGGER.debug("Raylogic MOD scan of %s: %d hosts, %d hits",
                  subnets, len(targets), len(hits))
    return hits


def describe(hit: dict) -> str:
    """One line for the pick-list: IP, node, model, area, channels."""
    parts = [hit["host"]]
    if hit.get("node"):
        parts.append(f"node {hit['node']}")
    model = hit.get("model")
    parts.append(DEVICE_MODELS[model]["name"] if model else "unknown model")
    if hit.get("area") is not None:
        start = hit["channel_start"]
        end = start + hit["channel_count"] - 1
        parts.append(f"area {hit['area']}, ch {start}-{end}")
    return " - ".join(parts)

