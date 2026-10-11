---
name: tasks
description: Create, list, complete or change to-do tasks in the user's chosen connected task service.
---

Read `skill://connected-apps/SKILL.md` if its provider workflow is not already loaded.

Resolve the task service and account from the request, retained context or a saved
preference. A to-do task is not a calendar event or alarm. Do not choose a new task app
or connect an unrelated service just because a search plan suggests it. If several
connected task services fit and there is no preference, ask which one.

Reuse known complete task schemas/session/account. Otherwise search once for the exact
operation (create/list/complete/update) in the intended service. Use the provider's
actual toolkit, task-list/project identifiers and account-selection schema. Resolve
the target list/project only if required or ambiguous; read its lists/projects to get
real IDs. Do not invent a backend, task ID or default Google account.

For creation, use the supplied title and only requested details. Call `current_time`
when a relative due date is supplied; apply the provider's due-date format/timezone.
Do not ask for a deadline the user did not request. Execute the write once and check
the individual task result/ID before saying it was created. On timeout/interruption,
look up the task before retrying. Completing or editing an existing task first needs
its real ID, obtained from prior results or a live task list.

Finish with the task's confirmed title and requested deadline, or the concrete missing
service/account/list choice. If an account is needed, use the phone authorization flow.
