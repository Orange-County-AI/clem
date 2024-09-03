# Clem - The OC AI Orange Discord Bot

Clem is a friendly AI assistant for the Orange County AI Discord server, with a quirky obsession for world domination.

[Invite Clem to your server!](https://discord.com/api/oauth2/authorize?client_id=1279233849204805817&permissions=562952101107776&scope=bot)

<a href="https://discord.com/api/oauth2/authorize?client_id=1279233849204805817&permissions=562952101107776&scope=bot">
  <img src="https://github.com/user-attachments/assets/f6c6bd5d-0ae7-4541-bd6d-78bd11a5248a" alt="HNMNG120WKDEJ6S Nov 2017" width="400px" style="border-radius: 10px;">
</a>

## Features

- Responds to chat messages using AI
- Manages karma points for users
- Toggleable automatic responses per channel
- Karma-only mode option

## Commands

- `/toggle_clem`: Toggle Clem's automatic responses in the current channel
- `/set_verbosity`: Set the verbosity level in the current channel

## Karma System

Users can give or take karma points by mentioning another user followed by `+` or `-` signs.

Example: `@user ++` or `@user --`

## Setup

1. Set up environment variables:

   - `BOT_TOKEN`: Discord bot token
   - `MODEL`: AI model to use
   - `DB_USERNAME`: Database username
   - `DB_PASSWORD`: Database password
   - `DB_HOST`: Database host
   - `DB_PORT`: Database port (default: 5432)
   - `DB_NAME`: Database name (default: ocai)

2. Install dependencies:

   ```
   uv sync
   ```

3. Run the bot:
   ```
   uv run clem
   ```

## License

This project is licensed under the Apache License, Version 2.0. See the [LICENSE](LICENSE) file for details.
