"""Шлюз качества email. Кейсы взяты из реальной чистки прогона 01.10.2026."""
import pytest

from utils.email_quality import (
    email_relation,
    sanitize_email,
    sanitize_row_emails,
)


@pytest.mark.parametrize(
    ("raw", "site", "expected", "note"),
    [
        ("%20stan.ksh@mail.ru", "kaskad.org", "stan.ksh@mail.ru", "repaired"),
        ("graqre@gx-xvg.pbz", "tk-kit.com", "tender@tk-kit.com", "rot13_decoded"),
        ("vasb@gx-xvg.pbz", "tk-kit.com", "info@tk-kit.com", "rot13_decoded"),
        ("tbebq.rxo@gx-xvg.pbz", "tk-kit.com", "gorod.ekb@tk-kit.com", "rot13_decoded"),
        ("xbevqbe@gx-xvg.pbz", "tk-kit.com", "koridor@tk-kit.com", "rot13_decoded"),
        ("  <INFO@Site.RU>  ", "site.ru", "info@site.ru", ""),
    ],
)
def test_repairs(raw, site, expected, note):
    assert sanitize_email(raw, site) == (expected, note)


@pytest.mark.parametrize(
    ("raw", "site"),
    [
        ("info@k1.surf", "k1.surf"),
        ("lombard@grouplk.ru", "capitallombard.ru"),
        ("director@fcb.expert", "фцб.рф"),
        ("info@tk-kit.com", "tk-kit.ru"),
        ("store@goodmi.ru", "mi92.ru"),
        ("klinika.almadent@gmail.com", "almadent-crimea.ru"),
        ("info@avtovse.com.ru", "автовсе.com"),
        ("sevas@woodstone.ooo", "мраморкрым.рф"),
        ("info@крымтур.рф", "крымтур.рф"),
        ("info@hotel888.ru", "hotel888.ru"),
        ("sales@betonstroy.ru", ""),
    ],
)
def test_legitimate_addresses_are_kept_unchanged(raw, site):
    assert sanitize_email(raw, site) == (raw.lower(), "")


@pytest.mark.parametrize(
    ("raw", "site", "reason"),
    [
        ("rating@mail.ru", "veloalushta.ru", "provider_service_address"),
        ("Rating@Mail.ru", "", "provider_service_address"),
        ("mail@mail.ru", "", "provider_service_address"),
        ("user@mail.ru", "", "placeholder"),
        ("example@tk-kit.com", "tk-kit.com", "placeholder"),
        ("name@email.com", "", "placeholder"),
        ("name-_09@mail09-.ru", "citilab.ru", "invalid_syntax"),
        ("al.mdmsh@crimeaedu/ru", "", "invalid_syntax"),
        ("@site.ru", "", "invalid_syntax"),
        ("info@site", "", "invalid_syntax"),
        ("a@b@c.ru", "", "invalid_syntax"),
        ("logo@2x.png", "", "invalid_tld"),
        ("press@vk.ru", "", "platform_or_authority"),
        ("support@vk.ru", "", "platform_or_authority"),
        ("info@sferum.ru", "exist.ru", "platform_or_authority"),
        ("manager@beget.com", "mcavicenna.com", "platform_or_authority"),
        ("info@dikidi.net", "dikidi.ru", "platform_or_authority"),
        ("info@reg82.roszdravnadzor.ru", "doctor-dent.su", "platform_or_authority"),
        ("info@minzdrav.gov.ru", "", "platform_or_authority"),
        ("greenefamilycamp@theurj.onmicrosoft.com", "greene.org", "platform_or_authority"),
        ("sevzdrav@sev.gov.ru", "", "platform_or_authority"),
        ("crimea@82.rospotrebnadzor.ru", "", "platform_or_authority"),
        ("85d71a895e71448591e0e0b21fa9e7af@stacks.vk-portal.net", "stroicentr.info", "platform_or_authority"),
        ("85d71a895e71448591e0e0b21fa9e7af@corp.example.org", "", "hex_token"),
        ("support@apex-casino.io", "brain-smart.ru", "spam_domain"),
        ("csky1@kykf888.com", "arena-crimea.com", "spam_domain"),
        ("csky1@kykf888.com", "", "spam_domain"),
        ("mirrabella86@maail.ru", "", "provider_typo"),
        ("someone@gmial.com", "", "provider_typo"),
    ],
)
def test_junk_is_dropped_with_reason(raw, site, reason):
    assert sanitize_email(raw, site) == (None, reason)


def test_ordinary_provider_addresses_and_lookalike_domains_are_not_typos():
    assert sanitize_email("shop@mail.ru", "")[0] == "shop@mail.ru"
    assert sanitize_email("user.name@gmail.com", "")[0] == "user.name@gmail.com"
    assert sanitize_email("info@mailer-service.ru", "")[0] == "info@mailer-service.ru"
    assert sanitize_email("ivan.petrov@ya.ru", "")[0] == "ivan.petrov@ya.ru"
    # «общий» адрес у самого почтовика принадлежит сервису, а не компании
    assert sanitize_email("info@ya.ru", "") == (None, "provider_service_address")


def test_spam_words_are_tolerated_on_the_companys_own_domain():
    assert sanitize_email("info@casino-decor.ru", "casino-decor.ru")[0] == "info@casino-decor.ru"
    assert sanitize_email("info@casino-decor.ru", "other-site.ru") == (None, "spam_domain")


@pytest.mark.parametrize(
    ("email", "site", "expected"),
    [
        ("info@tk-kit.com", "tk-kit.ru", "same"),
        ("evpatoria@top-academy.ru", "evpatoria.top-academy.ru", "same"),
        ("info@lugovets.ru", "lugovets-flowers.ru", "same"),
        ("info@site.ru", "www.site.ru", "same"),
        ("x@gmail.com", "a.ru", "free"),
        ("x@ukr.net", "a.ru", "free"),
        ("gfc@urj.org", "greene.org", "foreign"),
        ("lombard@grouplk.ru", "capitallombard.ru", "foreign"),
        ("a@b.ru", "", "unknown"),
    ],
)
def test_email_relation(email, site, expected):
    assert email_relation(email, site) == expected


def test_relation_handles_internationalized_hosts():
    punycode = "крымтур.рф".encode("idna").decode("ascii")
    assert email_relation(f"info@{punycode}", "крымтур.рф") == "same"
    assert email_relation("info@крымтур.рф", punycode) == "same"


def test_sanitize_row_emails_promotes_and_deduplicates():
    primary, everything = sanitize_row_emails(
        "rating@mail.ru",
        "rating@mail.ru | INFO@okna.ru | info@okna.ru | graqre@gx-xvg.pbz | gfc@beget.com",
        "okna.ru",
    )
    assert primary == "info@okna.ru"
    assert everything == "info@okna.ru | tender@tk-kit.com"


def test_sanitize_row_emails_keeps_good_primary_first_and_handles_empty():
    assert sanitize_row_emails("a@okna.ru", "b@okna.ru", "okna.ru") == ("a@okna.ru", "a@okna.ru | b@okna.ru")
    assert sanitize_row_emails("", "", "okna.ru") == ("", "")
    assert sanitize_row_emails("junk", "", "") == ("", "")
