#!/usr/bin/env python3
"""Download series episodes or movies from Jellyfin.

Usage:
  jellyfin-download.py list movies|series [filter]
  jellyfin-download.py info [-r RANGE] "Series Name"
  jellyfin-download.py [download] [-t movie|series] [-r RANGE] "Name" [destination-dir]

Ranges (-r/--range, series only; comma-separate several):
  s1e1-s2e8   S01E01 through S02E08
  s1-s2       all of seasons 1 and 2
  s3          all of season 3
  s2e5        just S02E05
  s1e3-e7     S01E03 through S01E07
  s4-         season 4 onwards

Name lookup prefers an exact (case-insensitive) title match, otherwise the
first search result wins; other matches are listed on stderr. Use -t to
search only movies or only series when names overlap.

Configuration: environment variables, or KEY=value lines in
${XDG_CONFIG_HOME:-~/.config}/jellyfin-download.env (path overridable with
JELLYFIN_CONFIG). Variables already set in the environment take precedence.
  JELLYFIN_URL       Server base URL, e.g. https://jellyfin.example.com
  JELLYFIN_API_KEY   API key (Dashboard > API Keys), or instead:
  JELLYFIN_OP_ITEM   1Password item holding the key, read with the op CLI
  JELLYFIN_OP_VAULT  Vault containing that item (optional)
  JELLYFIN_OP_FIELD  Field holding the key (default: credential)

Filenames: if the server supplies a filename via the Download endpoint's
Content-Disposition header, that name is used as-is. Otherwise falls back
to "Series - S##E## - Title.ext" (episodes) or "Title (Year).ext" (movies).
Complete files are skipped and partial files are resumed.
"""

import argparse
import json
import math
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "2.0.0"
COMMANDS = ("list", "info", "download")
TYPES = {
    "movie": "Movie", "movies": "Movie", "film": "Movie", "films": "Movie",
    "series": "Series", "show": "Series", "shows": "Series", "tv": "Series",
}
CHUNK_SIZE = 1024 * 1024


class JellyfinError(Exception):
    pass


# --- Configuration -----------------------------------------------------------

