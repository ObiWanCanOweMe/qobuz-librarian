# Changelog

All notable changes to Qobuz Librarian are recorded here, newest first. The project follows [semantic versioning](https://semver.org/); dates are when each version was tagged during local development.

## [0.11.0] - 2026-07-14

A long reliability pass over everything that moves or deletes files.

- Interrupted downloads, imports, migrations, backups, and Undo now recover by re-checking what's actually on disk; anything that can't be verified is left in place instead of guessed at.
- Undo on a single track removes exactly the file the job recorded, plus any folders it created that are now empty — replaced files, reused folders, and symlinks survive.
- When the data folder can't enforce the single-writer lock, library writes stay paused instead of running unprotected.
- beets 2.12.0 is now required and verified before imports run — it's bundled in the Docker image, so this only affects bare CLI installs, which must provide that exact version. Cleanup stays inside the staging folders the import opened.
- Albums credited to several artists can be re-filed under the primary artist again — the move now survives crashes and never overwrites an existing folder.
- Smaller fixes from just after 0.10.3: review dismissal and mobile disclosure rendering, scans pick up changed candidate settings, cleaner web/terminal handoff coordination, and container user ids are normalised before the privilege drop.

## [0.10.3] - 2026-07-10

Reliability fixes across the app — mostly review-lifecycle fixes, plus clearer waiting states.

- Downloading everything in the Library review no longer resurrects the same albums as "missing" on the next visit: a fully worked-through review retires, and anything that failed to download comes back ticked and ready to retry instead of vanishing until the next scan.
- Failed downloads from a partial approve also fold back into the open review, ticked, rather than surviving only as an error line in a finished job.
- The Library review survives restarts and discarded jobs: it rebuilds from the last scan's saved state, so the page can't end up saying the baseline is ready while showing nothing. A worked-through or discarded review now gets a proper finished page with the tabs still visible, a link to dismissed albums, and a "Bring all back" action.
- New-release batches can no longer leak their albums into the Library review's fold-back paths — their results live and die with their own job.
- The first downsample's keep-or-delete-originals answer now takes effect for the run that asked it. It previously could sit deferred behind the running job, and a "keep" choice risked being applied too late.
- The downsample cap now follows the album itself (by release identity, not folder path), so moving or renaming a downsampled album no longer re-opens it as an upgrade candidate — which would have re-downloaded hi-res you deliberately shed.
- Retrying a parked download review after fixing your Qobuz token in Settings now works without restarting the container; the retry no longer holds onto the dead token it was parked with.
- Undo on a single-track download no longer hangs while a long job (a lyrics scan, a migration) is using the library — it tells you what's busy and to try again after.
- A download waiting behind a library-wide lyrics scan or a migration now says what it's waiting for instead of sitting on "Running" with no explanation.
- The terminal's `--check-new-releases` now applies the same guards as the web check: the catalogue-limit re-baseline, the singles-suppression setting, and the baseline merge behave identically in both.
- Small fixes: the History tab heading, scan progress copy, and the dismissed-count on tab-scoped reviews read correctly; `AUTO_LIBRARY_SCAN` documentation now describes both things it controls.

## [0.10.2] - 2026-07-05

Reliability and interface polish over 0.10.1.

- Filing a downloaded album into its primary-artist folder now copies and verifies each file at the new location before removing the original. On a library spread across two drives, a mid-move failure could previously leave the album split between both folders; the reorganise step is now as crash-safe as the standalone library migration, and reconciles the database only for the files that actually moved.
- Search shows placeholder rows while it fetches results, instead of a lone spinner on an empty panel, so a lookup reads as loading rather than stalled.
- The library and tool reviews are now a light hairline list, one clean row per artist rather than a boxed panel each, which is easier to scan down a long list and on a phone and matches the search results.

## [0.10.1] - 2026-07-04

Fixes from running 0.10.0 against a full-size library, including album-matching and downsample hardening.

- The library review no longer counts an empty folder as an album you already own. A failed download, or a folder whose tracks were deleted, used to make a genuinely-missing album quietly disappear from the review; the same empty-folder check now also gates the Search "in library" tag.
- Downsampling a loud master no longer clips it. The resampler can momentarily overshoot full scale on hot, brickwalled sources, and the rewrite used to hard-clip those peaks into distortion. Those files — and only those — are now eased down by the small amount needed first; quieter, more dynamic albums are untouched.
- The first time you downsample, you choose once whether to keep a restorable copy of your hi-res originals or delete them to save the space. The choice is saved in Settings and applied from then on, so you're never silently left without a backup.
- Approving a review now asks before it queues anything, with the real count in the question — previously one tap on the review button could start every selected download unprompted. Repair reviews confirm the same way.
- Review checkboxes respond immediately on large libraries: each tick used to serialize and write the entire candidate list (tens of megabytes at tens of thousands of candidates) before answering; saves now coalesce in the background.
- The first library scan reports what it's doing from the start — reading albums on disk, checking upgrade quality, fingerprinting folders — instead of sitting on "waiting for output" for minutes while it worked through the pre-scan passes.
- Phone fixes: the bottom tab bar's icons render again (their stroke style was scoped to the desktop nav), pages scroll clear of the bar, the queue badge reads as a badge and hides properly when the count is zero (`hidden` now beats component display rules), whole queue cards are tap targets, and the reconnect notice waits five seconds so backgrounding the app doesn't flash a warning.
- The Upgrade page shows the connect card when Qobuz isn't linked instead of bouncing to Search; the Queue and History pages stopped calling themselves "Activity"; downsample's three overlapping cards merged into one with both actions; scan copy says "Scan library" instead of "run Library"; the setup page's no-login note is a footnote instead of a fake dropdown; and the settings defaults drawer no longer crowds the save button.
- The queue is work in flight: parked reviews moved off it into History (with an Open link back to their review), the Queue badge counts only running and waiting jobs instead of pinning a permanent "1" while a review sits parked, and opening a review no longer asks for confirmation — it's navigation, not an action.
- Big-review handling: each artist row can be dismissed without expanding it, the review tools (select, expand, dismiss unselected, dismissed link) live in one row so the sticky footer holds just the download button, and "Load more artists" loads itself as you scroll.
- Once Qobuz is connected, the Settings credential fields collapse behind "Change account or token" instead of sitting as an open form.
- Confirmations are drawn by the app now instead of the browser's plain popup, logins survive container restarts and image updates, "What's on disk" folds to one line above a parked review instead of hiding entirely, and the Dismissed results page got a plain heading with an inline way back.
- A finished job is saved to disk before its notification hook fires, and the hook runs in the background — a slow webhook could previously leave a completed scan looking stuck and hold up the next job in line.
- Opening a page while signed out returns you to that page after login instead of dropping you on Search.
- The Queue shows its empty state instead of a blank page when the only thing outstanding is a parked review, and its empty-state copy matches the new queue model.
- A parked Repair review gets the same "review ready" dot in the nav that Library, Upgrade, and Downsample reviews get.
- Downsample's "Refresh candidates" says up front when it will replace a review you already have parked, and the toolbar's "Dismissed (N)" link updates as you dismiss instead of waiting for a reload.
- Library migration is reachable from Settings → Library paths (it previously existed only as a direct URL), approving a copy migration asks about copying rather than "downloading", and discarding an Upgrade, Downsample, or migration review returns you to its page instead of the queue.
- The library census notes that its counts come from the last scan.
- History reads like the rest of the app: timestamps say "2 hr ago" (or a short date once they're old, with the exact time in a tooltip), long job summaries trim to three lines, and each row's status, time, and Open link sit on one line instead of stacking. Section counts sit beside their heading rather than floating at the far edge.
- On phones the review footer is one button tall — Discard keeps its tap target but drops the button chrome so downloading stays the only loud action.
- Repair's Scan–Review–Repair steps no longer stretch across the whole page, and the Settings Mode panel lost its box-in-a-box border.
- The Upgrade/Downsample switcher hides when upgrades are unavailable (the nav already did), and status chips use the same capitalisation and names everywhere — History now says "Needs review" like the job page does.
- Settings saves apply immediately when the only outstanding work is a parked review — previously they were silently held until some future job finished, while the page showed the new values as if live. Saves still defer under a genuinely running job.
- Switching to terminal mode works while reviews sit parked — the handoff guard treated a parked review as an active job and refused with a message about a download that wasn't running. Only genuinely running work blocks the switch.
- Downloading an album from Search works even when that album appears among a parked review's candidates — it used to refuse with "Already queued" although nothing was queued. Approving the review later still skips anything that already landed.
- Search's download confirmation uses the app's own dialog like everywhere else instead of the browser's plain popup.
- New-release check results live on their own review page instead of taking over the Library page — the Missing Albums / Gap Fill review stays put, and the dashboard shows a "N new releases" notice that opens the check's results.
- Search deep links with an album or track kind run the search on load like artist links do, the "/" shortcut reaches the search box from any page, the grouped album view names both numbers when editions fold ("94 albums · 99 releases"), and the phone's More sheet closes on Escape.
- After the baseline scan, the Library header keeps a small refresh icon for the occasional case of music added to the folders outside the app. A refresh folds anything new into the review you already have parked — your picks stay exactly as you left them — instead of stacking a second review, and the open page updates live when it finishes. "Force full rescan" moved to Settings → Library maintenance.
- The refresh also keeps the open review honest about the disk: an album you added by hand while it sat parked becomes a Gap Fill row (keeping its tick), a folder you deleted returns to Missing Albums, rows for albums now fully in your library leave the review, results dismissed mid-refresh stay dismissed, and a refresh that couldn't check some artists says so instead of reporting "up to date".
- A download that still lands under your quality target after the automatic retry now marks its History row "Below target quality" and puts a warning dot on the Queue nav until the job page is opened — 0.10.0 promised this flag; now it exists.
- Retry (failed downloads) and Undo (single tracks) work from History for any archived job and survive restarts — they used to quietly vanish once a job aged out of memory.
- Undo on a single-track download works again; the confirmation dialog was swallowing the click, so Continue did nothing.
- Approving any review needs its feature to actually work: dead Qobuz credentials, a missing downsample engine, or upgrades turned off refuse up front with the review left untouched — and a run that can't reach Qobuz before anything downloads puts the review back exactly as it was, picks intact, instead of burying them in a failed job.
- Reviews reopened after a container restart could hand out duplicate internal row ids, and a dismiss then silently deleted unrelated candidates — including ticked ones. Ids now stay unique across restarts and tab splits.
- A rejected Qobuz token no longer hides a parked Library review behind the connect card: the reconnect notice sits above the review, and ticking and dismissing keep working while downloads wait for the connection.
- Restoring dismissed results puts them straight back into the open Library review — restore used to look like a no-op until some future scan — and the page says where they went.
- "Select all" and "Dismiss unselected" respect the review filter: with a filter showing three albums they act on those three, not the whole tab, so a filtered select-all can't silently overwrite a thousand saved ticks.
- The "Discard review" confirmation names the real stake — every pick you've made is lost — instead of reassuring that no files change.
- Search's bulk "Download selected" asks first with the real count, the same way a single download does.
- The Downsample and Lyrics pages show their own scan running instead of an idle "Ready" state, repair's confirmation counts file(s) rather than album(s), the Lyrics "Change" link lands on the right Settings section, and search results remember the table/grid choice.
- Changing the download quality in Settings flags a parked Upgrade review as stale on save, and the next refresh re-derives Upgrade and Downsample candidates under the new policy even for unchanged folders. Downloads themselves always used the new setting immediately.
- Review pages split on a row budget as well as an artist count, so a page of prolific artists can't put thousands of collapsed rows into a phone's DOM at once.
- Stale job links and buttons say "That job is no longer in the record" instead of bouncing silently, a mistyped address renders the app's own error page instead of raw framework JSON, and an interrupted scan's restart note points at the resume that actually exists.
- Winter theme small text and warning chips darkened to meet the readability bar (night already passed), and the result-cap notice tells web users what to actually do instead of naming a CLI-only option.
- The compose file passes `TZ` through (documented beside PUID/PGID) so exact timestamps in History and on job pages can show your local time; relative times were always right.

## [0.10.0] - 2026-07-02

**A new web interface**

- The web UI has been redesigned end to end: a night theme built on gold and espresso, a matching all-light winter theme, a persistent sidebar on desktop, and a bottom tab bar on phones. Search is the front page, and the logo, app icons, and favicon are new to match.
- Search results group each album with its other pressings in an expandable version tree, with cover art, per-release quality, and bulk select. Albums you own are marked "In library", with a toggle to hide them.
- Every page moved onto the same design system: the Library review with its Missing Albums and Gap Fill tabs, Activity with live job tiles, History split into job cards and a downloads table, a restyled Settings that shows music-folder storage, and matching login, setup, and error pages.

**One scan for the whole library**

- "Scan library" now refreshes everything in one pass — missing albums, Gap Fill, quality upgrades, and downsample candidates share a single baseline instead of four separate scans. Quick passes skip unchanged artist folders, an interrupted scan resumes where it stopped, and a scan that couldn't check every artist says so instead of reporting a clean finish.
- The Library page owns the whole flow: the scan launcher, live progress, and the parked review all live there, with dismissed candidates recoverable from its Dismissed page.
- The per-artist Artist page and its scan routes are gone; artist discovery lives in Search, and whole-library work lives on each tool's page. An old parked per-artist review restores as failed with a hint to re-run the scan.

**Downloads verify their quality**

- Each download's staged FLACs are checked against what your quality tier implies before import. When Qobuz under-delivers, the download retries once from the highest source, and a retry that comes back less complete than the first rip is discarded rather than trusted. Anything unresolved is flagged in History instead of slipping into the library silently.

**Quality of life**

- The Library page shows a quality census once a baseline exists: track counts and disk use by tier (CD, hi-res to 96 kHz, hi-res to 192 kHz), which artists hold the most hi-res data, and roughly how much a downsample pass would reclaim. All read from the scan cache — no network.
- New *Keep originals when downsampling* setting (`DOWNSAMPLE_KEEP_ORIGINALS`): the on-demand Downsample parks a verified copy of each hi-res original in the backup area, and Settings → Diagnostics grows a Restore button that undoes the rewrite until the retention window ends. The same button also recovers backups orphaned by a hard kill, which previously required a terminal command.
- New-release checks now run on a background timer, so `NEW_RELEASE_CHECK_INTERVAL` holds on a headless box instead of waiting for someone to open the dashboard.
- `POST_JOB_HOOK` also fires once when the saved Qobuz token stops being accepted (`status: auth_lost`), the image now bundles `curl` so one-line ntfy/Discord hooks work as documented, and the configuration docs gained ready-made recipes.
- Pressing `/` jumps to the search box from anywhere, and the installed app's icon offers Library and Queue shortcuts on a long-press.

**Fixes**

- Rapid review ticks no longer get lost while the same review is open in another tab, and cross-tab sync stays live.
- Saving Settings stores only the fields you changed, so later edits to `.env`/Compose values keep applying.
- A scan page that reconnects keeps its "across N artists" tally instead of restarting the count.
- The job log console matches the app theme, live updates on Library and Repair no longer reintroduce a duplicate page header, and a mixed-quality upgrade candidate names its low end instead of reading as a no-op.
- The browser theme-color, favicon, and app icons follow the active theme and cache-bust on release, so a stale icon doesn't linger after an update.

**Internals**

- The image runs on Python 3.14; beets 2.12, htmx 2.0.4, and refreshed dependency locks; slimmer logo, icon, and font assets (the mono font ships as WOFF2).

## [0.9.4] - 2026-06-25

**Search and review**

- Search results are grouped by album and show what is already in your library. A matched album shows "In library" with no download button; other editions (remaster, deluxe, a live take) are grouped under expandable "other versions" you can still download. Quality upgrades stay on the Upgrade page, keeping search focused on finding music. Albums filed under a collaboration folder are now matched correctly.
- Closing a scan review now returns to the queue with the scan parked and reopenable; discarding is a separate, confirmed action. Select-all, clear-all, and "dismiss unselected" are available from the review screen, with dismissed albums recoverable from the Dismissed albums page.
- Dismissing a review's last album completes it instead of leaving the job stuck on an empty list with an old "0 new releases" banner.
- "New releases" means new to the saved Qobuz catalogue baseline and within the release-age window, so a back-filled old album no longer shows as new (window `NEW_RELEASE_MAX_AGE_DAYS`, default 365 days).

**Library and tools**

- The Library page now treats the full scan as a one-time baseline. After the baseline exists, new-release checks become the main action, while a full re-scan remains available when needed.
- The queue and history spell out destructive actions: bulk actions show how many they affect ("Cancel all N jobs", "Clear all N finished jobs") and each per-job control says what it does (remove from queue, stop, or discard the scan).
- Cancelling a queued job takes effect at once: it leaves the queue the moment you cancel it, instead of sitting as "Queued" until the job ahead of it finishes.
- The Library and maintenance tool pages use consistent naming and warnings; Downsample and Lyrics run without a Qobuz token; an unconnected account gets a setup prompt rather than a warning; and Settings holds back the operational toggles until a token is saved.

**Polish and fixes**

- The search box is one joined bar at every width, the dashboard leads with search and recent activity, a single-track release reads "1 track" rather than "1 tracks", and a failed download says what actually went wrong rather than a catch-all.
- The navigation menu closes when you tap outside it, not only when you tap the button again, and the dismissed-albums list moved out of the menu (it stays one tap away on the Library page and after any review).
- Approving a review with nothing selected used to flip the job to done over an empty set; it now keeps the job in review and says nothing was selected.
- Saved Qobuz credentials are flushed to disk durably, the dismissed-album store is safe against two processes writing it at once, and the lyrics pass no longer prints raw status codes and counter dicts to the log.
- Mobile polish: dismissed-album artist names no longer truncate to "R…", history lines wrap instead of clipping, and small-screen headers stack cleanly.

**Setup and docs**

- compose.yaml forwards the documented `.env` knobs it previously dropped, and a new `WEB_BIND` sets the host interface the UI binds to. The README, configuration docs, and example env are brought in line with the current UI.

## [0.9.3] - 2026-06-23

**Data-safety polish**

- In-place migration to a destination short on space is blocked before files move, matching the CLI safeguard. The Migrate screen includes an explicit low-space override. Copy mode still warns rather than blocks, because the source library is left intact.
- The migration space preview now counts the cover art, booklets, and `.cue`/`.log` sidecars that get carried alongside the audio, so the estimate matches what the copy actually writes — previously a library with large booklets could see an estimate that was too low, and an in-place move could read "0 bytes" while still copying art.
- When a parked album finally imports on a retry, any non-audio companions it left behind (booklets, scans, cover art) are now preserved outside the staging folder before cleanup — the same protection the upgrade path already had.

**Correctness**

- An artist's discography no longer stops paginating early if Qobuz returns a page with a few malformed entries mixed in, which could silently hide some of that artist's albums during a scan.
- Fuzzy-match thresholds set via the environment are clamped to their valid 0–1 range, so a typo like `CONSOLIDATE_THRESH=-1` can't quietly turn duplicate cleanup into "match everything."
- The gap-fill "will downsample to…" note now respects your download-quality tier — at CD-lossless it no longer promises a downsample that won't happen.
- Saved Qobuz credentials are now flushed to disk durably, matching the web-login credential write, so a crash right after saving can't roll back a token the UI reported as saved.
- The hidden/single-album store is now safe against two processes writing it at once (a web dismissal during a CLI hand-off), via a cross-process lock and unique temp files.

**Setup, docs, and release polish**

- `compose.yaml` now forwards the documented `.env` knobs that it previously dropped — `WEB_AUTH_PASSWORD_FILE`, the free-space floor, the repair cache/pacing settings, beets path/plugin overrides, and the live-album filter — so setting them in `.env` actually takes effect.
- New `WEB_BIND` controls the host interface the UI is published on; set `WEB_BIND=127.0.0.1` to keep it off the LAN. `WEB_HOST` remains the in-container bind for non-Docker runs.
- New releases are described everywhere as listed for review rather than "pre-ticked": the review screen leaves them un-ticked so they cannot all be queued by accident.
- Configuration docs now state the real new-release and catalogue-cache defaults, clarify that Settings covers the common behaviour knobs while advanced ones stay in `.env`/Compose, and fix the migration-results filename, lock-handoff, and CLI-container wording. The Docker image's licence metadata now reflects the third-party (GPL) tools it bundles, and the release smoke test verifies the compiled stylesheet is actually served.

## [0.9.2] - 2026-06-23

**New-release check needs a baseline first**

- New-release checks now require the baseline produced by a full library scan. Until that baseline exists, "Check for new releases" is disabled with an explanatory note, direct requests are refused, and the automatic daily check waits behind an interrupted baseline scan.

## [0.9.1] - 2026-06-23

**Reviews no longer duplicate**

- Re-running a scan — repair, library gap-fill, upgrade, or downsample — replaces its earlier pending review instead of stacking duplicate review cards on the dashboard.

**New-release checks use the saved baseline**

- The new-release check flags albums in an artist's catalogue that are not in the baseline, including older albums newly added to Qobuz. Candidates default to un-ticked so a review cannot queue a large set of downloads in one tap.

**Repair scan — cleaner live activity**

- A whole-library repair scan now shows its progress as a single status line under the progress bar — `Scanning "<artist>" · N albums · M flagged`, refreshing a couple of times a second — instead of appending a "still scanning…" line to the activity log every few seconds. The activity log now lists only flagged albums (the actual findings), and a finished scan no longer keeps hundreds of heartbeat lines.

**Repair runs on one page**

- A repair scan now stays on the Repair page from start to finish — scanning, reviewing the flagged albums, and the repair itself all happen there and update live, instead of handing you off to a separate job page partway through. A parked review is no longer reachable only behind a "Start scan" button that would have discarded it.

**Clearer job status**

- A queued job now shows what it is waiting behind instead of only "Queued"; multi-album jobs keep progress on the full run rather than resetting per album; and the Upgrade, Downsample, and Lyrics pages show when they last ran.

**Safety fixes**

- Undo on a single-track download can no longer remove a same-numbered track from a different disc of a multi-disc album. A flood of failed logins can no longer lock the admin out — a request that already carries a valid session skips the limit — and an unreadable credentials file now fails closed instead of re-opening first-run setup. A library migration to a destination short on free space no longer starts an unattended run that would relocate files until it ran out.
- Upgrading an album now carries its booklets, scans, `.cue`/`.log`, and hand-placed cover art into the rebuilt folder instead of discarding them, matching the single-album path. Consolidation moves overlapping tracks to a recoverable backup rather than deleting them, repair no longer removes an album folder it failed to recognise, and a near-full disk stops the download queue cleanly for a retry rather than failing each album in turn.

## [0.9.0] - 2026-06-21

The repair scan was rebuilt for broader detection, faster scans, and clearer live progress. This release also includes reliability and safety fixes. The only changed default is removal of the unusable 320 kbps tier.

**Repair catches truncated files that still play**

- The whole-library repair scan now checks every track's length against its exact Qobuz recording, not only visibly short files. A track cut short at a frame boundary, with its FLAC header rewritten to the shorter length, decodes cleanly and passes the size check — so the old sweep marked it intact and moved on, and a genuinely damaged album could scan green. Every ISRC-tagged track is now duration-verified (the command-line sweep too).

**Faster, and re-scans skip the network**

- The sweep now checks several artists at once instead of one at a time, making the first scan of a large library several times quicker. Per-track Qobuz lookups are cached: a re-scan, or any album that shares a track's ISRC, skips the network round trip. Files are still decode-tested fresh on every scan, so new corruption is still detected. Set `REPAIR_CACHE_ENABLED=false` to skip the lookup cache, or `REPAIR_CACHE_TTL_DAYS` to change how often a cached lookup re-verifies against Qobuz.

**Clearer repair progress**

- A clean library can produce long periods with no findings. The scan now shows the current album, a periodic "still scanning — checked N albums…" heartbeat, and an elapsed clock, with the activity log open by default.

**Fresh downloads are double-checked**

- After an album finishes downloading, its track lengths are re-checked against Qobuz. The downloader already discards tracks that won't decode, but a clean truncation (decodes fine, header rewritten short) could slip past that — now it's caught right after the download with a note to repair it, instead of waiting to be found by a later scan.

**Backups verify contents, not just size**

- Cross-filesystem backup, restore, and gap-fill now verify the copy by hashing its contents before the original is deleted, instead of trusting a matching file count and total byte size. A same-size corruption — a transfer glitch, or a partial write re-padded back to length — used to pass the size check, and the source was then removed, leaving the damaged copy as the only one. The copy is now compared byte-for-byte and any mismatch aborts the operation with the original left untouched.

**Download quality**

- The 320 kbps MP3 tier is removed. The pipeline is FLAC-only and the post-download cleanup discards any non-FLAC file, so choosing that tier downloaded each track and then deleted it — the setting silently fetched nothing. It's gone from Settings and the docs, and an existing `STREAMRIP_QUALITY=1` is now coerced to CD lossless (the smallest lossless tier) with a clear message rather than passed straight through.

**Container runs as the user you asked for**

- A non-numeric `PUID`/`PGID` (a typo) used to log a warning and then silently run the container as root, defeating the non-root isolation. It now refuses to start; running as root requires the explicit, valid pair `PUID=0 PGID=0`.

**Review selection matches the server**

- Select-all and the per-artist select now tick boxes only after the server confirms each save. A failed save used to leave boxes ticked while the server held none, so approval acted on a selection you never really made; a failure now flags the affected boxes and leaves the rest alone so you can retry.

## [0.8.0] - 2026-06-20

Quality-of-life and reliability improvements across search, scanning, and the web UI.

**Search & scanning**

- Search returns more results, so big artists surface properly.
- Whole-library scans now show the full set instead of capping the list, and prolific artists are no longer cut short.
- Artists sort by name ignoring a leading "The"/"A"/"An" (so "The Beatles" files under B).

**Web UI**

- The Search page lays out correctly on narrow phone screens.
- Improved web UI responsiveness under load and fixed list/pagination edge cases.

**Under the hood**

- A range of correctness and reliability fixes across downloads and library maintenance, plus tighter build checks.

## [0.7.0] - 2026-06-18

Strengthens the library repair scan so it can no longer report a corrupt file as intact, plus two smaller correctness fixes. No changed defaults.

**Repair scan**

- The whole-library repair scan now decode-tests every FLAC instead of trusting its size and STREAMINFO header. A file with frame-CRC damage or a zeroed-out middle keeps its original size and reported duration, so the old size-and-header check passed it as "verified intact" and the scan reported no damage. Every file is now run through `flac -t` locally (no network): a clean file still costs no Qobuz call, a file that won't decode is surfaced and refilled, and when the `flac` tool is missing a file is counted "unverified" rather than silently "ok". The scan summary now reports what was actually decode-verified.

**Offline page**

- The offline page's Retry button works again. It loaded a small script that was never shipped in the image, so the button did nothing; it is now a plain link that still works while the service worker is serving the page.

**Dismissed-album list**

- A corrupt hidden-albums file is now moved aside to a `.corrupt` copy with a warning instead of being silently overwritten by the next dismissal. Previously one unreadable read returned an empty list and the next hide or restore wrote a fresh file over it, destroying a dismissed-album list curated over weeks with no trace.

## [0.6.1] - 2026-06-13

Bugfix release — seventeen edge-case fixes, no new features or changed defaults.

**Backup safety**

- The age sweep now proves each track in an upgrade backup is actually back at its origin path — same relative filename, at least as many bytes — before reaping the backup. File-count matching was fooled when a gap-fill or other operation added a different file to the origin while one of the backup's own tracks was still missing there. Previously that could silently destroy the only surviving copy of the unreturned track.
- An upgrade backup kept because the re-rip couldn't be verified as complete (e.g. a truncated-but-decodable track shrank the playtime) now gets an explicit keep-marker. A same-count, larger hi-res re-rip could look redundant by bytes alone and be reaped on the next sweep; the marker stops that.
- The beets import override now always forces `move: yes`. A user beets config with `copy: yes` was silently leaving every newly-downloaded album in staging, which the pipeline's success check read as "import failed" and parked.
- Retrying parked albums now checks whether the audio actually left disk before removing the parking entry. A beets run that exits 0 while skipping a library duplicate (under `duplicate_action: skip`) used to trigger cleanup on the strength of the exit code alone, deleting the only copy.

**Single-track download and undo**

- Downloading the last missing track of an album now clears the "downloaded single" mark an earlier partial download may have left. Without this the album's artist stayed hidden from bulk scans and the new-release check even after you completed the album.
- The upgrade walk now keys the "skip downloaded singles" check on the Qobuz artist name, not the folder name. A folder called "Beatles" where Qobuz says "The Beatles" was leaking the downloaded single back into upgrade candidates.
- The single-track undo now takes the cross-process run lock before deleting any files or touching the beets database.
- The undo track-match now uses the `tracknumber` field (the one `read_album_dir` actually writes). Also, two tracks with no ISRC and no track number on record can no longer accidentally match each other and delete the wrong file.

**Consolidation and repair**

- Consolidation stops immediately under `--dry-run` — it deletes overlapping tracks, so letting it run was a dry-run violation.
- Repair stops under `--dry-run` before moving any files aside, for the same reason: repair moves the truncated originals out of the way before re-ripping, so an interrupt could have stranded them.
- A sibling FLAC whose quality cannot be read (broken STREAMINFO or no title tag) now shows as "quality unreadable" and requires the same explicit DELETE confirmation as a clearly better-quality track. Previously it was silently counted as safe to delete.

**Web and CLI polish**

- The settings page keeps the token you just typed in the (masked) field when Qobuz rejects it, so you can fix a paste slip without re-entering the whole thing.
- Pasting an album URL into Tracks mode now shows a clear "album URL — switch to Albums" message instead of a silent empty result.
- An interrupted repair scan now tells you to start the repair scan again (which resumes from the checkpoint), not the library scan.

## [0.6.0] - 2026-06-09

- **Single-track downloads.** Search has a Tracks mode with a *Get track* button that pulls one track into the right `Artist/Album (Year)/` folder, never a full-album rip. It's recorded as a deliberate single, so scans and the new-release check don't treat that artist as one you're collecting; finish the album later and it files as a normal complete album. **Undo** removes the track and any empty folder it created, and the Upgrade walk leaves single-track downloads alone unless you set `UPGRADE_SINGLES_ENABLED`.
- Two quick retries of the same album can no longer double-queue it; retry now re-checks for a job already touching that album under the submit lock.
- The downsample step caps the ffmpeg encode at ten minutes, so a track on a hung NFS or FUSE mount fails with a clear message and leaves the original untouched instead of pinning a worker forever.
- Behind a reverse proxy the entrypoint passes `--proxy-headers` and honours `FORWARDED_ALLOW_IPS`, so the login rate-limiter sees each client's real address instead of the proxy's and stops locking everyone out at once.

## [0.5.0] - 2026-06-05

First packaged release during local development. Major additions included:

- **Migrate** mode turns an existing or partially tagged collection into the `Artist/Album (Year)/` layout the rest of the tool expects. It reads each file's tags first and can fall back to AcoustID fingerprinting; copy mode leaves the originals in place, and anything it cannot place confidently is left alone and listed in a manifest.
- ISRC-anchored **repair** now snapshots a truncated file's tags before it goes and restores them onto the refilled track, and backs up the source by ISRC before replacing it — a crash mid-refill can no longer strand a track.
- The awaiting-review list pages by artist and keeps its selection on the server, so approving thousands of candidates no longer rides on form state.
- Lyric state and the retry manifest are locked across processes; rejected staging files are quarantined instead of silently left in place.

## [0.4.1] - 2026-05-27

- A corrupt fetch-log line can no longer 500 the dashboard.
- `Retry-After: 0` from Qobuz is honoured instead of being treated as no header.
- An unrecognised `STREAMRIP_QUALITY` warns loudly rather than defaulting to the most permissive cap.

## [0.4.0] - 2026-05-21

- **Check for new releases** — across the whole library or one artist — compares each artist's current Qobuz catalogue against the saved baseline and surfaces albums newly added to that baseline, flagged for review. It reads the catalogue listing alone, so it's about one API call per artist.
- On-disk caches (album fetches, parsed FLAC tags keyed on path+mtime+size, and artist catalogues with a TTL) turn a re-scan of an unchanged library into seconds instead of minutes.
- Jobs survive a container restart: an awaiting-review list comes back, and an interrupted job returns marked as such with a retry hint instead of vanishing.

## [0.3.1] - 2026-04-30

- Multi-disc folders detect disc numbers for non-FLAC tracks.
- Two upgrade-backup restore edges (equal-byte and empty-backup-dir) no longer block automatic recovery.

## [0.3.0] - 2026-04-28

- **Upgrade** mode re-rips albums Qobuz can now serve at a higher quality, backing up the originals first.
- **Downsample** mode shrinks hi-res FLACs above 44.1/48 kHz, each verified to decode cleanly before it replaces the original.
- **Repair** finds truncated or short FLACs and refills exact tracks by ISRC when matching is safe, leaving good files untouched.
- **Lyrics** mode backfills lyrics across tracks already on disk.

## [0.2.1] - 2026-04-03

- The dashboard's stale-token banner flips the moment the API rejects the token, instead of only checking at startup.
- Cancelling a queued download stops cleanly instead of leaving a half-finished album to be swept into a later import.

## [0.2.0] - 2026-03-26

- A web UI (FastAPI) for searching, downloading and watching jobs stream their log live, alongside the existing CLI.
- A crash-safe persistent download queue that resumes after a restart, with a shared-data run lock so the web app and CLI in one stack cannot write at the same time.
- Whole-library and per-artist gap scans that list every missing album.
- Ships as a multi-stage Docker image with a compose stack.

## [0.1.0] - 2026-01-29

- First working version: download a single Qobuz album or a whole artist, scan a local library to know what's already there, and import cleanly with beets so only the genuinely missing tracks are fetched.
