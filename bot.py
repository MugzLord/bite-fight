import os
import json
import random
import asyncio
import datetime
from collections import defaultdict

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("COMMAND_PREFIX", "!")
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=INTENTS)

# ---- Game State (per channel) ----
class BiteFightGame:
    def __init__(self, channel: discord.TextChannel, banter):
        self.channel = channel
        self.banter = banter
        self.in_lobby = False
        self.running = False
        self.players = []           # list[discord.Member]
        self.hp = {}                # member_id -> int
        self.bleed = defaultdict(int)  # member_id -> bleed stacks (damage per round)
        self.round_num = 0
        self.max_hp = 100
        self.task = None

    def reset(self):
        self.in_lobby = False
        self.running = False
        self.players = []
        self.hp.clear()
        self.bleed.clear()
        self.round_num = 0
        self.task = None

# Channel ID -> Game
GAMES: dict[int, BiteFightGame] = {}

# ---- Banter helpers ----
def line(pool_name, banter):
    pool = banter.get(pool_name, [])
    return random.choice(pool) if pool else ""

def format_line(t, **kw):
    s = t
    for k, v in kw.items():
        s = s.replace(f"[{k}]", str(v))
    return s

# ---- Utils ----
def alive_players(game: BiteFightGame):
    return [p for p in game.players if game.hp.get(p.id, 0) > 0]

def pick_target(game: BiteFightGame, attacker: discord.Member):
    candidates = [p for p in alive_players(game) if p.id != attacker.id]
    return random.choice(candidates) if candidates else None

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

# ---- Commands ----
@bot.command(name="bf_start")
async def bf_start(ctx):
    """Start a Bite & Fight lobby in this channel."""
    chan_id = ctx.channel.id
    if chan_id in GAMES and (GAMES[chan_id].in_lobby or GAMES[chan_id].running):
        return await ctx.reply("A game is already active in this channel.")

    # load banter once per start (so you can live edit the json between games)
    try:
        with open("banter.json", "r", encoding="utf-8") as f:
            banter = json.load(f)
    except Exception as e:
        return await ctx.reply(f"Failed to load banter.json: {e}")

    game = BiteFightGame(ctx.channel, banter)
    GAMES[chan_id] = game
    game.in_lobby = True

    join_text = line("join_prompt", banter) or "Bite & Fight is open. Type !bf_join to enter."
    embed = discord.Embed(
        title="Bite & Fight — Lobby Open",
        description=f"{join_text}\nLobby closes in **30 seconds**.",
        color=discord.Color.red(),
        timestamp=datetime.datetime.utcnow()
    )
    embed.set_footer(text="Use !bf_join to enter. Host: " + ctx.author.display_name)
    await ctx.send(embed=embed)

    # 30s join window (with a midpoint update)
    async def lobby_timer():
        await asyncio.sleep(15)
        if game.in_lobby:
            names = ", ".join(p.display_name for p in game.players) or "None yet"
            await ctx.send(f"15s left. Joined: {names}")
        await asyncio.sleep(15)
        if game.in_lobby:
            await bf_begin(ctx)

    game.task = bot.loop.create_task(lobby_timer())

@bot.command(name="bf_join")
async def bf_join(ctx):
    """Join the current Bite & Fight lobby."""
    chan_id = ctx.channel.id
    game = GAMES.get(chan_id)
    if not game or not game.in_lobby:
        return await ctx.reply("No open lobby. Start one with !bf_start")

    if ctx.author in game.players:
        return await ctx.reply("You are already in.")

    if ctx.author.bot:
        return  # ignore bots

    game.players.append(ctx.author)
    game.hp[ctx.author.id] = game.max_hp

    joined_line = line("on_join", game.banter)
    if joined_line:
        await ctx.send(format_line(joined_line, player=ctx.author.display_name))
    else:
        await ctx.send(f"{ctx.author.display_name} joined the arena.")

@bot.command(name="bf_begin")
async def bf_begin(ctx):
    """Force start the game (if lobby is open)."""
    chan_id = ctx.channel.id
    game = GAMES.get(chan_id)
    if not game or not game.in_lobby:
        return await ctx.reply("No open lobby to begin.")

    # Require at least 2 players
    if len(game.players) < 2:
        await ctx.send("Not enough players joined. Cancelling.")
        game.reset()
        return

    game.in_lobby = False
    game.running = True
    game.round_num = 0

    intro = line("intro", game.banter) or "The gates close. The crowd roars. Fight."
    embed = discord.Embed(
        title="Bite & Fight — Match Begins",
        description=intro,
        color=discord.Color.dark_red(),
        timestamp=datetime.datetime.utcnow()
    )
    roster = "\n".join(f"• {p.display_name} — {game.max_hp} HP" for p in game.players)
    embed.add_field(name="Combatants", value=roster, inline=False)
    await ctx.send(embed=embed)

    # run rounds
    await run_game(ctx, game)

@bot.command(name="bf_stop")
async def bf_stop(ctx):
    """Stop the current game."""
    chan_id = ctx.channel.id
    game = GAMES.get(chan_id)
    if not game or not (game.in_lobby or game.running):
        return await ctx.reply("No active game in this channel.")

    game.reset()
    await ctx.send("Game stopped.")

@bot.command(name="bf_help")
async def bf_help(ctx):
    msg = (
        f"**Bite & Fight Commands**\n"
        f"{PREFIX}bf_start — open a lobby (30s join window)\n"
        f"{PREFIX}bf_join — join the lobby\n"
        f"{PREFIX}bf_begin — force begin the match\n"
        f"{PREFIX}bf_stop — stop the current game\n"
        f"{PREFIX}bf_help — this help\n\n"
        "Rounds are automatic. One embed per round keeps the channel tidy."
    )
    await ctx.send(msg)

