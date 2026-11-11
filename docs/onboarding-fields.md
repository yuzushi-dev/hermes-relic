# Onboarding fields

Onboarding runs in a Telegram chat with your own bot, or in a local Hermes
chat: the Desktop chat panel, the CLI or the TUI. Closed questions are
buttons (one per row on Telegram, one native picker per question on the
Desktop, CLI and TUI); open questions take free text on Telegram and use
choice-only fallbacks on local surfaces. Answers are kept in `$RELIC_HOME/user/onboarding_progress.json`
(private, `0600`) so you can stop and resume — on the local surfaces too, where
`/gumi-setup` resumes from the last answered question. Nothing is written
to the canonical files until you press **Save** on the final summary.
`/gumi-setup` redoes the onboarding on every surface (on any other messaging platform, such as Discord or Slack, it only points you to Telegram or the local chat). After a "saved but
scheduling failed" notice, `/gumi-setup retry` re-runs only the scheduling
step on every surface and keeps your answers.

On the Desktop, CLI and TUI (Hermes platforms `desktop`, `cli`, `tui`,
`local`) the flow is the same battery, asked with one native picker
per question (up to 7 buttons, texts verbatim from the same catalog); the
name uses a Skip button and the time zone uses a list of common zones.
Hermes has no public free-text plugin API, so local onboarding cannot capture
a display name, an arbitrary IANA zone or custom quiet hours; quiet hours keep
22:00-08:00. Use Telegram onboarding for those free-text answers. All other
questions still use the public `ctx.ask_choice` API. While the setup is not complete, the first message on
these surfaces is answered by a fixed notice inviting you to run `/gumi-setup`;
no inference happens for it. The catalog and storage destinations
are shared across surfaces; the free-text restrictions above apply locally
(see the table below).

On Telegram, the first message you send, whatever it says (`/start`, "hi"), only
wakes the bot; Gumi answers with a fixed notice (fictional character, no human
reader, not a clinical service, 112 for emergencies), then the name question,
and your next text is the
name.

A question exists only if its answer lands in a canonical file **and** something
reads it at runtime. This page is derived from `relic.onboarding.questions`
(`build_questions()`) and `relic.onboarding.assemble`; prompts are shortened here.

Mode: **always** means asked in both setups; **Custom** means asked only when
you choose the Custom setup (Quick keeps the Gumi Classic defaults).

## Questions

The three media questions are phrased as friendly requests. The summary still
labels them Images, Audio and Music.

