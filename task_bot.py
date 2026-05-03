"""
Discord Task Bot
================
Features:
  - Personal daily & weekly tasks
  - Mark tasks as done with a command
  - Daily 10pm IST announcement of everyone's progress
  - Weekly Sunday 10pm IST summary with completion rates

Commands:
  /task add <title> <daily|weekly>   — Add a task
  /task done <title>                 — Mark a task as complete
  /task list                         — See your tasks
  /task delete <title>               — Delete a task
  /tasks @user                       — See someone else's tasks
  /settaskschannel                   — (Admin) Set announcement channel
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import sqlite3
import datetime
import os
import asyncio

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "task_bot.db"
# IST = UTC+5:30, so 10pm IST = 16:30 UTC
ANNOUNCE_HOUR_UTC = 16
ANNOUNCE_MINUTE_UTC = 30
# ─────────────────────────────────────────────────────────────────────────────


# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL,
                title       TEXT NOT NULL,
                type        TEXT NOT NULL CHECK(type IN ('daily','weekly')),
                done        INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL,
                done_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS config (
                guild_id            INTEGER PRIMARY KEY,
                announce_channel_id INTEGER
            );
        """)
# ─────────────────────────────────────────────────────────────────────────────


# ── Helpers ───────────────────────────────────────────────────────────────────
def today_str():
    return datetime.date.today().isoformat()


def week_str():
    d = datetime.date.today()
    return f"{d.year}-W{d.isocalendar()[1]:02d}"


def get_announce_channel(guild_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT announce_channel_id FROM config WHERE guild_id=?",
            (guild_id,)
        ).fetchone()
        return row["announce_channel_id"] if row else None


def truncate(text, length=40):
    return text if len(text) <= length else text[:length - 1] + "…"
# ─────────────────────────────────────────────────────────────────────────────


# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    init_db()
    await bot.tree.sync()
    daily_announcement.start()
    print(f"Logged in as {bot.user} — slash commands synced.")
# ─────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
#  Scheduled announcements
# ══════════════════════════════════════════════════════════════════════════════
@tasks.loop(minutes=1)
async def daily_announcement():
    now_utc = datetime.datetime.utcnow()
    if now_utc.hour != ANNOUNCE_HOUR_UTC or now_utc.minute != ANNOUNCE_MINUTE_UTC:
        return

    today = today_str()
    is_sunday = now_utc.weekday() == 6  # Sunday

    with get_db() as conn:
        guild_configs = conn.execute("SELECT guild_id, announce_channel_id FROM config").fetchall()

    for cfg in guild_configs:
        guild = bot.get_guild(cfg["guild_id"])
        channel = guild.get_channel(cfg["announce_channel_id"]) if guild else None
        if not channel:
            continue

        # ── Daily summary ──
        with get_db() as conn:
            # Get all users who have daily tasks today
            users = conn.execute(
                """SELECT DISTINCT user_id FROM tasks
                   WHERE guild_id=? AND type='daily' AND created_at=?""",
                (cfg["guild_id"], today)
            ).fetchall()

        if users:
            lines = []
            for u in users:
                uid = u["user_id"]
                with get_db() as conn:
                    total = conn.execute(
                        "SELECT COUNT(*) as c FROM tasks WHERE user_id=? AND guild_id=? AND type='daily' AND created_at=?",
                        (uid, cfg["guild_id"], today)
                    ).fetchone()["c"]
                    done = conn.execute(
                        "SELECT COUNT(*) as c FROM tasks WHERE user_id=? AND guild_id=? AND type='daily' AND created_at=? AND done=1",
                        (uid, cfg["guild_id"], today)
                    ).fetchone()["c"]
                member = guild.get_member(uid)
                name = member.display_name if member else f"User {uid}"
                pct = int((done / total) * 100) if total else 0
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                status = "✅" if done == total else ("🔥" if done > 0 else "😴")
                lines.append(f"{status} **{name}** — {done}/{total} tasks  `{bar}` {pct}%")

            embed = discord.Embed(
                title="📋 Daily Task Report — 10 PM IST",
                description="\n".join(lines),
                color=discord.Color.blurple(),
                timestamp=datetime.datetime.utcnow()
            )
            embed.set_footer(text="Keep going! Tomorrow is a new day.")
            await channel.send(embed=embed)

        # ── Weekly summary (Sundays only) ──
        if is_sunday:
            current_week = week_str()
            with get_db() as conn:
                users_w = conn.execute(
                    """SELECT DISTINCT user_id FROM tasks
                       WHERE guild_id=? AND type='weekly'""",
                    (cfg["guild_id"],)
                ).fetchall()

            if users_w:
                lines_w = []
                for u in users_w:
                    uid = u["user_id"]
                    with get_db() as conn:
                        # weekly tasks created this week
                        total = conn.execute(
                            """SELECT COUNT(*) as c FROM tasks
                               WHERE user_id=? AND guild_id=? AND type='weekly'
                               AND strftime('%Y-W%W', created_at)=?""",
                            (uid, cfg["guild_id"],
                             datetime.date.today().strftime("%Y-W%W"))
                        ).fetchone()["c"]
                        done = conn.execute(
                            """SELECT COUNT(*) as c FROM tasks
                               WHERE user_id=? AND guild_id=? AND type='weekly'
                               AND strftime('%Y-W%W', created_at)=? AND done=1""",
                            (uid, cfg["guild_id"],
                             datetime.date.today().strftime("%Y-W%W"))
                        ).fetchone()["c"]
                    member = guild.get_member(uid)
                    name = member.display_name if member else f"User {uid}"
                    pct = int((done / total) * 100) if total else 0
                    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                    status = "🏆" if pct == 100 else ("📈" if pct >= 50 else "📉")
                    lines_w.append(f"{status} **{name}** — {done}/{total} tasks  `{bar}` {pct}%")

                embed_w = discord.Embed(
                    title="📊 Weekly Task Summary",
                    description="\n".join(lines_w),
                    color=discord.Color.gold(),
                    timestamp=datetime.datetime.utcnow()
                )
                embed_w.set_footer(text="New week starts tomorrow. Set new goals!")
                await channel.send(embed=embed_w)

            # Reset daily tasks for all users (new week)
            with get_db() as conn:
                conn.execute(
                    "DELETE FROM tasks WHERE guild_id=? AND type='daily'",
                    (cfg["guild_id"],)
                )


