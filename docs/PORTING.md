# Porting notes

This package is generated from a private single-User codebase. Commits are
ported by cherry-pick and then adapted: personal identifiers are replaced and
private-infrastructure defaults are removed.

## Intentional differences from the private codebase

- **No private device integrations.** A few private-only modules and one
  database column are not part of this package. Code that referenced them was
  removed when porting (detached cron dispatch, check-in follow-up chain
  schema).
- **Telegram delivery results are simpler.** The dispatcher returns a plain
  delivered / not delivered flag instead of the structured status
  (`not_sent` / `unknown`, `retry_after`) that the private tree uses.
- **Local generator endpoints are configurable.** `RELIC_COMFYUI_URL` and
  `RELIC_ACESTEP_URL` replace hard-coded addresses; both default to
  `127.0.0.1`.
- **No multi-subject research CLI.** Only the canonical single-User path ships.

## Ported in one step

Four upstream commits (detached cron dispatch for media and text, follow-up
chain logging, chain-close hint) interleave with the private-only code, so
they are applied as a single change. The private tree's matching test file is
not included.

## Known limitation

Follow-up plans are held in memory until the matching `post_llm_call`
arrives. If a turn is interrupted before that hook runs, the entry stays until
the process restarts.
