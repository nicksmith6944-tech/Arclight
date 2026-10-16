import os
import base64
import asyncio
import re
import difflib
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional
from collections import defaultdict, deque

import discord
from discord.ext import commands
from dotenv import load_dotenv


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "user_warnings.db")
MOD_ACTIONS_CHANNEL = "✨│mod-actions"
JAILED_ROLE_NAME = "Jailed"
PREFIX = ","

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable is missing."
    )


# ============================================================
# DATABASE
# ============================================================

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_database() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users_per_guild (
                user_id INTEGER NOT NULL,
                warnings_count INTEGER NOT NULL DEFAULT 0,
                guild_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, guild_id)
            );

            CREATE TABLE IF NOT EXISTS mod_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                mod_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT 'No reason provided',
                timestamp DATETIME NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                mod_id INTEGER NOT NULL,
                note TEXT NOT NULL,
                timestamp DATETIME NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_user_notes_lookup
            ON user_notes (guild_id, target_id, timestamp);

            CREATE INDEX IF NOT EXISTS idx_mod_logs_lookup
            ON mod_logs (guild_id, mod_id, action_type, timestamp);

            CREATE TABLE IF NOT EXISTS forced_nicknames (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                nickname TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS jailed_roles (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id, role_id)
            );

            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                modlog_channel_id INTEGER,
                prefix TEXT NOT NULL DEFAULT ',',
                jail_role_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS booster_roles (
                guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, owner_id),
                UNIQUE (guild_id, role_id)
            );

            CREATE TABLE IF NOT EXISTS booster_role_gifts (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS stickies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                stopped_at DATETIME
            );

            CREATE INDEX IF NOT EXISTS idx_stickies_channel_active
            ON stickies (guild_id, channel_id, active);

            CREATE TABLE IF NOT EXISTS disabled_commands (
                guild_id INTEGER NOT NULL,
                command_name TEXT NOT NULL,
                disabled_by INTEGER NOT NULL,
                disabled_at DATETIME NOT NULL,
                PRIMARY KEY (guild_id, command_name)
            );
            """
        )

        # Add the reason column to databases created by older versions.
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(mod_logs)").fetchall()
        }
        if "reason" not in columns:
            conn.execute(
                "ALTER TABLE mod_logs ADD COLUMN reason TEXT NOT NULL DEFAULT 'No reason provided'"
            )

        settings_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(guild_settings)").fetchall()
        }
        if "prefix" not in settings_columns:
            conn.execute(
                "ALTER TABLE guild_settings ADD COLUMN prefix TEXT NOT NULL DEFAULT ','"
            )

        if "jail_role_id" not in settings_columns:
            conn.execute(
                "ALTER TABLE guild_settings ADD COLUMN jail_role_id INTEGER"
            )


def get_active_sticky(guild_id: int, channel_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM stickies WHERE guild_id = ? AND channel_id = ? AND active = 1 ORDER BY id DESC LIMIT 1",
            (guild_id, channel_id),
        ).fetchone()


def create_sticky(guild_id: int, channel_id: int, content: str, created_by: int) -> int:
    with db_connect() as conn:
        conn.execute(
            "UPDATE stickies SET active = 0, stopped_at = ? WHERE guild_id = ? AND channel_id = ? AND active = 1",
            (datetime.now(timezone.utc).isoformat(), guild_id, channel_id),
        )
        cursor = conn.execute(
            "INSERT INTO stickies (guild_id, channel_id, content, active, created_by, created_at) VALUES (?, ?, ?, 1, ?, ?)",
            (guild_id, channel_id, content, created_by, datetime.now(timezone.utc).isoformat()),
        )
        return int(cursor.lastrowid)


def stop_sticky(guild_id: int, channel_id: int) -> bool:
    with db_connect() as conn:
        cursor = conn.execute(
            "UPDATE stickies SET active = 0, stopped_at = ? WHERE guild_id = ? AND channel_id = ? AND active = 1",
            (datetime.now(timezone.utc).isoformat(), guild_id, channel_id),
        )
        return cursor.rowcount > 0


def is_command_disabled(guild_id: int, command_name: str) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM disabled_commands WHERE guild_id = ? AND command_name = ?",
            (guild_id, command_name.casefold()),
        ).fetchone()
    return row is not None


def disable_command(guild_id: int, command_name: str, disabled_by: int) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO disabled_commands (guild_id, command_name, disabled_by, disabled_at) VALUES (?, ?, ?, ?)",
            (guild_id, command_name.casefold(), disabled_by, datetime.now(timezone.utc).isoformat()),
        )


def enable_command(guild_id: int, command_name: str) -> bool:
    with db_connect() as conn:
        cursor = conn.execute(
            "DELETE FROM disabled_commands WHERE guild_id = ? AND command_name = ?",
            (guild_id, command_name.casefold()),
        )
        return cursor.rowcount > 0


def get_guild_prefix(guild: Optional[discord.Guild]) -> str:
    if guild is None:
        return PREFIX
    with db_connect() as conn:
        row = conn.execute(
            "SELECT prefix FROM guild_settings WHERE guild_id = ?",
            (guild.id,),
        ).fetchone()
    return str(row["prefix"]) if row and row["prefix"] else PREFIX


def set_guild_prefix(guild: discord.Guild, prefix: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO guild_settings (guild_id, prefix) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET prefix = excluded.prefix",
            (guild.id, prefix),
        )


def get_command_prefix(bot: commands.Bot, message: discord.Message):
    # Accept a direct mention of the bot, the built-in prefixes, and the
    # server's configured custom prefix. discord.py handles both mention forms.
    custom = get_guild_prefix(message.guild)
    prefixes = [PREFIX, "?"]
    if custom not in prefixes:
        prefixes.append(custom)
    return commands.when_mentioned_or(*prefixes)(bot, message)


def get_modlog_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Return the configured mod-log channel, or the default channel by name."""
    with db_connect() as conn:
        row = conn.execute(
            "SELECT modlog_channel_id FROM guild_settings WHERE guild_id = ?",
            (guild.id,),
        ).fetchone()

    if row and row["modlog_channel_id"]:
        channel = guild.get_channel(int(row["modlog_channel_id"]))
        if isinstance(channel, discord.TextChannel):
            return channel

    return discord.utils.get(guild.text_channels, name=MOD_ACTIONS_CHANNEL)


async def set_modlog_channel(guild: discord.Guild, channel_id: Optional[int]) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO guild_settings (guild_id, modlog_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET modlog_channel_id = excluded.modlog_channel_id
            """,
            (guild.id, channel_id),
        )


def log_moderation_action(
    guild_id: int,
    moderator_id: int,
    target_id: int,
    action_type: str,
    reason: str = "No reason provided",
) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO mod_logs
                (guild_id, mod_id, target_id, action_type, reason, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                moderator_id,
                target_id,
                action_type.lower(),
                reason[:1000],
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def add_warning(guild_id: int, user_id: int) -> int:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users_per_guild (user_id, warnings_count, guild_id)
            VALUES (?, 1, ?)
            ON CONFLICT(user_id, guild_id)
            DO UPDATE SET warnings_count = warnings_count + 1
            """,
            (user_id, guild_id),
        )

        row = conn.execute(
            """
            SELECT warnings_count
            FROM users_per_guild
            WHERE user_id = ? AND guild_id = ?
            """,
            (user_id, guild_id),
        ).fetchone()

        return int(row["warnings_count"])


# ============================================================
# BOT SETUP
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix=get_command_prefix,
    intents=intents,
    case_insensitive=True,
    help_command=None,
)


# Per-guild AFK status. AFK is intentionally in memory and resets when the bot restarts.
afk_users: dict[tuple[int, int], str] = {}

# Keep the latest 200 messages for every channel so deleted messages can be sniped.
message_cache: dict[int, deque] = defaultdict(lambda: deque(maxlen=200))
deleted_messages: dict[int, deque] = defaultdict(lambda: deque(maxlen=200))


# ============================================================
# GENERAL HELPERS
# ============================================================

def parse_duration(value: str) -> Optional[timedelta]:
    """
    Accepted:
        30s = seconds
        10m = minutes
        2h  = hours
        7d  = days
    """
    match = re.fullmatch(r"(\d+)\s*([smhd])", value.lower().strip())

    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)

    units = {
        "s": "seconds",
        "m": "minutes",
        "h": "hours",
        "d": "days",
    }

    duration = timedelta(**{units[unit]: amount})

    if duration <= timedelta(0):
        return None

    return duration


def format_duration(duration: timedelta) -> str:
    seconds = int(duration.total_seconds())

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts = []

    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")

    return " ".join(parts) or "0s"


def clean_text(value: str, maximum: int = 1024) -> str:
    value = str(value)

    if len(value) <= maximum:
        return value

    return value[: maximum - 3] + "..."


def bot_member(guild: discord.Guild) -> Optional[discord.Member]:
    return guild.me


def can_moderate(
    ctx: commands.Context,
    member: discord.Member,
) -> tuple[bool, str]:
    me = bot_member(ctx.guild)

    if me is None:
        return False, "❌ I couldn't determine my member information."

    if member.id == ctx.author.id:
        return False, "❌ You can't use this command on yourself."

    if member.id == ctx.guild.owner_id:
        return False, "❌ You can't moderate the server owner."

    if member.top_role >= me.top_role:
        return False, "❌ That member's highest role is equal to or higher than mine."

    if isinstance(ctx.author, discord.Member):
        if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
            return False, "❌ You can't moderate someone with an equal or higher role than yours."

    return True, ""


async def send_embed(
    ctx: commands.Context,
    message: str,
    *,
    title: Optional[str] = None,
    color: discord.Color = discord.Color.blurple(),
    delete_after: Optional[float] = None,
) -> None:
    embed = discord.Embed(title=title, description=message, color=color)
    await ctx.send(embed=embed, delete_after=delete_after)


async def send_error(ctx: commands.Context, message: str) -> None:
    await send_embed(
        ctx, message, title="❌ Error", color=discord.Color.red(), delete_after=7
    )


async def log_mod_action_channel(
    ctx: commands.Context,
    action: str,
    target,
    reason: str = "No reason provided",
    extra: Optional[str] = None,
) -> None:
    channel = get_modlog_channel(ctx.guild)

    if channel is None:
        return

    embed = discord.Embed(
        title=f"🛡️ {action.upper()}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="Moderator",
        value=f"{ctx.author.mention} (`{ctx.author.id}`)",
        inline=True,
    )

    target_id = getattr(target, "id", None)
    target_mention = getattr(target, "mention", str(target))

    embed.add_field(
        name="Target",
        value=(
            f"{target_mention} (`{target_id}`)"
            if target_id is not None
            else clean_text(str(target))
        ),
        inline=True,
    )

    embed.add_field(
        name="Reason",
        value=clean_text(reason),
        inline=False,
    )

    if extra:
        embed.add_field(
            name="Details",
            value=clean_text(extra),
            inline=False,
        )

    embed.set_footer(text=f"Guild: {ctx.guild.name}")

    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


async def resolve_member(
    guild: discord.Guild,
    target: str,
) -> Optional[discord.Member]:
    target = target.strip()

    # Mention: <@123> or <@!123>
    mention_match = re.fullmatch(r"<@!?(\d+)>", target)

    if mention_match:
        member_id = int(mention_match.group(1))
        return guild.get_member(member_id)

    # ID
    if target.isdigit():
        return guild.get_member(int(target))

    target_lower = target.casefold()

    # Exact username first.
    for member in guild.members:
        if member.name.casefold() == target_lower:
            return member

    # Then exact display name.
    for member in guild.members:
        if member.display_name.casefold() == target_lower:
            return member

    # Finally try "username#1234" for older Discord-style names.
    for member in guild.members:
        full_name = f"{member.name}#{member.discriminator}"
        if full_name.casefold() == target_lower:
            return member

    return None


def get_configured_jail_role(guild: discord.Guild) -> Optional[discord.Role]:
    """Return the jail role configured for this guild, if it still exists."""
    with db_connect() as conn:
        row = conn.execute(
            "SELECT jail_role_id FROM guild_settings WHERE guild_id = ?",
            (guild.id,),
        ).fetchone()

    if not row or not row["jail_role_id"]:
        return None

    return guild.get_role(int(row["jail_role_id"]))


def set_configured_jail_role(guild: discord.Guild, role_id: Optional[int]) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO guild_settings (guild_id, jail_role_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET jail_role_id = excluded.jail_role_id",
            (guild.id, role_id),
        )


def get_booster_role(guild: discord.Guild, owner_id: int) -> Optional[discord.Role]:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT role_id FROM booster_roles WHERE guild_id = ? AND owner_id = ?",
            (guild.id, owner_id),
        ).fetchone()

    if not row:
        return None

    return guild.get_role(int(row["role_id"]))


def save_booster_role(guild: discord.Guild, owner_id: int, role_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO booster_roles (guild_id, owner_id, role_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, owner_id) DO UPDATE SET role_id = excluded.role_id",
            (guild.id, owner_id, role_id),
        )


def count_booster_gifts(guild: discord.Guild, role_id: int) -> int:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM booster_role_gifts WHERE guild_id = ? AND role_id = ?",
            (guild.id, role_id),
        ).fetchone()
    return int(row["count"]) if row else 0


def booster_gift_exists(guild: discord.Guild, role_id: int, user_id: int) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM booster_role_gifts WHERE guild_id = ? AND role_id = ? AND user_id = ?",
            (guild.id, role_id, user_id),
        ).fetchone()
    return row is not None


def save_booster_gift(guild: discord.Guild, role_id: int, user_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO booster_role_gifts (guild_id, role_id, user_id) VALUES (?, ?, ?)",
            (guild.id, role_id, user_id),
        )


# ============================================================
# AFK / SNIPE / MODERATION DM HELPERS
# ============================================================

