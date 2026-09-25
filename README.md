# jellyfin-download

A single-file Python script that downloads series and movies from a Jellyfin server, with episode ranges, resumable downloads, and a quick overview of what the server has.

## What it does

- Downloads a whole series, a range of episodes (`s1e1-s2e8`, `s1-s2`, `s3`, …), or a single movie
- Lists the movies or series on the server, optionally filtered by name
- Shows what's available for a series: seasons, episodes, runtimes and file sizes
- Uses the original filename from the server when it provides one, otherwise names files `Series - S01E02 - Title.ext` or `Title (Year).ext`
- Skips files that are already complete and resumes partial downloads, so an interrupted run can be restarted safely
- Shows progress as a TIE fighter chasing an X-wing across a scrolling starfield, with size, speed and ETA (set `NO_COLOR` to disable colours)
- Skips "missing" placeholder episodes that Jellyfin lists but doesn't have a file for
- Can read the API key from 1Password via the `op` CLI instead of storing it anywhere

## Requirements

- Python 3.9+ (standard library only, nothing to `pip install`)
- A Jellyfin API key (Dashboard → API Keys)
- Optional: the [1Password CLI](https://developer.1password.com/docs/cli/) (`op`) if the key lives in 1Password

## Configuration

Set these as environment variables, or put them as `KEY=value` lines in `~/.config/jellyfin-download.env` (the path follows `XDG_CONFIG_HOME` and can be overridden with `JELLYFIN_CONFIG`). Variables set in the environment take precedence over the file. The file is parsed, not executed.

| Variable | Meaning |
| --- | --- |
| `JELLYFIN_URL` | Server base URL, e.g. `https://jellyfin.example.com` (required) |
| `JELLYFIN_API_KEY` | API key, **or** instead: |
| `JELLYFIN_OP_ITEM` | 1Password item holding the key |
| `JELLYFIN_OP_VAULT` | Vault containing that item (optional) |
| `JELLYFIN_OP_FIELD` | Field holding the key (default: `credential`) |

Example `~/.config/jellyfin-download.env`:

```bash
JELLYFIN_URL=https://jellyfin.example.com
JELLYFIN_OP_ITEM=Jellyfin
JELLYFIN_OP_VAULT=Private
```

## Usage

```bash
./jellyfin-download.py list series                  # everything
./jellyfin-download.py list movies zombie           # filtered by name

./jellyfin-download.py info "Zombie Land Saga"      # seasons, episodes, sizes
./jellyfin-download.py info -r s2 "Zombie Land Saga"  # preview a range

./jellyfin-download.py "Zombie Land Saga"                      # whole series into ./Zombie Land Saga/
./jellyfin-download.py -r s1e1-s2e8 "Zombie Land Saga" ~/TV    # a range, into ~/TV
./jellyfin-download.py -t movie "Zombieland" ~/Movies          # a movie
./jellyfin-download.py --help
```

### Ranges

`-r` / `--range` accepts one or more comma-separated ranges:

| Range | Selects |
| --- | --- |
| `s1e1-s2e8` | S01E01 through S02E08 |
| `s1-s2` | all of seasons 1 and 2 |
| `s3` | all of season 3 |
| `s2e5` | just S02E05 |
| `s1e3-e7` | S01E03 through S01E07 |
| `s4-` | season 4 onwards |
| `s1e1-e3,s3` | combinations |

Specials are season 0 (`s0`).

### Movies and series with the same name

A name is looked up among both movies and series. An exact (case-insensitive) title match is preferred, otherwise the first search result is used, and any other matches are listed so you can see what else it found. Use `-t movie` or `-t series` to search only one type.

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

MIT
