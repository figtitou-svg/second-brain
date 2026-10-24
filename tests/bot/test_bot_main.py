import pytest

from bot.main import load_config
from bot.transcribe import transcribe_audio
from bot.fakes import FakeRouter
from bot.ports import CoachAdapter, NotReadyError, RouterAdapter, MemoryAdapter


def test_load_config_ok():
    c = load_config({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USER_ID": "123"})
    assert c.allowed_user_id == 123 and c.token == "t"


@pytest.mark.parametrize("env", [
    {}, {"TELEGRAM_BOT_TOKEN": "t"}, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USER_ID": "abc"},
    {"TELEGRAM_ALLOWED_USER_ID": "1"},
])
def test_load_config_missing(env):
    with pytest.raises(RuntimeError):
        load_config(env)


def test_transcribe_delegates_and_strips():
    r = FakeRouter("  bonjour \n")
    assert transcribe_audio("a.ogg", r) == "bonjour"
    assert r.transcribed == ["a.ogg"]


def test_adapters_tolerate_missing_modules():
    with pytest.raises(NotReadyError):
        MemoryAdapter("module_inexistant_xyz").ask("q")
    with pytest.raises(NotReadyError):
        MemoryAdapter("module_inexistant_xyz").add_note("a", "private", "s")


def test_adapters_construct_without_import():
    CoachAdapter()
    RouterAdapter()
