# DiskWala Bot

Telegram bot project implementing the agreed DiskWala-only download flow.

## Features in this project

- Public private-chat bot; accepts only `https://www.diskwala.com/app/<ID>` or `https://diskwala.com/app/<ID>` links.
- English + Hindi welcome, help, maintenance and error messages.
- Sends videos back to the requesting user as Telegram videos (not documents), without changing/re-encoding the source bytes.
- Posts the same video once to the configured destination group, with no caption. A group-posting failure is logged on the server and does not undo the user's delivery.
- 24-hour local cache; repeated links reuse the cached Telegram `file_id`/file rather than resolving and downloading again.
- Percentage-only download progress; no speed or progress bar. The temporary progress message is deleted after delivery.
- No automatic retry on a failed resolver/download/upload or group post.
- No daily download quota. Different links can be processed concurrently; actual concurrency is limited by the VPS/server's RAM, CPU, disk and network.
- Owner-only `/stats`, `/broadcast`, `/setgroup`, `/maintenance on`, and `/maintenance off` commands. Owner commands work only in the owner's private chat.
- SQLite persistence, server-only rotating logs, cache cleanup, and automatic restart using systemd/Docker restart policy.
- Optional GitHub Actions controller for a 5-hour-50-minute online / 20-minute offline cycle using external host webhooks.


## Lightweight Android/Termux test mode

The default `requirements.txt` intentionally contains only the small Telegram, HTTP, and dotenv dependencies. This lets you test `/start`, `/help`, owner commands, SQLite, and Telegram connectivity on a phone without downloading Chromium. In this mode, a DiskWala link will return a clear message that the browser downloader is not installed.

For the actual browser-based DiskWala resolver, install `requirements-browser.txt` and the Playwright Chromium runtime on a compatible Linux host. Android/Termux proot is not a reliable production Chromium host.

## Important limits and verification notes

### Telegram upload size

Telegram's hosted Bot API currently permits `sendVideo` uploads up to 50 MB. For the requested uploads up to 2,000 MB, use a self-hosted **Local Bot API server** and set `LOCAL_BOT_API_URL`. Telegram documents 2,000 MB uploads for a local Bot API server. If you run without it, this bot limits files to 50 MB and reports a clear error.

The included `docker-compose.yml` starts the `aiogram/telegram-bot-api` Docker image with local mode. That image is an unofficial packaging of Telegram's Bot API server. Obtain `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from `my.telegram.org` and place them in `.env`. The bot connects to that container at `http://telegram-bot-api:8081`. Telegram requires calling `logOut` on the hosted Bot API first when migrating an already-running bot to a local Bot API server; see the setup steps below.

### DiskWala link resolver

DiskWala's resolver is not a documented public API. This project opens the official share page in Playwright/Chromium and watches for the page's `temp_info` response. A third-party public repository describes that endpoint and a page-JavaScript approach, but it is not an official guarantee. DiskWala may change the web app at any time. **The resolver has not been live-tested against your own share link in this environment**, so test a link you own or have permission to download before public launch. It will fail with a reason instead of silently retrying.

The bot does not attempt to bypass account permissions, private links, expired links, or access controls. Only download content you have permission to access and save.

### Original quality and metadata

The bot stores and sends the downloaded source bytes without compression or transcoding. Telegram may generate its own preview/streaming metadata when it processes the upload; this project does not alter the source video file. If Telegram classifies a source container as a document instead of a playable video, the bot removes that message and reports the format limitation rather than leaving a document in the chat.

### Concurrency

There is no artificial daily quota or fixed download queue. The update worker setting is finite (`UPDATE_WORKERS=32`) to protect the host and Chromium from unbounded memory use. A VPS may need a lower number, especially because each active resolver uses a browser context.

## Setup A: Docker Compose (recommended for 2 GB videos)

