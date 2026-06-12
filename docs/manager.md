# Deployment Manager

The manager creates isolated music bot deployments from this repository.

## Required Manager Env

Copy `manager.env.sample` to `manager.env` and fill:

```env
MANAGER_API_ID=
MANAGER_API_HASH=
MANAGER_BOT_TOKEN=
MANAGER_OWNER_ID=
MANAGER_DEFAULT_MONGO_URL=
```

Optional:

```env
MANAGER_SUDOERS=123456789,987654321
MANAGER_API_KEY=
DEPLOYMENTS_DIR=deployments
TEMPLATE_PATH=.
```

`MANAGER_SUDOERS` accepts multiple Telegram user IDs separated by commas or spaces. The owner is always authorized automatically. Recovery and failure notifications continue going only to `MANAGER_OWNER_ID`.

The manager owner can update access at runtime with `/addsudo <user_id>` and `/delsudo <user_id>`. `/sudolist` shows current access. Telegram-added sudoers are persisted in `manager_sudoers.json` and take effect immediately without restarting the manager.

The manager sends a recovery configuration backup to `MANAGER_OWNER_ID` every 24 hours. Use `/backup` for an immediate backup. The archive contains manager/deployment environment files and manager state, including credentials, but excludes MongoDB data, logs, downloads, caches, and session files. Store it securely. Configure the interval with `MANAGER_BACKUP_INTERVAL`.

## Run

```bash
pm2 delete world
pm2 start ecosystem.config.cjs
pm2 save
```

The ecosystem file sets `treekill: false`. This is required because PM2 otherwise kills deployment processes when it restarts the manager. Restart the manager with `pm2 restart ecosystem.config.cjs` so the setting remains explicit.

## Commands

```text
/newbot <name> <bot_token> [owner_id] [database_name]
/reconfigure <name> <bot_token> [owner_id]
/changedb <name> <database_name>
/list
/status <name>
/deploy <name>
/stop <name>
/delete <name>
/restart <name|all>
/logs <name>
/addbotsudo <name> <user_id>
/delbotsudo <name> <user_id>
/botsudolist <name>
```

Manager-authorized users can manage each deployed bot's sudo list directly with `/addbotsudo`, `/delbotsudo`, and `/botsudolist`.

**Activation required:** After `/addbotsudo` or `/delbotsudo`, the deployed bot owner or an existing sudo user must run `/refreshconfig` in that deployed bot's private chat. The database change is stored immediately, but the running bot continues using its cached sudo list until `/refreshconfig` is issued.

`/delete` stops the deployment if needed, permanently removes its deployment directory, and removes it from the manager store.

`/newbot` generates an isolated database name by default. To manually choose one, provide it after the owner ID or directly after the bot token when no owner ID is needed. Use `-` as the owner placeholder for a numeric database name:

```text
/newbot music_bot <bot_token> music_database
/newbot music_bot <bot_token> 123456789 music_database
/newbot music_bot <bot_token> - 12345
```

`/reconfigure` verifies a new bot token, rebuilds the existing deployment configuration, and restarts it while preserving its MongoDB connection, `DB_NAME`, `DEPLOYMENT_ID`, and stored setup data.

`/changedb` safely stops the specified deployment, switches only its `DB_NAME`, and starts it again. It does not copy, migrate, or delete data from either database.

`/restart <name>` restarts only the specified deployed bot. `/restart all` restarts every
running deployment while preserving intentionally stopped deployments. If streams are
active, each restart is persisted and waits until they finish. New playback requests are
paused while a restart is queued. `/list` and `/status` show pending restarts, and
`/stop <name>` cancels one while intentionally stopping the deployment. The requester is
notified when a waiting restart begins and when it completes.

`/logs <name>` sends a sanitized copy of the deployment's full run log.

## Health Monitoring

Deployed bots write a heartbeat every 15 seconds. The manager detects a process that
still exists but no longer responds, captures diagnostics, and restarts it automatically.
Automatic recovery stops after three attempts within one hour and sends an urgent alert.
Intentional `/stop` commands never trigger automatic recovery.

Existing deployments are not restarted when the manager is upgraded or restarted.
Heartbeat monitoring activates for them after their next manual or normal deployment restart.

## Deployment Setup Flow

After `/newbot`, setup happens inside the deployed bot:

1. Send `/start` to the deployed bot in private chat to claim owner.
2. Connect an assistant session with `/addsession` in private chat.
3. Optionally create a log group, add the deployed bot, promote it as admin, then run `/setlog` in that group.
4. Optionally set support group, updates channel, language, and more assistant sessions.

The log group is optional. Missing access, removal from the group, or lost admin rights disables log delivery without preventing the bot or assistants from starting.

You can skip first-user owner claiming by passing `owner_id` to `/newbot`.
The deployed bot owner can also transfer ownership later with:

```text
/changeowner <user_id>
```

Only the current owner can transfer ownership. The command asks whether the previous owner should remain as a sudo user.

The start-menu Owner button can point to a public Telegram profile independently of the owner ID:

```text
/config owner_link https://t.me/ViPdEeE
```

Assistant sessions can be removed by slot:

```text
/removesession <1|2|3>
```

Owners and sudo users can view configured session slots with `/sessions` and remove them with `/removesession`. Session strings are never displayed. Restart the deployed bot after removing a session to disconnect it and rebuild the assistant clients.

On Linux, deployments are launched outside the manager's process tree. Restarting a PM2-managed manager process therefore leaves already-running deployments untouched. Deployments started by older manager versions move to this detached launch model the next time they are started.

Using `/stop <name>` records a persistent intentional-stop state. Health monitoring and automatic recovery will not start that deployment again until `/deploy <name>` or `/restart <name>` is issued manually.

## Isolation Rules

Each manager-created deployment gets:

- `MANAGED_SETUP=True`
- A unique `DEPLOYMENT_ID`
- A unique `DB_NAME`
- A deployment-local `SESSION_PATH`

This prevents recreated deployments from inheriting old owner, logger, assistant session, or Pyrogram session state.

## Runtime Files

These are runtime artifacts and must not be committed:

- `manager.env`
- `manager_deployments.json`
- `deployments/`
- `*.session`
- `*.session-journal`
- `log.txt`
- `run.log`
- `cache/`
- `downloads/`
