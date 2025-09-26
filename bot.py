import os
import json
import random
import asyncio
import datetime
from collections import defaultdict
from io import BytesIO

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

# =========================
# Config / Env
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("COMMAND_PREFIX", "!")
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

# Files
STATS_FILE = os.getenv("BF_STATS_FILE", "bf_stats.json")                 # global/server wins/kills
TOURNEY_STATE_FILE = os.getenv("BF_TOURNEY_STATE", "bf_tourney.json")    # current tournament state
TOURNEY_STATS_FILE = os.getenv("BF_TOURNEY_STATS", "bf_tourney_stats.json")  # per-tournament stats
PRIZES_FILE = os.getenv("BF_PRIZES_FILE", "bf_prizes.json")              # wishlist/credit prize ledger

# Per-channel default ante (used when starting a new tournament)
CHANNEL_ANTE = defaultdict(lambda: int(os.getenv("BF_ANTE", "100")))

# =========================
# Small JSON helpers
# =========================
def _json_load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _json_save(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# Global/server stats (wins, kills)
def _load_stats():
    return _json_load(STATS_FILE, {"global": {"wins": {}, "kills": {}}, "guilds": {}})

def _save_stats(stats):
    _json_save(STATS_FILE, stats)

def _bump(dct, key, by=1):
    key = str(key)
    dct[key] = dct.get(key, 0) + by

# Tournament state & stats
def _tourney_state_load():
    return _json_load(TOURNEY_STATE_FILE, {
        "active": False, "id": None, "name": "",
        "ante": 100, "channel_id": None, "created_at": None,
        "games_target": 0, "games_played": 0,
        "prize_mode": "credits",      # "credits" | "wishlist" | "mixed"
        "wishlist_count": 2,
        "mixed_credits_pct": 70
    })

def _tourney_state_save(s):
    _json_save(TOURNEY_STATE_FILE, s)

def _tourney_stats_all():
    return _json_load(TOURNEY_STATS_FILE, {})

def _tourney_stats_save(all_stats):
    _json_save(TOURNEY_STATS_FILE, all_stats)

# Prize ledger
def _prizes_load():
    return _json_load(PRIZES_FILE, {"seq": 0, "open": [], "closed": []})

def _prizes_save(d):
    _json_save(PRIZES_FILE, d)

# Find a local asset by possible names
def _find_asset(names):
    for n in names:
        p = os.path.join("assets", n)
        if os.path.exists(p):
            return p
    return None

# =========================
# Bot
# =========================
bot = commands.Bot(command_prefix=PREFIX, intents=INTENTS)

# ---- Game State (per channel) ----
class BiteFightGame:
    def __init__(self, channel: discord.TextChannel, banter):
        self.channel = channel
        self.banter = banter
        self.in_lobby = False
        self.running = False
        self.players = []                # list[discord.Member]
        self.hp = {}                     # member_id -> int
        self.bleed = defaultdict(int)    # member_id -> bleed stacks
        self.round_num = 0
        self.max_hp = 100
        self.task = None

        # Lobby view / context
        self.lobby_view = None
        self._ctx = None

        # Per-match stats
        self.start_time = None
        self.kills = defaultdict(int)    # attacker_id -> kills this match

        # Tournament context (snapshotted at creation/reset)
        state = _tourney_state_load()
        self.is_tournament = bool(state.get("active", False))
        self.entry_fee = int(state.get("ante", 100)) if self.is_tournament else 0
        self.pot = 0
        self.buyins = {}                 # user_id -> amount (for display only)

    def reset(self):
        self.in_lobby = False
        self.running = False
        self.players = []
        self.hp.clear()
        self.bleed.clear()
        self.round_num = 0
        self.task = None

        self.lobby_view = None
        self._ctx = None

        self.start_time = None
        self.kills.clear()

        state = _tourney_state_load()
        self.is_tournament = bool(state.get("active", False))
        self.entry_fee = int(state.get("ante", 100)) if self.is_tournament else 0
        self.pot = 0
        self.buyins.clear()

# Channel ID -> Game
GAMES: dict[int, BiteFightGame] = {}

# ---- Lobby UI ----
class LobbyView(discord.ui.View):
    def __init__(self, game: "BiteFightGame", host: discord.Member, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.game = game
        self.host = host
        self.message: discord.Message | None = None

    async def on_timeout(self):
        if self.game.in_lobby:
            try:
                for c in self.children:
                    if isinstance(c, discord.ui.Button):
                        c.disabled = True
                if self.message:
                    await self.message.edit(view=self)
            except Exception:
                pass
            ctx = self.game._ctx
            await bf_begin(ctx)

    async def update_counter(self):
        if not self.message:
            return
        embed = self.message.embeds[0]
        count = len(self.game.players)
        status_line = f"⚔️ {count} tribute{'s' if count != 1 else ''} have volunteered"

        def set_field(name, value):
            for i, f in enumerate(embed.fields):
                if f.name == name:
                    embed.set_field_at(i, name=name, value=value, inline=False)
                    return
            embed.add_field(name=name, value=value, inline=False)

        set_field("Status", status_line)
        if self.game.is_tournament:
            set_field("Pot", f"💰 {self.game.pot} • Entry {self.game.entry_fee}")

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

        # Tournament: auto-add entry to pot (no balances)
        if self.game.is_tournament:
            self.game.pot += self.game.entry_fee
            self.game.buyins[user.id] = self.game.buyins.get(user.id, 0) + self.game.entry_fee

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
        if interaction.user.id != self.host.id:
            return await interaction.response.send_message("Only the host can start.", ephemeral=True)
        if not self.game.in_lobby:
            return await interaction.response.send_message("Already started.", ephemeral=True)
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

# ---- Versus Card (swords overlay) ----
async def fetch_avatar(member: discord.Member, size=256) -> Image.Image:
    try:
        b = await member.display_avatar.replace(size=size, format="png").read()
        im = Image.open(BytesIO(b)).convert("RGBA")
    except Exception:
        im = Image.new("RGBA", (size, size), (40, 40, 40, 255))
    im = im.resize((size, size), Image.LANCZOS)
    return im

def rounded_square(im: Image.Image, radius=36):
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
    W, H = 900, 500
    bg = Image.new("RGBA", (W, H), (24, 24, 24, 255))
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(g)
    for y in range(H):
        a = int(180 * (y / H))
        draw.line([(0, y), (W, y)], fill=(255, 255, 255, int(a*0.08)))
    bg = Image.alpha_composite(bg, g)

    pad = 32
    face = 360
    left_box = (pad, pad)
    right_box = (W - pad - face, pad)

    la = await fetch_avatar(attacker, size=512)
    ra = await fetch_avatar(target, size=512)
    la = fit_center(la, face, face)
    ra = fit_center(ra, face, face)
    la = rounded_square(la, radius=40)
    ra = rounded_square(ra, radius=40)

    card = bg.copy()
    card.alpha_composite(la, dest=left_box)
    card.alpha_composite(ra, dest=right_box)

    try:
        swords = load_asset("swords.png")
        sw = int(W * 0.35)
        sh = int(swords.height * (sw / swords.width))
        swords = swords.resize((sw, sh), Image.LANCZOS)
        sx = (W - sw) // 2
        sy = int(H * 0.18)
        card.alpha_composite(swords, dest=(sx, sy))
    except Exception:
        pass

    strip_h = 92
    strip = Image.new("RGBA", (W - pad*2, strip_h), (0, 0, 0, 170))
    card.alpha_composite(strip, dest=(pad, H - pad - strip_h))
    draw = ImageDraw.Draw(card)
    try:
        fnt = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
    except Exception:
        fnt = ImageFont.load_default()

    text = action_text.strip()
    max_px = W - pad*2 - 30
    while fnt.getlength(text) > max_px and len(text) > 8:
        text = text[:-4] + "..."

    tx = pad + 16
    ty = H - pad - strip_h + (strip_h - fnt.getbbox(text)[3]) // 2 - 6
    draw.text((tx, ty), text, font=fnt, fill=(255, 255, 255, 255))

    buf = BytesIO()
    card.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

# =========================
# Commands
# =========================
@bot.command(name="bf_start")
async def bf_start(ctx):
    chan_id = ctx.channel.id
    if chan_id in GAMES and (GAMES[chan_id].in_lobby or GAMES[chan_id].running):
        return await ctx.reply("A game is already active in this channel.")
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
    embed = discord.Embed(title=title, description=f"**{subtitle}**\n\n{desc}", color=discord.Color.dark_gold())

    # attach a local logo as thumbnail if available
    thumb_file = None
    logo_path = _find_asset(["logo.png", "logo.jpg", "logo.jpeg"])
    if logo_path:
        embed.set_thumbnail(url="attachment://logo.png")
        thumb_file = discord.File(logo_path, filename="logo.png")

    embed.add_field(name="Status", value="⚔️ 0 tributes have volunteered", inline=False)
    if game.is_tournament:
        embed.add_field(name="Pot", value=f"💰 {game.pot} • Entry {game.entry_fee}", inline=False)
    embed.set_footer(text=f"Host: {ctx.author.display_name} • Lobby closes in 60s")

    view = LobbyView(game, host=ctx.author, timeout=30.0)
    game.lobby_view = view

    if thumb_file:
        msg = await ctx.send(embed=embed, view=view, file=thumb_file)
    else:
        msg = await ctx.send(embed=embed, view=view)
    view.message = msg

    async def lobby_timer():
        await asyncio.sleep(15)
        if game.in_lobby:
            names = ", ".join(p.display_name for p in game.players) or "None yet"
            await ctx.send(f"15s left. Joined: {names}")
        await asyncio.sleep(15)
        if game.in_lobby:
            await bf_begin(ctx)

    game.task = bot.loop.create_task(lobby_timer())

# NOTE: removed the !bf_join text command (buttons only)

# Make bf_begin internal (no command decorator)
async def bf_begin(ctx):
    """Begin the match (called by the button/timeout)."""
    chan_id = ctx.channel.id
    game = GAMES.get(chan_id)
    if not game or not game.in_lobby:
        return await ctx.reply("No open lobby to begin.")
    if len(game.players) < 2:
        await ctx.send("Not enough players joined. Cancelling.")
        game.reset()
        return

    game.in_lobby = False
    game.running = True
    game.round_num = 0
    game.start_time = datetime.datetime.utcnow()

    intro = line("intro", game.banter) or "The gates close. The crowd roars. Fight."
    embed = discord.Embed(
        title="Bite & Fight — Match Begins",
        description=intro,
        color=discord.Color.dark_red(),
        timestamp=datetime.datetime.utcnow()
    )
    roster = "\n".join(f"• {p.display_name} — {game.max_hp} HP" for p in game.players)
    embed.add_field(name="Combatants", value=roster, inline=False)
    if game.is_tournament:
        s = _tourney_state_load()
        embed.add_field(name="Pot", value=f"💰 {game.pot} • Entry {game.entry_fee}", inline=True)
        embed.add_field(name="Prize mode", value=s.get("prize_mode", "credits").title(), inline=True)
    await ctx.send(embed=embed)

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
        f"**Bite & Fight — Commands**\n"
        f"{PREFIX}bf_start — open a lobby (buttons handle Join/Start)\n"
        f"{PREFIX}bf_stop — stop the current game\n"
        f"{PREFIX}bf_tourney_start <games> <entry> [name] — start a tournament\n"
        f"{PREFIX}bf_tourney_end — end & publish final leaderboard\n"
        f"{PREFIX}bf_tourney_lb — show current leaderboard\n"
        f"{PREFIX}bf_tourney_info — show tournament progress\n"
        f"{PREFIX}bf_prize_mode <credits|wishlist|mixed> [credits%] [wishlistCount]\n"
        f"{PREFIX}bf_prizes / {PREFIX}bf_prize_done <id> — manage prize log\n"
        f"{PREFIX}bf_pot — show current pot (if tournament)\n"
        f"{PREFIX}bf_profile — your lifetime wins/kills\n"
        f"{PREFIX}bf_set_ante <amount> — set default entry for the next tournament\n"
    )
    await ctx.send(msg)

# ---- Core game loop ----
async def run_game(ctx, game: BiteFightGame):
    await asyncio.sleep(2)

    while game.running:
        game.round_num += 1
        events = []

        # Apply bleed first (no kill credit for bleed)
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

        # Attackers act in random order
        attackers = list(alive_players(game))
        random.shuffle(attackers)

        for attacker in attackers:
            if game.hp.get(attacker.id, 0) <= 0:
                continue  # might have died to bleed

            target = pick_target(game, attacker)
            if not target:
                break  # only one alive

            do_bite = random.random() < 0.55  # Bite vs Fight

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
                        game.kills[attacker.id] += 1
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
                        game.kills[attacker.id] += 1
                        events.append(format_line(
                            line("death_fight", game.banter) or "[target] is knocked out.",
                            attacker=attacker.display_name, target=target.display_name
                        ))

        # Check end condition
        alive_now = alive_players(game)
        if len(alive_now) <= 1:
            winner = alive_now[0] if alive_now else None

            # Compute time survived
            ended_at = datetime.datetime.utcnow()
            dur_secs = int((ended_at - game.start_time).total_seconds()) if game.start_time else 0

            # Persist lifetime stats (global/server)
            stats = _load_stats()
            guild_id = ctx.guild.id if ctx.guild else "dm"
            if str(guild_id) not in stats["guilds"]:
                stats["guilds"][str(guild_id)] = {"wins": {}, "kills": {}}
            if winner:
                _bump(stats["global"]["wins"], winner.id, 1)
                _bump(stats["guilds"][str(guild_id)]["wins"], winner.id, 1)
            for uid, k in game.kills.items():
                if k > 0:
                    _bump(stats["global"]["kills"], uid, k)
                    _bump(stats["guilds"][str(guild_id)]["kills"], uid, k)
            _save_stats(stats)

            # Tournament payout + per-tournament stats & progress
            prize_note = ""
            actual_prize_mode = None
            if game.is_tournament:
                state = _tourney_state_load()
                tid = state.get("id")
                actual_prize_mode = state.get("prize_mode", "credits")
                if actual_prize_mode == "mixed":
                    actual_prize_mode = "credits" if random.randint(1, 100) <= int(state.get("mixed_credits_pct", 70)) else "wishlist"

                if winner:
                    if actual_prize_mode == "credits":
                        payout = game.pot
                        prize_note = f"Payout: {payout} credits"
                        L = _prizes_load()
                        L["seq"] += 1
                        L["open"].append({
                            "id": L["seq"], "created_at": datetime.datetime.utcnow().isoformat(),
                            "type": "credits", "amount": payout,
                            "winner_id": winner.id, "winner_name": winner.display_name,
                            "guild_id": ctx.guild.id if ctx.guild else None,
                            "tournament_id": tid
                        })
                        _prizes_save(L)
                    else:
                        L = _prizes_load()
                        L["seq"] += 1
                        entry = {
                            "id": L["seq"],
                            "created_at": datetime.datetime.utcnow().isoformat(),
                            "type": "wishlist",
                            "count": int(state.get("wishlist_count", 2)),
                            "winner_id": winner.id,
                            "winner_name": winner.display_name,
                            "guild_id": ctx.guild.id if ctx.guild else None,
                            "tournament_id": tid
                        }
                        L["open"].append(entry)
                        _prizes_save(L)
                        prize_note = f"Wishlist x{entry['count']} (Prize ID #{entry['id']})"

                # Per-tournament stats
                all_ts = _tourney_stats_all()
                tstats = all_ts.get(tid) or {"wins": {}, "kills": {}, "credits_won": {}, "games": 0, "pots": 0}
                if winner:
                    _bump(tstats["wins"], winner.id, 1)
                    if actual_prize_mode == "credits":
                        _bump(tstats["credits_won"], winner.id, game.pot)
                for uid, k in game.kills.items():
                    if k > 0:
                        _bump(tstats["kills"], uid, k)
                tstats["games"] += 1
                tstats["pots"] += game.pot
                all_ts[tid] = tstats
                _tourney_stats_save(all_ts)

                state["games_played"] = int(state.get("games_played", 0)) + 1
                _tourney_state_save(state)

            # Build the final embed
            total_kills_this_match = sum(game.kills.values())
            wins_in_server = stats["guilds"][str(guild_id)]["wins"].get(str(winner.id), 0) if winner else 0
            wins_global = stats["global"]["wins"].get(str(winner.id), 0) if winner else 0

            title = "Bite & Fight — Winner"
            top_line = f"{winner.display_name} wins in {ctx.guild.name if ctx.guild else 'this arena'}!" if winner else "No winner. Everyone fell."
            embed = discord.Embed(title=title, description=f"🏆 {top_line}", color=discord.Color.gold(), timestamp=datetime.datetime.utcnow())
            if winner:
                embed.add_field(name="Total kills (match)", value=f"💀 {total_kills_this_match}", inline=True)
                embed.add_field(name="Time survived", value=f"⏱️ {dur_secs}s", inline=True)
                embed.add_field(name="\u200b", value="\u200b", inline=True)
                embed.add_field(name="Wins in this server", value=f"🏆 {wins_in_server}", inline=True)
                embed.add_field(name="Wins globally", value=f"🌍 {wins_global}", inline=True)

            if game.is_tournament:
                s = _tourney_state_load()
                embed.add_field(name="Pot", value=f"💰 {game.pot}", inline=True)
                embed.add_field(name="Entry per player", value=f"{game.entry_fee}", inline=True)
                if actual_prize_mode:
                    embed.add_field(name="Prize mode", value=actual_prize_mode.title(), inline=True)
                if prize_note:
                    embed.add_field(name="Prize", value=prize_note[:1024], inline=False)
                embed.add_field(name="\u200b", value=f"Games {s.get('games_played',0)}/{s.get('games_target',0)}", inline=False)

            # Always label as HP (these numbers are health)
            board = "\n".join(f"{p.display_name}: {game.hp.get(p.id, 0)} HP" for p in game.players) or "No combatants."
            embed.add_field(name="Final HP", value=board[:1024], inline=False)

            if events:
                embed.add_field(name=f"Round {game.round_num} Summary", value="\n".join(events)[:1024], inline=False)

            await game.channel.send(embed=embed)

            if game.is_tournament:
                state = _tourney_state_load()
                tid = state.get("id")
                all_ts = _tourney_stats_all()
                tstats = all_ts.get(tid, {})
                ids = set(tstats.get("wins", {}).keys()) | set(tstats.get("credits_won", {}).keys()) | set(tstats.get("kills", {}).keys())
                rows = []
                for uid in ids:
                    rows.append((int(uid),
                                 tstats.get("wins", {}).get(uid, 0),
                                 tstats.get("credits_won", {}).get(uid, 0),
                                 tstats.get("kills", {}).get(uid, 0)))
                rows.sort(key=lambda r: (-r[1], -r[2], -r[3]))
                top = rows[:5]
                if top:
                    lines = []
                    for i, (uid, w, c, k) in enumerate(top, start=1):
                        mem = ctx.guild.get_member(uid)
                        name = mem.display_name if mem else f"User {uid}"
                        lines.append(f"{i}. {name} — Wins {w}, Credits {c}, Kills {k}")
                    await game.channel.send(f"Current tourney leaderboard ({state.get('games_played',0)}/{state.get('games_target',0)}):\n" + "\n".join(lines))

                if int(state.get("games_played", 0)) >= int(state.get("games_target", 0)) > 0:
                    await bf_tourney_end(ctx)

            game.reset()
            return

        # Post one embed for the round
        if not events:
            events.append("The fighters circle, waiting for an opening.")

        # Try to pick a key play to illustrate
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
                file = None

        hp_board = ", ".join(f"{p.display_name}({game.hp[p.id]})" for p in alive_players(game))
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

        await asyncio.sleep(2.2)

# =========================
# Stats / Pot / Tourney
# =========================
@bot.command(name="bf_profile")
async def bf_profile(ctx, member: discord.Member = None):
    member = member or ctx.author
    stats = _load_stats()
    gid = str(ctx.guild.id) if ctx.guild else "dm"
    g = stats.get("guilds", {}).get(gid, {"wins": {}, "kills": {}})
    wins_server = g["wins"].get(str(member.id), 0)
    kills_server = g["kills"].get(str(member.id), 0)
    wins_global = stats["global"]["wins"].get(str(member.id), 0)
    kills_global = stats["global"]["kills"].get(str(member.id), 0)

    embed = discord.Embed(title=f"Bite & Fight — {member.display_name}", color=discord.Color.dark_gold())
    embed.add_field(name="Wins (this server)", value=f"{wins_server}", inline=True)
    embed.add_field(name="Wins (global)", value=f"{wins_global}", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="Kills (this server)", value=f"{kills_server}", inline=True)
    embed.add_field(name="Kills (global)", value=f"{kills_global}", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="bf_pot")
async def bf_pot(ctx):
    game = GAMES.get(ctx.channel.id)
    if not game or not (game.in_lobby or game.running):
        return await ctx.send("No active game here.")
    if not game.is_tournament:
        return await ctx.send("No tournament pot for casual games.")
    await ctx.send(f"Current pot: {game.pot} (Entry {game.entry_fee}). Players: {len(game.players)}.")

# =========================
# Tournament Commands
# =========================
@bot.command(name="bf_set_ante")
@commands.has_permissions(administrator=True)
async def bf_set_ante(ctx, amount: int):
    if amount < 1 or amount > 10_000_000:
        return await ctx.send("Entry must be between 1 and 10,000,000.")
    CHANNEL_ANTE[ctx.channel.id] = amount
    await ctx.send(f"Entry fee set to {amount} credits for the next tournament in this channel.")

@bot.command(name="bf_tourney_start")
@commands.has_permissions(administrator=True)
async def bf_tourney_start(ctx, games: int = 10, entry: int = None, *, name: str = None):
    """Start a tournament. Example: !bf_tourney_start 10 50 Bite & Fight Cup"""
    state = _tourney_state_load()
    if state.get("active"):
        return await ctx.send("A tournament is already active. End it with !bf_tourney_end.")
    tid = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    ante = int(entry) if entry is not None else CHANNEL_ANTE.get(ctx.channel.id, int(os.getenv("BF_ANTE", "100")))
    if entry is not None:
        CHANNEL_ANTE[ctx.channel.id] = ante  # remember host choice for this channel
    state.update({
        "active": True, "id": tid, "name": name or f"Tournament {tid}",
        "ante": ante, "channel_id": ctx.channel.id,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "games_target": int(games), "games_played": 0,
        "prize_mode": "credits", "wishlist_count": 2, "mixed_credits_pct": 70
    })
    _tourney_state_save(state)
    ts = _tourney_stats_all()
    ts[tid] = {"wins": {}, "kills": {}, "credits_won": {}, "games": 0, "pots": 0}
    _tourney_stats_save(ts)
    await ctx.send(f"Started tournament: **{state['name']}** — {games} games • Entry {state['ante']} credits. Host games with `!bf_start`.")

@bot.command(name="bf_tourney_end")
@commands.has_permissions(administrator=True)
async def bf_tourney_end(ctx):
    state = _tourney_state_load()
    if not state.get("active"):
        return await ctx.send("No active tournament.")
    tid = state["id"]
    name = state.get("name", tid)
    stats_all = _tourney_stats_all()
    stats = stats_all.get(tid, {"wins": {}, "kills": {}, "credits_won": {}, "games": 0, "pots": 0})

    ids = set(stats["wins"].keys()) | set(stats["credits_won"].keys()) | set(stats["kills"].keys())
    rows = []
    for uid in ids:
        rows.append((int(uid),
                     stats["wins"].get(uid, 0),
                     stats["credits_won"].get(uid, 0),
                     stats["kills"].get(uid, 0)))
    rows.sort(key=lambda r: (-r[1], -r[2], -r[3]))
    top = rows[:10]

    embed = discord.Embed(
        title=f"{name} — Final Leaderboard",
        description=f"Games: {stats.get('games',0)} • Total pot: {stats.get('pots',0)} • Entry {state.get('ante',0)}",
        color=discord.Color.gold()
    )
    if top:
        lines = []
        for i, (uid, w, c, k) in enumerate(top, start=1):
            mem = ctx.guild.get_member(uid)
            display = mem.display_name if mem else f"User {uid}"
            lines.append(f"{i}. {display} — Wins {w}, Credits {c}, Kills {k}")
        embed.add_field(name="Top 10", value="\n".join(lines)[:1024], inline=False)
    else:
        embed.add_field(name="Top 10", value="No results.", inline=False)
    await ctx.send(embed=embed)

    state["active"] = False
    state["id"] = None
    _tourney_state_save(state)
    await ctx.send("Tournament ended. Leaderboard archived. Use `!bf_tourney_start` for a new one.")

@bot.command(name="bf_tourney_lb")
async def bf_tourney_lb(ctx):
    state = _tourney_state_load()
    if not state.get("active"):
        return await ctx.send("No active tournament.")
    tid = state["id"]
    name = state.get("name", tid)
    stats_all = _tourney_stats_all()
    stats = stats_all.get(tid, {"wins": {}, "kills": {}, "credits_won": {}, "games": 0, "pots": 0})

    ids = set(stats["wins"].keys()) | set(stats["credits_won"].keys()) | set(stats["kills"].keys())
    rows = []
    for uid in ids:
        rows.append((int(uid),
                     stats["wins"].get(uid, 0),
                     stats["credits_won"].get(uid, 0),
                     stats["kills"].get(uid, 0)))
    rows.sort(key=lambda r: (-r[1], -r[2], -r[3]))
    top = rows[:10]

    embed = discord.Embed(
        title=f"{name} — Leaderboard",
        description=f"Games: {stats.get('games',0)} • Total pot: {stats.get('pots',0)} • Entry {state.get('ante',0)}",
        color=discord.Color.dark_gold()
    )
    if top:
        lines = []
        for i, (uid, w, c, k) in enumerate(top, start=1):
            mem = ctx.guild.get_member(uid)
            display = mem.display_name if mem else f"User {uid}"
            lines.append(f"{i}. {display} — Wins {w}, Credits {c}, Kills {k}")
        embed.add_field(name="Top 10", value="\n".join(lines)[:1024], inline=False)
    else:
        embed.add_field(name="Top 10", value="No results yet.", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="bf_tourney_info")
async def bf_tourney_info(ctx):
    s = _tourney_state_load()
    if not s.get("active"):
        return await ctx.send("No active tournament.")
    ch = ctx.guild.get_channel(s.get("channel_id")) if ctx.guild and s.get("channel_id") else None
    await ctx.send(f"Tournament: **{s.get('name')}** • Games {s.get('games_played',0)}/{s.get('games_target',0)} • Entry {s.get('ante')} • Channel: #{ch.name if ch else 'any'} • Prize mode: {s.get('prize_mode','credits')}")

@bot.command(name="bf_prize_mode")
@commands.has_permissions(administrator=True)
async def bf_prize_mode(ctx, mode: str, mixed_credits_pct: int = 70, wishlist_count: int = 2):
    mode = mode.lower()
    if mode not in ("credits", "wishlist", "mixed"):
        return await ctx.send("Mode must be: credits | wishlist | mixed")
    s = _tourney_state_load()
    if not s.get("active"):
        return await ctx.send("No active tournament.")
    s["prize_mode"] = mode
    s["wishlist_count"] = max(1, min(10, int(wishlist_count)))
    s["mixed_credits_pct"] = max(0, min(100, int(mixed_credits_pct)))
    _tourney_state_save(s)
    extra = f" • credits chance {s['mixed_credits_pct']}%" if mode == "mixed" else ""
    await ctx.send(f"Prize mode set to **{mode}**{extra}. Wishlist per win: {s['wishlist_count']}.")

@bot.command(name="bf_prizes")
@commands.has_permissions(administrator=True)
async def bf_prizes(ctx):
    L = _prizes_load()
    if not L["open"]:
        return await ctx.send("No open prizes.")
    lines = []
    for e in L["open"][:15]:
        if e["type"] == "wishlist":
            lines.append(f"#{e['id']} • {e['winner_name']} • Wishlist x{e.get('count', 2)}")
        elif e["type"] == "credits":
            lines.append(f"#{e['id']} • {e['winner_name']} • {e.get('amount',0)} credits")
        else:
            lines.append(f"#{e['id']} • {e['winner_name']} • {e.get('type')}")
    await ctx.send("Open prizes:\n" + "\n".join(lines))

@bot.command(name="bf_prize_done")
@commands.has_permissions(administrator=True)
async def bf_prize_done(ctx, prize_id: int):
    L = _prizes_load()
    for i, e in enumerate(L["open"]):
        if int(e["id"]) == int(prize_id):
            L["closed"].append(e)
            del L["open"][i]
            _prizes_save(L)
            return await ctx.send(f"Marked prize #{prize_id} as delivered.")
    await ctx.send("Prize ID not found.")

# =========================
# Lifecycle
# =========================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} with prefix {PREFIX}")

# =========================
# Entry
# =========================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN env var.")
    bot.run(TOKEN)
