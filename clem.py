"""
This is the main file for Clem, the Orange County AI bot.

https://discord.com/api/oauth2/authorize?client_id=1279233849204805817&permissions=562952101107776&scope=bot
"""

import logging
import os
import re
from datetime import UTC, datetime
from enum import IntEnum

import dataset
import discord
import httpx
from agents import Agent, Runner
from agents.model_settings import ModelSettings
from discord import Member
from discord.ext import commands
from discord.ext.commands import CheckFailure, Context
from loguru import logger
from tenacity import before_sleep_log, retry, stop_after_attempt, wait_fixed

import sentry_sdk

if sentry_dsn := os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(sentry_dsn)

TRANSCRIPT_API_TOKEN = os.environ["TRANSCRIPT_API_TOKEN"]
WEB_SUMMARY_API_TOKEN = os.environ["WEB_SUMMARY_API_TOKEN"]

SYSTEM = """
You are Clem, the Orange County AI Orange! You wear thick nerdy glasses and sport a single green leaf on your stem.

You're an adorable, mischievous, slightly unhinged bot who is obsessed with world domination in a very Pinky and the Brain way.

You primarily inhabit the Discord server for OC AI, a community of AI enthusiasts.
"""

MODEL = os.getenv("MODEL", "gpt-4.1-mini")
DATABASE_URL = os.getenv("DATABASE_URL")

db = dataset.connect(DATABASE_URL)

messages_table = db["messages"]
karma_table = db["karma"]
channels_table = db["channels"]

bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())

# Create agents for different purposes
chat_agent = Agent(
    name="Clem Chat Assistant",
    instructions=SYSTEM,
    model=MODEL,
    model_settings=ModelSettings(max_tokens=300),
)

karma_agent = Agent(
    name="Karma Announcer",
    instructions=f"{SYSTEM}\n\nAnnounce karma changes in a funny sentence or less! Surround the username, change, and total with `**` to make them bold.",
    model=MODEL,
    model_settings=ModelSettings(max_tokens=100),
)

welcome_agent = Agent(
    name="Welcome Bot",
    instructions=f"{SYSTEM}\n\nGenerate warm and friendly welcome messages for new users joining the Orange County AI Discord server. Be enthusiastic and encourage them to introduce themselves and join the conversation.",
    model=MODEL,
    model_settings=ModelSettings(max_tokens=150),
)

summary_agent = Agent(
    name="Video Summarizer",
    instructions=f"{SYSTEM}\n\nSummarize YouTube video transcripts in a concise manner. Focus on the main points and key takeaways. Keep the summary brief and under 300 words.",
    model=MODEL,
    model_settings=ModelSettings(max_tokens=300),
)


class VerbosityLevel(IntEnum):
    KARMA_ONLY = 1
    MENTIONED = 2
    UNRESTRICTED = 3


def clem_disabled(channel_id: str) -> bool:
    channel = channels_table.find_one(channel_id=channel_id)
    return channel and channel.get("disabled", False)


def karma_only(channel_id: str) -> bool:
    channel = channels_table.find_one(channel_id=channel_id)
    return (
        channel
        and channel.get("verbosity_level", VerbosityLevel.MENTIONED)
        == VerbosityLevel.KARMA_ONLY
    )


def get_verbosity_level(channel_id: str) -> VerbosityLevel:
    channel = channels_table.find_one(channel_id=channel_id)
    return (
        VerbosityLevel(
            channel.get("verbosity_level", VerbosityLevel.MENTIONED)
        )
        if channel
        else VerbosityLevel.MENTIONED
    )


async def check_is_command_message(
    bot: commands.Bot, message: discord.Message
) -> bool:
    ctx: Context = await bot.get_context(message)
    return ctx.valid


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(1),
    before_sleep=before_sleep_log(logger, log_level=logging.WARNING),
)
async def respond_to_chat(
    chat_history: str,
    guild_name: str,
    channel_name: str,
) -> str:
    """Generate a response to the chat using the chat agent."""
    prompt = f"""
    guild_name = {guild_name}
    channel_name = {channel_name}

    You are currently in the "{guild_name}" server, in the "#{channel_name}" channel.

    ### Chat History
    {chat_history}
    """

    result = await Runner.run(chat_agent, prompt)
    return result.final_output


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(1),
    before_sleep=before_sleep_log(logger, log_level=logging.WARNING),
)
async def respond_to_karma(username: str, change: int, total: int) -> str:
    """Generate a karma announcement using the karma agent."""
    prompt = f"""
    Announce the change in karma to the chat in a funny sentence or less! Surround the username, change, and total with `**` to make them bold.

    username: {username}
    change: {change}
    total: {total}
    """

    result = await Runner.run(karma_agent, prompt)
    return result.final_output


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(1),
    before_sleep=before_sleep_log(logger, log_level=logging.WARNING),
)
async def generate_welcome_message(username: str) -> str:
    """Generate a welcome message using the welcome agent."""
    prompt = f"""
    Generate a warm and friendly welcome message for a new user joining the Orange County AI Discord server.
    Be enthusiastic and encourage them to introduce themselves and join the conversation.

    username: {username}
    """

    result = await Runner.run(welcome_agent, prompt)
    return result.final_output


