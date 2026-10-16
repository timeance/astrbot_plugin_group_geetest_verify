import os
import logging
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
        get_platform_inst = getattr(self.context, "get_platform_inst", None)
        if platform_id and callable(get_platform_inst):
            platform_inst = get_platform_inst(platform_id)
            if platform_inst is not None:
                get_client = getattr(platform_inst, "get_client", None)
                if callable(get_client):
                    client = get_client()
                    if client is not None:
                        return client

        raise RuntimeError(
            f"event-bound platform client is unavailable (platform_id={platform_id or 'unknown'})"
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
        return call_action

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
            if hasattr(platform_client, "call_action"):
                await platform_client.call_action("kickChatMember", chat_id=gid, user_id=int(uid))
            else:
                await platform_client.kick_chat_member(chat_id=gid, user_id=int(uid))
        else:
            await self._call_event_action(
                event,
                "set_group_kick",
                group_id=gid,
                user_id=int(uid),
                reject_add_request=False,
            )

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
