# Stacy: standing product and architecture decisions

These decisions were explicitly confirmed by the user on 2026-10-10. Apply them
to all future design, implementation and setup work in this repository.

- Stacy is a headless personal assistant whose target home is a Raspberry Pi
  with a microphone and speaker. The user's Mac is a development environment.
- Voice and chat are the product interface. The user is adding iMessage in a
  separate worktree. Integrations should use the shared conversation/tool layer
  and the messaging channel when available.
- Do not build a Stacy dashboard, browser setup page, settings UI or companion
  app. Do not design flows that require the user to sit at the host computer,
  use its browser, copy configuration files, edit JSON or run terminal commands.
- Setup and connection management happen conversationally. For example, the
  user asks to connect Google; Stacy initiates authentication, texts the provider's
  authorization link, verifies completion and confirms in the conversation.
  Provider sign-in/consent pages opened on the user's phone are compatible with
  this design; they are not a Stacy UI.
- For custom MCP servers, accept connection information through the conversation,
  request only missing information and save/validate configuration internally.
  Keep secrets out of spoken responses, personal memory and ordinary tool traces;
  use a suitable credential-entry/authentication flow where needed.
- Adding a connection should make it usable without manual file edits or a
  user-managed restart. Support N MCP servers with server/tool discovery rather
  than advertising every tool schema in every conversation.
- Authentication links and callbacks must work when the assistant runs on a
  separate device. A localhost URL on the Pi is not reachable from the user's
  phone. Avoid flows that depend on opening a browser on the assistant host.
- Persist account identities, connection state and credentials separately from
  personal memory. Keep the two Google accounts and their calendars distinct;
  resolve the intended account explicitly when ambiguous.
- CLI commands, localhost tool endpoints and configuration files may exist as
  developer/operational internals. They are not the end-user onboarding flow.
- Do not claim messaging, authentication or hot connection activation works
  until the actual implementation is wired and verified. Coordinate with the
  user's iMessage work rather than implementing a competing messaging channel.

## Agent skills

### Issue tracker

Issues and specs live in `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default triage roles as status strings. See `docs/agents/triage-labels.md`.

### Domain docs

Use a single-context layout: root `GLOSSARY.md` and `docs/adr/`. See `docs/agents/domain.md`.
