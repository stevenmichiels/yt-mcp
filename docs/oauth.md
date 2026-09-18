# OAuth and Playlist Recovery

Playlist creation uses Google OAuth and the official YouTube Data API. Search
and radio discovery remain anonymous and do not read account credentials.

The requested `youtube` scope can manage the connected YouTube account. Use a
Google Cloud project you control, connect only the intended account, and revoke
the grant from that account when it is no longer needed. `yt-mcp` never asks
for or stores a Gmail password.

## Google Cloud Setup

1. Enable the YouTube Data API in Google Cloud.
2. Configure the OAuth consent screen. If it is in testing, add the intended
   Google account as a test user.
3. Create an OAuth client of type **TVs and Limited Input devices**.
4. Download the client JSON as `oauth-client.json`.
5. Restrict the file and start the device authorization flow:

```sh
chmod 600 oauth-client.json
uv run --locked yt auth oauth
```

Choose the intended Google account on Google's authorization page. The command
writes `oauth.json` with owner-only permissions.

## Credential Files

Both OAuth files are ignored by Git. Do not paste their contents, displayed
device codes, or tokens into chat. On POSIX systems, playlist operations refuse
credential files that are readable or writable by group or other users:

```sh
chmod 600 oauth.json oauth-client.json
```

With the default names, `yt-mcp` finds `oauth-client.json` beside
`oauth.json` and can refresh the token without an environment file. For custom
paths, copy `.env.example` to `.env`, set absolute paths, and pass
`--env-file .env`:

```dotenv
YTMUSIC_OAUTH_CLIENT_FILE=/absolute/path/to/oauth-client.json
YTMUSIC_AUTH_FILE=/absolute/path/to/oauth.json
```

Direct `YTMUSIC_OAUTH_CLIENT_ID` and `YTMUSIC_OAUTH_CLIENT_SECRET` values
remain supported, but protected files are easier to review and manage.

## Create a Private Playlist

Create a playlist from a single song radio:

```sh
uv run --locked yt playlist create "Italiaanse zomeravond" \
  "Alan Sorrenti Figli delle stelle" \
  --description "Warme Italiaanse avondmuziek" \
  --limit 100
```

Both single-radio and multi-seed creation fetch fresh recommendations. Their
tracks can therefore differ from an earlier read-only result. Seed songs are
excluded, and `--json` returns structured output.

## Recover an Interrupted Write

Before inserting the first track, each create command stores its exact plan in
`.yt-mcp-state/PLAYLIST_ID.json` beside `oauth.json`. The owner-only plan is
limited to 100 tracks and authenticated with the OAuth client secret; an edited
plan is rejected before network access.

If OAuth expires during a write and cannot be refreshed, the playlist can
remain partially populated. Restore access and resume the same playlist rather
than creating a replacement:

```sh
uv run --locked yt playlist resume PLAYLIST_ID
```

Resume verifies that the remote playlist is private and that its current video
IDs are an exact prefix of the saved plan. It appends only the missing suffix.
A completed sequential retry is a no-op; a changed prefix stops before any
write. Do not run concurrent resumes for the same playlist, and keep the state
file while recovery may still be needed.

For the underlying authorization model, see the upstream
[ytmusicapi OAuth setup](https://ytmusicapi.readthedocs.io/en/stable/setup/oauth.html)
and Google's
[YouTube OAuth guide](https://developers.google.com/youtube/v3/guides/authentication).
