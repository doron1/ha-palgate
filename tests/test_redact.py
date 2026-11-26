"""Tests for the debug-log redaction helpers (no Home Assistant needed)."""

import re

import pytest

from custom_components.palgate.redact import (
    Redacted,
    redact,
    redact_tag,
    redact_url,
)

TAG = re.compile(r"<redacted:[0-9a-f]{6}>")

# Shaped like a real get_device_log response, with made-up values
LOG_PAYLOAD = {
    "log": [
        {
            "userId": "972501234567",
            "operation": "Output 1",
            "time": 1795698302,
            "firstname": "972501234567",
            "lastname": "",
            "image": False,
            "reason": 0,
            "type": 100,
            "sn": "",
        },
        {
            "userId": "0",
            "operation": "srundefined",
            "time": 1795422889,
            "firstname": "Unknown",
            "image": False,
            "reason": 1,
            "type": 1,
            "sn": "554614089",
        },
    ],
    "err": None,
    "msg": "showing last 2 operations",
    "status": "ok",
}


def test_log_entry_identifiers_are_tagged():
    """Phone numbers and remote serial numbers never appear in clear."""
    text = str(redact(LOG_PAYLOAD))
    assert "972501234567" not in text
    assert "554614089" not in text
    first, second = redact(LOG_PAYLOAD)["log"]
    assert TAG.fullmatch(first["userId"])
    assert TAG.fullmatch(second["sn"])


def test_log_entry_non_identifying_fields_stay_readable():
    """Operation, time and reason are what makes a log useful: keep them."""
    first = redact(LOG_PAYLOAD)["log"][0]
    assert first["operation"] == "Output 1"
    assert first["time"] == 1795698302
    assert first["reason"] == 0
    assert first["type"] == 100


def test_empty_and_null_values_are_not_tagged():
    """An empty string or None carries nothing to hide, so it stays as is."""
    first = redact(LOG_PAYLOAD)["log"][0]
    assert first["lastname"] == ""
    assert first["sn"] == ""
    assert redact({"id": None, "name1": ""}) == {"id": None, "name1": ""}


def test_same_value_gets_the_same_tag_across_fields():
    """The phone shows up as userId and firstname: one value, one tag."""
    first = redact(LOG_PAYLOAD)["log"][0]
    assert first["userId"] == first["firstname"]


def test_secrets_get_a_plain_marker():
    assert redact({"user": {"id": "972501234567", "token": "abc123"}}) == {
        "user": {"id": redact_tag("972501234567"), "token": "<redacted>"}
    }


def test_output_suffix_stays_visible_outside_the_tag():
    assert redact_tag("GATE1:2").endswith(">:2")
    assert redact_tag("GATE1:2")[:-2] == redact_tag("GATE1")


@pytest.mark.parametrize(
    "path",
    [
        "/device/GATEXYZ789",
        "/device/GATEXYZ789:2",
        "/device/GATEXYZ789/users-v2?skip=0&limit=50",
        "/device/GATEXYZ789/user?pn=972501234567",
        "/device/GATEXYZ789/user",
        "/user/log?id=GATEXYZ789",
        "/un/secondary/init/0f8fad5b-d9cb-469f-a165-70867728950e",
    ],
)
def test_every_url_form_the_client_builds_is_redacted(path):
    """Mirrors the URL builders in api.py and the config flow."""
    text = redact_url("https://api.example.test/api/v1.1" + path)
    for secret in ("GATEXYZ789", "972501234567", "0f8fad5b-d9cb-469f-a165"):
        assert secret not in text
    assert "<redacted:" in text


def test_redacted_wrapper_is_lazy_and_applies_the_function():
    calls = []

    def fn(obj):
        calls.append(obj)
        return "hidden"

    wrapped = Redacted("secret", fn)
    assert calls == []  # nothing happens until the log record is formatted
    assert str(wrapped) == "hidden"
    assert calls == ["secret"]