def config_path():
    if os.environ.get("JELLYFIN_CONFIG"):
        return Path(os.environ["JELLYFIN_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "jellyfin-download.env"


def load_config(path):
    """Reads JELLYFIN_* assignments from the config file without executing it.
    Values already set in the environment are left alone."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        m = re.match(r"\s*(?:export\s+)?(JELLYFIN_[A-Z_]+)=(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        quoted = re.fullmatch(r'"(.*)"|\'(.*)\'', value)
        if quoted:
            value = quoted.group(1) if quoted.group(1) is not None else quoted.group(2)
        if not os.environ.get(key):
            os.environ[key] = value


def api_key_from_1password(item):
    cmd = ["op", "item", "get", item, "--reveal",
           "--fields", os.environ.get("JELLYFIN_OP_FIELD") or "credential"]
    if os.environ.get("JELLYFIN_OP_VAULT"):
        cmd += ["--vault", os.environ["JELLYFIN_OP_VAULT"]]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise JellyfinError("JELLYFIN_OP_ITEM is set but the 1Password CLI (op) is not installed")
    if result.returncode != 0:
        raise JellyfinError(f"op failed to read the API key: {result.stderr.strip()}")
    return result.stdout.strip()


# --- Jellyfin API ------------------------------------------------------------

class Jellyfin:
    def __init__(self, url, api_key):
        self.url = url.rstrip("/")
        host = socket.gethostname() or "unknown"
        self.auth = (f'MediaBrowser Client="jellyfin-download", Device="{host}", '
                     f'DeviceId="jellyfin-download-{host}", Version="{VERSION}", '
                     f'Token="{api_key}"')

    def request(self, path, params=None, headers=None):
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": self.auth, **(headers or {})})
        try:
            return urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 416:
                return e
            raise JellyfinError(f"{e.code} {e.reason} from {path}") from None
        except urllib.error.URLError as e:
            raise JellyfinError(f"Cannot reach {self.url}: {e.reason}") from None

    def get(self, path, **params):
        with self.request(path, params) as resp:
            return json.load(resp)

    def search(self, types, term=None):
        params = {"recursive": "true", "IncludeItemTypes": types,
                  "SortBy": "SortName", "SortOrder": "Ascending"}
        if term:
            params["searchTerm"] = term
        return self.get("/Items", **params).get("Items", [])

    def episodes(self, series_id):
        """Available episodes, skipping placeholders for missing ones."""
        items = self.get(f"/Shows/{series_id}/Episodes",
                         IsMissing="false", Fields="Path,MediaSources").get("Items", [])
        return [ep for ep in items
                if ep.get("ParentIndexNumber") is not None
                and ep.get("IndexNumber") is not None
                and ep.get("LocationType") != "Virtual"]


# --- Helpers -----------------------------------------------------------------

def sanitize(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def with_year(item, name=None):
    name = name or item["Name"]
    year = item.get("ProductionYear")
    return f"{name} ({year})" if year and str(year) not in name else name


def ep_code(ep):
    code = f"S{ep['ParentIndexNumber']:02d}E{ep['IndexNumber']:02d}"
    if ep.get("IndexNumberEnd"):
        code += f"-E{ep['IndexNumberEnd']:02d}"
    return code


def ep_size(ep):
    sources = ep.get("MediaSources") or []
    return (sources[0].get("Size") or 0) if sources else 0


def parse_ranges(spec):
    """Parses e.g. "s1e1-s2e8,s4-" into inclusive ((season, ep), (season, ep)) pairs."""
    ranges = []
    for part in spec.lower().replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"s(\d+)(?:e(\d+))?(-(?:s(\d+))?(?:e(\d+))?)?", part)
        if not m:
            raise JellyfinError(f"Invalid range: {part} (expected e.g. s1e1-s2e8, s1-s2, s3, s2e5)")
        s1, e1, dash, s2, e2 = m.groups()
        start = (int(s1), int(e1) if e1 else 0)
        if not dash:
            end = (int(s1), int(e1) if e1 else math.inf)
        elif not s2 and not e2:
            end = (math.inf, math.inf)
        else:
            end = (int(s2) if s2 else int(s1), int(e2) if e2 else math.inf)
        if start > end:
            raise JellyfinError(f"Invalid range: {part} (start is after end)")
        ranges.append((start, end))
    if not ranges:
        raise JellyfinError("Empty range")
    return ranges


def filter_episodes(episodes, spec):
    if not spec:
        return episodes
    ranges = parse_ranges(spec)
    selected = [ep for ep in episodes
                if any(start <= (ep["ParentIndexNumber"], ep["IndexNumber"]) <= end
                       for start, end in ranges)]
    if not selected:
        raise JellyfinError(f"No available episodes match range {spec}")
    return selected


def lookup(api, name, types):
    items = api.search(types, name)
    if not items:
        kind = {"Movie": "movie", "Series": "series"}.get(types, "series or movie")
        raise JellyfinError(f'No {kind} found matching "{name}"')
    exact = [i for i in items if i["Name"].casefold() == name.casefold()]
    item = (exact or items)[0]
    others = [i for i in items if i is not item]
    if others:
        print("Also matched (use a more exact name or -t to choose):", file=sys.stderr)
        for other in others[:10]:
            print(f"  {with_year(other)} [{other['Type']}]", file=sys.stderr)
    return item


# --- Downloading -------------------------------------------------------------

def content_disposition_filename(header):
    """Extracts the filename, preferring the RFC 5987 filename*= form."""
    if not header:
        return None
    star = re.search(r"filename\*\s*=\s*UTF-8''([^;\r\n]+)", header, re.IGNORECASE)
    if star:
        return urllib.parse.unquote(star.group(1).strip())
    plain = re.search(r'filename\s*=\s*"?([^";\r\n]+)"?', header, re.IGNORECASE)
    return plain.group(1).strip() if plain else None


def remote_size(resp):
    m = re.search(r"/(\d+)$", resp.headers.get("Content-Range") or "")
    if m:
        return int(m.group(1))
    length = resp.headers.get("Content-Length")
    return int(length) if resp.status == 200 and length else None


class Progress:
    """Progress line where a TIE fighter chases an X-wing across a scrolling
    starfield. The X-wing's position along the track is the download progress.
    Every few seconds the X-wing flips around and fires back; when the download
    completes the TIE fighter explodes."""

    TIE, TIE_HIT, XWING, BOOM = "(-o-)", "(*o*)", "X=>", " *#* "
    GAP = 7  # laser corridor between the two ships
    VOLLEY_FRAMES = 9  # flip, fire back, hit, flip again
    VOLLEY_GAP = (25, 60)  # frames between volleys
    STATS_WIDTH = 52  # room for "100.0%  999.9 MB / 999.9 GB  999.9 MB/s  ETA 9:59:59"
    FRAME_INTERVAL = 0.1

    def __init__(self, total, done):
        self.total, self.done, self.start_done = total, done, done
        self.start = self.last = time.monotonic()
        self.frame = 0
        self.rng = random.Random()
        self.next_volley = self.rng.randint(*self.VOLLEY_GAP)
        self.enabled = sys.stderr.isatty()
        color = self.enabled and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        codes = {"star": "2", "tie": "37", "laser": "92", "xwing": "1;97",
                 "engine": "91", "xlaser": "1;91", "boom": "1;93"}
        self.style = {k: f"\033[{v}m" if color else "" for k, v in codes.items()}
        self.reset = "\033[0m" if color else ""

    def update(self, n):
        self.done += n
        now = time.monotonic()
        if self.enabled and now - self.last >= self.FRAME_INTERVAL:
            self.last = now
            self.frame += 1
            if self.frame >= self.next_volley + self.VOLLEY_FRAMES:
                self.next_volley = self.frame + self.rng.randint(*self.VOLLEY_GAP)
            self.draw(now)

    def stats(self, now):
        rate = (self.done - self.start_done) / max(now - self.start, 1e-6)
        parts = [human_size(self.done)]
        if self.total:
            parts = [f"{self.done / self.total:6.1%}", f"{human_size(self.done)} / {human_size(self.total)}"]
        parts.append(f"{human_size(rate)}/s")
        if self.total and rate > 0 and self.done < self.total:
            eta = int((self.total - self.done) / rate)
            parts.append(f"ETA {eta // 3600}:{eta // 60 % 60:02d}:{eta % 60:02d}" if eta >= 3600
                         else f"ETA {eta // 60}:{eta % 60:02d}")
        return "  ".join(parts)

    def track(self, width, finished):
        # Stars drift left as the ships fly right.
        cells = [(" ", "")] * width
        for i in range(width):
            j = i + self.frame // 2
            if j % 11 == 0 or j % 17 == 5:
                cells[i] = ("\u00b7", "star")

        if self.total:
            head = round(min(self.done / self.total, 1) * (width - len(self.XWING)))
        else:
            head = width * 2 // 3
        tie = [(c, "tie") for c in self.TIE]
        xwing = [("X", "xwing"), ("=", "engine"), (">", "engine")]
        volley = self.frame - self.next_volley
        if finished:
            tie, lasers = [(c, "boom") for c in self.BOOM], [(" ", "")] * self.GAP
        elif 0 <= volley < self.VOLLEY_FRAMES:
            # The X-wing flips around; its red bolts travel left, and the
            # TIE fighter flashes when they arrive.
            if volley < self.VOLLEY_FRAMES - 1:
                xwing = [(c, "xwing") for c in "<=X"]
            if volley in (5, 6):
                tie = [(c, "boom") for c in self.TIE_HIT]
            lasers = [("-", "xlaser") if 1 <= volley <= 5 and (i + volley) % 3 == 0 else (" ", "")
                      for i in range(self.GAP)]
        else:
            # Bolts travel right, one cell per frame.
            lasers = [("-", "laser") if (i - self.frame) % 3 == 0 else (" ", "")
                      for i in range(self.GAP)]
        ships = tie + lasers + xwing

        first = head - len(tie) - self.GAP
        for k, cell in enumerate(ships):
            if 0 <= first + k < width:
                cells[first + k] = cell
        return "".join(f"{self.style[st]}{ch}{self.reset}" if st else ch for ch, st in cells)

    def draw(self, now, finished=False):
        stats = self.stats(now)
        columns = shutil.get_terminal_size((80, 24)).columns
        # Fixed per terminal size, so the ships don't jump as the stats change.
        width = min(48, columns - self.STATS_WIDTH - 7)
        line = f"  [{self.track(width, finished)}]  {stats}" if width >= 20 else f"  {stats}"
        print(f"\r{line}\033[K", end="", file=sys.stderr, flush=True)

    def finish(self):
        if self.enabled:
            self.draw(time.monotonic(), finished=True)
            print(file=sys.stderr)


def download_file(api, item_id, fallback_name, dest_dir, label):
    path = f"/Items/{item_id}/Download"

    # Probe headers with a 1-byte ranged request to learn the server's
    # filename and the total size without pulling the whole file twice.
    with api.request(path, headers={"Range": "bytes=0-0"}) as probe:
        name = content_disposition_filename(probe.headers.get("Content-Disposition"))
        size = remote_size(probe)
    filename = sanitize(name) if name else fallback_name
    out_path = dest_dir / filename

    offset = out_path.stat().st_size if out_path.exists() else 0
    if offset and size is not None:
        if offset == size:
            print(f"{label} Skipping (already complete): {filename}")
            return
        if offset > size:
            print(f"{label} Local file larger than remote, re-downloading: {filename}")
            offset = 0
    if offset:
        print(f"{label} Resuming ({human_size(offset)} / {human_size(size or 0)}): {filename}")
    else:
        print(f"{label} Downloading: {filename}")

    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with api.request(path, headers=headers) as resp:
        if resp.status == 416:
            # Nothing left to fetch: the local file is already complete.
            print(f"{label} Already complete: {filename}")
            return
        if offset and resp.status != 206:
            # Server ignored the Range header; start over.
            offset = 0
        progress = Progress(size, offset)
        with open(out_path, "r+b" if offset else "wb") as f:
            f.seek(offset)
            f.truncate()
            while chunk := resp.read(CHUNK_SIZE):
                f.write(chunk)
                progress.update(len(chunk))
        progress.finish()


def download_all(api, files, dest_dir):
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"Found {len(files)} file(s). Downloading to {dest_dir}/")
    for n, (item_id, fallback_name) in enumerate(files, 1):
        download_file(api, item_id, fallback_name, dest_dir, f"[{n}/{len(files)}]")
    print(f"Done. {len(files)} file(s) processed into {dest_dir}/")


# --- Commands ----------------------------------------------------------------

def cmd_list(api, args):
    types = TYPES[args.kind]
    items = api.search(types, args.filter)
    for item in items:
        print(with_year(item))
    sys.stdout.flush()
    print(f"\n{len(items)} {args.kind}", file=sys.stderr)


def cmd_info(api, args):
    series = lookup(api, args.name, "Series")
    episodes = filter_episodes(api.episodes(series["Id"]), args.range)
    seasons = {}
    for ep in episodes:
        seasons.setdefault(ep["ParentIndexNumber"], []).append(ep)

    print(with_year(series))
    print(f"{len(seasons)} season(s), {len(episodes)} episode(s), "
          f"{human_size(sum(map(ep_size, episodes)))}")
    for season, eps in sorted(seasons.items()):
        label = "Specials" if season == 0 else f"Season {season}"
        print(f"\n{label}: {len(eps)} episode(s) ({ep_code(eps[0])} - {ep_code(eps[-1])}), "
              f"{human_size(sum(map(ep_size, eps)))}")
        for ep in eps:
            minutes = round((ep.get("RunTimeTicks") or 0) / 600_000_000)
            runtime = f"{minutes}m" if minutes else ""
            title = (ep.get("Name") or "Untitled")[:50]
            print(f"  {ep_code(ep):<11} {title:<50} {runtime:>5} {human_size(ep_size(ep)):>9}")


def cmd_download(api, args):
    types = TYPES[args.type] if args.type else "Series,Movie"
    print(f"Looking up: {args.name}")
    item = lookup(api, args.name, types)
    print(f"{item['Type']} ID: {item['Id']} ({item['Name']})")
    title = sanitize(item["Name"])

    if item["Type"] == "Movie":
        if args.range:
            raise JellyfinError("--range only applies to series")
        ext = (item.get("Container") or "mkv").split(",")[0]
        files = [(item["Id"], f"{with_year(item, title)}.{ext}")]
    else:
        print("Fetching episode list...")
        files = []
        for ep in filter_episodes(api.episodes(item["Id"]), args.range):
            ep_title = sanitize(ep.get("Name") or "Untitled")
            ext = (ep.get("Container") or "mkv").split(",")[0]
            files.append((ep["Id"], f"{title} - {ep_code(ep)} - {ep_title}.{ext}"))

    download_all(api, files, Path(args.dest or args.name))


# --- Entry point -------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="jellyfin-download.py", description=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list movies or series on the server")
    p.add_argument("kind", type=str.lower, choices=sorted(TYPES), metavar="movies|series")
    p.add_argument("filter", nargs="?", help="only show titles matching this")

    p = sub.add_parser("info", help="show available seasons and episodes of a series")
    p.add_argument("-r", "--range", help="only show episodes in this range")
    p.add_argument("name")

    p = sub.add_parser("download", help="download a series or movie (the default command)")
    p.add_argument("-t", "--type", type=str.lower, choices=sorted(TYPES), metavar="movie|series",
                   help="only match this type (when a movie and a series share a name)")
    p.add_argument("-r", "--range", help="series only: download a subset of episodes")
    p.add_argument("name")
    p.add_argument("dest", nargs="?", help='destination directory (default: "./<name>")')
    return parser


def main(argv=None):
    # Keep stdout and stderr (lookup notes, progress) in order when piped.
    sys.stdout.reconfigure(line_buffering=True)
    argv = list(sys.argv[1:] if argv is None else argv)
    # "download" is the default command, so a bare name works too.
    if argv and argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version"):
        argv.insert(0, "download")
    args = build_parser().parse_args(argv or ["--help"])
    if args.command == "list":
        args.kind = "movies" if TYPES[args.kind] == "Movie" else "series"

    try:
        cfg = config_path()
        load_config(cfg)
        url = os.environ.get("JELLYFIN_URL")
        if not url:
            raise JellyfinError(f"JELLYFIN_URL is not set. Set it in the environment or in {cfg} (see --help).")
        api_key = os.environ.get("JELLYFIN_API_KEY")
        if not api_key:
            if not os.environ.get("JELLYFIN_OP_ITEM"):
                raise JellyfinError("No API key: set JELLYFIN_API_KEY or JELLYFIN_OP_ITEM (see --help).")
            api_key = api_key_from_1password(os.environ["JELLYFIN_OP_ITEM"])

        api = Jellyfin(url, api_key)
        {"list": cmd_list, "info": cmd_info, "download": cmd_download}[args.command](api, args)
    except JellyfinError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to resume.", file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    main()