async def send_moderation_dm(
    target: discord.abc.User,
    guild: discord.Guild,
    action: str,
    reason: str = "No reason provided",
) -> bool:
    """Try to DM the target and return True if Discord accepted the message."""
    reason = reason.strip() or "No reason provided"
    try:
        embed = discord.Embed(
            title=f"🛡️ Moderation Action: {action.title()}",
            description=(
                f"👋 {target.mention}, you have been **{action}** in "
                f"**{guild.name}** for **{reason}**."
            ),
            color=discord.Color.blurple(),
        )
        await target.send(embed=embed)
        return True
    except discord.Forbidden:
        print(f"⚠️ Could not DM {target} ({target.id}): DMs are closed or the bot is blocked.")
        return False
    except discord.HTTPException as error:
        print(f"⚠️ Could not DM {target} ({target.id}): {error}")
        return False


def cache_message(message: discord.Message) -> None:
    if message.guild is None:
        return
    message_cache[message.channel.id].append(message)


@bot.check
async def check_disabled_command(ctx: commands.Context) -> bool:
    if ctx.guild is None or ctx.command is None:
        return True

    command_name = ctx.command.qualified_name.casefold()
    if command_name in {"disable", "enable"}:
        return True

    if is_command_disabled(ctx.guild.id, command_name):
        return False

    return True


# ============================================================
# EVENTS
# ============================================================
async def refresh_sticky(channel: discord.TextChannel, sticky: sqlite3.Row) -> Optional[discord.Message]:
    """Delete the previous sticky copy and send the sticky text again as a normal message."""
    try:
        async for message in channel.history(limit=25):
            if message.author.id == bot.user.id and message.content == sticky["content"]:
                try:
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                break

        return await channel.send(sticky["content"])
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"⚠️ Could not refresh sticky in #{channel.name}: {error}")
        return None


async def refresh_active_sticky(channel: discord.TextChannel) -> None:
    sticky = get_active_sticky(channel.guild.id, channel.id)
    if sticky is not None:
        await refresh_sticky(channel, sticky)



@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online!")
    print(f"📡 Connected to {len(bot.guilds)} guild(s).")
    print(f"⌨️ Default prefix: {PREFIX!r}")
    print(f"🧠 Message Content Intent in code: {bot.intents.message_content}")

    # Start the console sender only after Discord is connected.
    # This guarantees the worker has a live bot connection before it tries
    # to send queued messages. The guard prevents duplicate workers on
    # Discord reconnects.
    if not getattr(bot, "_console_sender_started", False):
        bot._console_sender_started = True
        bot._console_sender_task = asyncio.create_task(console_sender_worker())
        print("🖥️ Railway console sender started!")

    if not getattr(bot, "_slash_commands_synced", False):
        try:
            synced = await bot.tree.sync()
            bot._slash_commands_synced = True
            print(f"🔄 Synced {len(synced)} slash command(s).")
        except discord.HTTPException as error:
            print(f"⚠️ Failed to sync slash commands: {error}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    cache_message(message)

    try:
        if message.guild is not None:
            removed_afk_reason = afk_users.pop(
                (message.guild.id, message.author.id), None
            )
            if removed_afk_reason is not None:
                try:
                    embed = discord.Embed(
                        title="👋 Welcome Back!",
                        description=(
                            f"{message.author.mention}, your AFK status has been removed."
                        ),
                        color=discord.Color.green(),
                    )
                    await message.channel.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException) as error:
                    print(
                        f"⚠️ Could not send AFK removal message in "
                        f"{message.channel}: {error}"
                    )

            mentioned_afk: list[tuple[int, str]] = []

            for member in message.mentions:
                reason = afk_users.get((message.guild.id, member.id))
                if reason is not None and member.id != message.author.id:
                    mentioned_afk.append((member.id, reason))

            if message.reference is not None and message.reference.message_id:
                referenced = message.reference.resolved

                if not isinstance(referenced, discord.Message):
                    for cached in reversed(message_cache.get(message.channel.id, ())):
                        if cached.id == message.reference.message_id:
                            referenced = cached
                            break

                if isinstance(referenced, discord.Message):
                    referenced_author = referenced.author
                    reason = afk_users.get((message.guild.id, referenced_author.id))
                    if reason is not None and referenced_author.id != message.author.id:
                        if all(uid != referenced_author.id for uid, _ in mentioned_afk):
                            mentioned_afk.append((referenced_author.id, reason))

            for user_id, reason in mentioned_afk:
                try:
                    embed = discord.Embed(
                        description=f"<@{user_id}> is AFK: **{reason}**",
                        color=discord.Color.blurple(),
                    )
                    await message.reply(embed=embed, mention_author=False)
                except (discord.Forbidden, discord.HTTPException) as error:
                    print(f"⚠️ Could not send AFK reply in {message.channel}: {error}")
                except Exception as error:
                    print(f"⚠️ Unexpected AFK handler error: {error!r}")
    except Exception as error:
        print(f"⚠️ Unexpected on_message error: {error!r}")
    finally:
        await bot.process_commands(message)
        if message.guild is not None and isinstance(message.channel, discord.TextChannel):
            await refresh_active_sticky(message.channel)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.guild_id is None:
        return

    cached = message_cache.get(payload.channel_id)
    if not cached:
        return

    for message in reversed(cached):
        if message.id == payload.message_id:
            deleted_messages[payload.channel_id].appendleft(message)
            break


@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    """Save messages removed by Discord's bulk-delete endpoint for snipe."""
    if payload.guild_id is None:
        return

    cached = message_cache.get(payload.channel_id)
    if not cached:
        return

    deleted_ids = set(payload.message_ids)
    for message in reversed(cached):
        if message.id in deleted_ids:
            deleted_messages[payload.channel_id].appendleft(message)


@bot.hybrid_command(name="stick", description="Stick a normal message to a channel.")
@commands.guild_only()
@commands.has_guild_permissions(manage_messages=True)
@commands.bot_has_permissions(send_messages=True, manage_messages=True)
async def stick(ctx: commands.Context, *, arguments: str):
    """Create a sticky message. A final channel mention is optional."""
    arguments = arguments.strip()
    target_channel = ctx.channel

    channel_match = re.search(r"\s*<#(\d+)>\s*$", arguments)
    if channel_match:
        target_channel = ctx.guild.get_channel(int(channel_match.group(1)))
        arguments = arguments[:channel_match.start()].rstrip()

    if not isinstance(target_channel, discord.TextChannel):
        await send_error(ctx, "❌ That isn't a text channel I can use.")
        return

    if not arguments:
        await send_error(ctx, "❌ The sticky message cannot be empty.")
        return

    if len(arguments) > 2000:
        await send_error(ctx, "❌ Sticky messages must be 2000 characters or fewer.")
        return

    me = ctx.guild.me
    permissions = target_channel.permissions_for(me) if me else None
    if permissions is None or not permissions.send_messages or not permissions.manage_messages:
        await send_error(ctx, f"❌ I need Send Messages and Manage Messages in {target_channel.mention}.")
        return

    sticky_id = create_sticky(ctx.guild.id, target_channel.id, arguments, ctx.author.id)
    sticky = get_active_sticky(ctx.guild.id, target_channel.id)
    sent = await refresh_sticky(target_channel, sticky) if sticky else None

    if sent is None:
        stop_sticky(ctx.guild.id, target_channel.id)
        await send_error(ctx, f"❌ I couldn't send the sticky message to {target_channel.mention}.")
        return

    await send_embed(
        ctx,
        f"📌 Sticky message `{sticky_id}` is now active in {target_channel.mention}.",
        title="📌 Sticky Enabled",
        color=discord.Color.green(),
        delete_after=7,
    )


@bot.hybrid_command(name="unstick", description="Stop the active sticky message in this channel.")
@commands.guild_only()
@commands.has_guild_permissions(manage_messages=True)
async def unstick(ctx: commands.Context):
    if not stop_sticky(ctx.guild.id, ctx.channel.id):
        await send_error(ctx, "❌ There is no active sticky message in this channel.")
        return

    await send_embed(
        ctx,
        "📌 The sticky message has been stopped.",
        title="📌 Sticky Stopped",
        color=discord.Color.green(),
        delete_after=7,
    )


@bot.hybrid_command(name="getsticks", description="Show active and stopped sticky messages in this server.")
@commands.guild_only()
@commands.has_guild_permissions(manage_messages=True)
async def getsticks(ctx: commands.Context):
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM stickies WHERE guild_id = ? ORDER BY id DESC",
            (ctx.guild.id,),
        ).fetchall()

    if not rows:
        await send_embed(ctx, "No sticky messages have been created in this server.", title="📌 Stickies")
        return

    lines = []
    for row in rows:
        channel = ctx.guild.get_channel(int(row["channel_id"]))
        channel_name = channel.mention if channel else f"Deleted channel ({row['channel_id']})"
        status = "🟢 Active" if row["active"] else "🔴 Stopped"
        preview = clean_text(row["content"].replace("\n", " "), 100)
        lines.append(f"`#{row['id']}` {status} • {channel_name} • `{preview}`")

    embed = discord.Embed(
        title="📌 Server Stickies",
        description="\n".join(lines)[:4096],
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed)


@bot.hybrid_command(name="disable", description="Disable a command in this server.")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def disable(ctx: commands.Context, *, command_name: str):
    command_name = command_name.strip().lstrip("/").casefold()
    command = bot.get_command(command_name)

    if command is None:
        await send_error(ctx, f"❌ I couldn't find a command named `{command_name}`.")
        return

    if command.qualified_name.casefold() == "disable":
        await send_error(ctx, "❌ You can't disable the `disable` command.")
        return

    if is_command_disabled(ctx.guild.id, command.qualified_name):
        await send_error(ctx, f"❌ The `{command.qualified_name}` command is already disabled in this server.")
        return

    disable_command(ctx.guild.id, command.qualified_name, ctx.author.id)
    await send_embed(
        ctx,
        f"🔒 The `{command.qualified_name}` command is now disabled in this server.",
        title="🔒 Command Disabled",
        color=discord.Color.green(),
        delete_after=7,
    )


@bot.hybrid_command(name="enable", description="Re-enable a command in this server.")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def enable(ctx: commands.Context, *, command_name: str):
    command_name = command_name.strip().lstrip("/").casefold()
    command = bot.get_command(command_name)

    if command is None:
        await send_error(ctx, f"❌ I couldn't find a command named `{command_name}`.")
        return

    if command.qualified_name.casefold() in {"disable", "enable"}:
        await send_error(ctx, "❌ You can't disable or enable the command-management commands.")
        return

    if not is_command_disabled(ctx.guild.id, command.qualified_name):
        await send_error(ctx, f"❌ The `{command.qualified_name}` command is not disabled in this server.")
        return

    enable_command(ctx.guild.id, command.qualified_name)
    await send_embed(
        ctx,
        f"🔓 The `{command.qualified_name}` command is now enabled in this server.",
        title="🔓 Command Enabled",
        color=discord.Color.green(),
        delete_after=7,
    )


@bot.hybrid_command(description="Set your AFK status with an optional reason.")
@commands.guild_only()
async def afk(ctx: commands.Context, *, reason: str = "afk"):
    reason = reason.strip() or "afk"
    afk_users[(ctx.guild.id, ctx.author.id)] = reason
    await send_embed(ctx, f"{ctx.author.mention} is now AFK: **{reason}**", title="💤 AFK Enabled")


@bot.hybrid_command(description="Show a recently deleted message from this channel.")
@commands.guild_only()
async def snipe(ctx: commands.Context, number: int = 1):
    if number < 1:
        await send_error(ctx, "❌ The snipe number must be 1 or higher.")
        return

    history = deleted_messages.get(ctx.channel.id)
    if not history or number > len(history):
        await send_error(ctx, f"❌ There aren't that many deleted messages saved. Available: `{len(history) if history else 0}`.")
        return

    message = history[number - 1]
    embed = discord.Embed(
        description=message.content or "*No text content*",
        color=discord.Color.blurple(),
        timestamp=message.created_at,
    )
    embed.set_author(
        name=message.author.display_name,
        icon_url=message.author.display_avatar.url,
    )
    embed.set_footer(text=f"Snipe #{number} • Message ID: {message.id}")

    if message.attachments:
        embed.add_field(
            name="Attachments",
            value="\n".join(a.url for a in message.attachments)[:1024],
            inline=False,
        )

    await ctx.send(embed=embed)

@bot.hybrid_command(name="announce", description="Send an announcement to a selected text channel.")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(send_messages=True)
async def announce(
    ctx: commands.Context,
    channel: discord.TextChannel,
    *,
    text: str,
):
    """Send an announcement to the selected text channel."""
    text = text.strip()

    if not text:
        await send_error(ctx, "❌ The announcement text cannot be empty.")
        return

    embed = discord.Embed(
        title="📢 Announcement",
        description=text,
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )

    if ctx.guild.icon:
        embed.set_author(
            name=ctx.guild.name,
            icon_url=ctx.guild.icon.url,
        )
    else:
        embed.set_author(name=ctx.guild.name)

    embed.set_footer(
        text=f"Announced by {ctx.author.display_name}",
        icon_url=ctx.author.display_avatar.url,
    )

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        await send_error(ctx, f"❌ I can't send messages in {channel.mention}.")
        return
    except discord.HTTPException as error:
        await send_error(ctx, f"❌ Discord rejected the announcement: `{error}`")
        return

    await send_embed(
        ctx,
        f"📢 Announcement sent to {channel.mention}.",
        title="📢 Announcement Sent",
        color=discord.Color.green(),
        delete_after=7,
    )


@bot.hybrid_command(name="cs", description="Clear all saved snipes for this channel.")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def cs(ctx: commands.Context):
    """Clear all saved deleted-message snipes for the current channel."""
    history = deleted_messages.get(ctx.channel.id)
    cleared = len(history) if history else 0

    if history:
        history.clear()

    await send_embed(
        ctx,
        f"🗑️ Cleared `{cleared}` saved snipe(s) from {ctx.channel.mention}.",
        title="🧹 Snipes Cleared",
        color=discord.Color.green(),
        delete_after=7,
    )



