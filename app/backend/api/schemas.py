from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator


class CreateProjectRequest(BaseModel):
    name: str
    notes: str = ""


class RenameRequest(BaseModel):
    name: str


class RepairRequest(BaseModel):
    kind: str
    project_id: str
    target_id: str = ""
    job_id: str = ""
    action: str
    base_url: str | None = None


class SetOutputsMirrorDirRequest(BaseModel):
    dir: str


class CreateTargetRequest(BaseModel):
    name: str
    base_url: str


MAX_THREADS = 500  # each thread costs memory; dirsearch is normally run with 10-100
MAX_CONCURRENT_JOBS = 10  # per target: every running job is its own dirsearch with its own threads

_CONTROL_CHARS = re.compile("[" + "".join(chr(c) for c in [*range(0, 9), *range(10, 32), 127]) + "]")  # every control character except a tab
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_STATUS_CODE = re.compile(r"^[1-5]\d{2}(-[1-5]\d{2})?$")
_PROXY = re.compile(r"^(?:(?:https?|socks4|socks5)://)?[^\s/:@]+(?::[^\s/@]*@[^\s/:@]+)?:\d{1,5}/?$")
_METHOD = re.compile(r"^[A-Z][A-Z-]{0,19}$")


def _no_control(value: str, what: str) -> str:
    if _CONTROL_CHARS.search(value):
        raise ValueError(f"{what} cannot contain line breaks or other control characters")
    return value


class _JobOptions(BaseModel):
    """The options of a fuzz, shared by "start a fuzz" and "save this target's defaults"."""

    extensions: list[str] = Field(default_factory=list)
    force_extensions: bool = False
    threads: int = Field(default=5, ge=1, le=MAX_THREADS)
    http_method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    cookie: str | None = None
    user_agent: str | None = None
    follow_redirects: bool = False
    include_status_codes: list[str] = Field(default_factory=list)
    exclude_status_codes: list[str] = Field(default_factory=list)
    timeout: float | None = Field(default=None, gt=0, le=3600)
    proxy: str | None = None
    max_rate: int | None = Field(default=None, ge=0, le=1_000_000)
    delay: float | None = Field(default=None, ge=0, le=3600)
    pause_on_status: list[int] = Field(default_factory=list)
    # Raw text, shell-style quoting (e.g. --header-file "my file.txt"),
    # split server-side with shlex.split() -- see api/jobs.py.
    extra_args: str = Field(default="", max_length=4000)
    # dirsearch's own default headers this job must not send (e.g. "keep-alive").
    disabled_default_headers: list[str] = Field(default_factory=list)
    authenticated: bool = False

    @field_validator("extensions")
    @classmethod
    def _check_extensions(cls, value: list[str]) -> list[str]:
        for item in value:
            if len(item) > 30 or re.search(r"[\s/\\,]", item) or _CONTROL_CHARS.search(item):
                raise ValueError(f"Not a valid extension: {item!r}")
        return value

    @field_validator("http_method")
    @classmethod
    def _check_method(cls, value: str) -> str:
        method = value.strip().upper() or "GET"
        if not _METHOD.match(method):
            raise ValueError(f"Not a valid HTTP method: {value!r}")
        return method

    @field_validator("headers")
    @classmethod
    def _check_headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name, content in value.items():
            if not _HEADER_NAME.match(name):
                raise ValueError(f"Not a valid header name: {name!r}")
            _no_control(content, f"The value of header {name}")
        return value

    @field_validator("cookie")
    @classmethod
    def _check_cookie(cls, value: str | None) -> str | None:
        return value if value is None else _no_control(value, "The cookie")

    @field_validator("user_agent")
    @classmethod
    def _check_user_agent(cls, value: str | None) -> str | None:
        return value if value is None else _no_control(value, "The User-Agent")

    @field_validator("include_status_codes", "exclude_status_codes")
    @classmethod
    def _check_status_codes(cls, value: list[str]) -> list[str]:
        for item in value:
            if not _STATUS_CODE.match(item.strip()):
                raise ValueError(f"Not a valid status code or range: {item!r} (use 404 or 200-399)")
        return value

    @field_validator("pause_on_status")
    @classmethod
    def _check_pause_codes(cls, value: list[int]) -> list[int]:
        for item in value:
            if not 100 <= item <= 599:
                raise ValueError(f"Not a valid status code: {item}")
        return value

    @field_validator("proxy")
    @classmethod
    def _check_proxy(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if not _PROXY.match(value.strip()):
            raise ValueError("The proxy must look like host:port or http://host:port (http, https, socks4 or socks5)")
        return value.strip()

    @field_validator("extra_args")
    @classmethod
    def _check_extra_args(cls, value: str) -> str:
        if chr(0) in value:
            raise ValueError("The custom options cannot contain a NUL character")
        return value


class CreateJobRequest(_JobOptions):
    path: str = Field(default="/", max_length=2000)
    wordlist_files: list[str] = Field(min_length=1, max_length=50)

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return _no_control(value, "The path")


class TargetConcurrencyRequest(BaseModel):
    max_concurrent_jobs: int = Field(ge=1, le=MAX_CONCURRENT_JOBS)


class TargetFuzzDefaultsRequest(_JobOptions):
    """Same options as a fuzz, minus `path` (contextual to where a fuzz is actually launched from, never a
    saved default). See backend.persistence.app_settings.get_target_fuzz_defaults's own docstring for why
    `wordlist_files` here is an OVERRIDE on top of the existing app-wide starred/last-used wordlist, not a
    replacement for it.
    """

    wordlist_files: list[str] = Field(default_factory=list, max_length=50)


class BrowserSettingsRequest(BaseModel):
    browser_path: str | None = None


class OpenUrlRequest(BaseModel):
    url: str


class OutputFilenameTemplateRequest(BaseModel):
    template: str


class PortSettingsRequest(BaseModel):
    # None/absent clears the override (back to the built-in default) -- the
    # same "null clears it" convention BrowserSettingsRequest.browser_path
    # already uses just above.
    port: int | None = Field(default=None, ge=1, le=65535)


class WordlistsDirRequest(BaseModel):
    # None or "" clears the override (back to the wordlists/ folder inside the program).
    dir: str | None = None


class UiZoomRequest(BaseModel):
    # None clears it (back to automatic); the range is checked by display_service.
    percent: int | None = None


class DefaultExtensionsFallbackRequest(BaseModel):
    # None clears the override (back to dirsearch's own real
    # default-extensions behavior, untouched). "" means "use none, ever". A
    # non-empty string is a custom comma-separated fallback list. See
    # app_settings.get_default_extensions_fallback's own docstring.
    value: str | None = None


class UiPrefRequest(BaseModel):
    key: str
    value: Any = None


class HideRulesRequest(BaseModel):
    rules: list[dict] = Field(default_factory=list)


class MarkSeenRequest(BaseModel):
    # Each a result's own "seen_key" (results_service.annotate_new) -- only
    # the ones actually painted on screen during this Results visit, not
    # every result that currently exists.
    keys: list[str] = Field(default_factory=list)


class AddTreePathRequest(BaseModel):
    path: str


class AddTreeResultRequest(BaseModel):
    path: str
    # Optional (not required): a synthetic ancestor segment in Results
    # (an implied path never itself discovered by dirsearch -- see
    # buildPathHierarchy() in app.js) has no real status to send when
    # "Add to tree" is used on it.
    status: int | None = None
    size: str | None = None
    job_id: str | None = None


class UpdateTreeNodeRequest(BaseModel):
    interesting: bool | None = None
    note: str | None = None
