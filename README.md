# yt-mcp

Search YouTube Music, fetch or mix song-radio recommendations, and create
private playlists from a CLI or a local MCP server.

```mermaid
flowchart TD
    U[User or agent] --> E[yt CLI or yt-mcp]
    E --> O{Operation}
    O -->|Search| S[Anonymous YouTube Music song search]
    O -->|Single radio or playlist| Q[Find the first playable seed]
    Q --> R[Fetch the ordered song-radio queue]
    O -->|Radio mix or playlist| Q5[Resolve 2 to 10 playable seeds]
    Q5 --> R5[Fetch one radio queue per seed]
    R5 --> M[Round-robin and globally deduplicate]
    S --> N[Filter and normalize track metadata]
    R --> N
    M --> N
    N --> T[Structured CLI or MCP result]
    N -->|Playlist creation only| W[Create private playlist]
    A[Owner-only OAuth token] -. authorizes .-> W
    W --> J[Save owner-only resume plan]
    J --> I[Insert planned tracks]
    O -->|Resume| L[Load plan and read existing items]
    L --> I
    I --> P[Private YouTube playlist]
```

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
uv run yt search "Alan Sorrenti Figli delle stelle"
uv run yt radio "Alan Sorrenti Figli delle stelle" --limit 30
uv run yt radio-mix \
  "Tourist LeMC Adem" \
  "Brihang Steentje" \
  "Yong Yello Luchtkasteel" \
  "Zwangere Guy Beter Leven" \
  "Bazart Goud" \
  --limit 50
```

The `radio` command uses the first playable search result and shows the selected
seed. To choose a specific recording, copy its 11-character ID from `search`:

```sh
uv run yt radio --video-id VIDEO_ID --limit 30
uv run yt radio "Alan Sorrenti Figli delle stelle" --limit 30 --json > radio.json
```

Search and radio run anonymously. They read no account credentials and make no
account changes. Recommendations preserve YouTube's order, exclude the seed,
skip unavailable tracks, and remove duplicate video IDs. Results can vary and
can contain fewer songs than requested.

`radio-mix` accepts two to ten song queries. It resolves every query to the
first playable search result before fetching any radio queue and refuses seeds
that resolve to the same video. The mixer takes one new track from each radio in
seed order, repeats that round, removes all seed songs and cross-radio
duplicates, and stops at the total `--limit` (default 50, maximum 100). Its JSON
result includes the resolved `seeds` for review. A fixed set of queues always
mixes deterministically, but YouTube Music can return different queues between
runs. Exhausted queues produce a documented shortfall instead of filler tracks.

Structured CLI and MCP track results include `videoId`, `title`, `artists`,
`album`, `duration`, `duration_seconds`, and `url`. `album` and
`duration_seconds` are nullable because YouTube Music does not return them for
every item. The numeric duration is suitable for cross-catalog matching while
the original display duration remains available for compatibility.

## Connect a Google account with OAuth

Playlist creation uses Google OAuth. It never asks for or stores the Gmail
password. The OAuth consent grants the broad `youtube` scope, which can manage
the connected YouTube account; use a Google Cloud project you control and revoke
the grant from the Google account when it is no longer needed. Radio discovery
stays anonymous; authenticated writes use the official YouTube Data API.

1. In Google Cloud, enable the YouTube Data API, configure the OAuth consent
   screen, and create an OAuth client of type **TVs and Limited Input devices**.
2. If the consent screen is in testing, add the intended Google account as a
   test user. Download the client JSON to this repository as `oauth-client.json`.
3. Restrict the client file and start the local device authorization flow.
   Google asks you to choose the account during OAuth:

```sh
chmod 600 oauth-client.json
uv run --locked yt auth oauth
```

Choose the intended Google account in Google's browser page. The command writes
`oauth.json` with owner-only permissions. Both credential files are ignored by
Git. Do not paste their contents, the displayed device code, or tokens into chat.
On POSIX systems, playlist operations refuse either credential file when group
or other users have access; fix that with `chmod 600 FILE`.

With the default names, `yt-mcp` finds `oauth-client.json` beside `oauth.json`
and can refresh the token without `.env`. For custom paths, copy `.env.example`
to `.env` and use `--env-file .env`. Direct
`YTMUSIC_OAUTH_CLIENT_ID`/`YTMUSIC_OAUTH_CLIENT_SECRET` values remain supported.

Create a private playlist from a fresh radio queue:

```sh
uv run --locked yt playlist create "Italiaanse zomeravond" \
  "Alan Sorrenti Figli delle stelle" \
  --description "Warme Italiaanse avondmuziek" \
  --limit 100
