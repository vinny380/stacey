---
name: calendar
description: Check schedules or create calendar events, with the intended Google account and local day boundaries.
---

Use the connected calendar provider and live events. For Google Calendar, the toolkit
is `googlecalendar`; `cal` is Cal.com. Read `skill://connected-apps/SKILL.md` if its
execution/authentication workflow is not already loaded.

1. Call `current_time` for relative dates. Query the complete local day from midnight
   inclusive to the next midnight exclusive in the returned timezone. “Today, including
   this morning” must not start at the current time. Resolve Monday to its calendar date.
2. Keep the user's Google accounts distinct. Resolve work/personal from the request and
   known account aliases/preferences; ask only when ambiguous. `primary` selects the
   primary calendar within the chosen account, not a default Google account.
3. Reuse retained event-query/create schemas and provider session. If missing, search
   once for this calendar operation, fetch complete schemas, and pass the selected
   account through execution. Do not call a provider clock after `current_time`.
4. Fetch live events with bounded results. Page only when the answer requires it. An
   authorization preview or provider sample is not the user's schedule. For creation,
   resolve title, start/end, account, calendar and IANA timezone before the single write.
5. Check each result. For creation, check `stacy_verification`, title, start/end and
   timezone before confirming. If uncertain, read the event; never create it again.

An interrupted/cancelled call is not still running. Fetch again for a cancelled read
when the user still wants it. For a write with an uncertain result, check state first.
Answer with the actual schedule or confirmed event, without narrating discovery.
