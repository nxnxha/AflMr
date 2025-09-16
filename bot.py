# bot.py — Mariage + Famille + Coins (FR, simple, UHQ)
# discord.py 2.x / aiosqlite / aiohttp / Pillow
#
# ENV obligatoires:
#   DISCORD_TOKEN=xxx
#   GUILD_IDS=1382730341944397967   (ou GUILD_ID=…)
#
# ENV coins (EPIC) facultatives (si vides, les actions perso échouent proprement):
#   EPIC_BASE_URL=https://ton-api-epic.tld
#   EPIC_API_KEY=xxxxxxxxxxxx
#   EPIC_AUTH_SCHEME=bearer         (ou "raw" pour X-API-Key)
#   EPIC_GET_BALANCE_PATH=/users/{user_id}/coins
#   EPIC_ADD_COINS_PATH=/users/{user_id}/coins/add
#   EPIC_SET_COINS_PATH=/users/{user_id}/coins/set
#   EPIC_SPEND_MODE=add_negative    (par défaut)

import os, io, time, asyncio, traceback
from typing import Optional, List, Dict, Tuple
from datetime import datetime

import discord
from discord import app_commands
import aiohttp
import aiosqlite
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ========= CONFIG =========
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_IDS = [int(x) for x in (os.getenv("GUILD_IDS") or os.getenv("GUILD_ID","")).replace(" ","").split(",") if x.strip().isdigit()]
if not GUILD_IDS:
    raise RuntimeError("GUILD_IDS (ou GUILD_ID) manquant.")
TARGET_GUILDS = [discord.Object(id=g) for g in GUILD_IDS]

LOGS_DEFAULT_CHAN_ID = int(os.getenv("LOGS_DEFAULT_CHAN_ID", "1417304969333440553"))
DB_PATH = os.getenv("DB_PATH", "./affiliations.db")
BRAND_COLOR = 0xFF69B4

# EPIC (coins)
EPIC_BASE_URL = os.getenv("EPIC_BASE_URL", "")
EPIC_API_KEY  = os.getenv("EPIC_API_KEY", "")
EPIC_AUTH_SCHEME = os.getenv("EPIC_AUTH_SCHEME", "raw")
EPIC_GET_BALANCE_PATH = os.getenv("EPIC_GET_BALANCE_PATH", "/users/{user_id}/coins")
EPIC_ADD_COINS_PATH   = os.getenv("EPIC_ADD_COINS_PATH", "/users/{user_id}/coins/add")
EPIC_SET_COINS_PATH   = os.getenv("EPIC_SET_COINS_PATH", "/users/{user_id}/coins/set")
EPIC_SPEND_MODE       = os.getenv("EPIC_SPEND_MODE", "add_negative")

# Thèmes arbre
THEMES = {
    "anime":     {"bg":(255,249,254), "primary":(255,105,180), "line":(255,182,193), "card":(255,255,255)},
    "kawaii":    {"bg":(250,247,255), "primary":(124,58,237),  "line":(160,140,210), "card":(255,255,255)},
    "sakura":    {"bg":(255,247,251), "primary":(221,73,104),  "line":(239,170,184), "card":(255,255,255)},
    "royal":     {"bg":(245,246,252), "primary":(66,90,188),   "line":(120,140,210), "card":(255,255,255)},
    "neon":      {"bg":(18,18,22),    "primary":(0,245,212),   "line":(80,255,200),  "card":(36,36,44)},
    "arabesque": {"bg":(248,246,240), "primary":(189,119,26),  "line":(169,139,99),  "card":(255,255,252)},
}

# ========= DB =========
CREATE_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS relations (
  rel_id    TEXT PRIMARY KEY,
  guild_id  INTEGER,
  rtype     TEXT,      -- marriage | family
  name      TEXT,      -- pour family
  since     INTEGER,
  wallet_id TEXT,
  theme     TEXT       -- pour family
);

CREATE TABLE IF NOT EXISTS relation_members (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  rel_id   TEXT,
  user_id  INTEGER,
  UNIQUE(rel_id, user_id)
);

CREATE TABLE IF NOT EXISTS wallets (
  wallet_id TEXT PRIMARY KEY,
  balance   INTEGER
);

CREATE TABLE IF NOT EXISTS wallet_members (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  wallet_id TEXT,
  user_id   INTEGER,
  UNIQUE(wallet_id, user_id)
);

