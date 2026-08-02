# Troubleshooting

[← README](../README.md)

| Symptom | Likely cause / next step |
|---|---|
| `Another Qobuz Librarian run is in progress` | The web app holds the download lock. For a CLI run, switch to terminal mode from **Settings → Mode** (then **Resume web app** after); stopping the container with `docker compose stop qobuz-librarian` is the manual alternative. |
| `Music library unavailable` | Music bind mount unset or wrong. Check `QL_MUSIC_DIR` in `.env` and that the host path exists. |
| Container exits immediately on `up` | Usually a non-numeric `PUID`/`PGID` (a typo makes the entrypoint refuse to start) or a host bind-mount path that does not exist. `docker compose logs qobuz-librarian` shows which. |
| `Volume not writable` (Settings → Diagnostics: FAIL) | `PUID`/`PGID` do not match the host owner. Run `chown -R $(id -u):$(id -g) ./music ./staging`, or set them in `.env`. |
| Library scan says "no artist folders found" | `/music` is mounted at an empty or one-level-off directory. `QL_MUSIC_DIR` must point at the folder that contains the artist folders, not its parent. |
| Token rejected (Save & connect) | Expired, copied with quotes, or trailing whitespace. Get a fresh token from play.qobuz.com (dev tools → Local Storage → `localuser` → `token`) and paste it cleanly. |
| Stalls in "Importing into beets…" | A beets plugin is loaded without its required config block (lastgenre key, replaygain backend). Disable it via `BEETS_PLUGINS` or add the block to `config.yaml`. |
| `docker compose pull` 404 | Image not published under that tag yet. [Build from source](../README.md#development). |
| Healthcheck failing but port reachable | Container could not reach its own `/healthz`. Check resource limits and `docker logs qobuz-librarian`. |
| Upgrade fails with `Permission denied` backing up an album | An earlier `docker exec … beet …` ran as root, leaving root-owned files `PUID 1000` cannot move. Rerun with `docker exec --user 1000:1000 …`, or `sudo chown -R 1000:1000 ./music`. |
| A Library review says an album's release identity needs manual review | Qobuz Librarian could not safely prove one edition, found invalid identity metadata, or detected a path/identity conflict. The card is informational and cannot be selected. Inspect the folder and listed full Qobuz IDs; the app leaves the folder and manifest unchanged rather than guessing or overwriting. |
| An album folder ends in `[qobuz-123…]` | The ordinary friendly path was already owned by a different Qobuz release, so this release received a deterministic collision-only suffix containing its complete normalized Qobuz ID. Albums without a real path collision keep the ordinary friendly name. |
| Files vanished from `/music` after a manual `beet` command | `beet -d /config/beets …` reads `-d` as the destination, so with `move: yes` it relocates the library into the config volume. The container already exports `BEETSDIR`; run `beet …` with no `-d`. |
| `curl` / `cp` / `mkdir` misbehave on Windows | PowerShell's `curl` is an alias for `Invoke-WebRequest` with different flags, and chained commands do not behave the same. Run the setup in WSL or Git Bash. |

## Inspecting a Qobuz release manifest

The reserved manifest is `.qobuz-librarian-release.json` inside the album
folder. To inspect it read-only from the container, substitute the real album
path and run:

```bash
docker compose exec qobuz-librarian \
  cat '/music/Artist/Album (2020)/.qobuz-librarian-release.json'
```

A version 1 file contains only `schema_version`, `provider`, and `release_id`.
Do not edit a manifest, create one by hand, or copy one between releases or
folders. Its release ID is authority for placement, and borrowed or modified
metadata can make the app correctly refuse a scan, import, move, or restore.
Let Qobuz Librarian create it lazily after a proven match. If the file is
malformed or names the wrong release, keep the album unchanged and resolve the
manual-review warning using the original Qobuz release information; the app
will not replace conflicting identity metadata automatically.
# Large migration previews

Migration reviews store detailed per-file safety receipts in `/data/jobs.db`
and keep only compact album references in the ordinary job record. This keeps
large previews restart-safe without constructing a multi-gigabyte SQLite
field. Keep `/data` on durable, writable storage for the entire review and
migration.

If a review reports that its saved preview details are missing or unreadable,
run the scan again. Qobuz Librarian deliberately refuses to reconstruct
execution authority from paths or the human-readable CSV manifest. Raising the
container memory limit does not repair missing or corrupt durable payload rows.