@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    # Local command-specific handlers are preferred.
    if hasattr(ctx.command, "on_error"):
        return

    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.CheckFailure) and ctx.guild is not None and ctx.command is not None:
        command_name = ctx.command.qualified_name.casefold()
        if is_command_disabled(ctx.guild.id, command_name):
            await send_error(ctx, f"❌ The `{command_name}` command is disabled in this server.")
            return

    if isinstance(error, commands.MissingPermissions):
        await send_error(
            ctx,
            f"❌ You need: `{', '.join(error.missing_permissions)}`.",
        )
        return

    if isinstance(error, commands.BotMissingPermissions):
        await send_error(
            ctx,
            f"❌ I need: `{', '.join(error.missing_permissions)}`.",
        )
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await send_error(
            ctx,
            f"❌ Missing argument: `{error.param.name}`.\n"
            f"Use `{PREFIX}help` for the command format.",
        )
        return

    if isinstance(error, commands.BadArgument):
        await send_error(
            ctx,
            "❌ I couldn't understand one of those arguments.",
        )
        return

    if isinstance(error, commands.NoPrivateMessage):
        await send_error(ctx, "❌ This command can only be used in a server.")
        return

    if isinstance(error, commands.CommandOnCooldown):
        await send_error(
            ctx,
            f"⏳ Try again in `{error.retry_after:.1f}s`.",
        )
        return

    if isinstance(error, commands.CheckFailure):
        await send_error(ctx, "❌ You don't have permission to use that command.")
        return

    print(f"Unhandled command error in {ctx.command}: {repr(error)}")
    await send_error(ctx, "❌ Something went wrong while running that command.")


@bot.event
async def on_member_update(
    before: discord.Member,
    after: discord.Member,
):
    # Only react when the nickname actually changes.
    if before.nick == after.nick:
        return

    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT nickname
            FROM forced_nicknames
            WHERE guild_id = ? AND user_id = ?
            """,
            (after.guild.id, after.id),
        ).fetchone()

    if row is None:
        return

    forced_nickname = row["nickname"]

    if after.nick == forced_nickname:
        return

    me = after.guild.me

    if me is None or after.top_role >= me.top_role:
        return

    try:
        await after.edit(
            nick=forced_nickname,
            reason="Restoring forced nickname",
        )
    except (discord.Forbidden, discord.HTTPException):
        pass


# ============================================================
# HELP
# ============================================================

@bot.hybrid_command(name="help", description="Show the moderation bot help menu.")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="📖 Moderation Bot — Help",
        description=(
            f"Prefix: `{get_guild_prefix(ctx.guild)}`\n"
            f"Mention prefix: {bot.user.mention if bot.user else '@ArcLight'}\n\n"
            "**Arguments:** `<required>` • `(optional)`"
        ),
        color=discord.Color.blurple(),
    )

    commands_list = [
        (
            "🧹 Purge",
            f"`{get_guild_prefix(ctx.guild)}purge` — Delete 1 recent message.\n"
            f"`{get_guild_prefix(ctx.guild)}purge <amount>` — Delete that many recent messages.\n"
            f"`{get_guild_prefix(ctx.guild)}purge <amount> <user_id>` — Delete that many messages from one user.\n"
            f"`{get_guild_prefix(ctx.guild)}purge <user_id>` — Delete 1 recent message from one user.",
        ),
        (
            "🧹 Clean",
            f"`{get_guild_prefix(ctx.guild)}clean`\n"
            "Deletes the 10 most recent bot messages in the channel.",
        ),
        (
            "⚠️ Warn",
            f"`{get_guild_prefix(ctx.guild)}warn <user> (reason)`\n"
            "Warns a member and increases their warning count.\n"
            f"`{get_guild_prefix(ctx.guild)}warns <user>`\n"
            "Shows a member's warning count and recent warnings.\n"
            f"`{get_guild_prefix(ctx.guild)}modlogs <user>`\n"
            "Shows a member's recent moderation history.",
        ),
        (
            "📝 Notes",
            f"`{get_guild_prefix(ctx.guild)}note <user> <note>`\n"
            "Adds a persistent moderator note without affecting moderation stats.\n"
            f"`{get_guild_prefix(ctx.guild)}notes <user>`\n"
            "Shows all notes, when they were added, and which moderator added them.",
        ),
        (
            "🔇 Mute",
            f"`{get_guild_prefix(ctx.guild)}mute <user> <duration> (reason)`\n"
            "Times out a member. Duration: `30s`, `10m`, `2h`, `7d`.",
        ),
        (
            "🔊 Unmute",
            f"`{get_guild_prefix(ctx.guild)}unmute <user> (reason)`\n"
            "Removes a member's timeout.",
        ),
        (
            "🔒 Jail",
            f"`{get_guild_prefix(ctx.guild)}jail <user> (reason)`\n"
            "Removes the member's roles and gives them the configured jail role.\n"
            f"`{get_guild_prefix(ctx.guild)}setjailrole <@role>` — Set the jail role (Manage Server).",
        ),
        (
            "🔓 Unjail",
            f"`{get_guild_prefix(ctx.guild)}unjail <user> (reason)`\n"
            "Removes Jailed and restores the roles saved during jail.",
        ),
        (
            "🔨 Ban",
            f"`{get_guild_prefix(ctx.guild)}ban <user> (reason)`\n"
            "Permanently bans a member.",
        ),
        (
            "🔓 Unban",
            f"`{get_guild_prefix(ctx.guild)}unban <user_id> (reason)`\n"
            "Unbans a user by ID.",
        ),
        (
            "👢 Kick",
            f"`{get_guild_prefix(ctx.guild)}kick <user> (reason)`\n"
            "Kicks a member from the server.",
        ),
        (
            "📌 Stickies",
            f"`{get_guild_prefix(ctx.guild)}stick <message> (channel)` — Stick a normal message to a channel.\n"
            f"`{get_guild_prefix(ctx.guild)}unstick` — Stop the active sticky in this channel.\n"
            f"`{get_guild_prefix(ctx.guild)}getsticks` — Show active and stopped stickies in the server.",
        ),
        (
            "🔒 Command Control",
            f"`{get_guild_prefix(ctx.guild)}disable <cmd name>` — Disable a command in this server (Manage Server).\n"
            f"`{get_guild_prefix(ctx.guild)}enable <cmd name>` — Re-enable a disabled command (Manage Server).",
        ),
        (
            "🎁 Booster Roles",
            f"`{get_guild_prefix(ctx.guild)}br <role name>` — Create/rename your booster role.\n"
            f"`{get_guild_prefix(ctx.guild)}br icon <emoji>` — Set its role icon.\n"
            f"`{get_guild_prefix(ctx.guild)}br color <hex>` — Set its color, e.g. `f30606`.\n"
            f"`{get_guild_prefix(ctx.guild)}br gift <member>` — Gift it to up to 4 members.",
        ),
        (
            "🎵 Music",
            f"`{get_guild_prefix(ctx.guild)}join` — Join the voice channel you're in.\n"
            f"`{get_guild_prefix(ctx.guild)}leave` — Leave voice (you must be in the same VC as the bot).\n"
            f"`{get_guild_prefix(ctx.guild)}play <song>` — Search and play a song in your VC; joins automatically if disconnected.\n"
            f"`{get_guild_prefix(ctx.guild)}p <url>` — Play a direct media URL or Spotify track/album/playlist link.\n"
            f"`{get_guild_prefix(ctx.guild)}pause` / `{get_guild_prefix(ctx.guild)}unpause` — Pause or resume.\n"
            f"`{get_guild_prefix(ctx.guild)}skip` / `{get_guild_prefix(ctx.guild)}previous` — Change tracks.\n"
            f"`{get_guild_prefix(ctx.guild)}queue` — Show the current track and upcoming songs.\n"
            f"`{get_guild_prefix(ctx.guild)}shuffle` — Shuffle the upcoming music queue.",
        ),
        (
            "⚙️ Prefix",
            f"`{get_guild_prefix(ctx.guild)}setprefix <prefix>` — Change the bot prefix.\n"
            f"`{get_guild_prefix(ctx.guild)}setprefix default` — Reset it to `,`.",
        ),
        (
            "📋 Mod-Log Channel",
            f"`{get_guild_prefix(ctx.guild)}setlogs #channel` — Set the moderation log channel.\n"
            f"`{get_guild_prefix(ctx.guild)}setlogs off` — Disable the custom channel.\n"
            f"`{get_guild_prefix(ctx.guild)}logs` — Show the current log channel.",
        ),
        (
            "📊 Moderation Stats",
            f"`{get_guild_prefix(ctx.guild)}ms`\n"
            f"`{get_guild_prefix(ctx.guild)}ms <user_id>`\n"
            "Shows 7-day, 30-day and all-time moderation statistics.",
        ),
        (
            "🏷️ Force Nickname",
            f"`{get_guild_prefix(ctx.guild)}forcenick <user> <nickname>`\n"
            "Sets and continuously enforces a nickname.",
        ),
        (
            "🏷️ Remove Forced Nickname",
            f"`{get_guild_prefix(ctx.guild)}unforcenick <user>`\n"
            "Removes the nickname lock.",
        ),
        (
            "📢 Announce",
            f"`{get_guild_prefix(ctx.guild)}announce #channel <text>`\n"
            "Sends an announcement to the selected text channel.",
        ),
        (
            "🧹 Clear Snipes",
            f"`{get_guild_prefix(ctx.guild)}cs`\n"
            "Clears all saved deleted-message snipes in the current channel.",
        ),
    ]

    for name, value in commands_list:
        embed.add_field(name=name, value=value, inline=False)

    embed.set_footer(text=f"Requested by {ctx.author.display_name}")

    await ctx.send(embed=embed)


# ============================================================
# PING
# ============================================================

@bot.hybrid_command(description="Check the bot's latency (ping).")
async def ping(ctx: commands.Context):
    """Check the bot's latency (ping)."""
    latency_ms = round(bot.latency * 1000)
    await send_embed(ctx, f"🏓 Pong! Bot ping: `{latency_ms}ms`", title="🏓 Pong!")


# ============================================================
# CLEAN
# ============================================================

@bot.hybrid_command(description="Delete the 10 most recent bot messages in this channel.")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True)
async def clean(ctx: commands.Context):
    deleted = []

    async for message in ctx.channel.history(limit=500):
        if message.author.bot:
            deleted.append(message)

            if len(deleted) >= 10:
                break

    if deleted:
        try:
            await ctx.channel.delete_messages(deleted)
        except discord.HTTPException:
            # Fallback for any Discord bulk-delete issue.
            for message in deleted:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass

    if ctx.message is not None:
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

    await log_mod_action_channel(
        ctx,
        "Clean",
        ctx.author,
        "Bot messages cleaned",
        extra=f"Bot messages deleted: `{len(deleted)}`",
    )


# ============================================================
# PURGE
# ============================================================

@bot.hybrid_command(
    name="purge",
    description="Delete a number of recent messages, optionally from one user.",
)
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True)
async def purge(ctx: commands.Context, *, arguments: str = ""):
    """
    Usage:
      ?purge                 -> delete 1 recent message
      ?purge 10              -> delete 10 recent messages
      ?purge 10 123456789012345678 -> delete up to 10 messages from that user
      ?purge 123456789012345678    -> delete that user's most recent message
      ?purge @user           -> delete that user's most recent message
    """
    tokens = arguments.split()
    amount = 1
    target_id = None

    def parse_user_id(value: str) -> Optional[int]:
        match = re.fullmatch(r"<@!?(\d+)>", value)
        if match:
            return int(match.group(1))
        if value.isdigit() and len(value) >= 15:
            return int(value)
        return None

    if tokens:
        first_user_id = parse_user_id(tokens[0])
        if first_user_id is not None:
            target_id = first_user_id
            if len(tokens) > 1:
                await send_error(ctx, "❌ Usage: `?purge [amount] [user_id]` or `?purge user_id`.")
                return
        else:
            try:
                amount = int(tokens[0])
            except ValueError:
                await send_error(ctx, "❌ Amount must be a positive number.")
                return

            if amount < 1:
                await send_error(ctx, "❌ Amount must be greater than 0.")
                return
            if amount > 1000:
                await send_error(ctx, "❌ You can purge at most 1000 messages at a time.")
                return

            if len(tokens) >= 2:
                target_id = parse_user_id(tokens[1])
                if target_id is None:
                    await send_error(ctx, "❌ The user ID must be a valid Discord ID or user mention.")
                    return
            if len(tokens) > 2:
                await send_error(ctx, "❌ Usage: `?purge [amount] [user_id]`.")
                return

    deleted_count = 0
    # Keep the invoking command out of the count; delete it separately below.
    try:
        async for message in ctx.channel.history(limit=None):
            if message.id == getattr(ctx.message, "id", None):
                continue
            if target_id is not None and message.author.id != target_id:
                continue

            try:
                await message.delete()
                deleted_count += 1
            except discord.NotFound:
                pass
            except discord.Forbidden:
                await send_error(ctx, "❌ I don't have permission to delete messages in this channel.")
                return
            except discord.HTTPException:
                pass

            if deleted_count >= amount:
                break
    except discord.Forbidden:
        await send_error(ctx, "❌ I don't have permission to read channel history.")
        return

    if ctx.message is not None:
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

    target_text = f" from <@{target_id}>" if target_id is not None else ""
    await send_embed(
        ctx,
        f"🧹 Deleted `{deleted_count}` message(s){target_text}.\n"
        f"Responsible Moderator: {ctx.author.mention}",
        title="🧹 Purge Complete",
        color=discord.Color.green(),
        delete_after=7,
    )

    log_moderation_action(
        ctx.guild.id,
        ctx.author.id,
        target_id or ctx.author.id,
        "purge",
        f"Messages purged: {deleted_count}; target user ID: {target_id or 'any user'}",
    )
    await log_mod_action_channel(
        ctx,
        "Purge",
        ctx.guild.get_member(target_id) if target_id and ctx.guild.get_member(target_id) else (f"User ID {target_id}" if target_id else "All users"),
        "Messages purged",
        extra=f"Messages deleted: `{deleted_count}`",
    )


