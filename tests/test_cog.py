"""Tests for the cog: lobby joins, leaves, owner commands, admin commands.

Everything here runs against MagicMock Discord objects — no bot, no
token, no network. The point is to prove the wiring: the right channel
gets created, the right member gets moved, the store stays in sync.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from vcspawn.cog import TempVCCog
from vcspawn.store import LobbyConfig, TempChannel

GUILD_ID = 111
LOBBY_ID = 222
TEMP_ID = 333


def _voice_channel(*, channel_id, name, guild=None, members=None, category=None,
                   user_limit=0, bitrate=64000):
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = channel_id
    channel.name = name
    channel.guild = guild
    channel.members = list(members or [])
    channel.category = category
    channel.overwrites = {}
    channel.user_limit = user_limit
    channel.bitrate = bitrate
    channel.delete = AsyncMock()
    channel.edit = AsyncMock()
    channel.set_permissions = AsyncMock()
    channel.mention = f"<#{channel_id}>"
    return channel


def _member(*, member_id=777, display_name="Krrish", name="krrish", guild=None,
            voice_channel=None, activity=None):
    member = MagicMock()
    member.id = member_id
    member.display_name = display_name
    member.name = name
    member.guild = guild
    member.activity = activity
    member.bot = False
    member.mention = f"<@{member_id}>"
    member.move_to = AsyncMock()
    voice = MagicMock()
    voice.channel = voice_channel
    member.voice = voice
    return member


def _guild(*, create_channel=None):
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.name = "Test Server"
    guild.create_voice_channel = AsyncMock(return_value=create_channel)
    guild.get_channel = MagicMock(return_value=None)
    return guild


def _voice_state(channel=None):
    state = MagicMock()
    state.channel = channel
    return state


def _ctx(*, guild=None, author=None):
    ctx = MagicMock()
    ctx.guild = guild
    ctx.author = author or _member(guild=guild)
    ctx.send = AsyncMock()
    return ctx


@pytest.fixture
def cog(tmp_path):
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=None)
    return TempVCCog(bot, store_path=tmp_path / "vcspawn.json")


@pytest.fixture
def lobby_cog(cog):
    cog.store.set_lobby(GUILD_ID, LOBBY_ID, LobbyConfig(delete_delay=0))
    return cog


# -- joining a lobby ---------------------------------------------------


async def test_join_lobby_creates_channel_and_moves_member(lobby_cog):
    new_channel = _voice_channel(channel_id=TEMP_ID, name="Krrish's room")
    guild = _guild(create_channel=new_channel)
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    member = _member(guild=guild)

    await lobby_cog.on_voice_state_update(member, _voice_state(None), _voice_state(lobby))

    guild.create_voice_channel.assert_awaited_once()
    assert guild.create_voice_channel.await_args.args[0] == "Krrish's room"
    member.move_to.assert_awaited_once()
    assert member.move_to.await_args.args[0] is new_channel
    record = lobby_cog.store.get_channel(TEMP_ID)
    assert record is not None and record.owner_id == 777 and record.lobby_id == LOBBY_ID


async def test_join_non_lobby_does_nothing(lobby_cog):
    guild = _guild()
    other = _voice_channel(channel_id=444, name="general", guild=guild)
    member = _member(guild=guild)

    await lobby_cog.on_voice_state_update(member, _voice_state(None), _voice_state(other))

    guild.create_voice_channel.assert_not_awaited()
    assert lobby_cog.store.all_channels() == []


async def test_second_channel_gets_number_two(lobby_cog):
    lobby_cog.store.set_lobby(
        GUILD_ID, LOBBY_ID, LobbyConfig(template="{user}'s room {n}", delete_delay=0)
    )
    lobby_cog.store.register_channel(
        TempChannel(channel_id=300, guild_id=GUILD_ID, lobby_id=LOBBY_ID, owner_id=1, number=1)
    )
    new_channel = _voice_channel(channel_id=TEMP_ID, name="x")
    guild = _guild(create_channel=new_channel)
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    member = _member(guild=guild)

    await lobby_cog.on_voice_state_update(member, _voice_state(None), _voice_state(lobby))

    assert guild.create_voice_channel.await_args.args[0] == "Krrish's room 2"


async def test_create_forbidden_does_not_raise_or_register(lobby_cog):
    guild = _guild()
    guild.create_voice_channel.side_effect = discord.Forbidden(MagicMock(), "no perms")
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    member = _member(guild=guild)

    await lobby_cog.on_voice_state_update(member, _voice_state(None), _voice_state(lobby))

    assert lobby_cog.store.all_channels() == []


async def test_move_forbidden_does_not_raise(lobby_cog):
    new_channel = _voice_channel(channel_id=TEMP_ID, name="Krrish's room")
    guild = _guild(create_channel=new_channel)
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    member = _member(guild=guild)
    member.move_to.side_effect = discord.Forbidden(MagicMock(), "no perms")

    await lobby_cog.on_voice_state_update(member, _voice_state(None), _voice_state(lobby))

    # Channel still registered — the member can join it themselves.
    assert lobby_cog.store.get_channel(TEMP_ID) is not None


# -- leaving a temp channel --------------------------------------------


async def test_leave_empty_channel_deletes_after_delay(lobby_cog):
    temp = _voice_channel(channel_id=TEMP_ID, name="Krrish's room", members=[])
    guild = _guild()
    guild.get_channel = MagicMock(return_value=temp)
    lobby_cog.bot.get_guild = MagicMock(return_value=guild)
    lobby_cog.store.register_channel(
        TempChannel(channel_id=TEMP_ID, guild_id=GUILD_ID, lobby_id=LOBBY_ID, owner_id=777)
    )
    member = _member(guild=guild)

    await lobby_cog.on_voice_state_update(member, _voice_state(temp), _voice_state(None))
    await asyncio.sleep(0.05)

    temp.delete.assert_awaited_once()
    assert lobby_cog.store.get_channel(TEMP_ID) is None


async def test_leave_with_people_left_keeps_channel(lobby_cog):
    friend = _member(member_id=888, display_name="Friend")
    temp = _voice_channel(channel_id=TEMP_ID, name="Krrish's room", members=[friend])
    lobby_cog.store.register_channel(
        TempChannel(channel_id=TEMP_ID, guild_id=GUILD_ID, lobby_id=LOBBY_ID, owner_id=777)
    )
    guild = _guild()
    member = _member(guild=guild)

    await lobby_cog.on_voice_state_update(member, _voice_state(temp), _voice_state(None))

    temp.delete.assert_not_awaited()
    assert lobby_cog.store.get_channel(TEMP_ID) is not None


async def test_owner_leaving_with_people_left_clears_owner(lobby_cog):
    friend = _member(member_id=888, display_name="Friend")
    temp = _voice_channel(channel_id=TEMP_ID, name="Krrish's room", members=[friend])
    lobby_cog.store.register_channel(
        TempChannel(channel_id=TEMP_ID, guild_id=GUILD_ID, lobby_id=LOBBY_ID, owner_id=777)
    )
    guild = _guild()
    member = _member(guild=guild)

    await lobby_cog.on_voice_state_update(member, _voice_state(temp), _voice_state(None))

    assert lobby_cog.store.get_channel(TEMP_ID).owner_id is None


async def test_non_owner_leaving_keeps_owner(lobby_cog):
    friend = _member(member_id=888, display_name="Friend")
    temp = _voice_channel(channel_id=TEMP_ID, name="Krrish's room", members=[friend])
    lobby_cog.store.register_channel(
        TempChannel(channel_id=TEMP_ID, guild_id=GUILD_ID, lobby_id=LOBBY_ID, owner_id=777)
    )
    guild = _guild()
    leaver = _member(member_id=888, guild=guild)  # the friend leaves

    await lobby_cog.on_voice_state_update(leaver, _voice_state(temp), _voice_state(None))

    assert lobby_cog.store.get_channel(TEMP_ID).owner_id == 777


async def test_move_from_temp_to_lobby_handles_both(lobby_cog):
    old = _voice_channel(channel_id=TEMP_ID, name="Krrish's room", members=[])
    new_channel = _voice_channel(channel_id=444, name="Krrish's room")
    guild = _guild(create_channel=new_channel)
    guild.get_channel = MagicMock(return_value=old)
    lobby_cog.bot.get_guild = MagicMock(return_value=guild)
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    lobby_cog.store.register_channel(
        TempChannel(channel_id=TEMP_ID, guild_id=GUILD_ID, lobby_id=LOBBY_ID, owner_id=777)
    )
    member = _member(guild=guild)

    await lobby_cog.on_voice_state_update(member, _voice_state(old), _voice_state(lobby))
    await asyncio.sleep(0.05)

    guild.create_voice_channel.assert_awaited_once()
    old.delete.assert_awaited_once()


# -- startup sweep -------------------------------------------------------


async def test_on_ready_forgets_gone_channels_and_deletes_empty_ones(cog):
    cog.store.set_lobby(GUILD_ID, LOBBY_ID, LobbyConfig(delete_delay=0))
    cog.store.register_channel(
        TempChannel(channel_id=900, guild_id=GUILD_ID, lobby_id=LOBBY_ID, owner_id=1)
    )
    empty = _voice_channel(channel_id=901, name="orphan", members=[])
    cog.store.register_channel(
        TempChannel(channel_id=901, guild_id=GUILD_ID, lobby_id=LOBBY_ID, owner_id=2)
    )
    guild = _guild()
    guild.get_channel = MagicMock(side_effect=lambda cid: empty if cid == 901 else None)
    cog.bot.get_guild = MagicMock(return_value=guild)

    await cog.on_ready()
    await asyncio.sleep(0.05)

    assert cog.store.get_channel(900) is None  # gone from Discord
    empty.delete.assert_awaited_once()
    assert cog.store.get_channel(901) is None


async def test_on_ready_runs_only_once(cog):
    cog.store.register_channel(
        TempChannel(channel_id=900, guild_id=GUILD_ID, lobby_id=LOBBY_ID, owner_id=1)
    )
    cog.bot.get_guild = MagicMock(return_value=None)
    await cog.on_ready()
    await cog.on_ready()  # second call is a no-op
    assert cog.store.get_channel(900) is None


# -- admin lobby commands --------------------------------------------------


async def test_lobby_add_and_list(cog):
    guild = _guild()
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    guild.get_channel = MagicMock(return_value=lobby)
    ctx = _ctx(guild=guild)

    await cog.vc_lobby_add.callback(cog, ctx, lobby, template="{user} — {game}")

    assert cog.store.get_lobby(GUILD_ID, LOBBY_ID).template == "{user} — {game}"
    assert "{user} — {game}" in ctx.send.await_args.args[0]

    await cog.vc_lobby_list.callback(cog, ctx)
    assert "create-a-room" in ctx.send.await_args.args[0]


async def test_lobby_add_rejects_bad_template(cog):
    guild = _guild()
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    ctx = _ctx(guild=guild)

    await cog.vc_lobby_add.callback(cog, ctx, lobby, template="x" * 101)

    assert cog.store.get_lobby(GUILD_ID, LOBBY_ID) is None
    assert "100" in ctx.send.await_args.args[0]


async def test_lobby_remove(cog):
    guild = _guild()
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    ctx = _ctx(guild=guild)
    cog.store.set_lobby(GUILD_ID, LOBBY_ID, LobbyConfig())

    await cog.vc_lobby_remove.callback(cog, ctx, lobby)
    assert cog.store.get_lobby(GUILD_ID, LOBBY_ID) is None

    await cog.vc_lobby_remove.callback(cog, ctx, lobby)
    assert "isn't a lobby" in ctx.send.await_args.args[0]


async def test_lobby_set_limit_and_delay(cog):
    guild = _guild()
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    ctx = _ctx(guild=guild)
    cog.store.set_lobby(GUILD_ID, LOBBY_ID, LobbyConfig())

    await cog.vc_lobby_set.callback(cog, ctx, lobby, "limit", value="5")
    assert cog.store.get_lobby(GUILD_ID, LOBBY_ID).user_limit == 5

    await cog.vc_lobby_set.callback(cog, ctx, lobby, "delay", value="30")
    assert cog.store.get_lobby(GUILD_ID, LOBBY_ID).delete_delay == 30


async def test_lobby_set_rejects_unknown_setting_and_bad_numbers(cog):
    guild = _guild()
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    ctx = _ctx(guild=guild)
    cog.store.set_lobby(GUILD_ID, LOBBY_ID, LobbyConfig())

    await cog.vc_lobby_set.callback(cog, ctx, lobby, "colour", value="red")
    assert "don't know" in ctx.send.await_args.args[0]

    await cog.vc_lobby_set.callback(cog, ctx, lobby, "limit", value="lots")
    assert cog.store.get_lobby(GUILD_ID, LOBBY_ID).user_limit == 0  # unchanged
    assert "whole number" in ctx.send.await_args.args[0]


async def test_lobby_set_on_non_lobby(cog):
    guild = _guild()
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    ctx = _ctx(guild=guild)

    await cog.vc_lobby_set.callback(cog, ctx, lobby, "limit", value="5")
    assert "isn't a lobby" in ctx.send.await_args.args[0]


# -- owner commands --------------------------------------------------------


def _owner_ctx(cog, *, owner_id=777, author_id=None, channel=None):
    guild = _guild()
    temp = channel or _voice_channel(channel_id=TEMP_ID, name="Krrish's room", guild=guild)
    cog.store.register_channel(
        TempChannel(channel_id=temp.id, guild_id=GUILD_ID, lobby_id=LOBBY_ID, owner_id=owner_id)
    )
    # The author defaults to the owner; pass a different author_id to
    # test the non-owner path.
    author = _member(
        member_id=owner_id if author_id is None else author_id,
        guild=guild,
        voice_channel=temp,
    )
    return _ctx(guild=guild, author=author), temp


async def test_lock_and_unlock(cog):
    ctx, temp = _owner_ctx(cog)
    ctx.guild.default_role = MagicMock()

    await cog.vc_lock.callback(cog, ctx)
    temp.set_permissions.assert_awaited_with(
        ctx.guild.default_role, connect=False, reason="vcspawn temp voice channel"
    )

    await cog.vc_unlock.callback(cog, ctx)
    temp.set_permissions.assert_awaited_with(
        ctx.guild.default_role, overwrite=None, reason="vcspawn temp voice channel"
    )


async def test_non_owner_cannot_lock(cog):
    ctx, temp = _owner_ctx(cog, owner_id=999, author_id=777)  # someone else owns it
    await cog.vc_lock.callback(cog, ctx)
    temp.set_permissions.assert_not_awaited()
    assert "Only the channel owner" in ctx.send.await_args.args[0]


async def test_ownerless_channel_tells_user_to_claim(cog):
    ctx, temp = _owner_ctx(cog, owner_id=None, author_id=777)
    await cog.vc_lock.callback(cog, ctx)
    assert "claim" in ctx.send.await_args.args[0]


async def test_limit_validates_range(cog):
    ctx, temp = _owner_ctx(cog)

    await cog.vc_limit.callback(cog, ctx, 150)
    temp.edit.assert_not_awaited()
    assert "between 0" in ctx.send.await_args.args[0]

    await cog.vc_limit.callback(cog, ctx, 4)
    temp.edit.assert_awaited_once_with(user_limit=4, reason="vcspawn temp voice channel")


async def test_rename_sanitises(cog):
    ctx, temp = _owner_ctx(cog)
    await cog.vc_rename.callback(cog, ctx, name="late\nnight   grind")
    temp.edit.assert_awaited_once()
    assert temp.edit.await_args.kwargs["name"] == "late night grind"


async def test_kick_disconnects_member_in_channel(cog):
    ctx, temp = _owner_ctx(cog)
    target = _member(member_id=888, display_name="Troll", guild=ctx.guild, voice_channel=temp)
    await cog.vc_kick.callback(cog, ctx, target)
    target.move_to.assert_awaited_once_with(None, reason="vcspawn temp voice channel (kicked by owner)")


async def test_kick_refuses_member_not_in_channel(cog):
    ctx, temp = _owner_ctx(cog)
    elsewhere = _voice_channel(channel_id=555, name="other", guild=ctx.guild)
    target = _member(member_id=888, guild=ctx.guild, voice_channel=elsewhere)
    await cog.vc_kick.callback(cog, ctx, target)
    target.move_to.assert_not_awaited()
    assert "isn't in this channel" in ctx.send.await_args.args[0]


async def test_claim_on_ownerless_channel(cog):
    ctx, temp = _owner_ctx(cog, owner_id=None, author_id=777)
    await cog.vc_claim.callback(cog, ctx)
    assert cog.store.get_channel(TEMP_ID).owner_id == 777
    assert "yours now" in ctx.send.await_args.args[0]


async def test_claim_refused_when_owned(cog):
    ctx, temp = _owner_ctx(cog, owner_id=999, author_id=777)
    await cog.vc_claim.callback(cog, ctx)
    assert cog.store.get_channel(TEMP_ID).owner_id == 999
    assert "still owns" in ctx.send.await_args.args[0]


async def test_transfer_hands_over_ownership(cog):
    ctx, temp = _owner_ctx(cog)
    friend = _member(member_id=888, display_name="Friend", guild=ctx.guild, voice_channel=temp)
    await cog.vc_transfer.callback(cog, ctx, friend)
    assert cog.store.get_channel(TEMP_ID).owner_id == 888
    assert "belongs to" in ctx.send.await_args.args[0]


async def test_transfer_refuses_member_not_in_channel(cog):
    ctx, temp = _owner_ctx(cog)
    elsewhere = _voice_channel(channel_id=555, name="other", guild=ctx.guild)
    friend = _member(member_id=888, guild=ctx.guild, voice_channel=elsewhere)
    await cog.vc_transfer.callback(cog, ctx, friend)
    assert cog.store.get_channel(TEMP_ID).owner_id == 777
    assert "isn't in this channel" in ctx.send.await_args.args[0]


async def test_commands_outside_temp_channel_reply_helpfully(cog):
    guild = _guild()
    plain = _voice_channel(channel_id=444, name="general", guild=guild)
    author = _member(guild=guild, voice_channel=plain)
    ctx = _ctx(guild=guild, author=author)

    await cog.vc_lock.callback(cog, ctx)
    assert "isn't a temp channel" in ctx.send.await_args.args[0]


async def test_vc_status_shows_room_and_lobbies(cog):
    guild = _guild()
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    guild.get_channel = MagicMock(return_value=lobby)
    cog.store.set_lobby(GUILD_ID, LOBBY_ID, LobbyConfig())
    temp = _voice_channel(channel_id=TEMP_ID, name="Krrish's room", guild=guild, members=[])
    cog.store.register_channel(
        TempChannel(channel_id=TEMP_ID, guild_id=GUILD_ID, lobby_id=LOBBY_ID, owner_id=777)
    )
    author = _member(guild=guild, voice_channel=temp)
    ctx = _ctx(guild=guild, author=author)

    await cog.vc.callback(cog, ctx)
    sent = ctx.send.await_args.args[0]
    assert "Krrish's room" in sent
    assert "create-a-room" in sent


async def test_vc_status_without_room_lists_lobbies_only(cog):
    guild = _guild()
    lobby = _voice_channel(channel_id=LOBBY_ID, name="create-a-room", guild=guild)
    guild.get_channel = MagicMock(return_value=lobby)
    cog.store.set_lobby(GUILD_ID, LOBBY_ID, LobbyConfig())
    ctx = _ctx(guild=guild, author=_member(guild=guild, voice_channel=None))

    await cog.vc.callback(cog, ctx)
    sent = ctx.send.await_args.args[0]
    assert "create-a-room" in sent
    assert "No lobbies" not in sent
