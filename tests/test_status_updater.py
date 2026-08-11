import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from src.turn import StatusUpdater

@pytest.mark.asyncio
async def test_status_updater_throttling():
    tg = AsyncMock()
    updater = StatusUpdater(tg, 123, 456)
    
    # First update should happen immediately
    await updater.update("Status 1")
    assert tg.edit_message_text.call_count == 1
    call_args = tg.edit_message_text.call_args[0]
    assert call_args[0] == 123
    assert call_args[1] == 456
    assert "Status 1" in call_args[2]
    
    # Second update immediately after should be delayed
    await updater.update("Status 2")
    assert tg.edit_message_text.call_count == 1
    
    # Wait for delay to pass
    await asyncio.sleep(2.5)
    
    # Now the delayed update should have fired
    assert tg.edit_message_text.call_count == 2
    call_args = tg.edit_message_text.call_args[0]
    assert "Status 2" in call_args[2]

    await updater.close()
