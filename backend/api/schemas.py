from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    name: str
    notes: str = ""


class RenameRequest(BaseModel):
    name: str


class SetOutputsMirrorDirRequest(BaseModel):
    dir: str


class CreateTargetRequest(BaseModel):
    name: str
    base_url: str


class CreateJobRequest(BaseModel):
    path: str = "/"
    wordlist_files: list[str] = Field(min_length=1)
    extensions: list[str] = Field(default_factory=list)
    force_extensions: bool = False
    threads: int = 5
    http_method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    cookie: str | None = None
    user_agent: str | None = None
    follow_redirects: bool = False
    include_status_codes: list[str] = Field(default_factory=list)
    exclude_status_codes: list[str] = Field(default_factory=list)
    timeout: float | None = None
    proxy: str | None = None
    max_rate: int | None = None
    delay: float | None = None
    pause_on_status: list[int] = Field(default_factory=list)
    # Raw text, shell-style quoting (e.g. --header-file "my file.txt"),
    # split server-side with shlex.split() -- see api/jobs.py.
    extra_args: str = ""
    # Purely a saved-output-filename tag ("__AUTH" appended after the
    # built stem) -- never touches the actual dirsearch invocation/config,
    # just makes an authenticated fuzz's report visually distinguishable
    # from an unauthenticated one when browsing outputs/ directly.
    authenticated: bool = False


class TargetConcurrencyRequest(BaseModel):
    max_concurrent_jobs: int = Field(ge=1)


class TargetFuzzDefaultsRequest(BaseModel):
    """Same field set as CreateJobRequest, minus `path` (contextual to
    where a fuzz is actually launched from, never a saved default). See
    backend.persistence.app_settings.get_target_fuzz_defaults's own
    docstring for why `wordlist_files` here is an OVERRIDE on top of the
    existing app-wide starred/last-used wordlist, not a replacement for it.
    """

    wordlist_files: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    force_extensions: bool = False
    threads: int = 5
    http_method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    cookie: str | None = None
    user_agent: str | None = None
    follow_redirects: bool = False
    include_status_codes: list[str] = Field(default_factory=list)
    exclude_status_codes: list[str] = Field(default_factory=list)
    timeout: float | None = None
    proxy: str | None = None
    max_rate: int | None = None
    delay: float | None = None
    pause_on_status: list[int] = Field(default_factory=list)
    extra_args: str = ""
    authenticated: bool = False


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
