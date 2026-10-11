# Stacy

The iMessage interface lives in [`stacey/`](stacey/README.md). It shares the voice
assistant's instructions, tools and personal memory through the local service.
See that README for startup and the reusable channel interface.

Stacy is a headless personal assistant built around Pipecat, targeting a Raspberry Pi with a microphone and speaker. The laptop is the development environment. The MVP combines conversational voice with automatic personal memory, Spotify, web search, timers and alarms, skills, MCP integrations, and an agent-owned code sandbox.

## Product interface and setup

Voice and chat are Stacy's entire product interface. The iMessage adapter is included
in this repository. There is no Stacy dashboard, browser setup page or companion app.
The standing design decisions are recorded in [AGENTS.md](AGENTS.md).

The intended account-connection flow is conversational: ask Stacy to connect an
account, receive the provider's sign-in link over chat, authorize on the phone,
then receive confirmation after Stacy verifies the connection. Additional Google
accounts must stay distinct. Custom MCP connections should also be added through
the conversation, with configuration maintained internally and new connections
usable without a manual restart.

Authentication must work when Stacy runs on a separate device. Opening the Pi's
browser or sending a localhost callback to the user's phone is not an onboarding
solution. Provider sign-in and consent pages on the phone are allowed; Stacy does
not have a separate UI. Persistent connection state and credentials belong in
operational storage, separate from personal memory.

Google and Slack account onboarding uses Composio Connect's existing MCP tools.
With a consumer `COMPOSIO_API_KEY` configured on the device, text Stacey "Connect my
Google Calendar." She discovers the toolkit and requests a provider-hosted sign-in
link when needed. Open the link on the phone, authorize, and text "done"; she checks
the provider's account status before confirming. No code needs to be copied back.
Ask to connect another calendar account to add it separately. Gmail, Calendar,
Sheets and Drive may use separate toolkits; the provider consent page shows access.

Composio manages connections and aliases under the consumer account, separate from
personal memory. Connected apps use the existing MCP discovery tools and become
available without restarting the assistant. Authentication headers remain internal;
only the selected tools' schemas reach the agent. An authorization link alone does
not prove a connection succeeded. A voice request uses `send_text` to hand the link to
the existing iMessage adapter. Completion is checked when the user says or texts "done";
spontaneous completion monitoring is not implemented yet.

Custom MCP conversational onboarding is still pending. The commands and config
examples below describe developer setup and the current runtime; they are not
the intended end-user experience.

## Run locally