1. Create a bot with [@BotFather](https://t.me/BotFather). Use display name **DiskWala Bot** and request username `DiskWalaDownloadBot` if available. Set the profile image from `assets/diskwala_bot_logo.png`.
2. Get your numeric Telegram user ID from a trusted ID bot.
3. Add your bot to the destination Telegram group and make it an admin with permission to post videos.
4. Copy `.env.example` to `.env` and fill `BOT_TOKEN`, `OWNER_ID`, `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH`.
5. If this bot token has already been used with Telegram’s hosted Bot API, call `logOut` once against the hosted API before switching to the local server (for example, `https://api.telegram.org/bot<TOKEN>/logOut`). Do not post the token anywhere. A fresh bot token that has not been logged in to the hosted API does not need this migration step.
6. Start the stack:

   ```bash
   docker compose up -d --build
   ```

7. Open the bot in Telegram and send `/start`.
8. In the owner's private chat, set/verify the destination group:

   ```text
   /setgroup -1001234567890
   ```

9. Test with a DiskWala link you own or are authorized to download. Inspect logs with:

   ```bash
   docker compose logs -f diskwala-bot
   ```

The database and 24-hour cache are stored in `./data`. Do not delete this folder while the bot is running.

## Setup B: Python + systemd

Use a Linux VPS with enough RAM and disk space. Chromium needs its system libraries.

```bash
sudo apt update
sudo apt install -y python3 python3-venv
sudo mkdir -p /opt/diskwala-bot
sudo chown "$USER":"$USER" /opt/diskwala-bot
# Copy this project into /opt/diskwala-bot
cd /opt/diskwala-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-browser.txt
playwright install --with-deps chromium
cp .env.example .env
nano .env
```

For 2 GB uploads, separately run a Local Bot API server and set `LOCAL_BOT_API_URL` to its reachable URL. Then install the service:

```bash
sudo useradd --system --home /opt/diskwala-bot --shell /usr/sbin/nologin diskwalabot || true
sudo chown -R diskwalabot:diskwalabot /opt/diskwala-bot
sudo cp systemd/diskwala-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now diskwala-bot
sudo journalctl -u diskwala-bot -f
```

`Restart=always` makes systemd restart the process after a crash and start it after a server reboot.

## Owner commands

Run these only in the bot's private chat, from the Telegram account whose numeric ID is `OWNER_ID`:

```text
/stats
/broadcast Your announcement text
/setgroup -1001234567890
/maintenance on
/maintenance off
```

- Maintenance ON blocks new link requests. Downloads already running are allowed to finish.
- Broadcast sends to users who have previously opened the bot and sent it a message. Telegram may reject messages to users who blocked the bot; those are counted as failed.
- The bot does not expose a `/logs` command. Errors are written only to `data/logs/bot.log` and container/server logs.

## GitHub Actions 5h50m ON / 20m OFF cycle

The workflow at `.github/workflows/uptime-cycle.yml` checks the desired state every five minutes. It is a controller only; it does **not** host the bot. It needs your hosting provider to expose two idempotent HTTPS POST webhooks which start and stop the service/instance. The workflow needs repository read permission for checkout.

Add these GitHub repository Actions secrets:

- `CYCLE_ANCHOR_UTC` — cycle start, ISO-8601 timestamp with timezone, e.g. `2026-11-16T00:00:00Z`.
- `HOST_START_WEBHOOK` — your host's start-service HTTPS webhook.
- `HOST_STOP_WEBHOOK` — your host's stop-service HTTPS webhook.

The cycle is **350 minutes online + 20 minutes offline** (370 minutes total), repeated from the anchor. Before the anchor, the desired state is offline. The webhook endpoints must be idempotent because GitHub may repeat or delay scheduled runs. The controller sends JSON `{"desired_state": "online"}` or `{"desired_state": "offline"}` and does not print webhook URLs or response bodies.

GitHub Actions is only the controller; the hosting provider runs the bot. Scheduled workflows can be delayed or skipped, and this workflow polls every five minutes, so transitions are approximate—not second-exact. A public repository's scheduled workflows may also be automatically disabled after a long period without repository activity. If your provider has no start/stop webhook, this workflow cannot control it until a provider-specific integration is added.

The bot's runtime variables (`BOT_TOKEN`, `OWNER_ID`, `DESTINATION_CHAT_ID`, `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH`) belong in the hosting provider's private environment-variable settings. They are **not** automatically passed to the bot just because similarly named GitHub Actions secrets exist. The repository's `.env.example` contains placeholders only; never commit a real `.env` file.
## Security

- Never commit `.env`, bot token, Telegram API hash, or owner ID to a public repository.
- Keep the SQLite/data directory private; it contains user IDs and cached videos.
- Do not make the Local Bot API port publicly accessible unless protected by firewall/network controls.
- Use an isolated VPS/container and monitor disk space; 24-hour cache can still consume significant storage under heavy traffic.
