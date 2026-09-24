import asyncio

from parsers import email_finder, vk_email


def test_vk_rejects_lookalike_hostname_without_fetch(monkeypatch):
    called = False

    async def fake_fetch(*args, **kwargs):
        nonlocal called
        called = True
        return "contact@example.org"

    monkeypatch.setattr(vk_email, "fetch_public_text", fake_fetch)
    result = asyncio.run(
        vk_email.extract_email_from_vk_async("https://vk.com.evil.example/group")
    )
    assert result is None
    assert called is False


def test_vk_async_fetch_uses_provider_allowlist(monkeypatch):
    captured = {}

    async def fake_fetch(url, **kwargs):
        captured.update(url=url, **kwargs)
        return "<main>booking@venue.example</main>"

    monkeypatch.setattr(vk_email, "fetch_public_text", fake_fetch)
    result = asyncio.run(vk_email.extract_email_from_vk_async("https://vk.com/venue"))
    assert result == "booking@venue.example"
    assert "vk.com" in captured["allowed_hosts"]
    assert captured["max_response_bytes"] == 2 * 1024 * 1024


def test_enrichment_csv_neutralizes_formula_prefixes():
    row = email_finder._csv_safe_row(
        {
            "name": "\n=HYPERLINK(\"https://evil\")",
            "phone": "+79780000000",
            "city": "Ялта",
        }
    )
    assert row["name"].startswith("'")
    assert row["phone"].startswith("'")
    assert row["city"] == "Ялта"


def test_contact_extractors_keep_ranked_alternatives():
    text = "sales@other.example, info@venue.ru, +7 978 111-22-33, 8 (978) 444-55-66"
    emails = email_finder.pick_emails(text, "venue.ru")
    phones = email_finder.pick_phones(text)

    assert emails[0] == "info@venue.ru"
    assert set(emails) == {"info@venue.ru", "sales@other.example"}
    assert len(phones) == 2
