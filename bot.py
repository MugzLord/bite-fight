import os
import json
import random
import asyncio
import datetime
from collections import defaultdict

import discord
from discord.ext import commands
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont


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
        # ADD
        self.lobby_view = None
        self._ctx = None  # store start context for later

    def reset(self):
        self.in_lobby = False
        self.running = False
        self.players = []
        self.hp.clear()
        self.bleed.clear()
        self.round_num = 0
        self.task = None
        # ADD
        self.lobby_view = None
        self._ctx = None

# Channel ID -> Game
GAMES: dict[int, BiteFightGame] = {}

class LobbyView(discord.ui.View):
    def __init__(self, game: "BiteFightGame", host: discord.Member, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.game = game
        self.host = host
        self.message: discord.Message | None = None

    async def on_timeout(self):
        # auto-begin when the lobby times out (if still open)
        if self.game.in_lobby:
            try:
                # disable buttons
                for c in self.children:
                    if isinstance(c, discord.ui.Button):
                        c.disabled = True
                if self.message:
                    await self.message.edit(view=self)
            except Exception:
                pass
            # call your existing begin
            ctx = self.game._ctx
            await bf_begin(ctx)

    async def update_counter(self):
        if not self.message:
            return
        count = len(self.game.players)
        embed = self.message.embeds[0]
        # update the "0 tributes have volunteered" line
        field_name = "Status"
        status_line = f"⚔️ {count} tribute{'s' if count != 1 else ''} have volunteered"
        # Replace or add field
        found = False
        for i, f in enumerate(embed.fields):
            if f.name == field_name:
                embed.set_field_at(i, name=field_name, value=status_line, inline=False)
                found = True
                break
        if not found:
            embed.add_field(name=field_name, value=status_line, inline=False)
        try:
            await self.message.edit(embed=embed, view=self)
        except Exception:
            pass

    @discord.ui.button(label="Join", emoji="🍔", style=discord.ButtonStyle.success)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        if not self.game.in_lobby:
            return await interaction.response.send_message("Lobby closed.", ephemeral=True)
        if user.bot:
            return await interaction.response.send_message("Bots cannot join.", ephemeral=True)
        if user in self.game.players:
            return await interaction.response.send_message("You are already in.", ephemeral=True)
        self.game.players.append(user)
        self.game.hp[user.id] = self.game.max_hp
        await interaction.response.send_message(f"{user.display_name} joined the arena.", ephemeral=True)
        await self.update_counter()

    @discord.ui.button(label="Tributes", emoji="⚔️", style=discord.ButtonStyle.secondary)
    async def tributes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        names = ", ".join(p.display_name for p in self.game.players) or "None yet"
        await interaction.response.send_message(f"Current tributes: {names}", ephemeral=True)

    @discord.ui.button(label="Let the battle begin", emoji="🍽️", style=discord.ButtonStyle.primary)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # only host can force start
        if interaction.user.id != self.host.id:
            return await interaction.response.send_message("Only the host can start.", ephemeral=True)
        if not self.game.in_lobby:
            return await interaction.response.send_message("Already started.", ephemeral=True)
        # disable buttons and start
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass
        ctx = self.game._ctx
        await bf_begin(ctx)


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

async def fetch_avatar(member: discord.Member, size=256) -> Image.Image:
    # Get the member's current avatar as bytes; fallback to a solid placeholder
    try:
        b = await member.display_avatar.replace(size=size, format="png").read()
        im = Image.open(BytesIO(b)).convert("RGBA")
    except Exception:
        im = Image.new("RGBA", (size, size), (40, 40, 40, 255))
    # ensure square
    im = im.resize((size, size), Image.LANCZOS)
    return im

def circle_mask(size):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.ellipse((0, 0, size, size), fill=255)
    return m

def rounded_square(im: Image.Image, radius=36):
    # apply rounded corners
    w, h = im.size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, w, h), radius=radius, fill=255)
    im.putalpha(mask)
    return im

def fit_center(im: Image.Image, box_w, box_h):
    return im.resize((box_w, box_h), Image.LANCZOS)

def load_asset(name: str) -> Image.Image:
    path = os.path.join("assets", name)
    return Image.open(path).convert("RGBA")

