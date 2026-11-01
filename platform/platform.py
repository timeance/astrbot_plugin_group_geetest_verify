import os
import logging
from types import SimpleNamespace
from ..core.api import GeetestAPIMixin
from ..core.permissions import query_current_bot_group_role, role_allows_verification

logger = logging.getLogger(__name__)


class PlatformMixin(GeetestAPIMixin):
    """平台抽象层：平台检测、原始消息解析、消息发送、踢人、图片路径解析"""

    def _get_platform(self, event) -> str:
        """获取事件所属的平台"""
        return event.get_platform_name()

    def _get_raw_value(self, raw, key: str, default=None):
        """安全地从 raw_message 中获取值，兼容字典和 Telegram Update 对象"""
        if isinstance(raw, dict):
            return raw.get(key, default)
        else:
            return getattr(raw, key, default)

    def _get_raw_dict(self, raw) -> dict:
        """将 raw_message 转换为字典格式"""
        if isinstance(raw, dict):
            return raw
        else:
            try:
                return vars(raw)
            except Exception:
                return {}

    def _get_event_self_id(self, event) -> str:
        """获取当前事件绑定的机器人 QQ（OneBot ``self_id``）。"""
        get_platform_name = getattr(event, "get_platform_name", None)
        platform_name = get_platform_name() if callable(get_platform_name) else ""

        # AstrBot's Telegram event exposes bot.username through get_self_id,
        # while python-telegram-bot's ExtBot also provides the stable numeric
        # account id. Prefer the numeric id for restart-safe persistence.
        if platform_name == "telegram":
            client = getattr(event, "bot", None)
            client_id = getattr(client, "id", None)
            if client_id not in (None, ""):
                return str(client_id).strip()

        get_self_id = getattr(event, "get_self_id", None)
        try:
            self_id = get_self_id() if callable(get_self_id) else None
        except Exception:
            self_id = None
        if self_id in (None, ""):
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            self_id = self._get_raw_value(raw, "self_id", None)
        return "" if self_id in (None, "") else str(self_id).strip()

    @staticmethod
    def _get_event_platform_id(event) -> str:
        """获取 AstrBot 平台实例 ID，而不是平台类型。"""
        get_platform_id = getattr(event, "get_platform_id", None)
        try:
            platform_id = get_platform_id() if callable(get_platform_id) else None
        except Exception:
            platform_id = None
        if platform_id in (None, ""):
            session = getattr(event, "session", None)
            platform_id = getattr(session, "platform_id", None)
        return "" if platform_id in (None, "") else str(platform_id).strip()

    def _get_event_client(self, event):
        """获取事件绑定的平台客户端，避免同类型多适配器串号。"""
        bot = getattr(event, "bot", None)
        if bot is not None:
            if callable(getattr(bot, "call_action", None)):
                return bot
            api = getattr(bot, "api", None)
            if api is not None and callable(getattr(api, "call_action", None)):
                return api
            if any(
                callable(getattr(bot, method, None))
                for method in ("send_message", "send_photo", "kick_chat_member")
            ):
                return bot

        platform_id = self._get_event_platform_id(event)
        platform_inst = self._get_platform_instance(platform_id)
        if platform_inst is not None:
            get_client = getattr(platform_inst, "get_client", None)
            if callable(get_client):
                client = get_client()
                if client is not None:
                    return client

        raise RuntimeError(
            f"event-bound platform client is unavailable (platform_id={platform_id or 'unknown'})"
        )

    def _get_platform_instance(self, platform_id: str):
        """Resolve exactly one configured AstrBot platform instance."""
        if not platform_id:
            return None
        get_platform_inst = getattr(getattr(self, "context", None), "get_platform_inst", None)
        if not callable(get_platform_inst):
            return None
        try:
            return get_platform_inst(platform_id)
        except Exception as exc:
            logger.warning(
                "[Geetest Verify] 获取平台实例失败，platform_id=%s，错误类型=%s",
                platform_id,
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _get_client_identity_candidates(client, platform_inst=None) -> set[str]:
        """Return declared account identities, never a generated object ID."""
        objects = [client]
        api = getattr(client, "api", None)
        if api is not None:
            objects.append(api)
        if platform_inst is not None:
            objects.append(platform_inst)

        identities = set()
        for obj in objects:
            for attr_name in ("id", "username"):
                value = getattr(obj, attr_name, None)
                if value not in (None, ""):
                    identities.add(str(value).strip())
            for method_name in ("get_self_id", "get_bot_id"):
                method = getattr(obj, method_name, None)
                if callable(method):
                    try:
                        value = method()
                    except Exception:
                        value = None
                    if value not in (None, ""):
                        identities.add(str(value).strip())
            for attr_name in ("self_id", "bot_id", "account_id"):
                value = getattr(obj, attr_name, None)
                if value not in (None, ""):
                    identities.add(str(value).strip())
        return identities

    @classmethod
    def _client_matches_persisted_identity(
        cls,
        client,
        platform_inst,
        platform_type: str,
        expected_identity: str,
    ) -> bool:
        if platform_type == "aiocqhttp":
            # AstrBot's real CQHttp object has no self_id attribute. Reverse
            # WebSocket clients are keyed by the OneBot self_id instead.
            api_clients = getattr(client, "_wsr_api_clients", None)
            if not isinstance(api_clients, dict):
                return False
            return any(
                str(client_id).strip() == expected_identity and api_client is not None
                for client_id, api_client in api_clients.items()
            )

        return expected_identity in cls._get_client_identity_candidates(
            client,
            platform_inst,
        )

    def _resolve_persisted_event(self, state: dict, gid: int, uid: str):
        """Build an event-like object only for the persisted platform identity.

        Recovery must never fall back to another adapter of the same type.  An
        unavailable instance or an identity mismatch therefore returns None and
        leaves the state pending for the next recovery pass.
        """
        platform_type = str(state.get("platform_type") or "").strip()
        platform_id = str(state.get("platform_instance_id") or "").strip()
        expected_identity = str(state.get("bot_identity") or "").strip()
        if not platform_type or not platform_id or not expected_identity:
            return None

        platform_inst = self._get_platform_instance(platform_id)
        if platform_inst is None:
            logger.warning(
                "[Geetest Verify] 恢复群 %s 用户 %s 失败：平台实例 %s 当前不可用，保留验证记录",
                gid,
                uid,
                platform_id,
            )
            return None

        meta = getattr(platform_inst, "meta", None)
        if callable(meta):
            try:
                metadata = meta()
                actual_platform_type = str(
                    getattr(metadata, "name", "") or ""
                ).strip()
            except Exception:
                actual_platform_type = ""
            if actual_platform_type and actual_platform_type != platform_type:
                logger.error(
                    "[Geetest Verify] 恢复群 %s 用户 %s 拒绝平台实例 %s：平台类型不匹配（期望=%s，实际=%s）",
                    gid,
                    uid,
                    platform_id,
                    platform_type,
                    actual_platform_type,
                )
                return None

        get_client = getattr(platform_inst, "get_client", None)
        if not callable(get_client):
            logger.warning(
                "[Geetest Verify] 恢复群 %s 用户 %s 失败：平台实例 %s 没有客户端，保留验证记录",
                gid,
                uid,
                platform_id,
            )
            return None
        try:
            client = get_client()
        except Exception as exc:
            logger.warning(
                "[Geetest Verify] 恢复群 %s 用户 %s 获取平台实例 %s 客户端失败（%s），保留验证记录",
                gid,
                uid,
                platform_id,
                type(exc).__name__,
            )
            return None
        if client is None:
            return None

        try:
            identity_matches = self._client_matches_persisted_identity(
                client,
                platform_inst,
                platform_type,
                expected_identity,
            )
        except Exception as exc:
            logger.warning(
                "[Geetest Verify] 恢复群 %s 用户 %s 读取平台实例 %s 机器人身份失败（%s），"
                "保留验证记录等待重试",
                gid,
                uid,
                platform_id,
                type(exc).__name__,
            )
            return None

        if not identity_matches:
            logger.error(
                "[Geetest Verify] 恢复群 %s 用户 %s 拒绝使用平台实例 %s：机器人身份不匹配（期望=%s）",
                gid,
                uid,
                platform_id,
                expected_identity,
            )
            return None

        raw_message = {
            "group_id": int(gid),
            "self_id": expected_identity,
            "chat": {"id": int(gid)},
        }
        return SimpleNamespace(
            bot=client,
            session=platform_inst,
            message_obj=SimpleNamespace(raw_message=raw_message),
            get_platform_name=lambda: platform_type,
            get_platform_id=lambda: platform_id,
            get_self_id=lambda: expected_identity,
            get_sender_id=lambda: str(uid),
            message_str="",
            stop_event=lambda: None,
        )

    def _get_event_call_action(self, event):
        """获取事件绑定客户端的 ``call_action`` 方法。"""
        client = self._get_event_client(event)
        call_action = getattr(client, "call_action", None)
        if not callable(call_action):
            api = getattr(client, "api", None)
            call_action = getattr(api, "call_action", None)
        if not callable(call_action):
            raise RuntimeError("event-bound platform client does not support call_action")

        async def routed_call_action(action, **kwargs):
            if self._get_platform(event) == "aiocqhttp":
                self_id = self._get_event_self_id(event)
                if self_id:
                    kwargs.setdefault("self_id", self_id)
            return await call_action(action, **kwargs)

        return routed_call_action

    async def _call_event_action(self, event, action: str, **kwargs):
        """通过收到事件的同一机器人执行平台动作。"""
        logger.debug(
            "[Geetest Verify] 执行动作 %s，platform_id=%s，self_id=%s",
            action,
            self._get_event_platform_id(event) or "未知",
            self._get_event_self_id(event) or "未知",
        )
        return await self._get_event_call_action(event)(action, **kwargs)

    def _event_matches_verification_platform(
        self,
        event,
        gid: int,
        group_config: dict,
        *,
        log_decision: bool = False,
    ) -> bool:
        """判断事件是否来自群配置指定的 AstrBot 平台实例。"""
        expected = str(
            group_config.get("verification_platform_id", "") or ""
        ).strip()
        if not expected:
            return True

        current = self._get_event_platform_id(event)
        self_id = self._get_event_self_id(event) or "未知"

        if not current:
            logger.error(
                "[Geetest Verify] 群 %s 已指定 AstrBot Bot ID=%s，但事件缺少 "
                "platform_id (self_id=%s)，严格跳过事件",
                gid,
                expected,
                self_id,
            )
            return False

        if current != expected:
            log = logger.info if log_decision else logger.debug
            log(
                "[Geetest Verify] 群 %s 事件来自非指定机器人，platform_id=%s，"
                "self_id=%s，指定 platform_id=%s，跳过事件",
                gid,
                current,
                self_id,
                expected,
            )
            return False

        if log_decision:
            logger.info(
                "[Geetest Verify] 群 %s 由指定机器人处理，platform_id=%s，self_id=%s",
                gid,
                current,
                self_id,
            )
        return True

    async def _is_current_bot_group_admin(self, event, gid: int):
        """检查当前事件绑定的 QQ 机器人是否为群管理员。"""
        try:
            role = await query_current_bot_group_role(event, gid, self._get_raw_value)
        except Exception as exc:
            logger.warning(
                "[Geetest Verify] 查询群 %s 当前机器人权限失败（%s），"
                "platform_id=%s，self_id=%s",
                gid,
                type(exc).__name__,
                self._get_event_platform_id(event) or "未知",
                self._get_event_self_id(event) or "未知",
            )
            return None

        allowed = role_allows_verification(role)
        if allowed is True:
            logger.info(
                "[Geetest Verify] 当前机器人在群 %s 的角色为 %s，platform_id=%s，self_id=%s",
                gid,
                role,
                self._get_event_platform_id(event) or "未知",
                self._get_event_self_id(event) or "未知",
            )
        elif allowed is False:
            logger.info(
                "[Geetest Verify] 当前机器人在群 %s 的角色为 %s，"
                "platform_id=%s，self_id=%s，跳过入群验证",
                gid,
                role,
                self._get_event_platform_id(event) or "未知",
                self._get_event_self_id(event) or "未知",
            )
        else:
            logger.warning(
                "[Geetest Verify] 未能获取群 %s 当前机器人的有效群角色，"
                "platform_id=%s，self_id=%s",
                gid,
                self._get_event_platform_id(event) or "未知",
                self._get_event_self_id(event) or "未知",
            )
        return allowed

    def _get_group_id(self, platform: str, raw) -> int:
        """安全地获取群组 ID"""
        if platform == "telegram":
            chat = self._get_raw_value(raw, "chat") or {}
            if chat:
                chat_id = self._get_raw_value(chat, "id")
                if chat_id:
                    logger.debug(f"[Geetest Verify] 从 chat 获取到群组 ID: {chat_id}")
                    return int(chat_id)

            message = self._get_raw_value(raw, "message") or {}
            if message:
                message_chat = self._get_raw_value(message, "chat") or {}
                if message_chat:
                    chat_id = self._get_raw_value(message_chat, "id")
                    if chat_id:
                        logger.debug(f"[Geetest Verify] 从 message.chat 获取到群组 ID: {chat_id}")
                        return int(chat_id)

            callback_query = self._get_raw_value(raw, "callback_query") or {}
            if callback_query:
                cb_message = self._get_raw_value(callback_query, "message") or {}
                if cb_message:
                    cb_chat = self._get_raw_value(cb_message, "chat") or {}
                    if cb_chat:
                        chat_id = self._get_raw_value(cb_chat, "id")
                        if chat_id:
                            logger.debug(f"[Geetest Verify] 从 callback_query.message.chat 获取到群组 ID: {chat_id}")
                            return int(chat_id)

            logger.warning(f"[Geetest Verify] 无法从 Telegram 消息中获取群组 ID, raw 类型: {type(raw)}")
            logger.debug(f"[Geetest Verify] raw 内容: {raw}")
            return None
        else:
            group_id = self._get_raw_value(raw, "group_id")
            if group_id:
                logger.debug(f"[Geetest Verify] 获取到 QQ 群组 ID: {group_id}")
                return int(group_id)
            logger.warning("[Geetest Verify] 无法从 QQ 消息中获取群组 ID")
            return None

    def _format_user_mention(self, event, uid: str) -> str:
        """根据平台格式化用户提及"""
        platform = self._get_platform(event)
        if platform == "telegram":
            raw = event.message_obj.raw_message
            new_member = self._get_raw_value(raw, "new_chat_member") or {}
            user_info = self._get_raw_value(new_member, "user") or {}
            username = self._get_raw_value(user_info, "username") or ""
            first_name = self._get_raw_value(user_info, "first_name") or ""

            if username:
                return f"@{username}"
            elif first_name:
                return f"[{first_name}](tg://user?id={uid})"
            else:
                return f"[用户](tg://user?id={uid})"
        else:
            return f"[CQ:at,qq={uid}]"

    async def _send_group_message(self, event, gid: int, message: str):
        """根据平台发送群消息。如果 message 为空则不发送。"""
        if not message:
            return
        platform = self._get_platform(event)
        try:
            if platform == "telegram":
                platform_client = self._get_event_client(event)
                if hasattr(platform_client, "call_action"):
                    return await platform_client.call_action("send_message", chat_id=gid, text=message, parse_mode="Markdown")
                else:
                    return await platform_client.send_message(chat_id=gid, text=message, parse_mode="Markdown")
            else:
                return await self._call_event_action(
                    event, "send_group_msg", group_id=gid, message=message
                )
        except Exception as e:
            logger.error(f"[Geetest Verify] 发送消息失败: {e}")

    def _resolve_image_path(self, image_path: str) -> str:
        """解析图片路径，支持 URL、绝对路径和相对路径"""
        if not image_path:
            return ""
        if image_path.lower().startswith(("http://", "https://")):
            return image_path
        if os.path.isabs(image_path):
            return image_path
        resolved = str(self._data_dir / image_path)
        logger.info(f"[Geetest Verify] 解析相对路径图片: {image_path} -> {resolved}")
        return resolved

    async def _send_group_message_with_image(self, event, gid: int, text: str, image_path: str = None):
        """发送群消息，附带图片。如果 text 和 image_path 都为空则不发送。"""
        if not text and not image_path:
            return
        if not image_path:
            return await self._send_group_message(event, gid, text)

        resolved_path = self._resolve_image_path(image_path)
        if not resolved_path:
            return await self._send_group_message(event, gid, text)

        platform = self._get_platform(event)
        try:
            if platform == "telegram":
                platform_client = self._get_event_client(event)
                if hasattr(platform_client, "call_action"):
                    await platform_client.call_action("send_photo", chat_id=gid, photo=resolved_path, caption=text)
                else:
                    await platform_client.send_photo(chat_id=gid, photo=resolved_path, caption=text)
            else:
                message_with_image = f"{text}\n[CQ:image,file={resolved_path}]"
                await self._call_event_action(
                    event,
                    "send_group_msg",
                    group_id=gid,
                    message=message_with_image,
                )
        except Exception as e:
            logger.error(f"[Geetest Verify] 发送带图片消息失败: {e}")
            await self._send_group_message(event, gid, text)

    async def _kick_member(self, event, gid: int, uid: str):
        """根据平台踢出成员"""
        platform = self._get_platform(event)

        if platform == "telegram":
            platform_client = self._get_event_client(event)
            ban_chat_member = getattr(platform_client, "ban_chat_member", None)
            if callable(ban_chat_member):
                return await ban_chat_member(chat_id=gid, user_id=int(uid))
            call_action = getattr(platform_client, "call_action", None)
            if callable(call_action):
                return await call_action(
                    "banChatMember",
                    chat_id=gid,
                    user_id=int(uid),
                )
            raise RuntimeError("event-bound Telegram client has no ban_chat_member")
        else:
            return await self._call_event_action(
                event,
                "set_group_kick",
                group_id=gid,
                user_id=int(uid),
                reject_add_request=False,
            )

    @staticmethod
    def _response_failed(response) -> bool:
        if response is False:
            return True
        if not isinstance(response, dict):
            return False
        if response.get("status") == "failed" or response.get("ok") is False:
            return True
        retcode = response.get("retcode")
        return retcode not in (None, 0)

    @staticmethod
    def _response_payload(response):
        if not isinstance(response, dict):
            return response
        for key in ("data", "result"):
            payload = response.get(key)
            if payload is not None:
                return payload
        return response

    async def _get_member_presence(self, event, gid: int, uid: str) -> str:
        """Return present, absent, or unknown for the exact event-bound bot."""
        platform = self._get_platform(event)
        try:
            if platform == "telegram":
                client = self._get_event_client(event)
                get_chat_member = getattr(client, "get_chat_member", None)
                if callable(get_chat_member):
                    member = await get_chat_member(
                        chat_id=int(gid),
                        user_id=int(uid),
                    )
                    status = getattr(member, "status", None)
                    is_member = getattr(member, "is_member", None)
                else:
                    call_action = getattr(client, "call_action", None)
                    if not callable(call_action):
                        return "unknown"
                    member = await call_action(
                        "getChatMember",
                        chat_id=int(gid),
                        user_id=int(uid),
                    )
                    if self._response_failed(member):
                        return "unknown"
                    member = self._response_payload(member)
                    if not isinstance(member, dict):
                        return "unknown"
                    status = member.get("status")
                    is_member = member.get("is_member")

                status = str(status or "").strip().lower()
                if status in {"left", "kicked"}:
                    return "absent"
                if status == "restricted" and is_member is False:
                    return "absent"
                if status in {"member", "administrator", "creator", "restricted"}:
                    return "present"
                return "unknown"

            response = await self._call_event_action(
                event,
                "get_group_member_info",
                group_id=int(gid),
                user_id=int(uid),
            )
            if not self._response_failed(response):
                payload = self._response_payload(response)
                if isinstance(payload, dict) and (
                    payload.get("user_id") not in (None, "")
                    or payload.get("role") not in (None, "")
                    or payload.get("nickname") not in (None, "")
                ):
                    return "present"

            # A failed member-info call is ambiguous across OneBot adapters.
            # A successful member list lets us confirm absence without guessing.
            response = await self._call_event_action(
                event,
                "get_group_member_list",
                group_id=int(gid),
            )
            if self._response_failed(response):
                return "unknown"
            payload = self._response_payload(response)
            if not isinstance(payload, list):
                return "unknown"
            target = str(uid)
            for member in payload:
                if isinstance(member, dict) and str(member.get("user_id")) == target:
                    return "present"
            return "absent"
        except Exception as exc:
            logger.warning(
                "[Geetest Verify] 查询群 %s 用户 %s 成员状态失败（%s）",
                gid,
                uid,
                type(exc).__name__,
            )
            return "unknown"

    async def _get_user_info(self, event, uid: str) -> dict:
        """根据平台获取用户信息"""
        platform = self._get_platform(event)
        if platform == "telegram":
            try:
                chat_member = await self._call_event_action(
                    event,
                    "getChatMember",
                    chat_id=event.message_obj.raw_message.get("chat", {}).get("id"),
                    user_id=int(uid),
                )
                return chat_member.get("user", {})
            except Exception:
                return {}
        else:
            try:
                user_info = await self._call_event_action(
                    event, "get_stranger_info", user_id=int(uid)
                )
                return user_info
            except Exception:
                return {}

    async def _delete_message(self, event, message_id):
        """通过事件绑定的机器人撤回消息。"""
        await self._call_event_action(event, "delete_msg", message_id=message_id)
