# bot.py
import os
import json
import random
import asyncio
import datetime
from collections import defaultdict
from io import BytesIO
from discord import File

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance

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
# (removed confusing global `files` variable)

# Per-channel default ante (used when starting a new tournament)
CHANNEL_ANTE = defaultdict(lambda: int(os.getenv("BF_ANTE", "100")))

# ----- asset lookup (root + assets/ + assets)) -----
ROOT_DIR = os.path.dirname(__file__)
ASSET_DIRS = [
    os.path.join(ROOT_DIR, "assets"),
    os.path.join(ROOT_DIR, "assets)"),
    ROOT_DIR,  # repo root (where your logo.png/swords.png currently live)
]

def find_asset(candidates: list[str]) -> str | None:
    """Return the first existing file path from our known asset dirs."""
    for d in ASSET_DIRS:
        for name in candidates:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    return None

# ==========================
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

def _pick_key_play(events, game):
    """
    Return (text, attacker_member, target_member) for an event that names both.
    Falls back to (None, None, None) if nothing matches.
    """
    alive = alive_players(game)
    for e in reversed(events):
        for a in alive:
            for t in game.players:
                if a.id == t.id:
                    continue
                if a.display_name in e and t.display_name in e:
                    return e, a, t
    return None, None, None

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
    """Only a Join button; no Tributes / Start buttons."""
    def __init__(self, game: "BiteFightGame", host: discord.Member, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.game = game
        self.host = host
        self.message: discord.Message | None = None

    async def on_timeout(self):
        # when timer ends the game auto-begins
        if self.game.in_lobby:
            try:
                for c in self.children:
                    if isinstance(c, discord.ui.Button):
                        c.disabled = True
                if self.message:
                    await self.message.edit(view=self)
            except Exception:
                pass
            await bf_begin(self.game._ctx)

    async def _set_footer(self):
        """Show host + countdown + joined count in the footer (no Status field)."""
        if not self.message: 
            return
        embed = self.message.embeds[0]
        joined = len(self.game.players)
        embed.set_footer(text=f"Host: {self.game._ctx.author.display_name} • Lobby closes in 30s • {joined} joined")
        try:
            await self.message.edit(embed=embed, view=self)
        except Exception:
            pass

    async def update_counter(self):
        await self._set_footer()

    @discord.ui.button(label="Join", emoji="🍔", style=discord.ButtonStyle.success)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        if not self.game.in_lobby:
            return await interaction.response.send_message("Lobby closed.", ephemeral=True)
        if user.bot:
            return await interaction.response.send_message("Bots cannot join.", ephemeral=True)
        if user in self.game.players:
            return await interaction.response.send_message("You are already in.", ephemeral=True)

        # tournament: auto add to pot
        if self.game.is_tournament:
            self.game.pot += self.game.entry_fee
            self.game.buyins[user.id] = self.game.buyins.get(user.id, 0) + self.game.entry_fee

        self.game.players.append(user)
        self.game.hp[user.id] = self.game.max_hp

        await interaction.response.send_message(f"{user.display_name} joined the arena.", ephemeral=True)
        await self.update_counter()


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

# ---- Versus Card (swords overlay + greying loser) ----
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

def grey_out(im: Image.Image, dim: float = 0.55) -> Image.Image:
    """Desaturate and darken an avatar to indicate the loser."""
    g = ImageOps.grayscale(im).convert("RGBA")
    g = ImageEnhance.Brightness(g).enhance(dim)
    return g


async def build_versus_card(
    attacker: discord.Member,
    target: discord.Member,
    action_text: str,
    grey_left: bool = False,
    grey_right: bool = False,
    left_hp: int | None = None,     # optional: for Catfight sliders
    right_hp: int | None = None,    # optional: for Catfight sliders
    max_hp: int = 100,              # optional: for Catfight sliders
) -> BytesIO:
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance
    from io import BytesIO

    W, H = 900, 500
    pad = 32
    face = 360

    # --- background ---
    from pathlib import Path
    try:
        bg_path = Path(__file__).with_name("versus_bg.png")
        background = Image.open(bg_path).convert("RGBA").resize((W, H), Image.LANCZOS)
    except Exception:
        background = Image.new("RGBA", (W, H), (24, 24, 24, 255))
    card = background.copy()

    # --- avatars ---
    async def _fetch(member, size=512):
        try:
            b = await member.display_avatar.replace(size=size, format="png").read()
            im = Image.open(BytesIO(b)).convert("RGBA")
        except Exception:
            im = Image.new("RGBA", (size, size), (40, 40, 40, 255))
        return im.resize((size, size), Image.LANCZOS)

    la = await _fetch(attacker)
    ra = await _fetch(target)

    def _rounded(im, radius=40):
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, *im.size), radius=radius, fill=255)
        im.putalpha(mask)
        return im

    la = _rounded(la.resize((face, face), Image.LANCZOS))
    ra = _rounded(ra.resize((face, face), Image.LANCZOS))

    if grey_left:
        la = ImageEnhance.Brightness(ImageOps.grayscale(la).convert("RGBA")).enhance(0.55)
    if grey_right:
        ra = ImageEnhance.Brightness(ImageOps.grayscale(ra).convert("RGBA")).enhance(0.55)

    card.alpha_composite(la, dest=(pad, pad))
    card.alpha_composite(ra, dest=(W - pad - face, pad))

    # --- winner/loser ribbons + badges ---
    rb_h = 120  # big badge band
    left_rect  = (pad, pad + face - rb_h, pad + face, pad + face)
    right_rect = (W - pad - face, pad + face - rb_h, W - pad, pad + face)

    WIN  = (70, 130, 180, 255)
    LOSE = (200, 60, 60, 255)

    if grey_left != grey_right:
        d2 = ImageDraw.Draw(card)
        if grey_left:
            d2.rectangle(left_rect,  fill=LOSE)
            d2.rectangle(right_rect, fill=WIN)
            left_badge, right_badge = "rip", "trophy"
        else:
            d2.rectangle(left_rect,  fill=WIN)
            d2.rectangle(right_rect, fill=LOSE)
            left_badge, right_badge = "trophy", "rip"

        def _paste_badge(kind: str, rect: tuple[int,int,int,int]):
            name_map = {
                "trophy": ["trophy.png", "cup.png", "trophy_emoji.png"],
                "rip":    ["rip.png", "tombstone.png", "grave.png", "rip_emoji.png"],
            }
            path = find_asset(name_map[kind]) if 'find_asset' in globals() else None
            x0, y0, x1, y1 = rect
            rw, rh = (x1 - x0), (y1 - y0)
        
            if path:
                try:
                    ic = Image.open(path).convert("RGBA")
                    max_w_frac = 0.70
                    overshoot   = 1.60
                    scale = min((rw * max_w_frac) / ic.width, (rh * overshoot) / ic.height)
                    ic = ic.resize((max(1, int(ic.width * scale)), max(1, int(ic.height * scale))), Image.LANCZOS)
                    px = x0 + (rw - ic.width) // 2
                    py = y0 + (rh - ic.height) // 2 - int(rh * 0.40)
                    card.alpha_composite(ic, dest=(px, py))
                    return
                except Exception:
                    pass
        
            try:
                f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 64)
            except Exception:
                f = ImageFont.load_default()
            label = "RIP" if kind == "rip" else "WIN"
            tw = int(f.getlength(label)); th = f.getbbox(label)[3]
            px = x0 + (rw - tw) // 2
            py = y0 + (rh - th) // 2 - int(rh * 0.20)
            ImageDraw.Draw(card).text((px, py), label, font=f, fill=(255,255,255,255))

        _paste_badge(left_badge,  left_rect)
        _paste_badge(right_badge, right_rect)

    # --- swords overlay (center) ---
    swords_path = find_asset(["swords.png", "sword.png", "crossed_swords.png"])
    if swords_path:
        try:
            swords = Image.open(swords_path).convert("RGBA")
            sw = int(W * 0.35)
            sh = int(swords.height * (sw / swords.width))
            swords = swords.resize((sw, sh), Image.LANCZOS)
            card.alpha_composite(swords, dest=((W - sw) // 2, int(H * 0.18)))
        except Exception:
            pass

    # --- action text strip (bigger, auto-fit, outlined) ---
    strip_h = 100
    strip = Image.new("RGBA", (W - pad * 2, strip_h), (0, 0, 0, 170))
    card.alpha_composite(strip, dest=(pad, H - pad - strip_h))
    draw = ImageDraw.Draw(card)

    text = action_text.strip()
    max_px = W - pad * 2 - 30
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    size = 48
    while size >= 26:
        try:
            fnt = ImageFont.truetype(font_path, size)
        except Exception:
            fnt = ImageFont.load_default()
            break
        if fnt.getlength(text) <= max_px:
            break
        size -= 2

    tx = pad + 16
    ty = H - pad - strip_h + (strip_h - fnt.getbbox(text)[3]) // 2 - 6
    draw.text(
        (tx, ty),
        text,
        font=fnt,
        fill=(255, 255, 255, 255),
        stroke_width=3,
        stroke_fill=(0, 0, 0, 180),
    )

    # --- Catfight-style HP sliders (bottom footer) ---
    if left_hp is not None and right_hp is not None and max_hp:
        footer_h = 64
        new_card = Image.new("RGBA", (W, H + footer_h), (24, 24, 24, 255))  # dark footer
        new_card.alpha_composite(card, dest=(0, 0))
        card = new_card
        H = H + footer_h
    
        def _draw_slider(x, y, w, h, pct, fill_rgb):
            pct = max(0.0, min(1.0, float(pct)))
            r = h // 2
    
            # track (soft grey)
            track = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(track)
            d.rounded_rectangle((0, 0, w, h), radius=r, fill=(180, 180, 180, 160))
            card.alpha_composite(track, dest=(x, y))
    
            # fill (keep rounded ends visible for tiny values)
            fw = int(w * pct)
            if 0 < fw < r * 2:
                fw = r * 2
            if fw > 0:
                fill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                df = ImageDraw.Draw(fill)
                df.rounded_rectangle((0, 0, fw, h), radius=r, fill=(*fill_rgb, 230))
                card.alpha_composite(fill, dest=(x, y))
    
            # subtle highlight
            hi = Image.new("RGBA", (w, h // 2), (255, 255, 255, 30))
            card.alpha_composite(hi, dest=(x, y))
    
        # bar geometry (in the footer, vertically centered)
        bar_h = 22
        left_x  = pad
        right_x = W - pad - face
        bar_w   = face
        y0 = H - footer_h + (footer_h - bar_h) // 2  # centered in footer
    
        green = (46, 204, 113)   # left
        pink  = (236, 64, 122)   # right
    
        lp = (left_hp / max_hp) if max_hp else 0.0
        rp = (right_hp / max_hp) if max_hp else 0.0
    
        _draw_slider(left_x,  y0, bar_w, bar_h, lp, green)
        _draw_slider(right_x, y0, bar_w, bar_h, rp, pink)
    
        # percent labels (white, to the right of each bar)
        try:
            pf = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        except Exception:
            pf = ImageFont.load_default()
        draw = ImageDraw.Draw(card)
        lw = f"{int(round(lp*100))}%"
        rw = f"{int(round(rp*100))}%"
        th = pf.getbbox(lw)[3]
        draw.text((left_x  + bar_w + 10, y0 + (bar_h - th)//2 - 2), lw, font=pf, fill=(255,255,255,255))
        th = pf.getbbox(rw)[3]
        draw.text((right_x + bar_w + 10, y0 + (bar_h - th)//2 - 2), rw, font=pf, fill=(255,255,255,255))
    # --- end sliders ---

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
    desc = "🍔 to join the fight!"  # keep this, remove the 'let the battle begin' line
    
    embed = discord.Embed(
        title=title,
        description=f"**{subtitle}**\n\n{desc}",
        color=discord.Color.dark_gold()
    )
          
    # NO Status field anymore
    if game.is_tournament:
        embed.add_field(name="Pot", value=f"💰 {game.pot} • Entry {game.entry_fee}", inline=False)
        # Footer shows host + timer (player count will be appended by the view)
    embed.set_footer(text=f"Host: {ctx.author.display_name} • Lobby closes in 30s")
    
    view = LobbyView(game, host=ctx.author, timeout=30.0)
    game.lobby_view = view
    
    embed, brand_files = brand_embed(embed)
    msg = await ctx.send(embed=embed, view=view, files=brand_files)
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

# NOTE: no !bf_join – buttons handle join; bf_begin is internal
async def bf_begin(ctx):
    """Begin the match (called by the button/timeout)."""
    chan_id = ctx.channel.id
    game = GAMES.get(chan_id)

    # guards
    if not game or not game.in_lobby:
        return
    if len(game.players) < 2:
        await ctx.send("Not enough players joined. Cancelling.")
        game.reset()
        return

    # flip state
    game.in_lobby = False
    game.running = True
    game.round_num = 0
    game.start_time = datetime.datetime.utcnow()

    # disable lobby buttons if that message still exists
    if game.lobby_view and game.lobby_view.message:
        try:
            for c in game.lobby_view.children:
                if isinstance(c, discord.ui.Button):
                    c.disabled = True
            await game.lobby_view.message.edit(view=game.lobby_view)
        except Exception:
            pass

    # Pixxie-style intro card (names only; no HP)
    intro = line("intro", game.banter) or "A hush falls. Then the roar. Time to settle scores."
    names_only = "\n".join(p.display_name for p in game.players) or "No challengers"

    embed = discord.Embed(
        title="May the odds be ever in your flavor!",
        description=f"**Part 2 - The Battle Begins...**\n{intro}",
        color=discord.Color.dark_gold(),
        timestamp=datetime.datetime.utcnow()
    )
    embed.add_field(name=f"🍽️ {len(game.players)} challengers on the menu",
                    value=f"```{names_only}```",
                    inline=False)

    if game.is_tournament:
        s = _tourney_state_load()
        embed.add_field(name="Pot", value=f"💰 {game.pot} (Entry {game.entry_fee})", inline=True)
        embed.add_field(name="Prize mode", value=s.get("prize_mode", "credits").title(), inline=True)

    embed.set_footer(text=f"Host: {ctx.author.display_name}")
    embed, brand_files = brand_embed(embed)
    await ctx.send(embed=embed, files=brand_files)

    # run rounds; if anything blows up, show it so it doesn't look "stuck"
    try:
        await run_game(ctx, game)
    except Exception as e:
        game.reset()
        await ctx.send(f"⚠️ Game crashed: `{e}`")
        
async def build_profile_card(member: discord.Member) -> BytesIO:
    # avatar
    try:
        b = await member.display_avatar.replace(size=512, format="png").read()
        av = Image.open(BytesIO(b)).convert("RGBA")
    except Exception:
        av = Image.new("RGBA", (512, 512), (40, 40, 40, 255))
    av = av.resize((512, 512), Image.LANCZOS)

    # round it
    m = Image.new("L", (512, 512), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, 512, 512), radius=40, fill=255)
    av.putalpha(m)

    # canvas = your versus background (fallback to dark if missing)
    W, H = 900, 500
    try:
        bg_path = find_asset(["versus_bg.png", "versus_bg.jpg", "bf bg.png"])
        background = Image.open(bg_path).convert("RGBA").resize((W, H), Image.LANCZOS)
    except Exception:
        background = Image.new("RGBA", (W, H), (20, 20, 24, 255))
    canvas = background.copy()

    # place avatar
    canvas.paste(av, (32, 32), av)

    # name text
    draw = ImageDraw.Draw(canvas)
    try:
        fnt = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)
    except Exception:
        fnt = ImageFont.load_default()
    draw.text((560, 60), member.display_name, fill=(255, 255, 255, 255), font=fnt)

    out = BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    out.seek(0)
    return out

def brand_embed(embed: discord.Embed, files_list=None):
    """Attach the Bite & Fight logo as an embed thumbnail.
    Returns (embed, files) so the caller can pass files=... when sending.
    """
    path = find_asset(["logo.png", "logo.jpg", "logo.jpeg"])
    files = list(files_list or [])
    if path:
        files.append(discord.File(path, filename="bf_logo.png"))
        embed.set_thumbnail(url="attachment://bf_logo.png")
    return embed, files

def build_hp_panel_image(game) -> BytesIO:
    """Small image that shows a Catfight-style HP bar for each player."""
    from PIL import Image, ImageDraw, ImageFont
    from io import BytesIO

    players = list(game.players)
    n = max(1, len(players))
    W = 900
    row_h = 40
    pad = 12
    name_w = 280
    bar_h = 12
    H = pad * 2 + n * row_h

    im = Image.new("RGBA", (W, H), (24, 24, 24, 255))
    d = ImageDraw.Draw(im)

    #microscopic fonts
    #try: 
        #f_name = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)  # CHANGED
        #f_pct  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)  # CHANGED
    #except Exception:
        #f_name = f_pct = ImageFont.load_default()

    # try to load a real TTF from your repo first
    font_path = find_asset([
        "DejaVuSans-Bold.ttf",     # put this file in /assets
        "arialbd.ttf",
        "Arial Bold.ttf"
    ]) or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    
    try:
        f_name = ImageFont.truetype(font_path, 30)  # bigger, bold
        f_pct  = ImageFont.truetype(font_path, 26)
    except Exception:
        # last resort — tiny bitmap font (avoid if possible)
        f_name = f_pct = ImageFont.load_default()



    def draw_slider(x, y, w, h, pct, fill_rgb):
        pct = max(0.0, min(1.0, float(pct)))
        r = h // 2
        # track
        d.rounded_rectangle((x, y, x + w, y + h), radius=r, fill=(180, 180, 180, 130)) #old 160
        # fill (keep rounded ends for tiny values)
        fw = int(w * pct)
        if 0 < fw < r * 2:
            fw = r * 2
        if fw > 0:
            d.rounded_rectangle((x, y, x + fw, y + h), radius=r, fill=(*fill_rgb, 230))
        # highlight
        #d.rectangle((x, y, x + w, y + h//2), fill=(255, 255, 255, 25))

    for i, p in enumerate(players):
        y = pad + i * row_h
        hp = game.hp.get(p.id, 0)
        pct = hp / game.max_hp if game.max_hp else 0.0

        # name
        #d.text((pad, y + (row_h - 22)//2), p.display_name, font=f_name, fill=(255, 255, 255, 255))
        # new
        name_h = f_name.getbbox(p.display_name)[3]
        name_y = y + (row_h - name_h) // 2
        d.text(
            (pad, name_y),
            p.display_name,
            font=f_name,
            fill=(255, 255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 180),
        )


        # colour by HP (green / yellow / red)
        if pct >= 2/3:
            col = (46, 204, 113)
        elif pct >= 1/3:
            col = (241, 196, 15)
        else:
            col = (231, 76, 60)

        # slider
        bar_x = pad + name_w
        bar_w = W - pad - bar_x - 60
        bar_y = y + (row_h - bar_h)//2
        draw_slider(bar_x, bar_y, bar_w, bar_h, pct, col)

        # % label
        label = f"{int(round(pct*100))}%"
        #d.text((bar_x + bar_w + 12, bar_y - 2), label, font=f_pct, fill=(255, 255, 255, 255))
        # new
        d.text(
            (bar_x + bar_w + 12, bar_y - 2),
            label,
            font=f_pct,
            fill=(255, 255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 160),
        )


    buf = BytesIO()
    im.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


#--commands--#
@bot.command(name="bf_stop")
async def bf_stop(ctx):
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
        file = None  # always defined for this round

        # -------- bleed ticks first --------
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

        # -------- attacks (random order) --------
        attackers = list(alive_players(game))
        random.shuffle(attackers)

        for attacker in attackers:
            if game.hp.get(attacker.id, 0) <= 0:
                continue

            target = pick_target(game, attacker)
            if not target:
                break

            do_bite = random.random() < 0.55

            if do_bite:
                if random.random() < 0.15:
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
                        attacker=attacker.display_name, target=target.display_name, dmg=dmg,
                        hp=game.hp[target.id], tag=tag
                    ))
                    if game.hp[target.id] <= 0:
                        game.kills[attacker.id] += 1
                        events.append(format_line(
                            line("death_bite", game.banter) or "[target] falls to the fangs.",  # <-- FIXED HERE
                            attacker=attacker.display_name, target=target.display_name
                        ))
            else:
                if random.random() < 0.12:
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

        # -------- choose a key play & build the image (avatars + swords) --------
        if not events:
            events.append("The fighters circle, waiting for an opening.")

        key_play, key_attacker, key_target = _pick_key_play(events, game)

        if key_play and key_attacker and key_target:
            try:
                lower = key_play.lower()
                attacker_missed = ("miss" in lower and key_attacker.display_name in lower)

                # Randomize sides: ~50% we swap A/T on the card.
                swap = bool(random.getrandbits(1))
                left = key_attacker
                right = key_target
                fade_left = attacker_missed
                fade_right = not attacker_missed

                if swap:
                    left, right = right, left
                    fade_left, fade_right = fade_right, fade_left

                img_bytes = await build_versus_card(
                    left, right, key_play,
                    grey_left=fade_left, grey_right=fade_right,
                    left_hp=game.hp.get(left.id, 0),
                    right_hp=game.hp.get(right.id, 0),
                    max_hp=game.max_hp,
                )

                file = discord.File(img_bytes, filename=f"round_{game.round_num}.png")
            except Exception:
                file = None  # never crash a round just for the art

        # -------- build the round embed (always) --------
        embed = discord.Embed(
            title=f"Bite & Fight — Round {game.round_num}",
            description=line("round_intro", game.banter) or "",
            color=discord.Color.dark_red(),
            timestamp=datetime.datetime.utcnow()
        )
        embed.add_field(name="Events", value="\n".join(events)[:1024], inline=False)

        # (ugly inline HP field removed)

        files = []
        if file is not None:
            embed.set_image(url=f"attachment://round_{game.round_num}.png")
            files.append(file)
        
        # MUST unpack and pass files
        embed, files = brand_embed(embed, files_list=files)
        await game.channel.send(embed=embed, files=files)

        hp_panel = build_hp_panel_image(game)
        await game.channel.send(file=discord.File(hp_panel, filename=f"hp_{game.round_num}.png"))


        # -------- end condition & winner embed --------
        alive_now = alive_players(game)
        if len(alive_now) <= 1:
            winner = alive_now[0] if alive_now else None

            # Compute time survived
            ended_at = datetime.datetime.utcnow()
            dur_secs = int((ended_at - game.start_time).total_seconds()) if game.start_time else 0

            # Persist lifetime stats
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

            # ---- TOURNAMENT STATS (wins, kills, credits, totals) ----
            if game.is_tournament:
                state = _tourney_state_load()
                tid = state.get("id")
            
                all_ts = _tourney_stats_all()
                ts = all_ts.get(tid, {"wins": {}, "kills": {}, "credits_won": {}, "games": 0, "pots": 0})
            
                if winner:
                    _bump(ts["wins"], winner.id, 1)
                    # credit the winner with this game's pot; change to a fixed value if you prefer
                    _bump(ts["credits_won"], winner.id, game.pot)
            
                for uid, k in game.kills.items():
                    if k > 0:
                        _bump(ts["kills"], uid, k)
            
                ts["games"] = ts.get("games", 0) + 1
                ts["pots"]  = ts.get("pots", 0) + game.pot
            
                all_ts[tid] = ts
                _tourney_stats_save(all_ts)
            
                # reflect progress on the active tournament
                state["games_played"] = int(state.get("games_played", 0)) + 1
                _tourney_state_save(state)

            # ---------- Winner card  ----------
            total_kills_this_match = sum(game.kills.values())
            wins_in_server = stats["guilds"][str(guild_id)]["wins"].get(str(winner.id), 0) if winner else 0
            wins_global = stats["global"]["wins"].get(str(winner.id), 0) if winner else 0
            
            top_line = f"**{winner.display_name}** wins in **{ctx.guild.name if ctx.guild else 'this arena'}**!" if winner else "No winner. Everyone fell."
            lines = [top_line]
            lines.append(f"💀 **Total kills:** {total_kills_this_match}")
            lines.append(f"⏱️ **Time survived:** {dur_secs}s")
            if winner:
                lines.append(f"🏆 **Total wins in server:** {wins_in_server}")
                lines.append(f"🌍 **Total wins globally:** {wins_global}")
            if getattr(game, "is_tournament", False):
                lines.append(f"💰 **Pot:** {game.pot}")
            lines.append("🔎 Check your stats with `!bf_profile`")
            
            w_embed = discord.Embed(
                title="🏆 Winner!",
                description="\n".join(lines),
                color=discord.Color.gold(),
                timestamp=datetime.datetime.utcnow()
            )
            
            # winner avatar as AUTHOR ICON (leave thumbnail for logo)
            if winner:
                av = winner.display_avatar.replace(size=256, static_format="png").url
                w_embed.set_author(name=winner.display_name, icon_url=av)
            
            # small Bite & Fight logo INSIDE the embed (via brand_embed)
            files_to_send = []
            w_embed, files_to_send = brand_embed(w_embed)   # returns (embed, files)
            
            # --- POST PROFILE IMAGE ABOVE THE WINNER EMBED
            if winner:
                try:
                    buf = await build_profile_card(winner)
                except Exception:
                    av_bytes = await winner.display_avatar.replace(size=512, format="png").read()
                    buf = BytesIO(av_bytes)
                await game.channel.send(file=discord.File(buf, filename="profile.png"))
            
            # --- "My Stats" button
            view = discord.ui.View(timeout=None)
            if winner:
                view.add_item(discord.ui.Button(
                    label="My Stats",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"bf_stats:{winner.id}",
                ))
            
            # --- SEND THE WINNER EMBED
            if files_to_send:
                await game.channel.send(embed=w_embed, files=files_to_send, view=view)
            else:
                await game.channel.send(embed=w_embed, view=view)
            
            game.reset()
            return

        # small pause between rounds
        await asyncio.sleep(2.0)


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

# ========= Debug helpers (to verify assets & card) =========
@bot.command(name="bf_dbg_assets")
@commands.has_permissions(administrator=True)
async def bf_dbg_assets(ctx):
    lines = []
    for d in ASSET_DIRS:
        try:
            if os.path.isdir(d):
                entries = ", ".join(sorted(os.listdir(d))) or "(empty)"
                lines.append(f"{d} -> {entries}")
            else:
                lines.append(f"{d} -> (not a directory)")
        except Exception as e:
            lines.append(f"{d} -> ERROR: {e}")
    await ctx.send("Asset scan:\n" + "\n".join(lines))

@bot.command(name="bf_cardtest")
@commands.has_permissions(administrator=True)
async def bf_cardtest(ctx, left: discord.Member=None, right: discord.Member=None, *, text: str="Swords check"):
    left = left or ctx.author
    right = right or ctx.author
    img = await build_versus_card(left, right, text, grey_right=True)
    await ctx.send(file=discord.File(img, filename="test_card.png"))
    
@bot.event
async def on_interaction(interaction: discord.Interaction):
    data = interaction.data or {}
    cid = data.get("custom_id", "")

    # --- profile image (from the winner button above the embed)
    if interaction.type.name == "component" and cid.startswith("bf_profile:"):
        uid = int(cid.split(":")[1])
        member = interaction.guild.get_member(uid) or await interaction.client.fetch_user(uid)

        buf = await build_profile_card(member)
        await interaction.response.send_message(  # <-- SEND THE FILE HERE
            file=discord.File(buf, filename="profile.png"),
            ephemeral=True
        )
        return  # ensure we don't try to respond again

    # --- stats button (acts like !bf_profile)
    elif interaction.type.name == "component" and cid.startswith("bf_stats:"):
        uid = int(cid.split(":")[1])
        member = interaction.guild.get_member(uid) or await interaction.client.fetch_user(uid)

        stats = _load_stats()
        gid = str(interaction.guild.id) if interaction.guild else "dm"
        g = stats.get("guilds", {}).get(gid, {"wins": {}, "kills": {}})
        wins_server = g["wins"].get(str(uid), 0)
        kills_server = g["kills"].get(str(uid), 0)
        wins_global = stats["global"]["wins"].get(str(uid), 0)
        kills_global = stats["global"]["kills"].get(str(uid), 0)

        e = discord.Embed(
            title=f"Bite & Fight — {member.display_name}",   # <-- use member.display_name
            color=discord.Color.dark_gold()
        )
        e.add_field(name="Wins (this server)", value=f"{wins_server}", inline=True)
        e.add_field(name="Wins (global)", value=f"{wins_global}", inline=True)
        e.add_field(name="\u200b", value="\u200b", inline=True)
        e.add_field(name="Kills (this server)", value=f"{kills_server}", inline=True)
        e.add_field(name="Kills (global)", value=f"{kills_global}", inline=True)

        await interaction.response.send_message(embed=e, ephemeral=True)
        return


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
