<!-- Visual assets follow RIS - Relic Interface System (cyber77 theme). -->
<p align="center">
  <a href="assets/readme/showcase.mp4">
    <img src="assets/readme/header.png" width="830" alt="RELIC GUMI: a local, longitudinal AI companion for Hermes Agent. Click to watch the 60-second showcase.">
  </a>
</p>

<div align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/LICENSE-AGPL--3.0--or--later-ff003c?style=flat-square&labelColor=120a0d" alt="License: AGPL-3.0-or-later"></a> <img src="https://img.shields.io/badge/PYTHON-3.10%2B-00e5ff?style=flat-square&labelColor=120a0d" alt="Python 3.10 or newer"> <img src="https://img.shields.io/badge/STORAGE-LOCAL-00e57e?style=flat-square&labelColor=120a0d" alt="Local storage; configurable inference"> <img src="https://img.shields.io/badge/HERMES-0.23%2B-ffe23a?style=flat-square&labelColor=120a0d" alt="Hermes 0.23 or newer">
</div>

**Gumi is a companion that gets to know you, and helps you reflect on who you are. It builds a longitudinal model of you from published psychometric instruments (TIPI for the Big Five, ECR-RS for attachment) used as a self-report baseline, plus your consented check-ins, and stores its model on your machine. Classifiers are local by default, not necessarily the whole conversation: inference follows your configured Hermes provider. Optional cloud media services require explicit enablement; remote classifiers require explicit configuration and send content to their providers.**

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

Classifier defaults use local Ollama at `http://localhost:11434` (Gemma 4 E4B; the attribution panel also needs Qwen 3 8B and Llama 3.1 8B). Missing local models fail without a cloud fallback. To opt into remote classification, explicitly set `RELIC_OLLAMA_ENDPOINT`, `RELIC_JURY_CLOUD_ENDPOINT`, or `RELIC_JUDGE_BASE_URL` and the corresponding model settings. Ollama Cloud (`https://ollama.com/v1`) receives chat excerpts if you select it; cloud tags on a local Ollama daemon also forward content to Ollama Cloud. Normal conversation uses your configured Hermes provider, which may be remote. Media pipelines are off unless you select media during onboarding and explicitly set `RELIC_ENABLE_GOOGLE_MEDIA=1` plus `GEMINI_API_KEY` in the profile environment. This authorizes Google (`generativelanguage.googleapis.com`) to process voice text and image/music prompt context, which can include recent chat or memory excerpts. Images currently render with local ComfyUI and music with local ACE-Step, but their prompt-generation fallbacks still use Gemini; the retained Gemini/Lyria generators also send prompts (and image references for Gemini images) to Google if explicitly invoked. The shared opt-in gate applies even to local media branches. Local storage does not imply local inference.

## Where it comes from