Use Python 3.11–3.13, [uv](https://docs.astral.sh/uv/), and Git. On macOS, install PortAudio before installing the voice dependencies:

```sh
brew install portaudio
uv sync --all-extras --locked
uv run stacy doctor
```

For a tools-and-alarms installation without voice or external integrations, use `uv sync --locked`. Linux needs `aplay` for alarms; building the voice dependencies also needs PortAudio development headers and Python development headers. Raspberry Pi installation and audio hardware are not validated yet.

No credentials are needed to run the local service:

```sh
uv run stacy serve
```

In another terminal, try the same alarm tools the voice agent uses:

```sh
uv run stacy timer 600 --label "Tea"
uv run stacy alarm 07:00
uv run stacy list
uv run stacy cancel <id>
uv run stacy stop
uv run stacy snooze <id> --seconds 300
```

`stop` silences all currently ringing events. `snooze` requires a ringing event. Alarms are one-time; recurring schedules are deferred. Use `stacy list --all` to include completed, cancelled and missed events. Keep the service running and the computer awake to hear alarms. Pending future events are restored on restart; deadlines missed while the service was stopped are marked missed. Events already ringing resume ringing on restart.

The service binds to `127.0.0.1:8765`, with MCP at `/mcp`. For a different port, put `--port` before the command in both terminals. This localhost HTTP endpoint is a development/tool connection, not a product UI.

## Enable live services later

Copy `.env.example` to `.env` and fill in credentials locally. The example defines the timezone, model, voice and optional storage locations. Then run:

```sh
uv run --all-extras stacy start
```

`start` launches the local service and the Pipecat microphone/speaker pipeline, or attaches voice if a Stacy backend is already running. It requires `OPENAI_API_KEY` for live voice. `serve` runs tools and alarms alone; `voice` attaches a voice session to an already running service. An existing backend is verified before constructing another scheduler. If voice fails, the local service keeps running.

The local MCP HTTP endpoint is stateless: an idle voice client can keep using its tools
after the backend restarts, without retaining an invalid transport session. Application
state remains in its existing stores. Commands interrupted during a restart remain
unconfirmed and are not automatically replayed.

`OPENAI_API_KEY` must be a direct OpenAI API key. Voice uses OpenAI Realtime (`STACY_REALTIME_MODEL=gpt-realtime-2.1`, `STACY_VOICE=marin`) with native turn detection and 24 kHz audio. Pipecat handles the conversation and MCP tools; separate transcription and speech-generation services are not required.

Audio defaults to speaker mode (`STACY_AUDIO_MODE=speaker`). On macOS, Stacy uses one
native `AVAudioEngine` for capture and playback with Apple's echo cancellation enabled.
The microphone stays open while she speaks, so you can interrupt. Pipecat's normal
Realtime speech detection stops the reply and clears speech queued on the audio device;
capture keeps running and the next reply can play. Work sounds use the same render path.
The internal helper is compiled and cached automatically using Apple's command line tools.
It saves no microphone recordings and needs no browser or companion app.

If native audio cannot initialize, Stacy reports the fallback and uses
[Pipecat's user mute strategy](https://docs.pipecat.ai/api-reference/server/utilities/turn-management/user-mute-strategies)
during playback plus a 350 ms echo tail. This fallback also applies to Linux/Pi until a
verified echo-cancelling capture/render pair is wired. In fallback mode, let her finish
before speaking. A device change that invalidates active native audio stops the voice
session rather than forwarding unprocessed microphone audio.

For development with headphones, `STACY_AUDIO_MODE=headphones` retains simultaneous
listening and natural interruptions. The local PyAudio transport does not provide acoustic
echo cancellation. [OpenAI input noise reduction](https://developers.openai.com/api/reference/resources/realtime/client-events)
is enabled with `STACY_NOISE_REDUCTION=far_field` for room microphones; `near_field` is
intended for headsets. That filtering does not replace echo cancellation. See Apple's
[voice-processing APIs](https://developer.apple.com/videos/play/wwdc2019/510/) and
[PipeWire's echo-cancel module](https://docs.pipewire.org/page_module_echo_cancel.html)
for the Mac and Pi audio paths.

Normal voice logging uses INFO. Set `STACY_LOG_LEVEL=DEBUG` for detailed diagnostics, including prompts and loaded personal memory. Pipecat's "realtime mode — mutated turn strategies" warning describes an automatic SDK adjustment; it does not indicate failed startup.

Alarm creation prints `Saved timer` or `Saved alarm` with the ID and local deadline; firing prints `Ringing`. If a spoken request seems to do nothing, run `uv run stacy list` in another terminal while Stacy is running. This reads the actual scheduled state rather than relying on a spoken confirmation.

Each voice session prints and saves an owner-only JSONL log in `$STACY_DATA_DIR/traces` (default `~/.local/share/stacy/traces`). It includes recognized user speech, generated assistant transcripts, tool call IDs/arguments/results, interruptions and errors, without raw audio or the system prompt. Generated transcripts may include speech interrupted before playback finished. Pipecat's existing function-call and error observers supply tool lifecycle events.

[Pipecat OpenTelemetry](https://docs.pipecat.ai/api-reference/server/utilities/opentelemetry) is enabled with metrics and turn tracking. Its SDK exporter saves a second local `otel-*.jsonl` file; an attribute allowlist excludes raw prompts, messages, URLs and exception details. No collector is needed. Negative SDK duration values are excluded, and voice logs separately measure user-turn-end to first/subsequent response audio. Set `STACY_OTEL_ENABLED=false` to disable spans. Conversation/tool logs remain enabled for debugging. Existing historical trace files are not rewritten.

Voice uses Pipecat's [shared context and aggregators](https://docs.pipecat.ai/pipecat/learn/context-management), with the assistant aggregator after speaker output. Personality and startup memory stay in `system_instruction` outside the compressible conversation. Realtime's reported input usage requests [Pipecat summarization](https://docs.pipecat.ai/pipecat/fundamentals/context-summarization#on-demand-summarization) at **100,000 tokens**, including audio, instructions and tools. Transcript estimates and message-count triggers are disabled. A dedicated `gpt-4.1-mini` background service generates a summary of up to 1,500 tokens; six recent messages and incomplete tool-call sequences stay intact. `STACY_SUMMARY_MODEL` overrides that background model. The Realtime session disables automatic history truncation and caps each response at 4,096 tokens; a local text-only server test retained 105,217 input tokens with these settings.

The installed Realtime adapter keeps a separate server-side history. A small bridge mirrors Pipecat's summarized prefix with typed Realtime create/delete events without requesting another response. It matches message roles and tool-call IDs, retains recent/pending work, and restores local history if the prefix cannot be matched safely. Such a mismatch logs `context_summary_sync_skipped`; it does not count as successful compaction. Pipecat's [Flows context strategies](https://docs.pipecat.ai/pipecat/flows/context-strategies) apply to node transitions; this open-ended assistant does not currently use Flows or the deprecated `RESET_WITH_SUMMARY` strategy.

A quiet typing loop plays through Pipecat's audio mixer after 650 ms of silent processing. It stops during user/assistant speech, after a completed answer, on interruption, and at shutdown. It never enters speech context or alters non-silent speech PCM. Set `STACY_WORK_SOUND=off` to disable it. Physical speaker/microphone behavior still needs listening tests.

Text requests save owner-only `text-*.jsonl` diagnostic traces in `$STACY_DATA_DIR/traces`.
Version 2 traces record queue/context/model/tool timing, model request IDs and output types,
token usage (including reasoning/cache tokens), tool call IDs, redacted arguments/results,
underlying MCP operations (including adapter-expanded calls), and discovery cache hits.
Payloads are bounded; omitted items and truncated strings are marked. Credential fields,
configured environment secrets and HTTP links are redacted, including embedded JSON.
Chat transcripts, system prompts and private memory tool payloads are omitted.
A hashed message reference correlates backend traces with the iMessage adapter's JSON
lifecycle logs: receipt, queue wait, backend wait, and provider send acknowledgement.
Acknowledgement does not prove delivery to the phone. Existing traces are not rewritten.

Text conversations retain a bounded operational working context from completed tool
exchanges, separate from personal memory. The latest three tool-bearing turns are eligible
for 15 minutes, within a 48,000-character budget. Complete exchanges are dropped when too
large; schemas are never cut in half. This gives follow-ups access to discovered schemas,
provider sessions and resolved targets without replaying actions. Authentication, private
memory and messaging payloads are omitted; credentials and HTTP links are redacted.
Historical playback/account status still requires fresh verification. Uncertain turns clear
this working context. A developer benchmark compares the production text loop against
simulated Spotify tools: `python scripts/check_text_harness.py --repeats 2`. It uses normal
model API credits and makes no real Spotify calls or iMessage sends.
The `--experiment routing` evaluation advertises both Composio and a separate Soloist
player. Its checks reject wrong-player calls, missing schemas, duplicate writes and
verification in the same parallel batch as the write. Music follow-ups keep the preceding
device/account/integration; instructions require complete app schemas and explicit device
IDs on controls that support them. These instructions are evaluated behavior, not a
general deterministic routing or schema-validation guarantee for every provider.

Composio Spotify exposes four shared workflow tools: play a genre/mood playlist in one call,
find music/device choices, control playback with verification, and read playback.
Open music requests use Spotify's top ranked matching playlist; exact track/album requests
keep the find/choose/control path. The harness resolves current schemas and an
explicit active account internally, caches capabilities for five minutes, and returns compact
results. Controls include play/resume, pause, next and previous. A control targets the supplied device, executes once, and then awaits a separate fresh
state read. Verification checks account, device, requested state and music URI when supplied.
Ambiguous accounts/devices, expired or missing schemas, and uncertain outcomes never count as
verified success. Text-turn retries of the same workflow mutation return the original result.
Skip reads the current device/track before the command and verifies a changed track afterward.
Each workflow holds its own upstream MCP connection until it completes; concurrent workflows
use separate connections. Generic discovery and conversational connection setup remain available.
The backend prepares the small Spotify schema/account registry in a cancellable read-only
startup task. It does not block local tools, play music, initiate authorization or extend
the five-minute evidence lifetime. An expired registry is refreshed on demand.

Text and voice context include a bounded local capability index: advertised direct tool names
and configured MCP aliases with short descriptions. Native callable schemas retain their full
constraints; remote catalogs stay behind lazy discovery. A listed server can be searched directly,
skipping server discovery. Text refreshes this index each turn; voice receives a startup snapshot
and can discover connections added during the session. Index metadata does not establish active
accounts or current device/playback state, and secret values/authorization URLs are redacted.

The developer hill-climbing runner is `python -m scripts.autoresearch_harness --repeats 3`.
It tests the production text loop with Luna and simulated providers, alternates paired order,
runs regression gates, and saves feature/source fingerprints, grades and redacted traces in
`.scratch/harness-optimization/autoresearch-*`. Faster invalid trials are rejected. It recommends
features without editing production settings. These model runs use API credits and do not make
real Spotify calls or send messages. Real-provider latency needs separate live validation.
Additional comparisons use `--incumbent-workflow --features index playlist`. The separate
`python -m scripts.check_capability_index --output <new-experiment-directory>` measures a
discovery-heavy fixture request without a direct workflow. Playback rows record time to command
acknowledgment separately from verified reply time.

iMessage uses the separate Responses API text agent with `STACY_TEXT_MODEL=gpt-6-luna`. Voice stays on `gpt-realtime-2.1`. Discovery schemas are cached for five minutes, exact-name lookups return one tool, and Composio search returns at most five relevant full schemas plus names for additional matches. Input constraints, prerequisite plans, pagination guidance and provider errors remain available.

Startup queues Pipecat's `LLMRunFrame` to initialize Realtime's instructions and tools before microphone requests. Quick actions, including alarms and memory operations, wait for their tool result before continuing; web searches and sandbox runs use Pipecat's background-tool mode. Saved alarms survive speech interruptions.

Optional tools are advertised only when their credentials are configured:

| Credential | Tools |
| --- | --- |
| `EXA_API_KEY` | Web search with source URLs and excerpts |
| `E2B_API_KEY` | Python execution in a fresh remote sandbox, destroyed after each call |
| `SPOTIFY_CLIENT_ID` | Spotify connection, track search, devices and playback controls |
| `SOLOIST_API_KEY` or `SOLOIST_WS_URL` | Spotify Soloist pairing status, local playback and playback verification |

For Spotify, create an app in the [developer dashboard](https://developer.spotify.com/dashboard), set its redirect URI to `http://127.0.0.1:8765/callback`, and add the client ID to `.env`. If you change Stacy's port, change both the registered redirect and `SPOTIFY_REDIRECT_URI`. Restart the service, run `uv run stacy connect-spotify`, and open the returned link on the computer running Stacy. Authentication uses [PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow), without a client secret. Tokens are stored locally with owner-only permissions and refreshed through Authlib. [Playback control requires Premium](https://developer.spotify.com/documentation/web-api/reference/start-a-users-playback); open Spotify on your existing device first. Stacy controls that device; it does not turn the laptop or Pi into a Spotify playback device.

The app owner also needs Premium for [development-mode API access](https://developer.spotify.com/documentation/web-api/concepts/quota-modes). Add the connecting Spotify account under the app's user access settings if required. Authorize only playback-state reading and playback control. Once connected, `uv run stacy spotify-devices` lists devices and `uv run stacy spotify-status` reads the actual track, device, playing/paused state, and progress. Voice uses the same status tool to check playback before confirming it. An empty playback session is reported explicitly; expired authorization, missing devices, and access errors include recovery instructions. Live verification should observe the intended device and track, increasing progress while playing, and a paused state after pausing.

### Spotify Soloist on a Mac with Docker

Soloist can replace the Web API playback backend without a Web API app or OAuth callback. Obtain your own [Soloist key](https://developer.spotify.com/documentation/soloist/concepts/authentication) and put it in `SOLOIST_API_KEY` in `.env`. Key creation requires Premium. `STACY_SPOTIFY_BACKEND=auto` (the default) selects Soloist when its key or WebSocket URL is configured; set `webapi` or `soloist` to choose explicitly. The key is used only by the player, never sent to the LLM or written into the image.

On macOS, start Docker Desktop and run these from the repository:

```sh
uv sync --all-extras --locked
docker compose --env-file .env -f docker/soloist/compose.yaml up -d --build
uv run --all-extras stacy start
```

Only the music player runs in Debian Linux. Voice, alarms and memory stay on the Mac. `stacy start` automatically starts a Mac bridge for Bonjour discovery and music audio. Select **Stacy** in the Spotify desktop app's device picker once to pair. Pairing survives container recreation in the `stacy-soloist-data` Docker volume; don't delete that volume when updating. To use the bridge separately (for example, with `stacy serve`), run `stacy soloist-host` in another terminal and set `SOLOIST_HOST_BRIDGE=false` before running voice to avoid two bridges.

Docker's private network does not advertise the player directly to the Mac. The development bridge forwards only Soloist's Spotify Connect pairing endpoint on port 24879 and advertises it through macOS Bonjour. This pairing port is reachable on the local network, like a normal Spotify Connect device. Playback controls (9090) and raw audio (4714) bind to host loopback only. PulseAudio's standard null sink and simple-protocol module deliver 48 kHz stereo PCM to the existing PyAudio output. The bridge forwards only the player's output, not microphone audio. Stop it with Ctrl+C; stop the player with `docker compose --env-file .env -f docker/soloist/compose.yaml stop`.

Soloist tools use the same `spotify_connect`, `spotify_devices`, `spotify_control`, and `spotify_status` names. Devices means this player only; controlling other devices still needs the Web API. Its protocol has no catalog search. Stacy can use Exa to obtain actual `open.spotify.com` links, or accept a URI/link from you; web results are not guaranteed to find every catalog item. Track/album/playlist/episode links are validated before playing. The adapter waits for command acknowledgement and checks playback separately, since accepted commands can take time to change state.

The Dockerfile downloads each user's binary directly from Spotify. Do not publish or redistribute the image/binary. [Soloist builds expire after 90 days](https://developer.spotify.com/documentation/soloist/concepts/overview); refresh with `docker compose --env-file .env -f docker/soloist/compose.yaml build --no-cache` and then `up -d`. Pairing storage is retained. On a Raspberry Pi/Linux device, Soloist can run natively with PulseAudio/PipeWire and `--ws 127.0.0.1:9090`; neither the Docker audio bridge nor macOS Bonjour relay is needed.

Live Mac validation used the official Linux ARM64 binary in Docker, paired through the desktop Spotify app, and called the real Soloist tools through FastMCP. Play produced a playing state, advancing position and nonzero PCM audio; pause restored the original paused state. Synthetic WebSocket tests also cover stale startup snapshots, delayed acknowledgements, pairing requirements and protocol errors.

Running the assistant requires no hosted backend. Voice, web search, Spotify and sandbox execution use their respective external APIs; scheduled alarms and personal memory operate locally.

## Developer MCP wiring and tool discovery

The current developer wiring uses a standard `mcpServers` JSON file for local
stdio or remote HTTP/SSE MCP servers. The product will manage this internally
through conversational connection tools; users should not have to edit this file.
Stacy advertises just three additional tools regardless of the number of servers or app tools:

1. `search_mcp_servers`: search local descriptions for a service/capability, or browse five servers at a time.
2. `search_mcp_tools`: connect only to the selected server and return at most five tool definitions with full input schemas.
3. `call_mcp_tool`: execute a discovered tool with its exact server alias and arguments.

FastMCP's existing proxy and `BM25SearchTransform` handle remote catalog caching, ranked
tool search, schema validation and execution. The same transform searches the local server
directory. No extra embeddings provider or agent harness is needed. Server descriptions
should name services and capabilities, such as "Gmail email, Google calendar, Sheets".
Search ranks keyword matches; try different keywords when nothing matches. Direct alarm,
memory, web and Spotify tools remain available for quick everyday actions.

```sh
cp mcp.example.json mcp.json
# Edit mcp.json; enable the entries you want and replace their placeholders.
# Add STACY_MCP_CONFIG=./mcp.json to .env, then restart Stacy.
uv run --all-extras stacy doctor
uv run --all-extras stacy start
```

Use `${ENV_NAME}` references for credentials in headers or subprocess environment variables;
put the values in your local `.env`. References are resolved when a server is first used,
so a missing credential on one server doesn't break alarms or other servers. `mcp.json`
is ignored by Git. `doctor` shows enabled aliases/descriptions without connecting or printing
transport credentials. The example entries are disabled until configured; no app accounts
are connected automatically.

Remote servers need their canonical URL, including a trailing slash when required.
HTTP redirects are disabled to keep custom authentication headers on their configured
destination. Local commands run without a shell; set an explicit interpreter, arguments
and working directory for portability. Each request reconnects, and local subprocesses
are stopped afterward. MCP servers that rely on in-session state across tool calls need
a persistent session adapter; this initial gateway is for servers whose tool calls work
across reconnects. Requests have a 60-second ceiling. A timed-out write may have completed
remotely, so Stacy is instructed to check its result before retrying.

Server and tool names remain separate, so identical tool names on different servers don't
collide. Remote errors are marked explicitly in text as well as MCP's error flag because
Pipecat's current MCP adapter reads text responses. Nontext content is relayed through MCP;
the voice adapter currently consumes only text. Tool call traces include the server alias,
selected tool and arguments. Configured server credentials do not enter discovery results.
This bounds advertised tool schemas; ordinary conversation and returned document content
still consume context. It doesn't implement conversation compaction.

### Conversational custom remote connections

The shared voice/text tools now include `connect_mcp`, `list_connections`,
`verify_connection`, `reconnect_connection` and `disconnect_connection`. A user can
provide a public HTTPS MCP endpoint and a distinct friendly alias in conversation.
Stacy initializes it and lists its tools before activating it in the running gateway.
This is a protocol/catalog check, not proof every upstream action succeeds. At least one
tool must be discoverable. No config-file edit or assistant restart is needed.

OAuth connections return a provider authorization link. During voice, Stacy uses the
existing `send_text` iMessage tool; in chat she includes the link. The phone callback
records consent, then the user's spoken/texted "done" triggers `verify_connection` for
the intended alias. Multiple pending setups require explicit selection. PKCE, state,
registration and callback results survive a process restart. A pending link is reused
for ten minutes; expired grants are renewed during verification. Reconnect explicitly
starts fresh authorization. Tokens refresh through the MCP SDK when supported.

**Deployment prerequisite:** set `STACY_MCP_CALLBACK_URL` to a stable public HTTPS URL
ending in `/connections/oauth/callback`, with that route forwarded to Stacy's local
process. This is an operational deployment setting, not a user onboarding task. Expose
only OAuth routes; `/mcp` and `/agent/message` must remain private. Disable or redact
callback query logging in the public proxy. The local server disables access logging,
and authorization tool results/SDK exception details are omitted from diagnostics.
Stacy never opens a host browser or offers a localhost callback. Without a provisioned
callback, public unauthenticated MCPs work, while authenticated connections report that
phone authorization is unavailable on this installation.

For providers supporting OAuth client metadata documents (CIMD), deployments may set
`STACY_MCP_CLIENT_METADATA_URL` to an HTTPS JSON metadata document under their control;
the MCP SDK uses it as the verified client identity instead of dynamic registration.
Providers may restrict dynamic registration even when they advertise a registration
endpoint. Discovery metadata alone does not prove an arbitrary client can register.
When that URL ends in `/connections/oauth/client.json` on the callback's origin, Stacy
serves the metadata document herself; it contains only client identity and callback
information. `RemoteConnections.oauth_app()` provides a separate ingress containing
only the callback and this metadata route, suitable for a constrained HTTPS proxy.

The Stanley connection was tested locally against `https://xapi.getstanley.ai/mcp`:
Stacy generated its `/mcp/consent` OAuth request using CIMD and PKCE, sent it through the
existing iMessage bridge, received the phone callback, exchanged the authorization code,
and verified both remote tools. A new local process reused the stored grant without
another consent and discovered `message_stanley` and `check_stanley_reply` through the
shared gateway. That initial catalog test executed no Stanley actions. The test used a temporary HTTPS tunnel
to the narrow local ingress, with access logging disabled; no application was deployed.
A temporary tunnel hostname is a developer test fixture, not a durable Pi client identity.

Headless bearer credentials can be imported through the internal
`RemoteConnections.import_bearer` interface by a trusted credential-entry adapter.
It is deliberately absent from model tools, stores the credential only in the private
credential file, and verifies discovery before activating access. A user-facing secure
credential-entry adapter is not implemented yet; do not ask for tokens in chat/voice.
Explicit local developer tests can opt into a loopback callback via the constructor's
`local_callback_test` option. Production configuration still requires public HTTPS.

`remote-connections.json` stores aliases, endpoints and cached status under the data
directory. `remote-credentials.json` separately stores OAuth registration, pending grants
and tokens; both files use owner-only permissions and atomic replacement. Neither is
personal memory. Disconnect removes local credentials and gateway access; it does not
revoke the provider's authorization grant. Request failures do not repeat upstream writes.
A one-time authorization code is never retried after an uncertain token exchange; use
an explicit reconnect to recover.

This flow supports public HTTPS Streamable HTTP MCPs and OAuth providers supporting
public-client dynamic registration. Embedded credentials, URL query/fragment credentials,
private-network endpoints, API-key entry and subprocess installation are excluded.
Claude/Grok model delegation and existing chats/projects require their own integrations.
Developer-configured MCPs and Composio accounts retain their existing management paths.

Validation includes HTTP protocol/PKCE/restart/refresh tests, a fixture handoff through
the existing iMessage bridge into a text-agent completion turn, and a live public DeepWiki
connection, discovery, documentation read and disconnection check. Stanley's actual OAuth
callback and code exchange were verified locally. The approved grant was then moved into
the regular local assistant's operational store; that same iMessage backend called Stanley,
received "Stacey, we are connected.", and sent the result through the existing bridge.
Provider acceptance confirms the text send. Stable Pi callback/identity provisioning
remains deployment work.
The production voice pipeline also verified a Stanley reply and Frank Sinatra playback
on the owner's Echo Show 5 after fixing the local transport's invalid session after restart.
The [backend restart regression](tests/test_voice_backend_restart.py) verifies tool use
after an idle backend restart.

### Composio account connections

Set `COMPOSIO_API_KEY` to the `ck_` consumer key from **For You → Sessions & API Key**.
Stacy automatically registers `https://connect.composio.dev/mcp` using the
`x-consumer-api-key` header. No Composio SDK, project API key, local OAuth callback or
manual MCP configuration is needed for this connection. Project keys belong to a
different API and are rejected with a clear setup error.
See [Composio Connect](https://docs.composio.dev/docs/composio-connect).

Stacy first discovers and calls `COMPOSIO_SEARCH_TOOLS`, then uses the returned toolkit
slugs and session ID. `COMPOSIO_MANAGE_CONNECTIONS` lists accounts, creates a link with
`action="add"`, and verifies status with `action="list"` after the user's reply.
Two Google accounts must stay distinct: select the intended account through the
provider's returned schema, asking when ambiguous. Google/Slack authorization still
requires the user's phone consent. This flow needs an end-to-end sign-in test with
each real account; one Google Calendar account has been verified active. Key authentication
and tool discovery alone do not prove other accounts are connected. Pending setup state
and account labels persist in owner-only `connections.json`, outside personal memory.
Unexpired links are reused for the same requested alias; a second account needs a distinct
alias. Connection metadata excludes sample calendar events; schedule questions fetch live
events. Calendar execution selects a verified account, requires an explicit calendar for
creation, supplies the local IANA timezone when omitted, and checks returned event fields
before confirmation. Verification failures never retry a creation automatically.

## MVP scope

Start with one conversational agent and asynchronous tools. Keep timers and wake-up alarms in the MVP. Additional integrations use configurable MCP servers and on-demand discovery. Defer virtual desktop control, a separate computer agent and vision model, code mode, scheduled memory consolidation, background inbox monitoring, remote memory synchronization, Raspberry Pi packaging, and wake-word detection.

## Timers and alarms

Support requests such as "set a ten-minute timer" and "wake me up at 7 am," plus listing, cancellation, stopping, and snoozing. Interpret clock times in the configured local timezone, initially `America/Toronto`, and confirm the resolved date and time. An unqualified "7 am" means the next occurrence.

Use APScheduler for scheduling and SQLite for persisted alarm and timer records. Save deadlines and timezone information and restore pending events after application restarts. Alarm state belongs in operational storage, separate from personal memory.

The LLM interprets requests and calls tools to manage timers and alarms. Once scheduled, events play a generated local alarm sound independently of internet access and the voice model session. Ringing requires the computer to remain awake and Stacy's local alarm runtime to be running.

## Adopted memory design

Use [Agent Memory Repo](https://cognition.com/agent-memory-repo) for the MVP. A separate local Git repository is the source of truth for durable personal knowledge. Keep it outside Stacy's source checkout and outside disposable E2B sandboxes. Its path is configurable so the same data can move to a Raspberry Pi.

Follow the [upstream format](https://github.com/AgentMemoryRepo/agentmemoryrepo/blob/main/SPEC.md): a compact `MEMORY.md` entry point, linked topic notes, and source and date metadata. The upstream skill and specification are vendored unchanged under `src/stacy/skills/agent-memory-repo`, with their MIT license and provenance, pinned to `8798cb26c817d451a5ed6cba0ad8d9eca8d5ec3e`.

## Reuse and integration

Use Pipecat for conversation context and asynchronous tool execution. Use FastMCP's existing `SkillsDirectoryProvider` and `ResourcesAsTools` transform to make skill instructions discoverable and readable. Expose narrowly scoped file, search, and Git tools for the configured memory repository.

Voice and text share a bounded skill catalog containing names, descriptions and resource URIs.
Calendar, tasks, Spotify and connected-app workflows live under `src/stacy/skills`; only the
selected recipe is loaded through `read_resource`. Calendar instructions use complete local
day boundaries and an explicit Google account. Task instructions resolve the user's intended
service/list before creating a task. Recipes reuse complete schemas, sessions and account/device
choices on continuations. Provider schemas stay in the existing bounded Spotify cache or retained
working context; skill instructions do not replace live discovery or result verification.

The upstream skill is invoked explicitly and does not install automatic startup hooks or scheduled maintenance. Stacy supplies the application wiring:

- Load the vendored memory skill and personal `MEMORY.md` automatically when a voice session starts; retrieve topic notes as needed. Skill resources remain available through MCP for on-demand instructions and supporting files.
- Update useful preferences, facts, projects, and decisions automatically, without requiring the user to say "remember this."
- Apply corrections to existing entries and retain source references.
- Serialize memory writes and commit completed updates.

Voice instructions adapt the upstream CLI workflow: the repo is already initialized, so
Stacy maintains it without setup prompts or Git announcements. Communication preferences
belong in the startup entry point; topic details such as music tastes belong in linked
notes. Entries cite the current conversation trace filename and local date. A personalized
question follows the index links before answering. `search_memory` is literal text search,
not semantic search: use a distinctive word or exact phrase. It returns `{"matches": []}`
when nothing matches, so Pipecat does not mistake an empty result for a failed tool call.

An opt-in live check exercises the production Pipecat worker and real HTTP MCP tools using
temporary memory and fresh Realtime connections. It sends natural preferences, checks Git
commits and index links, restarts, retrieves notes, applies a correction, and restarts again.
It never changes your personal memory or opens your microphone/speakers. It uses the key in
`.env` and incurs normal Realtime API charges:

```sh
uv run --all-extras python scripts/check_memory.py
# macOS: send the initial preferences as generated PCM speech instead of text
uv run --all-extras python scripts/check_memory.py --audio
```

Conversational wording remains model-driven. The live check reports extra spoken preambles
as style notes, separately from persistence/retrieval failures. Startup loads saved memory,
but does not replay the preceding voice session's chat history.

Periodic consolidation is deferred until the initial memory loop is working.

Keep speech interruption separate from cancellation of memory maintenance. Treat retrieved memories as contextual data. Keep credentials in account connection storage. Local operation requires no remote Git repository; synchronization can be configured later.

## Storage responsibilities

| Store | Responsibility |
| --- | --- |
| Agent Memory Repo | Durable personal knowledge and linked notes |
| SQLite | Timers and alarms |
| Local account storage | Spotify tokens, separate from personal memory |
| Skill directories | Reusable task instructions and supporting files |
| E2B sandboxes | Code execution; virtual desktop sessions are deferred |

Supermemory is deferred for the MVP. File search and linked navigation are the initial retrieval mechanism. If retrieval becomes inadequate, evaluate semantic indexing over the memory repository while retaining one authoritative store.

## Libraries and layout

| Component | Library |
| --- | --- |
| Voice and tool orchestration | Pipecat + OpenAI Realtime, local PyAudio transport |
| Tool server and skill discovery | FastMCP, SkillsDirectoryProvider, ResourcesAsTools |
| Timers and alarms | APScheduler + Python SQLite |
| Personal memory | Vendored Agent Memory Repo skill + local Git/file adapter |
| Web search | Official Exa async SDK |
| Agent-owned Python sandbox | Official E2B Code Interpreter async SDK |
| Spotify authorization and API | Authlib PKCE + HTTPX |
| Local serving and configuration | Uvicorn + python-dotenv |

```text
src/stacy/
  cli.py           process lifecycle and development commands
  voice.py         Pipecat pipeline, conversation instructions, MCP connection
  tools.py         alarm/memory tools and upstream skill provider
  alarms.py        durable alarm state and scheduler
  sound.py         local alarm playback
  memory.py        separate personal Git repository
  integrations.py  Exa and E2B SDK adapters
  spotify.py       local OAuth callback and Spotify tools
  skills/          vendored upstream instructions
tests/             offline scheduler, persistence and integration checks
```

State defaults to `~/.local/share/stacy` (or `$XDG_DATA_HOME/stacy`). Set `STACY_DATA_DIR` and `STACY_MEMORY_DIR` to relocate it. Memory must remain outside the source checkout, in its own Git repository. Writes use revision checks, serialization and local commits; no remote is required. Keep operational state and personal memory when moving machines, and reinstall platform audio dependencies on the destination.

## Development and validation

```sh
uv sync --all-extras --locked
uv run --all-extras pytest -q
uv run --all-extras ruff check src tests scripts
```

The offline tests use local temporary storage and mocked provider responses; they make no live provider calls. A startup regression check verifies that the production worker sends tool schemas and instructions without any injected user message. Separate live checks sent both text and generated PCM speech through Realtime and the actual HTTP MCP tools: a five-second timer fired through the scheduler and invoked macOS `afplay` without a playback error, and spoken timer/wake-up requests saved their expected state. The generated-audio timer check observed one saved timer and no repeated confirmations during nine seconds of idle time. A live Exa search returned five results through MCP.

The live memory check passed with generated PCM input: naturally mentioned music and communication preferences were saved and committed, fresh sessions retrieved linked notes, a correction replaced the music preference, and another fresh session recalled the correction while preserving the communication preference. An empty literal search returned a valid no-matches result. Final check traces had no tool or pipeline errors. One retrieval turn still included a spoken preamble; single acknowledgements were observed for the preference and correction turns.

The speaker fallback regression replays output PCM into microphone capture and verifies
suppression during playback and the echo tail. Echo-cancelled and headphone paths keep
simultaneous listening. A production-pipeline interruption test uses the real Realtime
speech-start handler to verify that queued device audio is cleared, capture continues,
stale playback callbacks are ignored and the next reply plays.

A bounded physical Mac check compared the same speaker phrase with processing disabled
and enabled: average microphone pickup fell from RMS 571 to 10 after warmup, about 35 dB.
The production native transport then captured 515 frames during playback, acknowledged
queue clearing in 1.8 ms, continued capture and played a fresh reply. This measures the
native clear operation, not total speech-detection latency. These checks saved only
aggregate metrics and made no provider calls. Human double-talk at different volumes
and the Pi audio hardware still need live validation.

Test alarms were cancelled and temporary operational/memory storage removed; diagnostic traces remain under `.cache/traces`. Everyday memory quality still needs observation during real conversations. Web API Spotify playback and E2B execution remain unverified. The MVP does not include wake-word detection, desktop GUI control or a boot-time service yet.
