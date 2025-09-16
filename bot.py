# bot.py — Affiliations FR (UHQ) — guild-only + réponses instantanées (defer)
# - Mariage/ami/frère-soeur/famille + wallets partagés (mariage & famille = toujours OUI)
# - Contrat de mariage + historique par famille
# - Divorce avec contrat (répartition + pénalité)
# - Arbre généalogique (affiche le NOM DE FAMILLE, pas l’ID)
# - API FastAPI /v1 pour intégrations casino/coins
# - Slash commands FR en guild-only (instantané)

import os, asyncio, time, io, traceback
from typing import Optional, List, Tuple, Dict

import discord
from discord import app_commands
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response
import uvicorn
import aiohttp
import aiosqlite
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageFilter

load_dotenv()

# ---------------- Config ----------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Guilds ciblées pour les slash (instantané). Mets GUILD_IDS="123,456" ou GUILD_ID="123"
GUILD_IDS = [int(x) for x in (os.getenv("GUILD_IDS") or os.getenv("GUILD_ID","")).replace(" ","").split(",") if x.strip().isdigit()]
TARGET_GUILDS = [discord.Object(id=g) for g in GUILD_IDS]
GUILD_ID_ENV  = GUILD_IDS[0] if GUILD_IDS else None  # pour seeds de défauts

API_HOST      = os.getenv("API_HOST", "0.0.0.0")
API_BASE      = os.getenv("API_BASE", "/v1")
API_PORT      = int(os.getenv("PORT") or os.getenv("API_PORT") or "8000")
API_SHARED_SECRET = os.getenv("API_SHARED_SECRET", "")
OWNER_IDS_ENV = [int(x) for x in (os.getenv("OWNER_IDS","").replace(" ","") or "").split(",") if x.strip().isdigit()]

# Epic (facultatif)
EPIC_BASE_URL = os.getenv("EPIC_BASE_URL", "")
EPIC_API_KEY  = os.getenv("EPIC_API_KEY", "")
EPIC_AUTH_SCHEME = os.getenv("EPIC_AUTH_SCHEME", "raw")   # "bearer" ou "raw"
EPIC_GET_BALANCE_PATH = os.getenv("EPIC_GET_BALANCE_PATH", "/users/{user_id}/coins")
EPIC_ADD_COINS_PATH   = os.getenv("EPIC_ADD_COINS_PATH", "/users/{user_id}/coins/add")
EPIC_SET_COINS_PATH   = os.getenv("EPIC_SET_COINS_PATH", "/users/{user_id}/coins/set")
EPIC_SPEND_MODE       = os.getenv("EPIC_SPEND_MODE", "add_negative")  # add_negative|set

DB_PATH = os.getenv("DB_PATH", "./affiliations.db")
BRAND_COLOR = 0x7C3AED
LOGS_DEFAULT_CHAN_ID = int(os.getenv("LOGS_DEFAULT_CHAN_ID", "1417304969333440553"))

# secret runtime (modifiable par commande)
RUNTIME_SECRET: Optional[str] = None

