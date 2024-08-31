import os
import urllib.parse
from datetime import UTC, datetime

import dataset
import discord
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

# Get the messages table
messages_table = db["messages"]

bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())


class ModelResponse(BaseModel):
    will_respond: bool
    response: str = ""


@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
@llm(
    system=SYSTEM,
    model=MODEL,
)
def respond(chat_history: str, response_required: bool) -> ModelResponse:
    """
    response_required = {response_required}

    If response_required is True, it's because you were mentioned in the last message.
    Otherwise, it's up to you to decide if you want to respond or not. Try not to interrupt an ongoing conversation without having something to add, but otherwise feel free to respond!

    ### Chat History
    {chat_history}
    """


@bot.event
async def on_message(message):
    logger.info(f"{message.author} (ID: {message.author.id}): {message.content}")

    is_bot_message = message.author == bot.user

    try:
        row = {
            "author": str(message.author),
            "author_id": str(message.author.id),
            "content": message.content,
            "timestamp": datetime.now(UTC),
            "channel_id": str(message.channel.id),
        }
        if is_bot_message:
            row["model"] = MODEL
        messages_table.insert(row)
        print("Message stored successfully")
    except Exception as e:
        print(f"Error storing message: {e}")

    if is_bot_message:
        return

    chat_history = messages_table.find(
        channel_id=str(message.channel.id),
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
        bot_response = respond(
            context, response_required=bot.user.mentioned_in(message)
        )
        logger.info(bot_response)

        if bot_response.will_respond:
            await message.channel.send(bot_response.response)
    except Exception as e:
        logger.error(f"Error in respond function after 3 attempts: {e}")

    await bot.process_commands(message)


bot.run(os.getenv("BOT_TOKEN"))
