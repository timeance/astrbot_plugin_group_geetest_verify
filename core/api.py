import logging
from typing import Optional
from urllib.parse import urljoin, urlparse
import aiohttp

from astrbot.core.config.default import VERSION as ASTRBOT_VERSION

from .version import PLUGIN_VERSION

logger = logging.getLogger(__name__)


class GeetestAPIMixin:
    """极验验证 API 调用相关方法"""

    def _normalize_base_url(self) -> str:
        """规范化后端地址，避免请求路径出现双斜杠。"""
        return (self.api_base_url or "").strip().rstrip("/")

    async def _create_geetest_verify(self, gid: int, uid: str) -> Optional[str]:
        """调用极验 API 生成验证链接，保留完整地址与参数。"""
        if not self.api_key:
            logger.error("[Geetest Verify] API 密钥未配置")
            return None

        url = f"{self._normalize_base_url()}/verify/create"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"AstrBot/v{ASTRBOT_VERSION} group_geetest_verify/v{PLUGIN_VERSION}"
        }
        data = {
            "group_id": str(gid),
            "user_id": uid
        }

        try:
            async with self.session.post(url, json=data, headers=headers) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("code") == 0:
                        payload = result.get("data")
                        full_url = payload.get("url") if isinstance(payload, dict) else None
                        if not isinstance(full_url, str) or not full_url.strip():
                            logger.error("[Geetest Verify] API 返回的验证链接为空或无效")
                            return None
                        full_url = urljoin(self._normalize_base_url() + "/", full_url.strip())
                        parsed = urlparse(full_url)
                        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                            logger.error("[Geetest Verify] API 返回的验证链接不是有效的 HTTP(S) 地址")
                            return None
                        logger.info("[Geetest Verify] 成功生成验证链接")
                        return full_url
                    else:
                        logger.error(f"[Geetest Verify] API 返回错误: {result.get('msg')}")
                        return None
                else:
                    logger.error(f"[Geetest Verify] API 请求失败，状态码: {response.status}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"[Geetest Verify] API 请求异常: {e}")
            return None
        except Exception as e:
            logger.error(f"[Geetest Verify] 生成验证链接异常: {e}", exc_info=True)
            return None

    async def _check_geetest_verify(self, gid: int, uid: str, code: str) -> bool:
        """调用极验 API 验证验证码"""
        if not self.api_key:
            logger.error("[Geetest Verify] API 密钥未配置")
            return False

        url = f"{self._normalize_base_url()}/verify/check"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"AstrBot/v{ASTRBOT_VERSION} group_geetest_verify/v{PLUGIN_VERSION}"
        }
        data = {
            "group_id": str(gid),
            "user_id": uid,
            "code": code
        }

        try:
            async with self.session.post(url, json=data, headers=headers) as response:
                if response.status == 200:
                    result = await response.json()
                    payload = result.get("data")
                    payload = payload if isinstance(payload, dict) else {}
                    passed = result.get("passed")
                    if passed is None:
                        passed = payload.get("passed")
                    if result.get("code") == 0 and passed is True:
                        logger.info("[Geetest Verify] 验证码验证成功")
                        return True
                    else:
                        logger.info(
                            "[Geetest Verify] 验证码验证失败: code=%s, passed=%s, msg=%s",
                            result.get("code"), passed, result.get("msg") or payload.get("msg"),
                        )
                        return False
                else:
                    logger.error(f"[Geetest Verify] API 请求失败，状态码: {response.status}")
                    return False
        except aiohttp.ClientError as e:
            logger.error(f"[Geetest Verify] API 请求异常: {e}")
            return False
        except Exception as e:
            logger.error(f"[Geetest Verify] 验证验证码异常: {e}", exc_info=True)
            return False
