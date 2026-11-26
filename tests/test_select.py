"""Select entity tests."""

import logging
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import HomeAssistantError

from custom_components.palgate.select import PalgateRelayModeSelect


async def test_relay_mode_error_log_does_not_leak_the_device_id(caplog):
    """The error log names the gate by tag, never by its controller id."""
    api = MagicMock()
    api.get_relay_mode = AsyncMock(side_effect=HomeAssistantError("boom"))
    entity = PalgateRelayModeSelect(api, "GATEXYZ789:2")

    with caplog.at_level(logging.ERROR, logger="custom_components.palgate"):
        await entity.async_update()

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Failed to fetch relay mode" in logged
    assert "GATEXYZ789" not in logged
    assert "<redacted:" in logged
    assert logged.count(":2") == 1  # the output number stays visible
    assert entity.available is False
