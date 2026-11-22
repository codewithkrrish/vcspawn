"""Minimal bot that runs the vcspawn cog.

Set DISCORD_TOKEN, then:

    python example_bot.py

The only intent vcspawn strictly needs is ``voice_states`` — without
it, Discord never tells the bot someone joined a lobby. Server Members
is not needed; the cog never looks at the member list for joins.

In your server, make a voice channel called "create-a-room", then run:

    !vc lobby add #create-a-room

Join it, and you get your own temp channel. That's the whole demo.
"""

import logging
import os
import sys

import discord
from discord.ext import commands

from vcspawn.cog import TempVCCog

logging.basicConfig(level=logging.INFO)


def main() -> int:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("Set DISCORD_TOKEN first — see README.md.", file=sys.stderr)
        return 2

    intents = discord.Intents.default()
    intents.voice_states = True

    class SpawnBot(commands.Bot):
        async def setup_hook(self) -> None:
            await self.add_cog(TempVCCog(self))

    bot = SpawnBot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready() -> None:
        print(f"Logged in as {bot.user} — lobbies ready.")

    @bot.event
    async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
        # The cog handles its own errors; this is the backstop for
        # anything else (unknown command, bad prefix use, ...).
        if isinstance(error, commands.CommandNotFound):
            return
        print(f"command error: {error}", file=sys.stderr)

    # The cog is loaded in setup_hook above — before login, so the
    # on_ready sweep runs with a full view of the guilds.
    bot.run(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
