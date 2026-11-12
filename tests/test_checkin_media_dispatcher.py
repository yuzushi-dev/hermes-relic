from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from relic.gumi_plugin import checkin_media_dispatcher as dispatcher
from relic.gumi_plugin import image_gen
from relic.profile.registry import ProfileRegistry


_VALID_TELEGRAM_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


@pytest.fixture(autouse=True)
def media_credentials(scoped_secrets):
    scoped_secrets["GEMINI_API_KEY"] = "test-google-key"


def _write_user_state(
    tmp_path: Path,
    *,
    capability: bool = True,
    armed: bool = True,
    media_enabled: bool = True,
) -> Path:
    relic_home = tmp_path / "relic"
    registry = ProfileRegistry(
        relic_home=relic_home,
        hermes_profiles_home=tmp_path / "hermes" / "profiles",
    )
    user = registry.initialize_user()
    private_env = {
        "TELEGRAM_BOT_TOKEN": _VALID_TELEGRAM_TOKEN,
        "TELEGRAM_HOME_CHANNEL": "123456789",
    }
    from tests.secret_fakes import seed_credentials
    seed_credentials(private_env)
    registry.materialize_user_artifacts(
        user,
        baseline={},
        gumi_profile={
            "flavor": {"id": "gumi-classic", "version": 1},
            "setup_mode": "default",
            "preferences": {},
            "media": {
                name: {"selected": media_enabled, "enabled": media_enabled}
                for name in ("images", "audio", "music")
            },
        },
        runtime_policy={
            "delivery_capability_enabled": capability,
            "delivery_execution_armed": armed,
            "first_message": "wait",
            "credential_references": {
                name: {
                    "location": "hermes_profile:.env",
                    "fingerprint": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                }
                for name, value in private_env.items()
            },
        },
        checkin_state={
            "recurring": {"status": "armed" if armed else "off"},
            "first_message": {"status": "waiting"},
        },
        private_env=private_env,
    )
    return relic_home


def _allow_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "require_outbound_ready", lambda _home: None)
    monkeypatch.setattr(dispatcher, "_critic_block_reason", lambda _text: None)
    monkeypatch.setattr(
        dispatcher, "_prose_block_reason", lambda _text, decision_type="": None
    )


@pytest.mark.parametrize("tipo", ("voice", "image", "music"))
def test_ambient_key_cannot_start_media_without_explicit_opt_in(tmp_path, monkeypatch, tipo):
    relic_home = _write_user_state(tmp_path)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-key")
    monkeypatch.delenv("RELIC_ENABLE_GOOGLE_MEDIA", raising=False)
    _allow_lifecycle(monkeypatch)
    monkeypatch.setattr(dispatcher, "synthesize_checkin_audio", pytest.fail)
    monkeypatch.setattr(dispatcher, "generate_checkin_image", pytest.fail)
    monkeypatch.setattr(dispatcher, "LyriaGenerator", pytest.fail)
    monkeypatch.setattr(image_gen, "generate_image", pytest.fail)
    monkeypatch.setattr("requests.post", pytest.fail)
    result = dispatcher.dispatch(
        f"tipo: {tipo}\nSynthetic companion text",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )
    assert result == {"tipo": tipo, "success": False, "reason": "no_api_key"}


