"""Protocol (slice 18.3): the numbered version of what the Hub and a Relay, or the Hub and an
Actuator, exchange, each part's counted apart from 1. A part sends its number in a header on
its connection (the Relay) or on every call (the Actuator, with its release version beside
it); none is 1, today's wire (0.62.0 and older send none). The Hub accepts its own and the
one before and answers any other 426 with its own, and the part says which side to upgrade.
Raise a part's number only for a change an older part would misread (a field removed or
renamed, or its meaning changed); an added optional field keeps it.

The Hub keeps what each part last said in memory (`SEEN`): a refused one is an Agenda item
until its next accepted contact, and an Actuator's version and Protocol show in Health. A
restart forgets it; a refused part tries again within minutes."""

from dataclasses import dataclass
from typing import Literal

Part = Literal["Relay", "Actuator"]
HEADER = "Squero-Protocol"
VERSION = "Squero-Version"  # the Actuator's release version (a Relay's comes in its MCP handshake)
OWN: dict[Part, int] = {"Relay": 1, "Actuator": 1}


def spoken(value: str | None) -> int | None:
    """A part's Protocol from its header: none is 1; anything but a small number, None."""
    if value is None:
        return 1
    v = value.strip()
    return int(v) if v.isascii() and v.isdigit() and len(v) <= 6 else None  # not ²


def needs(own: int) -> str:
    """What a side speaking `own` accepts, in words: its own and the one before."""
    return f"{own - 1} or {own}" if own > 1 else str(own)


def accepts(part: Part, n: int | None) -> bool:
    return n is not None and OWN[part] - 1 <= n <= OWN[part] and n >= 1


def refusal(part: Part, cluster: str, n: int | None) -> str:
    """The Agenda's line for a part the Hub refused: which side to upgrade."""
    if n is None:
        return f"{part} in {cluster} sends no readable Protocol, the Hub needs {needs(OWN[part])}"
    if n < OWN[part]:
        return (
            f"{part} in {cluster} speaks Protocol {n}, the Hub needs {needs(OWN[part])}: upgrade it"
        )
    return f"{part} in {cluster} speaks Protocol {n}, this Hub {OWN[part]}: upgrade the Hub"


def told(part: Part, hub: str | None) -> str:
    """A part's own line when the Hub answered 426, from the Hub's Protocol in that answer
    (none: not a Hub that knows Protocols, so no side to name)."""
    mine, theirs = OWN[part], spoken(hub) if hub is not None else None
    if theirs is None:
        return f"the Hub refused this {part}'s Protocol {mine}"
    side = "the Hub" if mine > theirs else f"this {part}"
    return f"the Hub speaks Protocol {needs(theirs)}, this {part} {mine}: upgrade {side}"


@dataclass
class Seen:
    protocol: int | None
    at: float
    version: str | None = None  # as the part says: shown, never trusted
    refused: bool = False


# What each part last said, by (contract, cluster, part): one process, one registry, as
# relay.HUB holds the connected Relays.
SEEN: dict[tuple[str, str, Part], Seen] = {}


def see(
    cid: str, cluster: str, part: Part, header: str | None, at: float, version: str | None = None
) -> Seen:
    """Note what a part said; refused when the Hub doesn't accept its Protocol."""
    n = spoken(header)
    seen = Seen(n, at, version[:40] if version else None, not accepts(part, n))
    SEEN[(cid, cluster, part)] = seen
    return seen
