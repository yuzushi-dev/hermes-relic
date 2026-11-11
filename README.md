<!-- Visual assets follow RIS - Relic Interface System (cyber77 theme). -->
<p align="center">
  <a href="assets/readme/showcase.mp4">
    <img src="assets/readme/header.png" width="830" alt="RELIC GUMI: a local, longitudinal AI companion for Hermes Agent. Click to watch the 60-second showcase.">
  </a>
</p>

<div align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/LICENSE-AGPL--3.0--or--later-ff003c?style=flat-square&labelColor=120a0d" alt="License: AGPL-3.0-or-later"></a> <img src="https://img.shields.io/badge/PYTHON-3.10%2B-00e5ff?style=flat-square&labelColor=120a0d" alt="Python 3.10 or newer"> <img src="https://img.shields.io/badge/RUNS-FULLY_LOCAL-00e57e?style=flat-square&labelColor=120a0d" alt="Runs fully local"> <img src="https://img.shields.io/badge/HERMES-0.23%2B-ffe23a?style=flat-square&labelColor=120a0d" alt="Hermes 0.23 or newer">
</div>

**Gumi is a companion that gets to know you, and helps you reflect on who you are. It builds a longitudinal model of you from published psychometric instruments (TIPI for the Big Five, ECR-RS for attachment) used as a self-report baseline, plus your consented check-ins, and it runs on your machine: nothing built into this package sends what it learns about you anywhere.**

