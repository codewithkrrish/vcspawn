"""Small JSON store for vcspawn.

Two things live on disk, both in one file so there's only one thing to
back up or move:

* lobbies — per guild, which voice channels are "hubs" and how their
  temp channels should look (name template, user limit, bitrate,
  category, delete delay).
* temp channels — every channel the cog created, who owns it, and which
  lobby it came from. This is what lets the cog clean up after a
  restart instead of leaking channels forever.

Writes are atomic (temp file in the same directory, then ``os.replace``)
so a crash mid-save can't leave half a JSON file behind. A corrupt file
is an error, not a silent reset: the cog logs it and keeps running
in-memory, because quietly dropping every lobby config would be worse
than telling the admin something is wrong.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

__all__ = [
    "LobbyConfig",
    "TempChannel",
    "StoreError",
    "TempVCStore",
    "DEFAULT_STORE_PATH",
]

log = logging.getLogger("vcspawn.store")

#: Where the store lives when nobody passes an explicit path.
DEFAULT_STORE_PATH = Path("vcspawn.json")

#: How long a lobby's channel waits empty before being deleted.
DEFAULT_DELETE_DELAY = 10


class StoreError(Exception):
    """The store file couldn't be read or written."""


@dataclass
class LobbyConfig:
    """How one lobby (hub) channel's temp channels should behave."""

    template: str = "{user}'s room"
    user_limit: int = 0  # 0 means Discord's default: no limit
    bitrate: int | None = None  # None means inherit the lobby's bitrate
    category_id: int | None = None  # None means same category as the lobby
    delete_delay: int = DEFAULT_DELETE_DELAY  # seconds empty before delete

    def validate(self) -> list[str]:
        """Return a list of problems with this config (empty = fine)."""
        problems: list[str] = []
        if not self.template.strip():
            problems.append("template must not be empty")
        if len(self.template) > 100:
            problems.append("template must be 100 characters or fewer")
        if self.user_limit < 0 or self.user_limit > 99:
            problems.append("user limit must be between 0 (no limit) and 99")
        if self.bitrate is not None and not 8000 <= self.bitrate <= 384000:
            problems.append("bitrate must be between 8000 and 384000")
        if self.delete_delay < 0 or self.delete_delay > 3600:
            problems.append("delete delay must be between 0 and 3600 seconds")
        return problems


@dataclass
class TempChannel:
    """One temp channel the cog created and is responsible for."""

    channel_id: int
    guild_id: int
    lobby_id: int
    owner_id: int | None  # None when the owner left and nobody claimed it
    number: int = 0  # the {n} this channel got, for naming the next one
    created_at: str = ""  # ISO timestamp, informational only


@dataclass
class _StoreData:
    lobbies: dict[str, dict[str, dict]] = field(default_factory=dict)
    channels: dict[str, dict] = field(default_factory=dict)


