"""
This is the main file for Clem, the Orange County AI bot.

https://discord.com/api/oauth2/authorize?client_id=1279233849204805817&permissions=562952101107776&scope=bot
"""

import logging
import os
import re
from datetime import UTC, datetime
from enum import IntEnum
import hashlib

import discord
import httpx
import meilisearch
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
MEILISEARCH_URL = os.getenv(
    "MEILISEARCH_URL", "https://meilisearch.knowsuchagency.com"
)
MEILISEARCH_API_KEY = os.getenv("MEILISEARCH_API_KEY")

# Initialize MeiliSearch client
logger.info(f"Initializing MeiliSearch client with URL: {MEILISEARCH_URL}")
client = meilisearch.Client(MEILISEARCH_URL, MEILISEARCH_API_KEY)

# Test connection
try:
    health = client.health()
    logger.info(f"MeiliSearch health check: {health}")
except Exception as e:
    logger.error(f"MeiliSearch health check failed: {e}")

# Create indices
logger.info("Creating MeiliSearch indices...")
messages_index = client.index("ocai_messages")
karma_index = client.index("ocai_karma")
channels_index = client.index("ocai_channels")

# Log index information
try:
    messages_stats = messages_index.get_stats()
    logger.info(f"Messages index stats: {messages_stats}")
except Exception as e:
    logger.error(f"Failed to get messages index stats: {e}")

try:
    karma_stats = karma_index.get_stats()
    logger.info(f"Karma index stats: {karma_stats}")
except Exception as e:
    logger.error(f"Failed to get karma index stats: {e}")

try:
    channels_stats = channels_index.get_stats()
    logger.info(f"Channels index stats: {channels_stats}")
except Exception as e:
    logger.error(f"Failed to get channels index stats: {e}")


# Configure indices with better error handling
def configure_index_safely(index, index_name, primary_key, config_func):
    """Safely configure a MeiliSearch index with proper error handling"""
    try:
        # First check if index exists and get primary key
        try:
            current_pk = index.get_primary_key()
            if current_pk is None:
                logger.info(
                    f"Setting primary key for {index_name} index to '{primary_key}'"
                )
                # Use the HTTP client to update primary key via PATCH request
                response = client.http.patch(
                    f"/indexes/{index.uid}", {"primaryKey": primary_key}
                )
                logger.info(f"Primary key update response: {response}")
            else:
                logger.info(
                    f"{index_name} index already has primary key: {current_pk}"
                )
        except Exception as pk_error:
            logger.warning(
                f"Primary key check/set for {index_name} failed: {pk_error}"
            )
            # Try to continue with configuration

        # Apply additional configuration
        config_func(index)

        logger.info(f"Successfully configured {index_name} index")
        return True
    except Exception as e:
        logger.error(f"Failed to configure {index_name} index: {e}")
        # For primary key errors, this is critical - we should fail fast
        if "primary" in str(e).lower():
            logger.error(
                f"Primary key configuration failed for {index_name} - this will cause document insertion failures"
            )
        return False


def configure_messages_index(index):
    """Configure messages index attributes"""
    try:
        # Update settings using the settings endpoint
        settings = {
            "searchableAttributes": ["content", "author"],
            "filterableAttributes": ["channel_id", "author", "timestamp"],
            "sortableAttributes": ["timestamp"],
        }
        index.update_settings(settings)
        logger.info("Messages index settings updated successfully")
    except Exception as e:
        logger.error(f"Failed to update messages index settings: {e}")
        raise


def configure_karma_index(index):
    """Configure karma index attributes"""
    try:
        settings = {
            "filterableAttributes": ["user_id"],
            "sortableAttributes": ["karma"],
        }
        index.update_settings(settings)
        logger.info("Karma index settings updated successfully")
    except Exception as e:
        logger.error(f"Failed to update karma index settings: {e}")
        raise


def configure_channels_index(index):
    """Configure channels index attributes"""
    try:
        settings = {
            "filterableAttributes": [
                "channel_id",
                "disabled",
                "verbosity_level",
            ]
        }
        index.update_settings(settings)
        logger.info("Channels index settings updated successfully")
    except Exception as e:
        logger.error(f"Failed to update channels index settings: {e}")
        raise


