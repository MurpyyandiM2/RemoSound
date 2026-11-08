# Security

RemoSound runs on your own machine. It opens no port to the internet, ships no
audio, and holds no accounts or secrets.

## What it protects against

- **Hammering a site.** Hard limits live in `remosound/safety.py` and
  `remosound/scraper.py` and apply to every provider, plugins included: 5
  parallel downloads, 0.2 s between download starts, at least 0.5 s between page
  fetches, at most 250 pages per listing, 250 sounds per run and 3000 downloads
  per day.
- **Hostile download links.** An audio link comes from a remote page. Only
  `http(s)` links to public addresses are fetched (redirects are checked hop by
  hop), and one file may not exceed 50 MB.
- **Other web pages driving the studio.** The studio listens on `127.0.0.1`
  only, refuses a foreign `Host` or `Origin` header, accepts changes only as
  JSON, and runs one download at a time.

## What it does not protect against

- **Provider plugins are code.** A plugin is a Python file in
  `~/.remosound/providers` that runs when RemoSound starts. Install only plugins
  you trust, and keep that folder writable by you alone.
- **A determined local user.** The daily budget and limits stop accidents and
  runaway loops. Someone who controls your machine can edit them.
- **DNS that changes its answer** between the address check and the connection.
- **Copyright.** RemoSound cannot tell whether a downloaded sound may be used.
  Check the rights yourself.

## Reporting a problem

Please use GitHub's private vulnerability reporting (Security tab, "Report a
vulnerability") rather than a public issue.
