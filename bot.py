import os, io, time, asyncio, aiosqlite, aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

# ---------- Config ----------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID      = int(os.getenv("GUILD_ID", "0"))
EPIC_BASE_URL = os.getenv("EPIC_BASE_URL", "")
EPIC_API_KEY  = os.getenv("EPIC_API_KEY", "")
DB_PATH       = "./affiliations.db"

THEMES = {
    "kawaii": {"bg": (255, 230, 240), "line": (255, 100, 150)},
    "sakura": {"bg": (255, 240, 245), "line": (220, 120, 160)},
    "royal": {"bg": (230, 235, 250), "line": (100, 120, 200)},
}

# ---------- DB ----------
async def init_db():
    conn = await aiosqlite.connect(DB_PATH)
    await conn.execute("""CREATE TABLE IF NOT EXISTS contracts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        type TEXT,
        users TEXT,
        theme TEXT,
        created_at INTEGER
    )""")
    await conn.commit()
    await conn.close()

async def save_contract(guild_id:int, ctype:str, users:str, theme:str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO contracts(guild_id,type,users,theme,created_at) VALUES (?,?,?,?,?)",
            (guild_id, ctype, users, theme, int(time.time()))
        )
        await conn.commit()

async def get_theme(guild_id:int):
    async with aiosqlite.connect(DB_PATH) as conn:
        row = await (await conn.execute(
            "SELECT theme FROM contracts WHERE guild_id=? ORDER BY created_at DESC LIMIT 1",
            (guild_id,)
        )).fetchone()
        return row[0] if row else "kawaii"

# ---------- Epic API ----------
def epic_headers():
    return {"Authorization": f"Bearer {EPIC_API_KEY}"} if EPIC_API_KEY else {}

async def epic_add_wallet(user_ids:list):
    if not EPIC_BASE_URL: return None
    url = f"{EPIC_BASE_URL}/wallets/create"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json={"members": user_ids}, headers=epic_headers()) as r:
            if r.status == 200:
                return await r.json()
    return None

async def epic_delete_wallet(wallet_id:str):
    if not EPIC_BASE_URL: return False
    url = f"{EPIC_BASE_URL}/wallets/{wallet_id}/delete"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=epic_headers()) as r:
            return r.status == 200

# ---------- Utils ----------
def make_contract_image(title:str, names:str, theme:str="kawaii"):
    t = THEMES.get(theme, THEMES["kawaii"])
    img = Image.new("RGB", (600, 400), t["bg"])
    d = ImageDraw.Draw(img)
    try:
        font_big = ImageFont.truetype("arial.ttf", 32)
        font_small = ImageFont.truetype("arial.ttf", 20)
    except:
        font_big = font_small = ImageFont.load_default()
    d.text((300, 80), title, font=font_big, fill=t["line"], anchor="mm")
    d.text((300, 200), names, font=font_small, fill=(60, 60, 60), anchor="mm")
    d.text((300, 300), time.strftime("%d/%m/%Y"), font=font_small, fill=(100, 100, 100), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

# ---------- Views ----------
class RelationView(discord.ui.View):
    def __init__(self, author, partenaire, rtype, theme):
        super().__init__(timeout=60)
        self.author = author
        self.partenaire = partenaire
        self.rtype = rtype
        self.theme = theme

    @discord.ui.button(label="✅ Accepter", style=discord.ButtonStyle.success)
    async def accepter(self, inter: discord.Interaction, button: discord.ui.Button):
        if inter.user.id != self.partenaire.id:
            await inter.response.send_message("❌ Seule la personne concernée peut répondre.", ephemeral=True)
            return
        names = f"{self.author.display_name} ❤️ {self.partenaire.display_name}"
        buf = make_contract_image(f"Contrat de {self.rtype.capitalize()}", names, self.theme)
        file = discord.File(buf, filename=f"{self.rtype}.png")
        await save_contract(inter.guild.id, self.rtype, f"{self.author.id},{self.partenaire.id}", self.theme)
        if self.rtype in ["mariage", "famille"]:
            await epic_add_wallet([self.author.id, self.partenaire.id])
        await inter.response.edit_message(content=f"🎉 {self.author.mention} et {self.partenaire.mention} sont liés par **{self.rtype}** !", attachments=[file], view=None)

    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger)
    async def refuser(self, inter: discord.Interaction, button: discord.ui.Button):
        if inter.user.id != self.partenaire.id:
            await inter.response.send_message("❌ Seule la personne concernée peut répondre.", ephemeral=True)
            return
        await inter.response.edit_message(content="🙅 Demande refusée.", view=None)