@pytest.mark.parametrize("tipo", ("text", "voice", "image", "music"))
def test_revoked_canonical_user_blocks_every_delivery_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tipo: str,
) -> None:
    relic_home = _write_user_state(tmp_path, capability=True, armed=False)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    # These tests exercise explicitly enabled media, not ambient credentials.
    monkeypatch.setenv("RELIC_ENABLE_GOOGLE_MEDIA", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "a" * 35)
    _allow_lifecycle(monkeypatch)
    monkeypatch.setattr(dispatcher, "_send_telegram_text", pytest.fail)
    monkeypatch.setattr(dispatcher, "_send_telegram_media", pytest.fail)
    monkeypatch.setattr(dispatcher, "synthesize_checkin_audio", pytest.fail)
    monkeypatch.setattr(dispatcher, "generate_checkin_image", pytest.fail)
    monkeypatch.setattr(dispatcher, "LyriaGenerator", pytest.fail)

    result = dispatcher.dispatch(
        f"tipo: {tipo}\nCiao",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert result["success"] is False
    assert capsys.readouterr().out == ""


def test_canonical_capability_disabled_blocks_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    relic_home = _write_user_state(tmp_path, capability=False, armed=False)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    _allow_lifecycle(monkeypatch)
    monkeypatch.setattr(dispatcher, "_send_telegram_text", pytest.fail)

    result = dispatcher.dispatch(
        "tipo: text\nCiao",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert result["success"] is False
    assert capsys.readouterr().out == ""

@pytest.mark.parametrize("tipo", ("voice", "image", "music"))
def test_disabled_canonical_media_blocks_media_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tipo: str,
) -> None:
    relic_home = _write_user_state(tmp_path, media_enabled=False)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    _allow_lifecycle(monkeypatch)
    monkeypatch.setattr(dispatcher, "synthesize_checkin_audio", pytest.fail)
    monkeypatch.setattr(dispatcher, "generate_checkin_image", pytest.fail)
    monkeypatch.setattr(dispatcher, "LyriaGenerator", pytest.fail)

    result = dispatcher.dispatch(
        f"tipo: {tipo}\nCiao",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert result == {
        "tipo": tipo,
        "success": False,
        "reason": "canonical_delivery_not_armed",
    }


def test_malformed_canonical_onboarding_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    relic_home = _write_user_state(tmp_path)
    policy = relic_home / "user" / "runtime_policy.json"
    policy.write_text("{not json", encoding="utf-8")
    policy.chmod(0o600)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    _allow_lifecycle(monkeypatch)
    monkeypatch.setattr(dispatcher, "_send_telegram_text", pytest.fail)

    result = dispatcher.dispatch(
        "tipo: text\nCiao",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert result["success"] is False
    assert capsys.readouterr().out == ""


def test_missing_canonical_user_blocks_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    relic_home = _write_user_state(tmp_path)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    _allow_lifecycle(monkeypatch)
    monkeypatch.setattr(dispatcher, "_send_telegram_text", pytest.fail)

    result = dispatcher.dispatch(
        "tipo: text\nCiao",
        hermes_home=tmp_path / "hermes",
        relic_user_home=tmp_path / "missing-user",
    )

    assert result == {
        "tipo": "text",
        "success": False,
        "reason": "invalid_user_profile",
    }
    assert capsys.readouterr().out == ""


def test_stdout_delivery_rechecks_canonical_guard_before_printing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    relic_home = _write_user_state(tmp_path)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    _allow_lifecycle(monkeypatch)
    checks = iter((True, False))
    monkeypatch.setattr(
        dispatcher,
        "_canonical_outbound_delivery_allowed",
        lambda *_args: next(checks),
    )
    monkeypatch.setattr(dispatcher, "_send_telegram_text", pytest.fail)

    result = dispatcher.dispatch(
        "tipo: text\nCiao",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert result["success"] is False
    assert capsys.readouterr().out == ""


def test_text_branch_sanitizes_stdout_and_telegram_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    relic_home = _write_user_state(tmp_path)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    _allow_lifecycle(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(
        dispatcher,
        "_send_telegram_text",
        lambda _home, text: sent.append(text) or False,
    )

    result = dispatcher.dispatch(
        "tipo: text\n[WARN] secret\nCiao",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert result["output"] == "Ciao?"
    assert sent == ["Ciao?"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Ciao" not in captured.err
    assert "text delivery chars=5" in captured.err


def test_voice_branch_sanitizes_source_before_synthesis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relic_home = _write_user_state(tmp_path)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    # These tests exercise explicitly enabled media, not ambient credentials.
    monkeypatch.setenv("RELIC_ENABLE_GOOGLE_MEDIA", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "a" * 35)
    _allow_lifecycle(monkeypatch)
    captured: dict[str, str] = {}
    def synthesize(text: str, *_args, **_kwargs) -> Path:
        captured["text"] = text
        return tmp_path / "voice.ogg"

    monkeypatch.setattr(dispatcher, "synthesize_checkin_audio", synthesize)
    monkeypatch.setattr(dispatcher, "record_media_delivery", lambda *_args: None)
    monkeypatch.setattr(dispatcher, "_send_telegram_media", lambda *_args, **_kwargs: False)

    result = dispatcher.dispatch(
        "tipo: voice\n[WARN] secret\nCiao",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert result["success"] is True
    assert captured["text"] == "Ciao?"


@pytest.mark.parametrize("caption", ["La luce entra dalla finestra", "[SILENT]", ""])
def test_image_contract_without_extra_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caption: str,
) -> None:
    relic_home = _write_user_state(tmp_path)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    _allow_lifecycle(monkeypatch)
    monkeypatch.setattr(dispatcher, "_send_telegram_text", pytest.fail)
    monkeypatch.setattr(dispatcher, "_send_telegram_media", pytest.fail)
    monkeypatch.setattr(image_gen, "generate_image", pytest.fail)

    result = dispatcher.dispatch(
        f"tipo: image\ncaption: {caption}\nimage_prompt: A sunlit room",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
        continuity_kind="diegetic",
        dry_run=True,
    )

    if caption and caption != "[SILENT]":
        assert result == {"tipo": "image", "success": True, "dry_run": True}
    else:
        assert result["success"] is False
        assert result["reason"] == "sanitized_empty"


def test_image_branch_sanitizes_caption_and_generation_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relic_home = _write_user_state(tmp_path)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    # These tests exercise explicitly enabled media, not ambient credentials.
    monkeypatch.setenv("RELIC_ENABLE_GOOGLE_MEDIA", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "a" * 35)
    _allow_lifecycle(monkeypatch)
    monkeypatch.setattr(
        dispatcher,
        "parse_gate_output",
        lambda _text: {
            "tipo": "image",
            "testo": "Descrizione",
            "caption": "[WARN] secret\nDidascalia",
            "image_prompt": "[WARN] secret\nUn ritratto",
        },
    )
    captured: dict[str, str] = {}
    monkeypatch.setattr(image_gen, "collect_reference_images", lambda *_args: [])
    def generate_image(prompt: str, *_args) -> Path:
        captured["prompt"] = prompt
        return tmp_path / "image.jpg"

    monkeypatch.setattr(image_gen, "generate_image", generate_image)
    monkeypatch.setattr(dispatcher, "record_media_delivery", lambda *_args: None)
    def send_image(_home, _path, _tipo, *, caption: str = "", **_kwargs) -> bool:
        captured["caption"] = caption
        return False

    monkeypatch.setattr(dispatcher, "_send_telegram_media", send_image)

    result = dispatcher.dispatch(
        "ignored",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert result["success"] is True
    assert captured == {"prompt": "Un ritratto", "caption": "Didascalia?"}


def test_music_branch_sanitizes_prompt_and_title_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relic_home = _write_user_state(tmp_path)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    # These tests exercise explicitly enabled media, not ambient credentials.
    monkeypatch.setenv("RELIC_ENABLE_GOOGLE_MEDIA", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "a" * 35)
    _allow_lifecycle(monkeypatch)
    captured: dict[str, str] = {}

    class _Generator:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def generate_and_deliver(self, *, lyria_prompt: str, **_kwargs) -> dict[str, object]:
            captured["prompt"] = lyria_prompt
            return {
                "success": True,
                "caption": "[WARN] secret\nNinna nanna",
                "audio_path": str(tmp_path / "song.wav"),
            }

    monkeypatch.setattr(dispatcher, "LyriaGenerator", _Generator)
    monkeypatch.setattr(dispatcher, "record_media_delivery", lambda *_args: None)
    def send_music(_home, _path, _tipo, *, title: str = "", **_kwargs) -> bool:
        captured["title"] = title
        return False

    monkeypatch.setattr(dispatcher, "_send_telegram_media", send_music)

    result = dispatcher.dispatch(
        "tipo: music\n[WARN] secret\nCanto",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert result["success"] is True
    assert captured == {"prompt": "Canto", "title": "Ninna nanna"}

def _assert_no_legacy_system_labels(value: object) -> None:
    forbidden = ("subject", "experiment", "researcher", "consent", "emergency", "risk")
    if isinstance(value, dict):
        for key, item in value.items():
            assert not any(label in str(key).lower() for label in forbidden)
            _assert_no_legacy_system_labels(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_legacy_system_labels(item)
    elif isinstance(value, str):
        assert not any(label in value.lower() for label in forbidden)


def test_delivered_event_uses_only_canonical_runtime_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relic_home = _write_user_state(tmp_path)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        "relic.chronicle.emitter.emit_event",
        lambda **kwargs: captured.append(kwargs),
    )
    monkeypatch.setattr(dispatcher, "require_outbound_ready", lambda _home: None)
    monkeypatch.setattr(dispatcher, "_critic_block_reason", lambda _text: None)
    monkeypatch.setattr(
        dispatcher, "_prose_block_reason", lambda _text, decision_type="": None
    )
    monkeypatch.setattr(dispatcher, "_send_telegram_text", lambda *_args: True)
    monkeypatch.setattr(dispatcher, "record_outbound_delivery", lambda *_args: None)

    result = dispatcher.dispatch(
        "tipo: text\nCiao",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert result["telegram_delivered"] is True
    assert len(captured) == 1
    assert captured[0]["runtime_scope_id"] == ProfileRegistry(relic_home).load_user().runtime_scope_id
    _assert_no_legacy_system_labels(captured[0])

def test_delivered_event_tags_use_chronicle_key_value_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relic_home = _write_user_state(tmp_path)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        "relic.chronicle.emitter.emit_event",
        lambda **kwargs: captured.append(kwargs),
    )
    monkeypatch.setattr(dispatcher, "require_outbound_ready", lambda _home: None)
    monkeypatch.setattr(dispatcher, "_critic_block_reason", lambda _text: None)
    monkeypatch.setattr(
        dispatcher, "_prose_block_reason", lambda _text, decision_type="": None
    )
    monkeypatch.setattr(dispatcher, "_send_telegram_text", lambda *_args: True)
    monkeypatch.setattr(dispatcher, "record_outbound_delivery", lambda *_args: None)

    dispatcher.dispatch(
        "tipo: text\nCiao",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert captured
    assert captured[0]["tags"] == ["channel:outbound", "channel:telegram"]


def test_music_performer_ignores_legacy_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relic_home = _write_user_state(tmp_path)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    # These tests exercise explicitly enabled media, not ambient credentials.
    monkeypatch.setenv("RELIC_ENABLE_GOOGLE_MEDIA", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "a" * 35)
    _allow_lifecycle(monkeypatch)
    legacy_profile = relic_home / "user" / "gumi_background_profile.json"
    legacy_profile.write_text(
        '{"display_name": "Researcher Echo"}',
        encoding="utf-8",
    )
    original_read_text = Path.read_text

    def read_text(path: Path, *args, **kwargs) -> str:
        if path == legacy_profile:
            pytest.fail("legacy metadata was read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    captured: dict[str, str] = {}
    events: list[dict[str, object]] = []

    class _Generator:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def generate_and_deliver(self, **_kwargs) -> dict[str, object]:
            return {
                "success": True,
                "caption": "Ninna nanna",
                "audio_path": str(tmp_path / "song.wav"),
            }

    monkeypatch.setattr(dispatcher, "LyriaGenerator", _Generator)
    monkeypatch.setattr(dispatcher, "record_media_delivery", lambda *_args: None)
    monkeypatch.setattr(dispatcher, "record_outbound_delivery", lambda *_args: None)
    monkeypatch.setattr(dispatcher, "record_sent_media_memory", lambda *_args: None)
    monkeypatch.setattr(
        "relic.chronicle.emitter.emit_event",
        lambda **kwargs: events.append(kwargs),
    )

    def send_music(_home, _path, _tipo, *, performer: str = "", **_kwargs) -> bool:
        captured["performer"] = performer
        return True

    monkeypatch.setattr(dispatcher, "_send_telegram_media", send_music)

    result = dispatcher.dispatch(
        "tipo: music\nCanto",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert result["telegram_delivered"] is True
    assert captured == {"performer": "Gumi"}
    assert len(events) == 1
    assert events[0]["runtime_scope_id"] == ProfileRegistry(relic_home).load_user().runtime_scope_id
    _assert_no_legacy_system_labels(events[0])


def test_user_authored_text_is_not_lexically_censored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relic_home = _write_user_state(tmp_path)
    monkeypatch.setenv("RELIC_HOME", str(relic_home))
    _allow_lifecycle(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(
        dispatcher,
        "_send_telegram_text",
        lambda _home, text: sent.append(text) or False,
    )

    dispatcher.dispatch(
        "tipo: text\nUna ricercatrice mi ha consigliato questo libro",
        hermes_home=tmp_path / "hermes",
        relic_user_home=relic_home / "user",
    )

    assert sent == ["Una ricercatrice mi ha consigliato questo libro?"]
