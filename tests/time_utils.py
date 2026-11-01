import asyncio
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch


@contextmanager
def advancing_clock():
    """Advance Unix time with each sleep while still yielding to other tasks."""
    clock = SimpleNamespace(now=time.time(), sleeps=[])
    real_sleep = asyncio.sleep

    async def sleep(delay):
        clock.sleeps.append(float(delay))
        clock.now += max(0, float(delay))
        await real_sleep(0)

    with patch('astrbot_plugin_group_geetest_verify.core.verifier.time.time', side_effect=lambda: clock.now), patch('astrbot_plugin_group_geetest_verify.core.verifier.asyncio.sleep', side_effect=sleep):
        yield clock
