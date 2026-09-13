"""Search songs, mix radio queues, and create a private playlist."""

import argparse
import json
import os
import re
import stat
import sys
import tempfile
import time
from functools import partial
from pathlib import Path

from requests import Session
from ytmusicapi import OAuthCredentials, YTMusic, setup_oauth


YOUTUBE_DATA_API = "https://www.googleapis.com/youtube/v3"
MIN_SEED_QUERIES = 2
MAX_SEED_QUERIES = 10


class RecommendationError(Exception):
    """An input or upstream result cannot produce recommendations."""


def require_private_file(path, label):
    try:
        file_stat = path.stat()
    except OSError:
        raise RecommendationError(f"{label} not found or unreadable: {path}.") from None
    if not path.is_file():
        raise RecommendationError(f"{label} is not a regular file: {path}.")
    if os.name == "posix" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise RecommendationError(
            f"{label} permissions are too broad: {path}. Run 'chmod 600 {path}'."
        )
    return path


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def video_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise argparse.ArgumentTypeError("use an 11-character video ID from search")
    return value


def normalize_album(value):
    """Return the album title from ytmusicapi's object or string shapes."""
    if isinstance(value, dict):
        value = value.get("name")
    return value if isinstance(value, str) and value else None


def normalize_duration_seconds(value, duration):
    """Prefer upstream seconds, otherwise parse a m:ss or h:mm:ss display value."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if not isinstance(duration, str):
        return None
    parts = duration.strip().split(":")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
        return None
    return sum(
        int(part) * (60**position) for position, part in enumerate(reversed(parts))
    )


def normalize_tracks(items, limit, exclude=None):
    """Keep playable, unique IDs in upstream order and normalize public metadata."""
    if not isinstance(items, list):
        raise RecommendationError("YouTube Music returned an unexpected track list.")
    if isinstance(exclude, str):
        seen = {exclude}
    else:
        seen = set(exclude or [])
    tracks = []
    for item in items:
        if not isinstance(item, dict):
            continue
        identifier = item.get("videoId")
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier)
            or identifier in seen
            or item.get("isAvailable") is False
        ):
            continue
        seen.add(identifier)
        duration = item.get("duration") or item.get("length")
        if not isinstance(duration, str):
            duration = None
        tracks.append(
            {
                "videoId": identifier,
                "title": item.get("title") or "Unknown title",
                "artists": [
                    artist["name"]
                    for artist in item.get("artists") or []
                    if isinstance(artist, dict) and artist.get("name")
                ],
                "album": normalize_album(item.get("album")),
                "duration": duration,
                "duration_seconds": normalize_duration_seconds(
                    item.get("duration_seconds"), duration
                ),
                "url": f"https://music.youtube.com/watch?v={identifier}",
            }
        )
        if len(tracks) == limit:
            break
    return tracks


def search_songs(client, query, limit):
    tracks = normalize_tracks(client.search(query, filter="songs", limit=limit), limit)
    if not tracks:
        raise RecommendationError("No playable songs found. Try an artist and song title.")
    return tracks


def get_radio(client, query, identifier, limit):
    if query:
        seed = search_songs(client, query, 5)[0]
        identifier = seed["videoId"]
    else:
        seed = normalize_tracks([{"videoId": identifier}], 1)[0]

    queue = client.get_watch_playlist(videoId=identifier, limit=limit + 1, radio=True)
    if not isinstance(queue, dict) or "tracks" not in queue:
        raise RecommendationError("YouTube Music returned an unexpected radio response.")
    items = queue["tracks"]
    if not query and isinstance(items, list):
        seed_items = [
            item for item in items
            if isinstance(item, dict) and item.get("videoId") == identifier
        ]
        seed = next(iter(normalize_tracks(seed_items, 1)), seed)
    tracks = normalize_tracks(items, limit, exclude=identifier)
    if not tracks:
        raise RecommendationError("No radio recommendations returned. Try another song.")
    return {"seed": seed, "requested": limit, "returned": len(tracks), "tracks": tracks}


def normalize_seed_queries(queries):
    """Return two to ten distinct, nonblank seed queries."""
    if (
        not isinstance(queries, list)
        or not MIN_SEED_QUERIES <= len(queries) <= MAX_SEED_QUERIES
    ):
        raise RecommendationError(
            f"Provide between {MIN_SEED_QUERIES} and {MAX_SEED_QUERIES} "
            "seed queries."
        )
    cleaned = []
    for query in queries:
        if not isinstance(query, str) or not query.strip():
            raise RecommendationError("Seed queries must not be blank.")
        cleaned.append(query.strip())
    if len({query.casefold() for query in cleaned}) != len(cleaned):
        raise RecommendationError("Seed queries must not contain duplicates.")
    return cleaned


def mix_radio_tracks(radios, seed_ids, limit):
    """Take one new track per radio per round until the total limit is reached."""
    iterators = [iter(radio) for radio in radios]
    exhausted = [False] * len(iterators)
    seen = set(seed_ids)
    tracks = []

    while len(tracks) < limit:
        added_this_round = False
        for index, iterator in enumerate(iterators):
            if exhausted[index]:
                continue
            for track in iterator:
                identifier = track["videoId"]
                if identifier in seen:
                    continue
                seen.add(identifier)
                tracks.append(track)
                added_this_round = True
                break
            else:
                exhausted[index] = True
            if len(tracks) == limit:
                return tracks
        if not added_this_round:
            break
    return tracks


def get_multi_seed_radio(client, queries, limit):
    """Resolve multiple seeds and fairly mix their YouTube Music radio queues."""
    queries = normalize_seed_queries(queries)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise RecommendationError("limit must be between 1 and 100.")
    if limit < len(queries):
        raise RecommendationError(
            "limit must be at least the number of seed queries."
        )

    seeds = []
    for index, query in enumerate(queries, start=1):
        try:
            seeds.append(search_songs(client, query, 5)[0])
        except RecommendationError as error:
            raise RecommendationError(f"Seed {index}: {error}") from None

    seed_ids = [seed["videoId"] for seed in seeds]
    if len(set(seed_ids)) != len(seed_ids):
        raise RecommendationError(
            "Multiple seed queries resolved to the same song. Use more specific seeds."
        )

    radios = []
    request_limit = limit + len(seed_ids)
    for index, seed in enumerate(seeds, start=1):
        try:
            queue = client.get_watch_playlist(
                videoId=seed["videoId"], limit=request_limit, radio=True
            )
            if not isinstance(queue, dict) or "tracks" not in queue:
                raise RecommendationError(
                    "YouTube Music returned an unexpected radio response."
                )
            tracks = normalize_tracks(queue["tracks"], limit, exclude=seed_ids)
            if not tracks:
                raise RecommendationError(
                    "No radio recommendations returned. Try another song."
                )
            radios.append(tracks)
        except RecommendationError as error:
            raise RecommendationError(f"Seed {index}: {error}") from None

    tracks = mix_radio_tracks(radios, seed_ids, limit)
    if not tracks:
        raise RecommendationError(
            "No unique radio recommendations returned. Try other seeds."
        )
    return {
        "seeds": seeds,
        "requested": limit,
        "returned": len(tracks),
        "tracks": tracks,
    }


def _create_playlist_result(playlist_client, title, description, radio):
    playlist_id = playlist_client.create_private_playlist(
        title, description, [track["videoId"] for track in radio["tracks"]]
    )
    if not isinstance(playlist_id, str) or not playlist_id:
        raise RecommendationError("YouTube Music did not confirm playlist creation.")
    return {
        "playlistId": playlist_id,
        "title": title,
        "privacyStatus": "PRIVATE",
        "url": f"https://music.youtube.com/playlist?list={playlist_id}",
        "requested": radio["requested"],
        "trackCount": radio["returned"],
        "tracks": radio["tracks"],
    }


def create_playlist_from_radio(
    discovery_client, playlist_client, title, description, query, limit
):
    radio = get_radio(discovery_client, query, None, limit)
    return {
        **_create_playlist_result(playlist_client, title, description, radio),
        "seed": radio["seed"],
    }


def create_playlist_from_multi_seed_radio(
    discovery_client, playlist_client, title, description, queries, limit
):
    radio = get_multi_seed_radio(discovery_client, queries, limit)
    return {
        **_create_playlist_result(playlist_client, title, description, radio),
        "seeds": radio["seeds"],
    }


def oauth_credentials_from_env(session):
    client_file = os.environ.get("YTMUSIC_OAUTH_CLIENT_FILE")
    if client_file:
        client_path = require_private_file(
            Path(client_file).expanduser(), "OAuth client file"
        )
        try:
            document = json.loads(client_path.read_text(encoding="utf-8"))
            client = document["installed"]
            return OAuthCredentials(
                client["client_id"], client["client_secret"], session=session
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            raise RecommendationError(
                "YTMUSIC_OAUTH_CLIENT_FILE must point to a readable Google OAuth "
                "client JSON file."
            ) from None

    client_id = os.environ.get("YTMUSIC_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("YTMUSIC_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RecommendationError(
            "Set YTMUSIC_OAUTH_CLIENT_FILE, or set YTMUSIC_OAUTH_CLIENT_ID and "
            "YTMUSIC_OAUTH_CLIENT_SECRET, from your Google OAuth client."
        )
    return OAuthCredentials(client_id, client_secret, session=session)


def create_oauth_file(auth_file, session):
    if auth_file.exists():
        raise RecommendationError(
            f"Authentication file already exists: {auth_file}. "
            "Move it first to connect another account."
        )
    if not auth_file.parent.is_dir():
        raise RecommendationError(f"Authentication directory not found: {auth_file.parent}.")

    credentials = oauth_credentials_from_env(session)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{auth_file.name}.", dir=auth_file.parent
    )
    os.close(descriptor)
    temporary_file = Path(temporary_name)
    try:
        setup_oauth(
            credentials.client_id,
            credentials.client_secret,
            filepath=str(temporary_file),
            session=session,
            open_browser=True,
        )
        if temporary_file.stat().st_size == 0:
            raise RecommendationError("The OAuth flow did not produce a token file.")
        temporary_file.chmod(0o600)
        temporary_file.replace(auth_file)
    finally:
        temporary_file.unlink(missing_ok=True)


def write_private_json(path, document):
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_file = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary_file.chmod(0o600)
        temporary_file.replace(path)
    finally:
        temporary_file.unlink(missing_ok=True)


class YouTubeDataAPI:
    """Minimal official YouTube Data API client for private playlist writes."""

    def __init__(self, auth_file, oauth_credentials, session):
        self.auth_file = require_private_file(auth_file, "OAuth token file")
        self.oauth_credentials = oauth_credentials
        self.session = session

    def _access_token(self):
        try:
            document = json.loads(self.auth_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise RecommendationError("OAuth token file is unreadable or invalid.") from None
        if not isinstance(document, dict):
            raise RecommendationError("OAuth token file is unreadable or invalid.")

        access_token = document.get("access_token")
        expires_at = document.get("expires_at")
        try:
            expired = float(expires_at) <= time.time() + 60
        except (TypeError, ValueError):
            expired = not isinstance(access_token, str) or not access_token

        if not expired and isinstance(access_token, str) and access_token:
            return access_token

        refresh_token = document.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise RecommendationError("OAuth token is expired and cannot be refreshed.")
        refreshed = self.oauth_credentials.refresh_token(refresh_token)
        if not isinstance(refreshed, dict) or not isinstance(
            refreshed.get("access_token"), str
        ):
            raise RecommendationError("Google did not return a refreshed OAuth token.")

        document.update(refreshed)
        try:
            document["expires_at"] = time.time() + float(refreshed["expires_in"])
        except (KeyError, TypeError, ValueError):
            raise RecommendationError("Google returned an invalid OAuth token lifetime.") from None
        write_private_json(self.auth_file, document)
        return refreshed["access_token"]

    @staticmethod
    def _error_reason(payload):
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        if not isinstance(error, dict):
            return None
        errors = error.get("errors")
        if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
            return None
        reason = errors[0].get("reason")
        if isinstance(reason, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", reason):
            return reason
        return None

    def _post(self, resource, part, body):
        response = self.session.post(
            f"{YOUTUBE_DATA_API}/{resource}",
            params={"part": part},
            headers={"Authorization": f"Bearer {self._access_token()}"},
            json=body,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            reason = self._error_reason(payload)
            suffix = f", reason={reason}" if reason else ""
            raise RecommendationError(
                f"YouTube Data API rejected the request (HTTP {response.status_code}{suffix})."
            )
        if not isinstance(payload, dict):
            raise RecommendationError("YouTube Data API returned an unexpected response.")
        return payload

    def create_private_playlist(self, title, description, video_ids):
        playlist = self._post(
            "playlists",
            "snippet,status",
            {
                "snippet": {"title": title, "description": description},
                "status": {"privacyStatus": "private"},
            },
        )
        playlist_id = playlist.get("id")
        if not isinstance(playlist_id, str) or not playlist_id:
            raise RecommendationError("YouTube Data API did not confirm playlist creation.")

        added = 0
        for identifier in video_ids:
            try:
                self._post(
                    "playlistItems",
                    "snippet",
                    {
                        "snippet": {
                            "playlistId": playlist_id,
                            "resourceId": {
                                "kind": "youtube#video",
                                "videoId": identifier,
                            },
                        }
                    },
                )
            except RecommendationError as error:
                url = f"https://music.youtube.com/playlist?list={playlist_id}"
                raise RecommendationError(
                    f"Created playlist {playlist_id}, but stopped after {added} of "
                    f"{len(video_ids)} tracks. {error} Playlist: {url}"
                ) from None
            added += 1
        return playlist_id


def youtube_data_client(auth_file, session):
    require_private_file(auth_file, "OAuth token file")
    return YouTubeDataAPI(auth_file, oauth_credentials_from_env(session), session)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    default_auth_file = os.environ.get("YTMUSIC_AUTH_FILE", "oauth.json")
    search = commands.add_parser("search", help="find songs and their video IDs")
    search.add_argument("query", help="artist and song title")
    radio = commands.add_parser("radio", help="get recommendations around one song")
    radio.add_argument("query", nargs="?", help="artist and song title; uses the first match")
    radio.add_argument("--video-id", type=video_id, help="use a specific song ID from search")
    radio_mix = commands.add_parser(
        "radio-mix", help="mix recommendations from two to ten songs"
    )
    radio_mix.add_argument(
        "queries", nargs="+", metavar="QUERY", help="artist and song title"
    )
    auth = commands.add_parser("auth", help="connect a Google account using OAuth")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    oauth = auth_commands.add_parser("oauth", help="create a local OAuth token")
    oauth.add_argument(
        "--auth-file",
        default=default_auth_file,
        help="OAuth token file to create (default: YTMUSIC_AUTH_FILE or oauth.json)",
    )
    playlist = commands.add_parser("playlist", help="manage authenticated playlists")
    playlist_commands = playlist.add_subparsers(dest="playlist_command", required=True)
    create = playlist_commands.add_parser(
        "create", help="create a private playlist from song radio"
    )
    create.add_argument("title", help="playlist title")
    create.add_argument("query", help="artist and song title used as the radio seed")
    create_mix = playlist_commands.add_parser(
        "create-mix", help="create a private playlist from two to ten song radios"
    )
    create_mix.add_argument("title", help="playlist title")
    create_mix.add_argument(
        "queries", nargs="+", metavar="QUERY", help="artist and song title"
    )
    for command, default in ((search, 5), (radio, 30), (radio_mix, 50)):
        command.add_argument("--limit", type=positive_int, default=default,
                             help=f"maximum results (default: {default})")
        command.add_argument("--json", action="store_true", help="write JSON to stdout")
    for command, default in ((create, 30), (create_mix, 50)):
        command.add_argument(
            "--description", default="Created by yt-mcp.", help="playlist description"
        )
        command.add_argument(
            "--auth-file",
            default=default_auth_file,
            help="Google OAuth token file (default: YTMUSIC_AUTH_FILE or oauth.json)",
        )
        command.add_argument(
            "--limit",
            type=positive_int,
            default=default,
            help=f"maximum tracks (default: {default})",
        )
        command.add_argument("--json", action="store_true", help="write JSON to stdout")
    return parser


def track_label(track):
    artists = ", ".join(track["artists"]) or "Unknown artist"
    return f"{artists} — {track['title']}"


def api_session():
    session = Session()
    session.trust_env = False
    session.request = partial(session.request, timeout=30)
    return session


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "query", None) is not None:
        args.query = args.query.strip()
        if not args.query:
            parser.error("the search query must not be blank")
    if args.command == "radio" and bool(args.query) == bool(args.video_id):
        parser.error("provide either a song query or --video-id")
    is_multi_seed = args.command == "radio-mix" or (
        args.command == "playlist" and args.playlist_command == "create-mix"
    )
    if is_multi_seed:
        try:
            args.queries = normalize_seed_queries(args.queries)
        except RecommendationError as error:
            parser.error(str(error))
        if args.limit > 100:
            parser.error("limit must be between 1 and 100")
        if args.limit < len(args.queries):
            parser.error("limit must be at least the number of seed queries")
    auth_file = None
    if args.command in {"auth", "playlist"}:
        auth_file = Path(args.auth_file).expanduser()
    if args.command == "playlist":
        args.title = args.title.strip()
        if not args.title:
            parser.error("the playlist title must not be blank")
        if "<" in args.title or ">" in args.title:
            parser.error("the playlist title must not contain < or >")
        if not auth_file.is_file():
            print(
                f"Error: Authentication file not found: {auth_file}. "
                "Run 'uv run yt auth oauth' first.",
                file=sys.stderr,
            )
            return 1

    try:
        with api_session() as session:
            if args.command == "auth":
                create_oauth_file(auth_file, session)
                print(f"OAuth token saved to {auth_file} with owner-only permissions.")
                return 0
            playlist_client = None
            if args.command == "playlist":
                playlist_client = youtube_data_client(auth_file, session)
            client = YTMusic(requests_session=session)
            if args.command == "search":
                result = {"tracks": search_songs(client, args.query, args.limit)}
            elif args.command == "radio":
                result = get_radio(client, args.query, args.video_id, args.limit)
            elif args.command == "radio-mix":
                result = get_multi_seed_radio(client, args.queries, args.limit)
            elif args.command == "playlist" and args.playlist_command == "create":
                result = create_playlist_from_radio(
                    client,
                    playlist_client,
                    args.title,
                    args.description,
                    args.query,
                    args.limit,
                )
            else:
                result = create_playlist_from_multi_seed_radio(
                    client,
                    playlist_client,
                    args.title,
                    args.description,
                    args.queries,
                    args.limit,
                )
    except RecommendationError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Keep raw responses, request headers, and OAuth details out of terminal output.
        print(
            f"Error: YouTube request failed ({type(error).__name__}). "
            "Check your connection and retry.",
            file=sys.stderr,
        )
        return 1

    if args.command in {"radio", "radio-mix"} and result["returned"] < args.limit:
        print(
            f"Warning: returned {result['returned']} of {args.limit} requested "
            "recommendations after filtering the radio queue.",
            file=sys.stderr,
        )
    if args.command == "playlist" and result["trackCount"] < args.limit:
        print(
            f"Warning: created the playlist with {result['trackCount']} of "
            f"{args.limit} requested tracks after filtering the radio queue.",
            file=sys.stderr,
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if args.command == "radio":
            print(f"Seed: {track_label(result['seed'])} [{result['seed']['videoId']}]")
            print(f"{result['returned']} recommendations\n")
        if args.command == "radio-mix":
            print("Seeds:")
            for index, seed in enumerate(result["seeds"], 1):
                print(f"  {index}. {track_label(seed)} [{seed['videoId']}]")
            print(f"{result['returned']} mixed recommendations\n")
        if args.command == "playlist":
            print(f"Created private playlist: {result['title']}")
            print(f"{result['trackCount']} tracks")
            print(result["url"])
            if args.playlist_command == "create":
                print(
                    f"Seed: {track_label(result['seed'])} "
                    f"[{result['seed']['videoId']}]"
                )
            else:
                print("Seeds:")
                for index, seed in enumerate(result["seeds"], 1):
                    print(f"  {index}. {track_label(seed)} [{seed['videoId']}]")
        else:
            for index, track in enumerate(result["tracks"], 1):
                duration = f" ({track['duration']})" if track["duration"] else ""
                print(f"{index:2}. {track_label(track)}{duration} [{track['videoId']}]")
                print(f"    {track['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
