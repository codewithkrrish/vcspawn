"""Tests for template rendering, sanitising, and member values."""

from vcspawn.naming import (
    MAX_CHANNEL_NAME_LENGTH,
    render_template,
    sanitise_channel_name,
    values_for_member,
)


class FakeActivity:
    def __init__(self, name):
        self.name = name


class FakeMember:
    def __init__(self, display_name="Krrish", name="krrish", activity=None):
        self.display_name = display_name
        self.name = name
        self.activity = activity


def test_render_fills_known_placeholders():
    result = render_template("{user}'s room {n} — {game}", {
        "user": "Krrish", "n": 2, "game": "Valorant",
    })
    assert result.text == "Krrish's room 2 — Valorant"
    assert not result.has_unknown


def test_render_leaves_unknown_placeholders_and_reports_them():
    result = render_template("hi {user}, welcome to {sever}", {"user": "Krrish"})
    assert result.text == "hi Krrish, welcome to {sever}"
    assert result.unknown == ["sever"]
    assert result.has_unknown


def test_render_keeps_doubled_braces_literal():
    result = render_template("{{user}} is not a placeholder", {"user": "Krrish"})
    assert result.text == "{user} is not a placeholder"


def test_render_empty_template():
    assert render_template("", {}).text == ""


def test_sanitise_strips_control_chars_and_collapses_spaces():
    assert sanitise_channel_name("late\nnight\t  grind") == "late night grind"


def test_sanitise_truncates_to_discord_limit():
    long = "x" * (MAX_CHANNEL_NAME_LENGTH + 50)
    assert len(sanitise_channel_name(long)) == MAX_CHANNEL_NAME_LENGTH


def test_sanitise_falls_back_when_nothing_is_left():
    assert sanitise_channel_name("   \n\t  ") == "voice-chat"
    assert sanitise_channel_name("") == "voice-chat"


def test_values_include_game_from_activity():
    values = values_for_member(FakeMember(activity=FakeActivity("Minecraft")), 3)
    assert values["game"] == "Minecraft"
    assert values["n"] == 3
    assert values["user"] == "Krrish"
    assert values["username"] == "krrish"
    assert values["count"] == 1


def test_values_fall_back_to_chilling_without_activity():
    assert values_for_member(FakeMember(activity=None), 1)["game"] == "chilling"


def test_values_ignore_blank_activity_name():
    assert values_for_member(FakeMember(activity=FakeActivity("   ")), 1)["game"] == "chilling"
