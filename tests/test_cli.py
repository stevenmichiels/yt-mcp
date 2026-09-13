import io
import json
import os
import stat
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from requests.exceptions import Timeout

from yt_mcp import RecommendationError, YouTubeDataAPI, main


SEED = "seed0000001"
SEED_TWO = "seed0000002"
SEED_THREE = "seed0000003"
FIRST = "track000001"
SECOND = "track000002"
THIRD = "track000003"
FOURTH = "track000004"
FIFTH = "track000005"
SIXTH = "track000006"


def song(identifier, title="A song", **extra):
    return {
        "videoId": identifier,
        "title": title,
        "artists": [{"name": "An artist"}],
        "duration": "3:12",
        **extra,
    }


def private_file(path, contents="{}"):
    path.write_text(contents)
    path.chmod(0o600)
    return path


class CliTests(unittest.TestCase):
    def setUp(self):
        self.factory = patch("yt_mcp.YTMusic").start()
        self.youtube_api_factory = patch("yt_mcp.YouTubeDataAPI").start()
        self.oauth_factory = patch("yt_mcp.OAuthCredentials").start()
        self.setup_oauth = patch("yt_mcp.setup_oauth").start()
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
        self.oauth_factory.return_value.client_id = "test-client-id"
        self.oauth_factory.return_value.client_secret = "test-client-secret"
        self.setup_oauth.side_effect = lambda *args, **kwargs: Path(
            kwargs["filepath"]
        ).write_text("{}")
        self.client = self.factory.return_value
        self.client.search.return_value = [song(SEED, "Figli delle stelle")]
        self.client.get_watch_playlist.return_value = {
            "tracks": [song(SEED), song(FIRST), song(SECOND), song(THIRD)]
        }
        self.youtube_api = self.youtube_api_factory.return_value
        self.youtube_api.create_private_playlist.return_value = "PLcreated123"

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = main(list(args))
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_search_returns_song_ids_and_links_with_a_hard_cap(self):
        self.client.search.return_value = [song(FIRST), song(SECOND), song(THIRD)]
        code, output, error = self.run_cli("search", "  Alan Sorrenti  ", "--limit", "2")
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn(f"https://music.youtube.com/watch?v={FIRST}", output)
        self.assertIn(SECOND, output)
        self.assertNotIn(THIRD, output)
        self.client.search.assert_called_once_with("Alan Sorrenti", filter="songs", limit=2)
        self.client.get_watch_playlist.assert_not_called()

    def test_query_radio_exposes_first_playable_seed_and_uses_radio_mode(self):
        self.client.search.return_value.insert(0, {"title": "Unavailable"})
        code, output, error = self.run_cli("radio", "Alan Sorrenti", "--limit", "2", "--json")
        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        self.assertEqual(result["seed"]["videoId"], SEED)
        self.assertEqual(result["seed"]["title"], "Figli delle stelle")
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["returned"], 2)
        self.assertEqual([track["videoId"] for track in result["tracks"]], [FIRST, SECOND])
        self.client.get_watch_playlist.assert_called_once_with(videoId=SEED, limit=3, radio=True)

    def test_radio_filters_seed_duplicates_and_unavailable_tracks_preserving_order(self):
        self.client.get_watch_playlist.return_value = {
            "tracks": [
                song(SEED), song(FIRST), song(FIRST), None, {},
                song("invalid"), song(THIRD, isAvailable=False), song(SECOND),
            ]
        }
        code, output, error = self.run_cli("radio", "song", "--limit", "2", "--json")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual([t["videoId"] for t in json.loads(output)["tracks"]], [FIRST, SECOND])

    def test_radio_derives_duration_seconds_from_length(self):
        self.client.get_watch_playlist.return_value = {
            "tracks": [song(SEED), song(FIRST, duration=None, length="4:01")]
        }
        code, output, error = self.run_cli("radio", "song", "--limit", "1", "--json")
        self.assertEqual((code, error), (0, ""))
        track = json.loads(output)["tracks"][0]
        self.assertEqual(track["duration"], "4:01")
        self.assertEqual(track["duration_seconds"], 241)

    def test_radio_mix_round_robins_and_removes_all_seeds_and_duplicates(self):
        self.client.search.side_effect = [
            [song(SEED, "Seed one")],
            [song(SEED_TWO, "Seed two")],
            [song(SEED_THREE, "Seed three")],
        ]
        self.client.get_watch_playlist.side_effect = [
            {
                "tracks": [
                    song(SEED),
                    song(FIRST),
                    song(SECOND),
                    song(SEED_TWO),
                    song(FOURTH),
                ]
            },
            {
                "tracks": [
                    song(SEED_TWO),
                    song(THIRD),
                    song(SECOND),
                    song(SEED_THREE),
                    song(FIFTH),
                ]
            },
            {
                "tracks": [
                    song(SEED_THREE),
                    song(SIXTH),
                    song(SEED),
                ]
            },
        ]

        code, output, error = self.run_cli(
            "radio-mix",
            "Seed one",
            "Seed two",
            "Seed three",
            "--limit",
            "5",
            "--json",
        )

        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        self.assertEqual(
            [seed["videoId"] for seed in result["seeds"]],
            [SEED, SEED_TWO, SEED_THREE],
        )
        self.assertEqual(result["requested"], 5)
        self.assertEqual(result["returned"], 5)
        self.assertEqual(
            [track["videoId"] for track in result["tracks"]],
            [FIRST, THIRD, SIXTH, SECOND, FIFTH],
        )
        self.assertEqual(
            self.client.get_watch_playlist.call_count,
            3,
        )
        for radio_call in self.client.get_watch_playlist.call_args_list:
            self.assertEqual(radio_call.kwargs["limit"], 8)
            self.assertIs(radio_call.kwargs["radio"], True)

    def test_radio_mix_shortfall_warns_and_keeps_json_valid(self):
        self.client.search.side_effect = [
            [song(SEED, "Seed one")],
            [song(SEED_TWO, "Seed two")],
        ]
        self.client.get_watch_playlist.side_effect = [
            {"tracks": [song(SEED), song(FIRST)]},
            {"tracks": [song(SEED_TWO), song(FIRST)]},
        ]

        code, output, error = self.run_cli(
            "radio-mix", "Seed one", "Seed two", "--limit", "5", "--json"
        )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["returned"], 1)
        self.assertIn("returned 1 of 5", error)

    def test_radio_mix_duplicate_resolved_seeds_fail_before_radio_requests(self):
        self.client.search.side_effect = [
            [song(SEED, "First query")],
            [song(SEED, "Second query")],
        ]

        code, output, error = self.run_cli(
            "radio-mix", "First query", "Second query", "--json"
        )

        self.assertEqual((code, output), (1, ""))
        self.assertIn("same song", error)
        self.client.get_watch_playlist.assert_not_called()

    def test_radio_mix_identifies_the_seed_with_a_malformed_radio(self):
        self.client.search.side_effect = [
            [song(SEED, "Seed one")],
            [song(SEED_TWO, "Seed two")],
        ]
        self.client.get_watch_playlist.side_effect = [
            {"tracks": [song(SEED), song(FIRST)]},
            {"tracks": "raw malformed response"},
        ]

        code, output, error = self.run_cli(
            "radio-mix", "Seed one", "Seed two", "--json"
        )

        self.assertEqual((code, output), (1, ""))
        self.assertIn("Seed 2", error)
        self.assertIn("unexpected track list", error)
        self.assertNotIn("raw malformed response", error)

    def test_explicit_id_bypasses_search_and_gets_seed_metadata_from_queue(self):
        code, output, error = self.run_cli("radio", "--video-id", SEED, "--limit", "1")
        self.assertEqual((code, error), (0, ""))
        self.client.search.assert_not_called()
        self.assertIn(f"Seed: An artist — A song [{SEED}]", output)
        self.assertNotIn(SECOND, output)

    def test_explicit_id_works_when_seed_is_missing_from_queue(self):
        self.client.get_watch_playlist.return_value = {"tracks": [song(FIRST)]}
        code, output, error = self.run_cli("radio", "--video-id", SEED, "--limit", "1", "--json")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["seed"]["videoId"], SEED)

    def test_shortfall_warns_on_stderr_and_keeps_stdout_valid_json(self):
        self.client.get_watch_playlist.return_value = {"tracks": [song(SEED), song(FIRST)]}
        code, output, error = self.run_cli("radio", "song", "--limit", "30", "--json")
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual((result["requested"], result["returned"]), (30, 1))
        self.assertIn("returned 1 of 30", error)
        self.client.get_watch_playlist.assert_called_once()

    def test_optional_metadata_and_unicode_are_supported(self):
        self.client.search.return_value = [
            {"videoId": FIRST, "title": "Perché", "artists": None, "length": "4:01"}
        ]
        code, output, error = self.run_cli("search", "song", "--json")
        self.assertEqual((code, error), (0, ""))
        track = json.loads(output)["tracks"][0]
        self.assertEqual(
            (
                track["title"],
                track["artists"],
                track["album"],
                track["duration"],
                track["duration_seconds"],
            ),
            ("Perché", [], None, "4:01", 241),
        )

    def test_matching_metadata_uses_album_name_and_upstream_duration_seconds(self):
        self.client.search.return_value = [
            song(
                FIRST,
                "Figli delle stelle",
                album={"name": "Figli delle stelle", "id": "MPREalbum123"},
                duration_seconds=274,
            )
        ]
        code, output, error = self.run_cli("search", "Alan Sorrenti", "--json")
        self.assertEqual((code, error), (0, ""))
        track = json.loads(output)["tracks"][0]
        self.assertEqual(track["album"], "Figli delle stelle")
        self.assertEqual(track["duration_seconds"], 274)

    def test_blank_or_conflicting_inputs_fail_before_client_creation(self):
        cases = [
            ["search", " "], ["radio"], ["radio", " "],
            ["radio", "song", "--video-id", SEED],
            ["radio", "--video-id", "not-an-id"],
            ["radio", "--video-id", "https://music.youtube.com/watch?v=" + SEED],
            ["radio", "song", "--limit", "0"],
            ["radio", "song", "--limit", "-1"],
            ["search", "song", "--limit", "1.5"],
            ["radio-mix", "only one seed"],
            ["radio-mix", "one", "two", "three", "four", "five", "six"],
            ["radio-mix", "one", " "],
            ["radio-mix", "same", "SAME"],
            ["radio-mix", "one", "two", "--limit", "1"],
            ["radio-mix", "one", "two", "--limit", "101"],
        ]
        for args in cases:
            with self.subTest(args=args):
                code, output, error = self.run_cli(*args)
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertIn("error:", error)
        self.factory.assert_not_called()

    def test_no_matches_fails_without_requesting_radio(self):
        self.client.search.return_value = []
        code, output, error = self.run_cli("radio", "no match")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("No playable songs", error)
        self.client.get_watch_playlist.assert_not_called()

    def test_empty_or_seed_only_radio_fails(self):
        for tracks in ([], [song(SEED)], [song(FIRST, isAvailable=False)]):
            with self.subTest(tracks=tracks):
                self.client.get_watch_playlist.return_value = {"tracks": tracks}
                code, output, error = self.run_cli("radio", "song")
                self.assertEqual((code, output), (1, ""))
                self.assertIn("No radio recommendations", error)

    def test_malformed_radio_is_reported(self):
        for response in (None, {}, {"tracks": None}, {"tracks": "unexpected"}):
            with self.subTest(response=response):
                self.client.get_watch_playlist.return_value = response
                code, output, error = self.run_cli("radio", "song")
                self.assertEqual((code, output), (1, ""))
                self.assertIn("unexpected", error)

    def test_network_and_upstream_parser_errors_do_not_dump_raw_details(self):
        for failure in (Timeout("raw request data"), KeyError("raw response data")):
            with self.subTest(failure=failure):
                self.client.get_watch_playlist.side_effect = failure
                code, output, error = self.run_cli("radio", "song", "--json")
                self.assertEqual((code, output), (1, ""))
                self.assertIn(type(failure).__name__, error)
                self.assertNotIn("raw", error)
                self.assertNotIn("Traceback", error)

    def test_anonymous_session_disables_implicit_credentials_and_has_timeout(self):
        self.run_cli("search", "song")
        session = self.factory.call_args.kwargs["requests_session"]
        self.assertFalse(session.trust_env)
        self.assertEqual(session.request.keywords["timeout"], 30)
        self.assertNotIn("auth", self.factory.call_args.kwargs)

    def test_help_does_not_create_client(self):
        code, output, error = self.run_cli("--help")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("radio", output)
        self.factory.assert_not_called()

    def test_playlist_create_uses_authenticated_private_write(self):
        with TemporaryDirectory() as directory:
            auth_file = Path(directory) / "browser.json"
            private_file(auth_file)
            code, output, error = self.run_cli(
                "playlist", "create", "Italiaanse zomeravond", "Alan Sorrenti",
                "--description", "Warme Italiaanse avondmuziek", "--limit", "2",
                "--auth-file", str(auth_file), "--json",
            )
        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        self.assertEqual(result["playlistId"], "PLcreated123")
        self.assertEqual(result["privacyStatus"], "PRIVATE")
        self.assertEqual(result["trackCount"], 2)
        self.assertEqual([track["videoId"] for track in result["tracks"]], [FIRST, SECOND])
        self.factory.assert_called_once()
        self.oauth_factory.assert_called_once_with(
            "test-client-id",
            "test-client-secret",
            session=self.factory.call_args.kwargs["requests_session"],
        )
        self.youtube_api_factory.assert_called_once_with(
            auth_file,
            self.oauth_factory.return_value,
            self.factory.call_args.kwargs["requests_session"],
        )
        self.youtube_api.create_private_playlist.assert_called_once_with(
            "Italiaanse zomeravond",
            "Warme Italiaanse avondmuziek",
            [FIRST, SECOND],
        )

    def test_playlist_missing_auth_fails_before_client_creation(self):
        code, output, error = self.run_cli(
            "playlist", "create", "Italiaanse zomeravond", "Alan Sorrenti",
            "--auth-file", "/definitely/missing/browser.json",
        )
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Authentication file not found", error)
        self.factory.assert_not_called()

    def test_invalid_playlist_titles_fail_before_client_creation(self):
        with TemporaryDirectory() as directory:
            auth_file = Path(directory) / "browser.json"
            private_file(auth_file)
            for title in (" ", "bad<title", "bad>title"):
                with self.subTest(title=title):
                    code, output, error = self.run_cli(
                        "playlist", "create", title, "song", "--auth-file", str(auth_file)
                    )
                    self.assertEqual((code, output), (2, ""))
                    self.assertIn("error:", error)
        self.factory.assert_not_called()

    def test_playlist_shortfall_warns_but_creates_available_tracks(self):
        self.client.get_watch_playlist.return_value = {"tracks": [song(SEED), song(FIRST)]}
        with TemporaryDirectory() as directory:
            auth_file = Path(directory) / "browser.json"
            private_file(auth_file)
            code, output, error = self.run_cli(
                "playlist", "create", "A playlist", "song", "--limit", "30",
                "--auth-file", str(auth_file), "--json",
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["trackCount"], 1)
        self.assertIn("created the playlist with 1 of 30", error)
        self.youtube_api.create_private_playlist.assert_called_once()

    def test_playlist_unconfirmed_write_fails_without_raw_response(self):
        self.youtube_api.create_private_playlist.return_value = {"error": "raw response"}
        with TemporaryDirectory() as directory:
            auth_file = Path(directory) / "browser.json"
            private_file(auth_file)
            code, output, error = self.run_cli(
                "playlist", "create", "A playlist", "song",
                "--auth-file", str(auth_file),
            )
        self.assertEqual((code, output), (1, ""))
        self.assertIn("did not confirm playlist creation", error)
        self.assertNotIn("raw response", error)

    def test_oauth_setup_saves_owner_only_token_without_creating_api_client(self):
        with TemporaryDirectory() as directory:
            auth_file = Path(directory) / "oauth.json"
            code, output, error = self.run_cli(
                "auth", "oauth", "--auth-file", str(auth_file)
            )
            permissions = stat.S_IMODE(auth_file.stat().st_mode)
        self.assertEqual((code, error), (0, ""))
        self.assertIn("OAuth token saved", output)
        self.assertEqual(permissions, 0o600)
        self.factory.assert_not_called()
        self.oauth_factory.assert_called_once()
        self.setup_oauth.assert_called_once()
        call = self.setup_oauth.call_args
        self.assertEqual(call.args, ("test-client-id", "test-client-secret"))
        self.assertTrue(call.kwargs["open_browser"])
        self.assertNotEqual(call.kwargs["filepath"], str(auth_file))

    def test_oauth_setup_refuses_to_overwrite_an_existing_token(self):
        with TemporaryDirectory() as directory:
            auth_file = Path(directory) / "oauth.json"
            auth_file.write_text("existing token")
            code, output, error = self.run_cli(
                "auth", "oauth", "--auth-file", str(auth_file)
            )
            contents = auth_file.read_text()
        self.assertEqual((code, output), (1, ""))
        self.assertIn("already exists", error)
        self.assertEqual(contents, "existing token")
        self.setup_oauth.assert_not_called()

    def test_playlist_requires_oauth_client_environment(self):
        with TemporaryDirectory() as directory:
            auth_file = Path(directory) / "oauth.json"
            private_file(auth_file)
            with patch.dict(
                os.environ,
                {"YTMUSIC_OAUTH_CLIENT_ID": "", "YTMUSIC_OAUTH_CLIENT_SECRET": ""},
            ):
                code, output, error = self.run_cli(
                    "playlist", "create", "A playlist", "song",
                    "--auth-file", str(auth_file),
                )
        self.assertEqual((code, output), (1, ""))
        self.assertIn("YTMUSIC_OAUTH_CLIENT_ID", error)
        self.factory.assert_not_called()

    def test_playlist_loads_oauth_client_from_local_json_path(self):
        with TemporaryDirectory() as directory:
            auth_file = Path(directory) / "oauth.json"
            client_file = Path(directory) / "oauth-client.json"
            private_file(auth_file)
            private_file(
                client_file,
                json.dumps(
                    {
                        "installed": {
                            "client_id": "file-client-id",
                            "client_secret": "file-client-secret",
                        }
                    }
                ),
            )
            with patch.dict(
                os.environ,
                {"YTMUSIC_OAUTH_CLIENT_FILE": str(client_file)},
                clear=True,
            ):
                code, output, error = self.run_cli(
                    "playlist", "create", "A playlist", "song",
                    "--auth-file", str(auth_file), "--limit", "1",
                )
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Created private playlist", output)
        self.oauth_factory.assert_called_once()
        self.assertEqual(
            self.oauth_factory.call_args.args,
            ("file-client-id", "file-client-secret"),
        )

    def test_playlist_uses_auth_file_from_environment_by_default(self):
        with TemporaryDirectory() as directory:
            auth_file = Path(directory) / "oauth.json"
            private_file(auth_file)
            with patch.dict(os.environ, {"YTMUSIC_AUTH_FILE": str(auth_file)}):
                code, output, error = self.run_cli(
                    "playlist", "create", "A playlist", "song", "--limit", "1"
                )
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Created private playlist", output)
        self.youtube_api_factory.assert_called_once_with(
            auth_file,
            self.oauth_factory.return_value,
            self.factory.call_args.kwargs["requests_session"],
        )

    @unittest.skipUnless(os.name == "posix", "POSIX permission check")
    def test_playlist_rejects_group_readable_oauth_token(self):
        with TemporaryDirectory() as directory:
            auth_file = Path(directory) / "oauth.json"
            auth_file.write_text("{}")
            auth_file.chmod(0o640)
            code, output, error = self.run_cli(
                "playlist", "create", "A playlist", "song",
                "--auth-file", str(auth_file),
            )
        self.assertEqual((code, output), (1, ""))
        self.assertIn("permissions are too broad", error)
        self.assertIn("chmod 600", error)
        self.factory.assert_not_called()


