"""
HTTP 客户端

封装 requests.Session，提供：
- Cookie 自动注入
- UA 伪装
- 随机延迟（速率限制）
- 指数退避重试
- 响应状态预检
"""

import time
import random
import logging
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import ScraperConfig
from .exceptions import (
    CookieExpiredError,
    RequestBlockedError,
    RateLimitError,
    PostNotFoundError,
    LoginRedirectError,
)

logger = logging.getLogger("weibo_scraper.client")

# ============================================================
# 微博移动端 API 端点
# ============================================================
BASE_URL = "https://m.weibo.cn"

# 获取帖子详情
API_POST_DETAIL = "/api/container/getIndex"

# 评论列表（热门 / 时间正序）
API_COMMENTS = "/comments/hotflow"

# 子评论（评论的回复）
API_SUB_COMMENTS = "/comments/hotFlowChild"


class WeiboClient:
    """微博移动端 HTTP 客户端"""

    # ---- 默认请求头 ----
    # 注意：Referer 不在此处固定，因为评论翻页接口要求 Referer
    # 指向具体帖子详情页 https://m.weibo.cn/detail/{post_id}，
    # 否则第二页起会被判定为未登录（ok=-100）。
    DEFAULT_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
    }

    def __init__(self, config: ScraperConfig):
        self.config = config
        self._session: Optional[requests.Session] = None
        self._request_count = 0

    # -----------------------------------------------------------
    # Session 懒加载
    # -----------------------------------------------------------

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = self._build_session()
        return self._session

    def _build_session(self) -> requests.Session:
        s = requests.Session()

        # User-Agent
        headers = {
            "User-Agent": self.config.user_agent,
            **self.DEFAULT_HEADERS,
        }
        s.headers.update(headers)

        # Cookie
        if self.config.has_cookie():
            s.headers["Cookie"] = self.config.cookie

        # 重试适配器（仅针对 5xx 和连接错误做短重试）
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        s.mount("https://", adapter)
        s.mount("http://", adapter)

        return s

    # -----------------------------------------------------------
    # 公开 API
    # -----------------------------------------------------------

    def fetch_post_detail(self, post_id: str) -> dict:
        """
        获取帖子详情，用于校验帖子是否存在。
        返回 API 原始 JSON。
        """
        params = {
            "type": "item",
            "id": post_id,
        }
        return self._get(API_POST_DETAIL, params=params)

    def fetch_comments(
        self,
        post_id: str,
        max_id: int = 0,
        max_id_type: int = 0,
    ) -> dict:
        """
        获取一页评论。

        Parameters
        ----------
        post_id : str
            微博帖子 ID。
        max_id : int
            分页游标，0 表示第一页。
        max_id_type : int
            分页游标类型，必须使用上一次响应返回的值；
            第一页传 0。

        Returns
        -------
        dict : API 原始 JSON。
        """
        params = {
            "id": post_id,
            "mid": post_id,
            "max_id": max_id,
            "max_id_type": max_id_type,
        }

        # hotflow 接口要求 Referer 指向帖子详情页，否则第二页起返回 ok=-100
        headers = {"Referer": f"{BASE_URL}/detail/{post_id}"}
        return self._get(API_COMMENTS, params=params, headers=headers)

    def fetch_sub_comments(
        self,
        comment_id: str,
        post_id: str = "",
        root_comment_id: str = "",
        max_id: int = 0,
        max_id_type: int = 0,
        limit: int = 30,
    ) -> dict:
        """
        获取某条评论的子评论（回复）。

        Parameters
        ----------
        comment_id : str
            当前评论 ID。
        post_id : str
            所属帖子 ID，用于生成 Referer。
        root_comment_id : str
            根评论 ID（一级评论 ID），用于获取回复的回复。
        max_id : int
            分页游标。
        max_id_type : int
            分页游标类型，必须使用上一次响应返回的值。
        limit : int
            每页条数。
        """
        params = {
            "cid": comment_id,
            "max_id": max_id,
            "max_id_type": max_id_type,
            "count": min(limit, 30),
            "rootCommentId": root_comment_id or comment_id,
        }
        headers = None
        if post_id:
            headers = {"Referer": f"{BASE_URL}/detail/{post_id}"}
        return self._get(API_SUB_COMMENTS, params=params, headers=headers)

    # -----------------------------------------------------------
    # 底层 HTTP 请求
    # -----------------------------------------------------------

    def _get(
        self,
        path: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> dict:
        """
        发送 GET 请求，含速率控制、重试、响应校验。
        """
        url = BASE_URL + path
        self._rate_limit()

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.config.retry_times + 1):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.config.timeout,
                )
                self._request_count += 1

                # -- 检查 HTTP 状态码 --
                if resp.status_code == 200:
                    data = resp.json()
                    self._validate_response(data, resp.url)
                    return data

                if resp.status_code == 403:
                    raise RequestBlockedError(
                        f"请求被拦截 (HTTP 403): {resp.url}\n"
                        "请更新 Cookie 或降低请求频率"
                    )
                if resp.status_code == 418:
                    raise RequestBlockedError(
                        f"触发了微博反爬机制 (HTTP 418): {resp.url}\n"
                        "建议更换 IP 或更新 Cookie"
                    )
                if resp.status_code == 404:
                    raise PostNotFoundError(
                        f"资源不存在 (HTTP 404): {resp.url}"
                    )
                if resp.status_code == 429:
                    if attempt < self.config.retry_times:
                        wait = 2 ** attempt * 5
                        logger.warning(
                            "触发频率限制 (429)，等待 %d 秒后重试 (%d/%d)...",
                            wait, attempt + 1, self.config.retry_times,
                        )
                        time.sleep(wait)
                        continue
                    raise RateLimitError("请求过于频繁，已达到最大重试次数")

                # 其他非 200 状态码
                logger.warning(
                    "非预期状态码 %d: %s (尝试 %d/%d)",
                    resp.status_code, resp.url, attempt, self.config.retry_times,
                )
                if attempt < self.config.retry_times:
                    time.sleep(2 ** attempt)
                    continue
                raise RequestBlockedError(
                    f"请求失败 (HTTP {resp.status_code}): {resp.url}"
                )

            except LoginRedirectError:
                # ok=-100：会话中多为软封锁，退避重试；持续失败则需重新登录
                if attempt < self.config.retry_times:
                    wait = 2 ** attempt * 5
                    logger.warning(
                        "登录重定向 (ok=-100)，疑似软封锁，等待 %d 秒后重试 (%d/%d)...",
                        wait, attempt + 1, self.config.retry_times,
                    )
                    time.sleep(wait)
                    continue
                raise CookieExpiredError(
                    "请求持续被重定向到登录页 (ok=-100)，Cookie 可能已失效。\n"
                    "请重新运行并扫码登录。"
                )
            except (CookieExpiredError, RequestBlockedError, RateLimitError, PostNotFoundError):
                raise
            except requests.exceptions.Timeout as e:
                last_exc = e
                logger.warning("请求超时 (尝试 %d/%d)", attempt, self.config.retry_times)
                if attempt < self.config.retry_times:
                    time.sleep(2 ** attempt)
                    continue
            except requests.exceptions.ConnectionError as e:
                last_exc = e
                logger.warning("连接错误 (尝试 %d/%d): %s", attempt, self.config.retry_times, e)
                if attempt < self.config.retry_times:
                    time.sleep(2 ** attempt)
                    continue
            except Exception as e:
                last_exc = e
                logger.warning("请求异常 (尝试 %d/%d): %s", attempt, self.config.retry_times, e)
                if attempt < self.config.retry_times:
                    time.sleep(2 ** attempt)
                    continue

        if last_exc:
            raise RequestBlockedError(f"请求失败: {url}，最后错误: {last_exc}") from last_exc
        raise RequestBlockedError(f"请求失败: {url}")

    # -----------------------------------------------------------
    # 内部辅助
    # -----------------------------------------------------------

    def _rate_limit(self):
        """随机延迟，避免触发频率限制"""
        if self._request_count > 0:
            delay = random.uniform(self.config.delay_min, self.config.delay_max)
            time.sleep(delay)

    @staticmethod
    def _validate_response(data: dict, url: str):
        """
        校验 API 返回的 JSON 是否正常。

        Weibo API 约定：
        - ok == 1        → 正常
        - ok == 0        → 错误（cookie 过期等）
        - ok == -100     → 未登录，重定向到 passport.weibo.com

        会话过程中 ok=-100 多为软封锁/限流，故抛 LoginRedirectError
        交由 _get 退避重试；持续失败才判定为 Cookie 失效。
        """
        if data.get("ok") == 0:
            errmsg = data.get("msg", "未知错误")
            if "cookie" in str(errmsg).lower() or "登录" in str(errmsg):
                raise CookieExpiredError(
                    f"Cookie 已过期或无效: {errmsg}\n请重新扫码登录"
                )
            raise RequestBlockedError(f"API 返回错误 (ok=0): {errmsg} | URL: {url}")
        if data.get("ok") == -100:
            # 重定向到登录页：会话中通常是软封锁，交给 _get 重试
            raise LoginRedirectError(
                f"请求被重定向到登录页 (ok=-100)：{data.get('url', '')}"
            )
        if data.get("ok") != 1:
            # 有些接口不返回 ok 字段，属于正常情况
            logger.debug("API 响应未包含 ok=1: %s...", str(data)[:200])

    # -----------------------------------------------------------
    # 资源管理
    # -----------------------------------------------------------

    def close(self):
        if self._session:
            self._session.close()
            self._session = None

    @property
    def total_requests(self) -> int:
        return self._request_count

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