async def build_versus_card(attacker: discord.Member, target: discord.Member, action_text: str) -> BytesIO:
    # Canvas
    W, H = 900, 500
    bg = Image.new("RGBA", (W, H), (24, 24, 24, 255))

    # Subtle gradient
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(g)
    for y in range(H):
        a = int(180 * (y / H))
        draw.line([(0, y), (W, y)], fill=(255, 255, 255, int(a*0.08)))
    bg = Image.alpha_composite(bg, g)

    # Avatar boxes
    pad = 32
    face = 360
    left_box = (pad, pad, pad + face, pad + face)
    right_box = (W - pad - face, pad, W - pad, pad + face)

    # Fetch avatars
    la = await fetch_avatar(attacker, size=512)
    ra = await fetch_avatar(target, size=512)

    la = fit_center(la, face, face)
    ra = fit_center(ra, face, face)

    la = rounded_square(la, radius=40)
    ra = rounded_square(ra, radius=40)

    card = bg.copy()
    card.alpha_composite(la, dest=(left_box[0], left_box[1]))
    card.alpha_composite(ra, dest=(right_box[0], right_box[1]))

    # Crossed swords overlay (center)
    try:
        swords = load_asset("swords.png")
        # scale down if needed
        sw = int(W * 0.35)
        sh = int(swords.height * (sw / swords.width))
        swords = swords.resize((sw, sh), Image.LANCZOS)
        sx = (W - sw) // 2
        sy = int(H * 0.18)
        card.alpha_composite(swords, dest=(sx, sy))
    except Exception:
        pass  # safe if asset missing

    # Action text strip at bottom
    strip_h = 92
    strip = Image.new("RGBA", (W - pad*2, strip_h), (0, 0, 0, 170))
    card.alpha_composite(strip, dest=(pad, H - pad - strip_h))
    draw = ImageDraw.Draw(card)

    # Font: Pillow will fall back; you can drop a TTF into assets and load it if you want
    try:
        fnt = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
    except Exception:
        fnt = ImageFont.load_default()

    # Truncate text if too long
    text = action_text.strip()
    max_px = W - pad*2 - 30
    while fnt.getlength(text) > max_px and len(text) > 8:
        text = text[:-4] + "..."

    tx = pad + 16
    ty = H - pad - strip_h + (strip_h - fnt.getbbox(text)[3]) // 2 - 6
    draw.text((tx, ty), text, font=fnt, fill=(255, 255, 255, 255))

    # Return as bytes for Discord upload
    buf = BytesIO()
    card.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ---- Commands ----
@bot.command(name="bf_start")
async def bf_start(ctx):
    chan_id = ctx.channel.id
    if chan_id in GAMES and (GAMES[chan_id].in_lobby or GAMES[chan_id].running):
        return await ctx.reply("A game is already active in this channel.")

    # load banter
    try:
        with open("banter.json", "r", encoding="utf-8") as f:
            banter = json.load(f)
    except Exception as e:
        return await ctx.reply(f"Failed to load banter.json: {e}")

    game = BiteFightGame(ctx.channel, banter)
    GAMES[chan_id] = game
    game.in_lobby = True
    game._ctx = ctx

    title = f"{ctx.guild.name or 'Bite & Fight'} — Arena"
    subtitle = "Part 1 - Setting The Table"
    desc = "🍔 to join the fight!\n🍽️ to let the battle begin!"

    embed = discord.Embed(
        title=title,
        description=f"**{subtitle}**\n\n{desc}",
        color=discord.Color.dark_gold()
    )
    # top-right badge image (optional: replace with your own URL)
    embed.set_thumbnail(url="https://i.imgur.com/4Zb9o2p.png")  # placeholder burger/flame badge
    embed.add_field(name="Status", value="⚔️ 0 tributes have volunteered", inline=False)
    embed.set_footer(text=f"Host: {ctx.author.display_name} • Lobby closes in 30s")

    view = LobbyView(game, host=ctx.author, timeout=30.0)
    game.lobby_view = view
    msg = await ctx.send(embed=embed, view=view)
    view.message = msg  # so the view can edit the message


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

        # choose a key play to illustrate (prefer messages with attacker/target)
        key_play = None
        key_attacker = None
        key_target = None
        for e in reversed(events):
            # very cheap parse: look up current attacker/target names in text
            # better: capture when you append events.
            for a in alive_players(game):
                for t in game.players:
                    if a.id == t.id:
                        continue
                    a_name = a.display_name
                    t_name = t.display_name
                    if a_name in e and t_name in e:
                        key_play = e
                        key_attacker = a
                        key_target = t
                        break
                if key_play:
                    break
            if key_play:
                break

        file = None
        if key_play and key_attacker and key_target:
            try:
                img_bytes = await build_versus_card(key_attacker, key_target, key_play)
                file = discord.File(img_bytes, filename=f"round_{game.round_num}.png")
            except Exception:
                file = None

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
        
        # Try to pick a key play that mentions both an attacker and a target
        key_play = None
        key_attacker = None
        key_target = None
        for e in reversed(events):
            found = False
            for a in alive_players(game):
                for t in game.players:
                    if a.id == t.id:
                        continue
                    if a.display_name in e and t.display_name in e:
                        key_play = e
                        key_attacker = a
                        key_target = t
                        found = True
                        break
                if found:
                    break
            if found:
                break
        
        file = None
        if key_play and key_attacker and key_target:
            try:
                img_bytes = await build_versus_card(key_attacker, key_target, key_play)
                file = discord.File(img_bytes, filename=f"round_{game.round_num}.png")
            except Exception:
                file = None  # keep the round going even if image fails
        
        hp_board = ", ".join(f"{p.display_name}({game.hp[p.id]})" for p in alive_now)
        embed = discord.Embed(
            title=f"Bite & Fight — Round {game.round_num}",
            description=line("round_intro", game.banter) or "",
            color=discord.Color.dark_red(),
            timestamp=datetime.datetime.utcnow()
        )
        embed.add_field(name="Events", value="\n".join(events)[:1024], inline=False)
        embed.add_field(name="HP", value=hp_board[:1024], inline=False)
        
        if file:
            embed.set_image(url=f"attachment://round_{game.round_num}.png")
            await game.channel.send(embed=embed, file=file)
        else:
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
