import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mcp import Client

import yt_mcp_server


SEED = "seed0000001"
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


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.public_factory = patch("yt_mcp_server.YTMusic").start()
        self.youtube_api_factory = patch("yt_mcp.YouTubeDataAPI").start()
        patch("yt_mcp.OAuthCredentials").start()
        self.environment = patch.dict(
            "yt_mcp.os.environ",
            {
                "YTMUSIC_OAUTH_CLIENT_ID": "test-client-id",
                "YTMUSIC_OAUTH_CLIENT_SECRET": "test-client-secret",
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(patch.stopall)

        client = self.public_factory.return_value
        client.search.return_value = [song(SEED, "Figli delle stelle")]
        client.get_watch_playlist.return_value = {
            "tracks": [song(SEED), song(FIRST), song(SECOND)]
        }
        self.youtube_api_factory.return_value.create_private_playlist.return_value = (
            "PLcreated123"
        )

    async def test_tools_have_structured_results_and_write_annotations(self):
        async with Client(yt_mcp_server.mcp, raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            result = await client.call_tool(
                "search_songs", {"query": "Alan Sorrenti", "limit": 1}
            )

        self.assertEqual(
            set(tools),
            {"search_songs", "get_song_radio", "create_private_radio_playlist"},
        )
        self.assertTrue(tools["search_songs"].annotations.read_only_hint)
        write_annotations = tools["create_private_radio_playlist"].annotations
        self.assertFalse(write_annotations.read_only_hint)
        self.assertFalse(write_annotations.destructive_hint)
        self.assertFalse(write_annotations.idempotent_hint)
        track_schema = tools["search_songs"].output_schema["$defs"]["TrackResult"]
        self.assertEqual(
            set(track_schema["properties"]),
            {
                "videoId",
                "title",
                "artists",
                "album",
                "duration",
                "duration_seconds",
                "url",
            },
        )
        track = result.structured_content["tracks"][0]
        self.assertEqual(track["videoId"], SEED)
        self.assertEqual(track["album"], "An album")
        self.assertEqual(track["duration_seconds"], 192)

    async def test_radio_tool_uses_fresh_youtube_music_radio(self):
        async with Client(yt_mcp_server.mcp, raise_exceptions=True) as client:
            result = await client.call_tool(
                "get_song_radio", {"query": "Alan Sorrenti", "limit": 2}
            )

        self.assertEqual(result.structured_content["returned"], 2)
        self.assertEqual(
            [track["videoId"] for track in result.structured_content["tracks"]],
            [FIRST, SECOND],
        )
        self.public_factory.return_value.get_watch_playlist.assert_called_once_with(
            videoId=SEED, limit=3, radio=True
        )

    async def test_playlist_tool_uses_hidden_auth_path_and_creates_private_playlist(self):
        with TemporaryDirectory() as directory:
            auth_file = Path(directory) / "oauth.json"
            auth_file.write_text("{}")
            auth_file.chmod(0o600)
            with patch.dict(
                "yt_mcp_server.os.environ", {"YTMUSIC_AUTH_FILE": str(auth_file)}
            ):
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

        self.assertEqual(result.structured_content["playlistId"], "PLcreated123")
        self.youtube_api_factory.return_value.create_private_playlist.assert_called_once_with(
            "Italiaanse zomeravond",
            "Warme Italiaanse avondmuziek",
            [FIRST, SECOND],
        )
        self.assertEqual(self.youtube_api_factory.call_args.args[0], auth_file)

    async def test_tool_rejects_out_of_range_limit_before_network(self):
        async with Client(yt_mcp_server.mcp) as client:
            result = await client.call_tool(
                "get_song_radio", {"query": "Alan Sorrenti", "limit": 101}
            )

        self.assertTrue(result.is_error)
        self.assertIn("between 1 and 100", result.content[0].text)
        self.public_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
