---
name: spotify
description: Play music, pause, resume or skip Spotify tracks on an Echo or other chosen playback device.
---

Prefer advertised `composio_spotify_*` tools for Echo and external Spotify devices.
They own discovery, schema caching, account selection and fresh verification; do not
precede them with MCP/Composio searches. Soloist's `spotify_control`/`spotify_status`
control only its own player and cannot verify an Echo.

- Genre/mood: call `composio_spotify_play(query, device_name, account)` directly.
- Named song/album: `composio_spotify_find` with explicit `kind='track'` or `'album'`;
  choose an actual returned URI, then `composio_spotify_control(action='play', ...)`.
- Named catalog playlist: find with `kind='playlist'`. “My playlist” means the user's
  library, not a public catalog result: use connected-app discovery for library playlists
  if not retained, resolve the real playlist URI, then control playback.
- Pause/resume/skip: retain the previous integration, account and device. Call control
  with `action='pause'`, `'play'`, `'next'` or `'previous'` and those explicit IDs.
  “Stop it” pauses the previous playback. A next/previous action skips once.

These tools return `verified=true` only after a separate fresh state read. Confirm once;
do not add another status call after verification. For unverified/uncertain results,
only `composio_spotify_read(account)` and check the device/track; never repeat a command
to obtain confirmation. Missing capability/schema means use generic discovery for the
requested control, not a claim that Spotify cannot do it. Read connected-apps when
generic discovery or authorization is needed. Ask only on account/device/music ambiguity.