> **Paper:** Veri', D. (2026). [*Relic and Gumi: Governing Memory, Safety, and Continuity in Diegetic Relational Agents*](https://arxiv.org/abs/2611.08504). arXiv:2611.08504 [cs.HC]. The paper presents the architecture and an exploratory two-participant pilot; it makes no claims of clinical efficacy or diagnosis, and neither does this plugin. Gumi supports introspection, it is not a diagnostic tool.
>
> **Live demo:** [yuzushi-dev.github.io/Relic](https://yuzushi-dev.github.io/Relic/), the researcher workbench of the parent project [`relic-oss`](https://github.com/yuzushi-dev/Relic), running on synthetic data only.

<div align="center">
  <img src="assets/readme/divider.svg" width="830" alt="Section divider">
</div>

## What makes it different

Every install starts from Gumi Classic, one curated personality: warm, brief, opt-in, consistent release to release rather than generated per person. What setup personalizes is delivery, not the character: media (images, music, voice), Telegram, quiet hours, message pacing, and a psychometric baseline (Big Five plus attachment style) that feeds the behavioral model below. A per-install, complementarity-driven persona pipeline exists in the wider `relic-oss` codebase but isn't wired into this public installer yet.

It can reach out first, too, on its own schedule instead of only replying to you, once you agree to it during the Telegram onboarding (the two consent questions; see [Current limitations](#current-limitations)). Once armed, it sends scheduled check-ins, proactive messages, and small diegetic "slice of life" fragments, plus images, music, or its own voice when you enable media.

A safety critic blocks false claims of dependency, physical presence, or clinical language in every reply. Scheduled messages also get a naturalness pass that scores AI-sounding phrasing, with hard-blocking opt-in while its threshold is still being calibrated.

The reference setup runs fully local: Gemma 4 E4B at 16K context, on your own hardware. A cloud LLM works too if you'd rather point it there; local is just what this reference setup uses.

## Where it comes from

RELIC Gumi is the consumer face of [`relic-oss`](https://github.com/yuzushi-dev/Relic), a research project on longitudinal, theory-grounded modeling of human-AI companion relationships (grounded in Cognitive Appraisal Theory, Self-Determination Theory, Attachment Theory, and Dual-Process Theory), described in the paper above ([arXiv:2611.08504](https://arxiv.org/abs/2611.08504)). This package keeps only the canonical, single-User path; the multi-subject research CLI and the persona generator it drove aren't part of it.

## The engram only you can read

A 60-facet registry exists to hold a longitudinal model of you, seeded with conservative defaults and filled in from eligible, consented check-in and passive observations over time; a fresh install starts nearly empty and fills in only as consented check-ins accumulate. Nothing here is meant to diagnose or label you. Whatever gets stored stays in the model on your machine; nothing built into this package phones it home.

<div align="center">
  <img src="assets/readme/divider.svg" width="830" alt="Section divider">
</div>

## Install

Not on PyPI yet. Choose the flow matching your Hermes installation:

- **Hermes package manager (PM), including Desktop:** from the repository root, run
  `hermes plugins install file://<absolute-path-to-this-clone> --yes-deps --enable`.
  PM installs this project and its declared dependencies into its selected environment.
  Then run `relic-gumi install` using that environment's interpreter/entry point to
  create the dedicated profile. Do not `pip install` into a PM-managed environment:
  a later environment generation would discard that change. The Desktop Plugins panel
  is another installation surface, but has not been exercised for this release.
- **Unmanaged/pip Hermes:** from a clone, using the interpreter that runs Hermes:

  ```bash
  python -m pip install .
  relic-gumi install
  ```

Note: `relic-gumi install` moves a plugin copy found in the base Hermes home (`<HERMES_HOME>/plugins/relic-gumi`, for example the clone that `hermes plugins install` creates) into the dedicated `relic-gumi` profile and disables the base entry, so `hermes plugins update` no longer tracks it; the profile copy is the active one.

The installer copies the runtime from its installed `relic` package into the profile
plugin's private `_vendor/relic/`; no duplicate runtime is stored in this repository.
The copy excludes tests, caches and the plugin payload itself, and rejects links. Entry points prefer
this copy (or the root checkout's `src/`) over any other `relic` on the path, so hooks run the code
that shipped with the plugin; a `relic` already imported in the process is left as is.
The copied manifest declares the same bounded third-party requirements as the package.
In a PM-managed interpreter, the installer runs
`hermes --profile relic-gumi plugins enable relic-gumi` after copying the plugin, before
verification and gateway reconciliation, to synchronize those requirements. This can
prepare a new environment and take several minutes, even with `--no-start`.
Detached deliveries carry the active runtime path into their child process; generated
action scripts resolve the bundle relative to their profile, so neither relies on a
source checkout remaining importable.
An unchanged copy is a no-op; runtime updates are fingerprinted and the previous plugin
and profile changes are restored if a later installation step fails. A failed PM sync
may still leave PM-created environment/cache artifacts: these are not rolled back by
this installer. See [Hermes integration](docs/hermes-integration.md) for evidence and
isolation limits.

The installer no longer asks questions. Onboarding (the psychometric baseline, preferences, optional media, and consents) happens in a chat with your own bot: on Telegram with buttons, one per row, for the closed answers and free text for the open ones; in the Hermes Desktop chat panel, the CLI or the TUI with one native picker per question (`/gumi-setup` starts or resumes it, and while the setup is incomplete the first message is answered by a notice inviting you to run it). Every question and where its answer goes is listed in [docs/onboarding-fields.md](docs/onboarding-fields.md). Send `/gumi-setup` to the bot to redo it. `relic-gumi configure` stays as an advanced command-line fallback.

If an AI agent installs this for you, it follows [AGENTS.md](AGENTS.md). In short, it:

1. installs the package and plugin (`relic-gumi install --hermes-home <path>`);
2. sets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USERS` in the `relic-gumi` profile `.env`;
3. checks the provider and model the installer imported: `relic-gumi install` copies `model.provider` and `model.default` (and `api_mode` / `base_url` when present) from the base Hermes config into the new profile, never overwriting a model the profile already has and never copying credentials. Only when the base config has no usable model does the agent set them by hand (`hermes --profile relic-gumi config set model.provider ...` and `model.default ...`), and it adds the provider's credentials to the profile `.env`;
4. runs `relic-gumi verify --skip-cron` and `hermes --profile relic-gumi gateway status`;
5. tells you to open Telegram and send `/start` to your bot.

The installer creates and owns its own Hermes profile, `<HERMES_HOME>/profiles/relic-gumi/`. It never reads or changes a profile called `gumi` (or any other name). If a `relic-gumi` profile already exists that the installer did not create (it is not empty and has no `.relic-gumi-installer.json` or `.relic-gumi/lifecycle.json`), `install`, `configure` and `uninstall` stop with an error before writing anything: pick another `--hermes-home`, or remove or rename that profile yourself. A run that failed halfway leaves a profile the next run accepts.

Model import details: the installer reads only the `model:` block of `<HERMES_HOME>/config.yaml` (never `.env`, `auth.json` or credential pools). A `base_url` containing userinfo (`@`) or a query string (`?`) is not imported, and any other key of the block (for example `api_key`) is left behind. A profile that already has `model.provider` or `model.default` is not touched, and a second `install` writes nothing. If the imported provider is a plugin enabled in the base config but not in the profile, the installer prints the exact command to run (`hermes --profile relic-gumi plugins install <provider> --enable`); it never installs the plugin itself. `configure` does not import, and `uninstall` leaves the imported `model:` block in place (it is ordinary profile configuration, not part of the plugin).

It does not answer the onboarding questions for you, and it never touches your consents. Verified against Hermes v0.23.0. See `relic-gumi install --help` for a non-default Hermes location, a non-interactive run, or reconciling without starting the gateway (`--no-start`, which still needs the gateway already stopped and may still install missing dependencies).

Requirements: Python 3.10+, Hermes installed in the same environment. No Hermes patches are needed: the plugin tells the subject's turns apart from cron, sub-agent and background-review turns using the arguments stock Hermes already passes to its hooks, and the installer manages its cron jobs through the profile's `cron/jobs.json`.

## Uninstall

```bash
relic-gumi uninstall
```

Removes the plugin, its hooks, and its scheduled jobs from Hermes. Your data at `$RELIC_HOME/user/` is kept by default, so reinstalling picks Gumi back up where it left off. To also permanently delete that directory:

```bash
relic-gumi uninstall --erase-data
```

This asks for a typed confirmation before deleting anything; `--yes` skips prompts for scripted use. It only erases `$RELIC_HOME/user/`; it does not touch `<HERMES_HOME>/profiles/relic-gumi/`, so the `.env` credentials described under [Privacy](#privacy) survive it and need deleting separately if you want them gone too.

## Verify your data

```bash
relic-gumi verify
```

Read-only. Checks that your five canonical JSON files (identity, baseline, preferences, policy, check-in state) exist, are well-formed, and have private (`0600`/`0700`) permissions, and that Hermes's scheduled jobs still match what RELIC Gumi owns. It doesn't check the onboarding provenance log or the private check-in database. Nothing is created, changed, or removed.

<div align="center">
  <img src="assets/readme/divider.svg" width="830" alt="Section divider">
</div>

## Current limitations

- **No generated persona yet.** `relic-gumi install` always sets up Gumi Classic. The complementarity-driven persona generator lives in `relic-oss` but isn't reachable from this installer. You can still hand-edit `SOUL.md`, `USER.md`, and `MEMORY.md` in the Hermes profile directly; the installer detects your changes by fingerprint and never overwrites them. The runtime also understands a richer set of canon files (`world.md`, `voice_canon.json`, `visual_canon.json`, `lyria_canon.json`, `media_policy.json`, `relationship_policy.md`) under `<HERMES_HOME>/profiles/relic-gumi/workspace/gumi/`, but the installer doesn't create them; write them yourself there if you want them.
- **Reach-out starts silent.** `consent_for_active_elicitation` and `consent_for_passive_extraction` start as `false` in `$RELIC_HOME/user/checkin/delivery_policy.json`. The Telegram onboarding sets them from your answers to the `c_act` and `c_pas` questions, and only if you say yes. Until you answer, check-ins, proactive messages, and diegetic fragments stay silent.

## What it touches on your machine

Disclosed up front, so you can decide before enabling it:

- **Hooks:** `pre_llm_call`, `post_llm_call`, `transform_llm_output` (context injection and the output safety critic, user conversations only) and `pre_gateway_dispatch` (Telegram onboarding). Tools: `gumi_read_canon`, `gumi_write_report`, `relic_continuity_candidate`. Command: `/gumi-setup`.
- **Files:** state under `$RELIC_HOME` (default `~/.relic`); the installer writes the `relic-gumi` Hermes profile, including its `.env` (Telegram token, provider keys you give it).
- **Credential files:** the plugin reads secrets from the process environment that Hermes provides. The only `.env` it parses is the dedicated `relic-gumi` profile's own file, which the installer creates, during onboarding (to find the Telegram token and your user ID). It never reads other profiles' `.env`, `auth.json`, or other tools' credential files. Moving that read behind Hermes' secret scope is a known follow-up.
- **Scheduled jobs:** the installer adds cron jobs to the profile's `cron/jobs.json` (check-ins, proactive messages). They stay silent until you consent during onboarding.
- **Processes:** detached delivery subprocesses for scheduled messages; optional local media generation (ComfyUI, ACE-Step) when you enable media.
- **Network:** only the Hermes provider you configure, Telegram, and the optional cloud media services (Gemini, Lyria) if you enable them.
- **No self-update, no core patching.** Hermes-internal imports are limited to the CLI/installer paths (`install.py`, `manual_cron.py`) and two guarded fallbacks that degrade when absent (`gateway.session_cron`, `hermes_cli.plugin_choices`).
- **Without a User profile** (fresh install) the plugin registers only onboarding and logs a warning; continuity hooks switch on after `/gumi-setup` and a Hermes restart.

## Privacy

Raw credentials live only in `<HERMES_HOME>/profiles/relic-gumi/.env`; canonical files store fingerprints, never secrets. Never publish either of these trees:

```text
$RELIC_HOME/user/
<HERMES_HOME>/profiles/relic-gumi/
```

## License

[AGPL-3.0-or-later](LICENSE)

The visual assets in `assets/readme/` use RIS - Relic Interface System (cyber77 theme). They contain no Cyberpunk 2077 screenshots, logos, or game artwork.

<div align="center">
  <img src="assets/readme/footer.svg" width="830" alt="End of transmission. Local state remains private.">
</div>
