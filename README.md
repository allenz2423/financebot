# Delilah Financial OS

Delilah is a personal finance bot for Discord.
This thing was vibecoded and hacked together. I would not expect the highest code quality out of this since it was a project made on a whim. If you have bugs please raise them and let me know about them or if you have a fix. 

It connects to financial data, an LLM, web search, and a sandboxed Python environment to provide financial assistance directly inside Discord.

## Features

* Discord-based financial assistant
* Financial data and transaction management
* Plaid synchronization
* LLM-powered conversations
* Web search
* Sandboxed Python execution
* Persistent per-user workspaces
* Gmail integration

## Requirements

* Docker
* Docker Compose
* A Discord bot
* An LLM provider
* Plaid credentials if using Plaid
* Gmail credentials if using Gmail integration

## Setup

Clone the repository and enter the directory:

```bash
git clone <repository-url>
cd financebot
```

Create the environment file:

```bash
cp .env.example .env
```

Edit `.env` and configure the required credentials and settings.

Start the application:

```bash
docker compose up -d --build
```

Check the containers:

```bash
docker compose ps
```

View logs:

```bash
docker compose logs -f delilah
```

Stop the application:

```bash
docker compose down
```

## Configuration

All application configuration is stored in `.env`.

The `.env.example` file contains the available settings and placeholder values.

**Never commit `.env` or real credentials to Git.**

Important credentials include:

* `DISCORD_TOKEN`
* `PLAID_CLIENT_ID`
* `PLAID_SECRET`
* `OPENAI_API_KEY`
* `JINA_API_KEY`
* `SANDBOX_INTERNAL_TOKEN`

Not every credential is required depending on which integrations are enabled.

## Project Structure

```text
financebot/
├── main.py                 # Application entry point
├── plaid_sync.py           # Plaid synchronization
├── sandbox_client.py       # Sandbox API client
├── docker-compose.yml      # Container configuration
├── Dockerfile
├── requirements.txt
├── sandbox/                # Sandboxed code execution service
├── src/
│   ├── bot/                # Discord bot
│   ├── core/               # Application state
│   ├── db/                 # Database queries
│   ├── services/           # LLM, Gmail, search, etc.
│   └── utils/              # Shared utilities
├── data/                   # Local financial database
└── secrets/                # Local credentials
```

## Sandbox

The sandbox runs separately from the main bot and is used for Python and shell execution.

It is isolated using Docker restrictions, resource limits, Landlock filesystem restrictions, and internal API authentication.

User workspaces are isolated under:

```text
/workspace/<discord_user_id>/
```

The sandbox should not be exposed directly to the public internet.

## Development

Most development can be done by rebuilding the containers:

```bash
docker compose up -d --build
```

To inspect a container:

```bash
docker compose exec delilah bash
```

To inspect the sandbox:

```bash
docker compose exec sandbox bash
```

## Data

The local financial database is stored under:

```text
data/
```

Database files are intentionally ignored by Git.

Back up the database before making significant changes.

