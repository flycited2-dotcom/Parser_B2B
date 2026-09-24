from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_service_keeps_browser_install_read_only_and_sandboxed():
    unit = (ROOT / "deploy" / "b2b_parser.service").read_text(encoding="utf-8")
    assert "chromium_sandbox=True" not in unit  # Python owns this launch setting.
    assert "-o root -g b2b-parser" in unit
    assert "ReadWritePaths=/home/b2b_parser/output" in unit
    assert "ReadWritePaths=/home/b2b_parser/output /home/b2b_parser/.cache" not in unit
    assert "XDG_CONFIG_HOME=/home/b2b_parser/output/.browser-config" in unit
    assert "StandardOutput=journal" in unit
    assert "append:/var/log" not in unit


def test_apparmor_allows_userns_only_for_root_owned_playwright_paths():
    profile = (ROOT / "deploy" / "b2b-parser-chromium.apparmor").read_text(
        encoding="utf-8"
    )
    assert "userns," in profile
    assert "/home/b2b_parser/.cache/ms-playwright/chromium-*/" in profile
    assert "flags=(unconfined)" in profile


def test_browser_source_never_disables_sandbox():
    browser = (ROOT / "utils" / "browser.py").read_text(encoding="utf-8")
    assert "chromium_sandbox=True" in browser
    assert "--no-sandbox" not in browser


def test_timer_runs_friday_and_does_not_collide():
    timer = (ROOT / "deploy" / "b2b_parser.timer").read_text(encoding="utf-8")
    assert "OnCalendar=Fri *-*-* 03:00:00" in timer