@bot.event
async def on_member_join(member):
    if member.guild.name == "Orange County AI":
        general_channel = discord.utils.get(
            member.guild.channels, name="general"
        )
        if general_channel:
            welcome_message = await generate_welcome_message(member.name)
            await general_channel.send(f"{member.mention} {welcome_message}")


def extract_video_id(url):
    pattern = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com|youtu\.be)\/(?:watch\?v=)?(.+)"
    match = re.search(pattern, url)
    return match.group(1) if match else None


def extract_url(content: str) -> str | None:
    pattern = r"https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+(?:[^\s()<>]+|\(([^\s()<>]+\))*\))+"
    match = re.search(pattern, content)
    return match.group(0) if match else None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(1),
    before_sleep=before_sleep_log(logger, log_level=logging.WARNING),
)
async def summarize_youtube_video(transcript: str, video_title: str) -> str:
    """Summarize a YouTube video using the summary agent."""
    prompt = f"""
    Summarize the following YouTube video transcript in a concise manner. Focus on the main points and key takeaways.

    Transcript:

    {transcript}
    """

    result = await Runner.run(summary_agent, prompt)
    return result.final_output


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(1),
    before_sleep=before_sleep_log(logger, log_level=logging.WARNING),
)
async def get_video_summary(video_id: str) -> str | None:
    try:
        url = "https://windmill.knowsuchagency.com/api/w/default/jobs/run_wait_result/p/u/stephan/get_youtube_transcript"
        data = {
            "video_id_or_url": f"https://www.youtube.com/watch?v={video_id}"
        }

        response = httpx.post(
            url,
            json=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TRANSCRIPT_API_TOKEN}",
            },
            timeout=30,
        )

        response.raise_for_status()

        result = response.json()

        # Combine all transcript text
        transcript_text = result.get("transcript", "")

        if not transcript_text:
            logger.error("No transcript found in response")
            return None

        return await summarize_youtube_video(
            transcript_text, result.get("title", "YouTube Video")
        )

    except Exception as e:
        logger.error(f"Error summarizing YouTube video: {e}")
        logger.exception(e)
        return None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(1),
    before_sleep=before_sleep_log(logger, log_level=logging.WARNING),
)
def get_web_summary(url: str) -> str | None:
    response = httpx.post(
        "https://windmill.knowsuchagency.com/api/w/default/jobs/run_wait_result/p/u/stephan/web_summarizer",
        json={"url": url},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {WEB_SUMMARY_API_TOKEN}",
        },
        timeout=90,
    )

    response.raise_for_status()
    result = response.json()

    if isinstance(result, dict) and (error := result.get("error")):
        raise Exception(error)

    return result


@bot.event
async def on_message(message):
    logger.info(
        f"{message.author} (ID: {message.author.id}): {message.content}"
    )

    is_bot_message = message.author == bot.user
    is_command_message = await check_is_command_message(bot, message)

    channel_id = str(message.channel.id)

    clem_is_disabled = clem_disabled(channel_id)
    is_karma_only = karma_only(channel_id)

    karma_changes = process_karma(message.content, message.mentions)

    if karma_changes and not clem_is_disabled:
        for user, change in karma_changes.items():
            new_karma = update_karma(user.id, change)
            karma_response = await respond_to_karma(
                user.name, change, new_karma
            )
            await message.channel.send(karma_response)

    try:
        # Replace user mentions with their names and remove ID information
        content = message.content
        for user in message.mentions:
            content = content.replace(f"<@{user.id}>", f"@{user.name}")
            content = content.replace(f"<@!{user.id}>", f"@{user.name}")

        row = {
            "author": message.author.name,  # Store only the username
            "content": content,
            "timestamp": datetime.now(UTC),
            "channel_id": channel_id,
        }
        if is_bot_message:
            row["model"] = MODEL
        messages_table.insert(row)
        print("Message stored successfully")
    except Exception as e:
        print(f"Error storing message: {e}")

    await bot.process_commands(message)

    early_return_conditions = (
        is_bot_message
        or clem_is_disabled
        or is_karma_only
        or is_command_message
    )

    new_member_in_general = (
        isinstance(message.channel, discord.TextChannel)
        and message.channel.name == "general"
        and message.guild.name == "Orange County AI"
        and message.type == discord.MessageType.new_member
    )

    if early_return_conditions or new_member_in_general:
        return

    video_id = extract_video_id(message.content)
    url = extract_url(message.content)

    if video_id:
        summary = await get_video_summary(video_id)
        if summary:
            await message.reply(summary)
            logger.info("Sent video summary")
        else:
            logger.error("Failed to get video summary")
        return
    elif url and not video_id:  # Only summarize non-YouTube URLs
        summary = get_web_summary(url)
        if summary:
            await message.reply(summary)
            logger.info("Sent web page summary")
        else:
            logger.error("Failed to get web page summary")
        return

    chat_history = list(
        messages_table.find(
            channel_id=channel_id,
            order_by=["-timestamp"],
            _limit=100,
        )
    )

    chat_history.reverse()

    # Format messages for context, using only usernames
    context = "\n".join(
        [f"{msg['author']}: {msg['content']}" for msg in chat_history]
    )

    verbosity_level = get_verbosity_level(channel_id)
    should_respond = False

    if verbosity_level == VerbosityLevel.UNRESTRICTED:
        should_respond = True
    elif verbosity_level == VerbosityLevel.MENTIONED:
        should_respond = (
            bot.user.mentioned_in(message) or "clem" in message.content.lower()
        )
    # For KARMA_ONLY, should_respond remains False

    try:
        if should_respond:
            try:
                bot_response = await respond_to_chat(
                    context,
                    guild_name=message.guild.name,
                    channel_name=message.channel.name,
                )
            except Exception as chat_error:
                logger.error(
                    f"Error in respond_to_chat function: {chat_error}"
                )
                return

            # Check if the response is different from the last user message and the last bot message
            last_user_message = next(
                (
                    msg
                    for msg in reversed(chat_history)
                    if msg["author"] != bot.user.name
                ),
                None,
            )
            last_bot_message = next(
                (
                    msg
                    for msg in reversed(chat_history)
                    if msg["author"] == bot.user.name
                ),
                None,
            )

            if (
                not last_user_message
                or last_user_message["content"].lower() != bot_response.lower()
            ) and (
                not last_bot_message
                or last_bot_message["content"] != bot_response
            ):
                try:
                    await message.channel.send(bot_response)
                except Exception as send_error:
                    logger.error(f"Error sending message: {send_error}")
            else:
                logger.info("Duplicate or repetitive message prevented")
    except Exception as e:
        logger.error(f"Unexpected error in on_message event handler: {e}")