# ---------------- DB ----------------
CREATE_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS relations (
  rel_id    TEXT PRIMARY KEY,
  guild_id  INTEGER,
  rtype     TEXT,      -- marriage|friend|sibling|family
  name      TEXT,      -- nom de famille (pour rtype='family')
  since     INTEGER,
  wallet_id TEXT
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

CREATE TABLE IF NOT EXISTS divorce_contracts (
  contract_id   TEXT PRIMARY KEY,
  guild_id      INTEGER,
  a_id          INTEGER,
  b_id          INTEGER,
  split_mode    TEXT,      -- equal|percent
  percent_for_a INTEGER,
  penalty_from  INTEGER,
  penalty_to    INTEGER,
  penalty_coins INTEGER,
  status        TEXT,      -- pending|a_accepted|b_accepted|accepted|rejected|expired|completed
  created_at    INTEGER,
  expires_at    INTEGER
);

CREATE TABLE IF NOT EXISTS guild_settings (
  guild_id   INTEGER PRIMARY KEY,
  theme      TEXT,
  rtl        INTEGER,
  avatars    INTEGER,
  log_chan   INTEGER
);

CREATE TABLE IF NOT EXISTS owners (
  guild_id INTEGER,
  user_id  INTEGER,
  PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS global_kv (
  k TEXT PRIMARY KEY,
  v TEXT
);

-- Contrat de mariage + logs
CREATE TABLE IF NOT EXISTS marriage_contracts (
  contract_id TEXT PRIMARY KEY,
  guild_id    INTEGER,
  a_id        INTEGER,
  b_id        INTEGER,
  wallet      INTEGER,   -- 1/0 (toujours 1 mais on garde la colonne)
  prenup      TEXT,      -- résumé/notes
  status      TEXT,      -- pending|accepted|rejected|expired
  created_at  INTEGER,
  accepted_at INTEGER
);

CREATE TABLE IF NOT EXISTS contract_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id TEXT,
  kind        TEXT,     -- marriage|divorce
  message     TEXT,
  ts          INTEGER
);

-- Verrou: un seul mariage par personne (par serveur)
CREATE TRIGGER IF NOT EXISTS trg_one_marriage_per_user_per_guild
BEFORE INSERT ON relation_members
WHEN (SELECT rtype FROM relations WHERE rel_id = NEW.rel_id) = 'marriage'
BEGIN
  SELECT RAISE(ABORT, 'already married')
  WHERE EXISTS (
    SELECT 1
    FROM relation_members rm
    JOIN relations r ON rm.rel_id = r.rel_id
    WHERE rm.user_id = NEW.user_id
      AND r.rtype   = 'marriage'
      AND r.guild_id = (SELECT guild_id FROM relations WHERE rel_id = NEW.rel_id)
  );
END;
"""

async def db():
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    return conn

async def init_db():
    conn = await db()
    await conn.executescript(CREATE_SQL)
    # seeds si GUILD_ID_ENV fourni
    if GUILD_ID_ENV:
        for uid in OWNER_IDS_ENV:
            await conn.execute("INSERT OR IGNORE INTO owners(guild_id,user_id) VALUES (?,?)", (GUILD_ID_ENV, int(uid)))
        row = await (await conn.execute("SELECT 1 FROM guild_settings WHERE guild_id=?", (GUILD_ID_ENV,))).fetchone()
        if not row:
            await conn.execute(
                "INSERT INTO guild_settings(guild_id,theme,rtl,avatars,log_chan) VALUES (?,?,?,?,?)",
                (GUILD_ID_ENV, "kawaii", 0, 1, LOGS_DEFAULT_CHAN_ID)
            )
    await conn.commit()
    await conn.close()

# ---------------- Utils ----------------
def E(title: str, desc: str) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=BRAND_COLOR)

async def is_owner(gid: int, user: discord.abc.User) -> bool:
    if isinstance(user, discord.Member) and user.guild_permissions.administrator:
        return True
    async with await db() as conn:
        row = await (await conn.execute("SELECT 1 FROM owners WHERE guild_id=? AND user_id=? LIMIT 1", (gid, user.id))).fetchone()
        return bool(row)

def owner_check():
    async def predicate(inter: discord.Interaction):
        if await is_owner(inter.guild.id, inter.user):
            return True
        await inter.response.send_message("❌ Cette commande est réservée aux **propriétaires**.", ephemeral=True)
        return False
    return app_commands.check(predicate)

async def get_settings(guild_id:int) -> Dict[str,int|str|None]:
    async with await db() as conn:
        row = await (await conn.execute("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,))).fetchone()
        if not row:
            return {"guild_id": guild_id, "theme": "kawaii", "rtl": 0, "avatars": 1, "log_chan": LOGS_DEFAULT_CHAN_ID}
        return dict(row)

async def set_setting(guild_id:int, key:str, value):
    keys = {"theme","rtl","avatars","log_chan"}
    if key not in keys: return False
    async with await db() as conn:
        exists = await (await conn.execute("SELECT 1 FROM guild_settings WHERE guild_id=?", (guild_id,))).fetchone()
        if exists:
            await conn.execute(f"UPDATE guild_settings SET {key}=? WHERE guild_id=?", (value, guild_id))
        else:
            theme = value if key=="theme" else "kawaii"
            rtl   = int(value) if key=="rtl" else 0
            avatars = int(value) if key=="avatars" else 1
            log_chan = int(value) if key=="log_chan" else LOGS_DEFAULT_CHAN_ID
            await conn.execute("INSERT INTO guild_settings(guild_id,theme,rtl,avatars,log_chan) VALUES (?,?,?,?,?)",
                               (guild_id, theme, rtl, avatars, log_chan))
        await conn.commit()
    return True

async def get_runtime_secret() -> Optional[str]:
    global RUNTIME_SECRET
    if RUNTIME_SECRET: return RUNTIME_SECRET
    async with await db() as conn:
        row = await (await conn.execute("SELECT v FROM global_kv WHERE k='api_secret'")).fetchone()
        if row: RUNTIME_SECRET = row["v"]
    return RUNTIME_SECRET

async def log_line(guild: discord.Guild, text: str):
    try:
        sett = await get_settings(guild.id)
        ch_id = int(sett.get("log_chan") or LOGS_DEFAULT_CHAN_ID)
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel):
            await ch.send(text)
    except Exception:
        print("[LOG_ERR]", text)

async def ack(inter: discord.Interaction, ephemeral: bool=True):
    # Confirme l'interaction en <3s pour éviter "appli ne répond plus"
    if not inter.response.is_done():
        await inter.response.defer(ephemeral=ephemeral)

# ---------------- Epic adapter ----------------
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

# ---------------- Relations & Wallets ----------------
THEMES = {
    "kawaii":    {"bg":(250,247,255), "primary":(124,58,237), "line":(160,140,210), "card":(255,255,255)},
    "sakura":    {"bg":(255,247,251), "primary":(221,73,104), "line":(239,170,184), "card":(255,255,255)},
    "royal":     {"bg":(245,246,252), "primary":(66,90,188),  "line":(120,140,210), "card":(255,255,255)},
    "neon":      {"bg":(18,18,22),    "primary":(0,245,212),  "line":(80,255,200),  "card":(36,36,44)},
    "arabesque": {"bg":(248,246,240), "primary":(189,119,26), "line":(169,139,99),  "card":(255,255,252)},
}

def deterministic_rel_id(rtype: str, members: List[int]) -> str:
    base = ":".join(map(str, sorted(set(members))))
    return f"family:{int(time.time())}:{base[:6]}" if rtype=="family" else f"{rtype}:{base}"

async def create_relation(guild_id: int, rtype: str, members: List[int], with_wallet: bool=False, name: Optional[str]=None) -> str:
    rtype = rtype.lower()
    uniq = sorted(set(map(int, members)))
    if rtype in {"marriage","friend","sibling"} and len(uniq)!=2:
        raise ValueError("Relation en duo requise")
    async with await db() as conn:
        if rtype=="marriage":
            for u in uniq:
                q = """SELECT 1 FROM relations r
                       JOIN relation_members m ON r.rel_id=m.rel_id
                       WHERE r.guild_id=? AND r.rtype='marriage' AND m.user_id=? LIMIT 1"""
                if await (await conn.execute(q, (guild_id, u))).fetchone():
                    raise ValueError("Déjà marié(e)")
        rel_id = deterministic_rel_id(rtype, uniq)
        await conn.execute(
            "INSERT OR REPLACE INTO relations(rel_id,guild_id,rtype,name,since,wallet_id) VALUES (?,?,?,?,?,NULL)",
            (rel_id, guild_id, rtype, (name[:64] if name else None), int(time.time()))
        )
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

async def add_member_to_family(rel_id: str, user_id: int):
    async with await db() as conn:
        row = await (await conn.execute("SELECT rtype, wallet_id FROM relations WHERE rel_id=?", (rel_id,))).fetchone()
        if not row or row["rtype"]!="family": raise ValueError("Relation non famille")
        await conn.execute("INSERT OR IGNORE INTO relation_members(rel_id,user_id) VALUES (?,?)", (rel_id, user_id))
        if row["wallet_id"]:
            await conn.execute("INSERT OR IGNORE INTO wallet_members(wallet_id,user_id) VALUES (?,?)", (row["wallet_id"], user_id))
        await conn.commit()

async def list_user_wallets(user_id: int) -> List[Tuple[str,str,str]]:
    async with await db() as conn:
        q = """SELECT r.rel_id, r.wallet_id, r.rtype
               FROM relations r JOIN relation_members m ON r.rel_id=m.rel_id
               WHERE m.user_id=? AND r.wallet_id IS NOT NULL"""
        rows = await (await conn.execute(q, (user_id,))).fetchall()
        return [(r["rel_id"], r["wallet_id"], r["rtype"]) for r in rows]

async def dissolve_relation(rel_id: str, split_evenly: bool=True, percent_for_a: int=50, a_id: Optional[int]=None, b_id: Optional[int]=None):
    async with await db() as conn:
        row = await (await conn.execute("SELECT wallet_id FROM relations WHERE rel_id=?", (rel_id,))).fetchone()
        if row:
            wid = row["wallet_id"]
            members = [r["user_id"] for r in await (await conn.execute("SELECT user_id FROM relation_members WHERE rel_id=?", (rel_id,))).fetchall()]
            if wid:
                w = await (await conn.execute("SELECT balance FROM wallets WHERE wallet_id=?", (wid,))).fetchone()
                bal = int(w["balance"]) if w else 0
                if bal>0 and members:
                    if split_evenly or len(members)!=2:
                        share = bal // len(members)
                        rest  = bal - share*len(members)
                        for i,u in enumerate(members):
                            plus = share + (1 if i<rest else 0)
                            if plus>0: await epic_add_coins(u, plus)
                    else:
                        if a_id and b_id:
                            a_share = int(bal * (percent_for_a/100))
                            b_share = bal - a_share
                            if a_share>0: await epic_add_coins(a_id, a_share)
                            if b_share>0: await epic_add_coins(b_id, b_share)
                await conn.execute("DELETE FROM wallet_members WHERE wallet_id=?", (wid,))
                await conn.execute("DELETE FROM wallets WHERE wallet_id=?", (wid,))
        await conn.execute("DELETE FROM relation_members WHERE rel_id=?", (rel_id,))
        await conn.execute("DELETE FROM relations WHERE rel_id=?", (rel_id,))
        await conn.commit()

async def get_marriage_rel_id(guild_id: int, a_id: int, b_id: int) -> Optional[str]:
    async with await db() as conn:
        q = """
        SELECT r.rel_id
        FROM relations r
        JOIN relation_members m1 ON r.rel_id = m1.rel_id
        JOIN relation_members m2 ON r.rel_id = m2.rel_id
        WHERE r.guild_id = ? AND r.rtype = 'marriage'
          AND m1.user_id = ? AND m2.user_id = ?
        LIMIT 1
        """
        row = await (await conn.execute(q, (guild_id, a_id, b_id))).fetchone()
        return row["rel_id"] if row else None

# ---------------- Contrats (mariage & divorce) ----------------
def _id_contrat(a: int, b: int) -> str:
    x,y = sorted([int(a), int(b)])
    return f"div:{x}:{y}:{int(time.time())}"

def _id_mariage(a: int, b: int) -> str:
    x, y = sorted([int(a), int(b)])
    return f"mar:{x}:{y}:{int(time.time())}"

async def log_contract_event(contract_id: str, kind: str, message: str):
    async with await db() as conn:
        await conn.execute(
            "INSERT INTO contract_logs(contract_id,kind,message,ts) VALUES (?,?,?,?)",
            (contract_id, kind, message, int(time.time()))
        )
        await conn.commit()

async def creer_contrat_mariage(guild_id:int, a_id:int, b_id:int, prenup:str) -> str:
    # wallet = toujours 1 (activé)
    cid = _id_mariage(a_id, b_id)
    async with await db() as conn:
        await conn.execute(
            """INSERT INTO marriage_contracts(contract_id,guild_id,a_id,b_id,wallet,prenup,status,created_at,accepted_at)
               VALUES (?,?,?,?,1,?,'pending', ?, NULL)""",
            (cid, guild_id, a_id, b_id, prenup[:400], int(time.time()))
        )
        await conn.commit()
    await log_contract_event(cid, "marriage", "Contrat créé (en attente)")
    return cid

async def maj_contrat_mariage_status(cid:str, status:str):
    async with await db() as conn:
        if status == "accepted":
            await conn.execute("UPDATE marriage_contracts SET status=?, accepted_at=? WHERE contract_id=?",
                               (status, int(time.time()), cid))
        else:
            await conn.execute("UPDATE marriage_contracts SET status=? WHERE contract_id=?", (status, cid))
        await conn.commit()
    await log_contract_event(cid, "marriage", f"Statut: {status}")

async def lire_contrat_mariage(cid:str):
    async with await db() as conn:
        row = await (await conn.execute("SELECT * FROM marriage_contracts WHERE contract_id=?", (cid,))).fetchone()
        return dict(row) if row else None

async def creer_contrat_divorce(guild_id:int, a_id:int, b_id:int, split_mode:str, percent_for_a:int, penalty_from:Optional[int], penalty_to:Optional[int], penalty_coins:int, expires_minutes:int) -> str:
    cid = _id_contrat(a_id, b_id)
    async with await db() as conn:
        await conn.execute("""INSERT INTO divorce_contracts(contract_id,guild_id,a_id,b_id,split_mode,percent_for_a,penalty_from,penalty_to,penalty_coins,status,created_at,expires_at)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (cid,guild_id,a_id,b_id,split_mode,percent_for_a,penalty_from,penalty_to,penalty_coins,"pending",int(time.time()),int(time.time()+expires_minutes*60)))
        await conn.commit()
    await log_contract_event(cid, "divorce", "Contrat créé (en attente)")
    return cid

