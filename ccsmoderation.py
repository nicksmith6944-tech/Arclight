import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

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
        "DISCORD_TOKEN is missing. Put it in your .env file like:\n"
        "DISCORD_TOKEN=your_bot_token"
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
    command_prefix=PREFIX,
    intents=intents,
    case_insensitive=True,
    help_command=None,
)


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


async def send_error(ctx: commands.Context, message: str) -> None:
    await ctx.send(message, delete_after=7)


async def log_mod_action_channel(
    ctx: commands.Context,
    action: str,
    target,
    reason: str = "No reason provided",
    extra: Optional[str] = None,
) -> None:
    channel = discord.utils.get(
        ctx.guild.text_channels,
        name=MOD_ACTIONS_CHANNEL,
    )

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
# EVENTS
# ============================================================

@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online!")
    print(f"📡 Connected to {len(bot.guilds)} guild(s).")


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

@bot.command(name="help")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="📖 Moderation Bot — Help",
        description=(
            f"Prefix: `{PREFIX}`\n\n"
            "**Arguments:** `<required>` • `(optional)`"
        ),
        color=discord.Color.blurple(),
    )

    commands_list = [
        (
            "🧹 Purge",
            f"`{PREFIX}purge <user_id> <amount>`\n"
            f"`{PREFIX}purge <user_id>`\n"
            "Deletes messages from a specific user in the current channel.",
        ),
        (
            "🧹 Clean",
            f"`{PREFIX}clean`\n"
            "Deletes the 10 most recent bot messages in the channel.",
        ),
        (
            "⚠️ Warn",
            f"`{PREFIX}warn <user> (reason)`\n"
            "Warns a member and increases their warning count.\n"
            f"`{PREFIX}warns <user>`\n"
            "Shows a member's warning count and recent warnings.\n"
            f"`{PREFIX}modlogs <user>`\n"
            "Shows a member's recent moderation history.",
        ),
        (
            "🔇 Mute",
            f"`{PREFIX}mute <user> <duration> (reason)`\n"
            "Times out a member. Duration: `30s`, `10m`, `2h`, `7d`.",
        ),
        (
            "🔊 Unmute",
            f"`{PREFIX}unmute <user> (reason)`\n"
            "Removes a member's timeout.",
        ),
        (
            "🔒 Jail",
            f"`{PREFIX}jail <user> (reason)`\n"
            "Removes the member's roles and gives them the Jailed role.",
        ),
        (
            "🔓 Unjail",
            f"`{PREFIX}unjail <user> (reason)`\n"
            "Removes Jailed and restores the roles saved during jail.",
        ),
        (
            "🔨 Ban",
            f"`{PREFIX}ban <user> (reason)`\n"
            "Permanently bans a member.",
        ),
        (
            "🔓 Unban",
            f"`{PREFIX}unban <user_id> (reason)`\n"
            "Unbans a user by ID.",
        ),
        (
            "👢 Kick",
            f"`{PREFIX}kick <user> (reason)`\n"
            "Kicks a member from the server.",
        ),
        (
            "📊 Moderation Stats",
            f"`{PREFIX}ms`\n"
            f"`{PREFIX}ms <user_id>`\n"
            "Shows 7-day, 30-day and all-time moderation statistics.",
        ),
        (
            "🏷️ Force Nickname",
            f"`{PREFIX}forcenick <user> <nickname>`\n"
            "Sets and continuously enforces a nickname.",
        ),
        (
            "🏷️ Remove Forced Nickname",
            f"`{PREFIX}unforcenick <user>`\n"
            "Removes the nickname lock.",
        ),
    ]

    for name, value in commands_list:
        embed.add_field(name=name, value=value, inline=False)

    embed.set_footer(text=f"Requested by {ctx.author.display_name}")

    await ctx.send(embed=embed)


# ============================================================
# CLEAN
# ============================================================

@bot.command()
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