# ══════════════════════════════════════════════════════════════════════════════
#  /settaskschannel  (Admin only)
# ══════════════════════════════════════════════════════════════════════════════
@bot.tree.command(name="settaskschannel", description="Set the channel for daily/weekly task announcements (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def set_tasks_channel(interaction: discord.Interaction):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO config (guild_id, announce_channel_id) VALUES (?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET announce_channel_id=excluded.announce_channel_id",
            (interaction.guild.id, interaction.channel.id)
        )
    await interaction.response.send_message(
        f"✅ Task announcements will be posted in {interaction.channel.mention} at **10 PM IST** daily.",
        ephemeral=True
    )


# ══════════════════════════════════════════════════════════════════════════════
#  /task
# ══════════════════════════════════════════════════════════════════════════════
task_group = app_commands.Group(name="task", description="Manage your personal tasks")


@task_group.command(name="add", description="Add a new task")
@app_commands.describe(
    title="What's the task?",
    type="Is it a daily or weekly task?"
)
@app_commands.choices(type=[
    app_commands.Choice(name="Daily", value="daily"),
    app_commands.Choice(name="Weekly", value="weekly"),
])
async def task_add(interaction: discord.Interaction, title: str, type: str):
    uid, gid = interaction.user.id, interaction.guild.id
    today = today_str()

    with get_db() as conn:
        # Check for duplicate
        exists = conn.execute(
            "SELECT id FROM tasks WHERE user_id=? AND guild_id=? AND title=? AND done=0",
            (uid, gid, title)
        ).fetchone()
        if exists:
            await interaction.response.send_message(
                f"❌ You already have an incomplete task called **{truncate(title)}**.",
                ephemeral=True
            )
            return

        # Limit to 10 active tasks per type
        count = conn.execute(
            "SELECT COUNT(*) as c FROM tasks WHERE user_id=? AND guild_id=? AND type=? AND done=0",
            (uid, gid, type)
        ).fetchone()["c"]
        if count >= 10:
            await interaction.response.send_message(
                f"❌ You can have at most 10 active {type} tasks. Complete or delete some first.",
                ephemeral=True
            )
            return

        conn.execute(
            "INSERT INTO tasks (user_id, guild_id, title, type, created_at) VALUES (?,?,?,?,?)",
            (uid, gid, title, type, today)
        )

    emoji = "📅" if type == "daily" else "📆"
    await interaction.response.send_message(
        f"{emoji} **{type.capitalize()} task added:** {truncate(title)}",
        ephemeral=True
    )


@task_group.command(name="done", description="Mark a task as completed")
@app_commands.describe(title="The name of the task to mark as done")
async def task_done(interaction: discord.Interaction, title: str):
    uid, gid = interaction.user.id, interaction.guild.id

    with get_db() as conn:
        task = conn.execute(
            "SELECT id, type FROM tasks WHERE user_id=? AND guild_id=? AND title=? AND done=0",
            (uid, gid, title)
        ).fetchone()
        if not task:
            await interaction.response.send_message(
                f"❌ No incomplete task found with that name. Check `/task list` for your tasks.",
                ephemeral=True
            )
            return
        conn.execute(
            "UPDATE tasks SET done=1, done_at=? WHERE id=?",
            (datetime.datetime.utcnow().isoformat(), task["id"])
        )

    await interaction.response.send_message(
        f"✅ Marked as done: **{truncate(title)}**  Great work!",
        ephemeral=False
    )


