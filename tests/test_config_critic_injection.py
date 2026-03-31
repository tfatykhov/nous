"""Tests for critic skill injection config settings (issue #229)."""
import pytest
from pydantic import ValidationError
from nous.config import Settings


def test_critic_skill_injection_default():
    s = Settings(_env_file=None)
    assert s.critic_skill_injection == "disabled"


def test_critic_skill_injection_values():
    for val in ("enabled", "disabled", "log_only"):
        s = Settings(_env_file=None, critic_skill_injection=val)
        assert s.critic_skill_injection == val


def test_critic_skill_injection_invalid():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, critic_skill_injection="bogus")


def test_critic_skill_slots_default():
    s = Settings(_env_file=None)
    assert s.critic_skill_slots == 2


def test_embedding_skill_slots_default():
    s = Settings(_env_file=None)
    assert s.embedding_skill_slots == 3


def test_critic_skill_slots_ge_zero():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, critic_skill_slots=-1)


def test_embedding_skill_slots_ge_zero():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, embedding_skill_slots=-1)
