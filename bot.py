import asyncio
import os
import discord
from discord.ext import tasks, commands
from discord import app_commands
import aiosqlite
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from keep_alive import keep_alive  # Import your keep_alive function

# ---------- SETUP ----------
load_dotenv()
keep_alive()  # Start the Flask server for uptime

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not set")

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

DB_PATH = "streakbot.db"

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

        # Handle migration: avoid duplicate column errors
        try:
            await db.execute("ALTER TABLE reminders ADD COLUMN last_sent TEXT")
        except aiosqlite.OperationalError:
            pass

        await db.commit()

# ---------- HELPERS ----------
async def ensure_user(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (uid,)
        )
        await db.commit()

async def get_guild_config(gid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT vlogger_role_id, reminder_channel_id, timezone FROM guild_config WHERE guild_id=?",
            (gid,)
        )
        return await cur.fetchone()

# ---------- WEEKLY CHECK ----------
@tasks.loop(time=dtime(hour=23, minute=59, second=0))
async def weekly_check():
    for guild in bot.guilds:
        config = await get_guild_config(guild.id)
        tz_name = config[2] if config and config[2] else "UTC"
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)

        # For testing, trigger at specific times, otherwise schedule weekly
        if now.weekday() != 5 or now.hour != 23 or now.minute != 59:
            continue

        today = now.date().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            # Check last checked to prevent duplicate checks
            cur = await db.execute(
                "SELECT last_checked FROM group_streak WHERE guild_id=?",
                (guild.id,)
            )
            row = await cur.fetchone()
            if row and row[0] == today:
                continue

            members = [m for m in guild.members if not m.bot]
            missed = []

            for m in members:
                await ensure_user(m.id)
                cur = await db.execute(
                    "SELECT recorded_this_week FROM users WHERE user_id=?",
                    (m.id,)
                )
                rec = await cur.fetchone()
                if not rec or rec[0] == 0:
                    missed.append(m.id)

            if not missed:
                await db.execute(
                    "INSERT INTO group_streak (guild_id, streak, last_checked) VALUES (?, 1, ?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET streak=streak+1, last_checked=?",
                    (guild.id, today, today)
                )
                await db.execute(
                    "UPDATE users SET personal_streak = personal_streak + 1 WHERE recorded_this_week=1"
                )
            else:
                await db.execute(
                    "UPDATE group_streak SET streak=0, last_checked=? WHERE guild_id=?",
                    (today, guild.id)
                )
                await db.execute(
                    "UPDATE users SET personal_streak=0 WHERE recorded_this_week=0"
                )

            await db.execute("UPDATE users SET recorded_this_week=0")
            await db.commit()