# ============================================================
# MUSIC (YouTube search + Discord voice playback)
# ============================================================

# Requires: discord.py[voice], PyNaCl, yt-dlp, and FFmpeg installed on the host.
# Search is performed against YouTube results. Spotify links are not streamed
# directly; Spotify's API does not provide playable audio URLs for Discord VC.
music_sessions: dict[int, dict] = {}


def get_music_session(guild_id: int) -> dict:
    if guild_id not in music_sessions:
        music_sessions[guild_id] = {
            "queue": deque(),
            "history": deque(maxlen=50),
            "current": None,
            "generation": 0,
        }
    return music_sessions[guild_id]


def normalize_song_text(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()



# Spotify URL support uses the official Web API for metadata only.
# Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in the hosting panel.
_spotify_token_cache = {"access_token": None, "expires_at": 0}


def spotify_api_get(url: str):
    """GET Spotify Web API JSON using the client-credentials flow."""
    import json
    import time
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Spotify links need SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET "
            "set as environment variables on the hosting panel."
        )

    token = _spotify_token_cache.get("access_token")
    if not token or time.time() >= _spotify_token_cache.get("expires_at", 0):
        credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        token_request = Request(
            "https://accounts.spotify.com/api/token",
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urlopen(token_request, timeout=15) as response:
                token_data = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise RuntimeError(
                "Couldn't authenticate with Spotify. Check the Client ID/Secret and app access."
            ) from error
        token = token_data.get("access_token")
        if not token:
            raise RuntimeError("Spotify didn't return an access token for this app.")
        _spotify_token_cache["access_token"] = token
        _spotify_token_cache["expires_at"] = time.time() + int(token_data.get("expires_in", 3600)) - 60

    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code == 401:
            _spotify_token_cache["access_token"] = None
            raise RuntimeError("Spotify authentication expired; try the command again.") from error
        if error.code == 403:
            raise RuntimeError(
                "Spotify denied access to this item. Make sure the track/playlist is public "
                "and your developer app has API access."
            ) from error
        if error.code == 404:
            raise RuntimeError("That Spotify track, album, or playlist could not be found.") from error
        raise RuntimeError(f"Spotify API returned HTTP {error.code}.") from error
    except URLError as error:
        raise RuntimeError("Couldn't reach Spotify's API from this host.") from error


def spotify_tracks_from_url(value: str):
    """
    Return Spotify metadata for a track, album, or playlist URL.
    Returns None for non-Spotify inputs. This does not provide audio streams.
    """
    match = re.search(
        r"(?:https?://)?open\.spotify\.com/(track|album|playlist)/([A-Za-z0-9]+)",
        value.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None

    kind, spotify_id = match.group(1).lower(), match.group(2)
    if kind == "track":
        item = spotify_api_get(f"https://api.spotify.com/v1/tracks/{spotify_id}")
        items = [item]
    elif kind == "album":
        album = spotify_api_get(f"https://api.spotify.com/v1/albums/{spotify_id}")
        album_tracks = spotify_api_get(
            f"https://api.spotify.com/v1/albums/{spotify_id}/tracks?limit=50"
        )
        items = album_tracks.get("items", [])
        for item in items:
            item.setdefault("album", {"name": album.get("name", "")})
    else:
        playlist = spotify_api_get(f"https://api.spotify.com/v1/playlists/{spotify_id}/tracks?limit=50")
        items = [entry.get("track") for entry in playlist.get("items", []) if entry.get("track")]

    tracks = []
    for item in items:
        if not item or item.get("type", "track") != "track" or not item.get("name"):
            continue
        artists = ", ".join(
            artist.get("name", "") for artist in item.get("artists", []) if artist.get("name")
        )
        if item.get("is_local"):
            continue
        tracks.append({
            "title": item["name"],
            "artists": artists or "Unknown artist",
            "spotify_url": item.get("external_urls", {}).get("spotify", value),
        })
        # Avoid turning a giant playlist into hundreds of extraction attempts.
        if len(tracks) >= 25:
            break

    if not tracks:
        raise LookupError("I couldn't find any playable track metadata in that Spotify link.")
    return tracks


def search_youtube_song(query: str) -> dict:
    """Search YouTube, rank likely title matches, and skip blocked/unplayable results."""
    try:
        import yt_dlp
    except ImportError as error:
        raise RuntimeError("yt-dlp is not installed. Add `yt-dlp` to requirements.txt and redeploy.") from error

    # If the user supplies a URL, extract that URL directly instead of
    # searching YouTube for the URL as if it were a song title.
    direct_url = query.strip()
    is_url = bool(re.match(r"^https?://", direct_url, re.IGNORECASE))

    # Shared options for resolving a playable audio stream.
    stream_options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "bestaudio/best",
        "noplaylist": True,
        # These are normal extractor preferences, not authentication credentials.
        "extractor_args": {"youtube": {"player_client": ["mweb", "android_vr"]}},
    }

    if is_url:
        try:
            with yt_dlp.YoutubeDL(stream_options) as ydl:
                full = ydl.extract_info(direct_url, download=False)
            if not full:
                raise LookupError("I couldn't resolve that link into a playable track.")
            formats = full.get("formats", [])
            audio_formats = [
                item for item in formats
                if item.get("url") and item.get("acodec") not in (None, "none")
            ]
            audio_url = (
                max(audio_formats, key=lambda item: item.get("abr") or item.get("tbr") or 0)["url"]
                if audio_formats else full.get("url")
            )
            if not audio_url:
                raise LookupError("That link resolved, but it doesn't provide a playable audio stream.")
            return {
                "title": full.get("title", direct_url),
                "url": audio_url,
                "webpage_url": full.get("webpage_url") or direct_url,
                "duration": full.get("duration"),
                "requester": None,
            }
        except (LookupError, RuntimeError):
            raise
        except Exception as error:
            raise LookupError(f"Couldn't play that link directly: {str(error)[:220]}") from error

    # Keep search resilient: one blocked result should not abort all candidates.
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "default_search": "ytsearch8",
        "noplaylist": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"ytsearch8:{query}", download=False)

    entries = [entry for entry in (info or {}).get("entries", []) if entry and entry.get("id")]
    if not entries:
        raise LookupError("YouTube returned no usable search results. It may be temporarily blocking requests from this host.")

    wanted = normalize_song_text(query)

    def score(entry):
        title = normalize_song_text(entry.get("title", ""))
        uploader = normalize_song_text(entry.get("uploader") or entry.get("channel") or "")
        title_ratio = difflib.SequenceMatcher(None, wanted, title).ratio()
        combined_ratio = difflib.SequenceMatcher(None, wanted, f"{title} {uploader}").ratio()
        wanted_words = set(wanted.split())
        title_words = set(title.split())
        overlap = len(wanted_words & title_words) / max(1, len(wanted_words))
        return (0.72 * title_ratio) + (0.18 * combined_ratio) + (0.10 * overlap)

    # Try the closest title matches first. YouTube may reject one video while
    # allowing another, so don't fail the entire request on the first error.
    entries.sort(key=score, reverse=True)
    failures = []
    for candidate in entries:
        video_id = candidate.get("id")
        webpage_url = candidate.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(stream_options) as ydl:
                full = ydl.extract_info(webpage_url, download=False)
            if not full:
                continue
            formats = full.get("formats", [])
            audio_formats = [
                item for item in formats
                if item.get("url") and item.get("acodec") not in (None, "none")
            ]
            audio_url = (
                max(audio_formats, key=lambda item: item.get("abr") or item.get("tbr") or 0)["url"]
                if audio_formats else full.get("url")
            )
            if not audio_url:
                continue
            return {
                "title": full.get("title", candidate.get("title", "Unknown title")),
                "url": audio_url,
                "webpage_url": full.get("webpage_url") or webpage_url,
                "duration": full.get("duration"),
                "requester": None,
            }
        except Exception as error:
            failures.append(str(error).replace("\n", " ")[:220])
            continue

    if failures and any("Sign in to confirm" in failure or "not a bot" in failure.lower() for failure in failures):
        raise RuntimeError(
            "YouTube is blocking audio extraction from this hosting server. I tried multiple search results, "
            "but they were rejected. This needs a supported alternative audio source or valid server-side "
            "authentication; reinstalling yt-dlp alone won't fix it."
        )
    raise LookupError("I found search results, but none could be played. Try another query or audio source.")


def format_track_duration(seconds) -> str:
    if not seconds:
        return "Live/unknown length"
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


async def start_music_track(guild: discord.Guild, track: dict, *, remember_current: bool = True):
    session = get_music_session(guild.id)
    voice = guild.voice_client

    if voice is None or not voice.is_connected():
        session["current"] = None
        return

    previous_track = session.get("current")
    if remember_current and previous_track is not None:
        session["history"].append(previous_track)

    session["generation"] += 1
    generation = session["generation"]
    session["current"] = track

    audio_source = discord.FFmpegPCMAudio(
        track["url"],
        before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        options="-vn",
    )

    def after_playback(error):
        if error:
            print(f"Music playback error in guild {guild.id}: {error!r}")

        def schedule_next():
            asyncio.run_coroutine_threadsafe(advance_music_queue(guild.id, generation), bot.loop)

        # Ignore callbacks from a track that was manually replaced/stopped.
        if session.get("generation") == generation:
            schedule_next()

    try:
        voice.play(audio_source, after=after_playback)
    except Exception:
        session["current"] = None
        raise


async def advance_music_queue(guild_id: int, expected_generation: Optional[int] = None):
    session = get_music_session(guild_id)
    if expected_generation is not None and session["generation"] != expected_generation:
        return

    guild = bot.get_guild(guild_id)
    if guild is None:
        return

    finished = session.get("current")
    if finished is not None:
        session["history"].append(finished)
    session["current"] = None

    if not session["queue"]:
        return

    next_track = session["queue"].popleft()
    try:
        await start_music_track(guild, next_track, remember_current=False)
        text_channel = bot.get_channel(next_track.get("text_channel_id"))
        if text_channel:
            await text_channel.send(
                f"🎶 Now playing **[{next_track['title']}]({next_track['webpage_url']})**"
            )
    except Exception as error:
        print(f"Could not start queued music in guild {guild_id}: {error!r}")
        await advance_music_queue(guild_id)


@bot.hybrid_command(name="join", description="Join the voice channel you're currently in.")
@commands.guild_only()
async def join(ctx: commands.Context):
    if not isinstance(ctx.author, discord.Member) or not ctx.author.voice or not ctx.author.voice.channel:
        await send_error(ctx, "❌ Join a voice channel first, then use this command.")
        return

    user_channel = ctx.author.voice.channel
    voice = ctx.guild.voice_client
    if voice and voice.is_connected():
        if voice.channel == user_channel:
            await send_embed(
                ctx,
                f"✅ I'm already connected to {user_channel.mention}.",
                title="🎧 Already Connected",
                color=discord.Color.green(),
            )
        else:
            await send_error(
                ctx,
                f"❌ I'm already in {voice.channel.mention}. Join that voice channel first; I won't switch channels automatically.",
            )
        return

    try:
        await user_channel.connect()
    except (discord.Forbidden, discord.HTTPException) as error:
        await send_error(ctx, f"❌ I couldn't join that voice channel: `{error}`")
        return

    await send_embed(
        ctx,
        f"✅ Joined {user_channel.mention}.",
        title="🎧 Voice Connected",
        color=discord.Color.green(),
    )


@bot.hybrid_command(name="leave", description="Leave the voice channel you're sharing with the bot.")
@commands.guild_only()
async def leave(ctx: commands.Context):
    if not isinstance(ctx.author, discord.Member) or not ctx.author.voice or not ctx.author.voice.channel:
        await send_error(ctx, "❌ Join the same voice channel as me before using this command.")
        return

    voice = ctx.guild.voice_client
    if voice is None or not voice.is_connected():
        await send_error(ctx, "❌ I'm not connected to a voice channel.")
        return

    if ctx.author.voice.channel != voice.channel:
        await send_error(
            ctx,
            f"❌ I'm in {voice.channel.mention}. Join that same voice channel to make me leave.",
        )
        return

    session = get_music_session(ctx.guild.id)
    session["generation"] += 1
    session["current"] = None
    session["queue"].clear()
    if voice.is_playing() or voice.is_paused():
        voice.stop()
    await voice.disconnect(force=True)
    await send_embed(
        ctx,
        "👋 Disconnected from voice and cleared the music queue.",
        title="🎧 Voice Disconnected",
        color=discord.Color.green(),
    )