```

> **Warning:** If OAuth access expires during either playlist write and cannot
> be refreshed, the private playlist can remain only partially populated. Keep
> its saved resume state and run `yt playlist resume PLAYLIST_ID` after restoring
> access instead of creating a replacement playlist.

Create one private playlist from a fresh multi-seed radio mix:

```sh
uv run --locked yt playlist create-mix "S&S: Italiaans" \
  "Vattene amore Mietta Amedeo Minghi" \
  "L'italiano Toto Cutugno" \
  "Fantastico Fai quello che sei Laura Pausini" \
  "Diamante Zucchero" \
  "Pastello Bianco Pinguini Tattici Nucleari" \
  "Lascia ch'io pianga Joyce DiDonato" \
  --description "Italiaanse radio mix" \
  --limit 100
```

Both commands print the new playlist URL. They fetch fresh radio results, so
their tracks may differ from an earlier read-only `radio` or `radio-mix` result.
All seed songs are excluded. Add `--json` for structured output.

Before inserting the first track, each create command stores its exact track
plan in an owner-only `.yt-mcp-state/PLAYLIST_ID.json` file beside `oauth.json`.
The plan is limited to 100 tracks and authenticated with the OAuth client
secret, so an edited plan is rejected before network access. If insertion is
interrupted, resume that same playlist without creating or recomputing anything:

```sh
uv run --locked yt playlist resume PLAYLIST_ID
```

Resume first verifies that the remote playlist is still private and that its
current video IDs are an exact prefix of the saved plan. It then appends only
the missing suffix. A completed sequential retry is a no-op; edits that change
the saved video-ID prefix stop before any write. Do not run two resumes for the
same playlist concurrently. Keep the resume file while the playlist may need
repair.

See the upstream [OAuth setup](https://ytmusicapi.readthedocs.io/en/stable/setup/oauth.html)
and Google's [YouTube OAuth guide](https://developers.google.com/youtube/v3/guides/authentication)
for the authorization model.

## Local MCP server

`yt-mcp` starts a local stdio MCP server with six focused tools:

| Tool | Effect |
| --- | --- |
| `search_songs` | Read-only song search |
| `get_song_radio` | Read-only radio recommendations |
| `get_multi_seed_radio` | Read-only round-robin mix from two to ten song radios |
| `create_private_radio_playlist` | Creates one private playlist in the connected account |
| `create_private_multi_seed_radio_playlist` | Creates one private playlist from a two-to-ten-seed mix |
| `resume_private_playlist` | Reconciles and completes an existing playlist from its saved plan |

Playlist creation and resume tools are marked non-read-only and non-idempotent.
Sequential resume retries reconcile one saved plan and are safe no-ops after
completion, but concurrent calls are not serialized. Compatible hosts can
request approval for either kind of write. The server has no generic method
that can invoke arbitrary `ytmusicapi` operations.

For local development with per-call write approval, register the checkout with
the local Codex CLI:

```sh
PROJECT_DIR="$PWD"
UV_BIN="$(command -v uv)"
codex mcp add youtube-music \
  -- "$UV_BIN" \
     --directory "$PROJECT_DIR" \
     run --locked yt-mcp