RELIC Gumi is the consumer face of [`relic-oss`](https://github.com/yuzushi-dev/Relic), a research project on longitudinal, theory-grounded modeling of human-AI companion relationships (grounded in Cognitive Appraisal Theory, Self-Determination Theory, Attachment Theory, and Dual-Process Theory), described in the paper above ([arXiv:2611.08504](https://arxiv.org/abs/2611.08504)). This package keeps only the canonical, single-User path; the multi-subject research CLI and the persona generator it drove aren't part of it.

## The engram only you can read

A 60-facet registry exists to hold a longitudinal model of you, seeded with conservative defaults and filled in from eligible, consented check-in and passive observations over time; a fresh install starts nearly empty and fills in only as consented check-ins accumulate. Nothing here is meant to diagnose or label you. The stored model stays on your machine. Chat excerpts may be processed by your configured inference providers as described above.

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

The installer no longer asks questions. Onboarding (the psychometric baseline, preferences, optional media, and consents) happens in a chat with your own bot: on Telegram with buttons, one per row, for the closed answers and free text for the open ones; in the Hermes Desktop chat panel, the CLI or the TUI with one native picker per question (`/gumi-setup` starts or resumes it, and while the setup is incomplete the first message is answered by a notice inviting you to run it). Every question and where its answer goes is listed in [docs/onboarding-fields.md](docs/onboarding-fields.md). Send `/gumi-setup` to the bot to redo it. `relic-gumi configure` only prints instructions and exits with status 1; it does not perform onboarding. Use `/gumi-setup` inside the target Hermes profile.

If an AI agent installs this for you, it follows [AGENTS.md](AGENTS.md). In short, it:

1. installs the package and plugin (`relic-gumi install --hermes-home <path>`);
2. asks you to configure `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, and `TELEGRAM_HOME_CHANNEL` through Hermes Desktop -> Settings -> Plugins masked fields (see [Security and credentials](#security-and-credentials));
3. checks the provider and model the installer imported: `relic-gumi install` copies `model.provider` and `model.default` (and `api_mode` / `base_url` when present) from the base Hermes config into the new profile, never overwriting a model the profile already has and never copying credentials. Only when the base config has no usable model does the agent set them by hand (`hermes --profile relic-gumi config set model.provider ...` and `model.default ...`), and asks you to enter provider credentials privately through Hermes' supported configuration/sign-in flow;
4. runs `relic-gumi verify --skip-cron` and `hermes --profile relic-gumi gateway status`;
5. tells you to open Telegram and send `/start` to your bot.

The installer creates and owns its own Hermes profile, `<HERMES_HOME>/profiles/relic-gumi/`. It never reads or changes a profile called `gumi` (or any other name). If a `relic-gumi` profile already exists that the installer did not create (it is not empty and has no `.relic-gumi-installer.json` or `.relic-gumi/lifecycle.json`), `install` and `uninstall` stop with an error before writing anything: pick another `--hermes-home`, or remove or rename that profile yourself. A run that failed halfway leaves a profile the next run accepts.

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
- **Files:** state under `$RELIC_HOME` (default `~/.relic`); the installer writes the `relic-gumi` Hermes profile with non-secret templates and cron configuration. It may create an empty private `.env`, but never populates it with credentials.
- **Credentials:** runtime hooks, onboarding, delivery, and the attribution jury use `agent.secret_scope.get_secret(NAME)` exclusively. They never parse `.env`, `auth.json`, or other credential stores, and never use bare process-environment reads for credentials. Hermes respects the active profile scope; without a scope, single-profile Hermes may use the process environment, while multiplexing raises `UnscopedSecretError`. Missing APIs and provider errors return no credential. Detached delivery sends the job (including conversation text) and the active scope snapshot only over a private pipe, never argv. The UTF-8 envelope is capped at 256 KiB; nonblocking handoff has a one-second deadline after process creation, with failed children killed and reaped (up to one additional second). Worker stdout/stderr are discarded so provider text cannot enter persistent logs; the handoff log records only PID, byte count, and queued/failed status, not delivery confirmation. No scoped credentials are inherited through environment variables.
- **Scheduled jobs:** the installer adds cron jobs to the profile's `cron/jobs.json` (check-ins, proactive messages). They stay silent until you consent during onboarding.
- **Processes:** detached delivery subprocesses for scheduled messages; optional local media generation (ComfyUI, ACE-Step) when you enable media.
- **Network:** the Hermes provider you configure, Telegram, local Ollama/ComfyUI/ACE-Step by default, any classifier or media endpoints you explicitly override, and Google media services (Gemini/Lyria) only after the separate opt-in described above.
- **No self-update, no core patching.** Runtime onboarding uses public `ctx.ask_choice`; without a public free-text API, local setup uses choice-only fallbacks (Skip name, common time zones, default quiet hours). Cron identity uses `cron.execution_identity.current_cron_execution()` and `.job_id`, with a session-name fallback when unavailable. Other Hermes-internal imports remain in operator CLI/installer paths (`install.py`, `manual_cron.py`).
- **Without a User profile** (fresh install) the plugin registers only onboarding and logs a warning; continuity hooks switch on after `/gumi-setup` and a Hermes restart.

Standalone `relic-gumi install` cannot verify credentials for the target profile:
it reports **Credentials not checked**, never missing/present based on the
operator's environment. `relic-gumi configure` now prints setup instructions and
returns nonzero without creating User artifacts or activating delivery. Use
`hermes --profile relic-gumi` and `/gumi-setup` inside that Hermes runtime (or the
profile's Telegram bot). That is where Hermes supplies the credential context.
A standalone CLI process, even with `HERMES_HOME` set, does not prove that context.

## Security and credentials

Recommended: select the `relic-gumi` profile in **Hermes Desktop -> Settings -> Plugins** and use its **masked** secret fields.

CLI alternative: the **user must run** the commands below themselves, replacing
`<value>` privately. The value appears in the command line and may remain in shell
history. Installer agents must never read, print, or pass these values. Commands
without a value show usage; they do not open a private prompt:

```bash
hermes --profile relic-gumi config set TELEGRAM_BOT_TOKEN <value>
hermes --profile relic-gumi config set TELEGRAM_ALLOWED_USERS <value>
hermes --profile relic-gumi config set TELEGRAM_HOME_CHANNEL <value>
hermes --profile relic-gumi config set GEMINI_API_KEY <value>
# Only for explicitly configured Ollama Cloud classification:
hermes --profile relic-gumi config set OLLAMA_API_KEYS <value>
```

Telegram IDs must be numeric; set the home channel to the chat where you want
scheduled messages. Restart the affected Hermes gateway after changing its
credentials, then run `/gumi-setup` to update consent and credential fingerprints.
Never send tokens to an agent or paste them into an onboarding chat. Relic does
not collect, copy, or persist credentials, including during reconfiguration.

All five names are declared as optional `config_schema` secret fields in
`plugin.yaml`; `requires_env: []` intentionally keeps continuity and Desktop setup
available without Telegram, Google media, or Ollama Cloud. Telegram delivery
requires configured credentials and explicit arming; media additionally requires
its existing separate opt-in. Missing credentials disable only those features.
Hermes >=0.23.0 is the supported floor. Hosts without the scoped-secret API fail
closed rather than falling back to another profile's environment.

The legacy migration CLI rejects credential-file input: configure credentials
through Hermes separately. Dry-run snapshots omit `.env` and `auth.json` entirely.

## Privacy

Hermes owns credential storage; canonical Relic files store fingerprints, never secrets. Never publish either of these trees:

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