# Configure all indices with retry logic
import time


def configure_all_indices_with_retry(max_retries=3):
    """Configure all indices with retry logic"""
    for attempt in range(max_retries):
        logger.info(
            f"Configuring indices (attempt {attempt + 1}/{max_retries})"
        )

        messages_configured = configure_index_safely(
            messages_index, "messages", "id", configure_messages_index
        )
        karma_configured = configure_index_safely(
            karma_index, "karma", "id", configure_karma_index
        )
        channels_configured = configure_index_safely(
            channels_index, "channels", "id", configure_channels_index
        )

        if all([messages_configured, karma_configured, channels_configured]):
            logger.info("All indices configured successfully")
            return True

        if attempt < max_retries - 1:
            logger.warning(
                f"Some indices failed to configure, retrying in 2 seconds..."
            )
            time.sleep(2)

    logger.error("Failed to configure all indices after all retry attempts")
    return False


# Configure indices
indices_configured = configure_all_indices_with_retry()
if not indices_configured:
    logger.error(
        "Critical: Indices configuration failed - message storage may not work properly"
    )


# Helper functions for MeiliSearch operations
def generate_message_id(author: str, timestamp: datetime, content: str) -> str:
    """Generate a unique ID for a message"""
    content_hash = hashlib.md5(
        f"{author}{timestamp.isoformat()}{content}".encode()
    ).hexdigest()[:8]
    return f"{int(timestamp.timestamp())}_{content_hash}"


def wait_for_task_completion(task_uid: int, timeout_ms: int = 5000) -> dict:
    """Wait for a MeiliSearch task to complete using the client's wait_for_task method"""
    try:
        logger.info(f"Waiting for task {task_uid} to complete...")
        # Use MeiliSearch client's built-in wait_for_task method
        task = client.wait_for_task(task_uid, timeout_in_ms=timeout_ms)
        # Convert to dict if it's an object
        task_dict = (
            task
            if isinstance(task, dict)
            else task.__dict__
            if hasattr(task, "__dict__")
            else {}
        )
        logger.info(
            f"Task {task_uid} completed with status: {task_dict.get('status', 'unknown')}"
        )
        return task_dict
    except Exception as e:
        logger.error(f"Error waiting for task {task_uid}: {e}")
        return {}


def check_task_status(task_uid: int) -> dict:
    """Check the status of a MeiliSearch task"""
    try:
        task = client.get_task(task_uid)
        # Convert to dict if it's an object
        task_dict = (
            task
            if isinstance(task, dict)
            else task.__dict__
            if hasattr(task, "__dict__")
            else {}
        )
        logger.info(f"Task {task_uid} status: {task_dict}")
        return task_dict
    except Exception as e:
        logger.error(f"Error checking task {task_uid}: {e}")
        return {}


