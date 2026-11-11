# Hermes integration

`relic-gumi` runs on stock Hermes. It needs no host patches.

## Which turns the plugin acts on

The plugin must only touch the subject's own conversation, not cron jobs,
delegated sub-agents, the curator, gateway housekeeping or the background
memory-review fork. Stock Hermes does not label turns for plugins, so the plugin
derives it from what every hook already receives:

- `platform` of `cron`, `subagent`, `curator` or `gateway_hygiene`: ignored.
- A non-empty `parent_session_id` (delegated sub-agents): ignored.
- `pre_llm_call` records the `turn_id` of each turn it accepts. `post_llm_call` and
  `transform_llm_output` act only on recorded turns. Hermes skips `pre_llm_call` and
  `post_llm_call` for the review fork but still calls `transform_llm_output` with the
  parent's `session_id` and `platform`; the record keeps that call out.
- A host that passes `execution_context` and `user_visible` (or `subject_visible`) is
  trusted instead, and checked strictly.

## Cron jobs

The installer manages its jobs by creating them with `hermes cron create` and by reading
the profile's `cron/jobs.json`, the file Hermes itself lists from. It does not use
`hermes cron ... --json`, which stock Hermes lacks.

## Command platform and `ask_choice`

`/gumi-setup` routes on the `platform` keyword Hermes passes to a plugin command: `desktop`,
`tui`, `cli` and `local` (CLI/Desktop sessions owned by the gateway) run the battery through
`ctx.ask_choice`, which Hermes shows as the clarify picker there; `telegram` (or no platform)
keeps the Telegram flow; any other platform gets a short pointer and nothing is started.
The first-turn notice uses the same set of local platforms.

## Verified

Hermes `v0.23.0` (`2bd378ed1b`), clean checkout with no modification, Python 3.14, plugin
installed from a built (non-editable) package, with a scripted model:

- `relic-gumi install` completes: 8 cron jobs, plugin enabled, `hermes cron list` shows them,
  a second install changes nothing.
- A real `hermes chat` turn: the Context Pack is injected and the reply is checked.
- Through `AIAgent`: CLI and gateway-style turns are handled; cron, sub-agent, curator and
  gateway-hygiene turns and the review fork (built with Hermes' own
  `build_cache_parity_fork`) are ignored. Without the `turn_id` record the fork's reply
  is rewritten.

Not exercised: TUI and Desktop platform names, `oneshot`, side-question agents. Resumed,
branched and compressed sessions were read from source (they do not set
`parent_session_id`), not run. Hermes changes quickly, so re-check on newer versions.

## Dependencies and the Hermes package manager

Hermes 0.23 keeps its Python packages in "environment generations" built by its package
manager (PM). Each generation is computed from the union of the plugins enabled in every
profile, so a package put into one venv by hand (for example `pip install relic-gumi`) is
gone as soon as a later generation is built. A plugin therefore has to carry what it needs
and declare its third-party requirements, and the installer has to ask PM to prepare them.

### What was run (sandbox, Hermes 0.23.0, local checkout)

Fake `HOME` and `HERMES_HOME` under a scratch directory, a throw-away local git repository
with a one-file plugin that imports `structlog`, and `HERMES_RUNTIME_DIR` pointing at a
scratch directory (see "Side effect" below for why that variable is required).

| Command | Result |
|---|---|
| `hermes plugins install file://<repo> --yes-deps --enable` | Works without any prompt. PM resolved `python_dependencies`, built a new generation, the plugin was enabled and `structlog` imported in that generation's interpreter. |
| `hermes plugins install file://<repo> --yes-deps --no-enable`, then `hermes plugins enable <name>` | Both steps work non-interactively. `--yes-deps` at install time is accepted; `enable` itself has no consent flag and did not need one. |
| `hermes --profile p plugins adopt <name>` on a plain copied directory | Refused: "Plugin is manual, not self-cloned - there is nothing to adopt". `adopt` only applies to git clones with an origin. |
| `hermes --profile p plugins enable <name>` on a plain copied directory in `<HERMES_HOME>/profiles/p/plugins/<name>/` (no git, no install record) | Works without a prompt. PM read `python_dependencies` from the copied `plugin.yaml`, built a new generation (`structlog` present in the selected one, absent in the previous one) and enabled the plugin. |
| Same command again after adding a requirement to the copied `plugin.yaml` | Prints "already enabled" but still resolves and installs the new requirement into a new generation. So `enable` is safe to repeat and is the way to re-sync after a manifest change. |
| `hermes plugins doctor <dir>` | Imports and registers the plugin; reports tool and hook counts. |
| `hermes plugins install file://<this repository> --yes-deps --enable` | The repository root is the plugin directory. Because `pyproject.toml` has a `[project]` table and a `build-system`, PM owns packaging: it read the five `[project].dependencies` (not `plugin.yaml`) and also installed the project itself as a workspace member, so `relic` is importable in the selected generation. The previous generation, built before the enablement, still lacks it. |

