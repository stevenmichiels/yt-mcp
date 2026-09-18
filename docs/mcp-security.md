# MCP Security and Approvals

MCP write tools are deliberately separated from read-only search and radio
operations. Compatible hosts can request approval for playlist creation and
resume calls, which are marked non-read-only and non-idempotent.

Sequential resume retries reconcile one saved plan and become no-ops after
completion. Concurrent calls are not serialized and must not be pre-approved as
if they were safe retries.

## Why a Mutable Checkout Is Unsafe

Do not permanently approve a write tool that runs from an agent-writable
checkout. Approval is attached to the tool name, not to a hash of its source,
so later source changes could alter what an approved tool does.

For no-prompt writes, install a reviewed, non-editable snapshot outside writable
workspaces. Keep its OAuth files and generated resume state in an owner-only
directory that is not an agent workspace.

## Install a Reviewed Snapshot

Run this from the reviewed repository checkout:

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

Do not add `--editable`. Reinstall the snapshot after reviewing an update.
The copied state preserves unfinished playlists.

After restarting and verifying the installed server, remove the original
credential and state copies from the workspace. Until then, the migration is
incomplete. For a new setup, create the OAuth files directly in the protected
directory instead of copying them.

## Configure Approvals

Set the MCP registration to the printed executable and protected credential
paths. Approve only the reviewed write tools you intend to run without prompts;
keep the server default set to prompt for writes so newly added tools do not
inherit approval automatically.

The exact Codex configuration and restart procedure are in
[MCP setup](mcp-setup.md#configure-a-protected-installation).

Write annotations can make a compatible host request approval, but they cannot
override a managed deny. Review source changes and reinstall the protected
snapshot before trusting a new version.
