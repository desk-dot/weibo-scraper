"""
配置管理模块

认证方式：仅扫码登录（Cookie 自动缓存到 .weibo_cookie.txt，失效自动重登）。
其他来源（命令行 --cookie / 环境变量等）均已移除，保持简单。
"""

import os
from dataclasses import dataclass, asdict
from pathlib import Path

from .exceptions import ConfigError


# ============================================================
# 环境变量名（仅保留非 cookie 的运行参数）
# ============================================================
ENV_DELAY_MIN = "WEIBO_DELAY_MIN"
ENV_DELAY_MAX = "WEIBO_DELAY_MAX"
ENV_MAX_PAGES = "WEIBO_MAX_PAGES"
ENV_MAX_COUNT = "WEIBO_MAX_COUNT"

# ============================================================
# 默认值
# ============================================================
DEFAULT_DELAY_MIN = 1.0       # 最小请求间隔（秒）
DEFAULT_DELAY_MAX = 3.0       # 最大请求间隔（秒）
DEFAULT_MAX_PAGES = 0         # 0 = 不限制
DEFAULT_MAX_COUNT = 0         # 0 = 不限制
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_SUB_COMMENT_LIMIT = 30
DEFAULT_RETRY_TIMES = 3
DEFAULT_TIMEOUT = 15          # HTTP 请求超时（秒）
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)


@dataclass
class ScraperConfig:
    """爬虫配置"""

    # ---- 认证（仅扫码登录获得） ----
    cookie: str = ""

    # ---- 请求控制 ----
    delay_min: float = DEFAULT_DELAY_MIN
    delay_max: float = DEFAULT_DELAY_MAX
    max_pages: int = DEFAULT_MAX_PAGES         # 0 = 不限
    max_count: int = DEFAULT_MAX_COUNT         # 0 = 不限
    retry_times: int = DEFAULT_RETRY_TIMES
    timeout: int = DEFAULT_TIMEOUT
    user_agent: str = DEFAULT_USER_AGENT

    # ---- 数据获取 ----
    fetch_sub_comments: bool = False
    sub_comment_limit: int = DEFAULT_SUB_COMMENT_LIMIT

    # ---- 输出（固定 CSV） ----
    output_dir: str = DEFAULT_OUTPUT_DIR

    # ---- 元数据 ----
    post_id: str = ""
    post_url: str = ""

    def __post_init__(self):
        """校验配置合法性"""
        if self.delay_min < 0:
            raise ConfigError("delay_min 不能为负数")
        if self.delay_max < self.delay_min:
            self.delay_max = self.delay_min

    # ---------------------------------------------------------------
    # 工厂方法
    # ---------------------------------------------------------------

    @classmethod
    def from_env(cls, **overrides) -> "ScraperConfig":
        """从环境变量加载，overrides 可覆盖任意字段"""
        kwargs: dict = {}

        for key, env_var in [
            ("delay_min", ENV_DELAY_MIN),
            ("delay_max", ENV_DELAY_MAX),
            ("max_pages", ENV_MAX_PAGES),
            ("max_count", ENV_MAX_COUNT),
        ]:
            val = os.getenv(env_var, "")
            if val:
                try:
                    kwargs[key] = float(val) if "delay" in key else int(val)
                except ValueError:
                    pass  # 保留默认值

        kwargs.update(overrides)
        return cls(**kwargs)

    # ---------------------------------------------------------------
    # 工具方法
    # ---------------------------------------------------------------

    def to_dict(self) -> dict:
        """转为字典（隐藏敏感字段）"""
        d = asdict(self)
        if d.get("cookie"):
            d["cookie"] = d["cookie"][:20] + "..." if len(d["cookie"]) > 20 else "***"
        return d

    def ensure_output_dir(self) -> Path:
        """确保输出目录存在，返回 Path"""
        p = Path(self.output_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def has_cookie(self) -> bool:
        return bool(self.cookie and self.cookie.strip())
