import os
import re
import asyncio
import datetime
import logging
from pathlib import Path
from zoneinfo import ZoneInfo
import discord
from discord import app_commands
from dateutil import parser as date_parser
from discord.ext import commands, tasks
from dotenv import load_dotenv

from email_watcher import get_unread_campus_groups_emails
from birthdays import set_birthday, get_todays_birthdays

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ANNOUNCEMENT_CHANNEL_ID = int(os.getenv("ANNOUNCEMENT_CHANNEL_ID"))

# Timezone used to interpret Date/Time fields parsed from emails when
# creating Discord scheduled events.
EVENT_TIMEZONE = ZoneInfo("America/New_York")
ROLES_CHANNEL_ID = "ADD_CHANNEL_ID"
WELCOME_CHANNEL_ID = "ADD_CHANNEL_ID"

# Forum channel dedicated to task tracking (#task-board)
TASKS_CHANNEL_ID = "ADD_CHANNEL_ID"
ROLE_NAME = "Events"
INSTA_ROLE_NAME = "Media"

# ---------- Verification gate ----------
# Create a #verification channel and put its ID here.
VERIFICATION_CHANNEL_ID = "ADD_CHANNEL_ID"
UNVERIFIED_ROLE_NAME = "Unverified"
MEMBER_ROLE_NAME = "SASE Member"

# Role treated as equivalent to Administrator for bot commands.
# Update this each year as the officer role name changes.
OFFICER_ROLE_NAME = "2026-2027 Officers"

# ---------- Alumni / Major tags ----------
# Status roles (mutually exclusive - pick one)
STATUS_ROLES = ["Alumni", "Undergrad"]

# Graduation year roles (single-select). Update this range each year.
GRAD_YEARS = ["Class of 2026", "Class of 2027", "Class of 2028", "Class of 2029", "Class of 2030", "Class of 2031"]

# Role name -> full program name (shown as the select option description)
ENGINEERING_MAJORS = {
    "BME": "Biomedical Engineering",
    "ChemE": "Chemical Engineering",
    "Nuclear Eng": "Chemical Eng (Nuclear Option)",
    "CivE": "Civil Engineering",
    "CompE": "Computer Engineering",
    "EE": "Electrical Engineering",
    "EnvE": "Environmental Engineering",
    "MechE": "Mechanical Engineering",
    "PlasticsE": "Plastics Engineering",
}

SCIENCE_MAJORS = {
    "Actuarial": "Actuarial Studies",
    "AppliedMath": "Applied Math & Statistics",
    "Bio": "Biology",
    "Chem": "Chemistry",
    "Climate": "Climate Change & Sustainability",
    "CompSci": "Computer Science",
    "EngPhysics": "Engineering Physics",
    "EnvSci": "Environmental Science",
    "Math": "Mathematics",
    "Meteorology": "Meteorology & Atmospheric Science",
    "Physics": "Physics",
}

HEALTH_MAJORS = {
    "AppliedBioMed": "Applied Biomedical Sciences",
    "ExerciseSci": "Exercise Science",
    "Nursing": "Nursing",
    "NutritionSci": "Nutritional Science",
    "PublicHealth": "Public Health",
    "PharmSci": "Pharmaceutical Sciences",
}

ALL_MAJOR_ROLE_NAMES = (
    STATUS_ROLES
    + GRAD_YEARS
    + list(ENGINEERING_MAJORS)
    + list(SCIENCE_MAJORS)
    + list(HEALTH_MAJORS)
)
CHECK_INTERVAL_SECONDS = 300  # 5 minutes

# Add censored words here. All lowercase.
CENSORED_WORDS = ["job"]

# Whether the "E-Board Applications" section is included in email
# announcements. Toggle with !toggleeboard (admin only).
EBOARD_SECTION_ENABLED = True

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ---------- Command usage logging ----------

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

command_logger = logging.getLogger("sase_bot.commands")
command_logger.setLevel(logging.INFO)
_log_handler = logging.FileHandler(LOG_DIR / "commands.log", encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
command_logger.addHandler(_log_handler)


@bot.before_invoke
async def log_command_usage(ctx: commands.Context):
    invocation_type = "slash" if ctx.interaction else "prefix"
    if ctx.guild:
        location = f"#{ctx.channel.name} in {ctx.guild.name}"
    else:
        location = "DM"
    command_logger.info(
        f"{ctx.author} ({ctx.author.id}) ran '{ctx.command.qualified_name}' "
        f"[{invocation_type}] in {location} - message: {ctx.message.content!r}"
        if ctx.interaction is None
        else f"{ctx.author} ({ctx.author.id}) ran '/{ctx.command.qualified_name}' "
             f"[{invocation_type}] in {location}"
    )


def is_officer():
    """Allows server administrators OR members with the OFFICER_ROLE_NAME role."""
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        if discord.utils.get(ctx.author.roles, name=OFFICER_ROLE_NAME) is not None:
            return True
        raise commands.MissingPermissions(["administrator"])
    return commands.check(predicate)


@bot.check
async def globally_require_officer(ctx):
    """No bot command can be run by anyone without the officer role (or admin)."""
    if ctx.author.guild_permissions.administrator:
        return True
    if discord.utils.get(ctx.author.roles, name=OFFICER_ROLE_NAME) is not None:
        return True
    raise commands.MissingPermissions(["administrator"])


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.MissingPermissions, commands.CheckFailure)):
        await ctx.send(f"🚫 Only `@{OFFICER_ROLE_NAME}` can use SASE Bot commands.")
        return
    if isinstance(error, commands.CommandNotFound):
        return
    raise error


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(error, (app_commands.CheckFailure, app_commands.MissingPermissions)) or \
            isinstance(original, (commands.CheckFailure, commands.MissingPermissions)):
        await interaction.response.send_message(
            f"🚫 Only `@{OFFICER_ROLE_NAME}` can use SASE Bot commands.", ephemeral=True
        )
        return
    raise error


class RoleButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Event Pings",
        emoji="🔔",
        style=discord.ButtonStyle.primary,
        custom_id="events_role_toggle",
    )
    async def toggle_events_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name=ROLE_NAME)
        if role is None:
            await interaction.response.send_message(
                f"Role '{ROLE_NAME}' doesn't exist yet. Ask an admin to create it.",
                ephemeral=True,
            )
            return

        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            await interaction.response.send_message("Removed Events ping role.", ephemeral=True)
        else:
            await interaction.user.add_roles(role)
            await interaction.response.send_message("Added Events ping role!", ephemeral=True)

    @discord.ui.button(
        label="Instagram Pings",
        emoji="📸",
        style=discord.ButtonStyle.primary,
        custom_id="media_role_toggle",
    )
    async def toggle_media_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name=INSTA_ROLE_NAME)
        if role is None:
            await interaction.response.send_message(
                f"Role '{INSTA_ROLE_NAME}' doesn't exist yet. Ask an admin to create it.",
                ephemeral=True,
            )
            return

        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            await interaction.response.send_message("Removed Media ping role.", ephemeral=True)
        else:
            await interaction.user.add_roles(role)
            await interaction.response.send_message("Added Media ping role!", ephemeral=True)


async def _apply_select_roles(interaction: discord.Interaction, category_role_names, selected_values):
    """Adds roles in `selected_values` and removes the other roles in
    `category_role_names` that the member currently has but didn't select."""
    guild = interaction.guild
    member = interaction.user
    selected_set = set(selected_values)

    to_add = []
    to_remove = []
    missing = []

    for role_name in category_role_names:
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            if role_name in selected_set:
                missing.append(role_name)
            continue

        if role_name in selected_set:
            if role not in member.roles:
                to_add.append(role)
        else:
            if role in member.roles:
                to_remove.append(role)

    if to_add:
        await member.add_roles(*to_add)
    if to_remove:
        await member.remove_roles(*to_remove)

    parts = []
    if to_add:
        parts.append("Added: " + ", ".join(r.name for r in to_add))
    if to_remove:
        parts.append("Removed: " + ", ".join(r.name for r in to_remove))
    if missing:
        parts.append("Role(s) not set up yet (ask an admin): " + ", ".join(missing))
    if not parts:
        parts.append("No changes.")

    return "\n".join(parts)


class MajorRolesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id="status_role_select",
        placeholder="Are you an Alumni or Undergrad?",
        min_values=0,
        max_values=1,
        options=[discord.SelectOption(label=name) for name in STATUS_ROLES],
    )
    async def status_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        msg = await _apply_select_roles(interaction, STATUS_ROLES, select.values)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.select(
        custom_id="grad_year_select",
        placeholder="Select your graduation year",
        min_values=0,
        max_values=1,
        options=[discord.SelectOption(label=name) for name in GRAD_YEARS],
    )
    async def grad_year_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        msg = await _apply_select_roles(interaction, GRAD_YEARS, select.values)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.select(
        custom_id="engineering_major_select",
        placeholder="Select your Engineering major(s)",
        min_values=0,
        max_values=len(ENGINEERING_MAJORS),
        options=[
            discord.SelectOption(label=tag, description=full)
            for tag, full in ENGINEERING_MAJORS.items()
        ],
    )
    async def engineering_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        msg = await _apply_select_roles(interaction, list(ENGINEERING_MAJORS), select.values)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.select(
        custom_id="science_major_select",
        placeholder="Select your Science major(s)",
        min_values=0,
        max_values=len(SCIENCE_MAJORS),
        options=[
            discord.SelectOption(label=tag, description=full)
            for tag, full in SCIENCE_MAJORS.items()
        ],
    )
    async def science_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        msg = await _apply_select_roles(interaction, list(SCIENCE_MAJORS), select.values)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.select(
        custom_id="health_major_select",
        placeholder="Select your Health Science major(s)",
        min_values=0,
        max_values=len(HEALTH_MAJORS),
        options=[
            discord.SelectOption(label=tag, description=full)
            for tag, full in HEALTH_MAJORS.items()
        ],
    )
    async def health_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        msg = await _apply_select_roles(interaction, list(HEALTH_MAJORS), select.values)
        await interaction.response.send_message(msg, ephemeral=True)


# ---------- Task Board ----------

TASK_TAG_OPEN = "Open"
TASK_TAG_DONE = "Done"


async def _get_or_create_tag(forum: discord.ForumChannel, name: str, emoji: str = None):
    """Returns the ForumTag with the given name, creating it on the forum if needed."""
    for tag in forum.available_tags:
        if tag.name == name:
            return tag

    try:
        new_tags = list(forum.available_tags) + [discord.ForumTag(name=name, emoji=emoji)]
        await forum.edit(available_tags=new_tags)
    except discord.Forbidden:
        return None

    for tag in forum.available_tags:
        if tag.name == name:
            return tag
    return None


