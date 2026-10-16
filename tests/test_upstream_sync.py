import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_new_member_permission_flow import Plugin, plugin_main
from test_geetest_verification import MemoryDB, flow_plugin


class UpstreamSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_relocated_database_reads_existing_state_file(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'verify_states.db'
            # Existing v1.3.6 table and pending state, created before plugin startup.
            with sqlite3.connect(db_path) as conn:
                conn.execute('''CREATE TABLE verify_states (
                    state_key TEXT PRIMARY KEY, status TEXT NOT NULL,
                    question TEXT, answer INTEGER, wrong_count INTEGER DEFAULT 0,
                    verify_method TEXT, max_wrong_answers INTEGER DEFAULT 5,
                    verify_time REAL, created_at REAL)''')
                conn.execute('''INSERT INTO verify_states
                    (state_key, status, question, answer, wrong_count, verify_method)
                    VALUES ('123:456', 'pending', '1 - 1 = ?', 0, 2, 'math')''')
            db = plugin_main.VerifyStateDB(db_path)
            try:
                await db.init()
                state = db.get_cached('123:456')
                self.assertEqual(state['status'], 'pending')
                self.assertEqual(state['answer'], 0)
                self.assertEqual(state['wrong_count'], 2)
                await db.update_field('123:456', 'status', 'verified')
            finally:
                await db.close()
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute(
                    "SELECT status FROM verify_states WHERE state_key = '123:456'"
                ).fetchone(), ('verified',))

    async def test_timeout_send_failures_still_kick_using_event_bot(self):
        for platform in ('aiocqhttp', 'telegram'):
            with self.subTest(platform=platform):
                plugin, config = flow_plugin()
                config['kick_delay'] = 0
                plugin.kick_delay = 0
                plugin.db = MemoryDB({'123:456': {
                    'status': 'pending', 'verify_method': 'geetest',
                }})
                plugin.timeout_reminder_geetest = '{at_user} {url}'
                plugin.failure_message = '{at_user} {countdown} failed'
                plugin.kick_message = '{at_user} kicked'
                plugin._get_platform = lambda event: platform
                # Exercise real routing and sending methods, including their error handling.
                del plugin._send_group_message
                async def action(name, **kwargs):
                    if name in ('send_group_msg', 'send_message'):
                        raise RuntimeError('send blocked')
                client = SimpleNamespace(call_action=AsyncMock(side_effect=action))
                event = SimpleNamespace(bot=client)
                plugin.context = SimpleNamespace(
                    get_platform=AsyncMock(side_effect=AssertionError('must use event bot'))
                )
                with patch('astrbot_plugin_group_geetest_verify.core.verifier.asyncio.sleep', new=AsyncMock()):
                    await Plugin._timeout_kick(plugin, '456', 123, 300, event)
                calls = client.call_action.await_args_list
                kick_action = 'set_group_kick' if platform == 'aiocqhttp' else 'kickChatMember'
                self.assertEqual([call.args[0] for call in calls].count(kick_action), 1)
                self.assertEqual(calls[2].kwargs['user_id'], 456)
                self.assertFalse(plugin.db.contains('123:456'))
                self.assertNotIn('123:456', plugin._tasks)
                plugin.context.get_platform.assert_not_called()

    async def test_cancelling_timeout_does_not_kick(self):
        plugin, config = flow_plugin()
        plugin._kick_member = AsyncMock()
        with patch('astrbot_plugin_group_geetest_verify.core.verifier.asyncio.sleep', new=AsyncMock(side_effect=asyncio.CancelledError)):
            await Plugin._timeout_kick(plugin, '456', 123, 300, object())
        plugin._kick_member.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