| id | type | mode | prompt | options | canonical field | read at runtime by |
|----|------|------|--------|---------|-----------------|--------------------|
| `name` | text | always | Nice to meet you! I'm Gumi 👋, who are you? | free text, or Skip | `baseline_response.baseline.display_name` | `context_injection` (runtime guidance) |
| `tz` | text | always | Which time zone are you in? (IANA name, validated) | free text | `baseline_response.baseline.timezone` | `user_runtime` (scheduling, quiet hours) |
| `mode` | choice | always | Choose your setup. | Quick (recommended) = `default`, Custom = `custom` | `gumi_profile.setup_mode` | legacy import only |
| `notice` | choice | always | Fixed notice: Gumi is a fictional character; no person reads the chat; this is not a clinical service; in an emergency call 112 or your local emergency services | Continue | none | - |
| `b01`..`b37` | choice | always | The baseline battery (see below) | scale labels | `baseline_response.baseline.battery` | check-in features, `context_injection` |
| `msglen` | choice | Custom | Message length? | Brief, Standard, Detailed | `gumi_profile.preferences.interaction.message_length` | `context_injection` |
| `emoji` | choice | Custom | Emoji? | None, Light, Frequent | `gumi_profile.preferences.interaction.emoji_use` | `context_injection` |
| `tone` | choice | Custom | Tone? | Warm, Neutral, Direct | `gumi_profile.preferences.relational.tone` | `context_injection` |
| `cont` | choice | Custom | Should I remember earlier conversations? | Only when you ask = `opt_in`, Never = `always_off` | `gumi_profile.preferences.relational.continuity` | `context_injection` |
| `safe1` | choice | Custom | Is romantic language okay with you? | No, Yes | `gumi_profile.preferences.safety.romantic_language` | `context_injection` |
| `safe2` | choice | Custom | Is sexual language okay with you? | No, Yes | `gumi_profile.preferences.safety.sexual_language` | `context_injection` |
| `img` | choice | always | Can I send you some pics of me sometimes? | Yes, No | `gumi_profile.media.images.selected` | media delivery (`user_runtime`, installer status) |
| `aud` | choice | always | Can I send you voice notes sometimes? | Yes, No | `gumi_profile.media.audio.selected` | media delivery |
| `mus` | choice | always | Can I send you some songs I write sometimes? | Yes, No | `gumi_profile.media.music.selected` | media delivery |
| `armed` | choice | always | Can I write to you here on my own (scheduled check-ins)? | Yes, No | `runtime_policy.delivery_execution_armed`, `checkin_state.recurring.status` | `user_runtime` (delivery) |
| `freq` | choice | Custom, only if `armed` = yes | Maximum frequency? | 1 per day, 2 per day, 3 per week | `gumi_profile.preferences.proactivity.maximum_frequency` | `user_runtime` (cadence) |
| `quiet` | text | Custom, only if `armed` = yes | Quiet hours as HH:MM-HH:MM (validated) | free text, or Keep 22:00-08:00 | `gumi_profile.preferences.proactivity.quiet_hours` | `user_runtime` (quiet hours) |
| `c_act` | choice | always | May I ask you questions on my own to get to know you better? | Yes (recommended), No | `user/checkin/delivery_policy.json: consent_for_active_elicitation` | follow-up chain, reply capture |
| `c_pas` | choice | always | May I learn from what you write in our chats? Classifiers are local by default; explicitly configured remote classifiers receive chat excerpts (including Ollama Cloud at ollama.com if selected). Separately enabled Google media (Gemini/Lyria, generativelanguage.googleapis.com) may receive text and recent chat/memory context; this consent does not enable it. | Yes (recommended), No | `user/checkin/delivery_policy.json: consent_for_passive_extraction` | passive extraction |
| `confirm` | choice | always | Summary of your answers | Save, Start over | none | - |

The safety questions (`safe1` and `safe2`) list "No" first. `exclusivity_language`
and `guilt_after_no_reply` are no longer questions: they are fixed, always-off
project rules (see "Not asked" below). The two consent
questions (`c_act`, `c_pas`) list "Yes (recommended)" first, then "No". Nothing
is preselected: a consent is `true` only if you tap Yes, and stays `false` when
the answer is missing. The consent file is merged, not replaced, so other keys
in it survive.

## The baseline battery

37 questions, shown as five screens, ids `b01`..`b37` (each battery prompt
shows its position as "(n/37)"). They record the
same registry items as the CLI battery: the ids (`TIPI_001`..`DIE_010`), the
recorded values and `reverse_scored` are unchanged. Only the Telegram wording
differs (`relic.onboarding.battery_text`, version `telegram-v2`, stored as
`baseline_response.baseline.battery_presentation`).

| screen | Telegram questions | items written |
|--------|--------------------|---------------|
| Big Five (TIPI) | 10 | 10 |
| Attachment (ECR-RS) | 9 | 9 |
| Interaction preferences | 8 | 8 |
| Relational comfort | 4 | 4 |
| Diegetic tolerance | 6 | 6 |

- **Big Five: the ten original TIPI items** (Gosling, Rentfrow, & Swann, 2003),
  asked one by one under the stem "I see myself as:" in the screen preamble.
  Item wording is the original. The scale uses the project's seven-point
  agreement anchors ("Strongly disagree" ... "Strongly agree", shared with the
  other screens), not the TIPI page's own anchors ("Disagree strongly" ...
  "Agree strongly"): a declared deviation in the labels only, the 1-7 values
  and scoring are unchanged.
  Reverse-scored items (`TIPI_002`, `004`, `006`, `008`, `010`) keep their
  registry keys. TIPI license, from the official page: "ANYONE CAN USE IT FOR
  ANY PURPOSE. NO NEED TO ASK ME FOR PERMISSION" — citation is given here and
  in the battery module.
