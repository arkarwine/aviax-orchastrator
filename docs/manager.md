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
MANAGER_API_KEY=
DEPLOYMENTS_DIR=deployments
TEMPLATE_PATH=.
```

## Run

```bash
pm2 start "python3 manager.py" --name world
```

## Commands

```text
/newbot <name> <bot_token> [owner_id] [database_name]
/reconfigure <name> <bot_token> [owner_id]
/list
/status <name>
/deploy <name>
/stop <name>
/delete <name>
/restart <name>
```

`/delete` stops the deployment if needed, permanently removes its deployment directory, and removes it from the manager store.

`/newbot` generates an isolated database name by default. To manually choose one, provide it after the owner ID or directly after the bot token when no owner ID is needed. Use `-` as the owner placeholder for a numeric database name:

```text
/newbot music_bot <bot_token> music_database
/newbot music_bot <bot_token> 123456789 music_database
/newbot music_bot <bot_token> - 12345
```

`/reconfigure` verifies a new bot token, rebuilds the existing deployment configuration, and restarts it while preserving its MongoDB connection, `DB_NAME`, `DEPLOYMENT_ID`, and stored setup data.

`/restart <name>` restarts only the specified deployed bot. It does not restart the manager.

## Deployment Setup Flow

After `/newbot`, setup happens inside the deployed bot:

1. Send `/start` to the deployed bot in private chat to claim owner.
2. Create a log group, add the deployed bot, promote it as admin, then run `/setlog` in that group.
3. Use the Next button from the `/setlog` success message to start assistant session extraction in DM.
4. Optionally set support group, updates channel, language, and more assistant sessions.

You can skip first-user owner claiming by passing `owner_id` to `/newbot`.
The deployed bot owner can also transfer ownership later with:

```text
/config owner_id <user_id>
```

Only the current owner can transfer ownership. The new owner receives sudo access, and the previous owner loses it.

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
