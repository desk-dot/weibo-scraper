"""
自定义异常类
"""


class WeiboScraperError(Exception):
    """基础异常类"""
    pass


class ConfigError(WeiboScraperError):
    """配置错误 —— 缺少必填项或格式不正确"""
    pass


class CookieExpiredError(WeiboScraperError):
    """Cookie 已过期或无效，需要重新获取"""
    pass


class RequestBlockedError(WeiboScraperError):
    """请求被微博反爬机制拦截"""
    pass


class RateLimitError(WeiboScraperError):
    """触发频率限制"""
    pass


class LoginRedirectError(WeiboScraperError):
    """
    API 返回 ok=-100（重定向到登录页）。

    会话期间出现该错误通常是软封锁/限流所致，可退避重试；
    若持续出现则说明 Cookie 失效，需要重新扫码登录。
    """
    pass


class ParseError(WeiboScraperError):
    """响应解析失败"""
    pass


class PostNotFoundError(WeiboScraperError):
    """帖子不存在或已被删除"""
    pass
