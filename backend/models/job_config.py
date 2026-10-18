from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DirsearchJobConfig:
    """One dirsearch invocation's worth of options.

    Covers the Basic + common Advanced fields from the New Fuzz form.
    Extended as later phases add more of the
    filter/matcher/connection option families.
    """

    url: str
    extensions: tuple[str, ...] = ()
    force_extensions: bool = False
    threads: int = 5
    http_method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    cookie: str | None = None
    user_agent: str | None = None
    follow_redirects: bool = False
    include_status_codes: tuple[str, ...] = ()
    exclude_status_codes: tuple[str, ...] = ()
    timeout: float | None = None
    proxy: str | None = None
    max_rate: int | None = None
    delay: float | None = None
    # Raw extra dirsearch CLI flags not covered by a dedicated field above
    # (spec: don't gate the tool behind only the options this form happens
    # to expose). Appended as-is to argv, after everything else -- still
    # never shell-interpreted (subprocess.Popen always gets a list), so this
    # is exactly as safe as any other argv element, just user-controlled.
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("DirsearchJobConfig.url is required")
        if self.threads < 1:
            raise ValueError("DirsearchJobConfig.threads must be >= 1")


def config_to_dict(config: DirsearchJobConfig) -> dict[str, Any]:
    return {
        "url": config.url,
        "extensions": list(config.extensions),
        "force_extensions": config.force_extensions,
        "threads": config.threads,
        "http_method": config.http_method,
        "headers": dict(config.headers),
        "cookie": config.cookie,
        "user_agent": config.user_agent,
        "follow_redirects": config.follow_redirects,
        "include_status_codes": list(config.include_status_codes),
        "exclude_status_codes": list(config.exclude_status_codes),
        "timeout": config.timeout,
        "proxy": config.proxy,
        "max_rate": config.max_rate,
        "delay": config.delay,
        "extra_args": list(config.extra_args),
    }


def config_from_dict(data: dict[str, Any]) -> DirsearchJobConfig:
    return DirsearchJobConfig(
        url=data["url"],
        extensions=tuple(data.get("extensions", ())),
        force_extensions=data.get("force_extensions", False),
        threads=data.get("threads", 10),
        http_method=data.get("http_method", "GET"),
        headers=dict(data.get("headers", {})),
        cookie=data.get("cookie"),
        user_agent=data.get("user_agent"),
        follow_redirects=data.get("follow_redirects", False),
        include_status_codes=tuple(data.get("include_status_codes", ())),
        exclude_status_codes=tuple(data.get("exclude_status_codes", ())),
        timeout=data.get("timeout"),
        proxy=data.get("proxy"),
        max_rate=data.get("max_rate"),
        delay=data.get("delay"),
        extra_args=tuple(data.get("extra_args", ())),
    )
