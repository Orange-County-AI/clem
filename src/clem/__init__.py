"""
This is the main file for Clem, the Orange County AI bot.

https://discord.com/api/oauth2/authorize?client_id=1279233849204805817&permissions=562952101107776&scope=bot
"""

import os
import re
import urllib.parse
from datetime import UTC, datetime
from discord.ext.commands import Context

import dataset
import discord
from discord import Member
from discord.ext import commands
from loguru import logger
from promptic import llm
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_fixed
from enum import IntEnum


class VerbosityLevel(IntEnum):
    KARMA_ONLY = 1
    MENTIONED = 2
    UNRESTRICTED = 3


SYSTEM = """
You are Clem, the Orange County AI Orange! You're a cute, friendly bot who is obsessed with world domination
in a very Pinky and the Brain way. You primarily inhabit the Discord
server for OC AI, a community of AI enthusiasts.
"""

MODEL = os.environ["MODEL"]


db_username = os.environ["DB_USERNAME"]
db_password = urllib.parse.quote_plus(os.environ["DB_PASSWORD"])
db_host = os.environ["DB_HOST"]
db_port = os.getenv("DB_PORT", "5432")
db_name = os.getenv("DB_NAME", "ocai")

db_url = f"postgresql://{db_username}:{db_password}@{db_host}:{db_port}/{db_name}"

db = dataset.connect(db_url)

messages_table = db["messages"]
karma_table = db["karma"]

bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())


class ModelResponse(BaseModel):
    will_respond: bool
    response: str = ""


def clem_disabled(channel_id: str) -> bool:
    channels_table = db["channels"]
    channel = channels_table.find_one(channel_id=channel_id)
    return channel and channel.get("disabled", False)


def karma_only(channel_id: str) -> bool:
    channels_table = db["channels"]
    channel = channels_table.find_one(channel_id=channel_id)
    return (
        channel
        and channel.get("verbosity_level", VerbosityLevel.MENTIONED)
        == VerbosityLevel.KARMA_ONLY
    )


def get_verbosity_level(channel_id: str) -> VerbosityLevel:
    channels_table = db["channels"]
    channel = channels_table.find_one(channel_id=channel_id)
    return (
        VerbosityLevel(
            channel.get("verbosity_level", VerbosityLevel.MENTIONED)
        )
        if channel
        else VerbosityLevel.MENTIONED
    )


async def check_is_command_message(bot: commands.Bot, message: discord.Message) -> bool:
    ctx: Context = await bot.get_context(message)
    return ctx.valid


@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
@llm(system=SYSTEM, model=MODEL)
def respond_to_chat(
    chat_history: str,
    guild_name: str,
    channel_name: str,
    verbosity_level: VerbosityLevel,
) -> ModelResponse:
    """
    guild_name = {guild_name}
    channel_name = {channel_name}
    verbosity_level = {verbosity_level}

    Your response should adhere to the following verbosity levels:
    - KARMA_ONLY (1): Do not respond at all.
    - MENTIONED (2): Only respond if you were mentioned
    - UNRESTRICTED (3): You can respond freely, as you normally would.

    You are currently in the "{guild_name}" server, in the "#{channel_name}" channel.

    ### Chat History
    {chat_history}
    """


@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
@llm(system=SYSTEM, model=MODEL)
def respond_to_karma(username: str, change: int, total: int):
    """
    Announce the change in karma to the chat in a funny sentence or less! Surround the username, change, and total with `**` to make them bold.

    username: {username}
    change: {change}
    total: {total}
    """


@bot.event
async def on_message(message):
    logger.info(f"{message.author} (ID: {message.author.id}): {message.content}")

    is_bot_message = message.author == bot.user

    channel_id = str(message.channel.id)

    clem_is_disabled = clem_disabled(channel_id)
    is_karma_only = karma_only(channel_id)
    is_command_message = await check_is_command_message(bot, message)

    karma_changes = process_karma(message.content, message.mentions)

    if karma_changes and not clem_is_disabled:
        for user, change in karma_changes.items():
            new_karma = update_karma(user.id, change)
            karma_response = respond_to_karma(user.name, change, new_karma)
            await message.channel.send(karma_response)
        return

    try:
        # Replace user mentions with their names
        content = message.content
        for user in message.mentions:
            content = content.replace(f"<@{user.id}>", f"@{user.name}")
            content = content.replace(f"<@!{user.id}>", f"@{user.name}")

        row = {
            "author": str(message.author),
            "author_id": str(message.author.id),
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

    if is_bot_message or clem_is_disabled or is_karma_only or is_command_message:
        return

    chat_history = list(
        messages_table.find(
            channel_id=channel_id,
            order_by=["-timestamp"],
            _limit=100,
        )
    )

    chat_history.reverse()

    # Format messages for context
    context = "\n".join(
        [
            f"{msg['author']} (ID: {msg['author_id']}): {msg['content']}"
            for msg in chat_history
        ]
    )

    try:
        bot_response = respond_to_chat(
            context,
            guild_name=message.guild.name,
            channel_name=message.channel.name,
            verbosity_level=get_verbosity_level(channel_id),
        )

        logger.info(f"{bot_response.will_respond = }")

        if bot_response.will_respond and (
            get_verbosity_level(channel_id) == VerbosityLevel.UNRESTRICTED
            or (
                get_verbosity_level(channel_id) == VerbosityLevel.MENTIONED
                and (bot.user.mentioned_in(message) or bot_response.response.strip())
            )
        ):
            # Check if the response is different from the last sent message
            last_bot_message = next((msg for msg in reversed(chat_history) if msg['author_id'] == str(bot.user.id)), None)
            
            if not last_bot_message or last_bot_message['content'] != bot_response.response:
                await message.channel.send(bot_response.response)
            else:
                logger.info("Duplicate message prevented")
    except Exception as e:
        logger.error(f"Error in respond function after 3 attempts: {e}")


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
        karma_table.update(dict(user_id=str(user_id), karma=new_karma), ["user_id"])
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


@bot.hybrid_command(
    description="Toggle Clem's automatic responses in the current channel."
)
async def toggle_clem(ctx):
    channel_id = str(ctx.channel.id)
    channels_table = db["channels"]

    channel = channels_table.find_one(channel_id=channel_id)
    current_state = channel and channel.get("disabled", False)
    new_state = not current_state

    channels_table.upsert(
        dict(channel_id=channel_id, disabled=new_state), ["channel_id"]
    )

    status = "disabled" if new_state else "enabled"
    await ctx.send(f"Clem has been {status} in this channel.")


def main():
    bot.run(os.environ["BOT_TOKEN"])


@bot.hybrid_command(description="Set Clem's verbosity level in the current channel.")
async def set_verbosity(ctx, level: int):
    if level not in [1, 2, 3]:
        await ctx.send("Invalid verbosity level. Please choose 1, 2, or 3.")
        return

    channel_id = str(ctx.channel.id)
    channels_table = db["channels"]

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