async def lire_contrat(cid: str):
    async with await db() as conn:
        row = await (await conn.execute("SELECT * FROM divorce_contracts WHERE contract_id=?", (cid,))).fetchone()
        return dict(row) if row else None

async def maj_contrat_status(cid: str, status: str):
    async with await db() as conn:
        await conn.execute("UPDATE divorce_contracts SET status=? WHERE contract_id=?", (status, cid))
        await conn.commit()
    await log_contract_event(cid, "divorce", f"Statut: {status}")

# ---------------- Famille: résolution nom/ID ----------------
async def resolve_family_rel_id(guild_id:int, key:str) -> Optional[str]:
    key = (key or "").strip()
    async with await db() as conn:
        row = await (await conn.execute(
            "SELECT rel_id FROM relations WHERE guild_id=? AND rtype='family' AND rel_id=? LIMIT 1",
            (guild_id, key)
        )).fetchone()
        if row: return row["rel_id"]
        row = await (await conn.execute(
            "SELECT rel_id FROM relations WHERE guild_id=? AND rtype='family' AND LOWER(name)=LOWER(?) LIMIT 1",
            (guild_id, key)
        )).fetchone()
        return row["rel_id"] if row else None

async def family_members(rel_id:str) -> List[int]:
    async with await db() as conn:
        rows = await (await conn.execute("SELECT user_id FROM relation_members WHERE rel_id=?", (rel_id,))).fetchall()
        return [int(r["user_id"]) for r in rows]

async def user_in_relation(rel_id:str, user_id:int) -> bool:
    async with await db() as conn:
        row = await (await conn.execute(
            "SELECT 1 FROM relation_members WHERE rel_id=? AND user_id=? LIMIT 1",
            (rel_id, user_id)
        )).fetchone()
        return bool(row)

# ---------------- Arbre (rendu) ----------------
def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont):
    try:
        x0,y0,x1,y1 = draw.textbbox((0,0), text, font=font)
        return (x1-x0, y1-y0)
    except Exception:
        return draw.textsize(text, font=font)

async def _fetch_avatar_bytes(url: str) -> Optional[bytes]:
    if not url: return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=6) as r:
                if r.status != 200: return None
                return await r.read()
    except Exception:
        return None