CREATE TABLE IF NOT EXISTS kin_edges (
  parent_id INTEGER,
  child_id  INTEGER,
  UNIQUE(parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS guild_settings (
  guild_id   INTEGER PRIMARY KEY,
  theme      TEXT,
  rtl        INTEGER,
  avatars    INTEGER,
  log_chan   INTEGER
);

-- Historique mariage
CREATE TABLE IF NOT EXISTS marriage_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  rel_id    TEXT,
  ts        INTEGER,
  actor_id  INTEGER,
  kind      TEXT,      -- propose|accept|deposit|withdraw_request|withdraw_ok|divorce_request|divorce_ok|gift|nick_set|nick_reset|anniv_set
  amount    INTEGER    -- optionnel (coins)
);

-- Meta mariage (surnoms + anniversaire)
CREATE TABLE IF NOT EXISTS marriage_meta (
  rel_id      TEXT PRIMARY KEY,
  a_id        INTEGER,
  b_id        INTEGER,
  nickname_a  TEXT,
  nickname_b  TEXT,
  anniversary TEXT    -- 'YYYY-MM-DD'
);
"""

async def db():
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    return conn

async def init_db():
    async with await db() as conn:
        await conn.executescript(CREATE_SQL)
        for gid in GUILD_IDS:
            row = await (await conn.execute("SELECT 1 FROM guild_settings WHERE guild_id=?", (gid,))).fetchone()
            if not row:
                await conn.execute(
                    "INSERT INTO guild_settings(guild_id,theme,rtl,avatars,log_chan) VALUES (?,?,?,?,?)",
                    (gid, "anime", 0, 1, LOGS_DEFAULT_CHAN_ID)
                )
        await conn.commit()

# ========= Utils =========
def E(title: str, desc: str) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=BRAND_COLOR)

async def get_settings(guild_id:int) -> Dict[str,int|str|None]:
    async with await db() as conn:
        row = await (await conn.execute("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,))).fetchone()
        if not row:
            return {"guild_id": guild_id, "theme": "anime", "rtl": 0, "avatars": 1, "log_chan": LOGS_DEFAULT_CHAN_ID}
        return dict(row)

async def log_line(guild: discord.Guild, text: str):
    try:
        sett = await get_settings(guild.id)
        ch_id = int(sett.get("log_chan") or LOGS_DEFAULT_CHAN_ID)
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel):
            await ch.send(text)
    except Exception:
        print("[LOG]", text)

async def reply(inter: discord.Interaction, *, content=None, embed=None, view=None, file=None, ephemeral=True):
    if inter.response.is_done():
        await inter.followup.send(content=content, embed=embed, view=view, file=file, ephemeral=ephemeral)
    else:
        await inter.response.send_message(content=content, embed=embed, view=view, file=file, ephemeral=ephemeral)

def deterministic_id(prefix:str, members: List[int]) -> str:
    base = ":".join(map(str, sorted(set(members))))
    return f"{prefix}:{int(time.time())}:{base[:6]}"

def valid_date_yyyy_mm_dd(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d"); return True
    except Exception: return False

# ========= EPIC coins =========
def _epic_headers():
    if not EPIC_API_KEY: return {}
    return {"Authorization": f"Bearer {EPIC_API_KEY}"} if EPIC_AUTH_SCHEME.lower()=="bearer" else {"X-API-Key": EPIC_API_KEY}

async def epic_get_balance(user_id: int) -> int:
    if not EPIC_BASE_URL: return 0
    url = EPIC_BASE_URL.rstrip("/") + EPIC_GET_BALANCE_PATH.format(user_id=user_id)
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=_epic_headers()) as r:
            if r.status != 200: return 0
            data = await r.json()
            return int(data.get("balance", 0))

async def epic_add_coins(user_id: int, amount: int) -> bool:
    if not EPIC_BASE_URL: return False
    url = EPIC_BASE_URL.rstrip("/") + EPIC_ADD_COINS_PATH.format(user_id=user_id)
    payload = {"amount": int(amount)}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, headers=_epic_headers()) as r:
            return r.status == 200

async def epic_set_coins(user_id: int, new_balance: int) -> bool:
    if not EPIC_BASE_URL: return False
    url = EPIC_BASE_URL.rstrip("/") + EPIC_SET_COINS_PATH.format(user_id=user_id)
    payload = {"balance": int(new_balance)}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, headers=_epic_headers()) as r:
            return r.status == 200

async def epic_spend(user_id: int, amount: int) -> bool:
    amount = int(amount)
    if amount <= 0: return True
    if EPIC_SPEND_MODE == "add_negative":
        return await epic_add_coins(user_id, -amount)
    cur = await epic_get_balance(user_id)
    if cur < amount: return False
    return await epic_set_coins(user_id, cur - amount)

# ========= Wallets =========
async def wallet_get_balance(wid:str) -> int:
    async with await db() as conn:
        row = await (await conn.execute("SELECT balance FROM wallets WHERE wallet_id=?", (wid,))).fetchone()
        return int(row["balance"]) if row else 0

async def wallet_add(wid:str, amount:int):
    async with await db() as conn:
        row = await (await conn.execute("SELECT balance FROM wallets WHERE wallet_id=?", (wid,))).fetchone()
        cur = int(row["balance"]) if row else 0
        if not row:
            await conn.execute("INSERT OR IGNORE INTO wallets(wallet_id,balance) VALUES (?,?)", (wid, 0))
        await conn.execute("UPDATE wallets SET balance=? WHERE wallet_id=?", (cur+int(amount), wid))
        await conn.commit()

# ========= Relations =========
async def create_relation(guild_id: int, rtype: str, members: List[int], with_wallet: bool=False, name: Optional[str]=None, theme: Optional[str]=None) -> str:
    rtype = rtype.lower()
    uniq = sorted(set(map(int, members)))
    if rtype=="marriage" and len(uniq)!=2:
        raise ValueError("Mariage = 2 personnes")
    if rtype=="family" and not uniq:
        raise ValueError("Famille vide")
    async with await db() as conn:
        rel_id = deterministic_id(rtype, uniq)
        await conn.execute("INSERT OR REPLACE INTO relations(rel_id,guild_id,rtype,name,since,wallet_id,theme) VALUES (?,?,?,?,?,NULL,?)",
                           (rel_id, guild_id, rtype, (name[:64] if name else None), int(time.time()), (theme if rtype=='family' else None)))
        for u in uniq:
            await conn.execute("INSERT OR IGNORE INTO relation_members(rel_id,user_id) VALUES (?,?)", (rel_id, u))
        if with_wallet:
            wid = f"rel:{rel_id}"
            await conn.execute("INSERT OR IGNORE INTO wallets(wallet_id,balance) VALUES (?,?)", (wid, 0))
            for u in uniq:
                await conn.execute("INSERT OR IGNORE INTO wallet_members(wallet_id,user_id) VALUES (?,?)", (wid, u))
            await conn.execute("UPDATE relations SET wallet_id=? WHERE rel_id=?", (wid, rel_id))
        await conn.commit()
    return rel_id

async def get_marriage_rel_id(guild_id:int, a:int, b:int) -> Optional[str]:
    async with await db() as conn:
        row = await (await conn.execute("""
            SELECT r.rel_id FROM relations r
            JOIN relation_members m1 ON r.rel_id=m1.rel_id AND m1.user_id=?
            JOIN relation_members m2 ON r.rel_id=m2.rel_id AND m2.user_id=?
            WHERE r.guild_id=? AND r.rtype='marriage' LIMIT 1
        """, (a,b,guild_id))).fetchone()
    return row["rel_id"] if row else None

async def get_spouse(guild_id:int, uid:int) -> Optional[int]:
    async with await db() as conn:
        row = await (await conn.execute("""
            SELECT r.rel_id FROM relations r
            JOIN relation_members m ON r.rel_id=m.rel_id
            WHERE r.guild_id=? AND r.rtype='marriage' AND m.user_id=? LIMIT 1
        """, (guild_id, uid))).fetchone()
        if not row: return None
        rid = row["rel_id"]
        rows = await (await conn.execute("SELECT user_id FROM relation_members WHERE rel_id=?", (rid,))).fetchall()
        members = [int(x["user_id"]) for x in rows]
        if len(members)==2:
            return members[1] if members[0]==uid else members[0]
        return None

async def user_already_married(guild_id:int, uid:int) -> bool:
    return (await get_spouse(guild_id, uid)) is not None

async def marriage_wallet_id(rel_id:str) -> Optional[str]:
    async with await db() as conn:
        row = await (await conn.execute("SELECT wallet_id FROM relations WHERE rel_id=?", (rel_id,))).fetchone()
    return row["wallet_id"] if row else None

async def add_marriage_event(rel_id:str, actor_id:int, kind:str, amount:int=0):
    async with await db() as conn:
        await conn.execute("INSERT INTO marriage_events(rel_id,ts,actor_id,kind,amount) VALUES (?,?,?,?,?)",
                           (rel_id, int(time.time()), actor_id, kind, int(amount)))
        await conn.commit()

# ----- Meta mariage (surnoms / anniversaire)
async def ensure_marriage_meta(rel_id:str):
    async with await db() as conn:
        row = await (await conn.execute("SELECT 1 FROM marriage_meta WHERE rel_id=?", (rel_id,))).fetchone()
        if row: return
        rows = await (await conn.execute("SELECT user_id FROM relation_members WHERE rel_id=?", (rel_id,))).fetchall()
        members = sorted([int(r["user_id"]) for r in rows])
        if len(members)!=2: return
        await conn.execute("INSERT OR IGNORE INTO marriage_meta(rel_id,a_id,b_id,nickname_a,nickname_b,anniversary) VALUES (?,?,?,?,?,NULL)",
                           (rel_id, members[0], members[1], None, None))
        await conn.commit()

async def set_nickname(rel_id:str, user_id:int, nickname: Optional[str]):
    await ensure_marriage_meta(rel_id)
    async with await db() as conn:
        meta = await (await conn.execute("SELECT a_id,b_id FROM marriage_meta WHERE rel_id=?", (rel_id,))).fetchone()
        if not meta: return False
        col = "nickname_a" if int(user_id)==int(meta["a_id"]) else ("nickname_b" if int(user_id)==int(meta["b_id"]) else None)
        if not col: return False
        await conn.execute(f"UPDATE marriage_meta SET {col}=? WHERE rel_id=?", (nickname, rel_id))
        await conn.commit()
    return True

async def set_anniversary(rel_id:str, date_str:str):
    if not valid_date_yyyy_mm_dd(date_str): raise ValueError("Format date attendu: YYYY-MM-DD")
    await ensure_marriage_meta(rel_id)
    async with await db() as conn:
        await conn.execute("UPDATE marriage_meta SET anniversary=? WHERE rel_id=?", (date_str, rel_id))
        await conn.commit()

async def get_meta(rel_id:str):
    async with await db() as conn:
        return await (await conn.execute("SELECT * FROM marriage_meta WHERE rel_id=?", (rel_id,))).fetchone()

# ========= Arbre (famille) =========
def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont):
    try:
        x0,y0,x1,y1 = draw.textbbox((0,0), text, font=font); return (x1-x0,y1-y0)
    except Exception:
        return draw.textsize(text, font=font)

async def _fetch(url: str) -> Optional[bytes]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=6) as r:
                if r.status != 200: return None
                return await r.read()
    except Exception: return None

def _circle(img: Image.Image, size: int) -> Image.Image:
    img = img.resize((size,size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L",(size,size),0); d=ImageDraw.Draw(mask); d.ellipse((0,0,size,size), fill=255)
    out = Image.new("RGBA",(size,size)); out.paste(img,(0,0),mask); return out

def _quad(p0,p1,p2,steps=32):
    pts=[]; 
    for i in range(steps+1):
        t=i/steps
        x=(1-t)**2*p0[0]+2*(1-t)*t*p1[0]+t**2*p2[0]
        y=(1-t)**2*p0[1]+2*(1-t)*t*p1[1]+t**2*p2[1]
        pts.append((x,y))
    return pts

async def render_tree_png(guild: discord.Guild, relation_id: str, rtl=False, show_avatars=True, res:int=1, fallback_theme:str="anime") -> bytes:
    async with await db() as conn:
        fam = await (await conn.execute("SELECT name, theme FROM relations WHERE rel_id=? AND rtype='family'", (relation_id,))).fetchone()
        if not fam: raise ValueError("Famille introuvable")
        fam_name = fam["name"] or relation_id
        theme_name = (fam["theme"] or "").strip() or fallback_theme
        rows_m = await (await conn.execute("SELECT user_id FROM relation_members WHERE rel_id=?", (relation_id,))).fetchall()
        members = [int(r["user_id"]) for r in rows_m]
        rows_e = await (await conn.execute("SELECT parent_id, child_id FROM kin_edges")).fetchall()
        edges = [(int(r["parent_id"]), int(r["child_id"])) for r in rows_e if int(r["parent_id"]) in members and int(r["child_id"]) in members]
    theme = THEMES.get(theme_name, THEMES["anime"])
    if not members: raise ValueError("La famille n'a pas de membres")

    parents_of: Dict[int, List[int]] = {m: [] for m in members}
    for p,c in edges:
        parents_of.setdefault(c, []).append(p); parents_of.setdefault(p, [])
    depth_cache: Dict[int,int]={}; visiting=set()
    def depth(u:int)->int:
        if u in depth_cache: return depth_cache[u]
        if u in visiting: return 0
        visiting.add(u); ps=parents_of.get(u,[])
        d=0 if not ps else max(depth(p)+1 for p in ps)
        visiting.discard(u); depth_cache[u]=d; return d
    for u in list(parents_of.keys()): depth(u)
    by_level: Dict[int,List[int]] = {}; max_level=0
    for u,d in depth_cache.items():
        by_level.setdefault(d, []).append(u); max_level=max(max_level,d)
    for d in by_level: by_level[d].sort()

    margin_x, margin_y = 120, 140
    cell_w, cell_h = 320, 220
    card_w, card_h = 260, 108
    av_size = 64 if show_avatars else 0
    max_cols = max(len(v) for v in by_level.values()) if by_level else 1
    base_w  = margin_x*2 + max_cols*cell_w
    base_h  = margin_y*2 + (max_level+1)*cell_h
    width   = max(860, base_w) * res; height = max(560, base_h) * res

    bg = Image.new("RGB",(width,height), theme["bg"]); draw = ImageDraw.Draw(bg)
    try:
        font_title = ImageFont.truetype("arial.ttf", 24*res); font_name = ImageFont.truetype("arial.ttf", 19*res)
    except Exception:
        font_title = ImageFont.load_default(); font_name = ImageFont.load_default()

    title = f"Arbre généalogique — {fam_name}"
    tw,_ = _measure(draw, title, font_title); draw.text(((width-tw)//2, 24*res), title, fill=(60,60,90), font=font_title)

    positions: Dict[int,Tuple[int,int]] = {}
    for lvl in range(max_level+1):
        row = by_level.get(lvl, [])
        for i, uid in enumerate(row):
            cx = (margin_x + i*cell_w + cell_w//2) * res
            cy = (margin_y + lvl*cell_h + cell_h//2) * res
            positions[uid] = (cx,cy)

    for (p,c) in edges:
        if p not in positions or c not in positions: continue
        px,py = positions[p]; cx,cy = positions[c]
        ctrl=((px+cx)//2, (py+cy)//2 - 60*res)
        pts=_quad((px,py+card_h//2*res), ctrl, (cx,cy-card_h//2*res), steps=36)
        draw.line(pts, fill=theme["line"], width=4*res)

    async def card(uid:int):
        cx,cy = positions[uid]
        x0 = cx - card_w//2*res; y0 = cy - card_h//2*res
        x1 = cx + card_w//2*res; y1 = cy + card_h//2*res
        sh = Image.new("RGBA",(int(card_w*res+18*res),int(card_h*res+18*res)),(0,0,0,0))
        d2 = ImageDraw.Draw(sh)
        d2.rounded_rectangle((9*res,9*res, card_w*res+9*res, card_h*res+9*res), radius=22*res, fill=(0,0,0,85))
        sh = sh.filter(ImageFilter.GaussianBlur(8*res))
        bg.alpha_composite(sh,(int(x0-9*res),int(y0-9*res)))
        draw.rounded_rectangle([x0,y0,x1,y1], radius=22*res, outline=theme["primary"], width=3*res, fill=theme["card"])

        name = str(uid)
        if guild:
            m = guild.get_member(uid)
            if m:
                name = m.display_name
                if av_size:
                    ab = await _fetch(m.display_avatar.url)
                    if ab:
                        im = Image.open(io.BytesIO(ab)).convert("RGB")
                        av = _circle(im, av_size*res)
                        bg.paste(av, (int(x0+14*res), int(y0+(card_h*res-av_size*res)//2)), av)
        if len(name)>24: name = name[:23]+"…"
        tx = x0 + 14*res + (av_size*res+12*res if av_size else 16*res)
        draw.text((tx, y0+18*res), name, fill=(30,30,40), font=font_name)

    for uid in positions: await card(uid)
    b=io.BytesIO(); bg.save(b, format="PNG", optimize=True); return b.getvalue()

# ========= Discord =========
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ---- Base
@tree.command(name="ping", description="Test du bot.", guilds=TARGET_GUILDS)
async def ping(inter: discord.Interaction):
    await reply(inter, content="pong ✅", ephemeral=True)

@tree.command(name="sync", description="Purger global & republier en guild (admin).", guilds=TARGET_GUILDS)
@app_commands.checks.has_permissions(administrator=True)
async def sync_cmd(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    try:
        tree.clear_commands(guild=None); await tree.sync()
        await tree.sync(guild=inter.guild)
        await inter.followup.send("🔁 Sync OK. (`/ping` pour tester)", ephemeral=True)
    except Exception as e:
        await inter.followup.send(f"⚠️ {e}", ephemeral=True)

@tree.command(name="diag_aff", description="Diagnostic rapide DB/commandes.", guilds=TARGET_GUILDS)
async def diag_aff(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    try:
        await init_db()
        async with await db() as conn:
            rels = (await (await conn.execute("SELECT COUNT(*) c FROM relations")).fetchone())["c"]
        await inter.followup.send(f"✅ DB OK • relations={rels}\nGuild={inter.guild.id}", ephemeral=True)
    except Exception as e:
        await inter.followup.send(f"❌ DB KO : `{e}`", ephemeral=True)

# ---- Mariage
class VueMariage(discord.ui.View):
    def __init__(self, demandeur_id:int, cible_id:int, message:str):
        super().__init__(timeout=300)
        self.demandeur_id = demandeur_id
        self.cible_id = cible_id
        self.message = message

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.cible_id:
            await inter.response.send_message("❌ Seule la personne mentionnée peut répondre.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="💍 Accepter", style=discord.ButtonStyle.success)
    async def accepter(self, inter: discord.Interaction, btn: discord.ui.Button):
        try:
            if await user_already_married(inter.guild.id, self.demandeur_id) or await user_already_married(inter.guild.id, self.cible_id):
                await inter.response.send_message("❌ L’un de vous est déjà marié.", ephemeral=True); return
            rid = await create_relation(inter.guild.id, "marriage", [self.demandeur_id, self.cible_id], with_wallet=True)
            await ensure_marriage_meta(rid)
            await add_marriage_event(rid, inter.user.id, "accept")
            await inter.response.edit_message(content=f"🎉 **Mariage confirmé** ! <@{self.demandeur_id}> ❤️ <@{self.cible_id}>.\nUn **wallet commun** a été créé.", view=None)
            await log_line(inter.guild, f"💍 Mariage: <@{self.demandeur_id}> + <@{self.cible_id}> → {rid}")
        except Exception as e:
            await inter.response.send_message(f"⚠️ {e}", ephemeral=True)

    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.secondary)
    async def refuser(self, inter: discord.Interaction, btn: discord.ui.Button):
        await inter.response.edit_message(content="🙅 Demande refusée.", view=None)

grp_m = app_commands.Group(name="mariage", description="Mariage & wallet", guild_ids=GUILD_IDS)

@grp_m.command(name="demander", description="Demander en mariage (boutons).")
@app_commands.describe(membre="Personne à épouser", message="Petit message (optionnel)")
async def mariage_demander(inter: discord.Interaction, membre: discord.Member, message: Optional[str] = None):
    await inter.response.defer()
    if membre.id == inter.user.id:
        await inter.followup.send("😅 Pas avec toi-même.", ephemeral=True); return
    if await user_already_married(inter.guild.id, inter.user.id) or await user_already_married(inter.guild.id, membre.id):
        await inter.followup.send("❌ L’un de vous est déjà marié.", ephemeral=True); return
    txt = f"{inter.user.mention} demande **{membre.mention}** en mariage !"
    if message: txt += f"\n> _{message[:200]}_"
    await inter.followup.send(txt, view=VueMariage(inter.user.id, membre.id, message or ""))
    await add_marriage_event("pending", inter.user.id, "propose")  # trace (rid inconnu ici)

@grp_m.command(name="profil", description="Voir partenaire, wallet, surnoms & anniversaire.")
async def mariage_profil(inter: discord.Interaction):
    spouse = await get_spouse(inter.guild.id, inter.user.id)
    if not spouse:
        await reply(inter, content="Tu n’es pas marié(e) dans ce serveur.", ephemeral=True); return
    rid = await get_marriage_rel_id(inter.guild.id, inter.user.id, spouse)
    wid = await marriage_wallet_id(rid) or "—"
    bal = await wallet_get_balance(wid) if wid!="—" else 0
    meta = await get_meta(rid)
    nick_me = None; nick_sp = None; anniv = None
    if meta:
        if int(inter.user.id) == int(meta["a_id"]):
            nick_me, nick_sp = meta["nickname_a"], meta["nickname_b"]
        else:
            nick_me, nick_sp = meta["nickname_b"], meta["nickname_a"]
        anniv = meta["anniversary"]
    sp_m = inter.guild.get_member(spouse)
    lines = [
        f"**Partenaire:** {sp_m.mention if sp_m else spouse}",
        f"**Wallet:** `{wid}` • **Solde:** **{bal}** coins",
        f"**Ton surnom:** {nick_me or '—'} • **Surnom partenaire:** {nick_sp or '—'}",
        f"**Anniversaire mariage:** {anniv or '—'}",
    ]
    await reply(inter, embed=E("💞 Profil mariage", "\n".join(lines)), ephemeral=True)

@grp_m.command(name="depot", description="Déposer des coins perso dans le wallet commun.")
@app_commands.describe(montant="Nombre de coins à déposer")
async def mariage_depot(inter: discord.Interaction, montant: int):
    await inter.response.defer(ephemeral=True)
    if montant <= 0:
        await inter.followup.send("Montant invalide.", ephemeral=True); return
    spouse = await get_spouse(inter.guild.id, inter.user.id)
    if not spouse:
        await inter.followup.send("Tu n’es pas marié(e).", ephemeral=True); return
    rid = await get_marriage_rel_id(inter.guild.id, inter.user.id, spouse)
    wid = await marriage_wallet_id(rid)
    if not wid:
        await inter.followup.send("Wallet introuvable.", ephemeral=True); return
    ok = await epic_spend(inter.user.id, montant)
    if not ok:
        await inter.followup.send("❌ Solde perso insuffisant (ou API coins non configurée).", ephemeral=True); return
    await wallet_add(wid, montant)
    await add_marriage_event(rid, inter.user.id, "deposit", montant)
    await inter.followup.send(f"✅ Déposé **{montant}** coins dans `{wid}`.", ephemeral=True)
    await log_line(inter.guild, f"💰 Dépôt {montant} → {wid} par {inter.user.mention}")

# Retrait: demande + confirmation partenaire
class VueRetrait(discord.ui.View):
    def __init__(self, rid:str, wid:str, demandeur:int, montant:int, partenaire:int):
        super().__init__(timeout=240)
        self.rid=rid; self.wid=wid; self.demandeur=demandeur; self.montant=montant; self.partenaire=partenaire

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.partenaire:
            await inter.response.send_message("❌ Seul ton/ta partenaire peut confirmer ce retrait.", ephemeral=True); return False
        return True

    @discord.ui.button(label="✅ Confirmer le retrait", style=discord.ButtonStyle.success)
    async def ok(self, inter: discord.Interaction, btn: discord.ui.Button):
        bal = await wallet_get_balance(self.wid)
        if bal < self.montant:
            await inter.response.send_message("❌ Solde commun insuffisant.", ephemeral=True); return
        await wallet_add(self.wid, -self.montant)
        await epic_add_coins(self.demandeur, self.montant)
        await add_marriage_event(self.rid, inter.user.id, "withdraw_ok", self.montant)
        await inter.response.edit_message(content=f"💸 Retrait **{self.montant}** confirmé. (→ <@{self.demandeur}>)", view=None)
        await log_line(inter.guild, f"💸 Retrait {self.montant} depuis {self.wid} confirmé par {inter.user.mention}")

    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.secondary)
    async def ko(self, inter: discord.Interaction, btn: discord.ui.Button):
        await inter.response.edit_message(content="Retrait refusé.", view=None)

@grp_m.command(name="retrait", description="Demander un retrait du wallet commun (confirmation partenaire).")
@app_commands.describe(montant="Nombre de coins à retirer")
async def mariage_retrait(inter: discord.Interaction, montant: int):
    await inter.response.defer()
    if montant <= 0:
        await inter.followup.send("Montant invalide.", ephemeral=True); return
    spouse = await get_spouse(inter.guild.id, inter.user.id)
    if not spouse:
        await inter.followup.send("Tu n’es pas marié(e).", ephemeral=True); return
    rid = await get_marriage_rel_id(inter.guild.id, inter.user.id, spouse)
    wid = await marriage_wallet_id(rid)
    if not wid:
        await inter.followup.send("Wallet introuvable.", ephemeral=True); return
    bal = await wallet_get_balance(wid)
    if bal < montant:
        await inter.followup.send(f"❌ Solde commun insuffisant (actuel: {bal}).", ephemeral=True); return
    await add_marriage_event(rid, inter.user.id, "withdraw_request", montant)
    await inter.followup.send(f"{inter.user.mention} demande à retirer **{montant}** coins du wallet commun.\n"
                              f"👉 {inter.guild.get_member(spouse).mention} doit confirmer ci-dessous.",
                              view=VueRetrait(rid, wid, inter.user.id, montant, spouse))

# Cadeau (coins perso → perso partenaire)
@grp_m.command(name="cadeau", description="Offrir des coins à ton/ta partenaire (coins perso → perso).")
@app_commands.describe(montant="Nombre de coins à offrir")
async def mariage_cadeau(inter: discord.Interaction, montant: int):
    await inter.response.defer(ephemeral=True)
    if montant <= 0:
        await inter.followup.send("Montant invalide.", ephemeral=True); return
    spouse = await get_spouse(inter.guild.id, inter.user.id)
    if not spouse:
        await inter.followup.send("Tu n’es pas marié(e).", ephemeral=True); return
    ok = await epic_spend(inter.user.id, montant)
    if not ok:
        await inter.followup.send("❌ Solde perso insuffisant (ou API coins non configurée).", ephemeral=True); return
    await epic_add_coins(spouse, montant)
    rid = await get_marriage_rel_id(inter.guild.id, inter.user.id, spouse)
    await add_marriage_event(rid or "marriage", inter.user.id, "gift", montant)
    sp_m = inter.guild.get_member(spouse)
    await inter.followup.send(f"🎁 Cadeau de **{montant}** coins à {sp_m.mention if sp_m else spouse} !", ephemeral=True)
    await log_line(inter.guild, f"🎁 Cadeau {montant} coins de {inter.user.mention} → {sp_m.mention if sp_m else spouse}")

# Surnom
grp_surnom = app_commands.Group(name="surnom", description="Surnoms de couple", parent=grp_m)

@grp_surnom.command(name="set", description="Définir ton surnom dans le couple.")
@app_commands.describe(nom="Ton surnom (ex: Chéri·e)")
async def surnom_set(inter: discord.Interaction, nom: str):
    await inter.response.defer(ephemeral=True)
    spouse = await get_spouse(inter.guild.id, inter.user.id)
    if not spouse:
        await inter.followup.send("Tu n’es pas marié(e).", ephemeral=True); return
    rid = await get_marriage_rel_id(inter.guild.id, inter.user.id, spouse)
    ok = await set_nickname(rid, inter.user.id, nom[:32])
    if not ok:
        await inter.followup.send("⚠️ Impossible de définir le surnom.", ephemeral=True); return
    await add_marriage_event(rid, inter.user.id, "nick_set")
    await inter.followup.send(f"✅ Surnom défini: **{nom[:32]}**", ephemeral=True)

@grp_surnom.command(name="reset", description="Réinitialiser ton surnom.")
async def surnom_reset(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    spouse = await get_spouse(inter.guild.id, inter.user.id)
    if not spouse:
        await inter.followup.send("Tu n’es pas marié(e).", ephemeral=True); return
    rid = await get_marriage_rel_id(inter.guild.id, inter.user.id, spouse)
    ok = await set_nickname(rid, inter.user.id, None)
    if not ok:
        await inter.followup.send("⚠️ Impossible de réinitialiser.", ephemeral=True); return
    await add_marriage_event(rid, inter.user.id, "nick_reset")
    await inter.followup.send("✅ Surnom réinitialisé.", ephemeral=True)

# Anniversaire
grp_anniv = app_commands.Group(name="anniversaire", description="Anniversaire de mariage", parent=grp_m)

@grp_anniv.command(name="set", description="Définir la date d'anniversaire (AAAA-MM-JJ).")
@app_commands.describe(date="Format YYYY-MM-DD")
async def anniv_set(inter: discord.Interaction, date: str):
    await inter.response.defer(ephemeral=True)
    spouse = await get_spouse(inter.guild.id, inter.user.id)
    if not spouse:
        await inter.followup.send("Tu n’es pas marié(e).", ephemeral=True); return
    if not valid_date_yyyy_mm_dd(date):
        await inter.followup.send("Format attendu: **YYYY-MM-DD** (ex: 2025-02-14).", ephemeral=True); return
    rid = await get_marriage_rel_id(inter.guild.id, inter.user.id, spouse)
    await set_anniversary(rid, date)
    await add_marriage_event(rid, inter.user.id, "anniv_set")
    await inter.followup.send(f"📅 Anniversaire défini: **{date}**", ephemeral=True)

@grp_anniv.command(name="voir", description="Voir la date d'anniversaire.")
async def anniv_voir(inter: discord.Interaction):
    spouse = await get_spouse(inter.guild.id, inter.user.id)
    if not spouse:
        await reply(inter, content="Tu n’es pas marié(e).", ephemeral=True); return
    rid = await get_marriage_rel_id(inter.guild.id, inter.user.id, spouse)
    meta = await get_meta(rid)
    date = meta["anniversary"] if meta else None
    await reply(inter, content=f"📅 Anniversaire: **{date or '—'}**", ephemeral=True)

@grp_m.command(name="historique", description="10 derniers événements du couple.")
async def mariage_historique(inter: discord.Interaction):
    spouse = await get_spouse(inter.guild.id, inter.user.id)
    if not spouse:
        await reply(inter, content="Tu n’es pas marié(e).", ephemeral=True); return
    rid = await get_marriage_rel_id(inter.guild.id, inter.user.id, spouse)
    async with await db() as conn:
        rows = await (await conn.execute("SELECT ts,actor_id,kind,amount FROM marriage_events WHERE rel_id=? ORDER BY id DESC LIMIT 10", (rid,))).fetchall()
    if not rows:
        await reply(inter, content="Aucun événement.", ephemeral=True); return
    icons = {
        "propose":"💌","accept":"💍","deposit":"💰","withdraw_request":"📤","withdraw_ok":"✅",
        "divorce_request":"💔","divorce_ok":"💔","gift":"🎁","nick_set":"✏️","nick_reset":"🧽","anniv_set":"📅"
    }
    lines=[]
    for r in rows:
        when = datetime.fromtimestamp(int(r["ts"])).strftime("%d/%m %H:%M")
        who = inter.guild.get_member(int(r["actor_id"]))
        ic  = icons.get(r["kind"], "•")
        amt = f" ({int(r['amount'])} coins)" if r["amount"] else ""
        lines.append(f"{ic} **{when}** — {who.mention if who else r['actor_id']} — {r['kind']}{amt}")
    await reply(inter, embed=E("🗒️ Historique (10)", "\n".join(lines)), ephemeral=True)

# ---- Divorce
@grp_m.command(name="divorce", description="Demander un divorce (confirmation partenaire).")
async def mariage_divorce(inter: discord.Interaction):
    await inter.response.defer()
    spouse = await get_spouse(inter.guild.id, inter.user.id)
    if not spouse:
        await inter.followup.send("Tu n’es pas marié(e).", ephemeral=True); return
    rid = await get_marriage_rel_id(inter.guild.id, inter.user.id, spouse)

    class VueDivorce(discord.ui.View):
        def __init__(self, rid:str, a:int, b:int):
            super().__init__(timeout=240); self.rid=rid; self.a=a; self.b=b
        async def interaction_check(self, inter2: discord.Interaction) -> bool:
            return inter2.user.id in (self.a, self.b)
        @discord.ui.button(label="💔 Confirmer le divorce (50/50)", style=discord.ButtonStyle.danger)
        async def ok(self, inter2: discord.Interaction, btn: discord.ui.Button):
            wid = await marriage_wallet_id(self.rid)
            if wid:
                bal = await wallet_get_balance(wid)
                if bal>0:
                    a_share = bal//2; b_share = bal - a_share
                    await wallet_add(wid, -bal)
                    await epic_add_coins(self.a, a_share)
                    await epic_add_coins(self.b, b_share)
            async with await db() as conn:
                await conn.execute("DELETE FROM wallet_members WHERE wallet_id=?", (wid,))
                await conn.execute("DELETE FROM wallets WHERE wallet_id=?", (wid,))
                await conn.execute("DELETE FROM relation_members WHERE rel_id=?", (self.rid,))
                await conn.execute("DELETE FROM relations WHERE rel_id=?", (self.rid,))
                await conn.execute("DELETE FROM marriage_meta WHERE rel_id=?", (self.rid,))
                await conn.execute("INSERT INTO marriage_events(rel_id,ts,actor_id,kind,amount) VALUES (?,?,?,?,0)",
                                   (self.rid, int(time.time()), inter2.user.id, "divorce_ok"))
                await conn.commit()
            await inter2.response.edit_message(content="💔 Divorce effectué. Partage du solde 50/50.", view=None)
            await log_line(inter2.guild, f"💔 Divorce {self.rid} entre <@{self.a}> et <@{self.b}>")
        @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.secondary)
        async def ko(self, inter2: discord.Interaction, btn: discord.ui.Button):
            await inter2.response.edit_message(content="Divorce annulé.", view=None)

    await add_marriage_event(rid, inter.user.id, "divorce_request")
    await inter.followup.send(f"{inter.user.mention} demande un **divorce** avec {inter.guild.get_member(spouse).mention}.",
                              view=VueDivorce(rid, inter.user.id, spouse))

tree.add_command(grp_m)

# ---- Famille + arbre + ID helper
async def get_user_family_rel_id(guild_id:int, user_id:int) -> Optional[str]:
    async with await db() as conn:
        row = await (await conn.execute("""
            SELECT r.rel_id, COUNT(m2.user_id) AS n
            FROM relations r
            JOIN relation_members m ON r.rel_id=m.rel_id AND m.user_id=?
            LEFT JOIN relation_members m2 ON r.rel_id=m2.rel_id
            WHERE r.guild_id=? AND r.rtype='family'
            GROUP BY r.rel_id ORDER BY n DESC, r.since ASC LIMIT 1
        """, (user_id, guild_id))).fetchone()
    return row["rel_id"] if row else None

async def ac_familles(inter: discord.Interaction, current: str):
    try:
        async with await db() as conn:
            rows = await (await conn.execute("""
                SELECT rel_id, COALESCE(NULLIF(name,''), rel_id) AS label
                FROM relations WHERE guild_id=? AND rtype='family'
                AND (name LIKE ? OR rel_id LIKE ? OR ?='')
                ORDER BY since DESC LIMIT 20
            """, (inter.guild.id, f"%{current}%", f"%{current}%", current))).fetchall()
        return [app_commands.Choice(name=str(r["label"])[:100], value=r["rel_id"]) for r in rows]
    except Exception:
        return []

@tree.command(name="famille_creer", description="Créer une famille (thème cute).", guilds=TARGET_GUILDS)
@app_commands.describe(nom="Nom de la famille", theme="anime/kawaii/sakura/royal/neon/arabesque")
async def famille_creer(inter: discord.Interaction, nom: str, theme: str = "anime"):
    await inter.response.defer(ephemeral=True)
    try:
        if theme not in THEMES:
            await inter.followup.send("Thèmes valides : " + ", ".join(THEMES.keys()), ephemeral=True); return
        rid = await create_relation(inter.guild.id, "family", [inter.user.id], with_wallet=False, name=nom, theme=theme)
        await inter.followup.send(f"👪 Famille **{nom}** créée (id=`{rid}`) • thème **{theme}** ✨", ephemeral=True)
        await log_line(inter.guild, f"👪 Famille `{rid}` (thème {theme}) par {inter.user.mention}")
    except Exception as e:
        await inter.followup.send(f"⚠️ {e}", ephemeral=True)

@tree.command(name="arbre", description="Affiche l'arbre (auto : ta famille).", guilds=TARGET_GUILDS)
@app_commands.describe(famille="Choisir une famille (autocomplétion)", personne="Afficher la famille de cette personne", public="Poster publiquement")
@app_commands.autocomplete(famille=ac_familles)
async def arbre(inter: discord.Interaction, famille: Optional[str] = None, personne: Optional[discord.Member] = None, public: bool = False):
    try:
        if famille:
            rel_id = famille
        elif personne:
            rel_id = await get_user_family_rel_id(inter.guild.id, personne.id)
        else:
            rel_id = await get_user_family_rel_id(inter.guild.id, inter.user.id)
        if not rel_id:
            await reply(inter, content="❌ Aucune famille trouvée. Utilise `/famille_creer` d’abord.", ephemeral=True); return
        sett = await get_settings(inter.guild.id)
        theme_default = sett.get("theme") or "anime"
        rtl   = bool(sett.get("rtl", 0)); show  = bool(sett.get("avatars", 1))
        await inter.response.defer(ephemeral=not public)
        png = await render_tree_png(inter.guild, rel_id, rtl=rtl, show_avatars=show, res=1, fallback_theme=theme_default)
        await inter.followup.send(file=discord.File(io.BytesIO(png), filename=f"arbre_{rel_id}.png"), ephemeral=not public)
    except Exception as e:
        await reply(inter, content=f"⚠️ Erreur: {e}", ephemeral=True)
        traceback.print_exc()

@tree.command(name="famille_id", description="Affiche l'ID de la famille principale (toi ou quelqu'un).", guilds=TARGET_GUILDS)
async def famille_id_cmd(inter: discord.Interaction, personne: Optional[discord.Member] = None):
    cible = personne or inter.user
    rid = await get_user_family_rel_id(inter.guild.id, cible.id)
    if not rid:
        await reply(inter, content=f"❌ Aucune famille trouvée pour {cible.mention}.", ephemeral=True); return
    async with await db() as conn:
        row = await (await conn.execute("SELECT name, theme FROM relations WHERE rel_id=?", (rid,))).fetchone()
    nom = (row["name"] or "Sans nom") if row else "—"
    theme = (row["theme"] or "anime") if row else "anime"
    await reply(inter, content=f"🆔 **Famille**: {nom} • **ID**: `{rid}` • thème: **{theme}**", ephemeral=True)

# ========= Lifecycle =========
@bot.event
async def on_ready():
    print(f"Connecté en {bot.user}")
    await init_db()
    try:
        # purge global puis push guild-only (immédiat)
        tree.clear_commands(guild=None); await tree.sync()
        for g in TARGET_GUILDS:
            await tree.sync(guild=g)
        print("Slash sync (guild-only) OK.")
    except Exception as e:
        print("Sync error:", e)

async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN manquant")
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
