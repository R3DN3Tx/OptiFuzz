from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from backend.api.schemas import (
    BrowserSettingsRequest,
    DefaultExtensionsFallbackRequest,
    OpenUrlRequest,
    UiPrefRequest,
    OutputFilenameTemplateRequest,
    PortSettingsRequest,
    UiZoomRequest,
    WordlistsDirRequest,
)
from backend.persistence import app_settings
from backend.services import browser_service, display_service, output_naming, wordlist_service

router = APIRouter(tags=["settings"])

# Single source of truth for the version shown next to the settings gear
# (frontend/app.js) -- a plain text file at the repo root, not a Python
# constant, so bumping it (v0.0.X per small change, v0.X.0 for a larger one) never needs touching any actual code. Read fresh on every
# request (a local file, effectively free) rather than cached, so an
# edit takes effect without restarting the app.
_VERSION_FILE = Path(__file__).resolve().parent.parent.parent / "VERSION"


@router.get("/api/version")
def get_version() -> dict:
    try:
        version = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        version = "0.0.0"
    return {"version": version}


def _output_filename_template_payload() -> dict:
    template = app_settings.get_output_filename_template()
    example = output_naming.build_output_stem(
        template, target_name="api.example.com", url="http://api.example.com:8080/", path="/admin/subfolder"
    )
    return {
        "template": template,  # the one in effect
        "configured": app_settings.get_configured_output_filename_template(),  # None = the default
        "default_template": app_settings.DEFAULT_OUTPUT_FILENAME_TEMPLATE,
        "example": f"{example}_<job id>.txt",
    }


@router.get("/api/settings/output-filename-template")
def get_output_filename_template() -> dict:
    return _output_filename_template_payload()


@router.put("/api/settings/output-filename-template")
def set_output_filename_template(body: OutputFilenameTemplateRequest) -> dict:
    # An empty template goes back to the default.
    app_settings.set_output_filename_template(body.template)
    return _output_filename_template_payload()


@router.get("/api/settings/browser")
def get_browser_settings() -> dict:
    configured = app_settings.get_browser_path()
    return {
        "configured_path": configured,
        "effective_path": configured or browser_service.default_browser_path(),
        "detected": browser_service.detect_installed_browsers(),
    }


@router.put("/api/settings/browser")
def set_browser_settings(body: BrowserSettingsRequest) -> dict:
    if body.browser_path:
        reason = browser_service.validate_browser_executable(body.browser_path)
        if reason:
            raise HTTPException(status_code=400, detail=reason)
    app_settings.set_browser_path(body.browser_path)
    return {"configured_path": app_settings.get_browser_path()}


@router.get("/api/settings/port")
def get_port_settings() -> dict:
    return {
        "configured_port": app_settings.get_custom_port(),
        "default_port": app_settings.DEFAULT_PORT,
    }


@router.put("/api/settings/port")
def set_port_settings(body: PortSettingsRequest) -> dict:
    # Only ever takes effect on the NEXT desktop-app launch -- this request
    # is served by an instance already bound to its own port, which nothing
    # here can rebind live. The frontend's own save confirmation says so.
    app_settings.set_custom_port(body.port)
    return {
        "configured_port": app_settings.get_custom_port(),
        "default_port": app_settings.DEFAULT_PORT,
    }


def _ui_zoom_payload() -> dict:
    return {
        "supported": display_service.zoom_supported(),
        "configured_percent": app_settings.get_ui_zoom_percent(),
        "min_percent": display_service.MIN_ZOOM_PERCENT,
        "max_percent": display_service.MAX_ZOOM_PERCENT,
    }


@router.get("/api/settings/ui-zoom")
def get_ui_zoom_settings() -> dict:
    return _ui_zoom_payload()


@router.put("/api/settings/ui-zoom")
def set_ui_zoom_settings(body: UiZoomRequest) -> dict:
    try:
        display_service.set_zoom_percent(body.percent)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ui_zoom_payload()


@router.get("/api/settings/default-extensions-fallback")
def get_default_extensions_fallback_settings() -> dict:
    return {
        "configured_value": app_settings.get_default_extensions_fallback(),
        "dirsearch_default": app_settings.DIRSEARCH_DEFAULT_EXTENSIONS,
    }


@router.put("/api/settings/default-extensions-fallback")
def set_default_extensions_fallback_settings(body: DefaultExtensionsFallbackRequest) -> dict:
    app_settings.set_default_extensions_fallback(body.value)
    return {
        "configured_value": app_settings.get_default_extensions_fallback(),
        "dirsearch_default": app_settings.DIRSEARCH_DEFAULT_EXTENSIONS,
    }


def _wordlists_dir_payload() -> dict:
    return {
        "configured_dir": app_settings.get_wordlists_dir(),
        "default_dir": str(wordlist_service.WORDLISTS_DIR),
    }


@router.get("/api/settings/wordlists-dir")
def get_wordlists_dir_settings() -> dict:
    return _wordlists_dir_payload()


@router.put("/api/settings/wordlists-dir")
def set_wordlists_dir_settings(body: WordlistsDirRequest) -> dict:
    try:
        wordlist_service.set_wordlists_dir(body.dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _wordlists_dir_payload()


@router.get("/api/settings/ui-prefs")
def get_ui_prefs() -> dict:
    return app_settings.get_ui_prefs()


@router.put("/api/settings/ui-prefs")
def set_ui_pref(body: UiPrefRequest) -> dict:
    app_settings.set_ui_pref(body.key, body.value)
    return {"ok": True}


@router.post("/api/open-url")
def open_url(body: OpenUrlRequest) -> dict:
    if not (body.url.startswith("http://") or body.url.startswith("https://")):
        raise HTTPException(status_code=400, detail="Only http(s) URLs can be opened")
    browser_path = app_settings.get_browser_path() or browser_service.default_browser_path()
    opened = browser_service.open_url(body.url, browser_path)
    if not opened:
        raise HTTPException(
            status_code=400,
            detail="No browser could be launched. Open Settings (⚙, top right) -> "
            "Browser for opening results, and configure one.",
        )
    return {"opened": body.url}