- **Attachment: the 9-item global ECR-RS form** (Fraley, Heffernan, Vicary, &
  Brumbaugh, 2011; global wording as published by Fraley's lab, 2014 update of
  the measures page). The preamble asks about "close relationships in general"
  and the statements refer to people/others ("It helps to turn to people in
  times of need."). Keys are unchanged: avoidance `ECRRS_001`..`006` (with
  `001`..`004` reverse-scored), anxiety `ECRRS_007`..`009`. The measures page
  carries no explicit license clause; that absence is declared here.
- **Interaction, relational comfort, diegetic tolerance:** project calibration
  rewrites in the first person to Gumi ("I'd like you to ask before giving me
  advice", "How comfortable would you be if I ..."). The comfort scale is
  symmetric (Very uncomfortable ... Very comfortable). `DIE_009` uses an
  importance scale and `DIE_010` a separation scale (7 = completely separate).
  Recorded values keep their direction.

**Validity, stated plainly.** The TIPI and ECR-RS wording on Telegram is the
original validated instrument text (for ECR-RS, the global form, declared
above), so those scores are comparable with published usage of the same forms.
The other three screens remain project calibration rewrites: they calibrate
Gumi's style and are not an assessment. This setup is not a clinical
instrument.

The registry items not asked (the eleven with no runtime reader —
`INT_008`, `INT_009`, `INT_011`, `REL_003`, `REL_006`, `REL_008`, `REL_010`,
`DIE_003`..`DIE_006`; the image, audio and music tolerance facets among them
are covered by the real media questions) get conservative defaults, as in the
CLI. Answers are stored under
`baseline_response.baseline.battery` (`responses` and `scores`). The
progress file records the battery version (`catalog`, currently
`telegram-v2`). Progress saved by an older catalog (a different or missing
`catalog`, or unknown ids such as the removed `welcome` question and the old
`b38`..`b43`) is ignored for the battery: its `b*` answers are dropped because
those ids meant different questions, even if every id is still valid.

## Not asked

| field | why |
|-------|-----|
| `reply_pace` | written by the CLI, but nothing reads it at runtime |
| `recurring_messages` | no runtime reader |
| `flavor` | fixed to Gumi Classic, not a user choice |
| `exclusivity_language` | fixed always-off rule, hardcoded in `context_injection` |
| `guilt_after_no_reply` | fixed always-off rule, hardcoded in `context_injection` |

`outside_support` and `after_no_reply` have no runtime reader either; their
intended behavior (encourage support outside the chat, without reproach; no
nudging or guilt after a missed reply) is now a fixed instruction in
`context_injection._build_user_runtime_guidance`, not a configurable field.

These keep their defaults. They can be added once something reads them.

## Never asked in chat

- **Gemini API key.** A key typed into Telegram would stay in the chat
  history. Put `GEMINI_API_KEY` in the profile `.env` yourself. Without it,
  media you picked stay `selected: true, enabled: false` with a remediation
  note; add the key and run `/gumi-setup` again. Delivery additionally requires
  your explicit `RELIC_ENABLE_GOOGLE_MEDIA=1` in that environment; a key alone
  is not consent. Google (`generativelanguage.googleapis.com`, Gemini/Lyria)
  can receive voice text, prompts and recent chat/memory context. Current
  ComfyUI/ACE-Step rendering is local, but Google prompt-generation fallbacks
  mean the same delivery gate covers those branches too. Passive-extraction
  consent (`c_pas`) never enables media or sets this flag.
- **Telegram bot token.** It is already in the profile `.env`; onboarding uses
  the connected bot and never asks for it.

## Derived, not asked

- `delivery_capability_enabled` is `true` (Telegram is configured by the installer).
- `TELEGRAM_HOME_CHANNEL` is the chat the onboarding runs in, stored in the
  profile `.env` with a fingerprint in `credential_references`.
- The first message is derived from `armed`, not asked. `armed` = Yes: Gumi
  writes first (`runtime_policy.first_message` = `gumi_sends_first`,
  `checkin_state.first_message.status` = `requested`). `armed` = No: Gumi waits
  (`wait`, `waiting`). The CLI still asks this question separately.
- `baseline.language` comes from the locale; onboarding text is English only.
