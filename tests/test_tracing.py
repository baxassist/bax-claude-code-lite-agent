import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "bax"))

import asyncio  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402
from bax_link.tracing import command_context, response_fields  # noqa: E402

PARENT = "00-11111111111111111111111111111111-2222222222222222-01"
OTHER = "00-33333333333333333333333333333333-4444444444444444-01"


async def test_context_isolated_per_command_and_not_inherited_by_background():
    async def worker(parent):
        with command_context({"traceparent": parent}):
            await asyncio.sleep(0)
            assert response_fields({}) == {"traceparent": parent}

            async def background():
                return response_fields({})

            assert await asyncio.create_task(background()) == {}
            with command_context():
                assert response_fields({}) == {}
            assert response_fields({}) == {"traceparent": parent}
            assert response_fields({"traceparent": OTHER}) == {"traceparent": OTHER}
        assert response_fields({}) == {}

    await asyncio.gather(worker(PARENT), worker(OTHER))
    with pytest.raises(RuntimeError), command_context({"traceparent": PARENT}):
        raise RuntimeError("synthetic")
    assert response_fields({}) == {}


@pytest.mark.parametrize("parent", ["bad", 1, "00-" + "0" * 32 + "-2222222222222222-01", PARENT[:-2] + "FF"])
async def test_invalid_context_rejected_before_handler(parent):
    with pytest.raises(ValueError), command_context({"traceparent": parent}):
        pytest.fail("invalid context entered")


async def test_real_transport_correlates_reply_without_leaking_to_next_command(tmp_path):
    from bax_link.connection import Link
    from websockets.asyncio.server import serve

    received = []
    completed = asyncio.Event()
    connection = None

    async def server(ws):
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "challenge", "nonce": "n", "ts": 1}))
        await ws.recv()
        await ws.send(json.dumps({"v": 1, "type": "ready", "agent": "a", "user": "u"}))
        for fields in ({"traceparent": PARENT}, {}):
            await ws.send(json.dumps({"v": 1, "type": "history", **fields}))
            received.append(json.loads(await ws.recv()))
        completed.set()
        await ws.wait_closed()

    async def ready(*args):
        pass

    async def handle(message):
        from bax_link.tracing import dependency
        with dependency("filesystem", "read"):
            await asyncio.sleep(0)
        await connection.send("history.done")

    async with serve(server, "127.0.0.1", 0, ping_interval=None) as listener:
        url = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/agent"
        connection = Link(
            url, "key", "secret", "0", engine="claude_code_lite", install_id="i", session_id="s"
        )
        task = asyncio.create_task(connection._session(ready, handle))
        try:
            await asyncio.wait_for(completed.wait(), 5)
            assert received[0]["traceparent"] == PARENT
            assert received[0]["dependency_timings"][0]["operation"] == "read"
            assert "dependency_timings" not in received[1]
            assert "traceparent" not in received[1]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_dependency_timings_are_bounded_private_and_owned_by_the_exact_command():
    from bax_link.tracing import dependency

    with command_context({"traceparent": PARENT}):
        with dependency("codex", "thread/read"):
            await asyncio.sleep(0)
        foreign = response_fields({"traceparent": OTHER})
        assert "dependency_timings" not in foreign
        reply = response_fields({})
        timing, = reply["dependency_timings"]
        assert timing["operation"] == "thread/read" and timing["elapsed_ms"] >= 0
        assert set(timing) == {"system", "operation", "elapsed_ms", "started_at_ms", "outcome"}
        assert response_fields({}) == {"traceparent": PARENT}
        async def background():
            with dependency("filesystem", "read"):
                pass
            return response_fields({})
        assert await asyncio.create_task(background()) == {}
        for _ in range(40):
            with dependency("filesystem", "read"):
                pass
        reply = response_fields({})
        assert len(reply["dependency_timings"]) == 32 and reply["dependency_timings_dropped"] == 8


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_dependency_error_and_cancel_do_not_export_error_messages(error):
    from bax_link.tracing import dependency

    with command_context({"traceparent": PARENT}):
        with pytest.raises(error), dependency("filesystem", "read"):
            raise error("secret file contents")
        reply = response_fields({})
        assert reply["dependency_timings"][0]["outcome"] == "error"
        assert "secret" not in repr(reply)
