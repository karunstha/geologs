# Accounts, email and the admin portal

GeoLogs keeps a separate archive for each user in one database. Your existing archive becomes the **owner**'s, the first user (an admin). `GEOLOGS_TOKEN` and the `.env` ingest token act as the owner.

```bash
docker compose exec app geologs owner-sign-in you@example.com      # give the owner an email and password
docker compose exec app geologs add-user friend@example.com        # another user (--admin for an admin)
docker compose exec app geologs add-token --user friend@example.com "MCP on laptop"   # a personal API token
docker compose exec app geologs invite friend@example.com           # an invite link (emailed when mail is set up)
```

Each asks for the password (or reads `GEOLOGS_NEW_PASSWORD`, for scripts). Commands about one archive (`import-csv`, `import-takeout`, `rebuild-stays`, `learn-paths`, `add-token`) take `--user EMAIL` and work on the owner's otherwise. A device token made in **Settings → Devices** sends locations as the user who made it. A request with a wrong token belongs to no one, so it no longer appears in anyone's device list. The reminder webhook (`GEOLOGS_WEBHOOK_URL`) is the owner's: other users' reminders are not sent to it.

Passwords are kept as scrypt hashes, and sessions and tokens only as SHA-256 hashes of what the browser or client holds. Sign-in takes the same time and gives the same answer for a wrong email and a wrong password, and after ten tries in fifteen minutes an address waits. Sessions last 30 days from last use; disabling a user ends theirs.

Every table that holds someone's data has a `user_id`, and PostgreSQL row-level security lets a connection see and change only the rows of the user it works for; a connection working for no one sees nothing. Shared lookups (place names, reverse geocoding, fuel prices) and the accounts themselves are the only exceptions, and a test fails if a new table is neither. A superuser (as the Compose database role is) skips row-level security, so the app switches its connections to the role `geologs_app`, which cannot, and refuses to run if that role is missing. Compose goes further: requests log in as `geologs_app` itself, with its own password (`GEOLOGS_APP_DB_PASSWORD`, which Compose requires), so no request connection is ever a superuser. The owner login in `GEOLOGS_DATABASE_URL` is used only to create and upgrade the tables when the server starts. Outside Compose, set `GEOLOGS_APP_DB_PASSWORD` the same way.

## Email

Set `GEOLOGS_PUBLIC_URL` (where people open GeoLogs, such as `https://geologs.example.com`) and your mail server (`GEOLOGS_SMTP_HOST`, `GEOLOGS_SMTP_PORT`, `GEOLOGS_SMTP_SECURITY` of `starttls`, `ssl` or `none`, `GEOLOGS_SMTP_USERNAME`, `GEOLOGS_SMTP_PASSWORD`, and `GEOLOGS_SMTP_FROM`) to email:

-   **Invites** from an admin (`POST /api/invites`, or `geologs invite`), valid for 7 days. Accepting one (`POST /api/invites/accept`) creates the account, its address proven, and signs it in. Without mail, the invite link is still given to the admin to share.
-   **Password resets** (`POST /api/password/forgot`, then `POST /api/password/reset`), valid for an hour. The answer is the same whether or not the address has an account; an address may ask five times an hour. A reset signs the account out everywhere.
-   **Address checks** (`POST /api/email/verify/send` while signed in, then `POST /api/email/verify`), valid for 2 days.

Each link is single-use and kept only as a hash. Messages wait in an outbox and a mail thread sends them, so a slow mail server never holds up a request; a failure is tried again after 1, 2, 4… minutes, eight times in all. A sent message's text (with its link) is not kept. Links open in the browser: a reset link asks for the new password, an invite link for a name and password (and signs the new account in), and a confirmation link just confirms.

## In the browser

The sign-in screen takes an email and password, offers **Forgot password?** when this server can email, and still lets the owner sign in with `GEOLOGS_TOKEN`. The account button at the top right opens **Account**: your name, your email (and whether it is confirmed), your password, the browsers you're signed in on (sign any of them out, or all but this one), and personal API tokens for your own tools. The owner, who has signed in with the token until now, can add an email and set a password there. Under **Two-factor sign-in** on the account page, add GeoLogs to an authenticator app (the setup key, or the link on a phone) and confirm with its code: signing in then asks for a code after the password. Each code works once, and eight recovery codes (shown once) each sign in once if the phone is lost. Turning it off needs the password. Signing in with `GEOLOGS_TOKEN` skips it: that token is the server's own key.

## Admin portal

Admins get an **Admin** group in **Settings**, with four pages:

-   **Users**: everyone's account, role, whether they confirmed their email or use two-factor sign-in, when they last signed in, and how much they recorded (fixes, trackers, the latest fix's time), never where. **Manage** makes someone an admin or a user (there is always at least one admin), makes a password-reset link (emailed when the server can, and shown to pass on), disables an account (signing it out everywhere), or deletes it with everything it recorded, after you type its email. The owner's account cannot be disabled or deleted, and nor can your own.
-   **Invites**: invite someone as a user or an admin, copy the link, and withdraw invites not yet used.
-   **Activity**: every change an admin made, by whom, to whom, and when.
-   **Server**: who may sign up (invite only, the default, or open to anyone who can reach the server), whether email is set up and how recent messages went, tracker requests with a wrong token (which belong to no one, so they show here), the background jobs, the database's size, and how often outside services were asked today.

Counts are taken by the background worker while it works as each user, so the portal never reads anyone's rows. They follow new fixes within five minutes.