class DivorceView(discord.ui.View):
    def __init__(self, author, partenaire, theme):
        super().__init__(timeout=60)
        self.author = author
        self.partenaire = partenaire
        self.theme = theme

    @discord.ui.button(label="✍️ Signer le divorce", style=discord.ButtonStyle.danger)
    async def signer(self, inter: discord.Interaction, button: discord.ui.Button):
        if inter.user.id not in [self.author.id, self.partenaire.id]:
            await inter.response.send_message("❌ Tu n'es pas concerné.", ephemeral=True)
            return
        names = f"{self.author.display_name} 💔 {self.partenaire.display_name}"
        buf = make_contract_image("Contrat de Divorce", names, self.theme)
        file = discord.File(buf, filename="divorce.png")
        await save_contract(inter.guild.id, "divorce", f"{self.author.id},{self.partenaire.id}", self.theme)
        await inter.response.edit_message(content=f"💔 Divorce entre {self.author.mention} et {self.partenaire.mention}", attachments=[file], view=None)

# ---------- Bot ----------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await init_db()
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print("✅ Commandes slash synchronisées")
    except Exception as e:
        print("Erreur sync:", e)
    print(f"Bot connecté comme {bot.user}")

# ---------- Commands ----------
@bot.tree.command(name="proposer_mariage", description="Proposer un mariage", guild=discord.Object(id=GUILD_ID))
async def proposer_mariage(inter: discord.Interaction, partenaire: discord.Member):
    theme = await get_theme(inter.guild.id)
    await inter.response.send_message(
        f"{inter.user.mention} propose en mariage {partenaire.mention} 🎀",
        view=RelationView(inter.user, partenaire, "mariage", theme)
    )

@bot.tree.command(name="proposer_amitie", description="Proposer une amitié", guild=discord.Object(id=GUILD_ID))
async def proposer_amitie(inter: discord.Interaction, partenaire: discord.Member):
    theme = await get_theme(inter.guild.id)
    await inter.response.send_message(
        f"{inter.user.mention} propose une amitié à {partenaire.mention} 💕",
        view=RelationView(inter.user, partenaire, "amitié", theme)
    )

@bot.tree.command(name="proposer_frere_soeur", description="Proposer un lien frère/soeur", guild=discord.Object(id=GUILD_ID))
async def proposer_frere_soeur(inter: discord.Interaction, partenaire: discord.Member):
    theme = await get_theme(inter.guild.id)
    await inter.response.send_message(
        f"{inter.user.mention} propose un lien frère/soeur à {partenaire.mention} 👨‍👩‍👧",
        view=RelationView(inter.user, partenaire, "frère/soeur", theme)
    )

@bot.tree.command(name="creer_famille", description="Créer une famille", guild=discord.Object(id=GUILD_ID))
async def creer_famille(inter: discord.Interaction, nom: str, membre: discord.Member):
    theme = await get_theme(inter.guild.id)
    await inter.response.send_message(
        f"{inter.user.mention} invite {membre.mention} à rejoindre la famille **{nom}** 👪",
        view=RelationView(inter.user, membre, "famille", theme)
    )

@bot.tree.command(name="proposer_divorce", description="Proposer un divorce", guild=discord.Object(id=GUILD_ID))
async def proposer_divorce(inter: discord.Interaction, partenaire: discord.Member):
    theme = await get_theme(inter.guild.id)
    await inter.response.send_message(
        f"{inter.user.mention} propose le divorce avec {partenaire.mention} 💔",
        view=DivorceView(inter.user, partenaire, theme)
    )

@bot.tree.command(name="contrathistorique", description="Voir l'historique des contrats", guild=discord.Object(id=GUILD_ID))
async def contrat_historique(inter: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as conn:
        rows = await (await conn.execute("SELECT type,users,theme,created_at FROM contracts WHERE guild_id=? ORDER BY created_at DESC LIMIT 10", (inter.guild.id,))).fetchall()
    if not rows:
        await inter.response.send_message("Aucun contrat trouvé.", ephemeral=True)
        return
    desc = []
    for r in rows:
        users = r[1].split(",")
        noms = [inter.guild.get_member(int(u)).display_name if inter.guild.get_member(int(u)) else u for u in users]
        desc.append(f"**{r[0]}** — {', '.join(noms)} ({time.strftime('%d/%m/%Y', time.localtime(r[3]))})")
    e = discord.Embed(title="📜 Historique des contrats", description="\n".join(desc), color=0xFFC0CB)
    await inter.response.send_message(embed=e)

@bot.tree.command(name="themes", description="Voir les thèmes disponibles", guild=discord.Object(id=GUILD_ID))
async def themes_cmd(inter: discord.Interaction):
    await inter.response.send_message("🎨 Thèmes: " + ", ".join(THEMES.keys()), ephemeral=True)

@bot.tree.command(name="set_theme", description="Changer le thème global", guild=discord.Object(id=GUILD_ID))
async def set_theme_cmd(inter: discord.Interaction, theme:str):
    if theme not in THEMES:
        await inter.response.send_message("❌ Thème invalide", ephemeral=True)
        return
    await save_contract(inter.guild.id, "theme", str(inter.user.id), theme)
    await inter.response.send_message(f"🎨 Thème changé pour **{theme}**", ephemeral=True)

# ---------- Run ----------
async def main():
    await init_db()
    if not DISCORD_TOKEN:
        raise RuntimeError("❌ DISCORD_TOKEN manquant")
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
