#!/usr/bin/env bash
# Download series episodes or movies from Jellyfin via curl.
#
# Usage:
#   jellyfin-download.sh list movies|series [filter]
#   jellyfin-download.sh info "Series Name"
#   jellyfin-download.sh [download] [-t movie|series] [-r RANGE] "Name" [destination-dir]
#
# Options:
#   -t, --type movie|series  Only match that type (use when a movie and a
#                            series share a name). Default: search both.
#   -r, --range RANGE        Series only: download a subset of episodes.
#                            Comma-separate several ranges. Examples:
#                              s1e1-s2e8   S01E01 through S02E08
#                              s1-s2       all of seasons 1 and 2
#                              s3          all of season 3
#                              s2e5        just S02E05
#                              s1e3-e7     S01E03 through S01E07
#                              s4-         season 4 onwards
#   -h, --help               Show this help.
#
# Name lookup prefers an exact (case-insensitive) title match, otherwise the
# first search result wins; other matches are listed on stderr.
#
# Configuration: environment variables, or KEY=value lines in
# ${XDG_CONFIG_HOME:-~/.config}/jellyfin-download.env (path overridable with
# JELLYFIN_CONFIG). Variables already set in the environment take precedence.
#   JELLYFIN_URL       Server base URL, e.g. https://jellyfin.example.com
#   JELLYFIN_API_KEY   API key (Dashboard > API Keys), or instead:
#   JELLYFIN_OP_ITEM   1Password item holding the key, read with the op CLI
#   JELLYFIN_OP_VAULT  Vault containing that item (optional)
#   JELLYFIN_OP_FIELD  Field holding the key (default: credential)
#
# Filenames: if the server supplies a filename via the Download endpoint's
# Content-Disposition header, that name is used as-is. Otherwise falls back
# to "Series - S##E## - Title.ext" (episodes) or "Title (Year).ext" (movies).

set -euo pipefail

CONFIG_FILE="${JELLYFIN_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/jellyfin-download.env}"

usage() {
    sed -n '2,/^$/{/^$/d;s/^# \{0,1\}//;p}' "$0"
    exit "${1:-0}"
}

COMMAND=download
case "${1:-}" in
    list|info|download) COMMAND=$1; shift ;;
    ""|-h|--help) usage ;;
esac

TYPE=""
RANGE=""
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--type) TYPE="${2:?--type needs movie or series}"; shift 2 ;;
        -r|--range) RANGE="${2:?--range needs a value, e.g. s1e1-s2e8}"; shift 2 ;;
        -h|--help) usage ;;
        --) shift; POSITIONAL+=("$@"); break ;;
        -*) echo "Unknown option: $1" >&2; usage 1 ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done
set -- "${POSITIONAL[@]}"

# Maps a user-supplied type to Jellyfin's IncludeItemTypes value.
normalize_type() {
    case "${1,,}" in
        movie|movies|film|films) echo Movie ;;
        series|show|shows|tv) echo Series ;;
        *) echo "Unknown type: $1 (expected movie or series)" >&2; exit 1 ;;
    esac
}

