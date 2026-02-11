import discord
from discord.ext import tasks, commands
from discord import app_commands
import aiosqlite
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from keep_alive import keep_alive

# ---------- SETUP ----------
load_dotenv()
keep_alive()  # For Render Web Service

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
@tasks.loop(minutes=1)
async def weekly_check():
    for guild in bot.guilds:
        config = await get_guild_config(guild.id)
        tz_name = config[2] if config else "UTC"
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)

        if now.weekday() != 5 or now.hour != 23 or now.minute != 59:
            continue

        today = now.date().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
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
                if (await cur.fetchone())[0] == 0:
                    missed.append(m.id)

            if not missed:
                await db.execute(
                    "INSERT INTO group_streak (guild_id, streak, last_checked) VALUES (?,1,?) "
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
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT r.user_id, r.reminder_time, u.timezone, u.recorded_this_week
            FROM reminders r
            JOIN users u ON r.user_id = u.user_id
        """)
        reminders = await cur.fetchall()

    for uid, r_time, tz_name, recorded in reminders:
        if recorded:
            continue

        try:
            now = datetime.now(ZoneInfo(tz_name))
        except Exception:
            now = datetime.utcnow()

        if now.strftime("%H:%M") != r_time:
            continue

        try:
            user = await bot.fetch_user(uid)
            for guild in bot.guilds:
                member = guild.get_member(uid)
                if not member:
                    continue
                config = await get_guild_config(guild.id)
                if not config:
                    continue
                vlogger_role_id, reminder_channel_id, _ = config
                if not vlogger_role_id:
                    continue
                if not discord.utils.get(member.roles, id=vlogger_role_id):
                    continue
                try:
                    await user.send("⏰ Reminder: Don't forget to record this week!")
                except discord.Forbidden:
                    if reminder_channel_id:
                        channel = guild.get_channel(reminder_channel_id)
                        if channel:
                            await channel.send(f"⏰ {member.mention} reminder: don’t forget to record this week!")
                break
        except Exception:
            continue

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
    except Exception:
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
    except Exception:
        await send_response(interaction, "❌ Invalid timezone. Example: America/Chicago")
        return
    await ensure_user(interaction.user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET timezone=? WHERE user_id=?", (timezone, interaction.user.id))
        await db.commit()
    await send_response(interaction, f"✅ Your timezone is set to {timezone}")

# ---------- REMINDER COMMANDS ----------
@tree.command(name="setreminder", description="Set a personal reminder (24-hour format HH:MM)")
async def setreminder(interaction: discord.Interaction, time: str):
    try:
        datetime.strptime(time, "%H:%M")
    except ValueError:
        await send_response(interaction, "⏰ Time must be HH:MM in 24-hour format")
        return
    await ensure_user(interaction.user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM reminders WHERE user_id=?", (interaction.user.id,))
        count = (await cur.fetchone())[0]
        if count >= 5:
            await send_response(interaction, "You already have 5 reminders set")
            return
        await db.execute(
            "INSERT OR IGNORE INTO reminders (user_id, reminder_time) VALUES (?,?)",
            (interaction.user.id, time)
        )
        await db.commit()
    await send_response(interaction, f"✅ Reminder set for {time} daily")

@tree.command(name="removereminder", description="Remove a personal reminder")
async def removereminder(interaction: discord.Interaction, time: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM reminders WHERE user_id=? AND reminder_time=?", (interaction.user.id, time))
        await db.commit()
    await send_response(interaction, f"✅ Reminder {time} removed")

@tree.command(name="listreminders", description="List all your personal reminders")
async def listreminders(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT reminder_time FROM reminders WHERE user_id=?", (interaction.user.id,))
        rows = await cur.fetchall()
    if rows:
        await send_response(interaction, "⏰ Your reminders: " + ", ".join(r[0] for r in rows))
    else:
        await send_response(interaction, "You have no reminders set")

# ---------- HELP ----------
@tree.command(name="help", description="Show all commands")
async def help_command(interaction: discord.Interaction):
    help_text = """
**📜 VlogBot Commands**

**/recorded** - Mark that you recorded for the week.

**/streak** - Check your personal weekly streak.

**/groupstreak** - Check the group weekly streak.

**/setreminder HH:MM** - Set a personal reminder DM at the time you specify.

**/removereminder HH:MM** - Remove a reminder you previously set.

**/listreminders** - Show all reminders you currently have set.

**/setvloggerrole @Role** - (Admin) Set which role is considered Vloggers.

**/setreminderchannel #Channel** - (Admin) Set the server channel used if DMs are closed.

**/setservertimezone Your/Timezone** - (Admin) Set the server timezone for streak checks.

**/settimezone Your/Timezone** - (Optional) Set your personal timezone.
"""
    await send_response(interaction, help_text)

# ---------- ERROR HANDLER ----------
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    print(f"Error in command {interaction.command.name}: {error}")
    try:
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)
    except:
        pass

# ---------- START ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await init_db()
    await tree.sync()
    weekly_check.start()
    reminder_loop.start()

@bot.event
async def on_guild_join(guild):
    await tree.sync(guild=guild)

bot.run(TOKEN)
