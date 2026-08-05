"""
微博评论爬虫 - Weibo Comment Scraper
====================================
基于微博移动端 API 的评论爬取工具，支持单帖子和批量爬取，
仅导出为 CSV 格式。

Usage:
    python main.py <weibo_url_or_id> [options]
"""

__version__ = "1.1.0"
__author__ = "Weibo Scraper"

from .config import ScraperConfig
from .client import WeiboClient
from .parser import CommentParser
from .storage import DataStorage, save_comments

__all__ = [
    "ScraperConfig",
    "WeiboClient",
    "CommentParser",
    "DataStorage",
    "save_comments",
]
