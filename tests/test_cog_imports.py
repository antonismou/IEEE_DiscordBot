import asyncio
from types import SimpleNamespace


def test_verify_view_builds_with_two_persistent_buttons():
    from bot.cogs.verify import VerifyView

    async def build():
        return VerifyView(SimpleNamespace())

    view = asyncio.run(build())
    ids = {child.custom_id for child in view.children}
    assert ids == {"verify:start", "verify:code"}
    assert view.timeout is None
