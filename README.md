# vcspawn

Every Discord server I've run ends up with the same mess: a voice
channel list full of dead rooms called "General 2" that someone made
three months ago and nobody uses. The bots that fix this either want a
dashboard and a database, or they just clone a channel and leave the
cleanup to you.

vcspawn is the middle ground — one discord.py cog you drop into a bot
you already have. You mark a voice channel as a lobby, and anyone who
joins it gets their own temporary room, named the way you configured
it, with them as the owner. When the room empties, it deletes itself
after a short grace period, so a dropped connection doesn't nuke a
conversation mid-sentence.

## What it does

- **Lobby channels** — join one, get your own temp voice channel, get
  moved into it automatically.
- **Name templates** — `{user}'s room` by default; use `{game}`,
  `{username}`, `{n}` (the room's number for that lobby), or `{count}`.
- **Owner controls** — the person who spawned the room can lock it,
  cap it, rename it, kick from it, or hand it to someone else, all
  from Discord. No admin needed.
- **Claim** — when the owner leaves and people are still talking, the
  room goes ownerless instead of auto-promoting whoever joined first.
  Anyone inside can run `!vc claim` to take it over.
- **Grace period** — empty rooms wait a few seconds before deleting,
  so a quick reconnect doesn't lose the room. Configurable per lobby.
- **Orphan cleanup** — on startup, the cog sweeps its records and
  deletes channels left over from before a restart.
- **No database** — one JSON file, saved atomically. Copy it to back
  up, delete it to reset.

## Quickstart

You need Python 3.10+ and a bot token from the
[Discord developer portal](https://discord.com/developers/applications).
Turn on the **Voice States intent** there — without it, Discord never
tells your bot someone joined a lobby, and nothing gets created.

```bash
git clone https://github.com/codewithkrrish/vcspawn.git
cd vcspawn
pip install -r requirements.txt
export DISCORD_TOKEN=<your token>
python example_bot.py
```

Or load it into a bot you already have:

```python
from vcspawn.cog import TempVCCog

# inside setup_hook, or wherever you load cogs:
await bot.add_cog(TempVCCog(bot))
```

Then, in your server, make a voice channel called `create-a-room` and run:

```
!vc lobby add #create-a-room
```

Join it. You get your own room. That's the whole demo.

## Commands

### Admins (need Manage Server)

| Command | What it does |
|---|---|
| `!vc lobby add #channel [template]` | Make a voice channel a lobby |
| `!vc lobby remove #channel` | Stop a lobby from spawning rooms |
| `!vc lobby list` | Show every lobby and its settings |
| `!vc lobby set #channel <setting> <value>` | Tune a lobby (below) |
| `!vc placeholders` | List the template placeholders |

Lobby settings for `!vc lobby set`:

- `template` — the name pattern, e.g. `{user} — {game}`
- `limit` — max people per room, `0` means no limit
- `delay` — seconds a room waits empty before deleting (default 10)
- `bitrate` — e.g. `64000`, or `none` to use the lobby's bitrate
- `category` — a category channel, or `none` to use the lobby's category

### Room owners (inside their temp channel)

| Command | What it does |
|---|---|
| `!vc lock` / `!vc unlock` | Stop / allow new people joining |
| `!vc limit <n>` | Cap the room (`0` = no limit) |
| `!vc rename <name>` | Rename the room |
| `!vc kick @member` | Disconnect someone from the room |
| `!vc claim` | Take over a room whose owner left |
| `!vc transfer @member` | Hand the room to someone in it |
| `!vc info` | Show the room's owner, lobby, and size |

`!vc` on its own shows your room's status and the server's lobbies.

## Example

```
!vc lobby add #create-a-room {user}'s room
!vc lobby set #create-a-room template {user} — {game}
!vc lobby set #create-a-room delay 30
```

Someone playing Valorant joins `#create-a-room` and lands in
`Krrish — Valorant`, which they own. They lock it while the squad
fills up, then hand it to a friend with `!vc transfer @friend` when
they leave. When the last person leaves, the room waits 30 seconds —
long enough for a reconnect — then deletes itself.

## How it works

```
vcspawn/
    naming.py    # name templates: render, sanitise, member values
    store.py     # the JSON store: lobbies + temp channels, atomic saves
    policy.py    # the rules, as pure functions (numbering, deletion)
    cog.py       # the discord.py cog: listeners + !vc commands
```

The rules live in `policy.py` on purpose — pure functions, no Discord
objects, so they're easy to test and hard to break by accident. The
cog only does the Discord work: creating channels, moving members,
replying to commands.

State lives in `vcspawn.json` next to the bot. If that file gets
corrupted, the cog logs it and keeps running with in-memory config
until you fix the file — it never takes the whole bot down with it.

## Limitations

- **Prefix commands only.** No slash commands yet — the `!vc` group
  is plain discord.py prefix commands, so it works in any bot without
  syncing an application command tree.
- **One JSON file, no locking.** If you run the same bot process twice
  against the same file, the last save wins. Don't do that.
- **Permissions are inherited from the lobby.** A temp channel copies
  the lobby's permission overwrites and adds the owner. If your lobby
  is visible to everyone, so is the room — use `!vc lock` or set the
  lobby up in a restricted category.
- **The grace period is per lobby, not per room.** Every room from
  the same lobby waits the same number of seconds before deleting.

## Running the tests

```bash
pip install pytest discord.py
pytest
```

58 tests, no bot or token needed — everything runs against mocked
Discord objects.

## License

MIT — see [LICENSE](LICENSE).

## Author

Built by [Krrish](https://github.com/codewithkrrish) — a discord.py cog
I wrote because I was tired of cleaning up dead voice channels by hand.
