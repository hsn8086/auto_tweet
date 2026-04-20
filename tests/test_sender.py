import unittest
from typing import Any, cast

from src import sender


class FakeLocator:
    def __init__(self, *, visible: bool = True, enabled: bool = True) -> None:
        self.visible = visible
        self.enabled = enabled
        self.clicked = False

    @property
    def first(self) -> "FakeLocator":
        return self

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled

    async def click(self) -> None:
        self.clicked = True


class SenderHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_e_returns_messageful_timeout(self) -> None:
        blocked = FakeLocator(enabled=False)

        with self.assertRaisesRegex(TimeoutError, "post button"):
            await sender.wait_e(
                cast(Any, blocked), timeout=1, description="post button"
            )

    async def test_click_first_available_uses_first_clickable_locator(self) -> None:
        blocked = FakeLocator(visible=False)
        ready = FakeLocator()

        result = await sender.click_first_available(
            cast(Any, [blocked, ready]), timeout=1
        )

        self.assertIs(result, ready)
        self.assertFalse(blocked.clicked)
        self.assertTrue(ready.clicked)

    async def test_click_if_available_returns_false_when_none_clickable(self) -> None:
        blocked = FakeLocator(visible=False)

        result = await sender.click_if_available(cast(Any, [blocked]), timeout=1)

        self.assertFalse(result)
        self.assertFalse(blocked.clicked)

    def test_describe_send_exception_falls_back_to_exception_type(self) -> None:
        detail = sender.describe_send_exception(TimeoutError())

        self.assertEqual(detail, "TimeoutError")


if __name__ == "__main__":
    unittest.main()
