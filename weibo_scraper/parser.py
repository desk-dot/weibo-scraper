"""
响应解析器

将微博移动端 API 的 JSON 响应解析为结构化的 Comment 对象。
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .exceptions import ParseError

logger = logging.getLogger("weibo_scraper.parser")

# ============================================================
# 数据模型
# ============================================================


@dataclass
class User:
    """用户信息"""
    user_id: str = ""
    screen_name: str = ""
    profile_url: str = ""
    avatar_url: str = ""
    verified: bool = False
    verified_type: int = 0

    @classmethod
    def from_api(cls, raw: dict) -> "User":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            user_id=str(raw.get("id", "")),
            screen_name=raw.get("screen_name", ""),
            profile_url=f"https://m.weibo.cn/profile/{raw.get('id', '')}",
            avatar_url=raw.get("profile_image_url", ""),
            verified=raw.get("verified", False),
            verified_type=raw.get("verified_type", 0),
        )


@dataclass
class Comment:
    """单条评论"""
    comment_id: str = ""
    post_id: str = ""
    created_at: str = ""
    text: str = ""                    # 纯文本，已清洗 HTML
    text_raw: str = ""                # 原始 HTML 文本
    like_count: int = 0
    reply_count: int = 0              # 子评论数
    user: Optional[User] = None
    source: str = ""                  # 客户端来源（如 iPhone 客户端）
    is_sub: bool = False              # 是否为子评论
    parent_comment_id: str = ""       # 若为子评论，指向父评论
    sub_comments: list["Comment"] = field(default_factory=list)

    def to_dict(self) -> dict:
        """转换为扁平字典（子评论不嵌套，单独记录）"""
        d = {
            "comment_id": self.comment_id,
            "post_id": self.post_id,
            "parent_comment_id": self.parent_comment_id,
            "is_sub": int(self.is_sub),
            "created_at": self.created_at,
            "text": self.text,
            "like_count": self.like_count,
            "reply_count": self.reply_count,
            "source": self.source,
        }
        if self.user:
            d["user_id"] = self.user.user_id
            d["user_name"] = self.user.screen_name
            d["user_verified"] = int(self.user.verified)
            d["user_profile_url"] = self.user.profile_url
            d["user_avatar_url"] = self.user.avatar_url
        return d


# ============================================================
# HTML 清洗工具
# ============================================================

# 匹配 HTML 标签
_RE_HTML_TAG = re.compile(r"<[^>]+>")

# 匹配微博表情 [xxx]
_RE_EMOJI = re.compile(r"\[([a-zA-Z一-鿿]+)\]")

# 匹配 @ 提醒和 # 话题（保留原文，仅移除 HTML）
_RE_MENTION_HTML = re.compile(r'<a[^>]*href="/n/([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE)
_RE_TOPIC_HTML = re.compile(r'<a[^>]*href="/p/[^"]*"[^>]*>(.*?)</a>', re.IGNORECASE)
_RE_HASHTAG = re.compile(r'<span class="surl-text">([^<]+)</span>', re.IGNORECASE)
_RE_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


def clean_html(text: str) -> str:
    """
    将微博评论文本中的 HTML 转换为可读纯文本。

    处理：
    - <br />         → 换行
    - <a href=...>   → 保留链接文字（@昵称、#话题#、超链接）
    - <img alt=...>  → [表情描述]
    - 其他标签        → 移除
    """
    if not text:
        return ""

    # <br> → 换行
    text = _RE_BR.sub("\n", text)

    # <img alt="[xxx]"> → [xxx]
    text = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*>', r"\1", text)

    # 提取 @ 昵称和话题文字，去掉 <a> 标签
    text = _RE_MENTION_HTML.sub(r"@\1", text)
    text = _RE_TOPIC_HTML.sub(r"\1", text)
    text = _RE_HASHTAG.sub(r"\1", text)

    # 删除剩余的所有 HTML 标签
    text = _RE_HTML_TAG.sub("", text)

    # 解码 HTML 实体
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    text = text.replace("&nbsp;", " ")

    return text.strip()


# ============================================================
# 解析器
# ============================================================


class CommentParser:
    """评论数据解析器"""

    # ---- 主评论列表 ----

    @staticmethod
    def parse_comments(data: dict, post_id: str) -> list[Comment]:
        """
        从 hotflow 响应中解析评论列表。

        Parameters
        ----------
        data : dict
            fetch_comments 返回的完整 JSON。
        post_id : str
            所属帖子 ID。

        Returns
        -------
        list[Comment]
        """
        if data is None:
            return []
        if not isinstance(data, dict):
            raise ParseError(f"期望 dict，收到 {type(data).__name__}")

        # 兼容不同的返回结构
        inner = data.get("data", {})
        if isinstance(inner, dict):
            comment_list = inner.get("data", [])
        elif isinstance(inner, list):
            comment_list = inner
        else:
            comment_list = []

        if not isinstance(comment_list, list):
            logger.warning("评论列表格式异常: %s", type(comment_list))
            return []

        comments = []
        for item in comment_list:
            try:
                c = CommentParser._parse_single_comment(item, post_id)
                comments.append(c)
            except Exception as exc:
                logger.warning("解析单条评论失败: %s (id=%s)", exc, item.get("id", "?"))
                continue

        return comments

    # ---- 子评论列表 ----

    @staticmethod
    def parse_sub_comments(
        data: dict,
        post_id: str,
        parent_comment_id: str,
    ) -> list[Comment]:
        """
        从 hotFlowChild 响应中解析子评论。

        Parameters
        ----------
        data : dict
            fetch_sub_comments 返回的完整 JSON。
        post_id : str
            所属帖子 ID。
        parent_comment_id : str
            父评论 ID。
        """
        if data is None or not isinstance(data, dict):
            return []

        inner = data.get("data", {})
        if isinstance(inner, dict):
            sub_list = inner.get("data", [])
        elif isinstance(inner, list):
            sub_list = inner
        else:
            sub_list = []

        if not isinstance(sub_list, list):
            return []

        subs = []
        for item in sub_list:
            try:
                c = CommentParser._parse_single_comment(item, post_id)
                c.is_sub = True
                c.parent_comment_id = parent_comment_id
                subs.append(c)
            except Exception as exc:
                logger.warning("解析子评论失败: %s", exc)
                continue

        return subs

    # ---- 分页游标提取 ----

    @staticmethod
    def _safe_int(value, default: int = 0) -> int:
        """安全转换为 int，失败返回默认值"""
        try:
            if value is None:
                return default
            return int(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def get_max_id(data: dict) -> int:
        """
        从 hotflow / hotFlowChild 响应中提取下一页游标。
        返回 0 表示没有更多页。
        """
        if not isinstance(data, dict):
            return 0
        inner = data.get("data", {})
        if isinstance(inner, dict):
            return CommentParser._safe_int(inner.get("max_id"), 0)
        return 0

    @staticmethod
    def get_max_id_type(data: dict) -> int:
        """
        从 hotflow / hotFlowChild 响应中提取下一页的 max_id_type。
        此值必须与 max_id 配对传给下一次请求，否则 API 会返回空数据。
        """
        if not isinstance(data, dict):
            return 0
        inner = data.get("data", {})
        if isinstance(inner, dict):
            return CommentParser._safe_int(inner.get("max_id_type"), 0)
        return 0

    @staticmethod
    def get_total_count(data: dict) -> int:
        """提取评论总数"""
        if not isinstance(data, dict):
            return 0
        inner = data.get("data", {})
        if isinstance(inner, dict):
            return CommentParser._safe_int(inner.get("total_number"), 0)
        return 0

    # ---- 内部解析逻辑 ----

    @staticmethod
    def _parse_single_comment(raw: dict, post_id: str) -> Comment:
        if not isinstance(raw, dict):
            raise ParseError(f"单条评论应为 dict，收到 {type(raw).__name__}")

        text_raw = raw.get("text", "")
        text_clean = clean_html(text_raw)

        user = User.from_api(raw.get("user", {}))

        return Comment(
            comment_id=str(raw.get("id", "")),
            post_id=post_id,
            created_at=raw.get("created_at", ""),
            text=text_clean,
            text_raw=text_raw,
            like_count=CommentParser._safe_int(raw.get("like_count"), 0),
            reply_count=CommentParser._safe_int(raw.get("total_number"), 0),
            user=user,
            source=raw.get("source", ""),
        )