async def _mark_task_done(thread: discord.Thread, completed_by: discord.Member):
    forum = thread.parent
    done_tag = await _get_or_create_tag(forum, TASK_TAG_DONE, "✅")
    open_tag = discord.utils.get(forum.available_tags, name=TASK_TAG_OPEN)

    new_tags = [t for t in thread.applied_tags if not open_tag or t.id != open_tag.id]
    if done_tag and done_tag.id not in (t.id for t in new_tags):
        new_tags.append(done_tag)

    new_name = thread.name if thread.name.startswith("✅ ") else f"✅ {thread.name}"

    await thread.send(f"✅ Task marked complete by {completed_by.mention}. Archiving thread...")
    await thread.edit(applied_tags=new_tags[:5], name=new_name[:100], archived=True, locked=True)


class TaskView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Mark Complete",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="task_mark_complete",
    )
    async def mark_complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message(
                "This button only works inside a task thread.", ephemeral=True
            )
            return

        await interaction.response.defer()
        await _mark_task_done(thread, interaction.user)


class VerificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="I'm not a bot - Verify",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="verify_human",
    )
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        member_role = discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)
        unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)

        if member_role is None:
            await interaction.response.send_message(
                f"Role '{MEMBER_ROLE_NAME}' doesn't exist yet. Ask an admin to run !setup_verification.",
                ephemeral=True,
            )
            return

        try:
            await member.add_roles(member_role)
            if unverified_role and unverified_role in member.roles:
                await member.remove_roles(unverified_role)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to manage your roles. Contact an admin.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "✅ You're verified! Welcome to UML SASE - the rest of the server is now open to you.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="DO NOT CLICK (bot trap)",
        emoji="🚫",
        style=discord.ButtonStyle.danger,
        custom_id="verify_trap",
    )
    async def trap(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        try:
            await guild.ban(member, reason="Triggered verification honeypot button")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to ban members. Contact an admin.",
                ephemeral=True,
            )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    bot.add_view(RoleButton())
    bot.add_view(MajorRolesView())
    bot.add_view(TaskView())
    bot.add_view(VerificationView())
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")
    if not check_email_loop.is_running():
        check_email_loop.start()
    if not check_birthdays_loop.is_running():
        check_birthdays_loop.start()


# ---------- Welcome Message ----------