@bot.hybrid_command(name="play", description="Search for a song and play it in your voice channel.")
@commands.guild_only()
async def play(ctx: commands.Context, *, song: str):
    if not song.strip():
        await send_error(ctx, "❌ Give me a song name to search for.")
        return

    if not isinstance(ctx.author, discord.Member) or not ctx.author.voice or not ctx.author.voice.channel:
        await send_error(ctx, "❌ Join a voice channel first.")
        return

    voice_channel = ctx.author.voice.channel
    voice = ctx.guild.voice_client
    try:
        if voice is None or not voice.is_connected():
            voice = await voice_channel.connect()
        elif voice.channel != voice_channel:
            await send_error(
                ctx,
                f"❌ I'm already in {voice.channel.mention}. Join that voice channel to use music; I won't move away from the channel I'm in.",
            )
            return
    except (discord.Forbidden, discord.HTTPException) as error:
        await send_error(ctx, f"❌ I couldn't join that voice channel: `{error}`")
        return

    await ctx.send(f"🔎 Resolving **{clean_text(song, 150)}**…")
    try:
        spotify_tracks = await asyncio.to_thread(spotify_tracks_from_url, song)
        search_items = spotify_tracks if spotify_tracks is not None else [
            {"title": song, "artists": "", "spotify_url": None}
        ]

        resolved_tracks = []
        # Spotify URLs become title/artist queries before the audio search.
        for item in search_items:
            query = f"{item['title']} {item.get('artists', '')}".strip()
            track = await asyncio.to_thread(search_youtube_song, query)
            track["spotify_url"] = item.get("spotify_url")
            track["requester"] = ctx.author.id
            track["text_channel_id"] = ctx.channel.id
            resolved_tracks.append(track)
    except (LookupError, RuntimeError) as error:
        await send_error(ctx, f"❌ {error}")
        return
    except Exception as error:
        print(f"Music search failed: {error!r}")
        await send_error(ctx, "❌ Music search failed. Check the host logs for the underlying error.")
        return

    session = get_music_session(ctx.guild.id)
    was_busy = voice.is_playing() or voice.is_paused() or session.get("current") is not None

    if was_busy:
        session["queue"].extend(resolved_tracks)
        first_position = len(session["queue"]) - len(resolved_tracks) + 1
        if len(resolved_tracks) == 1:
            track = resolved_tracks[0]
            await send_embed(
                ctx,
                f"➕ Added **[{track['title']}]({track['webpage_url']})** to the queue at position `{first_position}`.",
                title="🎵 Added to Queue",
                color=discord.Color.green(),
            )
        else:
            await send_embed(
                ctx,
                f"➕ Added `{len(resolved_tracks)}` tracks from Spotify to the queue "
                f"(positions `{first_position}`–`{first_position + len(resolved_tracks) - 1}`).",
                title="🎵 Spotify Tracks Queued",
                color=discord.Color.green(),
            )
        return

    first_track, *remaining_tracks = resolved_tracks
    session["queue"].extend(remaining_tracks)
    try:
        await start_music_track(ctx.guild, first_track, remember_current=False)
    except Exception as error:
        print(f"Could not start music: {error!r}")
        await send_error(ctx, "❌ I couldn't start playback. Make sure FFmpeg is installed on the host.")
        return

    if len(resolved_tracks) == 1:
        description = (
            f"🎶 Now playing **[{first_track['title']}]({first_track['webpage_url']})**\n"
            f"Requested by {ctx.author.mention} • `{format_track_duration(first_track.get('duration'))}`"
        )
        title = "🎵 Now Playing"
    else:
        description = (
            f"🎶 Started **[{first_track['title']}]({first_track['webpage_url']})** and queued "
            f"`{len(remaining_tracks)}` more track(s) from Spotify.\nRequested by {ctx.author.mention}"
        )
        title = "🎵 Spotify Queue Started"

    await send_embed(ctx, description, title=title, color=discord.Color.green())


@bot.hybrid_command(name="p", description="Play a track or playlist from a URL.")
@commands.guild_only()
async def p(ctx: commands.Context, *, url: str):
    """Play a direct media URL or resolve Spotify track/album/playlist links."""
    url = url.strip().strip("<>")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        await send_error(ctx, "❌ Give me a valid URL. Example: `?p https://...`")
        return

    if not isinstance(ctx.author, discord.Member) or not ctx.author.voice or not ctx.author.voice.channel:
        await send_error(ctx, "❌ Join a voice channel first.")
        return

    voice_channel = ctx.author.voice.channel
    voice = ctx.guild.voice_client
    try:
        if voice is None or not voice.is_connected():
            voice = await voice_channel.connect()
        elif voice.channel != voice_channel:
            await send_error(
                ctx,
                f"❌ I'm already in {voice.channel.mention}. Join that voice channel to use music; I won't move away from the channel I'm in.",
            )
            return
    except (discord.Forbidden, discord.HTTPException) as error:
        await send_error(ctx, f"❌ I couldn't join that voice channel: `{error}`")
        return

    await ctx.send(f"🔗 Resolving URL: {clean_text(url, 180)}…")
    try:
        spotify_tracks = await asyncio.to_thread(spotify_tracks_from_url, url)
        if spotify_tracks is not None:
            resolved_tracks = []
            for item in spotify_tracks:
                query = f"{item['title']} {item.get('artists', '')}".strip()
                track = await asyncio.to_thread(search_youtube_song, query)
                track["spotify_url"] = item.get("spotify_url")
                track["requester"] = ctx.author.id
                track["text_channel_id"] = ctx.channel.id
                resolved_tracks.append(track)
        else:
            # Direct YouTube/media URLs are passed to yt-dlp as URLs, not search text.
            track = await asyncio.to_thread(search_youtube_song, url)
            track["spotify_url"] = None
            track["requester"] = ctx.author.id
            track["text_channel_id"] = ctx.channel.id
            resolved_tracks = [track]
    except (LookupError, RuntimeError) as error:
        await send_error(ctx, f"❌ {error}")
        return
    except Exception as error:
        print(f"URL music resolution failed: {error!r}")
        await send_error(ctx, "❌ I couldn't resolve that URL. Check the host logs for details.")
        return

    if not resolved_tracks:
        await send_error(ctx, "❌ That URL didn't contain any tracks I could queue.")
        return

    session = get_music_session(ctx.guild.id)
    was_busy = voice.is_playing() or voice.is_paused() or session.get("current") is not None
    if was_busy:
        session["queue"].extend(resolved_tracks)
        first_position = len(session["queue"]) - len(resolved_tracks) + 1
        if len(resolved_tracks) == 1:
            track = resolved_tracks[0]
            await send_embed(
                ctx,
                f"➕ Added **[{track['title']}]({track['webpage_url']})** to the queue at position `{first_position}`.",
                title="🎵 Added from URL",
                color=discord.Color.green(),
            )
        else:
            await send_embed(
                ctx,
                f"➕ Added `{len(resolved_tracks)}` tracks from the URL to the queue "
                f"(positions `{first_position}`–`{first_position + len(resolved_tracks) - 1}`).",
                title="🎵 URL Tracks Queued",
                color=discord.Color.green(),
            )
        return

    first_track, *remaining_tracks = resolved_tracks
    session["queue"].extend(remaining_tracks)
    try:
        await start_music_track(ctx.guild, first_track, remember_current=False)
    except Exception as error:
        print(f"Could not start URL music: {error!r}")
        await send_error(ctx, "❌ I couldn't start playback. Make sure FFmpeg is installed on the host.")
        return

    if remaining_tracks:
        description = (
            f"🎶 Started **[{first_track['title']}]({first_track['webpage_url']})** and queued "
            f"`{len(remaining_tracks)}` more track(s).\nRequested by {ctx.author.mention}"
        )
        title = "🎵 URL Queue Started"
    else:
        description = (
            f"🎶 Now playing **[{first_track['title']}]({first_track['webpage_url']})**\n"
            f"Requested by {ctx.author.mention} • `{format_track_duration(first_track.get('duration'))}`"
        )
        title = "🎵 Now Playing from URL"
    await send_embed(ctx, description, title=title, color=discord.Color.green())


@bot.hybrid_command(name="pause", description="Pause the current song.")
@commands.guild_only()
async def pause(ctx: commands.Context):
    voice = ctx.guild.voice_client
    if voice and voice.is_playing():
        voice.pause()
        await send_embed(ctx, "⏸️ Playback paused.", title="Music Paused", color=discord.Color.green())
    else:
        await send_error(ctx, "❌ Nothing is currently playing.")


@bot.hybrid_command(name="unpause", description="Resume the paused song.")
@commands.guild_only()
async def unpause(ctx: commands.Context):
    voice = ctx.guild.voice_client
    if voice and voice.is_paused():
        voice.resume()
        await send_embed(ctx, "▶️ Playback resumed.", title="Music Resumed", color=discord.Color.green())
    else:
        await send_error(ctx, "❌ Playback isn't paused.")


@bot.hybrid_command(name="skip", description="Skip the current song.")
@commands.guild_only()
async def skip(ctx: commands.Context):
    voice = ctx.guild.voice_client
    session = get_music_session(ctx.guild.id)
    if voice is None or not (voice.is_playing() or voice.is_paused()):
        await send_error(ctx, "❌ Nothing is currently playing.")
        return

    # Invalidate the old audio callback before stopping the player.
    session["generation"] += 1
    voice.stop()
    await advance_music_queue(ctx.guild.id)
    await send_embed(ctx, "⏭️ Skipped the current song.", title="Song Skipped", color=discord.Color.green())


@bot.hybrid_command(name="previous", description="Replay the previous song.")
@commands.guild_only()
async def previous(ctx: commands.Context):
    voice = ctx.guild.voice_client
    session = get_music_session(ctx.guild.id)
    if voice is None or not session["history"]:
        await send_error(ctx, "❌ There isn't a previous song to play.")
        return

    previous_track = session["history"].pop()
    current = session.get("current")
    if current is not None:
        session["queue"].appendleft(current)

    session["generation"] += 1
    if voice.is_playing() or voice.is_paused():
        voice.stop()

    try:
        await start_music_track(ctx.guild, previous_track, remember_current=False)
        await send_embed(
            ctx,
            f"⏮️ Replaying **[{previous_track['title']}]({previous_track['webpage_url']})**.",
            title="Previous Song",
            color=discord.Color.green(),
        )
    except Exception as error:
        print(f"Could not replay previous song: {error!r}")
        await send_error(ctx, "❌ I couldn't replay the previous song.")


@bot.hybrid_command(name="shuffle", description="Shuffle the upcoming songs in the music queue.")
@commands.guild_only()
async def shuffle(ctx: commands.Context):
    """Randomize upcoming tracks without interrupting the currently playing song."""
    session = get_music_session(ctx.guild.id)
    queued = list(session["queue"])

    if len(queued) < 2:
        await send_error(ctx, "❌ Add at least two songs to the queue before shuffling.")
        return

    random.shuffle(queued)
    session["queue"].clear()
    session["queue"].extend(queued)

    await send_embed(
        ctx,
        f"🔀 Shuffled `{len(queued)}` upcoming songs. The current song (if any) will keep playing.",
        title="Queue Shuffled",
        color=discord.Color.green(),
    )


@bot.hybrid_command(name="queue", description="Show the current song and upcoming songs.")
@commands.guild_only()
async def queue(ctx: commands.Context):
    session = get_music_session(ctx.guild.id)
    current = session.get("current")
    queued = list(session["queue"])

    embed = discord.Embed(title="🎶 Music Queue", color=discord.Color.blurple())
    if current:
        embed.add_field(
            name="▶️ Now Playing",
            value=f"[{clean_text(current['title'], 200)}]({current['webpage_url']})",
            inline=False,
        )
    else:
        embed.description = "Nothing is currently playing."

    if queued:
        lines = [
            f"`{index}.` [{clean_text(track['title'], 150)}]({track['webpage_url']})"
            for index, track in enumerate(queued[:15], start=1)
        ]
        if len(queued) > 15:
            lines.append(f"…and `{len(queued) - 15}` more song(s).")
        embed.add_field(name="⏭️ Up Next", value="\\n".join(lines), inline=False)
    else:
        embed.add_field(name="⏭️ Up Next", value="The queue is empty.", inline=False)

    await ctx.send(embed=embed)


# ============================================================
# WARN
# ============================================================

@bot.hybrid_command(description="Warn a member and increase their warning count.")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def warn(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason: str = "No reason provided",
):
    allowed, message = can_moderate(ctx, member)

    if not allowed:
        await send_error(ctx, message)
        return

    total_warnings = add_warning(
        ctx.guild.id,
        member.id,
    )

    log_moderation_action(
        ctx.guild.id,
        ctx.author.id,
        member.id,
        "warn",
        reason,
    )

    await send_moderation_dm(member, ctx.guild, "warned", reason)

    await send_embed(
        ctx,
        f"⚠️ {member.mention} has been warned for **{reason}**.\n"
        f"Responsible Moderator: {ctx.author.mention}\n"
        f"Total Warnings: `{total_warnings}`",
    )

    await log_mod_action_channel(
        ctx,
        "Warn",
        member,
        reason,
        extra=f"Total Warnings: `{total_warnings}`",
    )


# ============================================================
# MUTE / TIMEOUT
# ============================================================

@bot.hybrid_command(description="Timeout a member for a specified duration.")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def mute(
    ctx: commands.Context,
    member: discord.Member,
    duration_text: str,
    *,
    reason: str = "No reason provided",
):
    allowed, message = can_moderate(ctx, member)

    if not allowed:
        await send_error(ctx, message)
        return

    duration = parse_duration(duration_text)

    if duration is None:
        await send_error(
            ctx,
            "❌ Invalid duration. Use `30s`, `10m`, `2h`, or `7d`.",
        )
        return

    if duration > timedelta(days=28):
        await send_error(ctx, "❌ Discord timeouts cannot exceed 28 days.")
        return

    try:
        await member.timeout(
            duration,
            reason=f"{reason} | Moderator: {ctx.author}",
        )
    except discord.Forbidden:
        await send_error(
            ctx,
            "❌ I can't timeout this member. Check my role position and permissions.",
        )
        return
    except discord.HTTPException as error:
        await send_error(ctx, f"❌ Discord rejected the timeout: `{error}`")
        return

    log_moderation_action(
        ctx.guild.id,
        ctx.author.id,
        member.id,
        "mute",
        reason,
    )

    pretty_duration = format_duration(duration)

    await send_moderation_dm(member, ctx.guild, "muted", reason)

    await send_embed(
        ctx,
        f"🔇 {member.mention} has been muted for `{pretty_duration}`.\n"
        f"Reason: {reason}\n"
        f"Responsible Moderator: {ctx.author.mention}",
    )

    await log_mod_action_channel(
        ctx,
        "Mute",
        member,
        reason,
        extra=f"Duration: `{pretty_duration}`",
    )


# ============================================================
# UNMUTE
# ============================================================

