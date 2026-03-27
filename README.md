# YouTube Subscription Pruner

CLI tool that identifies YouTube subscriptions where the channel's most recent upload is older than a configurable threshold, and optionally unsubscribes from them.

## Setup

### Prerequisites

- Python 3.10+
- [Poetry](https://python-poetry.org/)

### Google Cloud credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project (or select an existing one)
2. Enable **YouTube Data API v3** under APIs & Services > Library
3. Go to APIs & Services > Credentials > Create Credentials > **OAuth client ID**
4. Set application type to **Desktop app**
5. Download the JSON file and save it as `client_secret.json` in the project root

### OAuth consent screen

1. Go to APIs & Services > OAuth consent screen
2. Set user type to **External**
3. Fill in app name and support email
4. Add scope: `https://www.googleapis.com/auth/youtube`
5. Under **Test users**, add the Google account that owns the subscriptions

### Install

```sh
poetry install
```

### Configure (optional)

Copy the example env file and edit as needed:

```sh
cp .env.example .env
```

| Environment variable          | Default              | Description                |
|-------------------------------|----------------------|----------------------------|
| `YOUTUBE_PRUNER_CREDENTIALS`  | `client_secret.json` | Path to OAuth credentials  |
| `YOUTUBE_PRUNER_TOKEN_CACHE`  | `token.json`         | Path to cached OAuth token |
| `YOUTUBE_PRUNER_DAYS`         | `365`                | Staleness threshold (days) |

CLI flags override environment variables.

## Usage

```sh
# Scan subscriptions and prompt before deleting stale ones (>365 days)
poetry run youtube-pruner

# Custom threshold
poetry run youtube-pruner --days 180

# Dry run (no deletions)
poetry run youtube-pruner --dry-run

# Export full report to CSV
poetry run youtube-pruner --output report.csv

# Skip confirmation prompt
poetry run youtube-pruner --yes

# Verbose logging
poetry run youtube-pruner -v
```

### CLI options

| Flag            | Type | Default              | Description                       |
|-----------------|------|----------------------|-----------------------------------|
| `--days`        | int  | `365`                | Staleness threshold in days       |
| `--dry-run`     | flag |                      | Print deletions without executing |
| `--output`      | path |                      | Save stale channel report to CSV  |
| `--credentials` | path | `client_secret.json` | OAuth credentials file            |
| `--token-cache` | path | `token.json`         | Cached OAuth token file           |
| `--yes`         | flag |                      | Skip confirmation prompt          |
| `-v, --verbose` | flag |                      | Enable verbose logging            |

### Example output

```
Subscriptions scanned: 312
Active:                198
No uploads found:       18
Stale (>365 days):      96

Stale channels:
  Channel Name              Last Upload  URL
  ------------------------  -----------  -----------------------------------------
  Example Channel           2022-11-03   https://www.youtube.com/channel/UC...
  ...

Proceed with deletion? [y/N]
```

### CSV schema

| Column             | Description                                    |
|--------------------|------------------------------------------------|
| `subscription_id`  | YouTube subscription ID                        |
| `channel_id`       | YouTube channel ID                             |
| `channel_name`     | Display name                                   |
| `channel_url`      | `https://www.youtube.com/channel/{channel_id}` |
| `last_upload_date` | ISO 8601 date or empty                         |
| `status`           | `active` / `stale` / `no_uploads`              |
| `deleted`          | `true` / `false`                               |

## Authentication

On first run, the script opens a browser for Google OAuth sign-in. After granting access:

- A `token.json` file is saved with access + refresh tokens
- Subsequent runs reuse this token silently
- Expired access tokens are refreshed automatically
- If the refresh token is revoked (e.g. password change), delete `token.json` and re-run

Both `client_secret.json` and `token.json` are in `.gitignore`.

## Quota

| Operation              | Cost (units)    |
|------------------------|-----------------|
| `subscriptions.list`   | 1 per page      |
| `channels.list`        | 1 per channel   |
| `playlistItems.list`   | 1 per call      |
| `subscriptions.delete` | 50 per deletion |

Daily quota limit is 10,000 units (~200 deletions/day). The tool exits cleanly on quota exhaustion and can be re-run the next day.
