from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mcp import Client

import yt_mcp_server


SEED = "seed0000001"
SEED_TWO = "seed0000002"
FIRST = "track000001"
SECOND = "track000002"


def song(identifier, title="A song"):
    return {
        "videoId": identifier,
        "title": title,
        "artists": [{"name": "An artist"}],
        "album": {"name": "An album", "id": "MPREalbum123"},
        "duration": "3:12",
        "duration_seconds": 192,
    }


@pytest.fixture
def services():
    with (
        patch("yt_mcp_server.YTMusic") as public_factory,
        patch("yt_mcp.YouTubeDataAPI") as youtube_api_factory,
        patch("yt_mcp.OAuthCredentials"),
        patch.dict(
            "yt_mcp.os.environ",
            {
                "YTMUSIC_OAUTH_CLIENT_ID": "test-client-id",
                "YTMUSIC_OAUTH_CLIENT_SECRET": "test-client-secret",
            },
            clear=True,
        ),
    ):
        client = public_factory.return_value
        client.search.return_value = [song(SEED, "Figli delle stelle")]
        client.get_watch_playlist.return_value = {
            "tracks": [song(SEED), song(FIRST), song(SECOND)]
        }
        youtube_api_factory.return_value.create_private_playlist.return_value = (
            "PLcreated123"
        )
        youtube_api_factory.return_value.resume_private_playlist.return_value = {
            "playlistId": "PLcreated123",
            "title": "S&S: Italiaans",
            "privacyStatus": "PRIVATE",
            "url": "https://music.youtube.com/playlist?list=PLcreated123",
            "requested": 100,
            "previousTrackCount": 23,
            "addedTrackCount": 77,
            "remainingTrackCount": 0,
            "trackCount": 100,
        }

        yield SimpleNamespace(
            public_factory=public_factory,
            youtube_api_factory=youtube_api_factory,
        )


@pytest.mark.asyncio
async def test_tools_have_structured_results_and_write_annotations(services):
    async with Client(yt_mcp_server.mcp, raise_exceptions=True) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        result = await client.call_tool(
            "search_songs", {"query": "Alan Sorrenti", "limit": 1}
        )

    assert set(tools) == {
        "search_songs",
        "get_song_radio",
        "get_multi_seed_radio",
        "create_private_radio_playlist",
        "create_private_multi_seed_radio_playlist",
        "resume_private_playlist",
    }
    assert tools["search_songs"].annotations.read_only_hint is True
    assert tools["get_multi_seed_radio"].annotations.read_only_hint is True
    write_annotations = tools["create_private_radio_playlist"].annotations
    assert write_annotations.read_only_hint is False
    assert write_annotations.destructive_hint is False
    assert write_annotations.idempotent_hint is False
    multi_write_annotations = tools[
        "create_private_multi_seed_radio_playlist"
    ].annotations
    assert multi_write_annotations.read_only_hint is False
    assert multi_write_annotations.destructive_hint is False
    assert multi_write_annotations.idempotent_hint is False
    resume_annotations = tools["resume_private_playlist"].annotations
    assert resume_annotations.read_only_hint is False
    assert resume_annotations.destructive_hint is False
    assert resume_annotations.idempotent_hint is False
    track_schema = tools["search_songs"].output_schema["$defs"]["TrackResult"]
    assert set(track_schema["properties"]) == {
        "videoId",
        "title",
        "artists",
        "album",
        "duration",
        "duration_seconds",
        "url",
    }
    track = result.structured_content["tracks"][0]
    assert track["videoId"] == SEED
    assert track["album"] == "An album"
    assert track["duration_seconds"] == 192


@pytest.mark.asyncio
async def test_radio_tool_uses_fresh_youtube_music_radio(services):
    async with Client(yt_mcp_server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "get_song_radio", {"query": "Alan Sorrenti", "limit": 2}
        )

    assert result.structured_content["returned"] == 2
    assert [
        track["videoId"] for track in result.structured_content["tracks"]
    ] == [FIRST, SECOND]
    services.public_factory.return_value.get_watch_playlist.assert_called_once_with(
        videoId=SEED, limit=3, radio=True
    )


@pytest.mark.asyncio
async def test_multi_seed_radio_returns_a_round_robin_mix(services):
    client = services.public_factory.return_value
    client.search.side_effect = [
        [song(SEED, "Seed one")],
        [song(SEED_TWO, "Seed two")],
    ]
    client.get_watch_playlist.side_effect = [
        {"tracks": [song(SEED), song(FIRST)]},
        {"tracks": [song(SEED_TWO), song(SECOND)]},
    ]

    async with Client(yt_mcp_server.mcp, raise_exceptions=True) as mcp_client:
        result = await mcp_client.call_tool(
            "get_multi_seed_radio",
            {"queries": ["Seed one", "Seed two"], "limit": 2},
        )

    assert [seed["videoId"] for seed in result.structured_content["seeds"]] == [
        SEED,
        SEED_TWO,
    ]
    assert [
        track["videoId"] for track in result.structured_content["tracks"]
    ] == [FIRST, SECOND]
    services.youtube_api_factory.assert_not_called()