@bot.hybrid_command(description="Remove a member's timeout.")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def unmute(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason: str = "No reason provided",
):
    allowed, message = can_moderate(ctx, member)

    if not allowed:
        await send_error(ctx, message)
        return

    if not member.is_timed_out():
        await send_error(ctx, f"❌ {member.mention} is not currently muted.")
        return

    try:
        await member.timeout(
            None,
            reason=f"{reason} | Moderator: {ctx.author}",
        )
    except discord.Forbidden:
        await send_error(ctx, "❌ I don't have permission to remove this timeout.")
        return
    except discord.HTTPException as error:
        await send_error(ctx, f"❌ Discord rejected the action: `{error}`")
        return

    log_moderation_action(
        ctx.guild.id,
        ctx.author.id,
        member.id,
        "unmute",
        reason,
    )

    await send_moderation_dm(member, ctx.guild, "unmuted", reason)

    await send_embed(
        ctx,
        f"🔊 {member.mention} has been unmuted.\n"
        f"Responsible Moderator: {ctx.author.mention}",
    )

    await log_mod_action_channel(
        ctx,
        "Unmute",
        member,
        reason,
    )


# ============================================================
# JAIL / UNJAIL
# ============================================================

@bot.hybrid_command(description="Jail a member and save their current roles.")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(manage_roles=True)
async def jail(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason: str = "No reason provided",
):
    allowed, message = can_moderate(ctx, member)

    if not allowed:
        await send_error(ctx, message)
        return

    jail_role = get_configured_jail_role(ctx.guild)

    if jail_role is None:
        await send_error(
            ctx,
            f"❌ No jail role is configured. Use `{get_guild_prefix(ctx.guild)}setjailrole @role` first.",
        )
        return

    me = ctx.guild.me

    if me is None or jail_role >= me.top_role:
        await send_error(
            ctx,
            "❌ The configured jail role must be below my highest role.",
        )
        return

    if jail_role in member.roles:
        await send_error(ctx, f"❌ {member.mention} is already jailed.")
        return

    removable_roles = [
        role
        for role in member.roles
        if not role.is_default()
        and role != jail_role
        and role < me.top_role
    ]

    # Save the roles so unjail can restore them later.
    with db_connect() as conn:
        conn.execute(
            "DELETE FROM jailed_roles WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, member.id),
        )

        for role in removable_roles:
            conn.execute(
                """
                INSERT OR IGNORE INTO jailed_roles
                    (guild_id, user_id, role_id)
                VALUES (?, ?, ?)
                """,
                (ctx.guild.id, member.id, role.id),
            )

    try:
        if removable_roles:
            await member.remove_roles(
                *removable_roles,
                reason=f"{reason} | Moderator: {ctx.author}",
            )

        await member.add_roles(
            jail_role,
            reason=f"{reason} | Moderator: {ctx.author}",
        )

    except discord.Forbidden:
        await send_error(
            ctx,
            "❌ I couldn't modify this member's roles. Check role hierarchy.",
        )
        return
    except discord.HTTPException as error:
        await send_error(ctx, f"❌ Discord rejected the jail action: `{error}`")
        return

    log_moderation_action(
        ctx.guild.id,
        ctx.author.id,
        member.id,
        "jail",
        reason,
    )

    await send_moderation_dm(member, ctx.guild, "jailed", reason)

    await send_embed(
        ctx,
        f"🔒 {member.mention} has been jailed for **{reason}**.\n"
        f"Responsible Moderator: {ctx.author.mention}",
    )

    await log_mod_action_channel(
        ctx,
        "Jail",
        member,
        reason,
        extra=f"Roles saved for restoration: `{len(removable_roles)}`",
    )


@bot.hybrid_command(description="Release a jailed member and restore their saved roles.")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(manage_roles=True)
async def unjail(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason: str = "No reason provided",
):
    jail_role = get_configured_jail_role(ctx.guild)

    if jail_role is None:
        await send_error(
            ctx,
            f"❌ No jail role is configured. Use `{get_guild_prefix(ctx.guild)}setjailrole @role` first.",
        )
        return

    if jail_role not in member.roles:
        await send_error(ctx, f"❌ {member.mention} is not currently jailed.")
        return

    me = ctx.guild.me

    if me is None or member.top_role >= me.top_role:
        await send_error(ctx, "❌ I can't modify this member because of role hierarchy.")
        return

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT role_id
            FROM jailed_roles
            WHERE guild_id = ? AND user_id = ?
            """,
            (ctx.guild.id, member.id),
        ).fetchall()

    roles_to_restore = []

    for row in rows:
        role = ctx.guild.get_role(row["role_id"])

        if role and role < me.top_role and not role.is_default():
            roles_to_restore.append(role)

    try:
        await member.remove_roles(
            jail_role,
            reason=f"{reason} | Moderator: {ctx.author}",
        )

        if roles_to_restore:
            await member.add_roles(
                *roles_to_restore,
                reason=f"{reason} | Moderator: {ctx.author}",
            )

    except discord.Forbidden:
        await send_error(ctx, "❌ I couldn't modify this member's roles.")
        return
    except discord.HTTPException as error:
        await send_error(ctx, f"❌ Discord rejected the unjail action: `{error}`")
        return

    with db_connect() as conn:
        conn.execute(
            "DELETE FROM jailed_roles WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, member.id),
        )

    log_moderation_action(
        ctx.guild.id,
        ctx.author.id,
        member.id,
        "unjail",
        reason,
    )

    await send_moderation_dm(member, ctx.guild, "unjailed", reason)

    await send_embed(
        ctx,
        f"🔓 {member.mention} has been released from jail.\n"
        f"Responsible Moderator: {ctx.author.mention}",
    )

    await log_mod_action_channel(
        ctx,
        "Unjail",
        member,
        reason,
        extra=f"Roles restored: `{len(roles_to_restore)}`",
    )


# ============================================================
# BAN / UNBAN / KICK
# ============================================================

@bot.hybrid_command(description="Permanently ban a member from the server.")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ban(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason: str = "No reason provided",
):
    allowed, message = can_moderate(ctx, member)

    if not allowed:
        await send_error(ctx, message)
        return

    # DM before the ban because the member leaves the guild immediately after it.
    dm_sent = await send_moderation_dm(member, ctx.guild, "banned", reason)

    try:
        await member.ban(
            reason=f"{reason} | Moderator: {ctx.author}",
        )
    except discord.Forbidden:
        await send_error(ctx, "❌ I don't have permission to ban this member.")
        return
    except discord.HTTPException as error:
        await send_error(ctx, f"❌ Discord rejected the ban: `{error}`")
        return

    log_moderation_action(
        ctx.guild.id,
        ctx.author.id,
        member.id,
        "ban",
        reason,
    )

    dm_status = "sent" if dm_sent else "could not be sent (DMs may be closed)"
    await send_embed(
        ctx,
        f"🔨 {member.mention} has been banned for **{reason}**.\n"
        f"📨 DM: {dm_status}.\n"
        f"Responsible Moderator: {ctx.author.mention}",
    )

    await log_mod_action_channel(
        ctx,
        "Ban",
        member,
        reason,
    )


@bot.hybrid_command(description="Kick a member from the server.")
@commands.guild_only()
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason: str = "No reason provided",
):
    allowed, message = can_moderate(ctx, member)

    if not allowed:
        await send_error(ctx, message)
        return

    # DM before the kick because the member leaves the guild immediately after it.
    dm_sent = await send_moderation_dm(member, ctx.guild, "kicked", reason)

    try:
        await member.kick(
            reason=f"{reason} | Moderator: {ctx.author}",
        )
    except discord.Forbidden:
        await send_error(ctx, "❌ I don't have permission to kick this member.")
        return
    except discord.HTTPException as error:
        await send_error(ctx, f"❌ Discord rejected the kick: `{error}`")
        return

    log_moderation_action(
        ctx.guild.id,
        ctx.author.id,
        member.id,
        "kick",
        reason,
    )

    dm_status = "sent" if dm_sent else "could not be sent (DMs may be closed)"
    await send_embed(
        ctx,
        f"👢 {member.mention} has been kicked for **{reason}**.\n"
        f"📨 DM: {dm_status}.\n"
        f"Responsible Moderator: {ctx.author.mention}",
    )

    await log_mod_action_channel(
        ctx,
        "Kick",
        member,
        reason,
    )


@bot.hybrid_command(description="Unban a user by their Discord user ID.")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def unban(
    ctx: commands.Context,
    user_id: int,
    *,
    reason: str = "No reason provided",
):
    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        await send_error(ctx, "❌ I couldn't find that user ID.")
        return
    except discord.HTTPException as error:
        await send_error(ctx, f"❌ Couldn't fetch that user: `{error}`")
        return

    try:
        await ctx.guild.unban(
            user,
            reason=f"{reason} | Moderator: {ctx.author}",
        )
    except discord.NotFound:
        await send_error(ctx, "❌ That user is not currently banned.")
        return
    except discord.Forbidden:
        await send_error(ctx, "❌ I don't have permission to unban this user.")
        return
    except discord.HTTPException as error:
        await send_error(ctx, f"❌ Discord rejected the unban: `{error}`")
        return

    log_moderation_action(
        ctx.guild.id,
        ctx.author.id,
        user.id,
        "unban",
        reason,
    )

    await send_moderation_dm(user, ctx.guild, "unbanned", reason)

    await send_embed(
        ctx,
        f"🔓 **{user}** has been unbanned.\n"
        f"Responsible Moderator: {ctx.author.mention}",
    )

    await log_mod_action_channel(
        ctx,
        "Unban",
        user,
        reason,
    )


# ============================================================
# BOOSTER CUSTOM ROLES
# ============================================================

@bot.hybrid_command(
    name="br",
    description="Create or manage your server booster role.",
)
@commands.guild_only()
@commands.bot_has_permissions(manage_roles=True)
async def booster_role(ctx: commands.Context, *, arguments: str):
    """
    Booster role command supporting the requested prefix syntax:

        ?br <role name>
        ?br icon <emoji>
        ?br color <hex>
        ?br gift <member>

    The slash version uses one `arguments` option with the same syntax.
    """
    author = ctx.author
    if not isinstance(author, discord.Member) or author.premium_since is None:
        await send_error(ctx, "❌ This command can only be used by server boosters.")
        return

    arguments = arguments.strip()
    if not arguments:
        await send_error(
            ctx,
            "❌ Usage: `?br <role name>`, `?br icon <emoji>`, `?br color <hex>`, or `?br gift <member>`.\n"
            "For example: `?br My Custom Role` or `?br color f30606`.",
        )
        return

    parts = arguments.split(maxsplit=1)
    action = parts[0].casefold()
    value = parts[1].strip() if len(parts) > 1 else ""

    role = get_booster_role(ctx.guild, author.id)

    # `?br <role name>` creates the role the first time, and renames the
    # existing booster role on later uses so the command remains useful.
    if action not in {"icon", "color", "gift"}:
        role_name = arguments[:100].strip()
        if not role_name:
            await send_error(ctx, "❌ The role name cannot be empty.")
            return

        me = ctx.guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await send_error(ctx, "❌ I need Manage Roles to create or edit booster roles.")
            return

        try:
            if role is None:
                role = await ctx.guild.create_role(
                    name=role_name,
                    reason=f"Booster custom role created for {author}.",
                )
                save_booster_role(ctx.guild, author.id, role.id)
                await send_embed(
                    ctx,
                    f"✅ Created your booster role {role.mention}: **{role.name}**.",
                    title="🎁 Booster Role Created",
                    color=discord.Color.green(),
                )
            else:
                if role >= me.top_role:
                    await send_error(ctx, "❌ I can't edit your booster role because it is at or above my highest role.")
                    return
                await role.edit(name=role_name, reason=f"Booster role renamed by {author}.")
                await send_embed(
                    ctx,
                    f"✅ Your booster role is now {role.mention}: **{role.name}**.",
                    title="🏷️ Booster Role Updated",
                    color=discord.Color.green(),
                )
        except discord.Forbidden:
            await send_error(ctx, "❌ I couldn't create or edit your booster role. Check my Manage Roles permission and role position.")
        except discord.HTTPException as error:
            await send_error(ctx, f"❌ Discord rejected the role change: `{error}`")
        return

    if role is None:
        await send_error(ctx, "❌ You don't have a booster role yet. Use `?br <role name>` first.")
        return

    me = ctx.guild.me
    if me is None or role >= me.top_role:
        await send_error(ctx, "❌ I can't edit that booster role because it is at or above my highest role.")
        return

    if action == "color":
        if not value:
            await send_error(ctx, "❌ Usage: `?br color <hex>` — example: `?br color f30606`.")
            return

        hex_value = value.strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", hex_value):
            await send_error(ctx, "❌ Invalid color. Use exactly 6 hexadecimal characters, such as `f30606`.")
            return

        try:
            await role.edit(
                color=discord.Color(int(hex_value, 16)),
                reason=f"Booster role color changed by {author}.",
            )
            await send_embed(
                ctx,
                f"✅ Updated {role.mention}'s color to `#{hex_value.lower()}`.",
                title="🎨 Booster Role Color",
                color=discord.Color(int(hex_value, 16)),
            )
        except discord.Forbidden:
            await send_error(ctx, "❌ I couldn't change your booster role color. Check my Manage Roles permission.")
        except discord.HTTPException as error:
            await send_error(ctx, f"❌ Discord rejected the color change: `{error}`")
        return

    if action == "icon":
        if "ROLE_ICONS" not in ctx.guild.features:
            await send_error(ctx, "this feature is not available in this server")
            return

        if not value:
            await send_error(ctx, "❌ Usage: `?br icon <emoji>` — example: `?br icon 🔥`.")
            return

        emoji_value = value.strip().split()[0]
        try:
            # Supports both Unicode emoji and Discord custom emoji strings.
            if emoji_value.startswith("<:") or emoji_value.startswith("<a:"):
                icon = discord.PartialEmoji.from_str(emoji_value)
                if icon.id is None:
                    raise ValueError
                display_icon = icon
            else:
                display_icon = emoji_value

            await role.edit(
                display_icon=display_icon,
                reason=f"Booster role icon changed by {author}.",
            )
            await send_embed(
                ctx,
                f"✅ Added {emoji_value} as the icon for {role.mention}.",
                title="✨ Booster Role Icon",
                color=discord.Color.green(),
            )
        except (ValueError, discord.InvalidArgument):
            await send_error(ctx, "❌ I couldn't understand that emoji. Use a Unicode emoji or a custom Discord emoji.")
        except discord.Forbidden:
            await send_error(ctx, "❌ I couldn't change the role icon. Check my Manage Roles permission.")
        except discord.HTTPException as error:
            await send_error(ctx, f"❌ Discord rejected the role icon: `{error}`")
        return

    # gift
    if not value:
        await send_error(ctx, "❌ Usage: `?br gift <member>` — for example: `?br gift @User`.")
        return

    target = await resolve_member(ctx.guild, value)
    if target is None:
        await send_error(ctx, "❌ I couldn't find that member. Mention them or provide their user ID.")
        return

    if target.id == author.id:
        await send_error(ctx, "❌ You already own your booster role.")
        return

    if booster_gift_exists(ctx.guild, role.id, target.id):
        await send_error(ctx, f"❌ {target.mention} already has your gifted role.")
        return

    gift_count = count_booster_gifts(ctx.guild, role.id)
    if gift_count >= 4:
        await send_error(ctx, "❌ You can gift your booster role to a maximum of 4 members.")
        return

    if role in target.roles:
        await send_error(ctx, f"❌ {target.mention} already has this role.")
        return

    try:
        await target.add_roles(
            role,
            reason=f"Booster role gifted by {author}.",
        )
        save_booster_gift(ctx.guild, role.id, target.id)
    except discord.Forbidden:
        await send_error(ctx, "❌ I couldn't give that member the role. Check my Manage Roles permission and role hierarchy.")
        return
    except discord.HTTPException as error:
        await send_error(ctx, f"❌ Discord rejected the role gift: `{error}`")
        return

    await send_embed(
        ctx,
        f"🎁 Gifted {role.mention} to {target.mention}.\n"
        f"Gift slots used: `{gift_count + 1}/4`.",
        title="🎁 Booster Role Gifted",
        color=discord.Color.green(),
    )


# ============================================================
# JAIL ROLE SETTINGS
# ============================================================

@bot.hybrid_command(
    name="setjailrole",
    description="Set the role that will be used by the jail command.",
)
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
@commands.bot_has_permissions(manage_roles=True)
async def setjailrole(ctx: commands.Context, role: discord.Role):
    me = ctx.guild.me
    if me is None:
        await send_error(ctx, "❌ I couldn't determine my role in this server.")
        return

    if role.is_default():
        await send_error(ctx, "❌ You can't use @everyone as the jail role.")
        return

    if role >= me.top_role:
        await send_error(ctx, "❌ The jail role must be below my highest role so I can assign it.")
        return

    set_configured_jail_role(ctx.guild, role.id)
    await send_embed(
        ctx,
        f"🔒 The jail role is now {role.mention}.",
        title="🔒 Jail Role Set",
        color=discord.Color.green(),
    )


# ============================================================
# PREFIX SETTINGS
# ============================================================

@bot.hybrid_command(name="setprefix", description="Set or reset the server bot prefix.")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def setprefix(ctx: commands.Context, *, new_prefix: Optional[str] = None):
    """Set or reset this server's bot command prefix."""
    current = get_guild_prefix(ctx.guild)

    if new_prefix is None:
        await send_embed(
            ctx,
            f"⚙️ Current prefix: `{current}`\n"
            f"Use `{current}setprefix <new_prefix>` to change it.\n"
            f"Use `{current}setprefix default` to reset it to `,`.",
        )
        return

    new_prefix = new_prefix.strip()
    if new_prefix.casefold() == "default":
        new_prefix = PREFIX
    if not new_prefix:
        await send_error(ctx, "❌ The prefix cannot be empty.")
        return
    if len(new_prefix) > 5:
        await send_error(ctx, "❌ The prefix cannot be longer than 5 characters.")
        return
    if any(char.isspace() for char in new_prefix):
        await send_error(ctx, "❌ The prefix cannot contain spaces.")
        return

    set_guild_prefix(ctx.guild, new_prefix)
    await send_embed(
        ctx,
        f"✅ Prefix changed from `{current}` to `{new_prefix}`.",
    )


