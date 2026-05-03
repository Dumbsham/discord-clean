"""
Discord Study Bot
=================
Features:
  - Study timer  (/study start | stop | status)
  - XP system    (/xp  or  /xp @user)
  - Leaderboard  (/leaderboard daily | weekly)

Anti-abuse:
  - Must be in a voice channel to start
  - Session auto-stops if you leave the voice channel
  - XP capped at 8 hours per session
"""

import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import time
import datetime
import os

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
XP_PER_MINUTE = 10
MAX_SESSION_HOURS = 8
DB_PATH = "study_bot.db"
# ─────────────────────────────────────────────────────────────────────────────


# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id     INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL,
                start_time  REAL NOT NULL,
                end_time    REAL,
                duration    REAL
            );

            CREATE TABLE IF NOT EXISTS xp (
                user_id           INTEGER NOT NULL,
                guild_id          INTEGER NOT NULL,
                total_xp          INTEGER DEFAULT 0,
                daily_xp          INTEGER DEFAULT 0,
                weekly_xp         INTEGER DEFAULT 0,
                last_reset_daily  TEXT DEFAULT '',
                last_reset_weekly TEXT DEFAULT '',
                PRIMARY KEY (user_id, guild_id)
            );
        """)
# ─────────────────────────────────────────────────────────────────────────────


# ── Helpers ───────────────────────────────────────────────────────────────────
def today_str():
    return datetime.date.today().isoformat()


def week_str():
    d = datetime.date.today()
    return f"{d.year}-W{d.isocalendar()[1]:02d}"


def ensure_user(conn, user_id, guild_id):
    conn.execute(
        "INSERT OR IGNORE INTO xp (user_id, guild_id) VALUES (?, ?)",
        (user_id, guild_id)
    )


def reset_periods(conn, user_id, guild_id):
    row = conn.execute(
        "SELECT last_reset_daily, last_reset_weekly FROM xp WHERE user_id=? AND guild_id=?",
        (user_id, guild_id)
    ).fetchone()
    updates = {}
    if row["last_reset_daily"] != today_str():
        updates["daily_xp"] = 0
        updates["last_reset_daily"] = today_str()
    if row["last_reset_weekly"] != week_str():
        updates["weekly_xp"] = 0
        updates["last_reset_weekly"] = week_str()
    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE xp SET {set_clause} WHERE user_id=? AND guild_id=?",
            (*updates.values(), user_id, guild_id)
        )


def add_xp(conn, user_id, guild_id, minutes):
    minutes = min(minutes, MAX_SESSION_HOURS * 60)  # cap at 8 hours
    xp_earned = int(minutes * XP_PER_MINUTE)
    ensure_user(conn, user_id, guild_id)
    reset_periods(conn, user_id, guild_id)
    conn.execute(
        """UPDATE xp SET
               total_xp  = total_xp  + ?,
               daily_xp  = daily_xp  + ?,
               weekly_xp = weekly_xp + ?
           WHERE user_id=? AND guild_id=?""",
        (xp_earned, xp_earned, xp_earned, user_id, guild_id)
    )
    return xp_earned


def fmt_duration(minutes):
    m = int(minutes)
    h, rem = divmod(m, 60)
    if h:
        return f"{h}h {rem}m"
    return f"{rem}m"


def active_session(conn, user_id, guild_id):
    return conn.execute(
        "SELECT rowid, start_time FROM sessions WHERE user_id=? AND guild_id=? AND end_time IS NULL",
        (user_id, guild_id)
    ).fetchone()
# ─────────────────────────────────────────────────────────────────────────────


# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    print(f"Logged in as {bot.user} — slash commands synced.")
# ─────────────────────────────────────────────────────────────────────────────


# ── Auto-stop when user leaves voice channel ──────────────────────────────────
@bot.event
async def on_voice_state_update(member, before, after):
    # Only trigger when someone fully leaves a voice channel
    if before.channel is None or after.channel is not None:
        return

    uid, gid = member.id, member.guild.id
    with get_db() as conn:
        session = active_session(conn, uid, gid)
        if not session:
            return
        now = time.time()
        minutes = (now - session["start_time"]) / 60
        conn.execute(
            "UPDATE sessions SET end_time=?, duration=? WHERE rowid=?",
            (now, minutes, session["rowid"])
        )
        xp_earned = add_xp(conn, uid, gid, minutes)

    try:
        embed = discord.Embed(
            title="⏹ Session auto-stopped",
            description="You left the voice channel, so your session was stopped automatically.",
            color=discord.Color.red()
        )
        embed.add_field(name="Duration", value=fmt_duration(minutes), inline=True)
        embed.add_field(name="XP earned", value=f"+{xp_earned:,} XP", inline=True)
        await member.send(embed=embed)
    except discord.Forbidden:
        pass  # user has DMs off, that's fine
# ─────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
#  /study
# ══════════════════════════════════════════════════════════════════════════════
study_group = app_commands.Group(name="study", description="Manage your study session")


@study_group.command(name="start", description="Start a study session (must be in a voice channel)")
async def study_start(interaction: discord.Interaction):
    uid, gid = interaction.user.id, interaction.guild.id

    # Must be in a voice channel
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            "❌ You must join a voice channel first before starting a session!",
            ephemeral=True
        )
        return

    with get_db() as conn:
        if active_session(conn, uid, gid):
            await interaction.response.send_message(
                "⏱ You already have an active session. Use `/study stop` to end it first.",
                ephemeral=True
            )
            return
        conn.execute(
            "INSERT INTO sessions (user_id, guild_id, start_time) VALUES (?,?,?)",
            (uid, gid, time.time())
        )

    channel_name = interaction.user.voice.channel.name
    await interaction.response.send_message(
        f"📚 Session started in **{channel_name}**! XP will be awarded when you stop.\n"
        f"⚠️ Leaving the voice channel will auto-stop your session.",
        ephemeral=False
    )


@study_group.command(name="stop", description="Stop your current study session and earn XP")
async def study_stop(interaction: discord.Interaction):
    uid, gid = interaction.user.id, interaction.guild.id
    with get_db() as conn:
        session = active_session(conn, uid, gid)
        if not session:
            await interaction.response.send_message(
                "❌ You don't have an active session. Use `/study start` first.",
                ephemeral=True
            )
            return
        now = time.time()
        minutes = (now - session["start_time"]) / 60
        conn.execute(
            "UPDATE sessions SET end_time=?, duration=? WHERE rowid=?",
            (now, minutes, session["rowid"])
        )
        xp_earned = add_xp(conn, uid, gid, minutes)

    embed = discord.Embed(title="✅ Session complete!", color=discord.Color.green())
    embed.add_field(name="Duration", value=fmt_duration(minutes), inline=True)
    embed.add_field(name="XP earned", value=f"+{xp_earned:,} XP", inline=True)
    if minutes >= MAX_SESSION_HOURS * 60:
        embed.set_footer(text=f"XP capped at {MAX_SESSION_HOURS}h max per session.")
    else:
        embed.set_footer(text=f"Keep it up, {interaction.user.display_name}!")
    await interaction.response.send_message(embed=embed)


@study_group.command(name="status", description="Check your current session status")
async def study_status(interaction: discord.Interaction):
    uid, gid = interaction.user.id, interaction.guild.id
    with get_db() as conn:
        session = active_session(conn, uid, gid)
        if not session:
            await interaction.response.send_message(
                "💤 No active session. Start one with `/study start`.",
                ephemeral=True
            )
            return
        elapsed = (time.time() - session["start_time"]) / 60
        xp_preview = int(min(elapsed, MAX_SESSION_HOURS * 60) * XP_PER_MINUTE)

    embed = discord.Embed(title="⏱ Session in progress", color=discord.Color.blurple())
    embed.add_field(name="Time so far", value=fmt_duration(elapsed), inline=True)
    embed.add_field(name="XP on stop", value=f"~{xp_preview:,} XP", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(study_group)


# ══════════════════════════════════════════════════════════════════════════════
#  /xp
# ══════════════════════════════════════════════════════════════════════════════
@bot.tree.command(name="xp", description="Check your XP (or another user's)")
@app_commands.describe(user="The user to check (leave blank for yourself)")
async def xp_command(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    uid, gid = target.id, interaction.guild.id
    with get_db() as conn:
        ensure_user(conn, uid, gid)
        reset_periods(conn, uid, gid)
        row = conn.execute(
            "SELECT total_xp, daily_xp, weekly_xp FROM xp WHERE user_id=? AND guild_id=?",
            (uid, gid)
        ).fetchone()

    embed = discord.Embed(title=f"⭐ {target.display_name}'s XP", color=discord.Color.gold())
    embed.add_field(name="Today", value=f"{row['daily_xp']:,} XP", inline=True)
    embed.add_field(name="This week", value=f"{row['weekly_xp']:,} XP", inline=True)
    embed.add_field(name="All time", value=f"{row['total_xp']:,} XP", inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed)


# ══════════════════════════════════════════════════════════════════════════════
#  /leaderboard
# ══════════════════════════════════════════════════════════════════════════════
@bot.tree.command(name="leaderboard", description="Show the study leaderboard")
@app_commands.describe(period="daily or weekly")
@app_commands.choices(period=[
    app_commands.Choice(name="Daily", value="daily"),
    app_commands.Choice(name="Weekly", value="weekly"),
])
async def leaderboard(interaction: discord.Interaction, period: str = "daily"):
    gid = interaction.guild.id
    col = "daily_xp" if period == "daily" else "weekly_xp"
    label = "Today" if period == "daily" else "This week"

    with get_db() as conn:
        rows = conn.execute(
            f"SELECT user_id, {col} as xp FROM xp WHERE guild_id=? ORDER BY {col} DESC LIMIT 10",
            (gid,)
        ).fetchall()

    if not rows or all(r["xp"] == 0 for r in rows):
        await interaction.response.send_message(
            f"📊 No study data for {label.lower()} yet. Start a session with `/study start`!",
            ephemeral=True
        )
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, row in enumerate(rows):
        if row["xp"] == 0:
            break
        member = interaction.guild.get_member(row["user_id"])
        name = member.display_name if member else f"User {row['user_id']}"
        prefix = medals[i] if i < 3 else f"`{i+1}.`"
        lines.append(f"{prefix} **{name}** — {row['xp']:,} XP")

    embed = discord.Embed(
        title=f"🏆 Leaderboard — {label}",
        description="\n".join(lines),
        color=discord.Color.orange()
    )
    embed.set_footer(text=f"Top {len(lines)} studiers • {XP_PER_MINUTE} XP/min • max {MAX_SESSION_HOURS}h/session")
    await interaction.response.send_message(embed=embed)


# ══════════════════════════════════════════════════════════════════════════════
bot.run(BOT_TOKEN)