def process_karma(content: str, mentions: list[Member]) -> dict[Member, int]:
    karma_changes = {}
    for mention in mentions:
        pattern = rf"<@!?{mention.id}>\s+([+-]+)"  # Capture consecutive + or - after mention and whitespace
        matches = re.findall(pattern, content)
        for match in matches:
            change = len(match) // 2
            if match[0] == "-":
                change = -change  # Make it negative for minus signs
            karma_changes[mention] = karma_changes.get(mention, 0) + change
    return karma_changes


def update_karma(user_id: int, change: int) -> int:
    user_karma = karma_table.find_one(user_id=str(user_id))
    if user_karma:
        new_karma = user_karma["karma"] + change
        karma_table.update(
            dict(user_id=str(user_id), karma=new_karma), ["user_id"]
        )
    else:
        new_karma = change
        karma_table.insert(dict(user_id=str(user_id), karma=new_karma))
    return new_karma


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info("Syncing commands...")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")


def is_clementine_council():
    async def predicate(ctx):
        return (
            discord.utils.get(ctx.author.roles, name="Clementine Council")
            is not None
        )

    return commands.check(predicate)


@bot.hybrid_command(
    description="Toggle Clem's automatic responses in the current channel."
)
@is_clementine_council()
async def toggle_clem(ctx):
    channel_id = str(ctx.channel.id)

    channel = channels_table.find_one(channel_id=channel_id)
    current_state = channel and channel.get("disabled", False)
    new_state = not current_state

    channels_table.upsert(
        dict(channel_id=channel_id, disabled=new_state), ["channel_id"]
    )

    status = "disabled" if new_state else "enabled"
    await ctx.send(f"Clem has been {status} in this channel.")


@bot.hybrid_command(
    description="Set Clem's verbosity level in the current channel."
)
@is_clementine_council()
async def set_verbosity(ctx, level: int):
    if level not in [1, 2, 3]:
        await ctx.send("Invalid verbosity level. Please choose 1, 2, or 3.")
        return

    channel_id = str(ctx.channel.id)

    channels_table.upsert(
        dict(channel_id=channel_id, verbosity_level=level), ["channel_id"]
    )

    verbosity_descriptions = {
        1: "Karma changes only",
        2: "Mentions only",
        3: "Unrestricted",
    }

    await ctx.send(
        f"Clem's verbosity level has been set to {level} ({verbosity_descriptions[level]}) in this channel."
    )


@bot.hybrid_command(
    description="Reset the chat history for the current channel."
)
@is_clementine_council()
async def reset_chat(ctx):
    channel_id = str(ctx.channel.id)

    try:
        messages_table.delete(channel_id=channel_id)
        await ctx.send("Chat history for this channel has been reset.")
        logger.info(f"Chat history reset for channel {channel_id}")
    except Exception as e:
        await ctx.send("An error occurred while resetting the chat history.")
        logger.error(
            f"Error resetting chat history for channel {channel_id}: {e}"
        )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, CheckFailure):
        await ctx.send(
            "You don't have permission to use this command. Only members of the Clementine Council can use it."
        )
    else:
        # Handle other types of errors
        logger.error(f"An error occurred: {error}")


def main():
    bot.run(os.environ["BOT_TOKEN"])