# ============================================================
# MOD-LOG CHANNEL SETTINGS
# ============================================================

@bot.hybrid_command(name="setlogs", description="Set, disable, or show the moderation log channel.")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def setlogs(ctx: commands.Context, channel_input: Optional[str] = None):
    """Set, disable, or show the server's moderation log channel."""
    if not channel_input:
        current = get_modlog_channel(ctx.guild)
        if current:
            await send_embed(
                ctx,
                f"📋 Current mod-log channel: {current.mention}",
            )
        else:
            await send_embed(
                ctx,
                f"📋 No mod-log channel is configured. Use `{PREFIX}setlogs #channel`.",
            )
        return

    if channel_input.casefold() in {"off", "disable", "none"}:
        await set_modlog_channel(ctx.guild, None)
        await send_embed(
            ctx,
            f"✅ Custom mod-log channel disabled. I'll use `{MOD_ACTIONS_CHANNEL}` if it exists.",
        )
        return

    # Resolve a channel mention, channel ID, or exact channel name.
    channel = None
    mention_match = re.fullmatch(r"<#(\d+)>", channel_input)
    if mention_match:
        channel = ctx.guild.get_channel(int(mention_match.group(1)))
    elif channel_input.isdigit():
        channel = ctx.guild.get_channel(int(channel_input))
    else:
        channel = discord.utils.get(
            ctx.guild.text_channels,
            name=channel_input,
        )

    if not isinstance(channel, discord.TextChannel):
        await send_error(
            ctx,
            f"❌ I couldn't find that text channel. Use `{PREFIX}setlogs #channel` or a channel ID.",
        )
        return

    me = ctx.guild.me
    if me is None:
        await send_error(ctx, "❌ I couldn't determine my permissions in this server.")
        return

    permissions = channel.permissions_for(me)
    missing = []
    if not permissions.view_channel:
        missing.append("View Channel")
    if not permissions.send_messages:
        missing.append("Send Messages")
    if not permissions.embed_links:
        missing.append("Embed Links")

    if missing:
        await send_error(
            ctx,
            f"❌ I can't use {channel.mention}. Missing: `{', '.join(missing)}`.",
        )
        return

    await set_modlog_channel(ctx.guild, channel.id)
    await send_embed(
        ctx,
        f"✅ Moderation logs will now be sent to {channel.mention}.",
    )


@bot.hybrid_command(name="logs", description="Show the current moderation log channel.")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def logs_channel(ctx: commands.Context):
    """Show the current moderation log channel."""
    with db_connect() as conn:
        row = conn.execute(
            "SELECT modlog_channel_id FROM guild_settings WHERE guild_id = ?",
            (ctx.guild.id,),
        ).fetchone()

    if row and row["modlog_channel_id"]:
        channel = ctx.guild.get_channel(int(row["modlog_channel_id"]))
        if channel is not None:
            await send_embed(
                ctx,
                f"📋 Current mod-log channel: {channel.mention}",
            )
            return

    fallback = discord.utils.get(ctx.guild.text_channels, name=MOD_ACTIONS_CHANNEL)
    if fallback:
        await send_embed(
            ctx,
            f"📋 No custom channel is set. Using the default {fallback.mention}.",
        )
    else:
        await send_embed(
            ctx,
            f"📋 No mod-log channel is set, and `{MOD_ACTIONS_CHANNEL}` doesn't exist.",
        )


# ============================================================
# WARNINGS / MODERATION LOGS
# ============================================================

