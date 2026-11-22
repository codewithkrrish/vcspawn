"""vcspawn — temporary voice channels for discord.py bots.

Join a lobby, get your own room. Leave it empty, it deletes itself.
"""

from .cog import TempVCCog
from .naming import DEFAULT_TEMPLATE, PLACEHOLDERS
from .store import LobbyConfig, TempChannel, TempVCStore

__all__ = [
    "TempVCCog",
    "LobbyConfig",
    "TempChannel",
    "TempVCStore",
    "DEFAULT_TEMPLATE",
    "PLACEHOLDERS",
]

__version__ = "1.0.0"
