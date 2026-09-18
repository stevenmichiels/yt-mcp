# MCP Setup

`yt-mcp` is a local stdio server. It does not open a listening network port,
and OAuth tokens remain in owner-readable files.

## Register a Development Checkout

For local development with per-call approval of write tools, register the
current checkout with Codex:

```sh
PROJECT_DIR="$PWD"
UV_BIN="$(command -v uv)"
codex mcp add youtube-music \
  -- "$UV_BIN" \
     --directory "$PROJECT_DIR" \
     run --locked yt-mcp
```

For an existing registration, remove `--env-file` and its `.env` path from
the configured arguments. With default credential filenames, the final
arguments only need `run --locked yt-mcp` after the project path.

Do not permanently approve write tools from this mutable checkout. For
no-prompt writes, first follow the
[protected snapshot procedure](mcp-security.md#install-a-reviewed-snapshot).

## Configure a Protected Installation

After installing a reviewed snapshot, point the MCP registration at the
executable reported by `uv tool dir --bin` and at credential files outside
agent-writable workspaces:

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

The exact write-tool overrides allow those reviewed operations when a task has
an effective `approval_policy = "never"`. Keeping
`default_tools_approval_mode = "writes"` ensures that future write tools are
still prompt-gated. Do not approve the whole server.

After changing MCP configuration, restart the server in Codex through
**Settings → MCP servers → Restart**. If an existing task retains an older tool
catalog or policy, start a new task. A managed deny remains binding.

## Runtime Boundary

The project pins `ytmusicapi` to 1.12.2 and the official MCP Python SDK to
major version 2. Read tools use `ytmusicapi` anonymously against YouTube
Music's unofficial internal API. Playlist writes use the official YouTube Data
API with the configured OAuth token.

The server exposes six focused tools and no generic method for arbitrary
`ytmusicapi` operations. See the [README](../README.md#mcp-server) for the
complete tool table.
