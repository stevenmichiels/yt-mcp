"""Local MCP server for focused YouTube Music discovery and playlist tools."""

import os
from pathlib import Path
from typing import cast

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from typing_extensions import TypedDict

from yt_mcp import (
    RecommendationError,
    api_session,
    create_playlist_from_multi_seed_radio,
    create_playlist_from_radio,
    get_multi_seed_radio as get_multi_seed_radio_results,
    get_radio,
    normalize_playlist_id,
    normalize_seed_queries,
    search_songs as search_song_results,
    youtube_data_client,
)
from ytmusicapi import YTMusic


SERVER_ROOT = Path(__file__).resolve().parent
mcp = MCPServer(
    "YouTube Music Recommender",
    instructions=(
        "Search, radio, and multi-seed radio tools are read-only. "
        "Playlist-creation tools create a new private playlist in the configured "
        "Google account. The resume tool only completes the saved plan for an "
        "existing private playlist. Call write tools only when the user asks."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
CREATES_PLAYLIST = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
RESUMES_PLAYLIST = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


class TrackResult(TypedDict):
    videoId: str
    title: str
    artists: list[str]
    album: str | None
    duration: str | None
    duration_seconds: int | None
    url: str


class SearchResult(TypedDict):
    tracks: list[TrackResult]


class RadioResult(TypedDict):
    seed: TrackResult
    requested: int
    returned: int
    tracks: list[TrackResult]


class MultiSeedRadioResult(TypedDict):
    seeds: list[TrackResult]
    requested: int
    returned: int
    tracks: list[TrackResult]


class PlaylistResult(TypedDict):
    playlistId: str
    title: str
    privacyStatus: str
    url: str
    seed: TrackResult
    requested: int
    trackCount: int
    tracks: list[TrackResult]


class MultiSeedPlaylistResult(TypedDict):
    playlistId: str
    title: str
    privacyStatus: str
    url: str
    seeds: list[TrackResult]
    requested: int
    trackCount: int
    tracks: list[TrackResult]


class ResumePlaylistResult(TypedDict):
    playlistId: str
    title: str
    privacyStatus: str
    url: str
    requested: int
    previousTrackCount: int
    addedTrackCount: int
    remainingTrackCount: int
    trackCount: int


def _query(value):
    value = value.strip()
    if not value:
        raise ToolError("The song query must not be blank.")
    return value


def _limit(value):
    if not 1 <= value <= 100:
        raise ToolError("limit must be between 1 and 100.")
    return value


def _queries(values):
    try:
        return normalize_seed_queries(values)
    except RecommendationError as error:
        raise ToolError(str(error)) from None


def _playlist_title(value):
    value = value.strip()
    if not value:
        raise ToolError("The playlist title must not be blank.")
    if "<" in value or ">" in value:
        raise ToolError("The playlist title must not contain < or >.")
    return value


def _playlist_id(value):
    try:
        return normalize_playlist_id(value)
    except RecommendationError as error:
        raise ToolError(str(error)) from None


def _auth_file():
    configured = os.environ.get("YTMUSIC_AUTH_FILE")
    path = Path(configured).expanduser() if configured else SERVER_ROOT / "oauth.json"
    if not path.is_file():
        raise RecommendationError(
            f"Authentication file not found: {path}. Run 'uv run yt auth oauth' first."
        )
    return path


def _request(operation):
    try:
        return operation()
    except (RecommendationError, ValueError) as error:
        raise ToolError(str(error)) from None
    except Exception as error:
        raise ToolError(
            f"YouTube request failed ({type(error).__name__}); check the connection "
            "and retry."
        ) from None


@mcp.tool(title="Search YouTube Music songs", annotations=READ_ONLY)
def search_songs(query: str, limit: int = 5) -> SearchResult:
    """Find songs with IDs, artists, album titles, durations, and links."""
    query = _query(query)
    limit = _limit(limit)

    def run():
        with api_session() as session:
            client = YTMusic(requests_session=session)
            return {"tracks": search_song_results(client, query, limit)}

    return cast(SearchResult, _request(run))


@mcp.tool(title="Get a YouTube Music song radio", annotations=READ_ONLY)
def get_song_radio(query: str, limit: int = 30) -> RadioResult:
    """Get YouTube Music's ordered radio recommendations around the first song match."""
    query = _query(query)
    limit = _limit(limit)

    def run():
        with api_session() as session:
            client = YTMusic(requests_session=session)
            return get_radio(client, query, None, limit)

    return cast(RadioResult, _request(run))


@mcp.tool(title="Mix multiple YouTube Music song radios", annotations=READ_ONLY)
def get_multi_seed_radio(
    queries: list[str], limit: int = 50
) -> MultiSeedRadioResult:
    """Round-robin two to ten song radios into one deduplicated result."""
    queries = _queries(queries)
    limit = _limit(limit)
    if limit < len(queries):
        raise ToolError("limit must be at least the number of seed queries.")

    def run():
        with api_session() as session:
            client = YTMusic(requests_session=session)
            return get_multi_seed_radio_results(client, queries, limit)

    return cast(MultiSeedRadioResult, _request(run))


@mcp.tool(title="Create a private radio playlist", annotations=CREATES_PLAYLIST)
def create_private_radio_playlist(
    title: str,
    query: str,
    description: str = "Created by yt-mcp.",
    limit: int = 30,
) -> PlaylistResult:
    """Create a private playlist from a fresh song-radio queue in the connected account."""
    title = _playlist_title(title)
    query = _query(query)
    limit = _limit(limit)

    def run():
        with api_session() as session:
            playlist_client = youtube_data_client(_auth_file(), session)
            discovery_client = YTMusic(requests_session=session)
            return create_playlist_from_radio(
                discovery_client, playlist_client, title, description, query, limit
            )

    return cast(PlaylistResult, _request(run))


@mcp.tool(title="Create a private multi-seed radio playlist", annotations=CREATES_PLAYLIST)
def create_private_multi_seed_radio_playlist(
    title: str,
    queries: list[str],
    description: str = "Created by yt-mcp.",
    limit: int = 50,
) -> MultiSeedPlaylistResult:
    """Create one private playlist from a fresh two-to-ten-seed radio mix."""
    title = _playlist_title(title)
    queries = _queries(queries)
    limit = _limit(limit)
    if limit < len(queries):
        raise ToolError("limit must be at least the number of seed queries.")

    def run():
        with api_session() as session:
            playlist_client = youtube_data_client(_auth_file(), session)
            discovery_client = YTMusic(requests_session=session)
            return create_playlist_from_multi_seed_radio(
                discovery_client,
                playlist_client,
                title,
                description,
                queries,
                limit,
            )

    return cast(MultiSeedPlaylistResult, _request(run))


@mcp.tool(title="Resume a private yt-mcp playlist", annotations=RESUMES_PLAYLIST)
def resume_private_playlist(playlist_id: str) -> ResumePlaylistResult:
    """Resume one exact saved track plan; do not invoke concurrently."""
    playlist_id = _playlist_id(playlist_id)

    def run():
        with api_session() as session:
            playlist_client = youtube_data_client(_auth_file(), session)
            return playlist_client.resume_private_playlist(playlist_id)

    return cast(ResumePlaylistResult, _request(run))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
