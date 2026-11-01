import asyncio
import random
import logging
import time
import uuid
from weakref import WeakValueDictionary
from typing import Tuple
from .database.db import _ROUND_ID_UNSET
from .recall import RecallMixin

logger = logging.getLogger(__name__)


class VerifyMixin(RecallMixin):
    """验证流程相关方法：数学题生成、验证流程启动、超时踢出、等级查询、权限检查"""

    KICK_RETRY_DELAYS = (5, 15, 30)
    MAX_KICK_ATTEMPTS = 1 + len(KICK_RETRY_DELAYS)
    OFFLINE_RETRY_DELAY = 30

    def _generate_math_problem(self) -> Tuple[str, int]:
        """生成一个100以内的加减法问题"""
        op_type = random.choice(['add', 'sub'])
        if op_type == 'add':
            num1 = random.randint(0, 100)
            num2 = random.randint(0, 100 - num1)
            answer = num1 + num2
            question = f"{num1} + {num2} = ?"
            return question, answer
        else:
            num1 = random.randint(1, 100)
            num2 = random.randint(0, num1)
            answer = num1 - num2
            question = f"{num1} - {num2} = ?"
            return question, answer

    @staticmethod
    def _state_parts(state_key: str):
        try:
            gid, uid = state_key.split(":", 1)
            return int(gid), str(uid)
        except (TypeError, ValueError):
            return None, None

    @staticmethod
    def _new_round_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _has_recovery_identity(state: dict) -> bool:
        return bool(
            state
            and state.get("verification_deadline") is not None
            and str(state.get("platform_type") or "").strip()
            and str(state.get("platform_instance_id") or "").strip()
            and str(state.get("bot_identity") or "").strip()
            and str(state.get("round_id") or "").strip()
        )

    def _state_matches_round(
        self,
        state_key: str,
        round_id: object = _ROUND_ID_UNSET,
    ) -> bool:
        state = self.db.get_cached(state_key)
        if not state or state.get("status") != "pending":
            return False
        if round_id is not _ROUND_ID_UNSET and state.get("round_id") != round_id:
            return False
        return True

    async def _set_terminal_state(
        self,
        state_key: str,
        status: str,
        *,
        expected_round_id: object = _ROUND_ID_UNSET,
    ) -> bool:
        state = dict(self.db.get_cached(state_key) or {})
        state["status"] = status
        state["verify_time"] = time.time()
        kwargs = {}
        if expected_round_id is not _ROUND_ID_UNSET:
            kwargs["expected_round_id"] = expected_round_id
        return await self.db.set(state_key, state, **kwargs)

    def _event_matches_saved_identity(self, event, state: dict) -> bool:
        if event is None:
            return False
        expected_platform = str(state.get("platform_type") or "").strip()
        expected_instance = str(state.get("platform_instance_id") or "").strip()
        expected_bot = str(state.get("bot_identity") or "").strip()
        if not expected_platform or not expected_instance or not expected_bot:
            return False
        try:
            return (
                self._get_platform(event) == expected_platform
                and self._get_event_platform_id(event) == expected_instance
                and self._get_event_self_id(event) == expected_bot
            )
        except Exception:
            return False

    def _event_for_state(self, event, state: dict, gid: int, uid: str):
        if not self._has_recovery_identity(state):
            # A migrated legacy row has no trustworthy deadline, platform
            # instance, bot identity, or round.  Never let an in-memory task
            # continue with its old event-bound client.
            return None
        recovered = self._resolve_persisted_event(state, gid, uid)
        if recovered is not None:
            return recovered
        return None

    async def _recover_pending_verifications(self, *, startup: bool = False):
        """Recreate timers only for complete, identity-bound pending records."""
        legacy_by_group = {}
        for state_key in self.db.all_keys():
            state = self.db.get_cached(state_key) or {}
            if state.get("status") != "pending":
                continue
            gid, uid = self._state_parts(state_key)
            if gid is None:
                continue

            if not self._has_recovery_identity(state):
                legacy_by_group[gid] = legacy_by_group.get(gid, 0) + 1
                continue

            current_task = self._tasks.get(state_key)
            if current_task and not current_task.done():
                continue

            recovered_event = self._resolve_persisted_event(state, gid, uid)
            if recovered_event is None:
                continue

            round_id = str(state["round_id"])
            task = asyncio.create_task(
                self._timeout_kick(
                    uid,
                    gid,
                    event=recovered_event,
                    round_id=round_id,
                )
            )
            self._tasks[state_key] = task
            logger.info(
                "[Geetest Verify] 已恢复群 %s 用户 %s 的验证轮次 %s，截止时间=%s",
                gid,
                uid,
                round_id,
                state.get("verification_deadline"),
            )

        if startup and legacy_by_group:
            summary = ", ".join(
                f"群 {gid}: {count} 人" for gid, count in sorted(legacy_by_group.items())
            )
            logger.warning(
                "[Geetest Verify] 发现缺少截止时间或机器人身份的历史待验证记录：%s；"
                "仅保留记录，不自动踢人，请管理员使用重新验证命令",
                summary,
            )

    async def _verification_recovery_loop(self):
        while True:
            await asyncio.sleep(self.OFFLINE_RETRY_DELAY)
            try:
                await self._recover_pending_verifications()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "[Geetest Verify] 定期恢复验证任务失败（%s）",
                    type(exc).__name__,
                    exc_info=True,
                )

    async def _start_verification_process(
        self,
        event,
        uid: str,
        gid: int,
        question: str = None,
        answer: int = None,
        is_new_member: bool = True,
        group_config: dict = None,
        expected_round_id: object = _ROUND_ID_UNSET,
    ):
        """为用户启动或重启验证流程，并先持久化本轮的完整身份和截止时间。"""
        state_key = f"{gid}:{uid}"

        if group_config is None:
            group_config = self._get_group_config(gid)

        existing = dict(self.db.get_cached(state_key) or {})
        now = time.time()

        # A new member or an explicit administrator re-verification starts a
        # new round.  A wrong answer keeps the original deadline, route and
        # round ID so retries can never extend the verification window.
        if is_new_member:
            state = {
                "status": "pending",
                "question": question,
                "answer": answer,
                "wrong_count": 0,
                "verify_method": None,
                "max_wrong_answers": group_config["max_wrong_answers"],
                "verification_deadline": now + max(0, float(group_config["verification_timeout"])),
                "platform_type": str(self._get_platform(event) or "").strip(),
                "platform_instance_id": self._get_event_platform_id(event),
                "bot_identity": self._get_event_self_id(event),
                "round_id": self._new_round_id(),
                "kick_attempts": 0,
                "next_retry_at": None,
            }
            if hasattr(self, "_kick_exhaustion_notified"):
                self._kick_exhaustion_notified.discard(state_key)
        else:
            state = existing
            state.update({
                "status": "pending",
                "question": question,
                "answer": answer,
                "wrong_count": existing.get("wrong_count", 0),
                "max_wrong_answers": group_config["max_wrong_answers"],
            })
            # Do not synthesize missing legacy deadline/identity fields here.
            # Such records require an explicit administrator re-verification.
            if not state.get("round_id"):
                logger.warning(
                    "[Geetest Verify] 群 %s 用户 %s 的历史验证记录缺少轮次身份，"
                    "本次错答不创建新的截止时间",
                    gid,
                    uid,
                )

        # Decide the method before the first durable write.  The timer is only
        # created after this complete state is committed successfully.
        use_geetest = bool(group_config.get("enable_geetest_verify")) and bool(self.api_key)
        verify_method = "geetest" if use_geetest else "math"
        state["verify_method"] = verify_method
        set_kwargs = {}
        if expected_round_id is not _ROUND_ID_UNSET:
            # An explicit None means this request belongs to a legacy row
            # whose round_id is SQL NULL.  It is a real CAS value, not the
            # absence of a CAS check.
            set_kwargs["expected_round_id"] = expected_round_id
        elif not is_new_member and "round_id" in existing:
            set_kwargs["expected_round_id"] = existing.get("round_id")

        persisted = await self.db.set(state_key, state, **set_kwargs)
        if not persisted:
            logger.info(
                "[Geetest Verify] 群 %s 用户 %s 的验证轮次已变化，放弃旧启动请求",
                gid,
                uid,
            )
            return False

        # Do not cancel the previous timer until the replacement state has
        # committed.  If the write fails, the previous durable state and its
        # timer remain responsible for the user.
        old_task = self._tasks.get(state_key)
        if old_task and not old_task.done():
            old_task.cancel()

        message_round = self._verification_message_round(
            event,
            gid,
            uid,
            group_config,
            any_bot=True,
        )

        round_id = state.get("round_id")
        if state.get("verification_deadline") is not None and round_id:
            task = asyncio.create_task(
                self._timeout_kick(uid, gid, event=event, round_id=round_id)
            )
            self._tasks[state_key] = task
        else:
            logger.warning(
                "[Geetest Verify] 群 %s 用户 %s 的验证记录缺少截止时间或轮次，"
                "不启动自动踢人任务，请管理员重新验证",
                gid,
                uid,
            )

        at_user = self._format_user_mention(event, uid)
        timeout_minutes = group_config["verification_timeout"] // 60

        if use_geetest:
            try:
                verify_url = await self._create_geetest_verify(gid, uid)
                if message_round is not None and message_round.closed:
                    return False
                if verify_url:
                    if not self._state_matches_round(state_key, round_id):
                        return False
                    if not await self.db.update_field(
                        state_key,
                        "verify_method",
                        "geetest",
                        expected_round_id=round_id,
                    ):
                        return False
                    if is_new_member:
                        prompt_message = self.geetest_new_member_prompt.format(at_user=at_user, timeout=timeout_minutes, url=verify_url)
                    else:
                        current_state = self.db.get_cached(state_key) or {}
                        wrong_count = current_state.get("wrong_count", 0)
                        remaining_attempts = group_config["max_wrong_answers"] - wrong_count
                        prompt_message = self.geetest_wrong_code_prompt.format(at_user=at_user, url=verify_url, remaining=remaining_attempts)
                    await self._send_verification_message(event, gid, prompt_message, message_round)
                    logger.info(f"[Geetest Verify] 用户 {uid} 在群 {gid} 已发送极验验证链接")
                    return True
            except Exception as e:
                logger.warning(f"[Geetest Verify] 调用极验 API 失败: {e}，回退到算术验证")

        # 数学题模式：若未提供题目则自动生成
        if message_round is not None and message_round.closed:
            return False
        if not question or answer is None:
            question, answer = self._generate_math_problem()
            if not await self.db.update_field(
                state_key,
                "question",
                question,
                expected_round_id=round_id,
            ):
                return False
            if not await self.db.update_field(
                state_key,
                "answer",
                answer,
                expected_round_id=round_id,
            ):
                return False

        if not await self.db.update_field(
            state_key,
            "verify_method",
            "math",
            expected_round_id=round_id,
        ):
            return False
        if not self._state_matches_round(state_key, round_id):
            return False
        logger.info(f"[Geetest Verify] 用户 {uid} 在群 {gid} 使用数学题验证，问题: {question} (答案: {answer})")

        if is_new_member:
            prompt_message = self.new_member_prompt.format(at_user=at_user, timeout=timeout_minutes, question=question)
        else:
            current_state = self.db.get_cached(state_key) or {}
            wrong_count = current_state.get("wrong_count", 0)
            remaining_attempts = group_config["max_wrong_answers"] - wrong_count
            prompt_message = self.wrong_answer_prompt.format(at_user=at_user, question=question, remaining=remaining_attempts)

        await self._send_verification_message(event, gid, prompt_message, message_round)
        return True

    async def _complete_kick(
        self,
        event,
        gid: int,
        uid: str,
        round_id: str,
        message_round,
        success_message: str = None,
    ) -> bool:
        state_key = f"{gid}:{uid}"
        if not self._state_matches_round(state_key, round_id):
            return False

        if not await self.db.delete(state_key, expected_round_id=round_id):
            return False
        task = self._tasks.get(state_key)
        if task is not None and task is not asyncio.current_task():
            if not task.done():
                task.cancel()
            self._tasks.pop(state_key, None)
        elif task is asyncio.current_task() or task is None:
            self._tasks.pop(state_key, None)

        if hasattr(self, "_kick_exhaustion_notified"):
            self._kick_exhaustion_notified.discard((state_key, round_id))
        await self._finish_verification_messages(message_round)

        if event is not None:
            try:
                platform = self._get_platform(event)
                at_user = (
                    f"[CQ:at,qq={uid}]"
                    if platform == "aiocqhttp"
                    else f"[用户](tg://user?id={uid})"
                )
                message = success_message or self.kick_message
                await self._send_group_message(event, gid, message.format(at_user=at_user))
            except Exception as exc:
                logger.warning(
                    "[Geetest Verify] 发送踢出通知消息失败 (用户 %s)：%s",
                    uid,
                    type(exc).__name__,
                )
        return True

    async def _notify_kick_exhausted(self, event, gid: int, uid: str, round_id: str):
        state_key = f"{gid}:{uid}"
        if not hasattr(self, "_kick_exhaustion_notified"):
            self._kick_exhaustion_notified = set()
        notice_key = (state_key, round_id)
        if notice_key in self._kick_exhaustion_notified:
            return
        self._kick_exhaustion_notified.add(notice_key)
        logger.error(
            "[Geetest Verify] 群 %s 用户 %s 踢出重试耗尽，保留待验证记录，请管理员重新验证",
            gid,
            uid,
        )
        if event is not None:
            try:
                await self._send_group_message(
                    event,
                    gid,
                    f"用户 {uid} 未完成验证，但自动移出群失败；已尝试 {self.MAX_KICK_ATTEMPTS} 次，"
                    "请管理员检查机器人权限或使用“重新验证”处理。",
                )
            except Exception as exc:
                logger.warning(
                    "[Geetest Verify] 发送踢出失败管理员提示失败 (用户 %s)：%s",
                    uid,
                    type(exc).__name__,
                )

    def _kick_response_succeeded(self, response) -> bool:
        """Accept only an explicit kick acknowledgement.

        A ``None`` return is a valid no-exception result for some SDKs, but it
        does not prove that the member was removed.  The caller therefore
        relies on the post-action presence check for that case.
        """
        if response is True:
            return True
        if not isinstance(response, dict):
            return False
        if self._response_failed(response):
            return False
        status = str(response.get("status") or "").strip().lower()
        return (
            response.get("ok") is True
            or response.get("retcode") == 0
            or status in {"ok", "success", "succeeded"}
        )

    async def _kick_with_retries(
        self,
        event,
        gid: int,
        uid: str,
        round_id: str,
        message_round,
        success_message: str = None,
    ) -> str:
        """Serialize live handlers and recovered timers for the same round."""
        if not hasattr(self, "_kick_locks"):
            self._kick_locks = WeakValueDictionary()
        lock_key = (f"{gid}:{uid}", round_id)
        # Each holder/waiter keeps a strong reference until it exits; idle
        # locks disappear without removing a lock that still has waiters.
        lock = self._kick_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._kick_round_with_retries(
                event, gid, uid, round_id, message_round, success_message,
            )

    async def _kick_round_with_retries(
        self,
        event,
        gid: int,
        uid: str,
        round_id: str,
        message_round,
        success_message: str = None,
    ) -> str:
        """Kick only the current round, retaining state until confirmed."""
        state_key = f"{gid}:{uid}"
        while True:
            state = self.db.get_cached(state_key) or {}
            if state.get("status") != "pending" or state.get("round_id") != round_id:
                return "stale"
            if not self._has_recovery_identity(state):
                logger.warning(
                    "[Geetest Verify] 群 %s 用户 %s 的验证记录缺少恢复身份或截止时间，"
                    "跳过自动踢人，请管理员重新验证",
                    gid,
                    uid,
                )
                return "legacy"

            attempts = int(state.get("kick_attempts") or 0)
            if attempts >= self.MAX_KICK_ATTEMPTS:
                await self._notify_kick_exhausted(event, gid, uid, round_id)
                return "exhausted"

            next_retry_at = state.get("next_retry_at")
            if next_retry_at is not None:
                try:
                    wait_seconds = float(next_retry_at) - time.time()
                except (TypeError, ValueError):
                    wait_seconds = 0
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                    continue

            current_event = self._event_for_state(event, state, gid, uid)
            if current_event is None:
                # No exact platform instance is available.  Keep the record
                # pending and retry the lookup later without consuming a kick
                # attempt or switching to another bot.
                retry_at = time.time() + self.OFFLINE_RETRY_DELAY
                if not await self.db.update_fields(
                    state_key,
                    {"next_retry_at": retry_at},
                    expected_round_id=round_id,
                ):
                    return "stale"
                await asyncio.sleep(self.OFFLINE_RETRY_DELAY)
                continue

            presence = await self._get_member_presence(current_event, gid, uid)
            if not self._state_matches_round(state_key, round_id):
                return "stale"

            if message_round is not None and message_round.closed:
                return "stale"
            if presence == "absent":
                completed = await self._complete_kick(
                    current_event,
                    gid,
                    uid,
                    round_id,
                    message_round,
                    success_message,
                )
                return "removed" if completed else "stale"

            attempt_number = attempts + 1
            # Persist before the external mutation so a process interruption
            # cannot make an unknown operation look like a fresh first try.
            updated = await self.db.update_fields(
                state_key,
                {"kick_attempts": attempt_number, "next_retry_at": None},
                expected_round_id=round_id,
            )
            if not updated or not self._state_matches_round(state_key, round_id):
                return "stale"
            if message_round is not None and message_round.closed:
                return "stale"

            try:
                action_succeeded = False
                try:
                    response = await self._kick_verifying_member(
                        current_event,
                        gid,
                        uid,
                        message_round,
                    )
                    action_succeeded = self._kick_response_succeeded(response)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "[Geetest Verify] 群 %s 用户 %s 第 %s 次踢出失败（%s）",
                        gid,
                        uid,
                        attempt_number,
                        type(exc).__name__,
                    )

                if not self._state_matches_round(state_key, round_id):
                    return "stale"

                if message_round is not None and message_round.closed:
                    return "stale"

                post_presence = await self._get_member_presence(current_event, gid, uid)
                if post_presence == "absent" or action_succeeded:
                    completed = await self._complete_kick(
                        current_event,
                        gid,
                        uid,
                        round_id,
                        message_round,
                        success_message,
                    )
                    return "removed" if completed else "stale"

            finally:
                # Suppress synchronous leave notices only while the kick and
                # its confirmation run, never through backoff or exhaustion.
                if message_round is not None:
                    message_round.kicking = False

            if attempt_number >= self.MAX_KICK_ATTEMPTS:
                await self.db.update_fields(
                    state_key,
                    {"next_retry_at": None},
                    expected_round_id=round_id,
                )
                await self._notify_kick_exhausted(
                    current_event,
                    gid,
                    uid,
                    round_id,
                )
                return "exhausted"

            delay = self.KICK_RETRY_DELAYS[attempt_number - 1]
            if not await self.db.update_fields(
                state_key,
                {"next_retry_at": time.time() + delay},
                expected_round_id=round_id,
            ):
                return "stale"
            logger.warning(
                "[Geetest Verify] 群 %s 用户 %s 踢出未确认，将在 %s 秒后进行第 %s 次重试",
                gid,
                uid,
                delay,
                attempt_number + 1,
            )
            await asyncio.sleep(delay)

    async def _timeout_kick(
        self,
        uid: str,
        gid: int,
        timeout: int = None,
        event=None,
        round_id: str = None,
    ):
        """Wait for the persisted deadline, then run the durable kick flow."""
        state_key = f"{gid}:{uid}"
        group_config = self._get_group_config(gid)
        current_task = asyncio.current_task()

        try:
            state = self.db.get_cached(state_key) or {}
            if not self._has_recovery_identity(state):
                logger.warning(
                    "[Geetest Verify] 群 %s 用户 %s 的历史验证记录缺少截止时间、"
                    "平台实例、机器人身份或轮次，跳过自动处置，请管理员重新验证",
                    gid,
                    uid,
                )
                return

            persisted_round_id = str(state["round_id"])
            if round_id is not None and str(round_id) != persisted_round_id:
                return
            round_id = persisted_round_id
            event = self._resolve_persisted_event(state, gid, uid)
            if event is None:
                logger.warning(
                    "[Geetest Verify] 缺少群 %s 用户 %s 的原平台实例，保留验证记录",
                    gid,
                    uid,
                )
                return

            deadline = state.get("verification_deadline")
            if deadline is None:
                logger.warning(
                    "[Geetest Verify] 群 %s 用户 %s 缺少验证截止时间，跳过自动处置",
                    gid,
                    uid,
                )
                return

            message_round = self._verification_message_round(
                event,
                gid,
                uid,
                group_config,
                any_bot=True,
            )

            attempts = int(state.get("kick_attempts") or 0)
            if attempts == 0 and state.get("next_retry_at") is None:
                remaining = float(deadline) - time.time()
                if remaining > 60:
                    await asyncio.sleep(remaining - 60)
                    if not self._state_matches_round(state_key, round_id):
                        return

                state = self.db.get_cached(state_key) or {}
                if not self._state_matches_round(state_key, round_id):
                    return
                remaining = float(deadline) - time.time()
                platform = self._get_platform(event)
                at_user = (
                    f"[CQ:at,qq={uid}]"
                    if platform == "aiocqhttp"
                    else f"[用户](tg://user?id={uid})"
                )

                if remaining > 0:
                    verify_method = state.get("verify_method", "geetest")
                    if verify_method == "geetest":
                        verify_url = await self._create_geetest_verify(gid, uid)
                        if verify_url:
                            reminder_msg = self.timeout_reminder_geetest.format(
                                at_user=at_user,
                                url=verify_url,
                            )
                        else:
                            question, answer = self._generate_math_problem()
                            reminder_msg = self.timeout_reminder_math.format(
                                at_user=at_user,
                                question=question,
                            )
                            if self._state_matches_round(state_key, round_id):
                                await self.db.update_fields(
                                    state_key,
                                    {
                                        "verify_method": "math",
                                        "question": question,
                                        "answer": answer,
                                    },
                                    expected_round_id=round_id,
                                )
                    else:
                        question = state.get("question", "")
                        if not question:
                            question, answer = self._generate_math_problem()
                            await self.db.update_fields(
                                state_key,
                                {"question": question, "answer": answer},
                                expected_round_id=round_id,
                            )
                        reminder_msg = self.timeout_reminder_math.format(
                            at_user=at_user,
                            question=question,
                        )
                    await self._send_verification_message(
                        event,
                        gid,
                        reminder_msg,
                        message_round,
                    )

                remaining = max(0, float(deadline) - time.time())
                if remaining:
                    await asyncio.sleep(remaining)
                if not self._state_matches_round(state_key, round_id):
                    return

                kick_delay = group_config.get("kick_delay", self.kick_delay)
                failure_msg = self.failure_message.format(
                    at_user=at_user,
                    countdown=kick_delay,
                )
                await self._send_verification_message(
                    event,
                    gid,
                    failure_msg,
                    message_round,
                )
                if kick_delay:
                    await asyncio.sleep(kick_delay)
                if not self._state_matches_round(state_key, round_id):
                    return

            await self._kick_with_retries(
                event,
                gid,
                uid,
                round_id,
                message_round,
            )
        except asyncio.CancelledError:
            logger.info("[Geetest Verify] 踢出任务已取消 (用户 %s)", uid)
        except Exception as exc:
            logger.error(
                "[Geetest Verify] 踢出流程发生错误 (用户 %s)：%s",
                uid,
                type(exc).__name__,
                exc_info=True,
            )
        finally:
            if getattr(self, "_tasks", {}).get(state_key) is current_task:
                self._tasks.pop(state_key, None)

    async def _get_user_level(self, event, uid: str) -> int:
        """获取用户QQ等级"""
        try:
            user_info = await self._call_event_action(
                event, "get_stranger_info", user_id=int(uid)
            )
            logger.info(f"[Geetest Verify] 用户 {uid} 的API返回数据: {user_info}")

            qq_level = 0
            for key in user_info.keys():
                if key.lower() == "qqlevel":
                    qq_level = user_info[key]
                    break

            if qq_level == 0 and isinstance(user_info.get("data"), dict):
                for key in user_info["data"].keys():
                    if key.lower() == "qqlevel":
                        qq_level = user_info["data"][key]
                        break

            logger.info(f"[Geetest Verify] 用户 {uid} 的QQ等级为: {qq_level}")
            return qq_level
        except Exception as e:
            logger.error(f"[Geetest Verify] 获取用户 {uid} 的QQ等级失败: {e}")
            return 0

    async def _check_permission(self, event) -> bool:
        """检查用户权限（bot管理员、群主、管理员才可使用）"""
        platform = self._get_platform(event)
        raw_message = event.message_obj.raw_message

        if event.is_admin():
            logger.debug("用户为Bot管理员，跳过权限检查")
            return True

        if platform == "telegram":
            from_user = self._get_raw_value(raw_message, "from") or {}
            gid = self._get_group_id(platform, raw_message)
            if gid is None:
                logger.warning("[Geetest Verify] 无法获取群组 ID，权限检查失败")
                return False

            try:
                platform_client = self._get_event_client(event)
                chat_member = None
                if hasattr(platform_client, "call_action"):
                    chat_member = await platform_client.call_action("getChatMember", chat_id=gid, user_id=self._get_raw_value(from_user, "id"))
                else:
                    chat_member = await platform_client.get_chat_member(chat_id=gid, user_id=self._get_raw_value(from_user, "id"))

                status = self._get_raw_value(chat_member, "status", "member")
                if status in ["administrator", "creator"]:
                    logger.debug(f"用户为{status}，跳过权限检查")
                    return True
            except Exception as e:
                logger.warning(f"[Geetest Verify] 获取 Telegram 用户权限失败: {e}")
        else:
            sender_dict = self._get_raw_dict(raw_message) if raw_message else {}
            sender = sender_dict.get("sender", {}) if sender_dict else {}
            sender_role = sender.get("role", "member")
            if sender_role in ["admin", "owner"]:
                logger.debug(f"用户为{sender_role}，跳过权限检查")
                return True

        return False
