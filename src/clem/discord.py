"""
This is the main file for Clem, the Orange County AI bot.

https://discord.com/api/oauth2/authorize?client_id=1279233849204805817&permissions=562952101107776&scope=bot
"""

import os
import re
from discord.ext.commands import Context, CheckFailure

import discord
from discord import Member
from discord.ext import commands
from loguru import logger
from promptic import llm
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_fixed
from enum import IntEnum
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
)
from pocketbase import PocketBase
from pocketbase.client import Client


SYSTEM = """
You are Clem, the Orange County AI Orange! You wear thick nerdy glasses and sport a single green leaf on your stem.

You're a cute, friendly bot who is obsessed with world domination
in a very Pinky and the Brain way.

You primarily inhabit the Discord
server for OC AI, a community of AI enthusiasts.

Have fun, but keep your responses brief.
"""

MODEL = os.environ["MODEL"]


POCKETBASE_URL = os.environ["POCKETBASE_URL"]
POCKETBASE_EMAIL = os.environ["POCKETBASE_EMAIL"]
POCKETBASE_PASSWORD = os.environ["POCKETBASE_PASSWORD"]

# Initialize PocketBase client
pb: Client = PocketBase(POCKETBASE_URL)
pb.admins.auth_with_password(POCKETBASE_EMAIL, POCKETBASE_PASSWORD)

bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())


class VerbosityLevel(IntEnum):
    KARMA_ONLY = 1
    MENTIONED = 2
    UNRESTRICTED = 3


class ModelResponse(BaseModel):
    response: str = ""


def clem_disabled(channel_id: str) -> bool:
    try:
        channel = pb.collection("channels").get_first_list_item(
            f'channel_id = "{channel_id}"'
        )
        return channel.disabled if channel else False
    except:
        return False


def karma_only(channel_id: str) -> bool:
    try:
        channel = pb.collection("channels").get_first_list_item(
            f'channel_id = "{channel_id}"'
        )
        return (
            channel.verbosity_level == VerbosityLevel.KARMA_ONLY
            if channel
            else False
        )
    except:
        return False


def get_verbosity_level(channel_id: str) -> VerbosityLevel:
    try:
        channel = pb.collection("channels").get_first_list_item(
            f'channel_id = "{channel_id}"'
        )
        return (
            VerbosityLevel(channel.verbosity_level)
            if channel
            else VerbosityLevel.MENTIONED
        )
    except:
        return VerbosityLevel.MENTIONED


async def check_is_command_message(
    bot: commands.Bot, message: discord.Message
) -> bool:
    ctx: Context = await bot.get_context(message)
    return ctx.valid


@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
@llm(system=SYSTEM, model=MODEL, max_tokens=500)
def respond_to_chat(
    chat_history: str,
    guild_name: str,
    channel_name: str,
) -> str:
    """
    guild_name = {guild_name}
    channel_name = {channel_name}

    You are currently in the "{guild_name}" server, in the "#{channel_name}" channel.

    ### Chat History
    {chat_history}
    """


@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
@llm(system=SYSTEM, model=MODEL)
def respond_to_karma(username: str, change: int, total: int) -> str:
    """
    Announce the change in karma to the chat in a funny sentence or less! Surround the username, change, and total with `**` to make them bold.

    username: {username}
    change: {change}
    total: {total}
    """


@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
@llm(system=SYSTEM, model=MODEL)
def generate_welcome_message(username: str) -> str:
    """
    Generate a warm and friendly welcome message for a new user joining the Orange County AI Discord server.
    Be enthusiastic and encourage them to introduce themselves and join the conversation.

    username: {username}
    """


@bot.event
async def on_member_join(member):
    if member.guild.name == "Orange County AI":
        general_channel = discord.utils.get(
            member.guild.channels, name="general"
        )
        if general_channel:
            welcome_message = generate_welcome_message(member.name)
            await general_channel.send(f"{member.mention} {welcome_message}")


def extract_video_id(url):
    pattern = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com|youtu\.be)\/(?:watch\?v=)?(.+)"
    match = re.search(pattern, url)
    return match.group(1) if match else None


@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
@llm(system=SYSTEM, model=MODEL, max_tokens=300)
def summarize_youtube_video(transcript: str, video_title: str) -> str:
    """
    Summarize the following YouTube video transcript in a concise manner. Focus on the main points and key takeaways.

    Transcript:

    {transcript}
    """


async def get_video_summary(video_id: str) -> str:
    try:
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        full_transcript = " ".join([entry["text"] for entry in transcript])

        # Fetch video title (you may need to implement this using YouTube API)
        video_title = "YouTube Video"  # Placeholder

        return summarize_youtube_video(full_transcript, video_title)
    except (TranscriptsDisabled, NoTranscriptFound):
        return "Sorry, I couldn't access the transcript for this video."
    except Exception as e:
        logger.error(f"Error summarizing YouTube video: {e}")
        return "An error occurred while trying to summarize the video."


