# Access Request Workflow

## Overview

The whitelist feature now includes a **polite access request workflow** instead of silently blocking users. When a non-whitelisted user tries to use the bot, they receive a friendly welcome message and their access request is automatically created for admin approval.

## User Experience

### First Contact (Non-Whitelisted User)

When a user without a username tries to use the bot:

```
👋 Welcome! I noticed you don't have a Telegram username set.

To use this bot, you'll need to set a username in Telegram settings:
Settings → Edit Profile → Username

Please set one and try again!
```

When a user with a username tries to use the bot:

```
👋 Welcome, [First Name]!

Thank you for your interest in using this bot. Your username is @[username].

Your access request has been submitted to the administrators for approval. 
You'll be notified once your request is reviewed.

📝 Request Status: Pending Approval
```

### Follow-Up Messages

If the user sends another message while still pending:

```
📝 Your access request is pending approval.

An administrator will review your request shortly. 
You'll be notified once a decision is made.
```

## Admin Commands

### View Pending Requests

```bash
/requests
```

Shows all pending access requests with:
- Username and full name
- User ID (needed for approve/reject)
- Request timestamp
- Optional message from user

Example output:
```
📋 Pending Access Requests (2):

1. @alice (Alice Smith)
   User ID: 123456789
   Requested: 2024-01-15T10:30:00

2. @bob (Bob Jones)
   User ID: 987654321
   Requested: 2024-01-15T11:45:00
   Message: I'd like to use this bot for my project
```

### Approve a Request

```bash
/approve <user_id>
```

Approves the access request and adds the user to the whitelist.

Example:
```bash
/approve 123456789
```

Response:
```
✅ Access request approved!

User: @alice
Approved by: @admin

They can now use the bot.
```

### Reject a Request

```bash
/reject <user_id>
```

Rejects the access request. The user is **not** notified (to avoid spam/abuse).

Example:
```bash
/reject 987654321
```

Response:
```
✅ Access request rejected.

User: @bob

They will not be notified.
```

## Data Model

### AccessRequest

Stored in `whitelist.json` under `access_requests`:

```json
{
  "username": "alice",
  "first_name": "Alice",
  "last_name": "Smith",
  "user_id": 123456789,
  "chat_id": 123456789,
  "requested_at": "2024-01-15T10:30:00",
  "status": "pending",
  "message": "I'd like to use this bot"
}
```

**Status values:**
- `pending` - Waiting for admin approval
- `approved` - Approved and added to whitelist
- `rejected` - Rejected by admin

## Workflow Diagram

```
┌─────────────────────┐
│ Non-whitelisted    │
│ user sends message │
└──────────┬──────────┘
           │
           ▼
    ┌──────────────┐
    │ No username? │──Yes──▶ Ask user to set username
    └──────┬───────┘
           │ No
           ▼
    ┌────────────────────┐
    │ Check for existing │
    │ pending request    │
    └────────┬───────────┘
             │
             ├──Exists──▶ "Request pending"
             │
             └──New─────▶ Create request
                          │
                          ▼
                   ┌──────────────┐
                   │ Send welcome │
                   │ message      │
                   └──────────────┘
                          │
                          ▼
                   ┌──────────────┐
                   │ Log request  │
                   │ for admins   │
                   └──────────────┘
                          │
                          ▼
                   ┌──────────────┐
                   │ Admin reviews│
                   │ with /requests│
                   └──────┬───────┘
                          │
                 ┌────────┴────────┐
                 │                 │
           /approve            /reject
                 │                 │
                 ▼                 ▼
            Add to           Mark rejected
          whitelist          (no notification)
                 │
                 ▼
          User can now use bot
```

## Configuration

### Enable/Disable

Set in `whitelist.json`:

```json
{
  "enabled": true,
  "admin_usernames": ["admin1", "admin2"],
  "entries": [...],
  "access_requests": [...]
}
```

### Silent Mode (Not Recommended)

If you want to revert to silent blocking (no messages), change in `bot.py`:

```python
# Old behavior (silent blocking)
whitelist_middleware = whitelist.create_middleware(silent=True)

# New behavior (polite access requests)
whitelist_middleware = whitelist.create_middleware(silent=False)
```

## Security Considerations

### No Spam Protection

- Users can send multiple messages before being approved
- Each message creates an access request (checks for existing pending request)
- Consider rate limiting in production

### No User Notification on Rejection

- Rejected users are **not** notified to avoid:
  - Spamming rejected users
  - Tipping off abusive users
  - Reducing bot ban risk

### Admin Verification

- Admins should verify users before approving
- Check user profiles if possible
- Consider adding a "message" field for users to introduce themselves

## Future Enhancements

Possible improvements:

1. **Admin notifications in-chat** - Send Telegram messages to admins when new requests arrive
2. **User introductions** - Capture user's first message as their introduction
3. **Expiration** - Auto-expire pending requests after N days
4. **Request limits** - Max N requests per user per day
5. **Appeal process** - Allow rejected users to re-request after cooldown
6. **Admin notes** - Allow admins to add notes to requests (why approved/rejected)
7. **Bulk operations** - Approve/reject multiple requests at once

## Testing

### Test as Non-Whitelisted User

1. Remove yourself from whitelist: `/whitelist_remove @your_username`
2. Send any message to the bot
3. You should receive the welcome message
4. Check that request was created with `/requests`

### Test Admin Approval

1. As admin, run `/requests`
2. Note the user_id from the output
3. Approve with `/approve <user_id>`
4. User should now be able to use the bot

### Test Admin Rejection

1. Create another request (or wait for new user)
2. Reject with `/reject <user_id>`
3. User should not be notified
4. Request status should be "rejected"
