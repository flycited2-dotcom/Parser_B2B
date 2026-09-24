import os

from utils.env_loader import load_all_env


def test_later_files_win_but_process_environment_wins(tmp_path, monkeypatch):
    file_key = "HORECA_TEST_FILE_ORDER"
    process_key = "HORECA_TEST_PROCESS_ORDER"
    ignored_key = "HORECA_TEST_EXAMPLE_IGNORED"
    for key in (file_key, process_key, ignored_key):
        monkeypatch.delenv(key, raising=False)

    (tmp_path / ".env").write_text(
        f"{file_key}=base\n{process_key}=from-file\n", encoding="utf-8"
    )
    (tmp_path / ".env.extra").write_text(f"{file_key}=extra\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text(f"{file_key}=local\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text(f"{ignored_key}=bad\n", encoding="utf-8")
    monkeypatch.setenv(process_key, "from-process")

    loaded = load_all_env(str(tmp_path))

    assert loaded == [".env", ".env.extra", ".env.local"]
    assert os.environ[file_key] == "local"
    assert os.environ[process_key] == "from-process"
    assert ignored_key not in os.environ


def test_empty_value_in_later_file_overrides_earlier_file(tmp_path, monkeypatch):
    key = "HORECA_TEST_EMPTY_OVERRIDE"
    monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(f"{key}=present\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text(f"{key}=\n", encoding="utf-8")

    load_all_env(str(tmp_path))

    assert os.environ[key] == ""
