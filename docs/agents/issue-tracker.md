# Issue tracker: Local Markdown

Issues and specs live as Markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`.
- Specs: `.scratch/<feature-slug>/spec.md`.
- One file per implementation ticket:
  `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`.
- Record triage state in a `Status:` line near the top, using
  `triage-labels.md`.
- Append conversation history under `## Comments`.

## Skill operations

“Publish to the issue tracker” means create the appropriate file under
`.scratch/<feature-slug>/`, creating directories as needed.

“Fetch the relevant ticket” means read the referenced issue file.

## Wayfinding operations

- Map: `.scratch/<effort>/map.md`, containing Notes, Decisions-so-far,
  and Fog.
- Child tickets: `.scratch/<effort>/issues/<NN>-<slug>.md`.
- Record ticket type with `Type: research|prototype|grilling|task`.
- Record dependencies with `Blocked by: NN, NN`.
- A ticket is unblocked when all dependencies are resolved.
- Select the lowest-numbered open, unblocked, unclaimed ticket.
- Before work, save `Status: claimed`.
- On completion, append `## Answer`, save `Status: resolved`, and add
  a summary and ticket link to Decisions-so-far in the map.
