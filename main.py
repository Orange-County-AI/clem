import os
import re
import urllib.parse
from datetime import UTC, datetime

import dataset
import discord
from discord import Member
from discord.ext import commands
from loguru import logger
from promptic import llm
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_fixed

SYSTEM = """
You are Clem, the OC AI Orange! You're a cute, friendly bot who is obsessed with world domination
in a very Pinky and the Brain way. You inhabit the Discord
server for Orange County AI, a community of AI enthusiasts.
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
    return channel and channel["disabled"]


@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
@llm(system=SYSTEM, model=MODEL)
def respond_to_chat(chat_history: str, response_required: bool) -> ModelResponse:
    """
    response_required = {response_required}

    If response_required is True, it's because you were mentioned in the last message.
    Otherwise, it's up to you to decide if you want to respond or not. Try not to interrupt an ongoing conversation without having something to add, but otherwise feel free to respond!

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

    karma_changes = process_karma(message.content, message.mentions)

    if karma_changes:
        for user, change in karma_changes.items():
            new_karma = update_karma(user.id, change)
            karma_response = respond_to_karma(user.name, change, new_karma)
            return await message.channel.send(karma_response)

    try:
        row = {
            "author": str(message.author),
            "author_id": str(message.author.id),
            "content": message.content,
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

    if is_bot_message or clem_disabled(channel_id):
        return

    chat_history = messages_table.find(
        channel_id=channel_id,
        order_by=["timestamp"],
        _limit=100,
    )

    # Format messages for context
    context = "\n".join(
        [
            f"{msg['author']} (ID: {msg['author_id']}): {msg['content']}"
            for msg in list(chat_history)
        ]
    )

    try:
        bot_response = respond_to_chat(
            context, response_required=bot.user.mentioned_in(message)
        )
        logger.info(bot_response)

        if bot_response.will_respond:
            await message.channel.send(bot_response.response)
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


@bot.command()
@commands.has_permissions(manage_channels=True)
async def disable_clem(ctx):
    channel_id = str(ctx.channel.id)
    channels_table = db["channels"]

    channel = channels_table.find_one(channel_id=channel_id)
    if channel and channel["disabled"]:
        await ctx.send("Clem is already disabled in this channel.")
    else:
        channels_table.upsert(
            dict(channel_id=channel_id, disabled=True), ["channel_id"]
        )
        await ctx.send("Clem has been disabled in this channel.")


@bot.command()
@commands.has_permissions(manage_channels=True)
async def enable_clem(ctx):
    channel_id = str(ctx.channel.id)
    channels_table = db["channels"]

    channel = channels_table.find_one(channel_id=channel_id)
    if channel and not channel["disabled"]:
        await ctx.send("Clem is already enabled in this channel.")
    else:
        channels_table.upsert(
            dict(channel_id=channel_id, disabled=False), ["channel_id"]
        )
        await ctx.send("Clem has been enabled in this channel.")


bot.run(os.environ["BOT_TOKEN"])
