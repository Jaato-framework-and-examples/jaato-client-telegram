# Whitelist Feature Documentation

## Overview

The whitelist feature allows you to control which Telegram users can access your bot. Only whitelisted users will be able to send messages and receive responses. Non-whitelisted users are silently ignored.

## Features

- **Username-based access control** - Allow specific Telegram usernames
- **JSON file storage** - Easy to edit and version control
- **Admin commands** - Manage whitelist via Telegram commands
- **Silent blocking** - Non-whitelisted users receive no feedback
- **Multiple admins** - Grant management permissions to multiple users
- **Runtime reload** - Reload whitelist without restarting the bot

## Quick Start

### 1. Create the Whitelist File

Copy the example whitelist:

```bash
cp whitelist.example.json whitelist.json
```

### 2. Configure Your Admins and Users

Edit `whitelist.json`:

```json
{
  "enabled": true,
  "admin_usernames": [
    "alice",
    "bob"
  ],
  "entries": [
    {
      "username": "alice",
      "added_by": "system",
      "added_at": "2024-01-01T00:00:00"
    },
    {
      "username": "charlie",
      "added_by": "alice",
      "added_at": "2024-01-01T12:00:00"
    }
  ]
}
```

**Fields:**
- `enabled`: Set to `false` to disable whitelist and allow everyone
- `admin_usernames`: List of usernames who can manage the whitelist
- `entries`: List of whitelisted users with metadata

### 3. Start the Bot

```bash
python -m jaato_client_telegram
```

The bot will automatically load `whitelist.json` from the current directory.

### 4. Test the Whitelist

- **Whitelisted user** (@alice): Can send messages and use the bot
- **Non-whitelisted user** (@eve): Messages are silently ignored
- **Admin** (@alice): Can use admin commands to manage the whitelist

## Admin Commands

Only users listed in `admin_usernames` can use these commands.

### /whitelist_add @username

Add a user to the whitelist.

**Example:**
```
/whitelist_add @david
```

**Response:**
```
✅ Added @david to the whitelist.

They can now use the bot.
```

### /whitelist_remove @username

Remove a user from the whitelist.

**Example:**
```
/whitelist_remove @charlie
```

**Response:**
```
✅ Removed @charlie from the whitelist.

They can no longer use the bot.
```

### /whitelist_list

List all whitelisted users.

**Example:**
```
/whitelist_list
```

**Response:**
```
📋 Whitelisted Users (3):

• @alice
• @bob
• @charlie
```

### /whitelist_reload

Reload the whitelist from the JSON file.

**Use case:** Manually edited `whitelist.json` and want to apply changes without restarting the bot.

**Example:**
```
/whitelist_reload
```

**Response:**
```
✅ Whitelist reloaded from file.

Current users: 3
```

### /whitelist_status

Show whitelist status and your access level.

**Example:**
```
/whitelist_status
```

**Response:**
```
🔒 Whitelist Status

Enabled: ✅ Yes
Total users: 3

Your username: @alice
Your access: ✅ Allowed
Admin: ✅ Yes
```

## Usernames Requirements

Users **must have a Telegram username** to use the bot when whitelist is enabled:

1. Open Telegram
2. Go to **Settings** → **Edit Profile**
3. Set a **username** (e.g., @alice)

Users without usernames will be silently ignored, even if you try to add them to the whitelist.

## Disabling the Whitelist

To allow **anyone** to use the bot:

### Option 1: Edit whitelist.json

```json
{
  "enabled": false,
  "admin_usernames": [],
  "entries": []
}
```

Then reload:
```
/whitelist_reload
```

### Option 2: Use CLI flag

Start the bot without whitelist:
```bash
python -m jaato_client_telegram --whitelist /dev/null
```

Or create an empty `whitelist.json`:
```json
{
  "enabled": false
}
```

## Advanced Usage

### Custom Whitelist Path

Use a custom whitelist file location:

```bash
python -m jaato_client_telegram --whitelist /path/to/custom_whitelist.json
```

### Programmatic Management

You can also edit `whitelist.json` directly while the bot is running, then use `/whitelist_reload` to apply changes.

### Backup and Version Control

The whitelist file is plain JSON. You can:
- Commit it to git (be careful with sensitive user data)
- Keep backups
- Copy between environments

## Troubleshooting

### "User not authorized" error

**Cause:** User is not in the whitelist.

**Solution:** Add the user with `/whitelist_add @username` (admin only).

### User without username can't access

**Cause:** User hasn't set a Telegram username.

**Solution:** Ask the user to set a username in Telegram Settings.

### Whitelist changes not taking effect

**Cause:** Bot hasn't reloaded the whitelist file.

**Solution:** Use `/whitelist_reload` or restart the bot.

### Admin commands not working

**Cause:** Your username is not in `admin_usernames`.

**Solution:** Add your username to `admin_usernames` in `whitelist.json` and reload.

## Security Considerations

### Best Practices

1. **Limit admins** - Only grant admin access to trusted users
2. **Monitor access** - Regularly review the whitelist with `/whitelist_list`
3. **Backup whitelist** - Keep a copy in case of accidental deletion
4. **Use usernames** - Usernames are more stable than numeric IDs
5. **Version control** - Commit whitelist changes to track modifications

### What the Whitelist Does

- ✅ Blocks non-whitelisted users from sending messages
- ✅ Silently ignores blocked users (no feedback)
- ✅ Allows admins to manage the whitelist via commands
- ✅ Supports runtime reloading without restart

### What the Whitelist Doesn't Do

- ❌ Doesn't hide the bot from Telegram search
- ❌ Doesn't prevent users from starting a chat
- ❌ Doesn't provide rate limiting or abuse protection
- ❌ Doesn't encrypt or secure the whitelist file

For production deployments, consider:
- File permissions on `whitelist.json` (read/write to bot user only)
- Additional rate limiting per user
- Monitoring and alerting for suspicious activity

## Examples

### Example 1: Personal Bot

For a personal bot that only you use:

```json
{
  "enabled": true,
  "admin_usernames": ["your_username"],
  "entries": [
    {
      "username": "your_username",
      "added_by": "system",
      "added_at": "2024-01-01T00:00:00"
    }
  ]
}
```

### Example 2: Team Bot

For a team bot with multiple users and one admin:

```json
{
  "enabled": true,
  "admin_usernames": ["alice"],
  "entries": [
    {
      "username": "alice",
      "added_by": "system",
      "added_at": "2024-01-01T00:00:00"
    },
    {
      "username": "bob",
      "added_by": "alice",
      "added_at": "2024-01-01T12:00:00"
    },
    {
      "username": "charlie",
      "added_by": "alice",
      "added_at": "2024-01-01T12:01:00"
    }
  ]
}
```

### Example 3: Multiple Admins

For shared management:

```json
{
  "enabled": true,
  "admin_usernames": ["alice", "bob"],
  "entries": [
    {
      "username": "alice",
      "added_by": "system",
      "added_at": "2024-01-01T00:00:00"
    },
    {
      "username": "bob",
      "added_by": "system",
      "added_at": "2024-01-01T00:00:00"
    },
    {
      "username": "charlie",
      "added_by": "alice",
      "added_at": "2024-01-01T12:00:00"
    }
  ]
}
```

## Integration with Existing Features

The whitelist works seamlessly with existing bot features:

- **Session isolation** - Each whitelisted user still gets their own isolated session
- **Commands** - All bot commands work normally for whitelisted users
- **Streaming** - Response streaming is unaffected by the whitelist
- **Workspace isolation** - Each user's workspace remains separate

The whitelist is checked **before** any message processing, so non-whitelisted users never consume server resources.