@pytest.mark.asyncio
async def test_multi_seed_radio_accepts_ten_seeds(services):
    client = services.public_factory.return_value
    seed_ids = [f"seed{index:07d}" for index in range(10)]
    track_ids = [f"track{index:06d}" for index in range(10)]
    queries = [f"Seed {index}" for index in range(10)]
    client.search.side_effect = [
        [song(seed_id, query)]
        for seed_id, query in zip(seed_ids, queries)
    ]
    client.get_watch_playlist.side_effect = [
        {"tracks": [song(seed_id), song(track_id)]}
        for seed_id, track_id in zip(seed_ids, track_ids)
    ]

    async with Client(yt_mcp_server.mcp, raise_exceptions=True) as mcp_client:
        result = await mcp_client.call_tool(
            "get_multi_seed_radio", {"queries": queries, "limit": 10}
        )

    assert [seed["videoId"] for seed in result.structured_content["seeds"]] == seed_ids
    assert [track["videoId"] for track in result.structured_content["tracks"]] == track_ids
    assert client.get_watch_playlist.call_count == 10


@pytest.mark.asyncio
async def test_playlist_tool_uses_hidden_auth_path_and_creates_private_playlist(
    services, monkeypatch, tmp_path: Path
):
    auth_file = tmp_path / "oauth.json"
    auth_file.write_text("{}", encoding="utf-8")
    auth_file.chmod(0o600)
    monkeypatch.setenv("YTMUSIC_AUTH_FILE", str(auth_file))

    async with Client(yt_mcp_server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "create_private_radio_playlist",
            {
                "title": "Italiaanse zomeravond",
                "query": "Alan Sorrenti",
                "description": "Warme Italiaanse avondmuziek",
                "limit": 2,
            },
        )

    assert result.structured_content["playlistId"] == "PLcreated123"
    services.youtube_api_factory.return_value.create_private_playlist.assert_called_once_with(
        "Italiaanse zomeravond",
        "Warme Italiaanse avondmuziek",
        [FIRST, SECOND],
    )
    assert services.youtube_api_factory.call_args.args[0] == auth_file


@pytest.mark.asyncio
async def test_multi_seed_playlist_tool_creates_one_private_playlist(
    services, monkeypatch, tmp_path: Path
):
    client = services.public_factory.return_value
    client.search.side_effect = [
        [song(SEED, "Seed one")],
        [song(SEED_TWO, "Seed two")],
    ]
    client.get_watch_playlist.side_effect = [
        {"tracks": [song(SEED), song(FIRST)]},
        {"tracks": [song(SEED_TWO), song(SECOND)]},
    ]
    auth_file = tmp_path / "oauth.json"
    auth_file.write_text("{}", encoding="utf-8")
    auth_file.chmod(0o600)
    monkeypatch.setenv("YTMUSIC_AUTH_FILE", str(auth_file))

    async with Client(yt_mcp_server.mcp, raise_exceptions=True) as mcp_client:
        result = await mcp_client.call_tool(
            "create_private_multi_seed_radio_playlist",
            {
                "title": "S&S: Italiaans",
                "queries": ["Seed one", "Seed two"],
                "description": "Warme Italiaanse radiomix",
                "limit": 2,
            },
        )

    assert [seed["videoId"] for seed in result.structured_content["seeds"]] == [
        SEED,
        SEED_TWO,
    ]
    assert result.structured_content["privacyStatus"] == "PRIVATE"
    assert result.structured_content["trackCount"] == 2
    services.youtube_api_factory.return_value.create_private_playlist.assert_called_once_with(
        "S&S: Italiaans",
        "Warme Italiaanse radiomix",
        [FIRST, SECOND],
    )
    assert services.youtube_api_factory.call_args.args[0] == auth_file


@pytest.mark.asyncio
async def test_resume_playlist_tool_uses_existing_manifest(
    services, monkeypatch, tmp_path: Path
):
    auth_file = tmp_path / "oauth.json"
    auth_file.write_text("{}", encoding="utf-8")
    auth_file.chmod(0o600)
    monkeypatch.setenv("YTMUSIC_AUTH_FILE", str(auth_file))

    async with Client(yt_mcp_server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "resume_private_playlist", {"playlist_id": "PLcreated123"}
        )

    assert result.structured_content["trackCount"] == 100
    assert result.structured_content["addedTrackCount"] == 77
    services.youtube_api_factory.return_value.resume_private_playlist.assert_called_once_with(
        "PLcreated123"
    )
    services.public_factory.assert_not_called()


@pytest.mark.asyncio
async def test_multi_seed_playlist_tool_rejects_one_seed_before_auth_or_network(
    services,
):
    async with Client(yt_mcp_server.mcp) as client:
        result = await client.call_tool(
            "create_private_multi_seed_radio_playlist",
            {"title": "Mix", "queries": ["only one seed"], "limit": 50},
        )

    assert result.is_error is True
    services.public_factory.assert_not_called()
    services.youtube_api_factory.assert_not_called()


@pytest.mark.asyncio
async def test_tool_rejects_out_of_range_limit_before_network(services):
    async with Client(yt_mcp_server.mcp) as client:
        result = await client.call_tool(
            "get_song_radio", {"query": "Alan Sorrenti", "limit": 101}
        )

    assert result.is_error is True
    assert "between 1 and 100" in result.content[0].text
    services.public_factory.assert_not_called()


@pytest.mark.parametrize(
    "queries",
    [
        ["only one"],
        [
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
        ],
        ["one", " "],
        ["same", "SAME"],
    ],
    ids=["one-seed", "eleven-seeds", "blank-seed", "duplicate-seed"],
)
@pytest.mark.asyncio
async def test_multi_seed_tool_rejects_bad_seed_lists_before_network(
    services, queries
):
    async with Client(yt_mcp_server.mcp) as client:
        result = await client.call_tool(
            "get_multi_seed_radio", {"queries": queries, "limit": 50}
        )

    assert result.is_error is True
    services.public_factory.assert_not_called()
