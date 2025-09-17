import os, io, time, asyncio, aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DB_PATH = "./affiliations_simple.db"

# ---------------------- DB ----------------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS mariages (
  id TEXT PRIMARY KEY,
  user1 INTEGER,
  user2 INTEGER,
  since INTEGER
);
CREATE TABLE IF NOT EXISTS divorces (
  id TEXT PRIMARY KEY,
  user1 INTEGER,
  user2 INTEGER,
  since INTEGER
);
CREATE TABLE IF NOT EXISTS familles (
  id TEXT PRIMARY KEY,
  nom TEXT,
  chef_id INTEGER,
  since INTEGER
);
CREATE TABLE IF NOT EXISTS famille_membres (
  fam_id TEXT,
  user_id INTEGER,
  UNIQUE(fam_id, user_id)
);
"""

async def db():
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    return conn

async def init_db():
    conn = await db()
    for stmt in CREATE_SQL.strip().split(";\n"):
        if stmt.strip():
            await conn.execute(stmt)
    await conn.commit()
    await conn.close()

# ---------------------- Certificats ----------------------
def certif_base(titre, texte, couleur=(255, 200, 200)):
    img = Image.new("RGB", (640, 400), couleur)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except:
        font = ImageFont.load_default()
    draw.text((50, 80), titre, fill=(60, 60, 60), font=font)
    draw.text((50, 200), texte, fill=(20, 20, 20), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

def certif_mariage(user1, user2):
    return certif_base("💍 Certificat de Mariage", f"{user1} ❤ {user2}", couleur=(255, 230, 250))

def certif_divorce(user1, user2):
    return certif_base("💔 Contrat de Divorce", f"{user1} ❌ {user2}", couleur=(220, 220, 220))

def certif_adoption(parent, enfant):
    return certif_base("👶 Certificat d’Adoption", f"{parent} adopte {enfant}", couleur=(210, 245, 255))

def img_to_file(img, name="certif.png"):
    return discord.File(img, filename=name)

# ---------------------- Bot ----------------------
intents = discord.Intents.default()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------------------- Commandes ----------------------
@tree.command(name="marier", description="Proposer un mariage", guild=discord.Object(id=GUILD_ID))
async def marier(inter: discord.Interaction, membre: discord.Member):
    if membre.id == inter.user.id:
        await inter.response.send_message("😅 Tu ne peux pas te marier avec toi-même.", ephemeral=True)
        return

    view = discord.ui.View()

    async def accepter(i: discord.Interaction):
        if i.user.id != membre.id:
            await i.response.send_message("❌ Seule la personne concernée peut répondre.", ephemeral=True)
            return
        img = certif_mariage(inter.user.display_name, membre.display_name)
        file = img_to_file(img, "mariage.png")
        await i.response.send_message(
            content=f"🎉 {inter.user.mention} et {membre.mention} sont désormais mariés !",
            file=file
        )
        async with await db() as conn:
            mid = f"mar:{inter.user.id}:{membre.id}"
            await conn.execute("INSERT OR IGNORE INTO mariages(id,user1,user2,since) VALUES(?,?,?,?)",
                               (mid, inter.user.id, membre.id, int(time.time())))
            await conn.commit()

    async def refuser(i: discord.Interaction):
        if i.user.id == membre.id:
            await i.response.send_message("🙅 Demande refusée.", ephemeral=True)

    btn_ok = discord.ui.Button(label="✅ Oui", style=discord.ButtonStyle.success)
    btn_ref = discord.ui.Button(label="❌ Non", style=discord.ButtonStyle.danger)
    btn_ok.callback = accepter
    btn_ref.callback = refuser
    view.add_item(btn_ok)
    view.add_item(btn_ref)

    await inter.response.send_message(f"{membre.mention}, acceptes-tu d’épouser {inter.user.mention} ?", view=view)

@tree.command(name="divorcer", description="Proposer un divorce", guild=discord.Object(id=GUILD_ID))
async def divorcer(inter: discord.Interaction, membre: discord.Member):
    if membre.id == inter.user.id:
        await inter.response.send_message("❌ Pas possible.", ephemeral=True)
        return

    img = certif_divorce(inter.user.display_name, membre.display_name)
    file = img_to_file(img, "divorce.png")

    async with await db() as conn:
        did = f"div:{inter.user.id}:{membre.id}:{int(time.time())}"
        await conn.execute("INSERT INTO divorces(id,user1,user2,since) VALUES(?,?,?,?)",
                           (did, inter.user.id, membre.id, int(time.time())))
        await conn.commit()

    await inter.response.send_message(
        content=f"💔 {inter.user.mention} a demandé le divorce avec {membre.mention}.",
        file=file
    )

@tree.command(name="famille_creer", description="Créer une famille", guild=discord.Object(id=GUILD_ID))
async def famille_creer(inter: discord.Interaction, nom: str):
    async with await db() as conn:
        fid = f"fam:{inter.user.id}:{int(time.time())}"
        await conn.execute("INSERT INTO familles(id,nom,chef_id,since) VALUES(?,?,?,?)",
                           (fid, nom, inter.user.id, int(time.time())))
        await conn.execute("INSERT INTO famille_membres(fam_id,user_id) VALUES(?,?)", (fid, inter.user.id))
        await conn.commit()
    await inter.response.send_message(f"👪 Famille **{nom}** créée avec succès !")

@tree.command(name="adopter", description="Adopter un membre", guild=discord.Object(id=GUILD_ID))
async def adopter(inter: discord.Interaction, membre: discord.Member):
    if membre.id == inter.user.id:
        await inter.response.send_message("😅 Tu ne peux pas t’adopter toi-même.", ephemeral=True)
        return

    view = discord.ui.View()

    async def accepter(i: discord.Interaction):
        if i.user.id != membre.id:
            await i.response.send_message("❌ Seule la personne concernée peut répondre.", ephemeral=True)
            return
        img = certif_adoption(inter.user.display_name, membre.display_name)
        file = img_to_file(img, "adoption.png")
        await i.response.send_message(
            content=f"🎉 {inter.user.mention} a officiellement adopté {membre.mention} !",
            file=file
        )
        async with await db() as conn:
            fam_id = f"fam:{inter.user.id}"
            await conn.execute("INSERT OR IGNORE INTO familles(id,nom,chef_id,since) VALUES(?,?,?,?)",
                               (fam_id, f"Famille de {inter.user.display_name}", inter.user.id, int(time.time())))
            await conn.execute("INSERT OR IGNORE INTO famille_membres(fam_id,user_id) VALUES(?,?)", (fam_id, membre.id))
            await conn.commit()

    async def refuser(i: discord.Interaction):
        if i.user.id == membre.id:
            await i.response.send_message("🙅 Adoption refusée.", ephemeral=True)

    btn_ok = discord.ui.Button(label="✅ Oui", style=discord.ButtonStyle.success)
    btn_ref = discord.ui.Button(label="❌ Non", style=discord.ButtonStyle.danger)
    btn_ok.callback = accepter
    btn_ref.callback = refuser
    view.add_item(btn_ok)
    view.add_item(btn_ref)

    await inter.response.send_message(f"{membre.mention}, acceptes-tu d’être adopté(e) par {inter.user.mention} ?", view=view)

# ---------------------- Historique ----------------------
@tree.command(name="historique", description="Voir ton historique familial", guild=discord.Object(id=GUILD_ID))
async def historique(inter: discord.Interaction, membre: discord.Member = None):
    membre = membre or inter.user
    uid = membre.id
    out = []

    async with await db() as conn:
        mariages = await (await conn.execute("SELECT * FROM mariages WHERE user1=? OR user2=?", (uid, uid))).fetchall()
        divorces = await (await conn.execute("SELECT * FROM divorces WHERE user1=? OR user2=?", (uid, uid))).fetchall()
        familles = await (await conn.execute("SELECT f.nom FROM familles f JOIN famille_membres m ON f.id=m.fam_id WHERE m.user_id=?", (uid,))).fetchall()

    if mariages:
        for m in mariages:
            other = m["user2"] if m["user1"] == uid else m["user1"]
            out.append(f"💍 Marié avec <@{other}> (depuis <t:{m['since']}:D>)")

    if divorces:
        for d in divorces:
            other = d["user2"] if d["user1"] == uid else d["user1"]
            out.append(f"💔 Divorce avec <@{other}> (<t:{d['since']}:D>)")

    if familles:
        for f in familles:
            out.append(f"👪 Membre de la famille **{f['nom']}**")

    if not out:
        await inter.response.send_message(f"ℹ️ Aucun historique trouvé pour {membre.mention}.", ephemeral=True)
    else:
        await inter.response.send_message("\n".join(out), ephemeral=True)

# ---------------------- Events ----------------------
@bot.event
async def on_ready():
    await init_db()
    try:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        print("✅ Slash commands synchronisées")
    except Exception as e:
        print("Erreur sync:", e)
    print(f"Bot prêt : {bot.user}")

# ---------------------- Run ----------------------
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN manquant")
bot.run(DISCORD_TOKEN)
