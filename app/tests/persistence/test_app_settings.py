from backend.persistence import app_settings


def test_get_browser_path_defaults_to_none(workspace_root) -> None:
    assert app_settings.get_browser_path() is None


def test_set_and_get_browser_path_round_trips(workspace_root) -> None:
    app_settings.set_browser_path(r"C:\browsers\firefox.exe")

    assert app_settings.get_browser_path() == r"C:\browsers\firefox.exe"


def test_set_browser_path_none_clears_it(workspace_root) -> None:
    app_settings.set_browser_path(r"C:\a.exe")
    app_settings.set_browser_path(None)

    assert app_settings.get_browser_path() is None


def test_load_settings_survives_missing_file(workspace_root) -> None:
    assert app_settings.load_settings() == {}


def test_get_target_fuzz_defaults_defaults_to_none(workspace_root) -> None:
    assert app_settings.get_target_fuzz_defaults("t1") is None


def test_set_and_get_target_fuzz_defaults_round_trips(workspace_root) -> None:
    defaults = {"extensions": ["php", "html"], "threads": 20, "headers": {"X-Api-Key": "abc"}}

    app_settings.set_target_fuzz_defaults("t1", defaults)

    assert app_settings.get_target_fuzz_defaults("t1") == defaults


def test_target_fuzz_defaults_are_isolated_per_target(workspace_root) -> None:
    app_settings.set_target_fuzz_defaults("t1", {"threads": 5})
    app_settings.set_target_fuzz_defaults("t2", {"threads": 50})

    assert app_settings.get_target_fuzz_defaults("t1") == {"threads": 5}
    assert app_settings.get_target_fuzz_defaults("t2") == {"threads": 50}


def test_get_custom_port_defaults_to_none(workspace_root) -> None:
    assert app_settings.get_custom_port() is None


def test_set_and_get_custom_port_round_trips(workspace_root) -> None:
    app_settings.set_custom_port(9999)

    assert app_settings.get_custom_port() == 9999


def test_set_custom_port_none_clears_it(workspace_root) -> None:
    app_settings.set_custom_port(9999)
    app_settings.set_custom_port(None)

    assert app_settings.get_custom_port() is None


def test_get_default_extensions_fallback_defaults_to_none(workspace_root) -> None:
    # None (not just absent from the settings file, genuinely returned)
    # means "not configured -- leave dirsearch's own real default-extensions
    # behavior untouched", the honest out-of-the-box default.
    assert app_settings.get_default_extensions_fallback() is None


def test_set_default_extensions_fallback_empty_string_means_use_none(workspace_root) -> None:
    app_settings.set_default_extensions_fallback("")

    assert app_settings.get_default_extensions_fallback() == ""


def test_set_and_get_default_extensions_fallback_custom_list_round_trips(workspace_root) -> None:
    app_settings.set_default_extensions_fallback("bak,old,swp")

    assert app_settings.get_default_extensions_fallback() == "bak,old,swp"


def test_set_default_extensions_fallback_strips_whitespace(workspace_root) -> None:
    app_settings.set_default_extensions_fallback("  bak, old  ")

    assert app_settings.get_default_extensions_fallback() == "bak, old"


def test_set_default_extensions_fallback_none_clears_it_back_to_unconfigured(workspace_root) -> None:
    app_settings.set_default_extensions_fallback("")
    app_settings.set_default_extensions_fallback(None)

    assert app_settings.get_default_extensions_fallback() is None