def store_message(
    author: str, content: str, channel_id: str, model: str = None
):
    """Store a message in MeiliSearch"""
    timestamp = datetime.now(UTC)
    doc = {
        "id": generate_message_id(author, timestamp, content),
        "author": author,
        "content": content,
        "timestamp": int(timestamp.timestamp()),
        "channel_id": channel_id,
    }
    if model:
        doc["model"] = model

    logger.info(
        f"Attempting to store message: ID={doc['id']}, Author={author}, Channel={channel_id}"
    )
    logger.debug(f"Document to store: {doc}")

    try:
        # Check if the index has a primary key set - if not, try to set it
        try:
            index_info = messages_index.get_primary_key()
            if index_info is None:
                logger.warning(
                    "Messages index has no primary key set, attempting to set it"
                )
                # Use HTTP client to update primary key
                response = client.http.patch(
                    f"/indexes/{messages_index.uid}", {"primaryKey": "id"}
                )
                logger.info(
                    f"Successfully set primary key for messages index: {response}"
                )
        except Exception as pk_error:
            logger.error(
                f"Could not verify/set primary key for messages index: {pk_error}"
            )
            # Continue anyway - the add_documents call will give us the specific error

        # MeiliSearch add_documents returns a task object with information about the operation
        task = messages_index.add_documents([doc])
        logger.info(f"MeiliSearch add_documents returned task: {task}")

        # Convert task to dict if it's not already
        task_dict = (
            task
            if isinstance(task, dict)
            else task.__dict__
            if hasattr(task, "__dict__")
            else {}
        )

        # Log task details - MeiliSearch Python client uses snake_case
        task_uid = None
        if "task_uid" in task_dict:
            task_uid = task_dict["task_uid"]
            logger.info(f"Task UID: {task_uid}")
        elif "taskUid" in task_dict:
            task_uid = task_dict["taskUid"]
            logger.info(f"Task UID: {task_uid}")
        else:
            logger.warning(f"No task_uid found in response: {task_dict}")

        if "status" in task_dict:
            logger.info(f"Initial task status: {task_dict['status']}")
        if "error" in task_dict:
            logger.error(f"Task error: {task_dict['error']}")
            return False

        # Wait for task completion if we have a task_uid
        if task_uid:
            # Use the built-in wait_for_task method
            final_task_status = wait_for_task_completion(
                task_uid, timeout_ms=5000
            )

            status = final_task_status.get("status", "unknown")
            if status == "succeeded":
                logger.info(
                    f"Message storage task {task_uid} completed successfully"
                )
                return True
            elif status == "failed":
                error_info = final_task_status.get("error", "No error details")
                logger.error(
                    f"Message storage task {task_uid} failed: {error_info}"
                )

                # Check if this is a primary key error and try to recover
                if (
                    isinstance(error_info, dict)
                    and "primary_key" in str(error_info).lower()
                ):
                    logger.error(
                        "Primary key error detected - the index may need to be reset"
                    )
                    logger.error(
                        "Consider using the !reset_indices command to fix this issue"
                    )

                return False
            else:
                logger.warning(
                    f"Message storage task {task_uid} finished with unexpected status: {status}"
                )
                return False

        return True
    except Exception as e:
        logger.error(f"Error storing message: {e}")
        logger.exception("Full exception details:")
        return False


def get_channel_messages(channel_id: str, limit: int = 100):
    """Get recent messages for a channel"""
    try:
        logger.debug(
            f"Searching for messages in channel {channel_id} with limit {limit}"
        )
        result = messages_index.search(
            "",
            {
                "filter": f"channel_id = '{channel_id}'",
                "sort": ["timestamp:desc"],
                "limit": limit,
            },
        )
        logger.info(
            f"Search returned {len(result['hits'])} messages for channel {channel_id}"
        )
        logger.debug(f"Search result: {result}")

        # Convert timestamp back to datetime and reverse order (oldest first)
        messages = []
        for hit in reversed(result["hits"]):
            hit["timestamp"] = datetime.fromtimestamp(hit["timestamp"], UTC)
            messages.append(hit)
        return messages
    except Exception as e:
        logger.error(
            f"Error retrieving messages for channel {channel_id}: {e}"
        )
        logger.exception("Full exception details for message retrieval:")
        return []


def get_user_karma(user_id: str) -> int:
    """Get user's karma"""
    try:
        result = karma_index.search(
            "",
            {
                "filter": f"user_id = '{user_id}'",
                "limit": 1,
            },
        )
        if result["hits"]:
            return result["hits"][0]["karma"]
        return 0
    except Exception as e:
        logger.error(f"Error retrieving karma: {e}")
        return 0


def update_user_karma(user_id: str, change: int) -> int:
    """Update user's karma and return new total"""
    current_karma = get_user_karma(user_id)
    new_karma = current_karma + change

    doc = {
        "id": user_id,
        "user_id": user_id,
        "karma": new_karma,
    }

    try:
        karma_index.add_documents([doc])
        return new_karma
    except Exception as e:
        logger.error(f"Error updating karma: {e}")
        return current_karma


