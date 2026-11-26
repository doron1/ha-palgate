"""Config flow tests.

The Palgate cloud is mocked at the HTTP level (aioclient_mock), so the real
flow code, including the progress task, runs inside Home Assistant's real
flow manager.
"""

import re
from unittest.mock import patch

import pytest
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.const import CONF_DEVICE_ID, CONF_TOKEN
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    MockLongPollSideEffect,
)

from custom_components.palgate.const import (
    CONF_ADVANCED,
    CONF_GATE_NAME,
    CONF_LINK_NEW_DEVICE,
    CONF_PHONE_NUMBER,
    CONF_TOKEN_TYPE,
    DOMAIN,
)
from custom_components.palgate.config_flow import DEVICES_URL, INIT_URL

PHONE = "972501234567"
TOKEN = "00" * 16
LINK_OK = {
    "status": "ok",
    "user": {"id": PHONE, "token": TOKEN},
    "secondary": 1,
}
ADVANCED = {
    "seconds_to_open": 10,
    "seconds_open": 20,
    "seconds_to_close": 10,
    "allow_invert_as_stop": False,
}


@pytest.fixture(autouse=True)
def no_entry_setup():
    """Creating an entry must not start the real integration."""
    with patch("custom_components.palgate.async_setup_entry", return_value=True):
        yield


def _user_input(phone=CONF_LINK_NEW_DEVICE):
    return {CONF_PHONE_NUMBER: phone, CONF_ADVANCED: dict(ADVANCED)}


def _mock_devices(aioclient_mock, devices):
    aioclient_mock.get(DEVICES_URL, json={"devices": devices})


async def _start_link(hass, aioclient_mock, response=None):
    """Start a Link New Device flow, return (flow result, long-poll mock).

    With a response, the vendor has already answered when the flow starts.
    """
    poll = MockLongPollSideEffect()
    if response:
        poll.queue_response(**response)
    aioclient_mock.get(re.compile(re.escape(INIT_URL) + ".*"), side_effect=poll)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input()
    )
    return result, poll


async def _finish_progress(hass):
    """Let the progress task finish and continue the flow like the frontend.

    When the task is done HA re-enters the flow, which returns progress-done
    pointing at select_device. The frontend then calls the flow once more, and
    that call returns the result of the next step. A stuck flow (issue 48)
    would still be showing progress here.
    """
    await hass.async_block_till_done()
    [flow] = hass.config_entries.flow.async_progress()
    assert flow["step_id"] == "select_device"
    return await hass.config_entries.flow.async_configure(flow["flow_id"])


async def test_link_new_device_creates_entry(hass, aioclient_mock):
    """Regression test for issue 48: flow must advance after linking."""
    _mock_devices(aioclient_mock, [{"id": "GATE1", "output1": True, "name1": "Gate"}])

    result, poll = await _start_link(hass, aioclient_mock)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "user"

    # Vendor reports the QR code was scanned
    poll.queue_response(json=LINK_OK)
    result = await _finish_progress(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    [entry] = hass.config_entries.async_entries(DOMAIN)
    assert entry.unique_id == "GATE1"
    assert entry.data[CONF_DEVICE_ID] == "GATE1"
    assert entry.data[CONF_GATE_NAME] == "Gate"
    assert entry.data[CONF_PHONE_NUMBER] == PHONE
    assert entry.data[CONF_TOKEN] == TOKEN
    assert entry.data[CONF_TOKEN_TYPE] == 1
    assert entry.title == f"Gate (via {PHONE})"


async def test_link_new_device_failure_aborts(hass, aioclient_mock, caplog):
    """A failed linking ends the flow without an entry (no endless spinner)."""
    result, poll = await _start_link(hass, aioclient_mock)
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    poll.queue_response(status=400, text='{"status":"failed"}')
    result = await _finish_progress(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_link_device"
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert "Not OK 400" in caplog.text
    assert "can only transition" not in caplog.text


async def test_link_new_device_no_devices(hass, aioclient_mock):
    """Linked phone with no gates left ends the flow without an entry."""
    _mock_devices(aioclient_mock, [])
    result, poll = await _start_link(hass, aioclient_mock)
    poll.queue_response(json=LINK_OK)
    result = await _finish_progress(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_multi_output_controller_offers_each_output(hass, aioclient_mock):
    """A two-output controller yields two gates; output 2 gets a ':2' id."""
    _mock_devices(
        aioclient_mock,
        [
            {
                "id": "GATE1",
                "output1": True,
                "output2": True,
                "name1": "Gate",
                "name2": "Garage",
                "customName2": "Side door",
            }
        ],
    )
    result, poll = await _start_link(hass, aioclient_mock)
    poll.queue_response(json=LINK_OK)
    result = await _finish_progress(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_device"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_ID: "GATE1:2"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_ID] == "GATE1:2"
    assert result["data"][CONF_GATE_NAME] == "Side door"


async def test_existing_phone_skips_linking(hass, aioclient_mock):
    """Choosing an already linked phone goes straight to gate selection,
    and gates that are already configured are not offered again."""
    MockConfigEntry(
        domain=DOMAIN,
        version=3,
        unique_id="GATE1",
        data={
            CONF_DEVICE_ID: "GATE1",
            CONF_PHONE_NUMBER: PHONE,
            CONF_TOKEN: TOKEN,
            CONF_TOKEN_TYPE: 1,
            CONF_ADVANCED: dict(ADVANCED),
        },
    ).add_to_hass(hass)
    _mock_devices(
        aioclient_mock,
        [
            {"id": "GATE1", "output1": True, "name1": "Gate"},
            {"id": "GATE2", "output1": True, "name1": "Garage"},
        ],
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(PHONE)
    )
    # Only GATE2 is left, so there is nothing to choose
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_ID] == "GATE2"


def _add_entry(hass, device_id="GATE1", unique_id="GATE1"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,  # current config flow version; older ones get migrated
        unique_id=unique_id,
        data={
            CONF_DEVICE_ID: device_id,
            CONF_GATE_NAME: "Gate",
            CONF_PHONE_NUMBER: PHONE,
            CONF_TOKEN: TOKEN,
            CONF_TOKEN_TYPE: 1,
            CONF_ADVANCED: dict(ADVANCED),
        },
    )
    entry.add_to_hass(hass)
    return entry


async def _start_reconfigure(hass, entry):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )


async def test_reconfigure_updates_advanced_options(hass):
    """Reconfigure shows the stored values and saves changed ones."""
    entry = _add_entry(hass)

    result = await _start_reconfigure(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    new_advanced = {**ADVANCED, "seconds_open": 99, "allow_invert_as_stop": True}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: "GATE1", CONF_ADVANCED: new_advanced},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_ADVANCED] == new_advanced
    # Everything else stays as it was
    assert entry.data[CONF_TOKEN] == TOKEN
    assert entry.data[CONF_PHONE_NUMBER] == PHONE


async def test_reconfigure_cannot_change_device_id(hass):
    """Changing the device id is rejected, and nothing is saved."""
    entry = _add_entry(hass)

    result = await _start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DEVICE_ID: "OTHER", CONF_ADVANCED: dict(ADVANCED)},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cant_reconfigure_device_id"}
    assert entry.data[CONF_DEVICE_ID] == "GATE1"


