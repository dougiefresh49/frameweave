# YouTube fetch notes

How frameweave downloads YouTube media today, what a 403 wave looks like, and
the deferred PO-token plan. Implementation lives in
`src/frameweave/sources/youtube.py`. There is no `sources/potoken.py` yet.

## Client chain

Player clients are tried in this order (voice-lab verified 2026-08-17):

1. `android`
2. `mweb`
3. `web`

`CLIENT_CHAIN = ("android", "mweb", "web")`. The default yt-dlp `android_vr`
client hands out CDN URLs that 403; pin yt-dlp at or above 2026.08.19 (locked in
`uv.lock`) and keep this order. Only HTTP 403 and "requested format not
available" advance to the next client. Permanent errors (private, removed,
age-gated, members-only, copyright) stop the chain.

## Format ladder

Each client first tries the adaptive/progressive ladder:

`bv*[height<=720]+ba/b[height<=720]/b`

If that client is the last in the chain and still advances, one degraded
attempt uses progressive-only `b`. A degraded success writes
`"degraded": true` into `sources/<id>/media.json` so a low-quality pull is
never mistaken for a clean one.

## Degraded marker

`media.json` always records `client`, `sha256`, `bytes`, and `fetched_at`. When
the progressive fallback was required, it also sets `degraded: true`. Downstream
stages treat the file as normal media; the marker is for humans and agents
reading the cache.

## What a 403 wave looks like in the log

A wave is every client in the chain failing the ladder with 403 (or format
unavailable) before either a later client succeeds or the last client's
degraded progressive attempt also fails.

In practice you see yt-dlp invoked once per client with
`--extractor-args youtube:player_client=<name>`, stderr containing
`HTTP Error 403` (or format-unavailable), then the next client. On success the
logger emits `youtube fetch used player_client=<name> path=...`. On total
failure the stage raises with the last error; it does not hang.

If `android` itself starts returning 403 on consecutive real runs, that is the
trigger to land the PO-token hook below — not a silent retry forever.

## PO-token plan (`FRAMEWEAVE_POT_PROVIDER`)

Deferred until the android client fails on two consecutive real runs. When
needed: add `sources/potoken.py` so that when `FRAMEWEAVE_POT_PROVIDER` is set,
the mweb client is configured with the named yt-dlp PO-token provider plugin
(tokens are per video id; bgutil-ytdlp-pot-provider is the usual example),
`doctor` reports whether the provider answers, and when the variable is unset
the client chain order stays exactly `android → mweb → web` (regression-tested).
Unset remains the default; the hook must not change behavior for ordinary runs.
