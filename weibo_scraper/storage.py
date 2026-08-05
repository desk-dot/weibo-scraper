"""
数据存储模块

固定输出 CSV（UTF-8 BOM，兼容 Excel）。提供增量写入能力，
避免长时间爬取时数据丢失。
"""

import csv
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .parser import Comment, User

_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

logger = logging.getLogger("weibo_scraper.storage")


class DataStorage:
    """评论数据持久化（CSV）"""

    def __init__(self, output_dir: str = "output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._current_path: Optional[Path] = None

    # -----------------------------------------------------------
    # 公开方法
    # -----------------------------------------------------------

    def save(
        self,
        comments: list[Comment],
        post_id: str,
        append: bool = False,
    ) -> str:
        """
        保存评论数据，返回输出文件路径。

        Parameters
        ----------
        comments : list[Comment]
            评论列表。
        post_id : str
            帖子 ID，用于生成文件名。
        append : bool
            是否追加写入。首调用应传 False，后续增量传 True。
        """
        # 生成或复用文件路径
        if append and self._current_path is not None:
            filepath = self._current_path
        else:
            filepath = self._make_path(post_id)
            self._current_path = filepath

        mode = "a" if (append and filepath.exists()) else "w"

        with open(filepath, mode, newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)

            # 新文件写表头
            if mode == "w":
                writer.writerow(self._csv_header())

            for comment in comments:
                self._write_comment_row(writer, comment)

        logger.info(
            "%s %d 条评论 → %s",
            "追加" if mode == "a" else "写入",
            len(comments),
            filepath,
        )
        return str(filepath)

    def flush(self, comments: list[Comment], post_id: str) -> str:
        """增量保存（追加），等价于 save(..., append=True)"""
        return self.save(comments, post_id, append=True)

    # -----------------------------------------------------------
    # CSV
    # -----------------------------------------------------------

    @staticmethod
    def _csv_header() -> list[str]:
        return [
            "comment_id",
            "post_id",
            "parent_comment_id",
            "is_sub",
            "created_at",
            "text",
            "like_count",
            "reply_count",
            "source",
            "user_id",
            "user_name",
            "user_verified",
            "user_profile_url",
            "user_avatar_url",
        ]

    def _write_comment_row(self, writer, comment: Comment):
        user = comment.user or User()
        writer.writerow([
            comment.comment_id,
            comment.post_id,
            comment.parent_comment_id,
            int(comment.is_sub),
            comment.created_at,
            comment.text,
            comment.like_count,
            comment.reply_count,
            comment.source,
            user.user_id,
            user.screen_name,
            int(user.verified),
            user.profile_url,
            user.avatar_url,
        ])

        # 递归写入子评论（如果已抓取）
        for sub in comment.sub_comments:
            self._write_comment_row(writer, sub)

    # -----------------------------------------------------------
    # 辅助
    # -----------------------------------------------------------

    def _make_path(self, post_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9_\-]", "_", post_id)
        if len(safe_id) > 64:
            safe_id = safe_id[:64]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"weibo_{safe_id}_{ts}.csv"
        path = self.output_dir / filename
        try:
            path.resolve().relative_to(self.output_dir.resolve())
        except ValueError:
            raise ValueError(f"非法的文件路径: {path}")
        return path


# ============================================================
# 便捷函数
# ============================================================


def save_comments(
    comments: list[Comment],
    post_id: str,
    output_dir: str = "output",
    append: bool = False,
) -> str:
    """一次性保存评论的便捷函数"""
    storage = DataStorage(output_dir=output_dir)
    return storage.save(comments, post_id, append=append)
