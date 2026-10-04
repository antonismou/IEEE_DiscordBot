import asyncio
from unittest.mock import AsyncMock, MagicMock

from bot.main import sync_commands_to


def test_commands_are_copied_to_the_server_and_synced_there():
    tree, guild = MagicMock(), object()
    tree.sync = AsyncMock()
    asyncio.run(sync_commands_to(tree, guild))
    tree.copy_global_to.assert_called_once_with(guild=guild)
    tree.sync.assert_awaited_once_with(guild=guild)
