#!/usr/bin/env python3
"""
微博评论爬虫 - CLI 入口（简化版）

认证方式：仅扫码登录。首次运行扫码，Cookie 自动缓存到
.weibo_cookie.txt；之后自动复用，失效时自动重新扫码。

输出格式：固定 CSV。

Usage:
    # 首次：扫码登录并爬取
    python main.py https://m.weibo.cn/detail/1234567890

    # 之后：自动复用已登录的 Cookie
    python main.py 1234567890

    # 强制重新扫码登录
    python main.py 1234567890 --login

    # 同时抓取子评论
    python main.py 1234567890 --sub-comments --max-pages 50
"""

import argparse
import logging
import re
import sys
from datetime import datetime
from typing import Optional

logger = logging.getLogger("weibo_scraper.cli")

from weibo_scraper.config import ScraperConfig
from weibo_scraper.client import WeiboClient
from weibo_scraper.parser import CommentParser
from weibo_scraper.storage import DataStorage
from weibo_scraper.login import LoginManager, LoginError
from weibo_scraper.exceptions import (
    WeiboScraperError,
    CookieExpiredError,
    RequestBlockedError,
    PostNotFoundError,
    ConfigError,
)

# 扫码登录后 Cookie 的缓存路径
COOKIE_CACHE = ".weibo_cookie.txt"

# ============================================================
# 日志
# ============================================================


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")


# ============================================================
# URL / ID 解析
# ============================================================

# 匹配微博帖子 URL 中的 ID
_RE_WEIBO_URL = re.compile(
    r"(?:weibo\.(?:com|cn)|m\.weibo\.cn)"
    r"/(?:detail|status|statuses/show)/([A-Za-z0-9]+)"
)

# 纯数字 ID
_RE_NUMERIC_ID = re.compile(r"^\d{10,}$")


def extract_post_id(raw: str) -> Optional[str]:
    """从 URL 或原始输入中提取帖子 ID"""
    raw = raw.strip()
    # 尝试匹配 URL
    m = _RE_WEIBO_URL.search(raw)
    if m:
        return m.group(1)
    # 尝试匹配纯数字 ID
    if _RE_NUMERIC_ID.match(raw):
        return raw
    # 尝试从查询参数中提取
    if "id=" in raw:
        m = re.search(r"id=(\d{10,})", raw)
        if m:
            return m.group(1)
    return None


# ============================================================
# 认证：仅扫码登录（自动缓存 + 失效重登）
# ============================================================


def resolve_cookie(args) -> str:
    """
    获取可用的登录 Cookie。必须在抓取前完成登录，
    以确保所有评论（包括第一页）都能完整获取。
    """
    cookie = LoginManager.load_cookie(COOKIE_CACHE) or ""
    need_login = (
        args.login
        or not cookie
        or not LoginManager.is_cookie_valid(cookie)
    )

    if not need_login:
        print("  ✓ 复用已登录的缓存 Cookie")
        return cookie

    # ---- 强制扫码登录 ----
    if cookie and not LoginManager.is_cookie_valid(cookie):
        print("  ⚠ 缓存 Cookie 已失效，需要重新登录")

    print("=" * 60)
    print("  微博扫码登录")
    print("=" * 60)
    try:
        login_mgr = LoginManager()
        cookie = login_mgr.login(qr_display=args.qr_display)
        LoginManager.save_cookie(cookie, COOKIE_CACHE)
        print(f"  Cookie 已保存到: {COOKIE_CACHE}")
    except LoginError as e:
        print(f"\n❌ 扫码登录失败: {e}")
        sys.exit(2)

    if not LoginManager.is_cookie_valid(cookie):
        print("\n❌ 登录后 Cookie 仍未生效（可能 SSO 未落到 m.weibo.cn 域）。")
        print("   请重新运行（将再次弹出二维码）。")
        sys.exit(2)

    print("  ✓ 登录成功")
    print("=" * 60 + "\n")
    return cookie


# ============================================================
# 核心爬取逻辑
# ============================================================


