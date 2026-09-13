import io
import json
import os
import stat
import time
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
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


@pytest.fixture
def cli():
    with ExitStack() as stack:
        factory = stack.enter_context(patch("yt_mcp.YTMusic"))
        youtube_api_factory = stack.enter_context(patch("yt_mcp.YouTubeDataAPI"))
        oauth_factory = stack.enter_context(patch("yt_mcp.OAuthCredentials"))
        setup_oauth = stack.enter_context(patch("yt_mcp.setup_oauth"))
        stack.enter_context(
            patch.dict(
                "yt_mcp.os.environ",
                {
                    "YTMUSIC_OAUTH_CLIENT_ID": "test-client-id",
                    "YTMUSIC_OAUTH_CLIENT_SECRET": "test-client-secret",
                },
                clear=True,
            )
        )
        oauth_factory.return_value.client_id = "test-client-id"
        oauth_factory.return_value.client_secret = "test-client-secret"
        setup_oauth.side_effect = lambda *args, **kwargs: Path(
            kwargs["filepath"]
        ).write_text("{}")
        client = factory.return_value
        client.search.return_value = [song(SEED, "Figli delle stelle")]
        client.get_watch_playlist.return_value = {
            "tracks": [song(SEED), song(FIRST), song(SECOND), song(THIRD)]
        }
        youtube_api = youtube_api_factory.return_value
        youtube_api.create_private_playlist.return_value = "PLcreated123"

        def run(*args):
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    code = main(list(args))
                except SystemExit as error:
                    code = error.code
            return code, stdout.getvalue(), stderr.getvalue()

        yield SimpleNamespace(
            factory=factory,
            youtube_api_factory=youtube_api_factory,
            oauth_factory=oauth_factory,
            setup_oauth=setup_oauth,
            client=client,
            youtube_api=youtube_api,
            run=run,
        )


