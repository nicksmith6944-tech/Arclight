import os
import asyncio
import re
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
                prefix TEXT NOT NULL DEFAULT ','
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
    # Always accept the built-in "," prefix. If a server has a custom
    # prefix, accept that one too.
    custom = get_guild_prefix(message.guild)
    if custom == PREFIX:
        return PREFIX
    return [PREFIX, custom]


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


async def get_or_create_jail_role(
    guild: discord.Guild,
) -> Optional[discord.Role]:
    role = discord.utils.get(guild.roles, name=JAILED_ROLE_NAME)

    if role:
        return role

    try:
        return await guild.create_role(
            name=JAILED_ROLE_NAME,
            reason="Creating moderation jail role",
        )
    except discord.Forbidden:
        return None
    except discord.HTTPException:
        return None


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


# ============================================================
# EVENTS
# ============================================================

@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online!")
    print(f"📡 Connected to {len(bot.guilds)} guild(s).")
    print(f"⌨️ Default prefix: {PREFIX!r}")
    print(f"🧠 Message Content Intent in code: {bot.intents.message_content}")

    # Start the Railway console sender only after Discord is connected.
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
            f"Prefix: `{get_guild_prefix(ctx.guild)}`\n\n"
            "**Arguments:** `<required>` • `(optional)`"
        ),
        color=discord.Color.blurple(),
    )

    commands_list = [
        (
            "🧹 Purge",
            f"`{get_guild_prefix(ctx.guild)}purge <user_id> <amount>`\n"
            f"`{get_guild_prefix(ctx.guild)}purge <user_id>`\n"
            "Deletes messages from a specific user in the current channel.",
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
            "Removes the member's roles and gives them the Jailed role.",
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

@bot.hybrid_command(description="Delete messages from a specific user in the current channel.")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True)
async def purge(
    ctx: commands.Context,
    user_id: int,
    amount: Optional[int] = None,
):
    if amount is not None and amount <= 0:
        await send_error(ctx, "❌ Amount must be greater than 0.")
        return

    target = ctx.guild.get_member(user_id)

    if target is None:
        try:
            target = await bot.fetch_user(user_id)
        except (discord.NotFound, discord.HTTPException):
            await send_error(ctx, "❌ I couldn't find that user ID.")
            return

    deleted_count = 0

    # Discord's bulk delete has a 14-day limitation. Older messages
    # are deleted one at a time.
    async for message in ctx.channel.history(limit=None):
        if message.author.id != target.id:
            continue

        if amount is not None and deleted_count >= amount:
            break

        try:
            age = datetime.now(timezone.utc) - message.created_at

            if age < timedelta(days=14):
                await message.delete()
            else:
                await message.delete()

            deleted_count += 1
        except discord.NotFound:
            pass
        except discord.Forbidden:
            await send_error(ctx, "❌ I don't have permission to delete that message.")
            break
        except discord.HTTPException:
            pass

    if ctx.message is not None:
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

    await send_embed(
        ctx,
        f"🧹 Deleted `{deleted_count}` message(s) from {target.mention}.\n"
        f"Responsible Moderator: {ctx.author.mention}",
        delete_after=7,
    )

    log_moderation_action(
        ctx.guild.id,
        ctx.author.id,
        target.id,
        "purge",
        f"Messages purged: {deleted_count}",
    )

    await log_mod_action_channel(
        ctx,
        "Purge",
        target,
        "Messages purged",
        extra=f"Messages deleted: `{deleted_count}`",
    )


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

    jail_role = await get_or_create_jail_role(ctx.guild)

    if jail_role is None:
        await send_error(ctx, "❌ I couldn't create/find the Jailed role.")
        return

    me = ctx.guild.me

    if me is None or jail_role >= me.top_role:
        await send_error(
            ctx,
            "❌ The Jailed role must be below my highest role.",
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
    jail_role = discord.utils.get(
        ctx.guild.roles,
        name=JAILED_ROLE_NAME,
    )

    if jail_role is None or jail_role not in member.roles:
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
# RAILWAY CONSOLE SENDER
# ============================================================

# Messages written by the Railway console helper are queued here.
CONSOLE_QUEUE_PATH = os.path.join(BASE_DIR, ".arclight_console_queue")
CONSOLE_HELPER_PATH = "/usr/local/bin/!send"


def install_console_send_helper() -> None:
    """
    Install a tiny shell helper so the Railway container console can use:

        !send <channel_id> <message>

    The helper only writes to ArcLight's local queue; the running bot process
    is responsible for actually sending the Discord message.
    """
    helper = f"""#!/bin/sh
QUEUE={CONSOLE_QUEUE_PATH!r}

if [ "$#" -lt 2 ]; then
    echo "Usage: !send <channel_id> <message>"
    exit 1
fi

case "$1" in
    *[!0-9]*|'')
        echo "❌ Invalid channel ID."
        exit 1
        ;;
esac

channel_id="$1"
shift
message="$*"

printf '%s\\t%s\\n' "$channel_id" "$message" >> "$QUEUE"
echo "📨 Queued message for channel $channel_id"
"""
    try:
        with open(CONSOLE_HELPER_PATH, "w", encoding="utf-8") as file:
            file.write(helper)
        os.chmod(CONSOLE_HELPER_PATH, 0o755)
    except (PermissionError, OSError) as error:
        print(f"⚠️ Could not install Railway !send helper: {error}")


def configure_bash_history_expansion() -> None:
    """
    Bash normally treats !send as history expansion. Disable that for
    interactive Railway shells so the literal !send command can be used.
    """
    bashrc_path = os.path.expanduser("~/.bashrc")
    try:
        with open(bashrc_path, "a", encoding="utf-8") as file:
            file.write(
                "\n# ArcLight Railway console sender\n"
                "set +H 2>/dev/null\n"
            )
    except (PermissionError, OSError) as error:
        print(f"⚠️ Could not configure bash history expansion: {error}")


async def console_sender_worker() -> None:
    """Read queued Railway console messages and send them through ArcLight."""
    print("🖥️ Railway console sender ready.", flush=True)
    print("💬 Use: !send <channel_id> <message>", flush=True)

    while True:
        try:
            if not os.path.exists(CONSOLE_QUEUE_PATH):
                await asyncio.sleep(0.5)
                continue

            with open(CONSOLE_QUEUE_PATH, "r", encoding="utf-8") as file:
                lines = file.readlines()

            if not lines:
                await asyncio.sleep(0.5)
                continue

            # Clear the queue before sending so new messages can be queued
            # while Discord requests are in progress.
            with open(CONSOLE_QUEUE_PATH, "w", encoding="utf-8"):
                pass

            for line in lines:
                line = line.rstrip("\n")
                if not line or "\t" not in line:
                    print("⚠️ Ignored malformed console message.")
                    continue

                channel_text, content = line.split("\t", 1)

                try:
                    channel_id = int(channel_text)
                except ValueError:
                    print(f"⚠️ Invalid console channel ID: {channel_text!r}")
                    continue

                channel = bot.get_channel(channel_id)

                # If the channel is not cached, fetch it directly. This also
                # lets the feature work across all servers ArcLight is in.
                if channel is None:
                    try:
                        channel = await bot.fetch_channel(channel_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                        print(f"❌ Could not access channel {channel_id}: {error}")
                        continue

                if not hasattr(channel, "send"):
                    print(f"❌ Channel {channel_id} cannot receive messages.")
                    continue

                try:
                    await channel.send(content)
                    print(f"✅ Sent console message to #{getattr(channel, 'name', channel_id)} ({channel_id})")
                except discord.Forbidden:
                    print(f"❌ Missing permission to send messages in channel {channel_id}.")
                except discord.HTTPException as error:
                    print(f"❌ Discord rejected the message for channel {channel_id}: {error}")

        except Exception as error:
            print(f"⚠️ Console sender error: {error!r}")

        await asyncio.sleep(0.5)


# ============================================================
# STARTUP
# ============================================================

init_database()

install_console_send_helper()
configure_bash_history_expansion()


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
