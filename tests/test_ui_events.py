from __future__ import annotations

import asyncio

from chameleon.ui.events import UiBridge


def test_bridge_ask_and_answer():
    async def scenario():
        bridge = UiBridge()
        queue = bridge.subscribe()

        async def answer_later():
            event = await queue.get()
            assert event["type"] == "ask"
            assert event["question"] == "Which shirt?"
            bridge.submit_answer("Bolt T-Shirt")

        task = asyncio.create_task(answer_later())
        text = await bridge.wait_answer("Which shirt?")
        await task
        assert text == "Bolt T-Shirt"
        bridge.unsubscribe(queue)

    asyncio.run(scenario())


def test_waiting_flags():
    async def scenario():
        bridge = UiBridge()
        assert not bridge.waiting_ask()
        assert not bridge.waiting_hold()
        ask = asyncio.create_task(bridge.wait_answer("Which?"))
        await asyncio.sleep(0)
        assert bridge.waiting_ask()
        bridge.submit_answer("blue")
        assert await ask == "blue"
        assert not bridge.waiting_ask()

        hold = asyncio.create_task(bridge.wait_dismiss())
        await asyncio.sleep(0)
        assert bridge.waiting_hold()
        bridge.dismiss()
        await hold
        assert not bridge.waiting_hold()

    asyncio.run(scenario())
