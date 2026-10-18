# Security policy

GeoLogs stores where people have been, so a security problem in it can expose someone's whereabouts. Please report one privately and give it time to be fixed before talking about it.

## Reporting a vulnerability

Use GitHub's private reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Please do not open a public issue or pull request for a security problem.

Include what you found, how to reproduce it (the version or commit, your setup, and the requests or steps involved), and what an attacker could do with it. If you have a fix, say so, but a clear report is enough.

You can expect a reply within a week, and a fix or a plan for one within a month for anything serious. Reporters are credited in the release notes unless they prefer not to be.

## What counts

Anything that lets someone do what they should not be able to:

-   Read or change another user's data, or any data without signing in. Each user's rows are protected by database row-level security, so a way around that is serious.
-   Take over an account, bypass sign-in or two-factor sign-in, or forge, steal or reuse a session, reset link, invite or token.
-   Reach admin features without being an admin, or learn locations through them.
-   Run code or SQL on the server, or read files on it.
-   Make the server fetch addresses the operator did not intend (for example, through the webhook or an outside-service setting).
-   A weakness in how secrets are stored or logged.

Problems that need the operator's own mistakes (a weak `GEOLOGS_TOKEN`, plain HTTP on the internet, an exposed database port) are not vulnerabilities in GeoLogs, though documentation that makes them easier to avoid is welcome.

## Supported versions

Fixes go into the latest release and the `main` branch. Please check that a problem still exists there before reporting it.

## Running GeoLogs safely

-   Serve it over HTTPS, and keep the database off the internet. See [Deploying](docs/deploying.md#putting-it-on-the-internet).
-   Use long random values for `POSTGRES_PASSWORD`, `GEOLOGS_APP_DB_PASSWORD` and `GEOLOGS_TOKEN`, and keep `.env` and backups private.
-   Turn on two-factor sign-in for admin accounts.
-   Keep it updated, and read [Privacy](docs/privacy.md) for what leaves your server.
