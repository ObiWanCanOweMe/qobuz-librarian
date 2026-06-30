# Product

## Register

product

## Users

Qobuz Librarian is for people maintaining a local FLAC music library from Qobuz. They use it as an operational tool: search for music, scan the library, review candidates, approve changes, and let the app download, import, repair, upgrade, downsample, or add lyrics.

## Product Purpose

The app keeps a local music library aligned with Qobuz while protecting existing files. Success means the user understands what a scan found, chooses what should run, and trusts that file-changing work is reviewed, backed up, and verified where needed.

## Brand Personality

Careful, capable, and direct. The interface should feel like a serious library tool for people who care about their collection, not like a generic SaaS dashboard.

## Anti-references

Do not use fake dashboard metrics, decorative charts, profile/avatar filler, notification-centre filler, generic glow-heavy AI styling, DaisyUI-looking component defaults, or an old multi-tool Artist page as primary navigation. Do not make Search responsible for upgrade decisions. Do not hide real tools behind vague buckets.

## Design Principles

- Show the real workflow: scan, review, approve, execute.
- Keep Search for discovery and downloads; keep Library and Quality for collection maintenance.
- Make destructive or file-changing actions deliberate and plainly labelled.
- Treat desktop and mobile as first-class surfaces.
- Use polished, restrained product UI with concise professional copy.
- Build visible UI with project-owned `ql-*` components. Tailwind is only the CSS build pipeline; DaisyUI package/plugin plumbing has been removed and must not be reintroduced.

## Accessibility & Inclusion

Support light and dark themes, keyboard-visible focus, readable contrast, and responsive layouts that do not collapse desktop tables into cramped mobile spreadsheets.
