from utils.cross_base import env_paths, is_in_other_base, load_exclusions


def test_loads_semicolon_and_comma_masters_and_warns_on_missing(tmp_path):
    horeca = tmp_path / "horeca.csv"
    horeca.write_text(
        'name;email;all_emails;website\n'
        '"Кафе";info@cafe.ru;"info@cafe.ru | book@cafe.ru";https://www.cafe.ru\n',
        encoding="utf-8-sig",
    )
    hotels = tmp_path / "hotels.csv"
    hotels.write_text(
        "Название,Email,Сайт\nОтель,hotel@mail.ru,https://vk.com/hotel\n",
        encoding="utf-8-sig",
    )

    emails, domains, warnings = load_exclusions(
        [str(horeca), str(hotels), str(tmp_path / "missing.csv")]
    )

    assert emails == {"info@cafe.ru", "book@cafe.ru", "hotel@mail.ru"}
    assert domains == {"cafe.ru"}
    assert len(warnings) == 1 and "missing.csv" in warnings[0]


def test_free_mail_and_social_hosts_never_exclude_by_domain():
    emails, domains = {"hotel@mail.ru"}, {"cafe.ru"}
    assert not is_in_other_base({"email": "stroy@mail.ru", "website": "https://vk.com/stroy"}, emails, domains)
    assert is_in_other_base({"email": "HOTEL@mail.ru"}, emails, domains)
    assert is_in_other_base({"email": "sales@cafe.ru"}, emails, domains)
    assert is_in_other_base({"all_websites": "https://cafe.ru/catering | https://x.ru"}, emails, domains)


def test_env_paths_splits_semicolons(monkeypatch):
    assert env_paths("a.csv; ;b.csv") == ["a.csv", "b.csv"]
    monkeypatch.setenv("EXCLUDE_MASTERS", "")
    assert env_paths() == []


def test_platform_hosts_outside_any_list_are_ignored_by_frequency(tmp_path):
    hotels = tmp_path / "hotels.csv"
    lines = ["name;website"] + [f"Отель {i};https://tvil-like.example/hotel{i}" for i in range(6)]
    lines.append("Отель X;https://hotel-x.ru")
    hotels.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    _emails, domains, _warnings = load_exclusions([str(hotels)])

    assert domains == {"hotel-x.ru"}


def test_booking_and_messenger_hosts_never_count_as_corporate(tmp_path):
    other = tmp_path / "other.csv"
    other.write_text(
        "name;website\nA;https://wa.me/79780000000\nB;https://booking.com/hotel/a\n"
        "C;https://taplink.ru/c\nD;https://youtube.com/@d\n",
        encoding="utf-8-sig",
    )
    _emails, domains, _warnings = load_exclusions([str(other)])
    assert domains == set()


def test_unreadable_master_is_a_warning_not_a_crash(tmp_path, monkeypatch):
    import pathlib

    locked = tmp_path / "locked.csv"
    original = pathlib.Path.is_file

    def fake_is_file(self):
        if self == locked:
            raise PermissionError(13, "Permission denied")
        return original(self)

    monkeypatch.setattr(pathlib.Path, "is_file", fake_is_file)
    emails, domains, warnings = load_exclusions([str(locked)])
    assert (emails, domains) == (set(), set())
    assert len(warnings) == 1 and "locked.csv" in warnings[0]


def test_master_without_known_columns_warns(tmp_path):
    odd = tmp_path / "odd.csv"
    odd.write_text("a;b\n1;2\n", encoding="utf-8-sig")
    _emails, _domains, warnings = load_exclusions([str(odd)])
    assert len(warnings) == 1 and "no email/website" in warnings[0]
