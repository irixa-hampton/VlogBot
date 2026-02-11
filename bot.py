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
        await db.execute("UPDATE users SET recorded_this_week=1 WHERE user_id=?", (interaction.user.id,))
        await db.commit()
    await interaction.response.send_message("✅ Recorded for this week!", ephemeral=True)

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
    await ensure_user(interaction.user.id)  # make sure user exists in DB
    try:
        # Option 1: send to reminder channel if set
        if reminder_channel_id:
            channel = bot.get_channel(reminder_channel_id)
            if channel:
                await channel.send(f"<@{interaction.user.id}> ⏰ This is a test reminder ping!")
                await interaction.response.send_message("✅ Test ping sent in the reminder channel!", ephemeral=True)
                return
        # Option 2: fallback to DM
        await interaction.user.send("⏰ This is a test reminder ping!")
        await interaction.response.send_message("✅ Test ping sent via DM!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to send test ping: {e}", ephemeral=True)



# ================== DAILY PING TASK ==================
@tasks.loop(minutes=1)
async def daily_ping():
    await load_user_timezones()
    now_utc = datetime.now(timezone.utc)
    for user_id, tz_str in user_timezones.items():
        try:
            user_tz = ZoneInfo(tz_str)
        except Exception:
            user_tz = ZoneInfo("UTC")
        user_now = now_utc.astimezone(user_tz)
        if user_now.hour == 7 and user_now.minute == 0:
            # Check if user already recorded
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT recorded_this_week FROM users WHERE user_id=?", (user_id,)) as cursor:
                    row = await cursor.fetchone()
                    recorded = row[0] if row else 0
                    if recorded:
                        continue
            # Send in reminder channel
            if reminder_channel_id:
                channel = bot.get_channel(reminder_channel_id)
                if channel:
                    try:
                        await channel.send(f"<@{user_id}> ⏰ Don't forget to record this week!")
                    except Exception as e:
                        print(f"Error pinging user {user_id}: {e}")

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