# ---------- REMINDERS ----------
@tasks.loop(minutes=1)
async def reminder_loop():
    await bot.wait_until_ready()
    print("[REMINDER LOOP TICK]")  # Debug

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT r.user_id, r.reminder_time, r.last_sent, u.timezone, u.recorded_this_week
            FROM reminders r
            JOIN users u ON r.user_id = u.user_id
        """)
        reminders = await cur.fetchall()

    for uid, r_time, last_sent, tz_name, recorded in reminders:
        if recorded:
            continue

        try:
            now = datetime.now(ZoneInfo(tz_name))
        except:
            now = datetime.utcnow()

        # Parse reminder time
        try:
            target_time = dtime.fromisoformat(r_time)
        except:
            continue

        # Check if it's time to send
        if not (now.hour == target_time.hour and abs(now.minute - target_time.minute) <= 1):
            continue

        today_str = now.date().isoformat()

        if last_sent == today_str:
            continue

        try:
            user = await bot.fetch_user(uid)
            # Send DM
            try:
                await user.send("⏰ Reminder: Don't forget to record this week!")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE reminders SET last_sent=? WHERE user_id=? AND reminder_time=?",
                        (today_str, uid, r_time)
                    )
                    await db.commit()
                continue
            except discord.Forbidden:
                pass  # fallback to channel

            # Send in server channel
            for guild in bot.guilds:
                member = guild.get_member(uid)
                if not member:
                    continue
                config = await get_guild_config(guild.id)
                if not config:
                    continue
                _, reminder_channel_id, _ = config
                if reminder_channel_id:
                    channel = guild.get_channel(reminder_channel_id)
                    if channel:
                        await channel.send(
                            f"⏰ {member.mention} reminder: don’t forget to record this week!"
                        )
                        # Update last_sent
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute(
                                "UPDATE reminders SET last_sent=? WHERE user_id=? AND reminder_time=?",
                                (today_str, uid, r_time)
                            )
                            await db.commit()
                        break
        except Exception as e:
            print(f"[REMINDER ERROR] User {uid}: {e}")

# ---------- SLASH COMMAND HELPERS ----------
async def send_response(interaction, message, ephemeral=True):
    try:
        await interaction.response.send_message(message, ephemeral=ephemeral)
    except discord.InteractionResponded:
        await interaction.followup.send(message, ephemeral=ephemeral)

# ---------- USER COMMANDS ----------
@tree.command(name="recorded", description="Mark that you recorded this week")
async def recorded(interaction: discord.Interaction):
    await ensure_user(interaction.user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET recorded_this_week=1 WHERE user_id=?",
            (interaction.user.id,)
        )
        await db.commit()
    await send_response(interaction, "✅ Recorded for this week!")

@tree.command(name="streak", description="Check your personal weekly streak")
async def streak(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT personal_streak FROM users WHERE user_id=?",
            (interaction.user.id,)
        )
        row = await cur.fetchone()
    await send_response(interaction, f"🔥 Your streak: **{row[0] if row else 0}** weeks")

@tree.command(name="groupstreak", description="Check the group weekly streak")
async def groupstreak(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT streak FROM group_streak WHERE guild_id=?",
            (interaction.guild.id,)
        )
        row = await cur.fetchone()
    await send_response(interaction, f"👥 Group streak: **{row[0] if row else 0}** weeks")

# ---------- ADMIN COMMANDS ----------
@tree.command(name="setvloggerrole", description="Set the Vlogger role (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def setvloggerrole(interaction: discord.Interaction, role: discord.Role):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO guild_config (guild_id, vlogger_role_id) VALUES (?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET vlogger_role_id=?",
            (interaction.guild.id, role.id, role.id)
        )
        await db.commit()
    await send_response(interaction, "✅ Vlogger role set")

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
    await send_response(interaction, "✅ Reminder channel set")

@tree.command(name="setservertimezone", description="Set server timezone (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def setservertimezone(interaction: discord.Interaction, timezone: str):
    try:
        ZoneInfo(timezone)
    except:
        await send_response(interaction, "❌ Invalid timezone")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO guild_config (guild_id, timezone) VALUES (?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET timezone=?",
            (interaction.guild.id, timezone, timezone)
        )
        await db.commit()
    await send_response(interaction, f"✅ Server timezone set to {timezone}")

# ---------- USER TIMEZONE ----------
@tree.command(name="settimezone", description="Set your personal timezone for reminders")
async def settimezone(interaction: discord.Interaction, timezone: str):
    try:
        ZoneInfo(timezone)
    except:
        await send_response(interaction, "❌ Invalid timezone. Example: America/Chicago")
        return
    await ensure_user(interaction.user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET timezone=? WHERE user_id=?", (timezone, interaction.user.id))
        await db.commit()
    await send_response(interaction, f"✅ Your timezone is set to {timezone}")

# ---------- REMINDER MANAGEMENT ----------
@tree.command(name="addreminder", description="Set a personal reminder at HH:MM in your timezone")
async def addreminder(interaction: discord.Interaction, time_str: str):
    try:
        # Validate time
        target_time = dtime.fromisoformat(time_str)
    except:
        await send_response(interaction, "❌ Invalid time format. Use HH:MM.")
        return
    await ensure_user(interaction.user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO reminders (user_id, reminder_time, last_sent) VALUES (?, ?, ?)",
            (interaction.user.id, time_str, None)
        )
        await db.commit()
    await send_response(interaction, f"✅ Reminder set at {time_str}.")

@tree.command(name="removereminder", description="Remove a specific reminder time")
async def removereminder(interaction: discord.Interaction, time_str: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM reminders WHERE user_id=? AND reminder_time=?",
            (interaction.user.id, time_str)
        )
        await db.commit()
    await send_response(interaction, f"✅ Reminder at {time_str} removed.")

@tree.command(name="listreminders", description="List all your active reminders")
async def listreminders(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT reminder_time FROM reminders WHERE user_id=?",
            (interaction.user.id,)
        )
        rows = await cur.fetchall()
    reminders = [row[0] for row in rows]
    if reminders:
        await send_response(interaction, "Your reminders:\n" + "\n".join(reminders))
    else:
        await send_response(interaction, "You have no active reminders.")

# ---------- HELP ----------
@tree.command(name="help", description="Show all commands")
async def help_command(interaction: discord.Interaction):
    help_text = """
**📜 VlogBot Commands**

**/recorded** - Mark that you recorded for the week.

**/streak** - Check your personal weekly streak.

**/groupstreak** - Check the group weekly streak.

**/settimezone Your/Timezone** - Set your personal timezone.

**/addreminder HH:MM** - Set a reminder at specific time.

**/removereminder HH:MM** - Remove a reminder.

**/listreminders** - List all your reminders.

**/setvloggerrole @Role** - (Admin) Set Vlogger role.

**/setreminderchannel #Channel** - (Admin) Set fallback reminder channel.

**/setservertimezone Your/Timezone** - (Admin) Set server timezone.
"""
    await send_response(interaction, help_text)

# ---------- ERROR HANDLING ----------
@tree.error
async def on_app_command_error(interaction, error):
    print(f"Error in {interaction.command}: {error}")
    try:
        await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
    except:
        pass

# ---------- START ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await init_db()
    try:
        await tree.sync()
    except Exception as e:
        print(f"Error syncing commands: {e}")
    # Start background tasks
    if not weekly_check.is_running():
        weekly_check.start()
    if not reminder_loop.is_running():
        reminder_loop.start()
    print("Tasks started.")

# Call keep_alive() before starting the bot
keep_alive()

# Run the bot
async def main():
    await bot.start(TOKEN)

asyncio.run(main())
