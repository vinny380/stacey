# Stacey iMessage

Stacey's iMessage interface connects Photon Spectrum to the same assistant instructions,
personal memory and MCP tools used by the Python voice app. Text requests can use timers,
alarms, Spotify, search, sandbox execution and configured MCP integrations when those tools
are enabled in the shared service. Timers and alarms ring on the host computer.

Voice can also use the shared `send_text` tool to send a sign-in link to the owner's phone.
The existing adapter exposes an authenticated, localhost-only bridge on port 8766; it
uses the same private bridge token. With one allowed phone number it is selected automatically.
If there are multiple phone numbers, set `STACY_IMESSAGE_OWNER` to an allowed identifier.
The model cannot choose another recipient. Links and send status persist separately from
personal memory and are available as context when the user texts "done" after a voice request.
`status="sent"` means Photon acknowledged sending, not delivery or account authorization.

## Run

From the repository root, start the shared assistant service:

```sh
uv sync --all-extras --locked
STACY_MESSAGING_ENABLED=true uv run stacy serve
```

Use `stacy start` instead of `serve` to run voice alongside iMessage. The existing root
configuration supplies `OPENAI_API_KEY`, integration credentials and memory paths.
`STACY_TEXT_MODEL` selects the text model (default `gpt-6-luna`). Voice keeps its Realtime model.
Install the `messaging` extra for text-only deployments; `--all-extras` also installs it.

The service creates an owner-only bridge token at `.cache/agent-token`. It never prints the
token. Start the service from the repository root so the adapter finds the same file.
`STACY_AGENT_TOKEN_FILE` can override its path in both processes, or set the same
`STACY_AGENT_TOKEN` in both environments. The bridge binds to localhost.

Then, from `stacey/`:

```sh
bun install
# Only on a fresh checkout; preserve your existing local account configuration.
cp config.example.json config.local.json
# Edit config.local.json with your phone number and/or Apple ID email.
bun start
```

On a fresh checkout, copy `.env.example` to `.env` in `stacey/` and set `PROJECT_ID` and
`PROJECT_SECRET` from the Photon dashboard. Keep an existing `.env` and `config.local.json`
when updating. `STACY_IMESSAGE_ALLOWED_SENDERS` can override the sender list with
comma-separated identifiers.
For a different service port set `STACY_AGENT_URL=http://127.0.0.1:PORT`.

Message the Spectrum line belonging to your project. Only direct inbound text messages from
allowed accounts reach the assistant. Group chats, unknown senders, outbound echoes and
reactions are ignored. Attachments and voice notes receive a text-only explanation.
When a text turn starts processing, Stacey marks the chat read and shows a typing
indicator until her reply finishes. Status-signal failures do not block the request.

## Extend

- `src/interface.ts`: `AgentInterface` is the transport contract; `Agent` is the assistant
  contract. `InterfaceRunner` handles turn ordering per conversation and duplicate messages.
- `src/spectrum-interface.ts`: reusable Spectrum adapter, with sender and space policies.
- `src/imessage.ts`: `IMessageInterface` configures the iMessage provider and DM-only policy.
- `src/client.ts`: authenticated client for the shared local assistant service.
- `../src/stacy/harness/text.py`: `Agent` protocol, normalized `AgentMessage` and `TextAgent` tool loop.
- `../src/stacy/harness/instructions.py`: common instructions consumed by text and voice.

For Slack or Telegram, add a provider adapter extending `SpectrumInterface` with the
appropriate account and conversation policy, then pass it to `InterfaceRunner`. Providers
outside Spectrum implement `AgentInterface`. No tool or memory logic needs to be copied.
Voice uses Pipecat's streaming Realtime pipeline rather than converting audio into this
request/reply loop; it already consumes the shared instructions and the same MCP service.

The memory repository belongs to one owner across all interfaces. It is not a multi-user
assistant. Chat history is scoped to channel, conversation and sender, stored locally in
`conversations.sqlite3`, and survives service restarts. The last 20 completed turns are loaded
for each request. Personal facts saved through the memory tools are shared with voice;
voice does not load the iMessage transcript. Recent outbound texts supply handoff context.
Before each text response and each voice session, the agent reads the memory index and
up to eight linked notes (24,000 characters total) through the memory tools. This supplies identity
and preferences even when the model does not initiate retrieval. Additional notes
remain available through read_memory and search_memory.

Incoming message IDs persist before model/tool execution. Redelivery returns a cached reply
without repeating tools. Failed or interrupted turns are marked uncertain and are never
executed again automatically. Process-local dedup prevents duplicate replies during a run;
a transport redelivery after restart may resend the cached reply. Voice-initiated outbound sends
are recorded before execution in private `notifications.sqlite3`; uncertain sends are never
automatically retried. The adapter deduplicates outbound HTTP request IDs during its lifetime.
There is no provider delivery reconciliation yet. Requests are ordered, not debounced; subsequent
texts wait for the active turn. Do not run multiple backend workers against this SQLite store.

## Validate

```sh
bun run typecheck
bun test
# From the repository root:
uv run --all-extras pytest -q
uv run --all-extras ruff check src tests scripts
```

Tests use fake models and local tools; they do not send iMessages or call live models.

[Spectrum documentation](https://photon.codes/docs/spectrum-ts/introduction)