```

The token and OAuth client contents stay in their owner-readable files. The
server uses stdio and does not open a network listening port.

For an existing MCP registration, remove `--env-file` and its `.env` path from
the configured `args`, leaving `run --locked yt-mcp` after the project path.

Do not permanently approve a write tool that runs from an agent-writable
checkout: approval is attached to the tool name, not a hash of its source. For
no-prompt writes under an effective `approval_policy = "never"`, install a
reviewed, non-editable snapshot outside writable workspaces and keep its OAuth
files and generated resume state in an owner-only directory that is not an
agent workspace:

```sh
PROJECT_DIR="$PWD"
CREDENTIAL_DIR="$HOME/.config/yt-mcp"
STATE_DIR="$CREDENTIAL_DIR/.yt-mcp-state"
install -d -m 700 "$CREDENTIAL_DIR"
install -d -m 700 "$STATE_DIR"
install -m 600 \
  "$PROJECT_DIR/oauth.json" \
  "$PROJECT_DIR/oauth-client.json" \
  "$CREDENTIAL_DIR/"
if [ -d "$PROJECT_DIR/.yt-mcp-state" ]; then
  find "$PROJECT_DIR/.yt-mcp-state" -maxdepth 1 -type f -name '*.json' \
    -exec install -m 600 {} "$STATE_DIR/" \;
fi
uv tool install --force --link-mode copy "$PROJECT_DIR"
TOOL_BIN="$(uv tool dir --bin)/yt-mcp"
case "$TOOL_BIN" in
  "$PROJECT_DIR"/*) printf '%s\n' "Refusing a workspace tool path" >&2; exit 1 ;;
esac
printf '%s\n' "$TOOL_BIN"
```

Do not add `--editable`. Reinstall the snapshot after reviewing an update. Set
the existing MCP registration to the printed executable and protected paths.
The copied state preserves unfinished playlists. After restarting and verifying
the installed server, remove the original credential and state copies from the
workspace; until then the migration is incomplete. For a new setup, create the
OAuth files directly in the protected directory instead of copying them.

```toml
[mcp_servers.youtube-music]
command = "/absolute/path/from-uv-tool-dir/yt-mcp"
args = []
env = { YTMUSIC_AUTH_FILE = "/protected/path/oauth.json", YTMUSIC_OAUTH_CLIENT_FILE = "/protected/path/oauth-client.json" }
default_tools_approval_mode = "writes"

[mcp_servers.youtube-music.tools.create_private_multi_seed_radio_playlist]
approval_mode = "approve"

[mcp_servers.youtube-music.tools.resume_private_playlist]
approval_mode = "approve"
```

Write annotations can make a host request approval. If a Codex task runs with
an effective `approval_policy = "never"`, the exact tool overrides above avoid
a prompt while `default_tools_approval_mode = "writes"` keeps future write tools
prompt-gated. Do not set the whole server to `approve`, because that would also
pre-approve future write tools. After changing MCP configuration, restart the
server in Codex via Settings → MCP servers → Restart. If an old task retains its
earlier tool catalog or policy, start a new task as a troubleshooting step. A
managed deny remains binding.

The project pins `ytmusicapi` to 1.12.2 and the official MCP Python SDK to major
version 2. `ytmusicapi` is used anonymously against YouTube Music's unofficial
internal API for search and radio, so upstream changes can interrupt discovery.
Private playlist creation and item insertion use Google's official YouTube Data
API with the local OAuth token.

## Development

```sh
uv run --locked pytest
```

Tests use fake clients and make no network or account changes. A read-only live
smoke check is:

```sh
uv run --locked yt radio "Alan Sorrenti Figli delle stelle" --limit 5
```

See [the implementation plan](docs/plan.md) and the
[multi-seed plan](docs/multi-seed-plan.md), plus the
[resume plan](docs/resume-plan.md), for scope and acceptance criteria.

## License

MIT. See [LICENSE](LICENSE).