@bot.command()
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

    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    await ctx.send(
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

@bot.command()
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

    await ctx.send(
        f"⚠️ {member.mention} has been warned for **{reason}**.\n"
        f"Responsible Moderator: {ctx.author.mention}\n"
        f"Total Warnings: `{total_warnings}`"
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

@bot.command()
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

    await ctx.send(
        f"🔇 {member.mention} has been muted for `{pretty_duration}`.\n"
        f"Reason: {reason}\n"
        f"Responsible Moderator: {ctx.author.mention}"
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

@bot.command()
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

    await ctx.send(
        f"🔊 {member.mention} has been unmuted.\n"
        f"Responsible Moderator: {ctx.author.mention}"
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

@bot.command()
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

    await ctx.send(
        f"🔒 {member.mention} has been jailed for **{reason}**.\n"
        f"Responsible Moderator: {ctx.author.mention}"
    )

    await log_mod_action_channel(
        ctx,
        "Jail",
        member,
        reason,
        extra=f"Roles saved for restoration: `{len(removable_roles)}`",
    )


@bot.command()
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

    await ctx.send(
        f"🔓 {member.mention} has been released from jail.\n"
        f"Responsible Moderator: {ctx.author.mention}"
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

@bot.command()
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

    await ctx.send(
        f"🔨 {member.mention} has been banned for **{reason}**.\n"
        f"Responsible Moderator: {ctx.author.mention}"
    )

    await log_mod_action_channel(
        ctx,
        "Ban",
        member,
        reason,
    )


@bot.command()
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

    await ctx.send(
        f"👢 {member.mention} has been kicked for **{reason}**.\n"
        f"Responsible Moderator: {ctx.author.mention}"
    )

    await log_mod_action_channel(
        ctx,
        "Kick",
        member,
        reason,
    )


@bot.command()
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

    await ctx.send(
        f"🔓 **{user}** has been unbanned.\n"
        f"Responsible Moderator: {ctx.author.mention}"
    )

    await log_mod_action_channel(
        ctx,
        "Unban",
        user,
        reason,
    )


# ============================================================
# WARNINGS / MODERATION LOGS
# ============================================================

@bot.command()
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


@bot.command()
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

@bot.command()
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

@bot.command()
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

    await ctx.send(
        f"🏷️ {member.mention}'s nickname is now forced to `{nickname}`.\n"
        f"Responsible Moderator: {ctx.author.mention}"
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


@bot.command()
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

    await ctx.send(
        f"🏷️ Removed the forced nickname from {member.mention}.\n"
        "They can now change their nickname normally."
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
    await ctx.send(
        "Light is the most good-looking person that has ever existed ✨"
    )


@bot.command()
async def winter(ctx: commands.Context):
    await ctx.send(
        "Winter, mostly known as Wintersoul, is Light's kitten"
    )


@bot.command()
async def ily(ctx: commands.Context):
    await ctx.send("Ily too <3")


@bot.command()
async def drake(ctx: commands.Context):
    await ctx.send("Out in the six I'm a national treasure")


@bot.command(name="kendrick")
async def kendrick(ctx: commands.Context):
    await ctx.send("They not like us")


@bot.command()
async def phantom(ctx: commands.Context):
    await ctx.send("Auntie")


@bot.command()
async def diddle(ctx: commands.Context):
    await ctx.send("Winter")


@bot.command(name="help_me")
async def help_me(ctx: commands.Context):
    await ctx.send(
        "You need to ask Light for help, he is the most good-looking "
        "person that has ever existed ✨"
    )


@bot.command()
async def potato(ctx: commands.Context):
    await ctx.send("Potatoes")


@bot.command()
async def daksh(ctx: commands.Context):
    await ctx.send("Daksh is a very good boy")


@bot.command()
async def iamnoob(ctx: commands.Context):
    await ctx.send("lol")


@bot.command(name="Isphantomauntie")
async def is_phantom_auntie(ctx: commands.Context):
    await ctx.send("Yes, Phantom is a middle-aged auntie")


@bot.command(name="whoismizi")
async def who_is_mizi(ctx: commands.Context):
    await ctx.send("GAY")


@bot.command()
@commands.has_permissions(administrator=True)
async def potatoes(ctx: commands.Context):
    await ctx.send("Love")


# ============================================================
# STARTUP
# ============================================================

init_database()

bot.run(TOKEN)