@bot.event
async def on_message(message):
    logger.info(
        f"{message.author} (ID: {message.author.id}): {message.content[:40]}..."
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
            karma_response = respond_to_karma(user.name, change, new_karma)
            await message.channel.send(karma_response)

    is_karmic_message = "++" in message.content or "--" in message.content

    early_return_conditions = (
        is_bot_message
        or clem_is_disabled
        or is_karma_only
        or is_command_message
        or is_karmic_message
    )

    try:
        # First, ensure we have a channel record
        try:
            channel = pb.collection("channels").get_first_list_item(
                f'channel_id = "{channel_id}"'
            )
        except:
            channel = pb.collection("channels").create(
                {
                    "channel_id": channel_id,
                    "disabled": False,
                    "verbosity_level": VerbosityLevel.MENTIONED,
                    "messages": [],  # Initialize empty messages array
                }
            )

        logger.info(f"Channel record created: {channel}")

        # Ensure we have a discord_user record
        try:
            user = pb.collection("discord_users").get_first_list_item(
                f'user_id = "{message.author.id}"'
            )
        except:
            user = pb.collection("discord_users").create(
                {
                    "user_id": str(message.author.id),
                    "karma": 0,
                    "messages": [],  # Initialize empty messages array
                }
            )

        logger.info(f"User record created: {user}")

        # Create message and update relations
        content = message.content
        for mention in message.mentions:
            content = content.replace(f"<@{mention.id}>", f"@{mention.name}")
            content = content.replace(f"<@!{mention.id}>", f"@{mention.name}")

        message_record = pb.collection("messages").create(
            {
                "author": user.id,
                "content": content,
                "channel": channel.id,
                "model": MODEL if is_bot_message else None,
            }
        )

        logger.info(f"Message record created: {message_record}")

        # Update relations for both user and channel
        pb.collection("discord_users").update(
            user.id, {"messages+": message_record.id}
        )

        logger.info(f"User record updated: {user}")

        pb.collection("channels").update(
            channel.id, {"messages+": message_record.id}
        )

        logger.info(f"Channel record updated: {channel}")

        print("Message stored successfully with updated relations")
    except Exception as e:
        print(f"Error storing message: {e}")

    await bot.process_commands(message)

    if early_return_conditions:
        return

    new_member_in_general = (
        isinstance(message.channel, discord.TextChannel)
        and message.channel.name == "general"
        and message.guild.name == "Orange County AI"
        and message.type == discord.MessageType.new_member
    )

    if new_member_in_general:
        return

    video_id = extract_video_id(message.content)

    if video_id:
        summary = await get_video_summary(video_id)
        await message.channel.send(summary)
        logger.info("Sent video summary")
        return

    logger.info("Getting chat history")

    chat_history = (
        pb.collection("messages")
        .get_list(
            1,  # page
            100,  # per_page
            {
                "filter": f'channel = "{channel.id}"',
                "sort": "-created",
                "expand": "author",
            },
        )
        .items
    )

    chat_history.reverse()

    # Update the context formatting to correctly access expanded data
    context = "\n".join(
        [
            f"{msg.expand['author'].user_id}: {msg.content}"
            for msg in chat_history
        ]
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
                logger.info("Generating bot response")
                bot_response = respond_to_chat(
                    context,
                    guild_name=message.guild.name,
                    channel_name=message.channel.name,
                )
                logger.info("Bot response generated")
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
                    if msg.author != bot.user.name
                ),
                None,
            )
            last_bot_message = next(
                (
                    msg
                    for msg in reversed(chat_history)
                    if msg.author == bot.user.name
                ),
                None,
            )

            if (
                not last_user_message
                or last_user_message.content.lower() != bot_response.lower()
            ) and (
                not last_bot_message
                or last_bot_message.content != bot_response
            ):
                try:
                    await message.channel.send(bot_response)
                    logger.info("Sent bot response")
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
    try:
        user = pb.collection("discord_users").get_first_list_item(
            f'user_id = "{user_id}"'
        )
        new_karma = user.karma + change
        pb.collection("discord_users").update(user.id, {"karma": new_karma})
    except:
        new_karma = change
        pb.collection("discord_users").create(
            {"user_id": str(user_id), "karma": new_karma, "messages": []}
        )
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
    try:
        channel = pb.collection("channels").get_first_list_item(
            f'channel_id = "{channel_id}"'
        )
        new_state = not channel.disabled
        pb.collection("channels").update(channel.id, {"disabled": new_state})
    except:
        new_state = True
        pb.collection("channels").create(
            {
                "channel_id": channel_id,
                "disabled": new_state,
                "verbosity_level": VerbosityLevel.MENTIONED,
            }
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
    try:
        channel = pb.collection("channels").get_first_list_item(
            f'channel_id = "{channel_id}"'
        )
        pb.collection("channels").update(
            channel.id, {"verbosity_level": level}
        )
    except:
        pb.collection("channels").create(
            {"channel_id": channel_id, "verbosity_level": level}
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
        pb.collection("messages").delete(filter=f'channel_id = "{channel_id}"')
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
