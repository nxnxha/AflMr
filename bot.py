import os, asyncio, io
import discord
from discord import app_commands
from dotenv import load_dotenv
import aiosqlite
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

DB_PATH = "./affiliations.db"
THEMES = {
    "kawaii": {"bg":(250,247,255), "line":(124,58,237)},
    "sakura": {"bg":(255,247,251), "line":(221,73,104)},
    "royal": {"bg":(245,246,252), "line":(66,90,188)},
    "neon": {"bg":(18,18,22), "line":(0,245,212)},
    "arabesque": {"bg":(248,246,240), "line":(189,119,26)}
}

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS mariages (id INTEGER PRIMARY KEY AUTOINCREMENT, user1 INTEGER, user2 INTEGER, since INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS familles (id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT, createur INTEGER, since INTEGER)")
        await db.commit()

async def create_image(theme: str, titre: str) -> discord.File:
    t = THEMES.get(theme, THEMES["kawaii"])
    img = Image.new("RGB", (500,300), t["bg"])
    d = ImageDraw.Draw(img)
    fnt = ImageFont.load_default()
    d.text((20,140), titre, fill=t["line"], font=fnt)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="arbre.png")

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Commande test /ping
@tree.command(name="ping", description="Tester si le bot répond", guild=discord.Object(id=GUILD_ID))
async def ping(inter: discord.Interaction):
    await inter.response.send_message("🏓 Pong ! Le bot est bien en ligne.")

@tree.command(name="proposer_mariage", description="Proposer un mariage", guild=discord.Object(id=GUILD_ID))
async def proposer_mariage(inter: discord.Interaction, membre: discord.Member):
    await inter.response.defer()
    if membre.id == inter.user.id:
        await inter.followup.send("❌ Tu ne peux pas te marier avec toi-même.")
        return
    await inter.followup.send(f"💍 {inter.user.mention} propose un mariage à {membre.mention}")

@tree.command(name="famille_creer", description="Créer une famille", guild=discord.Object(id=GUILD_ID))
async def famille_creer(inter: discord.Interaction, nom: str, theme: str="kawaii"):
    await inter.response.defer()
    theme = theme if theme in THEMES else "kawaii"
    file = await create_image(theme, f"Famille {nom}")
    await inter.followup.send(content=f"👪 Famille **{nom}** créée avec le thème **{theme}**", file=file)

@tree.command(name="arbre_famille", description="Afficher l'arbre de famille", guild=discord.Object(id=GUILD_ID))
async def arbre_famille(inter: discord.Interaction, nom: str, theme: str="kawaii"):
    await inter.response.defer()
    theme = theme if theme in THEMES else "kawaii"
    file = await create_image(theme, f"Arbre: {nom}")
    await inter.followup.send(file=file)

@bot.event
async def on_ready():
    await init_db()
    try:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        print("✅ Commandes slash synchronisées")
    except Exception as e:
        print("Erreur sync:", e)
    print(f"Bot prêt: {bot.user}")

if __name__ == "__main__":
    asyncio.run(bot.start(DISCORD_TOKEN))