# Reads JELLYFIN_* assignments from the config file without executing it.
# Values already set in the environment are left alone.
load_config() {
    local line key value
    [[ -f "$CONFIG_FILE" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?(JELLYFIN_[A-Z_]+)=(.*)$ ]] || continue
        key=${BASH_REMATCH[2]}
        value=${BASH_REMATCH[3]}
        if [[ "$value" =~ ^\"(.*)\"[[:space:]]*$ || "$value" =~ ^\'(.*)\'[[:space:]]*$ ]]; then
            value=${BASH_REMATCH[1]}
        fi
        if [[ -z "${!key:-}" ]]; then
            export "$key=$value"
        fi
    done < "$CONFIG_FILE"
}

load_config

if [[ -z "${JELLYFIN_URL:-}" ]]; then
    echo "JELLYFIN_URL is not set. Set it in the environment or in ${CONFIG_FILE} (see --help)." >&2
    exit 1
fi
JELLYFIN_URL=${JELLYFIN_URL%/}

if [[ -z "${JELLYFIN_API_KEY:-}" ]]; then
    if [[ -z "${JELLYFIN_OP_ITEM:-}" ]]; then
        echo "No API key: set JELLYFIN_API_KEY or JELLYFIN_OP_ITEM (see --help)." >&2
        exit 1
    fi
    JELLYFIN_API_KEY=$(op item get "$JELLYFIN_OP_ITEM" ${JELLYFIN_OP_VAULT:+--vault "$JELLYFIN_OP_VAULT"} \
        --reveal --fields "${JELLYFIN_OP_FIELD:-credential}")
fi

AUTH_HEADER='Authorization: MediaBrowser Client="jellyfin-download", Device="'"${HOSTNAME:-unknown}"'", DeviceId="jellyfin-download-'"${HOSTNAME:-unknown}"'", Version="1.0.0", Token="'"${JELLYFIN_API_KEY}"'"'

api_get() {
    curl -sf "${JELLYFIN_URL}${1}" -H "$AUTH_HEADER"
}

urlencode() {
    python3 -c 'import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))' "$1"
}

sanitize_filename() {
    python3 -c 'import re, sys; print(re.sub(r"[<>:\"/\\|?*]", "_", sys.argv[1]).strip())' "$1"
}

# Looks up a movie or series by name. Prints one tab-separated line:
# id, name, type, production year, container.
lookup_item() {
    local name=$1 types=$2
    api_get "/Items?recursive=true&IncludeItemTypes=${types}&searchTerm=$(urlencode "$name")" \
        | python3 -c '
import json, sys

name, types = sys.argv[1], sys.argv[2]
items = json.load(sys.stdin).get("Items", [])
kind = {"Movie": "movie", "Series": "series"}.get(types, "series or movie")
if not items:
    sys.exit(f"No {kind} found matching \"{name}\"")
exact = [i for i in items if i["Name"].casefold() == name.casefold()]
item = (exact or items)[0]
others = [i for i in items if i is not item]
if others:
    print("Also matched (use a more exact name or -t to choose):", file=sys.stderr)
    for o in others[:10]:
        year = " (%s)" % o["ProductionYear"] if o.get("ProductionYear") and str(o["ProductionYear"]) not in o["Name"] else ""
        print("  %s%s [%s]" % (o["Name"], year, o["Type"]), file=sys.stderr)
print("\t".join(str(x) for x in (
    item["Id"], item["Name"], item["Type"],
    item.get("ProductionYear") or "", (item.get("Container") or "mkv").split(",")[0],
)))
' "$name" "$types"
}

# Shared episode handling for the info and download commands. Reads the
# /Shows/{id}/Episodes JSON on stdin.
#   argv: mode (info|download), series title, range spec (may be empty)
#   info:     prints a season/episode overview
#   download: prints "id<TAB>fallback filename" per selected episode
read -r -d '' EPISODES_PY <<'PY' || true
import json, math, re, sys

mode, series_title, range_spec = sys.argv[1], sys.argv[2], sys.argv[3]

def sanitize(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()

def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024

def parse_ranges(spec):
    ranges = []
    for part in spec.lower().replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"s(\d+)(?:e(\d+))?(-(?:s(\d+))?(?:e(\d+))?)?", part)
        if not m:
            sys.exit(f"Invalid range: {part} (expected e.g. s1e1-s2e8, s1-s2, s3, s2e5)")
        s1, e1, dash, s2, e2 = m.groups()
        start = (int(s1), int(e1) if e1 else 0)
        if not dash:
            end = (int(s1), int(e1) if e1 else math.inf)
        elif not s2 and not e2:
            end = (math.inf, math.inf)
        else:
            end = (int(s2) if s2 else int(s1), int(e2) if e2 else math.inf)
        if start > end:
            sys.exit(f"Invalid range: {part} (start is after end)")
        ranges.append((start, end))
    return ranges

episodes = []
for ep in json.load(sys.stdin).get("Items", []):
    season, index = ep.get("ParentIndexNumber"), ep.get("IndexNumber")
    if season is None or index is None or ep.get("LocationType") == "Virtual":
        continue
    episodes.append(ep)

if range_spec:
    ranges = parse_ranges(range_spec)
    episodes = [
        ep for ep in episodes
        if any(start <= (ep["ParentIndexNumber"], ep["IndexNumber"]) <= end for start, end in ranges)
    ]
    if not episodes:
        sys.exit(f"No available episodes match range {range_spec}")

def ep_code(ep):
    code = f"S{ep['ParentIndexNumber']:02d}E{ep['IndexNumber']:02d}"
    if ep.get("IndexNumberEnd"):
        code += f"-E{ep['IndexNumberEnd']:02d}"
    return code

def ep_size(ep):
    return sum(s.get("Size") or 0 for s in (ep.get("MediaSources") or [])[:1])

if mode == "download":
    for ep in episodes:
        title = sanitize(ep.get("Name") or "Untitled")
        ext = (ep.get("Container") or "mkv").split(",")[0]
        print(f"{ep['Id']}\t{series_title} - {ep_code(ep)} - {title}.{ext}")
    sys.exit(0)

seasons = {}
for ep in episodes:
    seasons.setdefault(ep["ParentIndexNumber"], []).append(ep)

total_size = sum(ep_size(ep) for ep in episodes)
print(f"{len(seasons)} season(s), {len(episodes)} episode(s), {human_size(total_size)}")
for season, eps in sorted(seasons.items()):
    label = "Specials" if season == 0 else f"Season {season}"
    first, last = ep_code(eps[0]), ep_code(eps[-1])
    print(f"\n{label}: {len(eps)} episode(s) ({first} - {last}), {human_size(sum(map(ep_size, eps)))}")
    for ep in eps:
        minutes = round((ep.get("RunTimeTicks") or 0) / 600_000_000)
        runtime = f"{minutes}m" if minutes else ""
        print(f"  {ep_code(ep):<11} {(ep.get('Name') or 'Untitled')[:50]:<50} {runtime:>5} {human_size(ep_size(ep)):>9}")
PY

fetch_episodes() {
    api_get "/Shows/${1}/Episodes?IsMissing=false&Fields=Path,MediaSources"
}

cmd_list() {
    local kind=${1:-} filter=${2:-} types
    [[ -n "$kind" ]] || usage 1
    types=$(normalize_type "$kind")
    api_get "/Items?recursive=true&IncludeItemTypes=${types}&SortBy=SortName&SortOrder=Ascending${filter:+&searchTerm=$(urlencode "$filter")}" \
        | python3 -c '
import json, sys

items = json.load(sys.stdin).get("Items", [])
for item in items:
    year = " (%s)" % item["ProductionYear"] if item.get("ProductionYear") and str(item["ProductionYear"]) not in item["Name"] else ""
    print(item["Name"] + year)
sys.stdout.flush()
print(f"\n{len(items)} {sys.argv[1]}", file=sys.stderr)
' "${kind,,}"
}

cmd_info() {
    local name=${1:?Usage: $0 info \"Series Name\"} info id title year
    info=$(lookup_item "$name" Series)
    IFS=$'\t' read -r id title _ year _ <<< "$info"
    [[ -n "$year" && "$title" != *"$year"* ]] && title+=" (${year})"
    echo "$title"
    fetch_episodes "$id" | python3 -c "$EPISODES_PY" info "$title" "$RANGE"
}

cmd_download() {
    local name=${1:?Usage: $0 [download] [-t movie|series] [-r RANGE] \"Name\" [destination-dir]}
    local dest_dir=${2:-./$name}
    local types=Series,Movie info id title item_type year container title_safe
    [[ -n "$TYPE" ]] && types=$(normalize_type "$TYPE")

    echo "Looking up: ${name}"
    info=$(lookup_item "$name" "$types")
    IFS=$'\t' read -r id title item_type year container <<< "$info"
    title_safe=$(sanitize_filename "$title")
    echo "${item_type} ID: ${id} (${title})"

    if [[ "$item_type" == "Movie" && -n "$RANGE" ]]; then
        echo "--range only applies to series" >&2
        exit 1
    fi

    mkdir -p "$dest_dir"
    local list_file="${dest_dir}/.episodes.tsv"
    if [[ "$item_type" == "Movie" ]]; then
        [[ -n "$year" && "$title_safe" != *"$year"* ]] && title_safe+=" (${year})"
        printf '%s\t%s.%s\n' "$id" "$title_safe" "$container" > "$list_file"
    else
        echo "Fetching episode list..."
        fetch_episodes "$id" | python3 -c "$EPISODES_PY" download "$title_safe" "$RANGE" > "$list_file"
    fi

    download_list "$list_file" "$dest_dir"
    rm -f "$list_file"
}

# Extracts the filename from a Content-Disposition header, if any (prefers
# the RFC 5987 filename*= form over the plain filename= form).
extract_cd_filename() {
    python3 -c '
import re, sys, urllib.parse
header = sys.stdin.read()
star = re.search(r"filename\*\s*=\s*UTF-8'"''"'([^;\r\n]+)", header, re.IGNORECASE)
if star:
    print(urllib.parse.unquote(star.group(1).strip()))
    sys.exit(0)
plain = re.search(r"filename\s*=\s*\"?([^\";\r\n]+)\"?", header, re.IGNORECASE)
if plain:
    print(plain.group(1).strip())
'
}

# Extracts the total remote file size from a Content-Range header
# (e.g. "Content-Range: bytes 0-0/1093772322" -> 1093772322).
extract_remote_size() {
    python3 -c '
import re, sys
header = sys.stdin.read()
m = re.search(r"content-range:\s*bytes\s+\d+-\d+/(\d+)", header, re.IGNORECASE)
if m:
    print(m.group(1))
'
}

# Downloads every "id<TAB>fallback filename" line in $1 into $2, skipping
# complete files and resuming partial ones.
download_list() {
    local list_file=$1 dest_dir=$2 total count=0
    total=$(wc -l < "$list_file")
    echo "Found ${total} file(s). Downloading to ${dest_dir}/"

    local item_id fallback_name download_url headers cd_name remote_size filename out_path local_size
    while IFS=$'\t' read -r item_id fallback_name; do
        count=$((count + 1))
        download_url="${JELLYFIN_URL}/Items/${item_id}/Download"

        # Probe headers with a 1-byte ranged request to see if the server
        # supplies a filename, without pulling the whole file twice.
        headers=$(curl -sf -D - -o /dev/null -r 0-0 "$download_url" -H "$AUTH_HEADER")
        cd_name=$(extract_cd_filename <<< "$headers")
        remote_size=$(extract_remote_size <<< "$headers")

        if [[ -n "$cd_name" ]]; then
            filename=$(sanitize_filename "$cd_name")
        else
            filename="$fallback_name"
        fi

        out_path="${dest_dir}/${filename}"
        if [[ -f "$out_path" ]]; then
            local_size=$(stat -c%s "$out_path")
            if [[ -n "$remote_size" && "$local_size" == "$remote_size" ]]; then
                echo "[${count}/${total}] Skipping (already complete): ${filename}"
                continue
            elif [[ -n "$remote_size" && "$local_size" -gt "$remote_size" ]]; then
                echo "[${count}/${total}] Local file larger than remote, re-downloading: ${filename}"
                rm -f "$out_path"
            else
                echo "[${count}/${total}] Resuming (${local_size}/${remote_size:-?} bytes): ${filename}"
            fi
        else
            echo "[${count}/${total}] Downloading: ${filename}"
        fi
        curl -f -L -C - -o "$out_path" \
            "$download_url" \
            -H "$AUTH_HEADER" \
            --progress-bar
    done < "$list_file"

    echo "Done. ${total} file(s) processed into ${dest_dir}/"
}

case "$COMMAND" in
    list) cmd_list "$@" ;;
    info) cmd_info "$@" ;;
    download) cmd_download "$@" ;;
esac