class WeiboCommentScraper:
    """微博评论爬虫 - 编排器"""

    def __init__(self, config: ScraperConfig):
        self.config = config
        self.client = WeiboClient(config)
        self.parser = CommentParser()
        self.storage = DataStorage(output_dir=config.output_dir)

        # 统计
        self.total_fetched = 0
        self.total_pages = 0
        self.start_time: Optional[datetime] = None

    def run(self) -> str:
        """执行爬取，返回输出文件路径。Cookie 失效时自动重新登录并从头开始。"""
        post_id = self.config.post_id
        self.start_time = datetime.now()

        print(f"\n{'='*60}")
        print(f"  微博评论爬虫 v1.1")
        print(f"  帖子 ID : {post_id}")
        print(f"  子评论  : {'是' if self.config.fetch_sub_comments else '否'}")
        if self.config.max_count > 0:
            print(f"  目标条数: {self.config.max_count} 条")
        else:
            print(f"  目标条数: 不限")
        print(f"  输出目录: {self.config.output_dir}")
        print(f"{'='*60}\n")

        # ---- Step 1: 校验帖子是否存在 ----
        print(f"[1/4] 校验帖子...", end=" ")
        try:
            detail = self.client.fetch_post_detail(post_id)
        except WeiboScraperError:
            # 有些帖子接口不可用但评论接口正常，不阻塞
            print("跳过（部分接口受限）")
        else:
            if detail.get("ok") == 0:
                raise PostNotFoundError(
                    f"帖子不存在或已被删除 (id={post_id})"
                )
            print("OK")

        # ---- Step 2: 获取第一页，拿到总数 ----
        # 如果 Cookie 失效，重新登录并从头开始
        comments = []
        total = 0
        max_id = 0
        max_id_type = 0
        self.total_pages = 0
        self.total_fetched = 0

        # 尝试最多 2 次（首次 + Cookie 失效重登）
        for attempt in range(2):
            try:
                print(f"[2/4] 获取评论第 1 页... (尝试 {attempt + 1}/2)")
                first_page = self.client.fetch_comments(
                    post_id, max_id=0, max_id_type=0,
                )
                comments = self.parser.parse_comments(first_page, post_id)
                total = self.parser.get_total_count(first_page)
                max_id = self.parser.get_max_id(first_page)
                max_id_type = self.parser.get_max_id_type(first_page)
                self.total_pages = 1
                self.total_fetched = len(comments)
                break
            except CookieExpiredError:
                if attempt == 0:
                    print("  ⚠ Cookie 失效，需要重新登录")
                    cookie = self._relogin()
                    self.config.cookie = cookie
                    self.client = WeiboClient(self.config)
                    self.storage = DataStorage(output_dir=self.config.output_dir)
                    continue  # 重试
                else:
                    raise  # 第二次仍然失败，抛出异常

        if not comments:
            raise WeiboScraperError("无法获取第一页评论，请检查网络或 Cookie")

        # 第 1 页立即落盘，避免后续分页失败时丢失最早的数据
        self.storage.flush(comments, post_id)
        print(f"  OK | 总数约 {total} 条 | 本页 {len(comments)} 条")

        # ---- Step 3: 分页循环（第 2 页起） ----
        print(f"[3/4] 分页抓取...")

        reached_count = False
        if self.config.max_count > 0 and self.total_fetched >= self.config.max_count:
            comments = comments[:self.config.max_count]
            self.total_fetched = len(comments)
            print(f"  已达到目标条数 ({self.config.max_count})，停止抓取")
            reached_count = True

        pagination_failed = False
        cookie_relogged = False
        while not reached_count and max_id != 0:
            if self.config.max_pages > 0 and self.total_pages >= self.config.max_pages:
                print(f"  已达到最大页数限制 ({self.config.max_pages})")
                break
            if self.config.max_count > 0 and self.total_fetched >= self.config.max_count:
                print(f"  已达到目标条数 ({self.config.max_count})，停止抓取")
                break

            try:
                page_data = self.client.fetch_comments(
                    post_id, max_id=max_id, max_id_type=max_id_type,
                )
            except CookieExpiredError as e:
                if not cookie_relogged:
                    print(f"  ⚠ Cookie 失效，尝试重新登录...")
                    cookie = self._relogin()
                    self.config.cookie = cookie
                    self.client = WeiboClient(self.config)
                    self.storage = DataStorage(output_dir=self.config.output_dir)
                    cookie_relogged = True
                    # 重新登录后从头开始抓取
                    print("  重新登录成功，从头开始抓取...")
                    try:
                        first_page = self.client.fetch_comments(
                            post_id, max_id=0, max_id_type=0,
                        )
                        comments = self.parser.parse_comments(first_page, post_id)
                        total = self.parser.get_total_count(first_page)
                        max_id = self.parser.get_max_id(first_page)
                        max_id_type = self.parser.get_max_id_type(first_page)
                        self.total_pages = 1
                        self.total_fetched = len(comments)
                        self.storage.flush(comments, post_id)
                        print(f"  ✓ 第 1 页重新抓取成功 | 本页 {len(comments)} 条")
                        continue  # 继续分页
                    except WeiboScraperError as re_err:
                        print(f"  重新登录后抓取失败：{re_err}")
                        pagination_failed = True
                        break
                else:
                    print(f"  ⚠ 重新登录后仍被拦截：{e}")
                    print(f"    已抓取 {self.total_fetched} 条，继续保存已得数据。")
                    pagination_failed = True
                    break
            except RequestBlockedError as e:
                print(f"  ⚠ 请求被拦截：{e}")
                print(f"    已抓取 {self.total_fetched} 条，继续保存已得数据。")
                pagination_failed = True
                break

            page_comments = self.parser.parse_comments(page_data, post_id)
            max_id = self.parser.get_max_id(page_data)
            max_id_type = self.parser.get_max_id_type(page_data)
            self.total_pages += 1
            self.total_fetched += len(page_comments)
            comments.extend(page_comments)

            # 每页增量落盘，防止中断丢失
            self.storage.flush(page_comments, post_id)

            print(
                f"  第 {self.total_pages} 页 | "
                f"本页 {len(page_comments)} 条 | "
                f"累计 {self.total_fetched} 条 | "
                f"max_id={max_id} max_id_type={max_id_type}"
            )

            # 达标后截断
            if self.config.max_count > 0 and self.total_fetched >= self.config.max_count:
                if self.total_fetched > self.config.max_count:
                    comments = comments[:self.config.max_count]
                    self.total_fetched = self.config.max_count
                    print(f"  已达到目标条数 ({self.config.max_count})，停止抓取")
                break

            # max_id 未变或为 0，说明没有下一页
            if max_id == 0:
                break

        # ---- Step 4: 子评论（可选，始终可达） ----
        if self.config.fetch_sub_comments and comments:
            print(f"[4/4] 抓取子评论...")
            self._fetch_sub_comments(comments, post_id)
        else:
            print(f"[4/4] 跳过子评论")

        # ---- 收尾：全量重写（含子评论） ----
        elapsed = (datetime.now() - self.start_time).total_seconds()
        output_path = self.storage.save(comments, post_id, append=False)

        print(f"\n{'='*60}")
        print(f"  ✅ 爬取完成!")
        print(f"  总评论数 : {self.total_fetched} 条")
        print(f"  总页数   : {self.total_pages} 页")
        print(f"  总请求数 : {self.client.total_requests} 次")
        if pagination_failed:
            print(f"  注意     : 分页中途被拦截，以上为已成功抓取的部分")
        print(f"  耗时     : {elapsed:.1f} 秒")
        print(f"  输出文件 : {output_path}")
        print(f"{'='*60}\n")

        return output_path

    def _fetch_sub_comments(self, comments: list, post_id: str):
        """为评论抓取子评论（支持分页）"""
        sub_total = 0
        limit = self.config.sub_comment_limit
        for i, comment in enumerate(comments):
            if comment.reply_count <= 0:
                continue

            try:
                all_subs = []
                max_id = 0
                max_id_type = 0
                # 一级评论的根评论ID就是它自己
                root_comment_id = comment.comment_id
                while True:
                    if limit > 0 and len(all_subs) >= limit:
                        break
                    sub_data = self.client.fetch_sub_comments(
                        comment.comment_id,
                        post_id=post_id,
                        root_comment_id=root_comment_id,
                        max_id=max_id,
                        max_id_type=max_id_type,
                        limit=min(limit - len(all_subs), 30) if limit > 0 else 30,
                    )
                    subs = self.parser.parse_sub_comments(
                        sub_data, post_id, comment.comment_id,
                    )
                    if not subs:
                        break
                    all_subs.extend(subs)

                    next_max_id = self.parser.get_max_id(sub_data)
                    next_max_id_type = self.parser.get_max_id_type(sub_data)
                    if next_max_id == 0 or next_max_id == max_id:
                        break
                    max_id = next_max_id
                    max_id_type = next_max_id_type

                if limit > 0 and len(all_subs) > limit:
                    all_subs = all_subs[:limit]
                comment.sub_comments = all_subs
                sub_total += len(all_subs)

                if (i + 1) % 20 == 0 or i == len(comments) - 1:
                    print(
                        f"  子评论进度: {i+1}/{len(comments)} "
                        f"（已抓取 {sub_total} 条）"
                    )
            except WeiboScraperError as e:
                logger.warning("子评论抓取失败 (comment_id=%s): %s", comment.comment_id, e)
                continue

        if sub_total > 0:
            print(f"  共抓取 {sub_total} 条子评论")

    def _relogin(self) -> str:
        """重新扫码登录并返回新 Cookie"""
        print("\n" + "=" * 60)
        print("  Cookie 失效，需要重新登录")
        print("=" * 60)
        try:
            login_mgr = LoginManager()
            cookie = login_mgr.login(qr_display="auto")
            LoginManager.save_cookie(cookie, COOKIE_CACHE)
            print(f"  ✓ 新 Cookie 已保存")
        except LoginError as e:
            raise WeiboScraperError(f"重新登录失败: {e}")

        if not LoginManager.is_cookie_valid(cookie):
            raise WeiboScraperError("重新登录后 Cookie 仍未生效")

        print("  ✓ 登录成功，将从头开始抓取")
        print("=" * 60 + "\n")
        return cookie

    def close(self):
        self.client.close()


