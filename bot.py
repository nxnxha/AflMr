# bot.py — Arbre généalogique "cute anime" (FR, simple & robuste)

import os, io, time, asyncio, traceback
from typing import Optional, List, Dict, Tuple

import discord
from discord import app_commands
import aiohttp
import aiosqlite
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ============ CONFIG ============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # requis
GUILD_IDS = [int(x) for x in (os.getenv("GUILD_IDS") or os.getenv("GUILD_ID","")).replace(" ","").split(",") if x.strip().isdigit()]
LOGS_DEFAULT_CHAN_ID = int(os.getenv("LOGS_DEFAULT_CHAN_ID", "1417304969333440553"))
DB_PATH = os.getenv("DB_PATH", "./affiliations.db")
BRAND_COLOR = 0xFF69B4  # rose cute

# Thèmes (inclut "anime")
THEMES = {
    "anime":     {"bg":(255,249,254), "primary":(255,105,180), "line":(255,182,193), "card":(255,255,255)},  # cute / pastel
    "kawaii":    {"bg":(250,247,255), "primary":(124,58,237),  "line":(160,140,210), "card":(255,255,255)},
    "sakura":    {"bg":(255,247,251), "primary":(221,73,104),  "line":(239,170,184), "card":(255,255,255)},
    "royal":     {"bg":(245,246,252), "primary":(66,90,188),   "line":(120,140,210), "card":(255,255,255)},
    "neon":      {"bg":(18,18,22),    "primary":(0,245,212),   "line":(80,255,200),  "card":(36,36,44)},
    "arabesque": {"bg":(248,246,240), "primary":(189,119,26),  "line":(169,139,99),  "card":(255,255,252)},
}

# ============ DB ============
CREATE_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS relations (
  rel_id    TEXT PRIMARY KEY,
  guild_id  INTEGER,
  rtype     TEXT,      -- 'family'
  name      TEXT,
  since     INTEGER,
  wallet_id TEXT,
  theme     TEXT
);