def _circle_avatar(img: Image.Image, size: int) -> Image.Image:
    img = img.resize((size,size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L",(size,size),0)
    d = ImageDraw.Draw(mask)
    d.ellipse((0,0,size,size), fill=255)
    out = Image.new("RGBA",(size,size))
    out.paste(img,(0,0),mask)
    return out

def _quad_curve(p0, p1, p2, steps=32):
    pts = []
    for i in range(steps+1):
        t = i/steps
        x = (1-t)**2*p0[0] + 2*(1-t)*t*p1[0] + t**2*p2[0]
        y = (1-t)**2*p0[1] + 2*(1-t)*t*p1[1] + t**2*p2[1]
        pts.append((x,y))
    return pts

def _arabesque_pattern(w,h, col):
    img = Image.new("RGBA",(w,h),(0,0,0,0))
    d = ImageDraw.Draw(img)
    step = 28
    for y in range(0,h,step):
        for x in range(0,w,step):
            r = step//2 - 6
            d.arc((x+6,y+6,x+6+2*r,y+6+2*r), start=0, end=360, fill=col+(50,), width=1)
    return img

async def render_family_tree_png(guild: Optional[discord.Guild], relation_id: str, theme_name:str="kawaii", rtl:bool=False, show_avatars:bool=True, res:int=1) -> bytes:
    theme = THEMES.get(theme_name, THEMES["kawaii"])
    async with await db() as conn:
        fam = await (await conn.execute("SELECT rtype, name FROM relations WHERE rel_id=?", (relation_id,))).fetchone()
        if not fam or fam["rtype"] != "family":
            raise ValueError("Relation non trouvée ou pas une famille")
        fam_name = fam["name"] or relation_id
        rows = await (await conn.execute("SELECT user_id FROM relation_members WHERE rel_id=?", (relation_id,))).fetchall()
        members = [int(r["user_id"]) for r in rows]
        edges_rows = await (await conn.execute("SELECT parent_id, child_id FROM kin_edges")).fetchall()
        edges = [(int(r["parent_id"]), int(r["child_id"])) for r in edges_rows if int(r["parent_id"]) in members and int(r["child_id"]) in members]
    if not members:
        raise ValueError("Cette famille n'a pas de membres")

    parents_of: Dict[int, List[int]] = {m: [] for m in members}
    for p,c in edges:
        parents_of.setdefault(c, []).append(p)
        parents_of.setdefault(p, [])
    depth_cache: Dict[int,int] = {}
    visiting=set()
    def depth(u:int)->int:
        if u in depth_cache: return depth_cache[u]
        if u in visiting: return 0
        visiting.add(u)
        ps = parents_of.get(u, [])
        d = 0 if not ps else max(depth(p)+1 for p in ps)
        visiting.discard(u)
        depth_cache[u]=d
        return d
    for u in list(parents_of.keys()): depth(u)
    levels = depth_cache
    max_level = max(levels.values()) if levels else 0
    by_level: Dict[int, List[int]] = {}
    for u,d in levels.items():
        by_level.setdefault(d, []).append(u)
    for d in by_level:
        by_level[d].sort(reverse=rtl)

    margin_x, margin_y = 120, 140
    cell_w, cell_h = 320, 220
    card_w, card_h = 260, 108
    avatar_size = 64 if show_avatars else 0

    max_cols = max(len(v) for v in by_level.values()) if by_level else 1
    base_w  = margin_x*2 + max_cols*cell_w
    base_h  = margin_y*2 + (max_level+1)*cell_h
    width   = max(860, base_w) * res
    height  = max(560, base_h) * res

    bg = Image.new("RGB", (width,height), theme["bg"])
    overlay = Image.new("RGBA",(width,height),(0,0,0,0))
    if theme_name == "arabesque":
        patt = _arabesque_pattern(600*res, 400*res, theme["line"][:3])
        for y in range(0, height, 400*res):
            for x in range(0, width, 600*res):
                overlay.alpha_composite(patt, (x,y))
    bg = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(bg)
    try:
        font_title = ImageFont.truetype("arial.ttf", 24*res)
        font_name  = ImageFont.truetype("arial.ttf", 19*res)
    except Exception:
        font_title = ImageFont.load_default()
        font_name  = ImageFont.load_default()

    title = f"Arbre généalogique — {fam_name}"
    tw, _ = _measure(draw, title, font_title)
    draw.text(((width-tw)//2, 24*res), title, fill=(60,60,90), font=font_title)

    positions: Dict[int, Tuple[int,int]] = {}
    for d in range(max_level+1):
        row = by_level.get(d, [])
        for i, uid in enumerate(row):
            cx = (margin_x + i*cell_w + cell_w//2) * res
            if rtl: cx = width - cx
            cy = (margin_y + d*cell_h + cell_h//2) * res
            positions[uid] = (cx, cy)

    for (p,c) in edges:
        if p not in positions or c not in positions: continue
        px,py = positions[p]; cx,cy = positions[c]
        ctrl = ((px+cx)//2, (py+cy)//2 - 60*res)
        pts = _quad_curve((px, py+card_h//2*res), ctrl, (cx, cy-card_h//2*res), steps=36)
        draw.line(pts, fill=theme["line"], width=4*res)

    async def render_card(uid:int):
        cx,cy = positions[uid]
        x0 = cx - card_w//2*res; y0 = cy - card_h//2*res
        x1 = cx + card_w//2*res; y1 = cy + card_h//2*res
        shadow = Image.new("RGBA", (int(card_w*res+18*res), int(card_h*res+18*res)), (0,0,0,0))
        d2 = ImageDraw.Draw(shadow)
        d2.rounded_rectangle((9*res,9*res, card_w*res+9*res, card_h*res+9*res), radius=22*res, fill=(0,0,0,85))
        shadow = shadow.filter(ImageFilter.GaussianBlur(8*res))
        bg.alpha_composite(shadow, (int(x0-9*res), int(y0-9*res)))
        draw.rounded_rectangle([x0,y0,x1,y1], radius=22*res, outline=theme["primary"], width=3*res, fill=theme["card"])

        ax = x0 + 14*res; ay = y0 + (108*res - (64*res if show_avatars else 0))//2
        display_name = str(uid)
        if guild:
            m = guild.get_member(uid)
            if m:
                display_name = m.display_name
                if show_avatars:
                    ab = await _fetch_avatar_bytes(m.display_avatar.url)
                    if ab:
                        try:
                            im = Image.open(io.BytesIO(ab)).convert("RGB")
                            av = _circle_avatar(im, 64*res)
                            bg.paste(av, (int(ax), int(ay)), av)
                        except Exception:
                            pass
        if len(display_name) > 24: display_name = display_name[:23] + "…"
        tx = ax + (64*res+12*res if show_avatars else 16*res)
        ty = y0 + 18*res
        draw.text((tx, ty), display_name, fill=(30,30,40), font=font_name)

    for uid in positions:
        await render_card(uid)

    b = io.BytesIO()
    bg.save(b, format="PNG", optimize=True)
    return b.getvalue()

# ---------------- Discord bot ----------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # pour noms/avatars
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# -------- Vues (consentement & divorce) --------
class VueRelation(discord.ui.View):
    def __init__(self, rtype: str, demandeur_id: int, cible_id: int, timeout: int = 240):
        super().__init__(timeout=timeout)
        self.rtype=rtype; self.demandeur_id=demandeur_id; self.cible_id=cible_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.cible_id:
            await interaction.response.send_message("❌ Seule la personne mentionnée peut répondre.", ephemeral=True); return False
        return True

    @discord.ui.button(label="✅ Accepter", style=discord.ButtonStyle.success)
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # mariage & famille => wallet toujours créé, ami/sibling => non
            with_wallet = True if self.rtype in ("marriage","family") else False
            rid = await create_relation(interaction.guild.id, self.rtype, [self.demandeur_id, self.cible_id], with_wallet=with_wallet)
            txt = f"🎉 Relation **{self.rtype}** créée entre <@{self.demandeur_id}> et <@{self.cible_id}>."
            if with_wallet: txt += f" Wallet: `rel:{rid}`"
            await interaction.response.edit_message(content=txt, view=None)
            await log_line(interaction.guild, f"🔗 Relation {self.rtype} créée: <@{self.demandeur_id}> + <@{self.cible_id}> — wallet:{with_wallet}")
        except Exception as e:
            await interaction.response.send_message(f"⚠️ Impossible: {e}", ephemeral=True)

    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger)
    async def refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="🙅 Demande refusée.", view=None)

class VueDivorce(discord.ui.View):
    def __init__(self, contrat_id: str, a_id: int, b_id: int, timeout: int = 1200):
        super().__init__(timeout=timeout)
        self.contrat_id = contrat_id
        self.a_id = a_id
        self.b_id = b_id

    async def _signer(self, inter: discord.Interaction, qui: str):
        c = await lire_contrat(self.contrat_id)
        if not c:
            await inter.response.send_message("⚠️ Contrat introuvable.", ephemeral=True); return
        if c["status"] in ("completed","rejected","expired"):
            await inter.response.send_message("ℹ️ Contrat déjà finalisé.", ephemeral=True); return
        now = int(time.time())
        if c["expires_at"] and now > int(c["expires_at"]):
            await maj_contrat_status(self.contrat_id, "expired")
            await inter.response.send_message("⌛ Contrat expiré.", ephemeral=True); return
        new_status = None
        if qui=="a" and inter.user.id==c["a_id"]:
            new_status = "a_accepted" if c["status"] in ("pending","b_accepted") else "a_accepted"
            if c["status"] == "b_accepted": new_status = "accepted"
        elif qui=="b" and inter.user.id==c["b_id"]:
            new_status = "b_accepted" if c["status"] in ("pending","a_accepted") else "b_accepted"
            if c["status"] == "a_accepted": new_status = "accepted"
        else:
            await inter.response.send_message("❌ Cette action n’est pas pour toi.", ephemeral=True); return
        await maj_contrat_status(self.contrat_id, new_status)
        await log_line(inter.guild, f"📝 Divorce {self.contrat_id}: {new_status} par {inter.user.mention}")
        if new_status != "accepted":
            await inter.response.send_message("✅ Signature enregistrée. En attente de l'autre personne.", ephemeral=True)
            return

        c = await lire_contrat(self.contrat_id)
        rid = await get_marriage_rel_id(inter.guild.id, c["a_id"], c["b_id"])
        if c["penalty_coins"] and c["penalty_coins"]>0 and c["penalty_from"] and c["penalty_to"]:
            ok = await epic_spend(int(c["penalty_from"]), int(c["penalty_coins"]))
            if ok:
                await epic_add_coins(int(c["penalty_to"]), int(c["penalty_coins"]))
        if rid:
            if c["split_mode"]=="percent":
                await dissolve_relation(rid, split_evenly=False, percent_for_a=int(c["percent_for_a"]), a_id=int(c["a_id"]), b_id=int(c["b_id"]))
            else:
                await dissolve_relation(rid, split_evenly=True, a_id=int(c["a_id"]), b_id=int(c["b_id"]))
        await maj_contrat_status(self.contrat_id, "completed")
        await inter.response.edit_message(content="💔 Divorce finalisé. Contrat exécuté.", view=None)
        await log_line(inter.guild, f"💔 Divorce complété pour {self.contrat_id}")

    @discord.ui.button(label="✍️ Je signe (A)", style=discord.ButtonStyle.success)
    async def signe_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._signer(interaction, "a")

    @discord.ui.button(label="✍️ Je signe (B)", style=discord.ButtonStyle.success)
    async def signe_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._signer(interaction, "b")

    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger)
    async def refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        c = await lire_contrat(self.contrat_id)
        if not c:
            await interaction.response.send_message("⚠️ Contrat introuvable.", ephemeral=True); return
        if interaction.user.id not in (c["a_id"], c["b_id"]):
            await interaction.response.send_message("❌ Cette action n’est pas pour toi.", ephemeral=True); return
        await maj_contrat_status(self.contrat_id, "rejected")
        await interaction.response.edit_message(content="❌ Divorce annulé (contrat rejeté).", view=None)
        await log_line(interaction.guild, f"🛑 Divorce {self.contrat_id}: rejeté par {interaction.user.mention}")

# ---------------- Slash FR (GUILD-ONLY) ----------------
@tree.command(name="proposer_relation", description="Proposer une relation (mariage|ami|frere_soeur)", guilds=TARGET_GUILDS)
@app_commands.describe(membre="Membre", type="mariage|ami|frere_soeur")
async def proposer_relation(interaction: discord.Interaction, membre: discord.Member, type: str):
    await ack(interaction)
    try:
        mapping = {"mariage":"marriage","ami":"friend","frere_soeur":"sibling"}
        if type not in mapping:
            await interaction.followup.send("Types valides: mariage, ami, frere_soeur", ephemeral=True); return
        if membre.id == interaction.user.id:
            await interaction.followup.send("😅 Pas avec toi-même.", ephemeral=True); return
        rtype = mapping[type]

        # si mariage → créer un contrat + wallet toujours activé
        if rtype == "marriage":
            rid_exist = await get_marriage_rel_id(interaction.guild.id, interaction.user.id, membre.id)
            if rid_exist:
                await interaction.followup.send("❌ Vous êtes déjà mariés (dans ce bot).", ephemeral=True); return
            resume = "Wallet partagé: Oui • Rappel: 1 seul mariage par personne."
            contrat_id = await creer_contrat_mariage(interaction.guild.id, interaction.user.id, membre.id, resume)
            e = E("🔗 Demande de mariage",
                  f"{interaction.user.mention} propose **mariage** à {membre.mention}.\n"
                  f"📄 **Contrat**: `{contrat_id}`\n{resume}")
            await interaction.followup.send(embed=e, view=VueRelation("marriage", interaction.user.id, membre.id), ephemeral=True)
        else:
            e = E("🔗 Demande de relation",
                  f"{interaction.user.mention} propose **{type}** à {membre.mention}.\n(Aucun contrat requis)")
            await interaction.followup.send(embed=e, view=VueRelation(rtype, interaction.user.id, membre.id), ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Erreur: {e}", ephemeral=True)
        traceback.print_exc()

@tree.command(name="famille_creer", description="Créer une famille (multi-membres, avec wallet)", guilds=TARGET_GUILDS)
async def famille_creer(interaction: discord.Interaction, nom: str):
    await ack(interaction)
    try:
        rid = await create_relation(interaction.guild.id, "family", [interaction.user.id], with_wallet=True, name=nom)
        await interaction.followup.send(f"👪 Famille **{nom}** créée (id=`{rid}`) avec wallet partagé.", ephemeral=True)
        await log_line(interaction.guild, f"👪 Famille créée `{rid}` par {interaction.user.mention}")
    except Exception as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)

@tree.command(name="famille_inviter", description="Inviter quelqu'un dans une famille", guilds=TARGET_GUILDS)
async def famille_inviter(interaction: discord.Interaction, relation_id: str, membre: discord.Member):
    await ack(interaction)
    try:
        e = E("👪 Invitation famille", f"{interaction.user.mention} invite {membre.mention} à rejoindre `{relation_id}`.")
        v = discord.ui.View(timeout=240)
        btn_ok = discord.ui.Button(label="👪 Rejoindre", style=discord.ButtonStyle.success)
        btn_ref = discord.ui.Button(label="❌ Refuser", style=discord.ButtonStyle.secondary)

        async def join_callback(inter: discord.Interaction):
            if inter.user.id != membre.id:
                await inter.response.send_message("❌ Seule la personne invitée peut répondre.", ephemeral=True); return
            try:
                await add_member_to_family(relation_id, membre.id)
                await inter.response.edit_message(content=f"✅ {membre.mention} a rejoint `{relation_id}`.", view=None)
                await log_line(inter.guild, f"👪 {membre.mention} a rejoint `{relation_id}`")
            except Exception as ex:
                await inter.response.send_message(f"⚠️ Impossible: {ex}", ephemeral=True)

        async def refuse_callback(inter: discord.Interaction):
            if inter.user.id != membre.id:
                await inter.response.send_message("❌ Seule la personne invitée peut répondre.", ephemeral=True); return
            await inter.response.edit_message(content="🙅 Invitation refusée.", view=None)

        btn_ok.callback = join_callback
        btn_ref.callback = refuse_callback
        v.add_item(btn_ok); v.add_item(btn_ref)
        await interaction.followup.send(embed=e, view=v, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)

# Groupes (guild-only)
groupe_kin = app_commands.Group(name="lien_parente", description="Liens de parenté", guild_ids=GUILD_IDS)

@groupe_kin.command(name="ajouter_parent", description="Définir un parent pour un enfant (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def ajouter_parent(interaction: discord.Interaction, enfant: discord.Member, parent: discord.Member):
    await ack(interaction)
    async with await db() as conn:
        await conn.execute("INSERT OR IGNORE INTO kin_edges(parent_id, child_id) VALUES (?,?)", (parent.id, enfant.id))
        await conn.commit()
    await interaction.followup.send(f"✅ Parent ajouté: {parent.mention} → {enfant.mention}", ephemeral=True)

@groupe_kin.command(name="retirer_parent", description="Retirer un lien parent→enfant (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def retirer_parent(interaction: discord.Interaction, enfant: discord.Member, parent: discord.Member):
    await ack(interaction)
    async with await db() as conn:
        await conn.execute("DELETE FROM kin_edges WHERE parent_id=? AND child_id=?", (parent.id, enfant.id))
        await conn.commit()
    await interaction.followup.send(f"🗑️ Lien retiré: {parent.mention} → {enfant.mention}", ephemeral=True)

@groupe_kin.command(name="lister", description="Lister les parents et enfants d'un membre")
async def lister_parente(interaction: discord.Interaction, user: discord.Member):
    await ack(interaction)
    async with await db() as conn:
        parents = await (await conn.execute("SELECT parent_id FROM kin_edges WHERE child_id=?", (user.id,))).fetchall()
        enfants = await (await conn.execute("SELECT child_id FROM kin_edges WHERE parent_id=?", (user.id,))).fetchall()
    g = interaction.guild
    ptxt = ", ".join([ (g.get_member(int(r["parent_id"])).mention if g.get_member(int(r["parent_id"])) else f"`{r['parent_id']}`") for r in parents]) or "—"
    ctxt = ", ".join([ (g.get_member(int(r["child_id"])).mention if g.get_member(int(r["child_id"])) else f"`{r['child_id']}`") for r in enfants]) or "—"
    await interaction.followup.send(f"👨‍👩‍👧 **Parents**: {ptxt}\n👶 **Enfants**: {ctxt}", ephemeral=True)

groupe_reglages = app_commands.Group(name="reglages_aff", description="Réglages du bot d'affiliation", guild_ids=GUILD_IDS)

@groupe_reglages.command(name="definir_theme", description="Définir le thème de l'arbre")
@app_commands.describe(theme="kawaii|sakura|royal|neon|arabesque")
@app_commands.checks.has_permissions(administrator=True)
async def definir_theme(interaction: discord.Interaction, theme: str):
    await ack(interaction)
    if theme not in THEMES:
        await interaction.followup.send("Thèmes valides: " + ", ".join(THEMES.keys()), ephemeral=True); return
    await set_setting(interaction.guild.id, "theme", theme)
    await interaction.followup.send(f"🎨 Thème défini: **{theme}**", ephemeral=True)

@groupe_reglages.command(name="definir_rtl", description="Activer le mode droite→gauche (RTL)")
@app_commands.checks.has_permissions(administrator=True)
async def definir_rtl(interaction: discord.Interaction, rtl: bool):
    await ack(interaction)
    await set_setting(interaction.guild.id, "rtl", 1 if rtl else 0)
    await interaction.followup.send(f"↔️ RTL: **{'on' if rtl else 'off'}**", ephemeral=True)

@groupe_reglages.command(name="definir_avatars", description="Montrer/masquer les avatars dans l'arbre")
@app_commands.checks.has_permissions(administrator=True)
async def definir_avatars(interaction: discord.Interaction, avatars: bool):
    await ack(interaction)
    await set_setting(interaction.guild.id, "avatars", 1 if avatars else 0)
    await interaction.followup.send(f"🖼️ Avatars: **{'on' if avatars else 'off'}**", ephemeral=True)

@groupe_reglages.command(name="definir_salon_logs", description="Choisir le salon pour les logs")
@app_commands.checks.has_permissions(administrator=True)
async def definir_salon_logs(interaction: discord.Interaction, salon: discord.TextChannel):
    await ack(interaction)
    await set_setting(interaction.guild.id, "log_chan", int(salon.id))
    await interaction.followup.send(f"🪵 Logs → {salon.mention}", ephemeral=True)

groupe_owner = app_commands.Group(name="proprietaires", description="Contrôle propriétaire", guild_ids=GUILD_IDS)

@groupe_owner.command(name="ajouter", description="Ajouter un propriétaire")
@owner_check()
async def owner_ajouter(interaction: discord.Interaction, user: discord.Member):
    await ack(interaction)
    async with await db() as conn:
        await conn.execute("INSERT OR IGNORE INTO owners(guild_id,user_id) VALUES (?,?)", (interaction.guild.id, user.id))
        await conn.commit()
    await interaction.followup.send(f"✅ {user.mention} est maintenant **propriétaire**.", ephemeral=True)

@groupe_owner.command(name="retirer", description="Retirer un propriétaire")
@owner_check()
async def owner_retirer(interaction: discord.Interaction, user: discord.Member):
    await ack(interaction)
    async with await db() as conn:
        await conn.execute("DELETE FROM owners WHERE guild_id=? AND user_id=?", (interaction.guild.id, user.id))
        await conn.commit()
    await interaction.followup.send(f"🗑️ {user.mention} retiré des propriétaires.", ephemeral=True)

@groupe_owner.command(name="lister", description="Lister les propriétaires")
@owner_check()
async def owner_lister(interaction: discord.Interaction):
    await ack(interaction)
    async with await db() as conn:
        rows = await (await conn.execute("SELECT user_id FROM owners WHERE guild_id=?", (interaction.guild.id,))).fetchall()
    noms = []
    for r in rows:
        m = interaction.guild.get_member(int(r["user_id"]))
        noms.append(m.mention if m else f"`{r['user_id']}`")
    await interaction.followup.send("👑 Propriétaires: " + (", ".join(noms) or "—"), ephemeral=True)

@groupe_owner.command(name="sauvegarder_bdd", description="Télécharger une sauvegarde de la base")
@owner_check()
async def owner_backup(interaction: discord.Interaction):
    await ack(interaction)
    try:
        await interaction.followup.send(file=discord.File(DB_PATH, filename="affiliations.db"), ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)

@groupe_owner.command(name="definir_clef_api", description="Changer la clé API (X-Secret) à chaud")
@owner_check()
async def owner_set_secret(interaction: discord.Interaction, cle: str):
    await ack(interaction)
    global RUNTIME_SECRET
    RUNTIME_SECRET = cle
    async with await db() as conn:
        await conn.execute("INSERT OR REPLACE INTO global_kv(k,v) VALUES ('api_secret',?)", (cle,))
        await conn.commit()
    await interaction.followup.send("🔐 Clé API mise à jour (immédiat).", ephemeral=True)

@groupe_owner.command(name="stats", description="Statistiques des relations & wallets")
@owner_check()
async def owner_stats(interaction: discord.Interaction):
    await ack(interaction)
    async with await db() as conn:
        nb_rel = (await (await conn.execute("SELECT COUNT(*) c FROM relations")).fetchone())["c"]
        nb_fam = (await (await conn.execute("SELECT COUNT(*) c FROM relations WHERE rtype='family'")).fetchone())["c"]
        nb_mar = (await (await conn.execute("SELECT COUNT(*) c FROM relations WHERE rtype='marriage'")).fetchone())["c"]
        nb_wal = (await (await conn.execute("SELECT COUNT(*) c FROM wallets")).fetchone())["c"]
    await interaction.followup.send(f"📊 Relations: {nb_rel} (familles {nb_fam}, mariages {nb_mar}) • Wallets: {nb_wal}", ephemeral=True)

# Ajout des groupes UNE SEULE FOIS
tree.add_command(groupe_kin)
tree.add_command(groupe_reglages)
tree.add_command(groupe_owner)

# -------- Historique / Arbre / Divorce --------
@tree.command(name="arbre_famille", description="Générer une image UHQ de la famille", guilds=TARGET_GUILDS)
@app_commands.describe(relation_id="Nom ou ID de la famille", theme="kawaii|sakura|royal|neon|arabesque", rtl="Mode droite→gauche", avatars="Montrer les avatars", res="1..3", public="Poster publiquement")
async def arbre_famille(interaction: discord.Interaction, relation_id: str, theme: str = None, rtl: bool = None, avatars: bool = None, res: int = 1, public: bool = False):
    await ack(interaction, ephemeral=not public)
    try:
        rid = await resolve_family_rel_id(interaction.guild.id, relation_id) or relation_id
        sett = await get_settings(interaction.guild.id)
        theme_name = (theme or sett.get("theme") or "kawaii")
        rtl_flag   = bool(sett.get("rtl")) if rtl is None else rtl
        show_av    = bool(sett.get("avatars",1)) if avatars is None else avatars
        res = max(1, min(3, int(res)))
        png = await render_family_tree_png(interaction.guild, rid, theme_name=theme_name, rtl=rtl_flag, show_avatars=show_av, res=res)
        file = discord.File(io.BytesIO(png), filename=f"arbre_{rid}_{theme_name}@{res}x.png")
        await interaction.followup.send(file=file, ephemeral=not public)
    except Exception as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)

@tree.command(name="proposer_divorce", description="Proposer un divorce (contrat + split wallet + pénalité coins)", guilds=TARGET_GUILDS)
@app_commands.describe(
    partenaire="La personne à divorcer (vous devez être mariés)",
    split_mode="egal|pourcentage",
    percent_pour_toi="Si 'pourcentage' : % du wallet pour toi (0..100)",
    penalite_coins="Pénalité en coins (A→B) si acceptée (0 pour aucune)",
    payeur_cest_moi="Si pénalité > 0: True=toi paies, False=partenaire",
    expire_minutes="Durée de validité du contrat (min)"
)
async def proposer_divorce(
    interaction: discord.Interaction,
    partenaire: discord.Member,
    split_mode: str = "egal",
    percent_pour_toi: int = 50,
    penalite_coins: int = 0,
    payeur_cest_moi: bool = True,
    expire_minutes: int = 60
):
    await ack(interaction)
    try:
        mode_int = {"egal":"equal","pourcentage":"percent"}.get(split_mode)
        if mode_int is None:
            await interaction.followup.send("split_mode doit être 'egal' ou 'pourcentage'.", ephemeral=True); return
        if mode_int=="percent" and not (0 <= percent_pour_toi <= 100):
            await interaction.followup.send("percent_pour_toi doit être entre 0 et 100.", ephemeral=True); return
        if interaction.user.id == partenaire.id:
            await interaction.followup.send("… tu ne peux pas divorcer de toi-même 😅", ephemeral=True); return
        rid = await get_marriage_rel_id(interaction.guild.id, interaction.user.id, partenaire.id)
        if not rid:
            await interaction.followup.send("❌ Vous n'êtes pas mariés (dans ce bot).", ephemeral=True); return
        a_id = interaction.user.id
        b_id = partenaire.id
        percent_for_a = percent_pour_toi if mode_int=="percent" else 50
        penalty_from = a_id if payeur_cest_moi else b_id
        penalty_to   = b_id if payeur_cest_moi else a_id
        cid = await creer_contrat_divorce(interaction.guild.id, a_id, b_id, mode_int, percent_for_a, penalty_from, penalty_to, int(max(0,penalite_coins)), int(max(5,expire_minutes)))

        desc = [f"**Divorce proposé par {interaction.user.mention}** avec {partenaire.mention}",
                f"- Relation: `mariage` → `{rid}`",
                f"- Répartition wallet: `{split_mode}`" + (f" — **{percent_for_a}%** pour {interaction.user.mention}" if mode_int=='percent' else ""),
                f"- Pénalité coins: **{int(max(0,penalite_coins))}** de {'toi' if payeur_cest_moi else 'lui/elle'} vers {'lui/elle' if payeur_cest_moi else 'toi'}",
                f"- Expire dans **{int(max(5,expire_minutes))} min**",
                f"- Contrat: `{cid}`" ]
        e = E("💔 Contrat de divorce", "\n".join(desc))
        await interaction.followup.send(content=f"{interaction.user.mention} {partenaire.mention}", embed=e, view=VueDivorce(cid, a_id, b_id), ephemeral=True)
        await log_line(interaction.guild, f"📄 Nouveau contrat de divorce `{cid}` entre {interaction.user.mention} et {partenaire.mention}")
    except Exception as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)

@tree.command(name="contrat_historique_famille", description="Historique mariages/divorces liés aux membres d'une famille (nom ou ID)", guilds=TARGET_GUILDS)
@app_commands.describe(famille="Nom de la famille ou ID relation (family:...)")
async def contrat_historique_famille(interaction: discord.Interaction, famille: str):
    await ack(interaction)
    try:
        rel_id = await resolve_family_rel_id(interaction.guild.id, famille)
        if not rel_id:
            await interaction.followup.send("❌ Famille introuvable (nom ou ID).", ephemeral=True); return
        if not (await is_owner(interaction.guild.id, interaction.user)) and not (await user_in_relation(rel_id, interaction.user.id)):
            await interaction.followup.send("⛔ Tu dois être propriétaire du bot ou membre de cette famille.", ephemeral=True); return
        members = await family_members(rel_id)
        if not members:
            await interaction.followup.send("Cette famille n'a pas de membres.", ephemeral=True); return

        async with await db() as conn:
            ph = ",".join("?" for _ in members)
            rows_m = await (await conn.execute(
                f"""SELECT * FROM marriage_contracts
                    WHERE guild_id=? AND (a_id IN ({ph}) OR b_id IN ({ph}))
                    ORDER BY created_at DESC""",
                (interaction.guild.id, *members, *members)
            )).fetchall()
            rows_d = await (await conn.execute(
                f"""SELECT * FROM divorce_contracts
                    WHERE guild_id=? AND (a_id IN ({ph}) OR b_id IN ({ph}))
                    ORDER BY created_at DESC""",
                (interaction.guild.id, *members, *members)
            )).fetchall()

        def user_tag(uid:int)->str:
            m = interaction.guild.get_member(uid)
            return m.mention if m else f"`{uid}`"

        lines = []
        for r in rows_m:
            d = time.strftime("%d/%m/%Y %H:%M", time.localtime(int(r["created_at"])))
            lines.append(f"**{d}** — 💍 Mariage {user_tag(r['a_id'])} ❤ {user_tag(r['b_id'])} — statut **{r['status']}** — wallet:oui")
        for r in rows_d:
            d = time.strftime("%d/%m/%Y %H:%M", time.localtime(int(r["created_at"])))
            lines.append(f"**{d}** — 💔 Divorce {user_tag(r['a_id'])} & {user_tag(r['b_id'])} — statut **{r['status']}** — pénalité:{int(r['penalty_coins'] or 0)}")

        if not lines:
            await interaction.followup.send("Aucun contrat trouvé pour cette famille.", ephemeral=True); return

        chunks, cur = [], ""
        for line in lines[:200]:
            if len(cur) + len(line) + 1 > 1900:
                chunks.append(cur); cur = ""
            cur += line + "\n"
        if cur: chunks.append(cur)

        await interaction.followup.send(embed=E(f"Historique — {famille}", chunks[0][:4000]), ephemeral=True)
        for extra in chunks[1:]:
            await interaction.followup.send(embed=E("Suite", extra[:4000]), ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ {e}", ephemeral=True)

# ---------------- Bot lifecycle ----------------
@bot.event
async def on_ready():
    try:
        for g in bot.guilds:
            try:
                await tree.sync(guild=g)
            except Exception as eg:
                print("Sync guild error:", g.id, eg)
        print("Slash FR sync OK (guild-only)")
    except Exception as e:
        print("Sync error:", e)
    print(f"Bot connecté: {bot.user} — guilds: {[g.id for g in bot.guilds]}")
    await bot.change_presence(status=discord.Status.online, activity=discord.Game("Affiliations • /proposer_relation"))

# ---------------- API ----------------
app = FastAPI(title="Miri Affiliations ULTIME FR API")

async def _check_secret(request: Request):
    hdr = request.headers.get("X-Secret")
    if API_SHARED_SECRET and hdr == API_SHARED_SECRET:
        return True
    rs = await get_runtime_secret()
    if rs and hdr == rs:
        return True
    return False

@app.get(API_BASE + "/affiliations/{guild_id}/{user_id}")
async def api_affiliations(guild_id: int, user_id: int, request: Request):
    if not await _check_secret(request): raise HTTPException(status_code=401, detail="Bad secret")
    wallets = await list_user_wallets(user_id)
    return {"user_id": user_id, "wallets": [{"rel_id": rid, "wallet_id": wid, "type": rtype} for rid, wid, rtype in wallets]}

@app.get(API_BASE + "/relations/{guild_id}/{user_id}")
async def api_relations(guild_id: int, user_id: int, request: Request):
    if not await _check_secret(request): raise HTTPException(status_code=401, detail="Bad secret")
    out = []
    async with await db() as conn:
        rels = await (await conn.execute("""SELECT r.rel_id, r.rtype
                                            FROM relations r
                                            JOIN relation_members m ON r.rel_id=m.rel_id
                                            WHERE r.guild_id=? AND m.user_id=?""", (guild_id, user_id))).fetchall()
        for r in rels:
            rid, rtype = r["rel_id"], r["rtype"]
            peers = [int(x["user_id"]) for x in await (await conn.execute("SELECT user_id FROM relation_members WHERE rel_id=?", (rid,))).fetchall() if int(x["user_id"]) != user_id]
            out.append({"rel_id": rid, "type": rtype, "peers": peers})
    return {"user_id": user_id, "relations": out}

@app.post(API_BASE + "/casino/spend")
async def api_casino_spend(request: Request):
    if not await _check_secret(request): raise HTTPException(status_code=401, detail="Bad secret")
    body = await request.json()
    user_id = int(body.get("user_id", 0))
    amount  = int(body.get("amount", 0))
    prefer  = (body.get("prefer_type") or "").lower() or None
    if user_id <= 0 or amount <= 0:
        raise HTTPException(400, "invalid payload")

    wallets = await list_user_wallets(user_id)
    choice = None
    order = ([prefer] if prefer else []) + ["marriage","family","friend","sibling"]
    for t in order:
        for rid, wid, rtype in wallets:
            if t and rtype == t:
                choice = (rid, wid); break
        if choice: break

    if choice:
        rid, wid = choice
        async with await db() as conn:
            row = await (await conn.execute("SELECT balance FROM wallets WHERE wallet_id=?", (wid,))).fetchone()
            bal = int(row["balance"]) if row else 0
            if bal >= amount:
                await conn.execute("UPDATE wallets SET balance=? WHERE wallet_id=?", (bal-amount, wid))
                await conn.commit()
                return {"ok": True, "source": f"shared:{rid}"}
            perso = await epic_get_balance(user_id)
            if perso + bal >= amount:
                need = amount - bal
                if bal>0:
                    await conn.execute("UPDATE wallets SET balance=0 WHERE wallet_id=?", (wid,))
                    await conn.commit()
                if await epic_spend(user_id, need):
                    return {"ok": True, "source": f"mixed:{rid}"}
                raise HTTPException(402, "insufficient funds")
    if await epic_spend(user_id, amount):
        return {"ok": True, "source": "personal"}
    raise HTTPException(402, "insufficient funds")

@app.get(API_BASE + "/family/{relation_id}/tree.png")
async def api_arbre_png(relation_id: str, request: Request, theme: str="kawaii", rtl:int=0, avatars:int=1, res:int=1):
    if not await _check_secret(request): raise HTTPException(status_code=401, detail="Bad secret")
    gid = None
    async with await db() as conn:
        row = await (await conn.execute("SELECT guild_id FROM relations WHERE rel_id=?", (relation_id,))).fetchone()
        gid = int(row["guild_id"]) if row else None
    guild = bot.get_guild(gid) if gid else None
    png = await render_family_tree_png(guild, relation_id, theme_name=theme, rtl=bool(int(rtl)), show_avatars=bool(int(avatars)), res=max(1,min(3,int(res))))
    return Response(content=png, media_type="image/png")

@app.get(API_BASE + "/health")
async def api_health():
    return {"ok": True, "time": int(time.time())}

# ---------------- Main ----------------
async def main():
    if not DISCORD_TOKEN: raise RuntimeError("DISCORD_TOKEN manquant")
    await init_db()
    server = uvicorn.Server(uvicorn.Config(app, host=API_HOST, port=API_PORT, loop="asyncio", log_level="info"))
    await asyncio.gather(bot.start(DISCORD_TOKEN), server.serve())

if __name__ == "__main__":
    asyncio.run(main())