# ---- Core game loop ----
async def run_game(ctx, game: BiteFightGame):
    await asyncio.sleep(2)

    while game.running:
        game.round_num += 1
        events = []

        # Apply bleed first
        for p in list(alive_players(game)):
            b = game.bleed.get(p.id, 0)
            if b > 0 and game.hp[p.id] > 0:
                dmg = b
                game.hp[p.id] = clamp(game.hp[p.id] - dmg, 0, game.max_hp)
                events.append(format_line(
                    line("bleed_tick", game.banter) or "[player] suffers bleed for [dmg] damage.",
                    player=p.display_name, dmg=dmg, hp=game.hp[p.id]
                ))
                if game.hp[p.id] <= 0:
                    events.append(format_line(
                        line("death_bleed", game.banter) or "[player] succumbs to bleeding.",
                        player=p.display_name
                    ))

        # Gather attackers in random order
        attackers = list(alive_players(game))
        random.shuffle(attackers)

        for attacker in attackers:
            if game.hp.get(attacker.id, 0) <= 0:
                continue  # died from bleed

            target = pick_target(game, attacker)
            if not target:
                break  # only one alive

            # Decide action
            # Bite: 55% chance, 8-18 dmg, 30% apply bleed (2-5 per round)
            # Fight: 45% chance, 14-28 dmg, 15% crit x1.5
            do_bite = random.random() < 0.55

            if do_bite:
                miss = random.random() < 0.15
                if miss:
                    events.append(format_line(
                        line("bite_miss", game.banter) or "[attacker] snaps at air. Miss.",
                        attacker=attacker.display_name, target=target.display_name
                    ))
                else:
                    dmg = random.randint(8, 18)
                    apply_bleed = random.random() < 0.30
                    if apply_bleed:
                        stack = random.randint(2, 5)
                        game.bleed[target.id] += stack
                        tag = format_line(
                            line("bite_bleed", game.banter) or "bleed applied (+[bleed] per round)",
                            bleed=stack
                        )
                    else:
                        tag = ""
                    game.hp[target.id] = clamp(game.hp[target.id] - dmg, 0, game.max_hp)
                    events.append(format_line(
                        line("bite_hit", game.banter) or "[attacker] bites [target] for [dmg]. [tag] [target] at [hp] HP.",
                        attacker=attacker.display_name, target=target.display_name, dmg=dmg, hp=game.hp[target.id], tag=tag
                    ))
                    if game.hp[target.id] <= 0:
                        events.append(format_line(
                            line("death_bite", game.banter) or "[target] falls to the fangs.",
                            attacker=attacker.display_name, target=target.display_name
                        ))
            else:
                miss = random.random() < 0.12
                if miss:
                    events.append(format_line(
                        line("fight_miss", game.banter) or "[attacker] swings wide at [target]. Miss.",
                        attacker=attacker.display_name, target=target.display_name
                    ))
                else:
                    base = random.randint(14, 28)
                    crit = random.random() < 0.15
                    dmg = int(base * 1.5) if crit else base
                    game.hp[target.id] = clamp(game.hp[target.id] - dmg, 0, game.max_hp)
                    key = "fight_crit" if crit else "fight_hit"
                    template = line(key, game.banter) or "[attacker] hits [target] for [dmg]. [target] at [hp] HP."
                    events.append(format_line(
                        template,
                        attacker=attacker.display_name, target=target.display_name, dmg=dmg, hp=game.hp[target.id]
                    ))
                    if game.hp[target.id] <= 0:
                        events.append(format_line(
                            line("death_fight", game.banter) or "[target] is knocked out.",
                            attacker=attacker.display_name, target=target.display_name
                        ))

        # Check end condition
        alive_now = alive_players(game)
        if len(alive_now) <= 1:
            winner = alive_now[0] if alive_now else None
            title = "Bite & Fight — Match Over"
            if winner:
                desc = format_line(
                    line("winner", game.banter) or "[winner] stands alone. Victory.",
                    winner=winner.display_name
                )
            else:
                desc = line("no_winner", game.banter) or "Everyone fell. No winner this time."

            embed = discord.Embed(
                title=title,
                description=desc,
                color=discord.Color.gold(),
                timestamp=datetime.datetime.utcnow()
            )
            if events:
                embed.add_field(
                    name=f"Round {game.round_num} Summary",
                    value="\n".join(events)[:1024],
                    inline=False
                )

            board = "\n".join(
                f"{p.display_name}: {game.hp.get(p.id, 0)} HP"
                for p in game.players
            )
            embed.add_field(name="Final HP", value=board[:1024], inline=False)

            await game.channel.send(embed=embed)
            game.reset()
            return

        # Post one embed for the round
        if not events:
            events.append("The fighters circle, waiting for an opening.")

        hp_board = ", ".join(f"{p.display_name}({game.hp[p.id]})" for p in alive_now)
        embed = discord.Embed(
            title=f"Bite & Fight — Round {game.round_num}",
            description=line("round_intro", game.banter) or "",
            color=discord.Color.dark_red(),
            timestamp=datetime.datetime.utcnow()
        )
        embed.add_field(name="Events", value="\n".join(events)[:1024], inline=False)
        embed.add_field(name="HP", value=hp_board[:1024], inline=False)
        await game.channel.send(embed=embed)

        # Short pause between rounds
        await asyncio.sleep(2.2)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} with prefix {PREFIX}")

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN env var.")
    bot.run(TOKEN)