def get_channel_config(channel_id: str):
    """Get channel configuration"""
    try:
        result = channels_index.search(
            "",
            {
                "filter": f"channel_id = '{channel_id}'",
                "limit": 1,
            },
        )
        if result["hits"]:
            return result["hits"][0]
        return None
    except Exception as e:
        logger.error(f"Error retrieving channel config: {e}")
        return None


def update_channel_config(
    channel_id: str, disabled: bool = None, verbosity_level: int = None
):
    """Update channel configuration"""
    current_config = get_channel_config(channel_id) or {}

    doc = {
        "id": channel_id,
        "channel_id": channel_id,
        "disabled": (
            disabled
            if disabled is not None
            else current_config.get("disabled", False)
        ),
        "verbosity_level": (
            verbosity_level
            if verbosity_level is not None
            else current_config.get("verbosity_level", 2)
        ),
    }

    try:
        channels_index.add_documents([doc])
        return True
    except Exception as e:
        logger.error(f"Error updating channel config: {e}")
        return False


def delete_channel_messages(channel_id: str):
    """Delete all messages for a channel"""
    try:
        # Get all message IDs for the channel
        result = messages_index.search(
            "",
            {
                "filter": f"channel_id = '{channel_id}'",
                "limit": 10000,  # Adjust if needed
                "attributesToRetrieve": ["id"],
            },
        )

        message_ids = [hit["id"] for hit in result["hits"]]
        if message_ids:
            messages_index.delete_documents(message_ids)
        return True
    except Exception as e:
        logger.error(f"Error deleting channel messages: {e}")
        return False


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
    channel = get_channel_config(channel_id)
    return channel and channel.get("disabled", False)


def karma_only(channel_id: str) -> bool:
    channel = get_channel_config(channel_id)
    return (
        channel
        and channel.get("verbosity_level", VerbosityLevel.MENTIONED)
        == VerbosityLevel.KARMA_ONLY
    )


def get_verbosity_level(channel_id: str) -> VerbosityLevel:
    channel = get_channel_config(channel_id)
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

        model = MODEL if is_bot_message else None
        logger.debug(
            f"About to store message - Author: {message.author.name}, Content length: {len(content)}, Channel: {channel_id}, Model: {model}"
        )

        success = store_message(
            message.author.name, content, channel_id, model
        )
        if success:
            logger.info("Message stored successfully in MeiliSearch")
        else:
            logger.error("Failed to store message in MeiliSearch")
    except Exception as e:
        logger.error(f"Exception while storing message: {e}")
        logger.exception("Full exception details for message storage:")

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

    chat_history = get_channel_messages(channel_id, 100)

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
    return update_user_karma(str(user_id), change)


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

    channel = get_channel_config(channel_id)
    current_state = channel and channel.get("disabled", False)
    new_state = not current_state

    update_channel_config(channel_id, disabled=new_state)

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

    update_channel_config(channel_id, verbosity_level=level)

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
        success = delete_channel_messages(channel_id)
        if success:
            await ctx.send("Chat history for this channel has been reset.")
            logger.info(f"Chat history reset for channel {channel_id}")
        else:
            await ctx.send(
                "An error occurred while resetting the chat history."
            )
    except Exception as e:
        await ctx.send("An error occurred while resetting the chat history.")
        logger.error(
            f"Error resetting chat history for channel {channel_id}: {e}"
        )


@bot.hybrid_command(description="Show message count for this channel.")
@is_clementine_council()
async def message_count(ctx):
    """Show how many messages are stored for this channel"""
    channel_id = str(ctx.channel.id)

    try:
        messages = get_channel_messages(
            channel_id, 1000
        )  # Get more messages to count
        await ctx.send(f"📊 This channel has {len(messages)} stored messages")

        if messages:
            latest = messages[-1]  # Last message (most recent)
            await ctx.send(
                f"🕐 Latest stored message: {latest['author']} at {latest['timestamp']}"
            )

    except Exception as e:
        await ctx.send(f"❌ Error getting message count: {e}")
        logger.error(f"Message count error: {e}")


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