Conclusions:

1. For a plugin directory the installer copies into a profile, the one non-interactive flow
   that works is `hermes --profile <profile> plugins enable <name>`: it needs no consent
   flag and no git origin. `adopt` is not usable, and `plugins install` expects a git source.
2. For the catalog layout (the repository root is the plugin), `plugins install ... --yes-deps`
   works and also installs `relic` through `pyproject.toml`.
3. When `pyproject.toml` has a `[project]` table PM reads dependencies from it and ignores
   `python_dependencies` in `plugin.yaml`. The copied profile payload has no `pyproject.toml`,
   so there `plugin.yaml` is what PM reads. Both lists are kept identical (a test compares
   them).
4. PM only installs third-party requirements it can resolve, and nothing documents that it
   installs the plugin project itself for a copied directory. The copied payload therefore
   carries its own copy of the `relic` package (`_vendor/relic`) and puts it on `sys.path`
   only when `relic` is not already importable; the repository root `__init__.py` does the
   same with `src/`. An existing installation always wins.

### Installer and packaging checks

The installer detects a PM interpreter by its
`installs/<id>/environments/<id>/venv` prefix. It never pip-installs missing
prerequisites there: it reports the PM installation command instead. With prerequisites
present it copies the directory plugin, enables it in the profile config and invokes
PM's `plugins enable` through its injectable runner before verification. A nonzero
sync result stops installation and restores the previous profile plugin/config; PM's
own caches and environment generations are outside that rollback.

The runtime bundle is copied at install time from the installed `relic` package.
It excludes tests, Python/tool caches, `hermes_plugin/gumi_entry` and the output
profile (including staging/backup directories); links and special files are rejected.
The runtime fingerprint hashes the resulting bundle, not files excluded from it.
Detached delivery propagates the active runtime via `PYTHONPATH`, and generated action
scripts resolve the profile-local bundle relative to their own path.

`tests/test_plugin_packaging.py` covers matching bounded manifests, PM detection and
refusal to pip-install, sync ordering/failure rollback with injected runners, exclusions,
idempotency, content hashes, nested-output recursion, fresh-process fallback imports,
SQL/JSON resources and subprocess/action-script runtime resolution. These tests do not
run PM or a gateway. For development, run with fake homes under `TMPDIR`, e.g.:

```bash
sandbox=$(mktemp -d "$TMPDIR/relic-tests.XXXXXX")
mkdir -p "$sandbox/home" "$sandbox/hermes"
env -u VIRTUAL_ENV -u HERMES_PROFILE -u HERMES_PROFILES_HOME \
  HOME="$sandbox/home" HERMES_HOME="$sandbox/hermes" \
  RELIC_HOME="$sandbox/relic" HERMES_RUNTIME_DIR="$sandbox/runtime" \
  PYTHONPATH="$PWD/src" python -m pytest -q -p no:cacheprovider \
  -m 'not live' -W ignore --basetemp="$sandbox/pytest"
```

A wheel built in a scratch copy was also installed non-editably to a scratch target.
With no installed `relic` on the child interpreter's import path, the real Hermes
`PluginManager` loaded `_vendor/relic`, registered all four hooks, and found the SQL
migration and JSON schema. No Hermes launcher or PM install was invoked in this check.

### Side effect to know about

The first run in the sandbox, without `HERMES_RUNTIME_DIR`, still rewrote files in the real
`~/.hermes/tools/` (the managed Python runtime of the same version was re-extracted and
`facts.json` rewritten): PM resolves its runtime store from the Hermes checkout's install
stamp, not from `HERMES_HOME`. Nothing under `installs/`, no profile and no config changed,
and every existing environment still started its interpreter afterwards. Later runs set
`HERMES_RUNTIME_DIR` to a scratch directory, but this is **not proof of isolation**:
merely running the Hermes launcher also takes a lease file under the current real
generation. Treat the historical experiments above as command-behaviour evidence, not
as a safe sandbox recipe. Do not repeat PM installation/enable experiments on this host
using only fake `HOME`, `HERMES_HOME` or `HERMES_RUNTIME_DIR`; complete isolation of
runtime/install stores and launcher leases has not been demonstrated. Packaging tests
use injected PM runners, never the real launcher, and fake homes under `TMPDIR`.

### What was and was not demonstrated

Demonstrated: the flows above, on this machine, in a sandbox, with the CLI of Hermes 0.23.0.
Not demonstrated: what PM does to the user's real environments when the same command runs
against the real home, behaviour on other Hermes versions, installation from the Hermes
plugin catalog (no catalog entry exists yet), and the Desktop's own Plugins panel (it was
not run).
