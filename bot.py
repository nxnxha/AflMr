import os, asyncio, time, io, aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID      = int(os.getenv("GUILD_ID", "0"))

# DB sur Railway (stockage temporaire → persiste tant que le conteneur tourne)
DB_PATH = "/tmp/affiliations_simple.db"

# ---------- DB ----------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS mariages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user1 INTEGER,
    user2 INTEGER,
    date INTEGER
);
CREATE TABLE IF NOT EXISTS familles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT,
    createur INTEGER,
    date INTEGER
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(CREATE_SQL)
        await conn.commit()
    print("✅ DB initialisée")

# ---------- BOT ----------
intents = discord.Intents.default()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------- COMMANDES ----------
@tree.command(name="proposer_relation", description="Proposer un mariage")
async def proposer_relation(inter: discord.Interaction, membre: discord.Member):
    if membre.id == inter.user.id:
        await inter.response.send_message("😅 Tu ne peux pas te marier avec toi-même.", ephemeral=True)
        return

    # Vérifier si déjà marié
    async with aiosqlite.connect(DB_PATH) as conn:
        row = await (await conn.execute(
            "SELECT 1 FROM mariages WHERE user1=? OR user2=?", (inter.user.id, inter.user.id)
        )).fetchone()
        if row:
            await inter.response.send_message("❌ Tu es déjà marié.", ephemeral=True)
            return

    embed = discord.Embed(
        title="💍 Demande en mariage",
        description=f"{inter.user.mention} demande {membre.mention} en mariage !",
        color=0xFFC0CB
    )
    view = discord.ui.View()

    async def accepter_callback(i: discord.Interaction):
        if i.user.id != membre.id:
            await i.response.send_message("❌ Ce bouton n'est pas pour toi.", ephemeral=True)
            return
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO mariages (user1, user2, date) VALUES (?,?,?)",
                (inter.user.id, membre.id, int(time.time()))
            )
            await conn.commit()
        await i.response.edit_message(content="🎉 Félicitations ! Vous êtes mariés 💕", view=None)

    async def refuser_callback(i: discord.Interaction):
        if i.user.id != membre.id:
            await i.response.send_message("❌ Ce bouton n'est pas pour toi.", ephemeral=True)
            return
        await i.response.edit_message(content="💔 La demande a été refusée.", view=None)

    btn_ok = discord.ui.Button(label="✅ Accepter", style=discord.ButtonStyle.success)
    btn_no = discord.ui.Button(label="❌ Refuser", style=discord.ButtonStyle.danger)
    btn_ok.callback = accepter_callback
    btn_no.callback = refuser_callback
    view.add_item(btn_ok)
    view.add_item(btn_no)

    await inter.response.send_message(embed=embed, view=view)

@tree.command(name="famille_creer", description="Créer une nouvelle famille")
async def famille_creer(inter: discord.Interaction, nom: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO familles (nom, createur, date) VALUES (?,?,?)",
            (nom, inter.user.id, int(time.time()))
        )
        await conn.commit()
    await inter.response.send_message(f"👪 Famille **{nom}** créée avec succès !")

@tree.command(name="contrathistorique", description="Voir l'historique de tes relations")
async def contrathistorique(inter: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as conn:
        mariages = await (await conn.execute(
            "SELECT user1,user2,date FROM mariages WHERE user1=? OR user2=?",
            (inter.user.id, inter.user.id)
        )).fetchall()
        familles = await (await conn.execute(
            "SELECT nom,date FROM familles WHERE createur=?",
            (inter.user.id,)
        )).fetchall()

    desc = ""
    if mariages:
        for m in mariages:
            u1, u2, d = m
            partenaire = u2 if u1 == inter.user.id else u1
            partenaire_tag = f"<@{partenaire}>"
            desc += f"💍 Marié avec {partenaire_tag} depuis <t:{d}:D>\n"
    if familles:
        for f in familles:
            nom, d = f
            desc += f"👪 Famille **{nom}** créée le <t:{d}:D>\n"

    if not desc:
        desc = "Aucun contrat trouvé."
    await inter.response.send_message(desc, ephemeral=True)

# ---------- READY ----------
@bot.event
async def on_ready():
    await init_db()
    try:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"✅ Commandes slash synchronisées sur {GUILD_ID}")
    except Exception as e:
        print("⚠️ Erreur sync:", e)
    print(f"🤖 Connecté en tant que {bot.user}")

# ---------- MAIN ----------
async def main():
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
