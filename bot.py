import os
import io
import datetime
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DB_PATH = "discipline.db"

# ===== CONFIG À ADAPTER (RÔLES + SEUILS) =====
PROBATION_ROLE_NAME = "Probation"
TOURNAMENT_BAN_ROLE_NAME = "BanniTournoi"

PROBATION_THRESHOLD = -3
TOURNAMENT_BAN_THRESHOLD = -6

MIN_POINTS = 1
MAX_POINTS = 3

SANCTION_TYPES = {
    "ANTI_FAIRPLAY": "Anti-fairplay (insultes / toxicité)",
    "TRICHERIE": "Tricheries",
    "ABUSIF": "Comportements abusifs",
}

# ===== DISCORD SETUP =====
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== DB SCHEMA =====
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS seasons (
  guild_id INTEGER NOT NULL,
  season_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  is_active INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, season_id)
);

CREATE TABLE IF NOT EXISTS player_points (
  guild_id INTEGER NOT NULL,
  season_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  points INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (guild_id, season_id, user_id)
);

CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  season_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  moderator_id INTEGER NOT NULL,
  delta INTEGER NOT NULL,
  action_type TEXT NOT NULL,
  sanction_type TEXT,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  guild_id INTEGER PRIMARY KEY,
  history_locked INTEGER NOT NULL DEFAULT 1,
  staff_role_id INTEGER,
  probation_role_id INTEGER,
  tournament_ban_role_id INTEGER
);
"""

def utc_now() -> str:
    return datetime.datetime.utcnow().isoformat()

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

async def ensure_settings(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT guild_id FROM settings WHERE guild_id=?", (guild_id,))
        row = await cur.fetchone()
        if not row:
            await db.execute("INSERT INTO settings (guild_id, history_locked) VALUES (?,1)", (guild_id,))
            await db.commit()

async def get_settings(guild_id: int):
    await ensure_settings(guild_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT history_locked, staff_role_id, probation_role_id, tournament_ban_role_id "
            "FROM settings WHERE guild_id=?",
            (guild_id,)
        )
        row = await cur.fetchone()
        return {
            "history_locked": bool(row[0]),
            "staff_role_id": row[1],
            "probation_role_id": row[2],
            "tournament_ban_role_id": row[3],
        }

async def set_role_ids_from_names(guild: discord.Guild):
    settings = await get_settings(guild.id)

    def find_role_id(name: str):
        role = discord.utils.get(guild.roles, name=name)
        return role.id if role else None

    probation_id = settings["probation_role_id"] or find_role_id(PROBATION_ROLE_NAME)
    ban_id = settings["tournament_ban_role_id"] or find_role_id(TOURNAMENT_BAN_ROLE_NAME)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE settings SET probation_role_id=?, tournament_ban_role_id=? WHERE guild_id=?",
            (probation_id, ban_id, guild.id)
        )
        await db.commit()

async def get_active_season(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT season_id, name FROM seasons WHERE guild_id=? AND is_active=1",
            (guild_id,)
        )
        return await cur.fetchone()

async def ensure_active_season(guild_id: int):
    active = await get_active_season(guild_id)
    if active:
        return active
    now = utc_now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO seasons (guild_id, season_id, name, started_at, is_active) VALUES (?,?,?,?,1)",
            (guild_id, 1, "Saison 1", now)
        )
        await db.commit()
    return (1, "Saison 1")

async def start_new_season(guild_id: int, name: str):
    now = utc_now()
    active = await ensure_active_season(guild_id)
    active_id = active[0]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE seasons SET is_active=0, ended_at=? WHERE guild_id=? AND season_id=?",
            (now, guild_id, active_id)
        )
        cur = await db.execute(
            "SELECT COALESCE(MAX(season_id),0) + 1 FROM seasons WHERE guild_id=?",
            (guild_id,)
        )
        next_id = (await cur.fetchone())[0]
        await db.execute(
            "INSERT INTO seasons (guild_id, season_id, name, started_at, is_active) VALUES (?,?,?,?,1)",
            (guild_id, next_id, name, now)
        )
        await db.commit()
    return (next_id, name)

async def get_points(guild_id: int, season_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT points FROM player_points WHERE guild_id=? AND season_id=? AND user_id=?",
            (guild_id, season_id, user_id)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

async def apply_delta(guild_id: int, season_id: int, user_id: int, moderator_id: int,
                      delta: int, reason: str, action_type: str, sanction_type: str | None) -> int:
    now = utc_now()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT points FROM player_points WHERE guild_id=? AND season_id=? AND user_id=?",
            (guild_id, season_id, user_id)
        )
        row = await cur.fetchone()
        current = row[0] if row else 0
        new_points = current + delta

        if row:
            await db.execute(
                "UPDATE player_points SET points=?, updated_at=? WHERE guild_id=? AND season_id=? AND user_id=?",
                (new_points, now, guild_id, season_id, user_id)
            )
        else:
            await db.execute(
                "INSERT INTO player_points (guild_id, season_id, user_id, points, updated_at) VALUES (?,?,?,?,?)",
                (guild_id, season_id, user_id, new_points, now)
            )

        await db.execute(
            "INSERT INTO actions (guild_id, season_id, user_id, moderator_id, delta, action_type, sanction_type, reason, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (guild_id, season_id, user_id, moderator_id, delta, action_type, sanction_type, reason, now)
        )
        await db.commit()
        return new_points

async def clear_player(guild_id: int, season_id: int, user_id: int, moderator_id: int, reason: str) -> int:
    current = await get_points(guild_id, season_id, user_id)
    if current == 0:
        return 0
    return await apply_delta(guild_id, season_id, user_id, moderator_id, -current, reason, "CLEAR", None)

async def get_history_all(guild_id: int, season_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT moderator_id, delta, action_type, sanction_type, reason, created_at "
            "FROM actions WHERE guild_id=? AND season_id=? AND user_id=? ORDER BY id ASC",
            (guild_id, season_id, user_id)
        )
        return await cur.fetchall()

async def get_stats_by_type(guild_id: int, season_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT sanction_type, COUNT(*), SUM(ABS(delta)) "
            "FROM actions "
            "WHERE guild_id=? AND season_id=? AND user_id=? AND action_type='PENALTY' "
            "GROUP BY sanction_type",
            (guild_id, season_id, user_id)
        )
        return await cur.fetchall()

async def get_leaderboard(guild_id: int, season_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, points FROM player_points WHERE guild_id=? AND season_id=? "
            "ORDER BY points ASC LIMIT ?",
            (guild_id, season_id, limit)
        )
        return await cur.fetchall()

def is_staff(interaction: discord.Interaction, settings: dict) -> bool:
    if not interaction.user or not isinstance(interaction.user, discord.Member):
        return False
    member: discord.Member = interaction.user
    if settings.get("staff_role_id"):
        return any(r.id == settings["staff_role_id"] for r in member.roles)
    return member.guild_permissions.manage_guild

def require_staff():
    async def predicate(interaction: discord.Interaction):
        settings = await get_settings(interaction.guild_id)
        if is_staff(interaction, settings):
            return True
        raise app_commands.CheckFailure("Tu n'as pas la permission d'utiliser cette commande.")
    return app_commands.check(predicate)

async def apply_auto_sanctions(guild: discord.Guild, member: discord.Member, points: int):
    settings = await get_settings(guild.id)
    probation_role = guild.get_role(settings["probation_role_id"]) if settings["probation_role_id"] else None
    ban_role = guild.get_role(settings["tournament_ban_role_id"]) if settings["tournament_ban_role_id"] else None

    if ban_role:
        if points <= TOURNAMENT_BAN_THRESHOLD and ban_role not in member.roles:
            await member.add_roles(ban_role, reason="Auto-sanction: seuil atteint")
        if points > TOURNAMENT_BAN_THRESHOLD and ban_role in member.roles:
            await member.remove_roles(ban_role, reason="Auto-sanction: seuil remonté")

    if probation_role:
        if points <= PROBATION_THRESHOLD and probation_role not in member.roles:
            await member.add_roles(probation_role, reason="Auto-sanction: seuil atteint")
        if points > PROBATION_THRESHOLD and probation_role in member.roles:
            await member.remove_roles(probation_role, reason="Auto-sanction: seuil remonté")

class Discipline(app_commands.Group):
    def __init__(self):
        super().__init__(name="disc", description="Sanctions / bonne conduite (par saison)")

    @app_commands.command(name="season", description="Affiche la saison active.")
    async def season(self, interaction: discord.Interaction):
        season_id, name = await ensure_active_season(interaction.guild_id)
        await interaction.response.send_message(f"🏁 Saison active : **{name}** (ID: {season_id})")

    @app_commands.command(name="season_reset", description="Démarre une nouvelle saison (reset des points).")
    @require_staff()
    @app_commands.describe(name="Nom de la nouvelle saison")
    async def season_reset(self, interaction: discord.Interaction, name: str):
        season_id, season_name = await start_new_season(interaction.guild_id, name)
        await interaction.response.send_message(f"✅ Nouvelle saison : **{season_name}** (ID: {season_id}).")

    @app_commands.command(name="penalize", description="Sanction (-1 à -3) + type.")
    @require_staff()
    @app_commands.describe(user="Joueur", points="1 à 3", sanction_type="ANTI_FAIRPLAY/TRICHERIE/ABUSIF", reason="Raison")
    async def penalize(self, interaction: discord.Interaction, user: discord.Member, points: int, sanction_type: str, reason: str):
        sanction_type = sanction_type.upper().strip()
        if sanction_type not in SANCTION_TYPES:
            return await interaction.response.send_message(
                f"⚠️ Type invalide. Types: {', '.join(SANCTION_TYPES.keys())}",
                ephemeral=True
            )
        if not (MIN_POINTS <= points <= MAX_POINTS):
            return await interaction.response.send_message("⚠️ points doit être 1, 2 ou 3.", ephemeral=True)

        season_id, season_name = await ensure_active_season(interaction.guild_id)
        new_points = await apply_delta(
            interaction.guild_id, season_id, user.id, interaction.user.id,
            -points, reason, "PENALTY", sanction_type
        )
        await apply_auto_sanctions(interaction.guild, user, new_points)
        await interaction.response.send_message(
            f"✅ Sanction **{SANCTION_TYPES[sanction_type]}** sur {user.mention} : **-{points}**\n"
            f"Saison: **{season_name}** — Total: **{new_points}**\n📝 {reason}"
        )

    @app_commands.command(name="good", description="Bonne conduite (+1 à +3).")
    @require_staff()
    @app_commands.describe(user="Joueur", points="1 à 3", reason="Pourquoi")
    async def good(self, interaction: discord.Interaction, user: discord.Member, points: int, reason: str):
        if not (MIN_POINTS <= points <= MAX_POINTS):
            return await interaction.response.send_message("⚠️ points doit être 1, 2 ou 3.", ephemeral=True)

        season_id, season_name = await ensure_active_season(interaction.guild_id)
        new_points = await apply_delta(
            interaction.guild_id, season_id, user.id, interaction.user.id,
            +points, reason, "GOOD", None
        )
        await apply_auto_sanctions(interaction.guild, user, new_points)
        await interaction.response.send_message(
            f"✅ Bonne conduite sur {user.mention} : **+{points}**\n"
            f"Saison: **{season_name}** — Total: **{new_points}**\n📝 {reason}"
        )

    @app_commands.command(name="clear", description="Lève les sanctions (remet à 0 pour la saison).")
    @require_staff()
    @app_commands.describe(user="Joueur", reason="Motif")
    async def clear(self, interaction: discord.Interaction, user: discord.Member, reason: str = "Sanctions levées"):
        season_id, season_name = await ensure_active_season(interaction.guild_id)
        new_points = await clear_player(interaction.guild_id, season_id, user.id, interaction.user.id, reason)
        await apply_auto_sanctions(interaction.guild, user, new_points)
        await interaction.response.send_message(
            f"✅ Sanctions levées pour {user.mention} — Saison **{season_name}**\nTotal: **{new_points}**"
        )

    @app_commands.command(name="status", description="Points d'un joueur (saison active).")
    @app_commands.describe(user="Joueur")
    async def status(self, interaction: discord.Interaction, user: discord.Member):
        season_id, season_name = await ensure_active_season(interaction.guild_id)
        pts = await get_points(interaction.guild_id, season_id, user.id)
        embed = discord.Embed(
            title="Discipline — Saison active",
            description=f"Joueur: {user.mention}\nSaison: **{season_name}**\nPoints: **{pts}**"
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="history", description="Historique COMPLET (saison active).")
    @app_commands.describe(user="Joueur")
    async def history(self, interaction: discord.Interaction, user: discord.Member):
        settings = await get_settings(interaction.guild_id)
        if settings["history_locked"] and not is_staff(interaction, settings):
            return await interaction.response.send_message("🔒 Historique verrouillé (staff).", ephemeral=True)

        season_id, season_name = await ensure_active_season(interaction.guild_id)
        rows = await get_history_all(interaction.guild_id, season_id, user.id)
        if not rows:
            return await interaction.response.send_message("Aucune action enregistrée.", ephemeral=True)

        lines = []
        for mod_id, delta, action_type, sanc_type, reason, created_at in rows:
            date = created_at.replace("T", " ")[:16]
            sign = "+" if delta > 0 else ""
            tag = f"[{sanc_type}] " if (action_type == "PENALTY" and sanc_type) else ""
            lines.append(f"{date} {sign}{delta} {tag}par <@{mod_id}> — {reason}")

        text = "\n".join(lines)
        header = f"Historique — {user} — Saison: {season_name}\n\n"

        if len(header) + len(text) > 1800:
            f = discord.File(fp=io.BytesIO((header + text).encode("utf-8")), filename=f"history_{user.id}_S{season_id}.txt")
            return await interaction.response.send_message(content=f"📎 {user.mention} — historique complet (fichier)", file=f)

        embed = discord.Embed(title="Historique complet", description=header + text)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stats", description="Stats sanctions par type (staff).")
    async def stats(self, interaction: discord.Interaction, user: discord.Member):
        settings = await get_settings(interaction.guild_id)
        if not is_staff(interaction, settings):
            return await interaction.response.send_message("🔒 Stats verrouillées (staff).", ephemeral=True)

        season_id, season_name = await ensure_active_season(interaction.guild_id)
        pts = await get_points(interaction.guild_id, season_id, user.id)
        rows = await get_stats_by_type(interaction.guild_id, season_id, user.id)

        embed = discord.Embed(
            title="Stats discipline — Saison active",
            description=f"Joueur: {user.mention}\nSaison: **{season_name}**\nTotal: **{pts}**"
        )

        if not rows:
            embed.add_field(name="Sanctions", value="Aucune sanction.", inline=False)
        else:
            out = []
            for sanc_type, count, points_sum in rows:
                label = SANCTION_TYPES.get(sanc_type, sanc_type)
                out.append(f"• **{label}** — {count} sanction(s), **-{int(points_sum)}** points")
            embed.add_field(name="Répartition", value="\n".join(out), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="leaderboard", description="Classement des plus sanctionnés (staff).")
    @require_staff()
    @app_commands.describe(limit="Nombre (max 25)")
    async def leaderboard(self, interaction: discord.Interaction, limit: int = 10):
        limit = max(1, min(limit, 25))
        season_id, season_name = await ensure_active_season(interaction.guild_id)
        rows = await get_leaderboard(interaction.guild_id, season_id, limit=limit)
        if not rows:
            return await interaction.response.send_message("Aucune donnée.", ephemeral=True)

        lines = [f"Saison: **{season_name}**\n"]
        for i, (uid, pts) in enumerate(rows, start=1):
            lines.append(f"**{i}.** <@{uid}> — **{pts}**")

        embed = discord.Embed(title="Classement — plus sanctionnés", description="\n".join(lines))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="lock_history", description="Active/désactive le verrouillage historique (staff).")
    @require_staff()
    @app_commands.describe(enabled="True = staff-only")
    async def lock_history(self, interaction: discord.Interaction, enabled: bool):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE settings SET history_locked=? WHERE guild_id=?", (1 if enabled else 0, interaction.guild_id))
            await db.commit()
        await interaction.response.send_message(f"✅ Historique verrouillé: **{enabled}**", ephemeral=True)

disc_group = Discipline()
bot.tree.add_command(disc_group)

@bot.event
async def on_ready():
    await db_init()
    for g in bot.guilds:
        await ensure_settings(g.id)
        await set_role_ids_from_names(g)

    # Sync commandes (rapide si GUILD_ID fourni)
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()

    print(f"Connecté en tant que {bot.user}.")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN manquant. Mets-le en variable d'environnement.")

bot.run(TOKEN)
