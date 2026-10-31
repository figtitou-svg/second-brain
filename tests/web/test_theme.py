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


# --- ambiance par projet (tâche 7) ---
import re  # noqa: E402

from web.data import PALETTE  # noqa: E402


def mix(fg: str, bg: str, alpha: float) -> str:
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * alpha + y * (1 - alpha)):02X}" for x, y in zip(f, b))


def _dosage() -> dict[str, tuple[float, float]]:
    """--tint / --halo lus dans theme.css : premier couple = sombre (défaut), second = clair."""
    pairs = re.findall(r"--tint:(\d+)%;--halo:(\d+)%", CSS.replace(" ", ""))
    assert len(pairs) == 2, pairs
    return {theme: (int(t) / 100, int(h) / 100) for theme, (t, h) in zip(("dark", "light"), pairs)}


# valeurs miroir des thèmes : carte opaque, verre (couleur, opacité), bas/haut du dégradé de fond, texte, atténué, alerte
SURFACE = {
    "dark": {"card": "#1E2740", "glass": ("#1E2740", .64), "bgs": ("#0E1426", "#1B1545"),
             "text": "#ECEEF6", "muted": "#8F99B5", "alert": "#FF5470"},
    "light": {"card": "#FFFFFF", "glass": ("#FFFFFF", .72), "bgs": ("#F5F8FE", "#EDEAFC"),
              "text": "#1A2238", "muted": "#5A6482", "alert": "#D3263F"},
}


def test_surface_mirror_values_are_in_the_stylesheets():
    for s in SURFACE.values():
        for value in (s["card"], s["text"], s["muted"], s["alert"], *s["bgs"]):
            assert value.lower() in BOTH.lower(), value


def test_text_stays_readable_on_every_project_tint():
    colors = list(PALETTE)
    assert len(colors) >= 8
    dosage = _dosage()
    for theme, s in SURFACE.items():
        tint, halo = dosage[theme]
        surfaces = [s["card"]]                       # carte opaque (repli) sous la teinte
        for bg in s["bgs"]:                          # pire cas empilé (page projet) : fond + halo + verre + teinte
            surfaces.append(mix(s["glass"][0], bg, s["glass"][1]))
        for color in colors:
            under = surfaces + [mix(s["glass"][0], mix(color, bg, halo), s["glass"][1]) for bg in s["bgs"]]
            for surface in under:
                tinted = mix(color, surface, tint)
                assert contrast(s["text"], tinted) >= 4.5, (theme, color, "text")
                # texte secondaire et lignes d'alerte (doublées d'un ● ou d'un drapeau) : seuil 3:1
                assert contrast(s["muted"], tinted) >= 3.0, (theme, color, "muted")
                assert contrast(s["alert"], tinted) >= 3.0, (theme, color, "alert")


def test_project_ambience_rules_exist():
    flat = CSS.replace(" ", "").replace("\n", "")
    assert "color-mix(insrgb,var(--c)var(--tint),transparent)" in flat
    assert "color-mix(insrgb,var(--c)var(--halo),transparent)" in flat
    assert ".pdetail::before" in CSS and "isolation:isolate" in flat
    assert "pointer-events:none" in flat and "z-index:-1" in flat
    for sel in (".now", ".rowcard", ".pcard", ".phead", ".focus li", ".sgroup"):
        assert sel in CSS
    # la teinte se pose sur le verre (jamais transparent faute de --c, qui vaut --accent par défaut)
    assert "transparent62%),var(--glass)}" in flat
    rules = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S).replace(" ", "")
    assert "border:" not in rules