class TestCli:
    def test_search_returns_song_ids_and_links_with_a_hard_cap(self, cli):
        cli.client.search.return_value = [song(FIRST), song(SECOND), song(THIRD)]
        code, output, error = cli.run("search", "  Alan Sorrenti  ", "--limit", "2")
        assert code == 0
        assert error == ""
        assert f"https://music.youtube.com/watch?v={FIRST}" in output
        assert SECOND in output
        assert THIRD not in output
        cli.client.search.assert_called_once_with("Alan Sorrenti", filter="songs", limit=2)
        cli.client.get_watch_playlist.assert_not_called()

    def test_query_radio_exposes_first_playable_seed_and_uses_radio_mode(self, cli):
        cli.client.search.return_value.insert(0, {"title": "Unavailable"})
        code, output, error = cli.run("radio", "Alan Sorrenti", "--limit", "2", "--json")
        assert (code, error) == (0, "")
        result = json.loads(output)
        assert result["seed"]["videoId"] == SEED
        assert result["seed"]["title"] == "Figli delle stelle"
        assert result["requested"] == 2
        assert result["returned"] == 2
        assert [track["videoId"] for track in result["tracks"]] == [FIRST, SECOND]
        cli.client.get_watch_playlist.assert_called_once_with(videoId=SEED, limit=3, radio=True)

    def test_radio_filters_seed_duplicates_and_unavailable_tracks_preserving_order(self, cli):
        cli.client.get_watch_playlist.return_value = {
            "tracks": [
                song(SEED), song(FIRST), song(FIRST), None, {},
                song("invalid"), song(THIRD, isAvailable=False), song(SECOND),
            ]
        }
        code, output, error = cli.run("radio", "song", "--limit", "2", "--json")
        assert (code, error) == (0, "")
        assert [t["videoId"] for t in json.loads(output)["tracks"]] == [FIRST, SECOND]

    def test_radio_derives_duration_seconds_from_length(self, cli):
        cli.client.get_watch_playlist.return_value = {
            "tracks": [song(SEED), song(FIRST, duration=None, length="4:01")]
        }
        code, output, error = cli.run("radio", "song", "--limit", "1", "--json")
        assert (code, error) == (0, "")
        track = json.loads(output)["tracks"][0]
        assert track["duration"] == "4:01"
        assert track["duration_seconds"] == 241

    def test_radio_mix_round_robins_and_removes_all_seeds_and_duplicates(self, cli):
        cli.client.search.side_effect = [
            [song(SEED, "Seed one")],
            [song(SEED_TWO, "Seed two")],
            [song(SEED_THREE, "Seed three")],
        ]
        cli.client.get_watch_playlist.side_effect = [
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

        code, output, error = cli.run(
            "radio-mix",
            "Seed one",
            "Seed two",
            "Seed three",
            "--limit",
            "5",
            "--json",
        )

        assert (code, error) == (0, "")
        result = json.loads(output)
        assert [seed["videoId"] for seed in result["seeds"]] == [
            SEED,
            SEED_TWO,
            SEED_THREE,
        ]
        assert result["requested"] == 5
        assert result["returned"] == 5
        assert [track["videoId"] for track in result["tracks"]] == [
            FIRST,
            THIRD,
            SIXTH,
            SECOND,
            FIFTH,
        ]
        assert cli.client.get_watch_playlist.call_count == 3
        for radio_call in cli.client.get_watch_playlist.call_args_list:
            assert radio_call.kwargs["limit"] == 8
            assert radio_call.kwargs["radio"] is True

    def test_radio_mix_shortfall_warns_and_keeps_json_valid(self, cli):
        cli.client.search.side_effect = [
            [song(SEED, "Seed one")],
            [song(SEED_TWO, "Seed two")],
        ]
        cli.client.get_watch_playlist.side_effect = [
            {"tracks": [song(SEED), song(FIRST)]},
            {"tracks": [song(SEED_TWO), song(FIRST)]},
        ]

        code, output, error = cli.run(
            "radio-mix", "Seed one", "Seed two", "--limit", "5", "--json"
        )

        assert code == 0
        assert json.loads(output)["returned"] == 1
        assert "returned 1 of 5" in error

    def test_radio_mix_duplicate_resolved_seeds_fail_before_radio_requests(self, cli):
        cli.client.search.side_effect = [
            [song(SEED, "First query")],
            [song(SEED, "Second query")],
        ]

        code, output, error = cli.run(
            "radio-mix", "First query", "Second query", "--json"
        )

        assert (code, output) == (1, "")
        assert "same song" in error
        cli.client.get_watch_playlist.assert_not_called()

    def test_radio_mix_identifies_the_seed_with_a_malformed_radio(self, cli):
        cli.client.search.side_effect = [
            [song(SEED, "Seed one")],
            [song(SEED_TWO, "Seed two")],
        ]
        cli.client.get_watch_playlist.side_effect = [
            {"tracks": [song(SEED), song(FIRST)]},
            {"tracks": "raw malformed response"},
        ]

        code, output, error = cli.run(
            "radio-mix", "Seed one", "Seed two", "--json"
        )

        assert (code, output) == (1, "")
        assert "Seed 2" in error
        assert "unexpected track list" in error
        assert "raw malformed response" not in error

    def test_explicit_id_bypasses_search_and_gets_seed_metadata_from_queue(self, cli):
        code, output, error = cli.run("radio", "--video-id", SEED, "--limit", "1")
        assert (code, error) == (0, "")
        cli.client.search.assert_not_called()
        assert f"Seed: An artist — A song [{SEED}]" in output
        assert SECOND not in output

    def test_explicit_id_works_when_seed_is_missing_from_queue(self, cli):
        cli.client.get_watch_playlist.return_value = {"tracks": [song(FIRST)]}
        code, output, error = cli.run("radio", "--video-id", SEED, "--limit", "1", "--json")
        assert (code, error) == (0, "")
        assert json.loads(output)["seed"]["videoId"] == SEED

    def test_shortfall_warns_on_stderr_and_keeps_stdout_valid_json(self, cli):
        cli.client.get_watch_playlist.return_value = {"tracks": [song(SEED), song(FIRST)]}
        code, output, error = cli.run("radio", "song", "--limit", "30", "--json")
        assert code == 0
        result = json.loads(output)
        assert (result["requested"], result["returned"]) == (30, 1)
        assert "returned 1 of 30" in error
        cli.client.get_watch_playlist.assert_called_once()

    def test_optional_metadata_and_unicode_are_supported(self, cli):
        cli.client.search.return_value = [
            {"videoId": FIRST, "title": "Perché", "artists": None, "length": "4:01"}
        ]
        code, output, error = cli.run("search", "song", "--json")
        assert (code, error) == (0, "")
        track = json.loads(output)["tracks"][0]
        assert (
            track["title"],
            track["artists"],
            track["album"],
            track["duration"],
            track["duration_seconds"],
        ) == ("Perché", [], None, "4:01", 241)

    def test_matching_metadata_uses_album_name_and_upstream_duration_seconds(self, cli):
        cli.client.search.return_value = [
            song(
                FIRST,
                "Figli delle stelle",
                album={"name": "Figli delle stelle", "id": "MPREalbum123"},
                duration_seconds=274,
            )
        ]
        code, output, error = cli.run("search", "Alan Sorrenti", "--json")
        assert (code, error) == (0, "")
        track = json.loads(output)["tracks"][0]
        assert track["album"] == "Figli delle stelle"
        assert track["duration_seconds"] == 274

    @pytest.mark.parametrize(
        "args",
        [
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
        ],
    )
    def test_blank_or_conflicting_inputs_fail_before_client_creation(self, cli, args):
        code, output, error = cli.run(*args)
        assert code == 2
        assert output == ""
        assert "error:" in error
        cli.factory.assert_not_called()

    def test_no_matches_fails_without_requesting_radio(self, cli):
        cli.client.search.return_value = []
        code, output, error = cli.run("radio", "no match")
        assert (code, output) == (1, "")
        assert "No playable songs" in error
        cli.client.get_watch_playlist.assert_not_called()

    @pytest.mark.parametrize(
        "tracks",
        [[], [song(SEED)], [song(FIRST, isAvailable=False)]],
    )
    def test_empty_or_seed_only_radio_fails(self, cli, tracks):
        cli.client.get_watch_playlist.return_value = {"tracks": tracks}
        code, output, error = cli.run("radio", "song")
        assert (code, output) == (1, "")
        assert "No radio recommendations" in error

    @pytest.mark.parametrize(
        "response",
        [None, {}, {"tracks": None}, {"tracks": "unexpected"}],
    )
    def test_malformed_radio_is_reported(self, cli, response):
        cli.client.get_watch_playlist.return_value = response
        code, output, error = cli.run("radio", "song")
        assert (code, output) == (1, "")
        assert "unexpected" in error

    @pytest.mark.parametrize(
        "failure",
        [Timeout("raw request data"), KeyError("raw response data")],
    )
    def test_network_and_upstream_parser_errors_do_not_dump_raw_details(self, cli, failure):
        cli.client.get_watch_playlist.side_effect = failure
        code, output, error = cli.run("radio", "song", "--json")
        assert (code, output) == (1, "")
        assert type(failure).__name__ in error
        assert "raw" not in error
        assert "Traceback" not in error

    def test_anonymous_session_disables_implicit_credentials_and_has_timeout(self, cli):
        cli.run("search", "song")
        session = cli.factory.call_args.kwargs["requests_session"]
        assert session.trust_env is False
        assert session.request.keywords["timeout"] == 30
        assert "auth" not in cli.factory.call_args.kwargs

    def test_help_does_not_create_client(self, cli):
        code, output, error = cli.run("--help")
        assert (code, error) == (0, "")
        assert "radio" in output
        cli.factory.assert_not_called()

    def test_playlist_create_uses_authenticated_private_write(self, cli, tmp_path):
        auth_file = private_file(tmp_path / "browser.json")
        code, output, error = cli.run(
            "playlist", "create", "Italiaanse zomeravond", "Alan Sorrenti",
            "--description", "Warme Italiaanse avondmuziek", "--limit", "2",
            "--auth-file", str(auth_file), "--json",
        )
        assert (code, error) == (0, "")
        result = json.loads(output)
        assert result["playlistId"] == "PLcreated123"
        assert result["privacyStatus"] == "PRIVATE"
        assert result["trackCount"] == 2
        assert [track["videoId"] for track in result["tracks"]] == [FIRST, SECOND]
        cli.factory.assert_called_once()
        cli.oauth_factory.assert_called_once_with(
            "test-client-id",
            "test-client-secret",
            session=cli.factory.call_args.kwargs["requests_session"],
        )
        cli.youtube_api_factory.assert_called_once_with(
            auth_file,
            cli.oauth_factory.return_value,
            cli.factory.call_args.kwargs["requests_session"],
        )
        cli.youtube_api.create_private_playlist.assert_called_once_with(
            "Italiaanse zomeravond",
            "Warme Italiaanse avondmuziek",
            [FIRST, SECOND],
        )

    def test_playlist_missing_auth_fails_before_client_creation(self, cli):
        code, output, error = cli.run(
            "playlist", "create", "Italiaanse zomeravond", "Alan Sorrenti",
            "--auth-file", "/definitely/missing/browser.json",
        )
        assert (code, output) == (1, "")
        assert "Authentication file not found" in error
        cli.factory.assert_not_called()

    @pytest.mark.parametrize("title", [" ", "bad<title", "bad>title"])
    def test_invalid_playlist_titles_fail_before_client_creation(
        self, cli, tmp_path, title
    ):
        auth_file = private_file(tmp_path / "browser.json")
        code, output, error = cli.run(
            "playlist", "create", title, "song", "--auth-file", str(auth_file)
        )
        assert (code, output) == (2, "")
        assert "error:" in error
        cli.factory.assert_not_called()

    def test_playlist_shortfall_warns_but_creates_available_tracks(self, cli, tmp_path):
        cli.client.get_watch_playlist.return_value = {"tracks": [song(SEED), song(FIRST)]}
        auth_file = private_file(tmp_path / "browser.json")
        code, output, error = cli.run(
            "playlist", "create", "A playlist", "song", "--limit", "30",
            "--auth-file", str(auth_file), "--json",
        )
        assert code == 0
        assert json.loads(output)["trackCount"] == 1
        assert "created the playlist with 1 of 30" in error
        cli.youtube_api.create_private_playlist.assert_called_once()

    def test_playlist_unconfirmed_write_fails_without_raw_response(self, cli, tmp_path):
        cli.youtube_api.create_private_playlist.return_value = {"error": "raw response"}
        auth_file = private_file(tmp_path / "browser.json")
        code, output, error = cli.run(
            "playlist", "create", "A playlist", "song",
            "--auth-file", str(auth_file),
        )
        assert (code, output) == (1, "")
        assert "did not confirm playlist creation" in error
        assert "raw response" not in error

    def test_oauth_setup_saves_owner_only_token_without_creating_api_client(
        self, cli, tmp_path
    ):
        auth_file = tmp_path / "oauth.json"
        code, output, error = cli.run(
            "auth", "oauth", "--auth-file", str(auth_file)
        )
        permissions = stat.S_IMODE(auth_file.stat().st_mode)
        assert (code, error) == (0, "")
        assert "OAuth token saved" in output
        assert permissions == 0o600
        cli.factory.assert_not_called()
        cli.oauth_factory.assert_called_once()
        cli.setup_oauth.assert_called_once()
        call = cli.setup_oauth.call_args
        assert call.args == ("test-client-id", "test-client-secret")
        assert call.kwargs["open_browser"] is True
        assert call.kwargs["filepath"] != str(auth_file)

    def test_oauth_setup_refuses_to_overwrite_an_existing_token(self, cli, tmp_path):
        auth_file = tmp_path / "oauth.json"
        auth_file.write_text("existing token")
        code, output, error = cli.run(
            "auth", "oauth", "--auth-file", str(auth_file)
        )
        contents = auth_file.read_text()
        assert (code, output) == (1, "")
        assert "already exists" in error
        assert contents == "existing token"
        cli.setup_oauth.assert_not_called()

    def test_playlist_requires_oauth_client_environment(self, cli, tmp_path):
        auth_file = private_file(tmp_path / "oauth.json")
        with patch.dict(
            os.environ,
            {"YTMUSIC_OAUTH_CLIENT_ID": "", "YTMUSIC_OAUTH_CLIENT_SECRET": ""},
        ):
            code, output, error = cli.run(
                "playlist", "create", "A playlist", "song",
                "--auth-file", str(auth_file),
            )
        assert (code, output) == (1, "")
        assert "YTMUSIC_OAUTH_CLIENT_ID" in error
        cli.factory.assert_not_called()

    def test_playlist_loads_oauth_client_from_local_json_path(self, cli, tmp_path):
        auth_file = private_file(tmp_path / "oauth.json")
        client_file = private_file(
            tmp_path / "oauth-client.json",
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
            code, output, error = cli.run(
                "playlist", "create", "A playlist", "song",
                "--auth-file", str(auth_file), "--limit", "1",
            )
        assert (code, error) == (0, "")
        assert "Created private playlist" in output
        cli.oauth_factory.assert_called_once()
        assert cli.oauth_factory.call_args.args == (
            "file-client-id",
            "file-client-secret",
        )

    def test_playlist_uses_auth_file_from_environment_by_default(self, cli, tmp_path):
        auth_file = private_file(tmp_path / "oauth.json")
        with patch.dict(os.environ, {"YTMUSIC_AUTH_FILE": str(auth_file)}):
            code, output, error = cli.run(
                "playlist", "create", "A playlist", "song", "--limit", "1"
            )
        assert (code, error) == (0, "")
        assert "Created private playlist" in output
        cli.youtube_api_factory.assert_called_once_with(
            auth_file,
            cli.oauth_factory.return_value,
            cli.factory.call_args.kwargs["requests_session"],
        )

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permission check")
    def test_playlist_rejects_group_readable_oauth_token(self, cli, tmp_path):
        auth_file = tmp_path / "oauth.json"
        auth_file.write_text("{}")
        auth_file.chmod(0o640)
        code, output, error = cli.run(
            "playlist", "create", "A playlist", "song",
            "--auth-file", str(auth_file),
        )
        assert (code, output) == (1, "")
        assert "permissions are too broad" in error
        assert "chmod 600" in error
        cli.factory.assert_not_called()


class TestYouTubeDataApi:
    def response(self, status, payload):
        response = MagicMock()
        response.status_code = status
        response.json.return_value = payload
        return response

    def test_creates_private_playlist_and_inserts_each_video(self, tmp_path):
        auth_file = private_file(
            tmp_path / "oauth.json",
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

        assert playlist_id == "PLcreated123"
        assert session.post.call_count == 3
        create_call, first_item, second_item = session.post.call_args_list
        assert create_call.args[0].endswith("/playlists")
        assert create_call.kwargs["params"] == {"part": "snippet,status"}
        assert create_call.kwargs["json"]["status"]["privacyStatus"] == "private"
        assert [
            first_item.kwargs["json"]["snippet"]["resourceId"]["videoId"],
            second_item.kwargs["json"]["snippet"]["resourceId"]["videoId"],
        ] == [FIRST, SECOND]

    def test_refreshes_expired_token_and_keeps_file_private(self, tmp_path):
        auth_file = private_file(
            tmp_path / "oauth.json",
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

        assert token == "refreshed-token"
        assert saved["access_token"] == "refreshed-token"
        assert saved["refresh_token"] == "refresh-token"
        assert permissions == 0o600

    def test_api_error_exposes_reason_but_not_raw_message(self, tmp_path):
        auth_file = private_file(
            tmp_path / "oauth.json",
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
                },
            },
        )
        api = YouTubeDataAPI(auth_file, MagicMock(), session)

        with pytest.raises(RecommendationError) as raised:
            api.create_private_playlist("A playlist", "Description", [FIRST])

        assert "HTTP 403, reason=quotaExceeded" in str(raised.value)
        assert "raw response details" not in str(raised.value)
