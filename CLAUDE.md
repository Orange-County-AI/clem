# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Clem is an AI-powered Discord bot for the Orange County AI Discord server. The bot has a distinct personality - it's an orange character with nerdy glasses and a green leaf, who is "adorable, mischievous, slightly unhinged" and obsessed with world domination (Pinky and the Brain style).

## Development Commands

```bash
# Install dependencies
uv sync

# Run locally
uv run clem
# or
just clem

# Run with Docker
docker-compose up

# Format code (79-char line limit)
uv run ruff format .

# Check code style
uv run ruff check .
```

## Architecture

The bot uses an Agent-based architecture with specialized agents:
- **Chat Agent**: General conversation with Clem's personality
- **Karma Agent**: Handles karma point announcements
- **Welcome Agent**: Generates welcome messages for new members
- **Summary Agent**: Summarizes YouTube videos and web pages

Each agent is configured in `clem.py` with specific instructions and model settings. The Agent framework comes from the `openai-agents` package.

## Database

PostgreSQL database with three tables:
- **messages**: Chat history (author, content, timestamp, channel_id, model)
- **karma**: User karma points (user_id, karma)
- **channels**: Channel configuration (channel_id, disabled, verbosity_level)

Database operations use the `dataset` library for SQL abstraction.

## Key Implementation Details

1. **Discord Commands**: All slash commands are defined in `clem.py` using discord.py's `@bot.tree.command()` decorator
2. **Karma System**: Look for `++` or `--` after user mentions in messages
3. **Verbosity Levels**: 
   - 1 = Karma only
   - 2 = Mentions only (default)
   - 3 = Unrestricted
4. **External APIs**: YouTube transcripts and web summaries use Windmill API endpoints
5. **Error Handling**: Uses `tenacity` for retries and Sentry for error tracking
6. **Model Configuration**: Default model is "gpt-4.1-mini", configurable via MODEL env var

## Environment Variables

Required:
- `BOT_TOKEN`: Discord bot token
- `DATABASE_URL`: PostgreSQL connection string
- `TRANSCRIPT_API_TOKEN`: For YouTube transcript API
- `WEB_SUMMARY_API_TOKEN`: For web summary API

Optional:
- `MODEL`: LLM model to use
- `SENTRY_DSN`: Error tracking
- API keys: `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`