class YouTubeDataApiTests(unittest.TestCase):
    def response(self, status, payload):
        response = MagicMock()
        response.status_code = status
        response.json.return_value = payload
        return response

    def test_creates_private_playlist_and_inserts_each_video(self):
        with TemporaryDirectory() as directory:
            auth_file = private_file(
                Path(directory) / "oauth.json",
                json.dumps(
                    {
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                        "expires_at": time.time() + 3600,
                    }
                ),
            )
            session = MagicMock()
            session.post.side_effect = [
                self.response(200, {"id": "PLcreated123"}),
                self.response(200, {"id": "item1"}),
                self.response(200, {"id": "item2"}),
            ]
            api = YouTubeDataAPI(auth_file, MagicMock(), session)

            playlist_id = api.create_private_playlist(
                "Italiaanse zomeravond", "Warme Italiaanse avondmuziek", [FIRST, SECOND]
            )

        self.assertEqual(playlist_id, "PLcreated123")
        self.assertEqual(session.post.call_count, 3)
        create_call, first_item, second_item = session.post.call_args_list
        self.assertTrue(create_call.args[0].endswith("/playlists"))
        self.assertEqual(create_call.kwargs["params"], {"part": "snippet,status"})
        self.assertEqual(
            create_call.kwargs["json"]["status"]["privacyStatus"], "private"
        )
        self.assertEqual(
            [
                first_item.kwargs["json"]["snippet"]["resourceId"]["videoId"],
                second_item.kwargs["json"]["snippet"]["resourceId"]["videoId"],
            ],
            [FIRST, SECOND],
        )

    def test_refreshes_expired_token_and_keeps_file_private(self):
        with TemporaryDirectory() as directory:
            auth_file = private_file(
                Path(directory) / "oauth.json",
                json.dumps(
                    {
                        "access_token": "expired-token",
                        "refresh_token": "refresh-token",
                        "expires_at": 0,
                    }
                ),
            )
            credentials = MagicMock()
            credentials.refresh_token.return_value = {
                "access_token": "refreshed-token",
                "expires_in": 3600,
            }
            api = YouTubeDataAPI(auth_file, credentials, MagicMock())

            token = api._access_token()
            saved = json.loads(auth_file.read_text())
            permissions = stat.S_IMODE(auth_file.stat().st_mode)

        self.assertEqual(token, "refreshed-token")
        self.assertEqual(saved["access_token"], "refreshed-token")
        self.assertEqual(saved["refresh_token"], "refresh-token")
        self.assertEqual(permissions, 0o600)

    def test_api_error_exposes_reason_but_not_raw_message(self):
        with TemporaryDirectory() as directory:
            auth_file = private_file(
                Path(directory) / "oauth.json",
                json.dumps(
                    {
                        "access_token": "access-token",
                        "expires_at": time.time() + 3600,
                    }
                ),
            )
            session = MagicMock()
            session.post.return_value = self.response(
                403,
                {
                    "error": {
                        "message": "raw response details",
                        "errors": [{"reason": "quotaExceeded"}],
                    }
                },
            )
            api = YouTubeDataAPI(auth_file, MagicMock(), session)

            with self.assertRaises(RecommendationError) as raised:
                api.create_private_playlist("A playlist", "Description", [FIRST])

        self.assertIn("HTTP 403, reason=quotaExceeded", str(raised.exception))
        self.assertNotIn("raw response details", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