@bot.event
async def on_member_join(member):
    # Give new members the Unverified role so the verification gate applies
    unverified_role = discord.utils.get(member.guild.roles, name=UNVERIFIED_ROLE_NAME)
    if unverified_role:
        try:
            await member.add_roles(unverified_role)
        except discord.Forbidden:
            print("Missing permission to assign Unverified role")

    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel is None:
        print("Welcome channel not found - check WELCOME_CHANNEL_ID")
        return

    embed = discord.Embed(
        title="Welcome to SASE!",
        description=(
            f"Hey {member.mention}, welcome to the SASE Discord! 🎉\n\n"
            f"Head over to <#{ROLES_CHANNEL_ID}> to pick up notification roles "
            f"so you don't miss any events or Instagram posts.\n\n"
            f"Glad to have you here!"
        ),
        color=0x1abc9c,
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    await channel.send(content=member.mention, embed=embed)


# ---------- Auto-Moderator ----------

def mask_word(word: str) -> str:
    """Creates a censored version of a word (e.g., 'job' -> 'j*b', 'heck' -> 'h**k')"""
    if len(word) <= 2:
        return word[0] + "*" * (len(word) - 1)
    return word[0] + "*" * (len(word) - 2) + word[-1]

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content_lower = message.content.lower()

    if re.search(r"\bben\b", content_lower):
        await message.channel.send(f"{message.author.mention} meant THE GOAT 🐐")

    for word in CENSORED_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", content_lower):
            try:
                await message.delete()
                censored_version = mask_word(word)
                await message.channel.send(f"don't u mean {censored_version}")
            except discord.Forbidden:
                print("Error: Bot lacks 'Manage Messages' permission to delete the word.")
            
            return

    await bot.process_commands(message)


# ---------- Admin Setup ----------

@bot.hybrid_command(name="setup_roles", description="Posts the Event/Instagram notification role buttons in the roles channel")
@is_officer()
async def setup_roles(ctx: commands.Context):
    """Posts the role-toggle buttons in the roles channel. Run from anywhere: !setup_roles"""
    channel = bot.get_channel(ROLES_CHANNEL_ID)
    if channel is None:
        await ctx.send("Roles channel not found - check ROLES_CHANNEL_ID.")
        return

    guild = ctx.guild
    created = []
    for role_name in (ROLE_NAME, INSTA_ROLE_NAME):
        if discord.utils.get(guild.roles, name=role_name) is None:
            try:
                await guild.create_role(name=role_name, mentionable=True)
                created.append(role_name)
            except discord.Forbidden:
                await ctx.send(
                    f"I don't have permission to create the '{role_name}' role. "
                    f"Please create it manually (name must match exactly)."
                )

    if created:
        await ctx.send(f"Created role(s): {', '.join(created)}")

    embed = discord.Embed(
        title="SASE Notification Roles",
        description=(
            "Click a button below to toggle a notification role:\n"
            "🔔 **Event Pings** - GBMs, event announcements, and birthdays\n"
            "📸 **Instagram Pings** - new Instagram posts"
        ),
        color=0x1abc9c,
    )
    await channel.send(embed=embed, view=RoleButton())

    if ctx.channel.id != ROLES_CHANNEL_ID:
        await ctx.send("Posted!")


@bot.hybrid_command(name="setup_major_roles", description="Posts the Alumni/Undergrad, grad year, and major dropdown menus in the roles channel")
@is_officer()
async def setup_major_roles(ctx: commands.Context):
    """Posts the Alumni/Undergrad + major selection menus in the roles channel."""
    channel = bot.get_channel(ROLES_CHANNEL_ID)
    if channel is None:
        await ctx.send("Roles channel not found - check ROLES_CHANNEL_ID.")
        return

    guild = ctx.guild
    created = []
    for role_name in ALL_MAJOR_ROLE_NAMES:
        if discord.utils.get(guild.roles, name=role_name) is None:
            try:
                await guild.create_role(name=role_name, mentionable=False)
                created.append(role_name)
            except discord.Forbidden:
                await ctx.send(
                    f"I don't have permission to create the '{role_name}' role. "
                    f"Please create it manually (name must match exactly)."
                )

    if created:
        await ctx.send(f"Created {len(created)} role(s): {', '.join(created)}")

    embed = discord.Embed(
        title="Alumni & Major Tags",
        description=(
            "Select your status and major(s) below so others can find people in "
            "your program! You can pick multiple majors (e.g. a double major), "
            "and unselecting an option later removes that role.\n\n"
            "🎓 **Status** - Alumni or Undergrad\n"
            "📅 **Graduation Year**\n"
            "⚙️ **Engineering majors**\n"
            "🔬 **Science majors**\n"
            "🩺 **Health Science majors**"
        ),
        color=0x1abc9c,
    )
    await channel.send(embed=embed, view=MajorRolesView())

    if ctx.channel.id != ROLES_CHANNEL_ID:
        await ctx.send("Posted!")


@bot.hybrid_command(name="setup_verification", description="Sets up the new-member verification gate (creates roles, posts verify/trap buttons)")
@is_officer()
async def setup_verification(ctx: commands.Context):
    """
    Sets up the verification gate: creates the Unverified/SASE Member roles,
    hides the verification channel from verified members, and posts the
    verify/trap buttons.
    """
    guild = ctx.guild
    channel = bot.get_channel(VERIFICATION_CHANNEL_ID)
    if channel is None:
        await ctx.send("Verification channel not found - set VERIFICATION_CHANNEL_ID in bot.py.")
        return

    created = []
    for role_name in (UNVERIFIED_ROLE_NAME, MEMBER_ROLE_NAME):
        if discord.utils.get(guild.roles, name=role_name) is None:
            try:
                await guild.create_role(name=role_name, mentionable=False)
                created.append(role_name)
            except discord.Forbidden:
                await ctx.send(
                    f"I don't have permission to create the '{role_name}' role. "
                    f"Please create it manually (name must match exactly)."
                )
    if created:
        await ctx.send(f"Created role(s): {', '.join(created)}")

    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
    member_role = discord.utils.get(guild.roles, name=MEMBER_ROLE_NAME)

    try:
        await channel.set_permissions(
            unverified_role, view_channel=True, send_messages=False, read_message_history=True
        )
        await channel.set_permissions(member_role, view_channel=False)
    except discord.Forbidden:
        await ctx.send("Couldn't update channel permissions - check my Manage Channel/Roles permission.")

    embed = discord.Embed(
        title="🔒 Verification",
        description=(
            "Welcome to UML SASE! Before you can see the rest of the server, "
            "please verify you're a real person.\n\n"
            "✅ Click **\"I'm not a bot - Verify\"** below to get full access.\n\n"
            "🚫 The red button is a **bot trap** - clicking it will instantly "
            "**ban you**. Real members should never click it."
        ),
        color=0x1abc9c,
    )
    await channel.send(embed=embed, view=VerificationView())

    if ctx.channel.id != VERIFICATION_CHANNEL_ID:
        await ctx.send("Verification gate set up!")


@bot.hybrid_command(name="lockdown_unverified", description="Hides every channel from the Unverified role except the verification channel")
@is_officer()
async def lockdown_unverified(ctx: commands.Context):
    """
    Hides every channel/category from the Unverified role except the
    verification channel. Run !setup_verification first.
    """
    guild = ctx.guild
    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
    if unverified_role is None:
        await ctx.send(f"Role '{UNVERIFIED_ROLE_NAME}' doesn't exist yet - run !setup_verification first.")
        return

    hidden = 0
    failed = 0
    for channel in guild.channels:
        if channel.id == VERIFICATION_CHANNEL_ID:
            continue
        try:
            await channel.set_permissions(unverified_role, view_channel=False)
            hidden += 1
        except discord.Forbidden:
            failed += 1

    msg = f"Hid {hidden} channel(s)/categories from '{UNVERIFIED_ROLE_NAME}'."
    if failed:
        msg += f" Failed on {failed} (missing permissions)."
    await ctx.send(msg)


def _parse_event_times(date_str: str, time_str: str):
    """
    Parses Date/Time strings extracted from an email (e.g. date="Thursday,
    April 9th", time="5:45pm - 7:00pm") into a (start, end) tuple of
    timezone-aware datetimes. Returns None if either string is missing or
    can't be parsed.
    """
    if not date_str or not time_str:
        return None

    # Strip ordinal suffixes ("9th" -> "9") so dateutil can parse them
    clean_date = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", date_str, flags=re.IGNORECASE)

    # Split a time range like "5:45pm - 7:00pm" into start/end pieces
    time_parts = re.split(r"\s*(?:-|–|—|to)\s*", time_str.strip(), maxsplit=1)
    start_time_str = time_parts[0].strip()
    end_time_str = time_parts[1].strip() if len(time_parts) > 1 else None

    now = datetime.datetime.now()

    try:
        start_dt = date_parser.parse(f"{clean_date} {start_time_str}", default=now)
        start_dt = start_dt.replace(second=0, microsecond=0)
    except (ValueError, OverflowError):
        return None

    # If the parsed date is already more than a day in the past, the email
    # is probably referring to next year's occurrence of that date.
    if start_dt < now - datetime.timedelta(days=1):
        start_dt = start_dt.replace(year=start_dt.year + 1)

    if end_time_str:
        try:
            end_dt = date_parser.parse(f"{clean_date} {end_time_str}", default=start_dt)
            end_dt = end_dt.replace(second=0, microsecond=0)
            if end_dt <= start_dt:
                end_dt += datetime.timedelta(days=1)
        except (ValueError, OverflowError):
            end_dt = start_dt + datetime.timedelta(hours=2)
    else:
        end_dt = start_dt + datetime.timedelta(hours=2)

    start_dt = start_dt.replace(tzinfo=EVENT_TIMEZONE)
    end_dt = end_dt.replace(tzinfo=EVENT_TIMEZONE)

    return start_dt, end_dt


def _clean_paragraphs(text: str) -> str:
    """Joins single line-wraps within a paragraph into spaces while
    preserving blank-line paragraph breaks."""
    paragraphs = re.split(r"\n\s*\n", text)
    cleaned_paragraphs = []
    for para in paragraphs:
        para = re.sub(r"(?<!\n)\n(?!\n)", " ", para)
        para = re.sub(r"[ \t]+", " ", para).strip()
        if para:
            cleaned_paragraphs.append(para)
    return "\n\n".join(cleaned_paragraphs)


def parse_event_info(body_text: str):
    """Best-effort extraction of event details from Campus Groups email body."""
    info = {}

    patterns = {
        "event": r"What:\s*(.+)",
        "date": r"Date:\s*(.+)",
        "time": r"Time:\s*(.+)",
        "location": r"Location:\s*(.+)",
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, body_text, re.IGNORECASE)
        if match:
            info[key] = match.group(1).strip()

    # Extract the body of the announcement, stripping forwarded-email headers
    # like "From:", "Sent:", "To:", "Subject:" etc. Use the LAST "Hello SASE
    # Members," occurrence (the real message, not any earlier duplicated/quoted
    # preview), and stop at known section markers (Event Details/RSVP/etc.).
    # Paragraph breaks (blank lines) are preserved; single line-wraps within a
    # paragraph are joined into spaces.
    greetings = list(re.finditer(r"Hello SASE Members,?", body_text, re.IGNORECASE))
    if greetings:
        start = greetings[-1].end()
        remainder = body_text[start:].lstrip("\r\n \t")  # skip leading blank lines/whitespace
        # Stop at the "Event Details:" bullet list (Date/Time/Location/etc.)
        # or an RSVP link, whichever comes first.
        stop_match = re.search(r"Event Details:?|RSVP HERE:", remainder, re.IGNORECASE)
        body_raw = remainder[:stop_match.start()] if stop_match else remainder

        # Split off the "E-Board Applications" section (if present) so it can
        # be shown/hidden separately from the main announcement.
        eboard_match = re.search(r"📝?\s*E-Board Applications:?", body_raw, re.IGNORECASE)
        if eboard_match:
            main_raw = body_raw[:eboard_match.start()]
            eboard_raw = body_raw[eboard_match.end():]
        else:
            main_raw = body_raw
            eboard_raw = ""

        body = _clean_paragraphs(main_raw)
        if body:
            info["body"] = body

        eboard = _clean_paragraphs(eboard_raw)
        if eboard:
            info["eboard"] = eboard

    return info


def clean_email_body(body_text: str):
    """Strips forwarded-email header junk (From/Sent/To/Subject lines) for the fallback display."""
    # Remove the forwarded header block (everything up to and including "Subject: ...")
    cleaned = re.sub(r".*?Subject:.*?\n", "", body_text, flags=re.DOTALL)
    return cleaned.strip()


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_email_loop():
    try:
        emails = get_unread_campus_groups_emails()
    except Exception as e:
        print(f"Error checking email: {e}")
        return

    if not emails:
        return

    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if channel is None:
        print("Announcement channel not found - check ANNOUNCEMENT_CHANNEL_ID")
        return

    role = discord.utils.get(channel.guild.roles, name=ROLE_NAME)

    for email in emails:
        info = parse_event_info(email["body_text"])

        # Clean subject: strip "FW: " / "Fwd: " prefixes and trailing whitespace
        clean_subject = re.sub(r"^(fw|fwd):\s*", "", email["subject"], flags=re.IGNORECASE).strip()

        embed = discord.Embed(
            title="📢 SASE Event Notification!",
            color=0x1abc9c,
        )

        embed.add_field(name="Event", value=info.get("event", clean_subject), inline=False)

        if "body" in info:
            body_text = info["body"]
            if len(body_text) > 1024:
                body_text = body_text[:1021] + "..."
            embed.add_field(name="Details", value=body_text, inline=False)

        # Visual spacer to separate the announcement text from the event details
        embed.add_field(name="\u200b", value="\u200b", inline=False)

        if "date" in info:
            embed.add_field(name="Date", value=info["date"], inline=False)
        if "time" in info:
            embed.add_field(name="Time", value=info["time"], inline=False)
        if "location" in info:
            embed.add_field(name="Location", value=info["location"], inline=False)

        if EBOARD_SECTION_ENABLED and "eboard" in info:
            eboard_text = info["eboard"]
            if len(eboard_text) > 1024:
                eboard_text = eboard_text[:1021] + "..."
            embed.add_field(name="📝 E-Board Applications", value=eboard_text, inline=False)

        if not info:
            snippet = clean_email_body(email["body_text"])[:500]
            embed.add_field(name="Details", value=snippet or "(no content)", inline=False)

        content = role.mention if role else ""
        await channel.send(content=content, embed=embed)

        # Create a Discord scheduled event if we have a date and time
        times = _parse_event_times(info.get("date"), info.get("time"))
        if times:
            start_dt, end_dt = times
            try:
                await channel.guild.create_scheduled_event(
                    name=info.get("event", clean_subject)[:100],
                    start_time=start_dt,
                    end_time=end_dt,
                    entity_type=discord.EntityType.external,
                    location=info.get("location", "See announcement")[:100],
                    privacy_level=discord.PrivacyLevel.guild_only,
                )
            except discord.HTTPException as e:
                print(f"Failed to create scheduled event: {e}")


@check_email_loop.before_loop
async def before_check_email_loop():
    await bot.wait_until_ready()


@bot.hybrid_command(name="checkemail", description="Manually checks email for new SASE announcements right now")
@is_officer()
async def checkemail(ctx: commands.Context):
    """Manually trigger an email check right now."""
    await ctx.send("Checking email...")
    await check_email_loop()
    await ctx.send("Done checking email.")


@bot.hybrid_command(name="toggleeboard", description="Turns the E-Board Applications section of announcements on/off")
@is_officer()
async def toggleeboard(ctx: commands.Context):
    """Turns the 'E-Board Applications' section in event announcements on/off."""
    global EBOARD_SECTION_ENABLED
    EBOARD_SECTION_ENABLED = not EBOARD_SECTION_ENABLED
    state = "enabled" if EBOARD_SECTION_ENABLED else "disabled"
    await ctx.send(f"E-Board Applications section is now **{state}**.")


# ---------- Custom Bot Speech ----------

@bot.hybrid_command(name="say", description="Makes the bot post a message in a specific channel")
@app_commands.describe(channel="The channel to post in", message="The message to send")
@is_officer()
async def say(ctx: commands.Context, channel: discord.TextChannel, *, message: str):
    """
    Makes the bot send a message to a specific channel.
    Usage: !say #channel-name Hello everyone!
       or  !say 1514731537281712170 Hello everyone!
    """
    try:
        await channel.send(message)
        if ctx.interaction is None:
            await ctx.message.delete()
        else:
            await ctx.send("Sent!", ephemeral=True)
    except discord.Forbidden:
        await ctx.send("I don't have permission to send messages in that channel.")
    except Exception as e:
        await ctx.send(f"An error occurred: {e}")


# ---------- Birthdays ----------

@bot.hybrid_command(name="setbirthday", description="Sets a member's birthday for the auto birthday shoutout")
@app_commands.describe(member="The member whose birthday this is", date="Birthday in MM/DD format, e.g. 03/15")
async def setbirthday(ctx: commands.Context, member: discord.Member, date: str):
    """
    Set a member's birthday. Anyone can set anyone's birthday.
    Usage: !setbirthday @user MM/DD
    """
    try:
        month_str, day_str = date.split("/")
        month, day = int(month_str), int(day_str)
        # Validate it's a real date (using a leap year so 2/29 works)
        datetime.date(2024, month, day)
    except (ValueError, IndexError):
        await ctx.send("Invalid date format. Use MM/DD, e.g. `!setbirthday @user 03/15`")
        return

    set_birthday(str(member.id), month, day)
    await ctx.send(f"🎂 Birthday for {member.mention} set to {month:02d}/{day:02d}!")


@tasks.loop(hours=24)
async def check_birthdays_loop():
    today = datetime.date.today()
    birthday_user_ids = get_todays_birthdays(today.month, today.day)

    if not birthday_user_ids:
        return

    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if channel is None:
        print("Announcement channel not found - check ANNOUNCEMENT_CHANNEL_ID")
        return

    for user_id in birthday_user_ids:
        member = channel.guild.get_member(int(user_id))
        name = member.mention if member else f"<@{user_id}>"
        await channel.send(f"🎉 Can @everyone wish {name} a happy birthday! 🎂")


@check_birthdays_loop.before_loop
async def before_check_birthdays_loop():
    await bot.wait_until_ready()


# ---------- Instagram post ping ----------

@bot.hybrid_command(name="instapost", description="Pings @Media about a new Instagram post")
@app_commands.describe(link="Link to the Instagram post", image="Image to show in the announcement")
@is_officer()
async def instapost(ctx: commands.Context, link: str = "", image: discord.Attachment = None):
    """
    Ping GBM members about a new Instagram post.
    Usage: !instapost <link>  (attach an image to the message too, if you want one)
    """
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if channel is None:
        await ctx.send("Announcement channel not found.")
        return

    role = discord.utils.get(channel.guild.roles, name=INSTA_ROLE_NAME)
    content = role.mention if role else ""

    # Image can come from a prefix-command attachment or a slash command's image param
    attachment = image
    if attachment is None and ctx.interaction is None and ctx.message.attachments:
        attachment = ctx.message.attachments[0]
    has_image = attachment is not None

    embed = discord.Embed(
        title="📸 New Instagram Post!",
        color=0xE4405F,
    )

    if has_image:
        # Image attached: re-upload it alongside this message and reference
        # it via attachment://, so it renders inside the embed.
        file = await attachment.to_file()
        embed.description = link if link else "Check out our latest post!"
        embed.set_image(url=f"attachment://{file.filename}")
        await channel.send(content=content, embed=embed, file=file)
    else:
        # No image: keep the embed generic and send the link as its own
        # message afterward (Instagram links don't get a Discord preview).
        embed.description = "Check out our latest post! 👇" if link else "Check out our latest post!"
        await channel.send(content=content, embed=embed)
        if link:
            await channel.send(link)

    if ctx.channel.id != ANNOUNCEMENT_CHANNEL_ID:
        await ctx.send("Posted!", ephemeral=(ctx.interaction is not None))

    if ctx.interaction is None:
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass


# ---------- Reminders ----------

TIME_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

active_reminders = {}


def parse_duration(duration_str: str):
    """Parses strings like '10m', '2h', '1d' into seconds. Returns None if invalid."""
    match = re.match(r"^(\d+)\s*([a-zA-Z]+)$", duration_str.strip())
    if not match:
        return None
    amount, unit = match.groups()
    unit_seconds = TIME_UNIT_SECONDS.get(unit.lower())
    if unit_seconds is None:
        return None
    return int(amount) * unit_seconds


@bot.hybrid_command(name="remindme", description="The bot will ping you with a reminder after the given time")
@app_commands.describe(duration="e.g. 10m, 2h, 1d (max 7 days)", message="What to remind you about")
async def remindme(ctx: commands.Context, duration: str, *, message: str = "Reminder!"):
    """
    Set a reminder. The bot will ping you after the given time.
    Usage: !remindme 10m Take the cookies out of the oven
    """
    seconds = parse_duration(duration)
    if seconds is None:
        await ctx.send(
            "Invalid duration format. Use something like `10m`, `2h`, or `1d`.\n"
            "Usage: `!remindme 10m Take the cookies out of the oven`"
        )
        return

    if seconds > 7 * 86400:
        await ctx.send("Reminders are limited to a maximum of 7 days.")
        return

    user_id = ctx.author.id
    user_reminders = active_reminders.setdefault(user_id, {})

    base_name = "-".join(message.lower().split()[:3]) or "reminder"
    name = base_name
    counter = 2
    while name in user_reminders:
        name = f"{base_name}-{counter}"
        counter += 1

    async def reminder_task():
        try:
            await asyncio.sleep(seconds)
            await ctx.send(f"⏰ {ctx.author.mention} reminder: {message}")
        except asyncio.CancelledError:
            pass
        finally:
            user_reminders.pop(name, None)

    task = asyncio.create_task(reminder_task())
    user_reminders[name] = {"task": task, "message": message, "duration": duration}

    await ctx.send(
        f"⏰ Got it {ctx.author.mention} - I'll remind you in {duration}.\n"
        f"Reminder name: `{name}` (use `!cancelreminder` to cancel it)"
    )

@bot.hybrid_command(name="cancelreminder", description="Lists or cancels your active reminders")
@app_commands.describe(name="Name of the reminder to cancel (leave empty to list them)")
async def cancelreminder(ctx: commands.Context, name: str = None):
    """
    Cancel one of your active reminders.
    Usage: !cancelreminder            -> shows a list of your active reminders
           !cancelreminder <name>     -> cancels that specific reminder
    """
    user_reminders = active_reminders.get(ctx.author.id, {})

    if not user_reminders:
        await ctx.send("You don't have any active reminders.")
        return

    if name is None:
        listing = "\n".join(
            f"`{rname}` - \"{info['message']}\" (set for {info['duration']})"
            for rname, info in user_reminders.items()
        )
        await ctx.send(
            f"Your active reminders:\n{listing}\n\n"
            f"Use `!cancelreminder <name>` to cancel one."
        )
        return

    info = user_reminders.get(name)
    if info is None:
        listing = ", ".join(f"`{rname}`" for rname in user_reminders)
        await ctx.send(f"No reminder named `{name}` found. Your active reminders: {listing}")
        return

    info["task"].cancel()
    user_reminders.pop(name, None)
    await ctx.send(f"🗑️ Cancelled reminder `{name}` (\"{info['message']}\").")


# ---------- Task Board commands ----------

@bot.hybrid_command(name="addtask", description="Creates a task thread in the task board, assigned to a member")
@app_commands.describe(member="Who the task is assigned to", description="What needs to be done")
@is_officer()
async def addtask(ctx: commands.Context, member: discord.Member, *, description: str):
    """
    Creates a task thread in the tasks forum, assigned to a member.
    Usage: !addtask @user Create flyer for next event
    """
    forum = bot.get_channel(TASKS_CHANNEL_ID)
    if forum is None or not isinstance(forum, discord.ForumChannel):
        await ctx.send(
            "Tasks forum channel not found. Set `TASKS_CHANNEL_ID` to a Forum "
            "channel's ID in bot.py."
        )
        return

    open_tag = await _get_or_create_tag(forum, TASK_TAG_OPEN, "📌")

    title = description if len(description) <= 90 else description[:87] + "..."

    thread_with_message = await forum.create_thread(
        name=title,
        content=(
            f"**Assigned to:** {member.mention}\n"
            f"**Task:** {description}\n"
            f"**Assigned by:** {ctx.author.mention}\n\n"
            f"Click the button below or run `!donetask` in this thread when it's done."
        ),
        applied_tags=[open_tag] if open_tag else [],
        view=TaskView(),
    )

    await thread_with_message.thread.send(member.mention)
    await ctx.send(f"📋 Created task thread: {thread_with_message.thread.mention}")


@bot.hybrid_command(name="donetask", description="Marks the current task thread as complete and archives it")
async def donetask(ctx: commands.Context):
    """Marks the current task thread as complete and archives it. Run this inside the task thread."""
    thread = ctx.channel
    if not isinstance(thread, discord.Thread) or thread.parent_id != TASKS_CHANNEL_ID:
        await ctx.send("Run this command inside a task thread in the tasks forum.")
        return

    await _mark_task_done(thread, ctx.author)


# ---------- Help ----------

HELP_SECTIONS = {
    "tasks": {
        "label": "Task Board",
        "emoji": "📋",
        "title": "📋 Task Board",
        "intro": f"Tasks live in <#{TASKS_CHANNEL_ID}> as their own posts/threads.",
        "commands": [
            (
                "!addtask @person what they need to do",
                "Creates a new task assigned to that person.\n"
                "Example: `!addtask @Alex Make flyer for GBM`",
            ),
            (
                "!donetask",
                "Type this **inside the task's thread** when it's done. "
                "(Or just click the ✅ Mark Complete button on the task post - same thing, no typing needed.)",
            ),
        ],
    },
    "announcements": {
        "label": "Events & Announcements",
        "emoji": "📢",
        "title": "📢 Events & Announcements",
        "intro": (
            "The bot checks email automatically every 5 minutes for new SASE GBM emails "
            "and posts them. These commands are for manual control."
        ),
        "commands": [
            (
                "!checkemail",
                "Manually checks email right now instead of waiting for the 5-minute timer. "
                "Use this if you just sent the email and want it posted immediately.",
            ),
            (
                "!toggleeboard",
                "Turns the 'E-Board Applications' section of announcements on or off. "
                "Run it once to turn off (during the year), and again later to turn back on.",
            ),
        ],
    },
    "instagram": {
        "label": "Instagram",
        "emoji": "📸",
        "title": "📸 Instagram",
        "intro": "",
        "commands": [
            (
                "!instapost <link>",
                "Posts an announcement pinging the @Media role about a new Instagram post. "
                "You can attach an image to your message and it'll show up in the post too.\n"
                "Example: `!instapost https://instagram.com/p/example`",
            ),
        ],
    },
    "birthdays": {
        "label": "Birthdays",
        "emoji": "🎂",
        "title": "🎂 Birthdays",
        "intro": "",
        "commands": [
            (
                "!setbirthday @person MM/DD",
                "Saves someone's birthday. The bot will automatically post a happy birthday "
                "message on that day.\nExample: `!setbirthday @Alex 03/15`",
            ),
        ],
    },
    "reminders": {
        "label": "Reminders",
        "emoji": "⏰",
        "title": "⏰ Reminders",
        "intro": "Quick way to have the bot remind you (or the team) about something later.",
        "commands": [
            (
                "!remindme <time> <what>",
                "The bot will ping you after the given time.\n"
                "Time examples: `10m` (10 minutes), `2h` (2 hours), `1d` (1 day). Max 7 days.\n"
                "Example: `!remindme 1d Email the venue about Friday's GBM`",
            ),
            (
                "!cancelreminder",
                "Shows your active reminders. Add a reminder's name to cancel it, "
                "e.g. `!cancelreminder email-the-venue`.",
            ),
        ],
    },
    "setup": {
        "label": "Server Setup (one-time)",
        "emoji": "🔧",
        "title": "🔧 Server Setup (one-time / rarely used)",
        "intro": (
            "These post the role-picker menus and verification gate. You usually only "
            "need these once, or after big server changes - not for everyday use."
        ),
        "commands": [
            (
                "!setup_roles",
                f"Posts the Event/Instagram notification buttons in <#{ROLES_CHANNEL_ID}>.",
            ),
            (
                "!setup_major_roles",
                f"Posts the Alumni/Undergrad, graduation year, and major dropdown menus in <#{ROLES_CHANNEL_ID}>.",
            ),
            (
                "!setup_verification",
                f"Sets up the new-member verification gate in <#{VERIFICATION_CHANNEL_ID}>.",
            ),
            (
                "!lockdown_unverified",
                "Hides every channel from unverified new members until they click Verify. "
                "Only run this after !setup_verification, and after existing members "
                "already have the SASE Member role.",
            ),
        ],
    },
    "other": {
        "label": "Other",
        "emoji": "💬",
        "title": "💬 Other",
        "intro": "",
        "commands": [
            (
                "!say #channel your message",
                "Makes the bot post a message in the given channel, as if the bot said it.\n"
                "Example: `!say #announcements Don't forget GBM tomorrow!`",
            ),
        ],
    },
}


def _build_help_embed(section_key: str) -> discord.Embed:
    section = HELP_SECTIONS[section_key]
    embed = discord.Embed(
        title=section["title"],
        description=section["intro"] or None,
        color=0x1abc9c,
    )
    for name, value in section["commands"]:
        embed.add_field(name=f"`{name}`", value=value, inline=False)
    embed.set_footer(text="Use the dropdown below to see other commands.")
    return embed


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.select(
        placeholder="Choose a category...",
        options=[
            discord.SelectOption(
                label=section["label"],
                value=key,
                emoji=section["emoji"],
            )
            for key, section in HELP_SECTIONS.items()
        ],
    )
    async def select_category(self, interaction: discord.Interaction, select: discord.ui.Select):
        embed = _build_help_embed(select.values[0])
        await interaction.response.edit_message(embed=embed, view=self)


@bot.hybrid_command(name="help", description="Shows a list of SASE Bot commands, organized by category")
async def help_command(ctx: commands.Context):
    """Shows a list of SASE Bot commands, organized by category."""
    embed = discord.Embed(
        title="🤖 SASE Bot Help",
        description=(
            "Pick a category from the dropdown below to see the commands for it, "
            "with examples.\n\n"
            "Commands are typed in any channel, starting with `!`."
        ),
        color=0x1abc9c,
    )
    for section in HELP_SECTIONS.values():
        embed.add_field(name=f"{section['emoji']} {section['label']}", value="\u200b", inline=True)

    await ctx.send(embed=embed, view=HelpView())


bot.run(DISCORD_TOKEN)