@bot.hybrid_command(description="Show a member's warning count and recent warnings.")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
async def warns(
    ctx: commands.Context,
    member: discord.Member,
):
    """Show a member's warning count and recent warnings."""
    with db_connect() as conn:
        count_row = conn.execute(
            """
            SELECT warnings_count
            FROM users_per_guild
            WHERE guild_id = ? AND user_id = ?
            """,
            (ctx.guild.id, member.id),
        ).fetchone()

        rows = conn.execute(
            """
            SELECT mod_id, reason, timestamp
            FROM mod_logs
            WHERE guild_id = ?
              AND target_id = ?
              AND action_type = 'warn'
            ORDER BY id DESC
            LIMIT 10
            """,
            (ctx.guild.id, member.id),
        ).fetchall()

    total = int(count_row["warnings_count"]) if count_row else 0

    embed = discord.Embed(
        title=f"⚠️ Warnings — {member}",
        description=f"**Total Warnings:** `{total}`\nShowing the latest `{len(rows)}` warning(s).",
        color=discord.Color.orange(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    if rows:
        lines = []
        for index, row in enumerate(rows, 1):
            moderator = ctx.guild.get_member(int(row["mod_id"]))
            moderator_text = moderator.mention if moderator else f"<@{row['mod_id']}>"
            try:
                when = discord.utils.format_dt(
                    datetime.fromisoformat(row["timestamp"]),
                    style="R",
                )
            except (ValueError, TypeError):
                when = "Unknown time"

            lines.append(
                f"**{index}.** {when} • by {moderator_text}\n"
                f"> {(row['reason'] or 'No reason provided')[:900]}"
            )

        embed.add_field(
            name="Recent Warnings",
            value="\n\n".join(lines)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name="Recent Warnings",
            value="No warnings found for this member.",
            inline=False,
        )

    embed.set_footer(text=f"User ID: {member.id} • Guild: {ctx.guild.name}")
    await ctx.send(embed=embed)


@bot.hybrid_command(description="Show a member's recent moderation history.")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
async def modlogs(
    ctx: commands.Context,
    member: discord.Member,
):
    """Show a member's recent moderation history."""
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT mod_id, action_type, reason, timestamp
            FROM mod_logs
            WHERE guild_id = ? AND target_id = ?
            ORDER BY id DESC
            LIMIT 15
            """,
            (ctx.guild.id, member.id),
        ).fetchall()

    embed = discord.Embed(
        title=f"📋 Mod Logs — {member}",
        description=f"Showing the latest `{len(rows)}` moderation action(s).",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    if not rows:
        embed.add_field(
            name="History",
            value="No moderation logs found for this member.",
            inline=False,
        )
    else:
        lines = []
        for index, row in enumerate(rows, 1):
            moderator = ctx.guild.get_member(int(row["mod_id"]))
            moderator_text = moderator.mention if moderator else f"<@{row['mod_id']}>"

            try:
                when = discord.utils.format_dt(
                    datetime.fromisoformat(row["timestamp"]),
                    style="R",
                )
            except (ValueError, TypeError):
                when = "Unknown time"

            action = str(row["action_type"]).replace("_", " ").title()
            reason = (row["reason"] or "No reason provided")[:700]

            lines.append(
                f"**{index}.** `{action}` • {when}\n"
                f"Moderator: {moderator_text}\n"
                f"Reason: {reason}"
            )

        embed.add_field(
            name="Recent Actions",
            value="\n\n".join(lines)[:1024],
            inline=False,
        )

    embed.set_footer(text=f"User ID: {member.id} • Guild: {ctx.guild.name}")
    await ctx.send(embed=embed)


# ============================================================
# USER NOTES
# ============================================================

@bot.hybrid_command(name="note", description="Add a private moderation note to a member.")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
async def note(
    ctx: commands.Context,
    member: discord.Member,
    *,
    note_text: str,
):
    """Add a persistent note to a member without counting it as a moderation action."""
    note_text = note_text.strip()

    if not note_text:
        await send_error(ctx, "❌ The note cannot be empty.")
        return

    if len(note_text) > 2000:
        await send_error(ctx, "❌ Notes must be 2000 characters or fewer.")
        return

    timestamp = datetime.now(timezone.utc)
    with db_connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO user_notes (guild_id, target_id, mod_id, note, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                ctx.guild.id,
                member.id,
                ctx.author.id,
                note_text,
                timestamp.isoformat(),
            ),
        )
        note_id = int(cursor.lastrowid)

    await send_embed(
        ctx,
        f"📝 Note `#{note_id}` added to {member.mention}.\n"
        f"Added by: {ctx.author.mention}\n"
        f"Added: {discord.utils.format_dt(timestamp, style='R')}",
        title="📝 Note Added",
        color=discord.Color.green(),
    )

    await log_mod_action_channel(
        ctx,
        "Note Added",
        member,
        "Moderator note added",
        extra=f"Note ID: `#{note_id}`\nNote: {clean_text(note_text, 900)}",
    )


@bot.hybrid_command(name="notes", description="Show all moderation notes for a member.")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
async def notes(
    ctx: commands.Context,
    member: discord.Member,
):
    """Show every persistent note stored for a member in this server."""
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, mod_id, note, timestamp
            FROM user_notes
            WHERE guild_id = ? AND target_id = ?
            ORDER BY id ASC
            """,
            (ctx.guild.id, member.id),
        ).fetchall()

    if not rows:
        embed = discord.Embed(
            title=f"📝 Notes — {member}",
            description=f"No notes have been added for {member.mention}.",
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"User ID: {member.id} • Guild: {ctx.guild.name}")
        await ctx.send(embed=embed)
        return

    # Discord embeds have a 4096-character description limit. Split a long
    # note history across multiple embeds while still showing every note.
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0

    for index, row in enumerate(rows, 1):
        moderator = ctx.guild.get_member(int(row["mod_id"]))
        moderator_text = moderator.mention if moderator else f"<@{row['mod_id']}>"

        try:
            timestamp = datetime.fromisoformat(row["timestamp"])
            when = (
                f"{discord.utils.format_dt(timestamp, style='F')} "
                f"({discord.utils.format_dt(timestamp, style='R')})"
            )
        except (ValueError, TypeError):
            when = "Unknown time"

        text = (
            f"**#{row['id']}** • {when}\n"
            f"**By:** {moderator_text}\n"
            f"> {(row['note'] or '').strip()}"
        )

        if current and current_length + len(text) + 2 > 3900:
            chunks.append("\n\n".join(current))
            current = []
            current_length = 0

        current.append(text)
        current_length += len(text) + 2

    if current:
        chunks.append("\n\n".join(current))

    for page, chunk in enumerate(chunks, 1):
        embed = discord.Embed(
            title=f"📝 Notes — {member}",
            description=chunk,
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(
            text=f"{len(rows)} total note(s) • Page {page}/{len(chunks)} • User ID: {member.id}"
        )
        await ctx.send(embed=embed)


# ============================================================
# MODERATION STATS
# ============================================================

@bot.hybrid_command(description="Show 7-day, 30-day, and all-time moderation statistics.")
@commands.guild_only()
async def ms(
    ctx: commands.Context,
    user_id: Optional[int] = None,
):
    target_id = user_id or ctx.author.id

    try:
        user = await bot.fetch_user(target_id)
    except discord.NotFound:
        await send_error(ctx, "❌ I couldn't find that user ID.")
        return
    except discord.HTTPException:
        await send_error(ctx, "❌ I couldn't fetch that user.")
        return

    actions = ("mute", "warn", "ban", "kick", "jail")

    def get_count(action: str, days: Optional[int] = None) -> int:
        with db_connect() as conn:
            if days is None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM mod_logs
                    WHERE guild_id = ?
                      AND mod_id = ?
                      AND action_type = ?
                    """,
                    (ctx.guild.id, user.id, action),
                ).fetchone()
            else:
                cutoff = datetime.now(timezone.utc) - timedelta(days=days)

                row = conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM mod_logs
                    WHERE guild_id = ?
                      AND mod_id = ?
                      AND action_type = ?
                      AND timestamp >= ?
                    """,
                    (
                        ctx.guild.id,
                        user.id,
                        action,
                        cutoff.isoformat(),
                    ),
                ).fetchone()

            return int(row["count"])

    def total(days: Optional[int] = None) -> int:
        return sum(get_count(action, days) for action in actions)

    embed = discord.Embed(
        title=f"📊 Moderation Statistics — {user}",
        color=discord.Color.blurple(),
    )

    if isinstance(user, discord.User):
        embed.set_thumbnail(url=user.display_avatar.url)

    def stats_text(days: Optional[int]) -> str:
        return (
            f"**Total Actions:** `{total(days)}`\n"
            f"🔇 Mutes: `{get_count('mute', days)}`\n"
            f"⚠️ Warns: `{get_count('warn', days)}`\n"
            f"🔨 Bans: `{get_count('ban', days)}`\n"
            f"👢 Kicks: `{get_count('kick', days)}`\n"
            f"🔒 Jails: `{get_count('jail', days)}`"
        )

    embed.add_field(
        name="Last 7 Days",
        value=stats_text(7),
        inline=False,
    )

    embed.add_field(
        name="Last 30 Days",
        value=stats_text(30),
        inline=False,
    )

    embed.add_field(
        name="All Time",
        value=stats_text(None),
        inline=False,
    )

    embed.set_footer(
        text=f"User ID: {user.id} • Guild: {ctx.guild.name}"
    )

    await ctx.send(embed=embed)


# ============================================================
# FORCE NICKNAME
# ============================================================

@bot.hybrid_command(description="Set and continuously enforce a member nickname.")
@commands.guild_only()
@commands.has_permissions(manage_nicknames=True)
@commands.bot_has_permissions(manage_nicknames=True)
async def forcenick(
    ctx: commands.Context,
    target: str,
    *,
    nickname: str,
):
    member = await resolve_member(ctx.guild, target)

    if member is None:
        await send_error(ctx, "❌ I couldn't find that member.")
        return

    allowed, message = can_moderate(ctx, member)

    if not allowed:
        await send_error(ctx, message)
        return

    nickname = nickname.strip()

    if not nickname:
        await send_error(ctx, "❌ The nickname cannot be empty.")
        return

    if len(nickname) > 32:
        await send_error(ctx, "❌ Nicknames cannot be longer than 32 characters.")
        return

    me = ctx.guild.me

    if me is None or member.top_role >= me.top_role:
        await send_error(
            ctx,
            "❌ I can't change this user's nickname because of role hierarchy.",
        )
        return

    old_nickname = member.nick or member.name

    try:
        await member.edit(
            nick=nickname,
            reason=f"Forced nickname by {ctx.author}",
        )
    except discord.Forbidden:
        await send_error(ctx, "❌ I don't have permission to change this user's nickname.")
        return
    except discord.HTTPException as error:
        await send_error(ctx, f"❌ Discord rejected the nickname change: `{error}`")
        return

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO forced_nicknames (guild_id, user_id, nickname)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET nickname = excluded.nickname
            """,
            (ctx.guild.id, member.id, nickname),
        )

    log_moderation_action(
        ctx.guild.id,
        ctx.author.id,
        member.id,
        "forcenick",
        "Nickname forcibly locked",
    )

    await send_moderation_dm(member, ctx.guild, "given a forced nickname", "Nickname forcibly locked")

    await send_embed(
        ctx,
        f"🏷️ {member.mention}'s nickname is now forced to `{nickname}`.\n"
        f"Responsible Moderator: {ctx.author.mention}",
    )

    await log_mod_action_channel(
        ctx,
        "Force Nickname",
        member,
        "Nickname forcibly locked",
        extra=(
            f"Old nickname: `{old_nickname}`\n"
            f"Forced nickname: `{nickname}`"
        ),
    )


@bot.hybrid_command(description="Remove a member's forced nickname.")
@commands.guild_only()
@commands.has_permissions(manage_nicknames=True)
@commands.bot_has_permissions(manage_nicknames=True)
async def unforcenick(
    ctx: commands.Context,
    target: str,
):
    member = await resolve_member(ctx.guild, target)

    if member is None:
        await send_error(ctx, "❌ I couldn't find that member.")
        return

    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT nickname
            FROM forced_nicknames
            WHERE guild_id = ? AND user_id = ?
            """,
            (ctx.guild.id, member.id),
        ).fetchone()

        if row is None:
            await send_error(
                ctx,
                f"❌ {member.mention} doesn't have a forced nickname.",
            )
            return

        forced_nickname = row["nickname"]

        conn.execute(
            """
            DELETE FROM forced_nicknames
            WHERE guild_id = ? AND user_id = ?
            """,
            (ctx.guild.id, member.id),
        )

    log_moderation_action(
        ctx.guild.id,
        ctx.author.id,
        member.id,
        "unforcenick",
        "Forced nickname removed",
    )

    await send_moderation_dm(member, ctx.guild, "removed from forced nickname enforcement", "Forced nickname removed")

    await send_embed(
        ctx,
        f"🏷️ Removed the forced nickname from {member.mention}.\n"
        "They can now change their nickname normally.",
    )

    await log_mod_action_channel(
        ctx,
        "Unforce Nickname",
        member,
        "Forced nickname removed",
        extra=f"Removed forced nickname: `{forced_nickname}`",
    )


# ============================================================
# EASTER-EGG COMMANDS
# ============================================================

@bot.command()
async def light(ctx: commands.Context):
    await send_embed(
        ctx,
        "Light is the most good-looking person that has ever existed ✨",
    )


@bot.command()
async def winter(ctx: commands.Context):
    await send_embed(
        ctx,
        "Winter, mostly known as Wintersoul, is Light's kitten",
    )


@bot.command()
async def ily(ctx: commands.Context):
    await send_embed(
        ctx,
        "Ily too <3",
    )


@bot.command()
async def drake(ctx: commands.Context):
    await send_embed(
        ctx,
        "Out in the six I'm a national treasure",
    )


@bot.command(name="kendrick")
async def kendrick(ctx: commands.Context):
    await send_embed(
        ctx,
        "They not like us",
    )


@bot.command()
async def phantom(ctx: commands.Context):
    await send_embed(
        ctx,
        "Auntie",
    )


@bot.command()
async def diddle(ctx: commands.Context):
    await send_embed(
        ctx,
        "Winter",
    )


@bot.command(name="help_me")
async def help_me(ctx: commands.Context):
    await send_embed(
        ctx,
        "You need to ask Light for help, he is the most good-looking "
        "person that has ever existed ✨",
    )


@bot.command()
async def potato(ctx: commands.Context):
    await send_embed(
        ctx,
        "Potatoes",
    )


@bot.command()
async def daksh(ctx: commands.Context):
    await send_embed(
        ctx,
        "Daksh is a very good boy",
    )


@bot.command()
async def iamnoob(ctx: commands.Context):
    await send_embed(
        ctx,
        "lol",
    )


@bot.command(name="Isphantomauntie")
async def is_phantom_auntie(ctx: commands.Context):
    await send_embed(
        ctx,
        "Yes, Phantom is a middle-aged auntie",
    )


@bot.command(name="whoismizi")
async def who_is_mizi(ctx: commands.Context):
    await send_embed(
        ctx,
        "GAY",
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def potatoes(ctx: commands.Context):
    await send_embed(
        ctx,
        "Love",
    )


# ============================================================
# BOT-HOSTING.NET CONSOLE SENDER
# ============================================================

# The shell helper and queue live beside this script, so no root-level
# directory permissions are required on managed hosting.
CONSOLE_QUEUE_PATH = os.path.join(BASE_DIR, ".arclight_console_queue")
CONSOLE_HELPER_PATH = os.path.join(BASE_DIR, "arclight-send")


def install_console_send_helper() -> None:
    """Install a local shell helper; the running bot sends queued messages."""
    import shlex

    helper = f"""#!/bin/sh
QUEUE={shlex.quote(CONSOLE_QUEUE_PATH)}

if [ "$#" -lt 2 ]; then
    echo "Usage: ./arclight-send <channel_id> <message>"
    exit 1
fi

case "$1" in
    *[!0-9]*|'')
        echo "Invalid channel ID."
        exit 1
        ;;
esac

channel_id="$1"
shift
message="$*"

printf '%s\t%s\n' "$channel_id" "$message" >> "$QUEUE" || exit 1
echo "Queued message for channel $channel_id"
"""
    try:
        with open(CONSOLE_HELPER_PATH, "w", encoding="utf-8", newline="\n") as file:
            file.write(helper)
        os.chmod(CONSOLE_HELPER_PATH, 0o755)
        print(f"Console sender helper installed at {CONSOLE_HELPER_PATH}", flush=True)
    except OSError as error:
        print(f"Could not install console sender helper: {error}", flush=True)


async def console_sender_worker() -> None:
    """Read queued console messages and send them through the connected bot."""
    print("Console sender ready.", flush=True)
    print("Use: ./arclight-send <channel_id> <message>", flush=True)

    while True:
        try:
            if not os.path.exists(CONSOLE_QUEUE_PATH):
                await asyncio.sleep(0.5)
                continue

            # Atomically swap the queue so messages arriving while we process
            # this batch aren't lost when the queue is cleared.
            processing_path = CONSOLE_QUEUE_PATH + ".processing"
            try:
                os.replace(CONSOLE_QUEUE_PATH, processing_path)
            except FileNotFoundError:
                await asyncio.sleep(0.5)
                continue

            with open(processing_path, "r", encoding="utf-8") as file:
                lines = file.readlines()
            try:
                os.remove(processing_path)
            except OSError:
                pass

            for line in lines:
                line = line.rstrip("\r\n")
                if not line or "\t" not in line:
                    print("Ignored malformed console message.", flush=True)
                    continue

                channel_text, content = line.split("\t", 1)
                try:
                    channel_id = int(channel_text)
                except ValueError:
                    print(f"Invalid console channel ID: {channel_text!r}", flush=True)
                    continue

                channel = bot.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await bot.fetch_channel(channel_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                        print(f"Could not access channel {channel_id}: {error}", flush=True)
                        continue

                if not callable(getattr(channel, "send", None)):
                    print(f"Channel {channel_id} cannot receive messages.", flush=True)
                    continue

                try:
                    await channel.send(content)
                    print(f"Sent console message to #{getattr(channel, 'name', channel_id)} ({channel_id})", flush=True)
                except discord.Forbidden:
                    print(f"Missing permission to send messages in channel {channel_id}.", flush=True)
                except discord.HTTPException as error:
                    print(f"Discord rejected the message for channel {channel_id}: {error}", flush=True)

        except Exception as error:
            print(f"Console sender error: {error!r}", flush=True)

        await asyncio.sleep(0.5)


# ============================================================
# STARTUP
# ============================================================

init_database()

install_console_send_helper()


async def run_bot_with_login_retry() -> None:
    # Keep the process alive when Discord temporarily returns a global 429
    # during login instead of letting the deployment restart-loop.
    retry_delay = 60

    while True:
        try:
            print("🚀 Starting ArcLight...")
            await bot.start(TOKEN, reconnect=True)
            return

        except discord.LoginFailure:
            print("❌ Discord rejected the bot token. Check DISCORD_TOKEN.")
            raise

        except discord.HTTPException as error:
            if getattr(error, "status", None) != 429:
                print(f"❌ Discord HTTP error during startup: {error}")
                raise

            print(
                f"⏳ Discord rate-limited the login request (429). "
                f"Waiting {retry_delay}s before trying again..."
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 900)

        except (discord.GatewayNotFound, discord.ConnectionClosed) as error:
            print(f"⚠️ Discord connection error during startup: {error}")
            await asyncio.sleep(min(retry_delay, 300))


async def main() -> None:
    # The console sender is started from on_ready(), after Discord is
    # connected, so it always has a live bot connection available.
    await run_bot_with_login_retry()


if __name__ == "__main__":
    asyncio.run(main())
