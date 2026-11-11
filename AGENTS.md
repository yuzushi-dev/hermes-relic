# AGENTS.md - instructions for the agent installing relic-gumi

You are an agent (Claude, Codex, Hermes or another) installing the relic-gumi plugin on behalf of a user.

## What you CAN and MUST do
1. Install the package and the plugin by following the flow for a Hermes managed by its package manager, or an unmanaged one, described in the Install section of `README.md`. Then run `relic-gumi install --hermes-home <path>`; do not install packages with pip directly into environments managed by Hermes.
   If a `relic-gumi` profile already exists that the installer did not create, `relic-gumi install` stops with an error without modifying it: do not work around it, report the message to the user (choose another `--hermes-home`, or remove/rename that profile by hand). A profile named `gumi` is not the installer's concern: do not read it and do not touch it.
2. Configure the Telegram channel in the `relic-gumi` Hermes profile: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USERS` (the user's numeric ID) in the profile's `.env`. Ask the user for the token and the ID; never print them, never log them.
3. Default provider and model: `relic-gumi install` imports them on its own from the `model` block of the base Hermes config (`provider`, `default`, and `api_mode`/`base_url` if present), without overwriting a model already present in the profile and without copying credentials. Read the installer output (the `relic-gumi model:` lines). Only if it says "No default model to import" set it yourself: `hermes --profile relic-gumi config set model.provider <provider>` and `hermes --profile relic-gumi config set model.default <model-id>`, with the provider credentials in the profile's `.env` (ask the user for them, do not print them). If the installer prints the command `hermes --profile relic-gumi plugins install <provider> --enable`, the provider is a plugin that is not yet enabled in the profile: the installer does not install it; report the command to the user and run it only if the user has authorized it.
4. Check: `relic-gumi verify --skip-cron` and `hermes --profile relic-gumi gateway status`.
5. Tell the user to open Telegram and send `/start` to their own bot.

## What you must NOT do
- Do NOT answer the onboarding questions yourself, do NOT choose preferences, consents, timezone, name or battery answers for the user, do NOT edit by hand the files in `$RELIC_HOME/user/`, do NOT set `consent_for_*`.
- Onboarding happens ONLY in the user's own chat: Telegram with their bot, or `/gumi-setup` in the Hermes Desktop chat panel, CLI or TUI. If the user asks you to do it, refuse and send them there.
- Do not ask for Gemini keys or other media secrets during onboarding.
- Classifiers are local by default; conversation inference uses the user's Hermes provider and may be remote. Never claim the whole plugin runs fully locally. Do not enable remote classifiers or Google media on the user's behalf. Google media requires the user's explicit `RELIC_ENABLE_GOOGLE_MEDIA=1` plus `GEMINI_API_KEY`; prompts, voice text and recent chat/memory context may be sent to `generativelanguage.googleapis.com` (Gemini/Lyria). The shared gate also covers local media renderers because of Google prompt fallbacks.

## End of your task
When the gateway and verify are fine and the user has been told to send `/start`, you are done. Do not wait for the outcome of the onboarding.
