"""The vcspawn cog.

One idea, done properly: a server sets up one or more "lobby" voice
channels. Anyone who joins a lobby gets their own temporary voice
channel — named from a template, with them as the owner — and gets
moved into it. When the channel empties, it deletes itself after a
short grace period, so a dropped connection doesn't nuke the room
mid-sentence.

Two command surfaces:

* ``!vc lobby ...`` — server admins (Manage Server) add/remove lobbies
  and tune how their channels behave.
* ``!vc lock|unlock|limit|rename|kick|claim|transfer|info`` — the
  channel owner manages their own room from inside it, no admin needed.

The cog never raises from its voice listener: a missing permission or
a deleted channel is a normal day on Discord, not a crash. Every rule
about *what* should happen lives in :mod:`vcspawn.policy`; this file
only does the Discord work.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands

from .naming import (
    DEFAULT_TEMPLATE,
    PLACEHOLDERS,
    render_template,
    sanitise_channel_name,
    values_for_member,
)
from .policy import is_owner, next_channel_number, should_delete
from .store import (
    DEFAULT_STORE_PATH,
    LobbyConfig,
    StoreError,
    TempChannel,
    TempVCStore,
)

__all__ = ["TempVCCog", "setup"]

log = logging.getLogger("vcspawn")

#: Reason string Discord shows in the audit log for our channel edits.
AUDIT_REASON = "vcspawn temp voice channel"


class TempVCCog(commands.Cog):
    """Temporary voice channels from lobby channels."""

    def __init__(self, bot: commands.Bot, store_path: str | Path = DEFAULT_STORE_PATH) -> None:
        self.bot = bot
        try:
            self.store = TempVCStore(store_path)
        except StoreError as exc:
            # A store that can't even load shouldn't take the whole bot
            # down — run in-memory and say so loudly in the logs.
            log.error("vcspawn: %s — running with in-memory config only", exc)
            self.store = TempVCStore.in_memory(store_path)
        self._swept = False
        self._pending_deletes: set[int] = set()

    # -- voice events --------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Create a temp channel on lobby join, clean up on leave."""
        guild = member.guild
        if guild is None:
            return

        # Joining a lobby spawns a channel. A move from a temp channel
        # into a lobby is both a join and a leave, so handle the join
        # first, then the leave below — never return early here.
        after_channel = after.channel
        if after_channel is not None and (
            before.channel is None or before.channel.id != after_channel.id
        ):
            lobby = self.store.get_lobby(guild.id, after_channel.id)
            if lobby is not None:
                await self._create_temp_channel(member, after_channel, lobby)

        before_channel = before.channel
        if before_channel is not None and (
            after_channel is None or after_channel.id != before_channel.id
        ):
            record = self.store.get_channel(before_channel.id)
            if record is not None:
                await self._handle_temp_leave(member, before_channel, record)

    async def _create_temp_channel(
        self,
        member: discord.Member,
        lobby_channel: discord.VoiceChannel,
        lobby: LobbyConfig,
    ) -> None:
        guild = member.guild
        number = next_channel_number(
            [c.number for c in self.store.channels_for_lobby(guild.id, lobby_channel.id)]
        )
        rendered = render_template(lobby.template, values_for_member(member, number))
        name = sanitise_channel_name(rendered.text)

        # New channels behave like the lobby (same permissions), except
        # the creator can manage their own room.
        overwrites = dict(lobby_channel.overwrites)
        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True, connect=True, manage_channels=True
        )
        category = lobby_channel.category
        if lobby.category_id is not None:
            wanted = guild.get_channel(lobby.category_id)
            if isinstance(wanted, discord.CategoryChannel):
                category = wanted

        try:
            channel = await guild.create_voice_channel(
                name,
                category=category,
                user_limit=lobby.user_limit if lobby.user_limit else None,
                bitrate=lobby.bitrate,
                overwrites=overwrites,
                reason=f"{AUDIT_REASON} for {member} ({member.id})",
            )
        except discord.Forbidden:
            log.warning("no permission to create a voice channel in %s", guild.id)
            return
        except discord.HTTPException as exc:
            log.warning("failed to create temp channel in %s: %s", guild.id, exc)
            return

        self.store.register_channel(
            TempChannel(
                channel_id=channel.id,
                guild_id=guild.id,
                lobby_id=lobby_channel.id,
                owner_id=member.id,
                number=number,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )

        try:
            await member.move_to(channel, reason=AUDIT_REASON)
        except discord.Forbidden:
            # Channel exists, member stays in the lobby. They can join
            # the new channel themselves — not worth an error.
            log.warning("could not move %s into their temp channel", member.id)
        except discord.HTTPException as exc:
            log.warning("failed to move %s into temp channel: %s", member.id, exc)

    async def _handle_temp_leave(
        self,
        member: discord.Member,
        channel: discord.VoiceChannel,
        record: TempChannel,
    ) -> None:
        if should_delete(len(channel.members)):
            lobby = self.store.get_lobby(record.guild_id, record.lobby_id)
            delay = lobby.delete_delay if lobby is not None else 0
            self._schedule_delete(channel.id, record.guild_id, delay)
            return
        if record.owner_id == member.id:
            # Owner left, people are still talking: the room becomes
            # ownerless until someone claims it with !vc claim.
            self.store.set_owner(channel.id, None)

    # -- deletion with a grace period ----------------------------------

    def _schedule_delete(self, channel_id: int, guild_id: int, delay: int) -> None:
        if channel_id in self._pending_deletes:
            return  # already counting down; one timer is enough
        self._pending_deletes.add(channel_id)
        asyncio.create_task(self._delete_after_delay(channel_id, guild_id, delay))

    async def _delete_after_delay(self, channel_id: int, guild_id: int, delay: int) -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            self._pending_deletes.discard(channel_id)

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            self.store.forget_channel(channel_id)
            return
        channel = guild.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            self.store.forget_channel(channel_id)
            return
        if not should_delete(len(channel.members)):
            return  # someone came back during the grace period
        try:
            await channel.delete(reason=AUDIT_REASON)
        except discord.Forbidden:
            log.warning("no permission to delete empty temp channel %s", channel_id)
            return  # keep the record; the next sweep retries
        except discord.HTTPException as exc:
            log.warning("failed to delete temp channel %s: %s", channel_id, exc)
            return
        self.store.forget_channel(channel_id)

    # -- startup sweep ---------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Clean up channels left over from before a restart."""
        if self._swept:
            return
        self._swept = True
        for record in self.store.all_channels():
            guild = self.bot.get_guild(record.guild_id)
            if guild is None:
                self.store.forget_channel(record.channel_id)
                continue
            channel = guild.get_channel(record.channel_id)
            if channel is None or not isinstance(channel, discord.VoiceChannel):
                self.store.forget_channel(record.channel_id)
                continue
            if should_delete(len(channel.members)):
                self._schedule_delete(channel.id, record.guild_id, 0)

    # -- helpers ---------------------------------------------------------

    def _guild_or_reply(self, ctx: commands.Context) -> discord.Guild | None:
        if ctx.guild is None:
            raise commands.NoPrivateMessage("vc commands only work in a server.")
        return ctx.guild

    async def _temp_channel_of(
        self, ctx: commands.Context
    ) -> tuple[TempChannel, discord.VoiceChannel] | None:
        """The temp channel the invoker is sitting in, or None + a reply."""
        guild = self._guild_or_reply(ctx)
        voice = getattr(ctx.author, "voice", None)
        channel = getattr(voice, "channel", None)
        if channel is None:
            await ctx.send("Join your temp voice channel first, then run that command.")
            return None
        record = self.store.get_channel(channel.id)
        if record is None:
            await ctx.send("That isn't a temp channel I created — these commands only work in one.")
            return None
        return record, channel

    async def _owner_channel_of(
        self, ctx: commands.Context
    ) -> tuple[TempChannel, discord.VoiceChannel] | None:
        """The temp channel the invoker owns, or None + a reply."""
        found = await self._temp_channel_of(ctx)
        if found is None:
            return None
        record, channel = found
        if not is_owner(record.owner_id, ctx.author.id):
            if record.owner_id is None:
                await ctx.send(
                    "Nobody owns this channel right now — run `!vc claim` to take it over."
                )
            else:
                await ctx.send("Only the channel owner can do that.")
            return None
        return record, channel

    # -- the !vc group ---------------------------------------------------

    @commands.group(name="vc", invoke_without_command=True)
    async def vc(self, ctx: commands.Context) -> None:
        """Temp voice channels. `!vc` on its own shows your room's status."""
        guild = self._guild_or_reply(ctx)
        lines = ["**Temp voice channels** — join a lobby and I make you a room."]
        # Silent lookup here: bare !vc is a status check, not a command
        # that needs you to be inside a room.
        voice = getattr(ctx.author, "voice", None)
        channel = getattr(voice, "channel", None)
        record = self.store.get_channel(channel.id) if channel is not None else None
        if record is not None and channel is not None:
            owner = f"<@{record.owner_id}>" if record.owner_id else "nobody (run `!vc claim`)"
            lines.append(
                f"You're in **{channel.name}** — owner: {owner}, "
                f"{len(channel.members)} here, limit {channel.user_limit or 'none'}."
            )
        lobbies = self.store.get_lobbies(guild.id)
        if lobbies:
            lines.append("")
            lines.append("**Lobbies in this server:**")
            for lobby_id, cfg in lobbies.items():
                lobby_channel = guild.get_channel(lobby_id)
                name = f"#{lobby_channel.name}" if lobby_channel else f"deleted channel {lobby_id}"
                lines.append(f"- {name} → `{cfg.template}`")
        else:
            lines.append("")
            lines.append("No lobbies yet. An admin can add one with `!vc lobby add #channel`.")
        lines.append("")
        lines.append(
            "Owner commands: `!vc lock`, `!vc unlock`, `!vc limit <n>`, "
            "`!vc rename <name>`, `!vc kick @member`, `!vc claim`, `!vc transfer @member`."
        )
        await ctx.send("\n".join(lines))

    # -- admin: lobbies ---------------------------------------------------

    @vc.group(name="lobby", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def vc_lobby(self, ctx: commands.Context) -> None:
        """Manage lobby channels. See `!vc lobby list` for what's set up."""
        await ctx.send(
            "Lobby commands: `!vc lobby add #channel [template]`, "
            "`!vc lobby remove #channel`, `!vc lobby list`, "
            "`!vc lobby set #channel <setting> <value>`."
        )

    @vc_lobby.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def vc_lobby_add(
        self,
        ctx: commands.Context,
        channel: discord.VoiceChannel,
        *,
        template: str = DEFAULT_TEMPLATE,
    ) -> None:
        """Make a voice channel a lobby. Example: !vc lobby add #create-a-room"""
        guild = self._guild_or_reply(ctx)
        config = LobbyConfig(template=template.strip() or DEFAULT_TEMPLATE)
        problems = config.validate()
        if problems:
            await ctx.send("That template won't work: " + "; ".join(problems))
            return
        self.store.set_lobby(guild.id, channel.id, config)
        await ctx.send(
            f"{channel.mention} is now a lobby. Joining it creates a temp channel "
            f"named `{config.template}` — change it with "
            f"`!vc lobby set {channel.mention} template <name>`."
        )

    @vc_lobby.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def vc_lobby_remove(self, ctx: commands.Context, channel: discord.VoiceChannel) -> None:
        """Stop a lobby from spawning channels. Existing temp rooms are left alone."""
        guild = self._guild_or_reply(ctx)
        if not self.store.remove_lobby(guild.id, channel.id):
            await ctx.send(f"{channel.mention} isn't a lobby — nothing to remove.")
            return
        await ctx.send(
            f"{channel.mention} is no longer a lobby. Temp channels it already "
            "made will still delete themselves when they empty."
        )

    @vc_lobby.command(name="list")
    @commands.has_permissions(manage_guild=True)
    async def vc_lobby_list(self, ctx: commands.Context) -> None:
        """Show every lobby and how its temp channels behave."""
        guild = self._guild_or_reply(ctx)
        lobbies = self.store.get_lobbies(guild.id)
        if not lobbies:
            await ctx.send(
                "No lobbies yet. Add one with `!vc lobby add #channel` — "
                "anyone joining that channel gets their own temp room."
            )
            return
        lines = ["**Lobbies in this server:**", ""]
        for lobby_id, cfg in lobbies.items():
            lobby_channel = guild.get_channel(lobby_id)
            name = f"#{lobby_channel.name}" if lobby_channel else f"deleted channel {lobby_id}"
            live = len(self.store.channels_for_lobby(guild.id, lobby_id))
            lines.append(
                f"- **{name}** → `{cfg.template}` "
                f"(limit {cfg.user_limit or 'none'}, "
                f"bitrate {cfg.bitrate or 'lobby default'}, "
                f"delete after {cfg.delete_delay}s empty, {live} live)"
            )
        await ctx.send("\n".join(lines))

    @vc_lobby.command(name="set")
    @commands.has_permissions(manage_guild=True)
    async def vc_lobby_set(
        self,
        ctx: commands.Context,
        channel: discord.VoiceChannel,
        setting: str,
        *,
        value: str,
    ) -> None:
        """Tune a lobby. Settings: template, limit, delay, bitrate, category.

        Examples: `!vc lobby set #lobby template {user} — {game}`,
        `!vc lobby set #lobby limit 5`, `!vc lobby set #lobby delay 30`,
        `!vc lobby set #lobby bitrate 64000`, `!vc lobby set #lobby category none`.
        """
        guild = self._guild_or_reply(ctx)
        config = self.store.get_lobby(guild.id, channel.id)
        if config is None:
            await ctx.send(f"{channel.mention} isn't a lobby yet — add it with `!vc lobby add` first.")
            return
        try:
            self._apply_lobby_setting(ctx, config, setting.lower(), value)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        problems = config.validate()
        if problems:
            await ctx.send("That won't work: " + "; ".join(problems))
            return
        self.store.set_lobby(guild.id, channel.id, config)
        await ctx.send(f"Updated {channel.mention}: `{setting.lower()}` is now `{value}`.")

    def _apply_lobby_setting(
        self, ctx: commands.Context, config: LobbyConfig, setting: str, value: str
    ) -> None:
        """Apply one `!vc lobby set` change, or raise ValueError with a human message."""
        if setting == "template":
            config.template = value.strip()
        elif setting == "limit":
            try:
                config.user_limit = int(value)
            except ValueError:
                raise ValueError("Limit has to be a whole number — 0 means no limit.") from None
        elif setting == "delay":
            try:
                config.delete_delay = int(value)
            except ValueError:
                raise ValueError("Delay has to be a whole number of seconds.") from None
        elif setting == "bitrate":
            if value.lower() in ("none", "auto", "default"):
                config.bitrate = None
            else:
                try:
                    config.bitrate = int(value)
                except ValueError:
                    raise ValueError("Bitrate has to be a number like 64000, or `none`.") from None
        elif setting == "category":
            if value.lower() == "none":
                config.category_id = None
            else:
                category = self._parse_channel_id(ctx, value)
                if category is None:
                    raise ValueError(
                        "Category has to be a category channel mention/id, or `none`."
                    )
                config.category_id = category
        else:
            raise ValueError(
                f"I don't know the setting `{setting}` — try template, limit, delay, "
                "bitrate, or category."
            )

    @staticmethod
    def _parse_channel_id(ctx: commands.Context, value: str) -> int | None:
        """Pull a channel id out of a mention, a raw id, or a channel object."""
        cleaned = value.strip().strip("<>#")
        if cleaned.isdigit():
            channel = ctx.guild.get_channel(int(cleaned)) if ctx.guild else None
            if isinstance(channel, discord.CategoryChannel):
                return channel.id
        return None

    # -- owner commands ----------------------------------------------------

    @vc.command(name="lock")
    async def vc_lock(self, ctx: commands.Context) -> None:
        """Lock your room: nobody new can join until you unlock it."""
        found = await self._owner_channel_of(ctx)
        if found is None:
            return
        _, channel = found
        try:
            await channel.set_permissions(
                ctx.guild.default_role, connect=False, reason=AUDIT_REASON
            )
        except discord.Forbidden:
            await ctx.send("I can't change this channel's permissions — check my role.")
            return
        except discord.HTTPException:
            await ctx.send("Discord didn't take that — try again in a bit.")
            return
        await ctx.send(f"Locked **{channel.name}** — nobody new can join. `!vc unlock` to open it.")

    @vc.command(name="unlock")
    async def vc_unlock(self, ctx: commands.Context) -> None:
        """Open a locked room back up."""
        found = await self._owner_channel_of(ctx)
        if found is None:
            return
        _, channel = found
        try:
            await channel.set_permissions(ctx.guild.default_role, overwrite=None, reason=AUDIT_REASON)
        except discord.Forbidden:
            await ctx.send("I can't change this channel's permissions — check my role.")
            return
        except discord.HTTPException:
            await ctx.send("Discord didn't take that — try again in a bit.")
            return
        await ctx.send(f"Unlocked **{channel.name}** — anyone can join again.")

    @vc.command(name="limit")
    async def vc_limit(self, ctx: commands.Context, limit: int) -> None:
        """Cap how many people can be in your room (0 = no limit)."""
        found = await self._owner_channel_of(ctx)
        if found is None:
            return
        _, channel = found
        if limit < 0 or limit > 99:
            await ctx.send("Limit has to be between 0 (no limit) and 99.")
            return
        try:
            await channel.edit(user_limit=limit, reason=AUDIT_REASON)
        except discord.Forbidden:
            await ctx.send("I can't edit this channel — check my role.")
            return
        except discord.HTTPException:
            await ctx.send("Discord didn't take that — try again in a bit.")
            return
        await ctx.send(f"**{channel.name}** now holds {'no limit' if limit == 0 else f'at most {limit} people'}.")

    @vc.command(name="rename")
    async def vc_rename(self, ctx: commands.Context, *, name: str) -> None:
        """Rename your room. Example: !vc rename late night grind"""
        found = await self._owner_channel_of(ctx)
        if found is None:
            return
        _, channel = found
        clean = sanitise_channel_name(name)
        try:
            await channel.edit(name=clean, reason=AUDIT_REASON)
        except discord.Forbidden:
            await ctx.send("I can't edit this channel — check my role.")
            return
        except discord.HTTPException:
            await ctx.send("Discord didn't take that — try again in a bit.")
            return
        await ctx.send(f"Renamed to **{clean}**.")

    @vc.command(name="kick")
    async def vc_kick(self, ctx: commands.Context, member: discord.Member) -> None:
        """Disconnect someone from your room."""
        found = await self._owner_channel_of(ctx)
        if found is None:
            return
        _, channel = found
        voice = getattr(member, "voice", None)
        if voice is None or voice.channel is None or voice.channel.id != channel.id:
            await ctx.send(f"{member.mention} isn't in this channel.")
            return
        if member.id == ctx.author.id:
            await ctx.send("You can't kick yourself — just leave the channel.")
            return
        try:
            await member.move_to(None, reason=f"{AUDIT_REASON} (kicked by owner)")
        except discord.Forbidden:
            await ctx.send("I can't move that person — check my role.")
            return
        except discord.HTTPException:
            await ctx.send("Discord didn't take that — try again in a bit.")
            return
        await ctx.send(f"Disconnected {member.mention} from **{channel.name}**.")

    @vc.command(name="claim")
    async def vc_claim(self, ctx: commands.Context) -> None:
        """Take over a room whose owner left."""
        found = await self._temp_channel_of(ctx)
        if found is None:
            return
        record, channel = found
        if record.owner_id is not None:
            if record.owner_id == ctx.author.id:
                await ctx.send("You already own this channel.")
            else:
                await ctx.send(f"<@{record.owner_id}> still owns this channel.")
            return
        self.store.set_owner(channel.id, ctx.author.id)
        try:
            await channel.set_permissions(
                ctx.author,
                view_channel=True,
                connect=True,
                manage_channels=True,
                reason=AUDIT_REASON,
            )
        except (discord.Forbidden, discord.HTTPException):
            # Ownership is recorded either way; the permission is a bonus.
            log.warning("could not grant manage to new owner of %s", channel.id)
        await ctx.send(f"**{channel.name}** is yours now.")

    @vc.command(name="transfer")
    async def vc_transfer(self, ctx: commands.Context, member: discord.Member) -> None:
        """Hand your room to someone else in it. Example: !vc transfer @friend"""
        found = await self._owner_channel_of(ctx)
        if found is None:
            return
        _, channel = found
        voice = getattr(member, "voice", None)
        if voice is None or voice.channel is None or voice.channel.id != channel.id:
            await ctx.send(f"{member.mention} isn't in this channel.")
            return
        if member.id == ctx.author.id:
            await ctx.send("You already own this channel.")
            return
        self.store.set_owner(channel.id, member.id)
        try:
            await channel.set_permissions(ctx.author, overwrite=None, reason=AUDIT_REASON)
            await channel.set_permissions(
                member,
                view_channel=True,
                connect=True,
                manage_channels=True,
                reason=AUDIT_REASON,
            )
        except (discord.Forbidden, discord.HTTPException):
            log.warning("could not move manage permission for %s", channel.id)
        await ctx.send(f"**{channel.name}** now belongs to {member.mention}.")

    @vc.command(name="info")
    async def vc_info(self, ctx: commands.Context) -> None:
        """Show who owns this room and how it's set up."""
        found = await self._temp_channel_of(ctx)
        if found is None:
            return
        record, channel = found
        owner = f"<@{record.owner_id}>" if record.owner_id else "nobody — run `!vc claim`"
        lobby = ctx.guild.get_channel(record.lobby_id) if ctx.guild else None
        lobby_name = f"#{lobby.name}" if lobby else "a deleted lobby"
        await ctx.send(
            f"**{channel.name}**\n"
            f"Owner: {owner}\n"
            f"From lobby: {lobby_name}\n"
            f"People here: {len(channel.members)}"
            + (f" (limit {channel.user_limit})" if channel.user_limit else "")
            + f"\nBitrate: {channel.bitrate} kbps"
        )

    @vc.command(name="placeholders")
    async def vc_placeholders(self, ctx: commands.Context) -> None:
        """List the placeholders you can use in a lobby's name template."""
        lines = ["**Placeholders for lobby name templates:**", ""]
        lines += [f"`{{{name}}}` — {desc}" for name, desc in PLACEHOLDERS.items()]
        lines += ["", "Example: `!vc lobby set #lobby template {user} — {game}`"]
        await ctx.send("\n".join(lines))

    # -- errors ------------------------------------------------------------

    @vc.error
    @vc_lobby.error
    @vc_lobby_add.error
    @vc_lobby_remove.error
    @vc_lobby_list.error
    @vc_lobby_set.error
    @vc_lock.error
    @vc_unlock.error
    @vc_limit.error
    @vc_rename.error
    @vc_kick.error
    @vc_claim.error
    @vc_transfer.error
    @vc_info.error
    @vc_placeholders.error
    async def _vc_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """Human replies for the usual mistakes — never a traceback."""
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need Manage Server to change the lobby setup.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                f"Missing something — check `!help {ctx.command}` for what it needs."
            )
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"Couldn't use that argument: {error}")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("VC commands only work inside a server, not in DMs.")
        else:
            log.exception("unexpected error in !vc", exc_info=error)
            await ctx.send("Something went wrong on my side — try again in a bit.")


async def setup(bot: commands.Bot) -> None:
    """Entry point for ``bot.load_extension("vcspawn.cog")``."""
    await bot.add_cog(TempVCCog(bot))
