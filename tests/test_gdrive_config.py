from utils.gdrive import configured_token_path


def test_empty_gdrive_token_uses_default(monkeypatch):
    monkeypatch.setenv("GDRIVE_TOKEN", "")
    assert configured_token_path() == "token.json"


def test_explicit_gdrive_token_is_trimmed(monkeypatch):
    monkeypatch.setenv("GDRIVE_TOKEN", "  secrets/token.json  ")
    assert configured_token_path() == "secrets/token.json"