# ============================================================
# CLI 参数解析
# ============================================================


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="weibo-scraper",
        description="微博评论爬虫 - 基于移动端 API 的评论数据采集工具（仅扫码登录 / 仅 CSV）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py 4933912829003094               # 首次扫码登录，之后自动复用
  python main.py https://m.weibo.cn/detail/4933912829003094
  python main.py 4933912829003094 --login        # 强制重新扫码登录
  python main.py 4933912829003094 --sub-comments # 同时抓取子评论
        """,
    )

    # ---- 位置参数 ----
    p.add_argument(
        "target",
        help="微博帖子 URL 或纯数字 ID",
    )

    # ---- 认证（仅扫码登录） ----
    auth = p.add_argument_group("认证")
    auth.add_argument(
        "--login",
        action="store_true",
        default=False,
        help="强制重新扫码登录（忽略已缓存的 Cookie）",
    )
    auth.add_argument(
        "--qr-display",
        choices=["auto", "window", "ascii", "url"],
        default="auto",
        help="二维码显示方式: auto=优先弹窗, window=GUI 弹窗, ascii=终端, url=只打印 URL (默认: auto)",
    )

    # ---- 输出 ----
    output = p.add_argument_group("输出控制")
    output.add_argument(
        "-o", "--output-dir",
        default="output",
        help="输出目录 (默认: output)",
    )

    # ---- 抓取控制 ----
    fetch = p.add_argument_group("抓取控制")
    fetch.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="最大抓取页数，0=不限制 (默认: 0)",
    )
    fetch.add_argument(
        "--max-count",
        type=int,
        default=0,
        help="最大抓取评论条数，0=不限制 (默认: 0)。达到条数后立即停止",
    )
    fetch.add_argument(
        "--sub-comments",
        action="store_true",
        default=False,
        help="同时抓取子评论（评论的回复）",
    )
    fetch.add_argument(
        "--sub-comment-limit",
        type=int,
        default=30,
        help="每条评论最多抓取的子评论数 (默认: 30)",
    )

    # ---- 速率控制 ----
    rate = p.add_argument_group("速率控制")
    rate.add_argument(
        "--delay-min",
        type=float,
        default=1.0,
        help="最小请求间隔/秒 (默认: 1.0)",
    )
    rate.add_argument(
        "--delay-max",
        type=float,
        default=3.0,
        help="最大请求间隔/秒 (默认: 3.0)",
    )
    rate.add_argument(
        "--retry",
        type=int,
        default=3,
        help="失败重试次数 (默认: 3)",
    )
    rate.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="请求超时/秒 (默认: 15)",
    )

    # ---- 其他 ----
    other = p.add_argument_group("其他")
    other.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="详细日志输出",
    )
    other.add_argument(
        "--version",
        action="version",
        version="weibo-scraper 1.1.0",
    )

    return p


# ============================================================
# main
# ============================================================


def main(argv: Optional[list[str]] = None):
    parser = build_argparser()
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    # ---- 提取帖子 ID ----
    post_id = extract_post_id(args.target)
    if not post_id:
        print(f"❌ 无法从输入中提取帖子 ID: {args.target}")
        print("   请提供完整的帖子 URL 或 16 位数字 ID")
        sys.exit(1)

    # ---- 认证：仅扫码登录 ----
    cookie = resolve_cookie(args)

    # ---- 构建配置 ----
    try:
        config = ScraperConfig.from_env(
            cookie=cookie,
            post_id=post_id,
            post_url=args.target if "http" in args.target else "",
            output_dir=args.output_dir,
            max_pages=args.max_pages,
            max_count=args.max_count,
            fetch_sub_comments=args.sub_comments,
            sub_comment_limit=args.sub_comment_limit,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            retry_times=args.retry,
            timeout=args.timeout,
        )
    except ConfigError as e:
        print(f"❌ 配置错误: {e}")
        sys.exit(1)

    # ---- 执行 ----
    scraper = WeiboCommentScraper(config)
    try:
        scraper.run()
    except CookieExpiredError as e:
        print(f"\n❌ Cookie 问题: {e}")
        sys.exit(2)
    except PostNotFoundError as e:
        print(f"\n❌ 帖子问题: {e}")
        sys.exit(3)
    except RequestBlockedError as e:
        print(f"\n❌ 被拦截: {e}")
        sys.exit(4)
    except WeiboScraperError as e:
        print(f"\n❌ 爬取出错: {e}")
        sys.exit(5)
    except KeyboardInterrupt:
        print(f"\n\n⚠️  用户中断。")
        print(f"   已抓取 {scraper.total_fetched} 条评论（{scraper.total_pages} 页）")
        print(f"   数据已保存到 {config.output_dir}/ 目录")
        sys.exit(130)
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
