"""Tests for the JSON store: round-trips, corruption, atomic saves."""

import json

import pytest

from vcspawn.store import LobbyConfig, StoreError, TempChannel, TempVCStore


@pytest.fixture
def store(tmp_path):
    return TempVCStore(tmp_path / "vcspawn.json")


def test_missing_file_starts_empty(store):
    assert store.get_lobbies(111) == {}
    assert store.all_channels() == []


def test_lobby_round_trip(store, tmp_path):
    store.set_lobby(111, 222, LobbyConfig(template="{user} — {game}", user_limit=5, delete_delay=30))
    reloaded = TempVCStore(tmp_path / "vcspawn.json")
    config = reloaded.get_lobby(111, 222)
    assert config is not None
    assert config.template == "{user} — {game}"
    assert config.user_limit == 5
    assert config.delete_delay == 30


def test_remove_lobby_returns_whether_it_existed(store):
    store.set_lobby(111, 222, LobbyConfig())
    assert store.remove_lobby(111, 222) is True
    assert store.get_lobby(111, 222) is None
    assert store.remove_lobby(111, 222) is False


def test_channel_register_forget_and_lookup(store, tmp_path):
    channel = TempChannel(channel_id=999, guild_id=111, lobby_id=222, owner_id=777, number=1)
    store.register_channel(channel)
    assert store.get_channel(999) == channel
    assert store.channels_for_lobby(111, 222) == [channel]
    assert store.channels_for_lobby(111, 333) == []
    assert store.forget_channel(999) is True
    assert store.get_channel(999) is None
    assert store.forget_channel(999) is False


def test_set_owner_clears_and_reassigns(store):
    store.register_channel(TempChannel(channel_id=1, guild_id=1, lobby_id=2, owner_id=10))
    assert store.set_owner(1, None) is True
    assert store.get_channel(1).owner_id is None
    assert store.set_owner(1, 20) is True
    assert store.get_channel(1).owner_id == 20
    assert store.set_owner(999, 20) is False


def test_corrupt_file_raises_store_error(tmp_path):
    path = tmp_path / "vcspawn.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError):
        TempVCStore(path)


def test_non_object_file_raises_store_error(tmp_path):
    path = tmp_path / "vcspawn.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(StoreError):
        TempVCStore(path)


def test_save_writes_no_temp_file_left_behind(store, tmp_path):
    store.set_lobby(111, 222, LobbyConfig())
    assert not (tmp_path / "vcspawn.json.tmp").exists()
    assert json.loads((tmp_path / "vcspawn.json").read_text())["lobbies"]["111"]["222"]["template"] == "{user}'s room"


def test_in_memory_store_never_touches_disk(tmp_path):
    path = tmp_path / "vcspawn.json"
    mem = TempVCStore.in_memory(path)
    mem.set_lobby(111, 222, LobbyConfig())
    assert mem.get_lobby(111, 222) is not None  # works in memory
    assert not path.exists()  # but nothing was written


def test_lobby_config_validation():
    assert LobbyConfig().validate() == []
    assert LobbyConfig(template="").validate() != []
    assert LobbyConfig(template="x" * 101).validate() != []
    assert LobbyConfig(user_limit=100).validate() != []
    assert LobbyConfig(user_limit=-1).validate() != []
    assert LobbyConfig(bitrate=100).validate() != []
    assert LobbyConfig(bitrate=None).validate() == []
    assert LobbyConfig(delete_delay=3601).validate() != []
    assert LobbyConfig(delete_delay=-5).validate() != []