async def test_duplicate_unique_id_aborts_with_translated_reason(hass, aioclient_mock):
    """The abort reason must exist in the translations.

    The gate list is already filtered by the stored device id, so this abort
    is a second line of defence. To reach it, the existing entry has a unique
    id of GATE1 but a different stored device id, so the filter lets GATE1 by.
    """
    _add_entry(hass, device_id="GATE1:9", unique_id="GATE1")
    _mock_devices(aioclient_mock, [{"id": "GATE1", "output1": True, "name1": "Gate"}])

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(PHONE)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured_device"
    assert result["reason"] in _translated_abort_reasons()


def _translated_abort_reasons():
    import json
    from pathlib import Path

    path = Path(__file__).parents[1] / "custom_components/palgate/translations/en.json"
    return json.loads(path.read_text(encoding="utf-8"))["config"]["abort"]


@pytest.mark.parametrize(
    "response",
    [
        {"status": 401, "text": "Unauthorized"},
        {"status": 500, "text": "Server error"},
        {"text": "this is not json"},
        {"json": {"status": "ok"}},
    ],
    ids=["unauthorized", "server_error", "invalid_json", "missing_fields"],
)
async def test_link_new_device_errors_abort(hass, aioclient_mock, response):
    """Whatever the vendor answers to the linking poll, a bad answer aborts."""
    result, poll = await _start_link(hass, aioclient_mock)
    poll.queue_response(**response)
    result = await _finish_progress(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_link_device"
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    "response",
    [
        {"status": 401, "text": "Unauthorized"},
        {"status": 500, "text": "Server error"},
        {"text": "this is not json"},
        {"exc": ClientError()},
    ],
    ids=["unauthorized", "server_error", "invalid_json", "connection_error"],
)
async def test_fetch_devices_errors_abort(hass, aioclient_mock, response):
    """A failing gate list call aborts with cannot_fetch_devices."""
    _add_entry(hass)
    aioclient_mock.get(DEVICES_URL, **response)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(PHONE)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_fetch_devices"


async def test_select_device_rejects_unknown_choice(hass, aioclient_mock):
    """A gate that was not offered is rejected before the flow looks it up."""
    _add_entry(hass, device_id="OTHER", unique_id="OTHER")
    _mock_devices(
        aioclient_mock,
        [
            {"id": "GATE1", "output1": True, "name1": "Gate"},
            {"id": "GATE2", "output1": True, "name1": "Garage"},
        ],
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_input(PHONE)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_device"

    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE_ID: "NOT_OFFERED"}
        )

    # The flow is still usable after the rejected choice
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_ID: "GATE2"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_ID] == "GATE2"


@pytest.mark.parametrize(
    ("response", "created"),
    [
        ({"json": LINK_OK}, True),
        ({"status": 401, "text": "Unauthorized"}, False),
    ],
    ids=["success", "failure"],
)
async def test_link_new_device_vendor_answers_at_once(
    hass, aioclient_mock, response, created
):
    """The vendor's answer is already there when the spinner is first shown."""
    _mock_devices(aioclient_mock, [{"id": "GATE1", "output1": True, "name1": "Gate"}])

    result, _ = await _start_link(hass, aioclient_mock, response)
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    result = await _finish_progress(hass)
    if created:
        assert result["type"] is FlowResultType.CREATE_ENTRY
    else:
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "cannot_link_device"
    assert bool(hass.config_entries.async_entries(DOMAIN)) is created
