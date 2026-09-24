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