CREATE TABLE IF NOT EXISTS relation_members (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  rel_id   TEXT,
  user_id  INTEGER,
  UNIQUE(rel_id, user_id)
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
"""

async def db():
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    return conn

async def init_db():
    async with await db() as conn:
        await conn.executescript(CREATE_SQL)
        await conn.commit()

# ============ UTILS ============
def E(title: str, desc: str) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=BRAND_COLOR)

async def get_settings(guild_id:int) -> Dict[str,int|str|None]:
    async with await db() as conn:
        row = await (await conn.execute("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,))).fetchone()
        if not row:
            return {"guild_id": guild_id, "theme": "anime", "rtl": 0, "avatars": 1, "log_chan": LOGS_DEFAULT_CHAN_ID}
        return dict(row)

async def set_setting(guild_id:int, key:str, value):
    async with await db() as conn:
        row = await (await conn.execute("SELECT 1 FROM guild_settings WHERE guild_id=?", (guild_id,))).fetchone()
        if row:
            await conn.execute(f"UPDATE guild_settings SET {key}=? WHERE guild_id=?", (value, guild_id))
        else:
            await conn.execute(
                "INSERT INTO guild_settings(guild_id,theme,rtl,avatars,log_chan) VALUES (?,?,?,?,?)",
                (guild_id,
                 value if key=="theme" else "anime",
                 int(value) if key=="rtl" else 0,
                 int(value) if key=="avatars" else 1,
                 int(value) if key=="log_chan" else LOGS_DEFAULT_CHAN_ID))
        await conn.commit()

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

def deterministic_rel_id(members: List[int]) -> str:
    base = ":".join(map(str, sorted(set(members))))
    return f"family:{int(time.time())}:{base[:6]}"

# ============ FAMILLES ============
async def create_family(guild_id:int, creator_id:int, name:str, theme:str="anime") -> str:
    theme = theme if theme in THEMES else "anime"
    rel_id = deterministic_rel_id([creator_id])
    async with await db() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO relations(rel_id,guild_id,rtype,name,since,wallet_id,theme) VALUES (?,?,?,?,?,NULL,?)",
            (rel_id, guild_id, "family", name[:64], int(time.time()), theme)
        )
        await conn.execute("INSERT OR IGNORE INTO relation_members(rel_id,user_id) VALUES (?,?)", (rel_id, creator_id))
        await conn.commit()
    return rel_id

async def resolve_family_rel_id(guild_id:int, key:str) -> Optional[str]:
    key = (key or "").strip()
    async with await db() as conn:
        row = await (await conn.execute(
            "SELECT rel_id FROM relations WHERE guild_id=? AND rtype='family' AND (rel_id=? OR LOWER(name)=LOWER(?)) LIMIT 1",
            (guild_id, key, key)
        )).fetchone()
    return row["rel_id"] if row else None

async def get_user_family_rel_id(guild_id:int, user_id:int) -> Optional[str]:
    async with await db() as conn:
        row = await (await conn.execute("""
            SELECT r.rel_id, COUNT(m2.user_id) AS n
            FROM relations r
            JOIN relation_members m ON r.rel_id = m.rel_id AND m.user_id = ?
            LEFT JOIN relation_members m2 ON r.rel_id = m2.rel_id
            WHERE r.guild_id = ? AND r.rtype = 'family'
            GROUP BY r.rel_id
            ORDER BY n DESC, r.since ASC
            LIMIT 1
        """, (user_id, guild_id))).fetchone()
    return row["rel_id"] if row else None

async def ac_familles(inter: discord.Interaction, current: str):
    try:
        async with await db() as conn:
            rows = await (await conn.execute("""
                SELECT rel_id, COALESCE(NULLIF(name,''), rel_id) AS label
                FROM relations
                WHERE guild_id = ? AND rtype='family'
                  AND (name LIKE ? OR rel_id LIKE ? OR ?='')
                ORDER BY since DESC
                LIMIT 20
            """, (inter.guild.id, f"%{current}%", f"%{current}%", current))).fetchall()
        return [app_commands.Choice(name=str(r["label"])[:100], value=r["rel_id"]) for r in rows]
    except Exception:
        return []

# ============ PARENTÉS ============
async def add_parent(child_id:int, parent_id:int):
    async with await db() as conn:
        await conn.execute("INSERT OR IGNORE INTO kin_edges(parent_id, child_id) VALUES (?,?)", (parent_id, child_id))
        await conn.commit()

async def remove_parent(child_id:int, parent_id:int):
    async with await db() as conn:
        await conn.execute("DELETE FROM kin_edges WHERE parent_id=? AND child_id=?", (parent_id, child_id))
        await conn.commit()

# ============ RENDU ARBRE ============
def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont):
    try:
        x0,y0,x1,y1 = draw.textbbox((0,0), text, font=font)
        return (x1-x0, y1-y0)
    except Exception:
        return draw.textsize(text, font=font)

async def _fetch(url: str) -> Optional[bytes]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=6) as r:
                if r.status != 200: return None
                return await r.read()
    except Exception:
        return None

def _circle(img: Image.Image, size: int) -> Image.Image:
    img = img.resize((size,size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L",(size,size),0)
    d=ImageDraw.Draw(mask); d.ellipse((0,0,size,size), fill=255)
    out = Image.new("RGBA",(size,size))
    out.paste(img,(0,0),mask)
    return out

def _quad(p0,p1,p2,steps=32):
    pts=[]
    for i in range(steps+1):
        t=i/steps
        x=(1-t)**2*p0[0] + 2*(1-t)*t*p1[0] + t**2*p2[0]
        y=(1-t)**2*p0[1] + 2*(1-t)*t*p1[1] + t**2*p2[1]
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
    by_level: Dict[int, List[int]] = {}
    max_level = 0
    for u,d in depth_cache.items():
        by_level.setdefault(d, []).append(u)
        max_level = max(max_level, d)
    for d in by_level: by_level[d].sort()

    margin_x, margin_y = 120, 140
    cell_w, cell_h = 320, 220
    card_w, card_h = 260, 108
    av_size = 64 if show_avatars else 0

    max_cols = max(len(v) for v in by_level.values()) if by_level else 1
    base_w  = margin_x*2 + max_cols*cell_w
    base_h  = margin_y*2 + (max_level+1)*cell_h
    width   = max(860, base_w) * res
    height  = max(560, base_h) * res

    bg = Image.new("RGB",(width,height), theme["bg"])
    draw = ImageDraw.Draw(bg)
    try:
        font_title = ImageFont.truetype("arial.ttf", 24*res)
        font_name  = ImageFont.truetype("arial.ttf", 19*res)
    except Exception:
        font_title = ImageFont.load_default(); font_name = ImageFont.load_default()

    title = f"Arbre généalogique — {fam_name}"
    tw,_ = _measure(draw, title, font_title)
    draw.text(((width-tw)//2, 24*res), title, fill=(60,60,90), font=font_title)

    positions: Dict[int,Tuple[int,int]] = {}
    for lvl in range(max_level+1):
        row = by_level.get(lvl, [])
        for i, uid in enumerate(row):
            cx = (margin_x + i*cell_w + cell_w//2) * res
            if rtl: cx = width - cx
            cy = (margin_y + lvl*cell_h + cell_h//2) * res
            positions[uid] = (cx,cy)

    for (p,c) in edges:
        if p not in positions or c not in positions: continue
        px,py = positions[p]; cx,cy = positions[c]
        ctrl=((px+cx)//2, (py+cy)//2 - 60*res)
        pts=_quad((px,py+card_h//2*res), ctrl, (cx,cy-card_h//2*res), steps=36)
        draw.line(pts, fill=theme["line"], width=4*res)

    async def card(uid:int):
        cx,cy=positions[uid]
        x0 = cx - card_w//2*res; y0 = cy - card_h//2*res
        x1 = cx + card_w//2*res; y1 = cy + card_h//2*res
        sh = Image.new("RGBA", (int(card_w*res+18*res), int(card_h*res+18*res)), (0,0,0,0))
        d2 = ImageDraw.Draw(sh)
        d2.rounded_rectangle((9*res,9*res, card_w*res+9*res, card_h*res+9*res), radius=22*res, fill=(0,0,0,85))
        sh = sh.filter(ImageFilter.GaussianBlur(8*res))
        bg.alpha_composite(sh, (int(x0-9*res), int(y0-9*res)))
        draw.rounded_rectangle([x0,y0,x1,y1], radius=22*res, outline=theme["primary"], width=3*res, fill=theme["card"])

        name = str(uid)
        if guild:
            m = guild.get_member(uid)
            if m:
                name = m.display_name
                if show_avatars:
                    ab = await _fetch(m.display_avatar.url)
                    if ab:
                        try:
                            im = Image.open(io.BytesIO(ab)).convert("RGB")
                            av = _circle(im, av_size*res)
                            bg.paste(av, (int(x0+14*res), int(y0+(card_h*res-av_size*res)//2)), av)
                        except Exception:
                            pass
        if len(name) > 24: name = name[:23]+"…"
        tx = x0 + 14*res + (av_size*res+12*res if show_avatars else 16*res)
        ty = y0 + 18*res
        draw.text((tx,ty), name, fill=(30,30,40), font=font_name)

    for uid in positions: await card(uid)

    b=io.BytesIO()
    bg.save(b, format="PNG", optimize=True)
    return b.getvalue()

# ============ DISCORD ============
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ---- commandes de base
@tree.command(name="ping", description="Test de présence (répond tout de suite).")
async def ping(inter: discord.Interaction):
    await reply(inter, content="pong ✅", ephemeral=True)

@tree.command(name="famille_creer", description="Créer une famille (avec thème cute).")
@app_commands.describe(nom="Nom de la famille", theme="Thème (anime/kawaii/sakura/royal/neon/arabesque)")
async def famille_creer_cmd(inter: discord.Interaction, nom: str, theme: str = "anime"):
    try:
        if theme not in THEMES:
            await reply(inter, content="Thèmes valides : " + ", ".join(THEMES.keys()), ephemeral=True); return
        rid = await create_family(inter.guild.id, inter.user.id, nom, theme)
        await reply(inter, content=f"👪 Famille **{nom}** créée (id=`{rid}`) • thème **{theme}** ✨", ephemeral=True)
        await log_line(inter.guild, f"👪 Famille `{rid}` (thème {theme}) par {inter.user.mention}")
    except Exception as e:
        await reply(inter, content=f"⚠️ {e}", ephemeral=True)

@tree.command(name="arbre", description="Affiche l'arbre généalogique (auto : ta famille).")
@app_commands.describe(
    famille="Choisir une famille (autocomplétion)",
    personne="Afficher la famille de cette personne",
    public="Poster publiquement (sinon en privé)"
)
@app_commands.autocomplete(famille=ac_familles)
async def arbre_cmd(inter: discord.Interaction,
                    famille: Optional[str] = None,
                    personne: Optional[discord.Member] = None,
                    public: bool = False):
    try:
        if famille:
            rel_id = famille
        elif personne:
            rel_id = await get_user_family_rel_id(inter.guild.id, personne.id)
        else:
            rel_id = await get_user_family_rel_id(inter.guild.id, inter.user.id)

        if not rel_id:
            cible = personne.mention if personne else "toi"
            await reply(inter, content=f"❌ Aucune famille trouvée pour {cible}. Utilise `/famille_creer` d’abord.", ephemeral=True)
            return

        sett = await get_settings(inter.guild.id)
        theme_default = sett.get("theme") or "anime"
        rtl   = bool(sett.get("rtl", 0))
        show  = bool(sett.get("avatars", 1))

        if not inter.response.is_done():
            await inter.response.defer(ephemeral=not public)

        png = await render_tree_png(inter.guild, rel_id, rtl=rtl, show_avatars=show, res=1, fallback_theme=theme_default)
        file = discord.File(io.BytesIO(png), filename=f"arbre_{rel_id}.png")
        await inter.followup.send(file=file, ephemeral=not public)
    except Exception as e:
        await reply(inter, content=f"⚠️ Erreur: {e}", ephemeral=True)
        traceback.print_exc()

# groupe parenté
g_kin = app_commands.Group(name="lien_parente", description="Gérer parent/enfant")

@g_kin.command(name="ajouter_parent", description="Définir un parent pour un enfant (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def ajouter_parent_cmd(inter: discord.Interaction, enfant: discord.Member, parent: discord.Member):
    await add_parent(enfant.id, parent.id)
    await reply(inter, content=f"✅ Parent ajouté: {parent.mention} → {enfant.mention}", ephemeral=True)

@g_kin.command(name="retirer_parent", description="Retirer un lien parent→enfant (admin)")
@app_commands.checks.has_permissions(administrator=True)
async def retirer_parent_cmd(inter: discord.Interaction, enfant: discord.Member, parent: discord.Member):
    await remove_parent(enfant.id, parent.id)
    await reply(inter, content=f"🗑️ Lien retiré: {parent.mention} → {enfant.mention}", ephemeral=True)

@g_kin.command(name="lister", description="Lister les liens d'un membre")
async def lister_parente_cmd(inter: discord.Interaction, user: discord.Member):
    async with await db() as conn:
        parents = await (await conn.execute("SELECT parent_id FROM kin_edges WHERE child_id=?", (user.id,))).fetchall()
        enfants = await (await conn.execute("SELECT child_id FROM kin_edges WHERE parent_id=?", (user.id,))).fetchall()
    g = inter.guild
    ptxt = ", ".join([ (g.get_member(int(r["parent_id"])).mention if g.get_member(int(r["parent_id"])) else f"`{r['parent_id']}`") for r in parents]) or "—"
    ctxt = ", ".join([ (g.get_member(int(r["child_id"])).mention if g.get_member(int(r["child_id"])) else f"`{r['child_id']}`") for r in enfants]) or "—"
    await reply(inter, content=f"👨‍👩‍👧 **Parents**: {ptxt}\n👶 **Enfants**: {ctxt}", ephemeral=True)

tree.add_command(g_kin)

# ============ LIFECYCLE ============
@bot.event
async def on_ready():
    try:
        await init_db()
        # Sync global + push rapide en guilds présentes
        await tree.sync()
        for g in bot.guilds:
            await tree.sync(guild=g)
        print("Slash sync OK.")
    except Exception as e:
        print("Sync error:", e)
    print(f"Connecté en {bot.user} — guilds: {[g.id for g in bot.guilds]}")

@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        await tree.sync(guild=guild)
    except Exception as e:
        print("Sync on join error:", e)

async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN manquant")
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
