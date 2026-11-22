"""Channel-name templates for vcspawn.

A lobby's name template is a plain string with ``{placeholders}`` in it,
for example ``"{user}'s room"`` or ``"squad {n} — {game}"``. This module
is pure string work on purpose: it has no discord imports, so it can be
tested without a bot, a token, or a network connection.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field

__all__ = [
    "PLACEHOLDERS",
    "DEFAULT_TEMPLATE",
    "MAX_CHANNEL_NAME_LENGTH",
    "RenderResult",
    "render_template",
    "sanitise_channel_name",
    "values_for_member",
]

#: Placeholders a template may use, with a one-line explanation each.
PLACEHOLDERS: dict[str, str] = {
    "user": "the member's display name",
    "username": "the member's username (without the @)",
    "game": "what the member is playing right now, or 'chilling'",
    "n": "the channel's number for this lobby (1, 2, 3, ...)",
    "count": "how many people are in the new channel (starts at 1)",
}

#: What a fresh lobby names its channels before an admin changes it.
DEFAULT_TEMPLATE = "{user}'s room"

#: Discord caps channel names at 100 characters.
MAX_CHANNEL_NAME_LENGTH = 100

# Anything Discord strips or that breaks a channel name on sight:
# newlines, tabs and other control characters, plus runs of spaces.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE_RUN = re.compile(r"\s+")


@dataclass
class RenderResult:
    """The outcome of rendering one template."""

    text: str
    unknown: list[str] = field(default_factory=list)

    @property
    def has_unknown(self) -> bool:
        """True when the template used a placeholder we don't know."""
        return bool(self.unknown)


def render_template(template: str, values: dict[str, object]) -> RenderResult:
    """Fill ``{placeholders}`` in *template* from *values*.

    Unknown placeholders are left exactly as written and reported in
    ``RenderResult.unknown`` — silently dropping them would hide a typo
    from the admin who typed it, and guessing would be worse.
    """
    unknown: list[str] = []
    parts: list[str] = []

    for literal, field_name, _, _ in string.Formatter().parse(template):
        parts.append(literal)
        if field_name is None:
            continue
        if field_name in values:
            parts.append(str(values[field_name]))
        else:
            unknown.append(field_name)
            parts.append("{" + field_name + "}")

    return RenderResult(text="".join(parts), unknown=unknown)


def sanitise_channel_name(name: str) -> str:
    """Make *name* safe to hand to Discord as a voice channel name.

    Control characters go, whitespace collapses to single spaces, and
    anything over the 100-character cap is cut. A name that ends up
    empty (someone's display name was only emoji Discord rejects, or
    the template rendered to whitespace) falls back to ``"voice-chat"``
    rather than asking Discord to create a nameless channel.
    """
    cleaned = _CONTROL_CHARS.sub(" ", name)
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned).strip()
    cleaned = cleaned[:MAX_CHANNEL_NAME_LENGTH].strip()
    return cleaned or "voice-chat"


def values_for_member(member: object, number: int) -> dict[str, object]:
    """Build the placeholder values for one member joining a lobby.

    ``member`` is typed as ``object`` and read with ``getattr`` so this
    stays testable with plain fakes; the cog passes a real
    ``discord.Member``.
    """
    display_name = str(getattr(member, "display_name", "someone"))
    username = str(getattr(member, "name", display_name))

    # Activity is the first thing that can be None, a Game, a Custom
    # Activity, or a plain string depending on the client — read it
    # defensively and fall back to something that still reads well in
    # a channel name.
    game = "chilling"
    activity = getattr(member, "activity", None)
    if activity is not None:
        activity_name = str(getattr(activity, "name", "") or "").strip()
        if activity_name:
            game = activity_name

    return {
        "user": display_name,
        "username": username,
        "game": game,
        "n": number,
        "count": 1,
    }
