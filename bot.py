from challenge_list import challenge_ideas
import random
import asyncio
import os
import discord
from discord.ext import tasks, commands
from discord import app_commands
import aiosqlite
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from keep_alive import keep_alive

# ---------- SETUP ----------
load_dotenv()
keep_alive()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not set")

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

DB_PATH = "streakbot.db"
reminder_channel_id = None
user_timezones = {}

# ---------- DATABASE ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT DEFAULT 'UTC',
            recorded_this_week INTEGER DEFAULT 0,
            personal_streak INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS reminders (
            user_id INTEGER,
            reminder_time TEXT,
            last_sent TEXT,
            PRIMARY KEY (user_id, reminder_time)
        );
        CREATE TABLE IF NOT EXISTS group_streak (
            guild_id INTEGER PRIMARY KEY,
            streak INTEGER DEFAULT 0,
            last_checked TEXT
        );
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            vlogger_role_id INTEGER,
            reminder_channel_id INTEGER,
            timezone TEXT DEFAULT 'UTC'
        );
        CREATE TABLE IF NOT EXISTS weekly_challenge (
            guild_id INTEGER PRIMARY KEY,
            challenge_text TEXT,
            last_sent TEXT
        );
        CREATE TABLE IF NOT EXISTS xp (
            user_id INTEGER PRIMARY KEY,
            xp INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS weekly_challenge (
            guild_id INTEGER PRIMARY KEY,
            challenge_text TEXT,
            last_sent TEXT
        );
        CREATE TABLE IF NOT EXISTS challenge_completions (
            user_id INTEGER,
            guild_id INTEGER,
            week INTEGER,
            PRIMARY KEY (user_id, guild_id, week)
        );
        CREATE TABLE IF NOT EXISTS monthly_bonus (
            user_id INTEGER,
            year INTEGER,
            month INTEGER,
            PRIMARY KEY (user_id, year, month)
        );
        """)
        await db.commit()

async def load_user_timezones():
    global user_timezones
    user_timezones = {}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, timezone FROM users") as cursor:
            rows = await cursor.fetchall()
            for user_id, tz in rows:
                user_timezones[user_id] = tz

async def load_reminder_channel():
    global reminder_channel_id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT reminder_channel_id FROM guild_config WHERE reminder_channel_id IS NOT NULL LIMIT 1"
        ) as cursor:
            result = await cursor.fetchone()
            if result:
                reminder_channel_id = result[0]

async def ensure_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        await db.commit()

async def add_xp(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO xp (user_id, xp) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET xp = xp + ?",
            (user_id, amount, amount)
        )
        await db.commit()

# ================== COMMANDS ==================
@tree.command(name="setreminderchannel", description="Set fallback reminder channel (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def setreminderchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO guild_config (guild_id, reminder_channel_id) VALUES (?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET reminder_channel_id=?",
            (interaction.guild.id, channel.id, channel.id)
        )
        await db.commit()
    global reminder_channel_id
    reminder_channel_id = channel.id
    await interaction.response.send_message(f"Reminder channel set to {channel.mention}")

@tree.command(name="settimezone", description="Set your personal timezone for reminders")
async def settimezone(interaction: discord.Interaction, timezone_str: str):
    try:
        ZoneInfo(timezone_str)
    except Exception:
        await interaction.response.send_message("❌ Invalid timezone. Example: America/Chicago", ephemeral=True)
        return
    await ensure_user(interaction.user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET timezone=? WHERE user_id=?", (timezone_str, interaction.user.id))
        await db.commit()
    await interaction.response.send_message(f"✅ Your timezone is set to {timezone_str}", ephemeral=True)

@tree.command(name="recorded", description="Mark that you recorded this week")
async def recorded(interaction: discord.Interaction):
    await ensure_user(interaction.user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET recorded_this_week=1 WHERE user_id=?",
            (interaction.user.id,)
        )
        await db.commit()

    await add_xp(interaction.user.id, 50)  # 🎯 XP for recording
    await interaction.response.send_message(
        "✅ Recorded for this week! (+50 XP)", ephemeral=True
    )

@tree.command(name="streak", description="Check your personal weekly streak")
async def streak(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT personal_streak FROM users WHERE user_id=?", (interaction.user.id,)) as cursor:
            row = await cursor.fetchone()
    await interaction.response.send_message(f"🔥 Your streak: **{row[0] if row else 0}** weeks", ephemeral=True)

@tree.command(name="groupstreak", description="Check the group weekly streak")
async def groupstreak(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT streak FROM group_streak WHERE guild_id=?", (interaction.guild.id,)) as cursor:
            row = await cursor.fetchone()
    await interaction.response.send_message(f"👥 Group streak: **{row[0] if row else 0}** weeks", ephemeral=True)

@tree.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    help_text = """
**VlogBot Commands:**

/setreminderchannel [channel] - Set the channel where weekly reminders are sent (Admin only)
/settimezone [timezone] - Set your personal timezone for reminders
/recorded - Mark that you recorded this week
/streak - Check your personal weekly streak
/groupstreak - Check the group weekly streak
/help - Show this message
"""
    await interaction.response.send_message(help_text, ephemeral=True)

@tree.command(name="testping", description="Send yourself a test reminder ping")
async def testping(interaction: discord.Interaction):
    await ensure_user(interaction.user.id)
    try:
        if reminder_channel_id:
            channel = bot.get_channel(reminder_channel_id)
            if channel:
                await channel.send(f"<@{interaction.user.id}> ⏰ This is a test reminder ping!")
                await interaction.response.send_message("✅ Test ping sent in the reminder channel!", ephemeral=True)
                return
        await interaction.user.send("⏰ This is a test reminder ping!")
        await interaction.response.send_message("✅ Test ping sent via DM!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to send test ping: {e}", ephemeral=True)


# ================== WEEKLY CHALLENGE ==================
async def send_weekly_challenge(guild_id: int, channel_id: int):
    """Send a weekly challenge for a guild if not already sent this week."""
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() != 6 or now_utc.hour != 7 or now_utc.minute != 0:
        return  # Only run on Sunday at 7:00 AM

    async with aiosqlite.connect(DB_PATH) as db:
        # Check if already sent this week
        async with db.execute("SELECT last_sent FROM weekly_challenge WHERE guild_id=?", (guild_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                last_sent = datetime.fromisoformat(row[0])
                if last_sent.isocalendar()[1] == now_utc.isocalendar()[1]:
                    return  # Already sent this week

        # Pick a random challenge
        challenge_text = random.choice(challenge_ideas)

        # Send to guild's reminder channel
        channel = bot.get_channel(channel_id)
        if channel:
            try:
                await channel.send(f"# 🎬 Weekly Challenge: ||{challenge_text}||")
            except Exception as e:
                print(f"Error sending weekly challenge in guild {guild_id}: {e}")

        # Save/update in the DB
        await db.execute("""
            INSERT INTO weekly_challenge (guild_id, challenge_text, last_sent)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET challenge_text=?, last_sent=?
        """, (guild_id, challenge_text, now_utc.isoformat(), challenge_text, now_utc.isoformat()))
        await db.commit()

@tree.command(name="challengecomplete", description="Mark the weekly challenge as completed")
async def challengecomplete(interaction: discord.Interaction):
    now = datetime.now(timezone.utc)
    week = now.isocalendar()[1]

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO challenge_completions (user_id, guild_id, week) VALUES (?, ?, ?)",
                (interaction.user.id, interaction.guild.id, week)
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            await interaction.response.send_message(
                "❌ You already completed this week's challenge.",
                ephemeral=True
            )
            return

    await add_xp(interaction.user.id, 100)  # 🎬 Challenge XP
    await interaction.response.send_message(
        "🎉 Weekly challenge completed! (+100 XP)",
        ephemeral=True
    )

@tree.command(name="leaderboard", description="Show the XP leaderboard")
async def leaderboard(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, xp FROM xp ORDER BY xp DESC LIMIT 10"
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await interaction.response.send_message("No XP data yet!", ephemeral=True)
        return

    lines = []
    for i, (user_id, xp) in enumerate(rows, start=1):
        user = interaction.guild.get_member(user_id)
        name = user.display_name if user else f"User {user_id}"
        lines.append(f"**{i}.** {name} — {xp} XP")

    await interaction.response.send_message(
        "🏆 **XP Leaderboard** 🏆\n\n" + "\n".join(lines)
    )

@tree.command(name="stats", description="View your vlogging stats")
async def stats(interaction: discord.Interaction):
    user_id = interaction.user.id
    guild_id = interaction.guild.id
    now = datetime.now(timezone.utc)
    week = now.isocalendar()[1]

    async with aiosqlite.connect(DB_PATH) as db:
        # XP
        async with db.execute("SELECT xp FROM xp WHERE user_id=?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            xp = row[0] if row else 0

        # Progressive level system (+150 XP per level)
            level = 0
            xp_remaining = xp
            xp_needed = 150

            while xp_remaining >= xp_needed:
                xp_remaining -= xp_needed
                level += 1
                xp_needed += 150
            xp_to_next = xp_needed - xp_remaining


        # Recorded this week + streak
        async with db.execute(
            "SELECT recorded_this_week, personal_streak FROM users WHERE user_id=?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            recorded = row[0] if row else 0
            streak = row[1] if row else 0

        # Weekly challenge completion
        async with db.execute(
            "SELECT 1 FROM challenge_completions WHERE user_id=? AND guild_id=? AND week=?",
            (user_id, guild_id, week)
        ) as cursor:
            challenge_done = await cursor.fetchone() is not None

        # Rank (XP leaderboard position)
        async with db.execute(
            "SELECT COUNT(*) + 1 FROM xp WHERE xp > ?",
            (xp,)
        ) as cursor:
            row = await cursor.fetchone()
            rank = row[0] if row else "N/A"

    # Build response
    msg = (
        "📊 **Your Vlog Stats**\n\n"
        f"⭐ **XP:** {xp}\n"
        f"📈 **Level:** {level} ({xp_to_next} XP to next level)\n\n"
        f"📹 **Recorded this week:** {'✅' if recorded else '❌'}\n"
        f"🔥 **Recording streak:** {streak} weeks\n\n"
        f"🎬 **Weekly challenge:** {'Completed ✅' if challenge_done else 'Not completed ❌'}\n"
        f"🏆 **XP Rank:** #{rank}"
    )

    await interaction.response.send_message(msg, ephemeral=True)

# ================ MONTHLY CONSISTENCY CHECK ================
async def check_monthly_consistency():
    now = datetime.now(timezone.utc)
    year = now.year
    month = now.month

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()

        for (user_id,) in users:
            # Already rewarded?
            async with db.execute(
                "SELECT 1 FROM monthly_bonus WHERE user_id=? AND year=? AND month=?",
                (user_id, year, month)
            ) as cursor:
                if await cursor.fetchone():
                    continue

            # Must have recorded this week (end-of-month check)
            async with db.execute(
                "SELECT recorded_this_week FROM users WHERE user_id=?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()

            if row and row[0] == 1:
                await add_xp(user_id, 500)
                await db.execute(
                    "INSERT INTO monthly_bonus (user_id, year, month) VALUES (?, ?, ?)",
                    (user_id, year, month)
                )

        await db.commit()


# ================== DAILY PING TASK ==================
@tasks.loop(minutes=1)
async def daily_ping():
    await load_user_timezones()
    now_utc = datetime.now(timezone.utc)

    # ---------- MONTHLY CONSISTENCY BONUS ----------
    if now_utc.day == 1 and now_utc.hour == 7 and now_utc.minute == 0:
        await check_monthly_consistency()

    # ---------- WEEKLY CHALLENGE ----------
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT guild_id, reminder_channel_id FROM guild_config WHERE reminder_channel_id IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
            for guild_id, channel_id in rows:
                await send_weekly_challenge(guild_id, channel_id)

    # ---------- DAILY REMINDERS ----------
    for user_id, tz_str in user_timezones.items():
        try:
            user_tz = ZoneInfo(tz_str)
        except Exception:
            user_tz = ZoneInfo("UTC")

        user_now = now_utc.astimezone(user_tz)

        if user_now.hour == 7 and user_now.minute == 0:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT recorded_this_week FROM users WHERE user_id=?",
                    (user_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0]:
                        continue

            if reminder_channel_id:
                channel = bot.get_channel(reminder_channel_id)
                if channel:
                    await channel.send(
                        f"<@{user_id}> ⏰ Don't forget to record this week!"
                    )


# ================== ON READY EVENT ==================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await init_db()
    await load_user_timezones()
    await load_reminder_channel()
    if not daily_ping.is_running():
        daily_ping.start()
    try:
        await tree.sync()
    except Exception as e:
        print(f"Error syncing commands: {e}")

# ================== MAIN ==================
async def main():
    await init_db()
    await load_user_timezones()
    await load_reminder_channel()
    await bot.start(TOKEN)

asyncio.run(main())
