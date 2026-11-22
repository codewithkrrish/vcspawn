"""The decisions behind vcspawn, as pure functions.

Everything in here takes plain values in and returns plain values out —
no discord objects, no I/O. The cog asks these questions and then does
the Discord work itself, which keeps the rules testable without a bot
and keeps the cog thin.

The two questions that matter:

* ``next_channel_number`` — what ``{n}`` should a new channel get?
  Smallest free number, so a lobby that has channels 1 and 3 open
  hands out 2 next, not 4.
* ``should_delete`` — is this channel empty right now? Only an empty
  channel gets deleted, and only after the grace period the lobby set.

Ownership is deliberately *not* auto-transferred here: when the owner
leaves, the room goes ownerless and someone claims it with ``!vc claim``.
Passing ownership to whoever happened to join first would surprise
people, so there is no "pick the next owner" rule to test.
"""

from __future__ import annotations

__all__ = [
    "next_channel_number",
    "should_delete",
    "is_owner",
]


def next_channel_number(used_numbers: list[int]) -> int:
    """Smallest positive integer not already in use.

    ``used_numbers`` is the ``{n}`` values of the lobby's channels that
    are still alive. Gaps get reused first — a lobby feels less messy
    when its channels are 1, 2, 3 instead of 1, 7, 42.
    """
    taken = {n for n in used_numbers if n > 0}
    number = 1
    while number in taken:
        number += 1
    return number


def should_delete(member_count: int) -> bool:
    """True only when nobody is left in the channel."""
    return member_count == 0


def is_owner(owner_id: int | None, member_id: int) -> bool:
    """True when *member_id* owns the channel.

    An ownerless channel (owner left, nobody claimed it yet) has no
    owner, so nobody passes this check — that's what forces people to
    use ``!vc claim`` instead of silently inheriting control.
    """
    return owner_id is not None and owner_id == member_id
