import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def test_every_bundled_font_referenced_by_the_css_exists() -> None:
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    urls = re.findall(r'url\("/static/(fonts/[^"]+)"\)', css)

    assert urls, "styles.css should declare the bundled @font-face fonts"
    for relative in urls:
        font = FRONTEND / relative
        assert font.is_file(), f"{relative} is referenced by styles.css but missing"
        assert font.stat().st_size > 10_000, f"{relative} looks truncated"


def test_bundled_fonts_ship_with_their_license_notice() -> None:
    notice = (FRONTEND / "fonts" / "README.txt").read_text(encoding="utf-8")

    assert "SIL Open Font License" in notice
    assert "Selawik" in notice and "Cascadia" in notice


def test_new_fuzz_form_follows_the_default_extensions_setting_without_a_tab_switch() -> None:
    # Reported: the "Default extensions activated" field only changed after leaving the tab. Settings announces
    # its new value with a window event and every New Fuzz form listens for it.
    source = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert source.count('"optifuzz:default-extensions"') == 3  # dispatch, add and remove of the listener