class TempVCStore:
    """Load, query and atomically save the vcspawn JSON file."""

    def __init__(self, path: str | Path = DEFAULT_STORE_PATH) -> None:
        self.path = Path(path)
        self._disk_ok = True
        self._data = self._load()

    @classmethod
    def in_memory(cls, path: str | Path = DEFAULT_STORE_PATH) -> "TempVCStore":
        """A store that never touches disk.

        Used when the file on disk is corrupt: the cog keeps working
        for the rest of the session instead of crashing the whole bot,
        and nothing it does gets persisted until an admin fixes the file.
        """
        obj = cls.__new__(cls)
        obj.path = Path(path)
        obj._disk_ok = False
        obj._data = _StoreData()
        return obj

    # -- loading -------------------------------------------------------

    def _load(self) -> _StoreData:
        if not self.path.exists():
            return _StoreData()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError(f"could not read {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise StoreError(f"{self.path} is not a JSON object")
        data = _StoreData()
        lobbies = raw.get("lobbies", {})
        channels = raw.get("channels", {})
        if isinstance(lobbies, dict):
            data.lobbies = {str(k): v for k, v in lobbies.items() if isinstance(v, dict)}
        if isinstance(channels, dict):
            data.channels = {str(k): v for k, v in channels.items() if isinstance(v, dict)}
        return data

    def save(self) -> None:
        """Write the whole store atomically (temp file + rename)."""
        if not self._disk_ok:
            return  # in-memory fallback: nothing to persist to
        payload = {
            "lobbies": self._data.lobbies,
            "channels": self._data.channels,
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp_path.write_text(text, encoding="utf-8")
            os.replace(tmp_path, self.path)
        except OSError as exc:
            raise StoreError(f"could not write {self.path}: {exc}") from exc

    # -- lobbies -------------------------------------------------------

    def get_lobbies(self, guild_id: int) -> dict[int, LobbyConfig]:
        """All lobby configs for one guild, keyed by lobby channel id."""
        raw = self._data.lobbies.get(str(guild_id), {})
        out: dict[int, LobbyConfig] = {}
        for lobby_id, cfg in raw.items():
            try:
                out[int(lobby_id)] = LobbyConfig(**{k: v for k, v in cfg.items() if k in LobbyConfig.__dataclass_fields__})
            except (TypeError, ValueError):
                log.warning("skipping malformed lobby config %s in guild %s", lobby_id, guild_id)
        return out

    def get_lobby(self, guild_id: int, lobby_id: int) -> LobbyConfig | None:
        """One lobby's config, or None if that channel isn't a lobby."""
        return self.get_lobbies(guild_id).get(lobby_id)

    def set_lobby(self, guild_id: int, lobby_id: int, config: LobbyConfig) -> None:
        """Add or replace a lobby config, then save."""
        self._data.lobbies.setdefault(str(guild_id), {})[str(lobby_id)] = asdict(config)
        self.save()

    def remove_lobby(self, guild_id: int, lobby_id: int) -> bool:
        """Remove a lobby config. Returns True if one existed."""
        guild_lobbies = self._data.lobbies.get(str(guild_id), {})
        if str(lobby_id) not in guild_lobbies:
            return False
        del guild_lobbies[str(lobby_id)]
        if not guild_lobbies:
            del self._data.lobbies[str(guild_id)]
        self.save()
        return True

    # -- temp channels -------------------------------------------------

    def register_channel(self, channel: TempChannel) -> None:
        """Record a channel the cog just created, then save."""
        self._data.channels[str(channel.channel_id)] = asdict(channel)
        self.save()

    def forget_channel(self, channel_id: int) -> bool:
        """Drop a channel from the registry. Returns True if it was there."""
        if str(channel_id) not in self._data.channels:
            return False
        del self._data.channels[str(channel_id)]
        self.save()
        return True

    def get_channel(self, channel_id: int) -> TempChannel | None:
        """One registered temp channel, or None if we don't track it."""
        raw = self._data.channels.get(str(channel_id))
        if raw is None:
            return None
        try:
            return TempChannel(**{k: v for k, v in raw.items() if k in TempChannel.__dataclass_fields__})
        except (TypeError, ValueError):
            log.warning("skipping malformed temp channel record %s", channel_id)
            return None

    def all_channels(self) -> list[TempChannel]:
        """Every temp channel on record, for the startup sweep."""
        out: list[TempChannel] = []
        for channel_id in list(self._data.channels):
            channel = self.get_channel(int(channel_id))
            if channel is not None:
                out.append(channel)
        return out

    def channels_for_lobby(self, guild_id: int, lobby_id: int) -> list[TempChannel]:
        """Temp channels that came from one lobby."""
        return [
            c
            for c in self.all_channels()
            if c.guild_id == guild_id and c.lobby_id == lobby_id
        ]

    def set_owner(self, channel_id: int, owner_id: int | None) -> bool:
        """Change (or clear) a temp channel's owner. True if tracked."""
        raw = self._data.channels.get(str(channel_id))
        if raw is None:
            return False
        raw["owner_id"] = owner_id
        self.save()
        return True
