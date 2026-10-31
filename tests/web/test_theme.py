# tests/web/test_theme.py
from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent.parent / "web" / "static"
CSS = (STATIC / "theme.css").read_text(encoding="utf-8")
# les --accent sont définis dans app.css, --accent-2 et --on-grad dans theme.css : on cherche dans les deux
BOTH = (STATIC / "app.css").read_text(encoding="utf-8") + "\n" + CSS


def lum(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4     # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# valeurs miroir des feuilles (le test vérifie plus bas qu'elles y figurent)
DARK = {"accent": "#5CB8FF", "accent-2": "#A78BFA", "on-grad": "#0B1020"}
LIGHT = {"accent": "#2563C9", "accent-2": "#7C4DDB", "on-grad": "#FFFFFF"}


def test_mirror_values_are_in_the_stylesheet():
    for value in (*DARK.values(), *LIGHT.values()):
        assert value.lower() in BOTH.lower(), value


def test_text_on_gradient_accents_is_readable_at_both_ends_in_both_themes():
    for theme in (DARK, LIGHT):
        for end in (theme["accent"], theme["accent-2"]):
            assert contrast(theme["on-grad"], end) >= 4.5, (theme["on-grad"], end)


def test_fallbacks_and_guards_are_present():
    assert ":root{--c:var(--accent)}" in CSS.replace(" ", "").replace("\n", "") or "--c:var(--accent)" in CSS
    assert "@supports" in CSS and "backdrop-filter" in CSS
    assert "prefers-reduced-transparency" in CSS
    assert "inset 0 0 0 1px var(--glass-line)" in CSS
    # le verre ne redéfinit jamais les bordures (les bandeaux de couleur de projet restent)
    for line in CSS.splitlines():
        if "box-shadow:inset 0 0 0 1px" in line.replace(" ", ""):
            assert "border:" not in line.replace(" ", "")


def test_theme_is_loaded_after_app_css():
    base = (STATIC.parent / "templates" / "base.html").read_text(encoding="utf-8")
    assert base.index("/static/app.css") < base.index("/static/theme.css")