@task_group.command(name="list", description="See all your current tasks")
async def task_list(interaction: discord.Interaction):
    uid, gid = interaction.user.id, interaction.guild.id
    today = today_str()

    with get_db() as conn:
        daily = conn.execute(
            "SELECT title, done FROM tasks WHERE user_id=? AND guild_id=? AND type='daily' AND created_at=? ORDER BY done, rowid",
            (uid, gid, today)
        ).fetchall()
        weekly = conn.execute(
            """SELECT title, done FROM tasks
               WHERE user_id=? AND guild_id=? AND type='weekly'
               AND strftime('%Y-W%W', created_at) = strftime('%Y-W%W', 'now')
               ORDER BY done, rowid""",
            (uid, gid)
        ).fetchall()

    embed = discord.Embed(
        title=f"📋 {interaction.user.display_name}'s Tasks",
        color=discord.Color.blurple()
    )

    def fmt_tasks(task_list):
        if not task_list:
            return "_No tasks yet._"
        return "\n".join(
            f"{'✅' if t['done'] else '⬜'} {truncate(t['title'])}"
            for t in task_list
        )

    daily_done = sum(1 for t in daily if t["done"])
    weekly_done = sum(1 for t in weekly if t["done"])

    embed.add_field(
        name=f"📅 Daily ({daily_done}/{len(daily)} done)",
        value=fmt_tasks(daily),
        inline=False
    )
    embed.add_field(
        name=f"📆 Weekly ({weekly_done}/{len(weekly)} done)",
        value=fmt_tasks(weekly),
        inline=False
    )
    embed.set_footer(text="Use /task done <title> to mark a task complete")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@task_group.command(name="delete", description="Delete a task")
@app_commands.describe(title="The name of the task to delete")
async def task_delete(interaction: discord.Interaction, title: str):
    uid, gid = interaction.user.id, interaction.guild.id

    with get_db() as conn:
        task = conn.execute(
            "SELECT id FROM tasks WHERE user_id=? AND guild_id=? AND title=? AND done=0",
            (uid, gid, title)
        ).fetchone()
        if not task:
            await interaction.response.send_message(
                "❌ No incomplete task found with that name.",
                ephemeral=True
            )
            return
        conn.execute("DELETE FROM tasks WHERE id=?", (task["id"],))

    await interaction.response.send_message(
        f"🗑️ Deleted task: **{truncate(title)}**",
        ephemeral=True
    )


bot.tree.add_command(task_group)


# ══════════════════════════════════════════════════════════════════════════════
#  /tasks @user  — view someone else's tasks
# ══════════════════════════════════════════════════════════════════════════════
@bot.tree.command(name="tasks", description="View another user's tasks")
@app_commands.describe(user="The user whose tasks you want to see")
async def view_tasks(interaction: discord.Interaction, user: discord.Member):
    uid, gid = user.id, interaction.guild.id
    today = today_str()

    with get_db() as conn:
        daily = conn.execute(
            "SELECT title, done FROM tasks WHERE user_id=? AND guild_id=? AND type='daily' AND created_at=? ORDER BY done, rowid",
            (uid, gid, today)
        ).fetchall()
        weekly = conn.execute(
            """SELECT title, done FROM tasks
               WHERE user_id=? AND guild_id=? AND type='weekly'
               AND strftime('%Y-W%W', created_at) = strftime('%Y-W%W', 'now')
               ORDER BY done, rowid""",
            (uid, gid)
        ).fetchall()

    embed = discord.Embed(
        title=f"📋 {user.display_name}'s Tasks",
        color=discord.Color.teal()
    )

    def fmt_tasks(task_list):
        if not task_list:
            return "_No tasks yet._"
        return "\n".join(
            f"{'✅' if t['done'] else '⬜'} {truncate(t['title'])}"
            for t in task_list
        )

    daily_done = sum(1 for t in daily if t["done"])
    weekly_done = sum(1 for t in weekly if t["done"])

    embed.add_field(
        name=f"📅 Daily ({daily_done}/{len(daily)} done)",
        value=fmt_tasks(daily),
        inline=False
    )
    embed.add_field(
        name=f"📆 Weekly ({weekly_done}/{len(weekly)} done)",
        value=fmt_tasks(weekly),
        inline=False
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


# ══════════════════════════════════════════════════════════════════════════════
bot.run(BOT_TOKEN)
