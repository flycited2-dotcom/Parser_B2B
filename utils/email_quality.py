"""Шлюз качества email: ремонт закодированных адресов и отсев мусора.

Правила выведены из реального прогона 01.10.2026: в добор попадали адреса
казино-спама, хостингов, надзорных органов, служебные адреса почтовиков
(`rating@mail.ru` — счётчик в подвале сайта), опечатки в доменах (`maail.ru`),
ROT13-кодировка (`graqre@gx-xvg.pbz` = `tender@tk-kit.com`), `%20` в начале.

Модуль чистый (без сети и состояния). Решение «мусор» возвращается с устойчивым
кодом причины; пустая причина означает, что адрес принят без изменений.
Домен адреса, не связанный с сайтом компании, не считается мусором сам по себе
(материнская компания, дилер) — такой адрес помечается отношением «foreign».
"""
from __future__ import annotations

import re
from urllib.parse import unquote

from config.hosts import FREE_MAIL_HOSTS, PLATFORM_HOSTS, SOCIAL_HOSTS

# Распространённые зоны: по ним решаем, декодировать ли адрес как ROT13.
COMMON_TLDS = frozenset(
    "ru su рф com net org info biz ua by kz uz md ge am az kg tj tm io me pro expert online site shop store "
    "tech club tv cc co eu de uk us fr it es pl cz fi lv lt ee asia xyz top app dev cloud agency studio "
    "design tours tour travel city today news life world group company email link live space website "
    "digital ooo moscow rus center clinic dental school academy market pw ws name mobi gov edu int "
    "yalta crimea spb msk one art bar cafe fit gold land media team tips zone photo pics gallery".split()
)
FILE_EXTENSION_TLDS = frozenset(
    "png jpg jpeg gif svg webp ico css js json php html htm pdf woff woff2 ttf eot map mp4 zip".split()
)

# Служебные адреса, которые принадлежат самому почтовому сервису.
GENERIC_AT_PROVIDER = frozenset({
    "info", "mail", "support", "admin", "rating", "contact", "contacts", "office", "sales", "post",
    "help", "webmaster", "postmaster", "noreply", "no-reply", "email", "name", "user", "test", "example",
})
PLACEHOLDER_LOCALS = frozenset({
    "name", "your", "yourname", "user", "username", "example", "sample", "test", "email",
})
PLACEHOLDER_SLDS = frozenset({"example", "domain", "yourdomain", "yoursite", "sitename", "mysite"})

# Хостинги, платформы, соцсети и надзорные органы: их адрес на сайте компании
# — подвал/виджет/лицензия, а не почта самой компании.
BLOCK_DOMAIN_SUFFIXES = (
    "beget.com", "beget.ru", "reg.ru", "nic.ru", "timeweb.ru", "timeweb.com", "jino.ru", "hostia.ru",
    "sprinthost.ru", "dikidi.net", "dikidi.ru", "yclients.com", "tilda.cc", "tilda.ws", "wix.com",
    "wixpress.com", "sferum.ru", "vk.com", "vk.ru", "vk-portal.net", "roszdravnadzor.ru",
    "rospotrebnadzor.ru", "roskomnadzor.ru", "gov.ru", "gosuslugi.ru", "sentry.io",
    "onmicrosoft.com", "jabber.ru", "jabber.org", "xmpp.ru", "xmpp.jp",
) + tuple(sorted((SOCIAL_HOSTS | PLATFORM_HOSTS) - FREE_MAIL_HOSTS))
# yandex.ru есть и среди площадок, и среди почтовых сервисов: ящик компании там легитимен,
# поэтому почтовые сервисы из блок-листа вычтены.
SPAM_DOMAIN_RE = re.compile(
    r"casino|kazino|1xbet|poker|slots?(?:[-.]|$)|porn|xxx|vulkan|888|(?:^|[-.])bet(?:[-.]|$)", re.I
)

# Крупные почтовые сервисы: домен на расстоянии одной правки — опечатка.
TYPO_PROVIDERS = (
    "mail.ru", "yandex.ru", "gmail.com", "inbox.ru", "rambler.ru", "hotmail.com", "outlook.com", "yahoo.com",
)

# ROT13 от com/net/info/biz: такие «зоны» не бывают настоящими. Двухбуквенные и org (bet — реальная
# зона) не входят: info@firma.com.tr не должен превращаться в vasb@svezn.pbz.ge.
ROT13_UNMISTAKABLE_TLDS = frozenset({"pbz", "arg", "vasb", "ovm"})

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_LOCAL_RE = re.compile(r"^[\w.+%'-]{1,64}$")
_LABEL_RE = re.compile(r"^(?!-)[\w-]{1,63}(?<!-)$")
_SECOND_LEVEL = frozenset({"com", "net", "org", "co", "msk", "spb", "pp", "gov", "edu", "ac"})
_ROT13 = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
    "NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyz abcdefghijklm".replace(" ", ""),
)


def rot13(text: str) -> str:
    return text.translate(_ROT13)


def _syntax_ok(email: str) -> bool:
    if email.count("@") != 1 or len(email) > 254:
        return False
    local, domain = email.split("@")
    if not _LOCAL_RE.match(local) or "." not in domain:
        return False
    return all(_LABEL_RE.match(label) and "_" not in label for label in domain.split("."))


def _tld(domain: str) -> str:
    return domain.rsplit(".", 1)[-1]


def _tld_plausible(tld: str) -> bool:
    if tld.startswith("xn--"):
        return len(tld) > 4
    return tld.isalpha() and 2 <= len(tld) <= 24


def _one_edit_apart(a: str, b: str) -> bool:
    """Расстояние Дамерау—Левенштейна ровно 1 (вставка, удаление, замена, перестановка)."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return (
            len(diff) == 2
            and diff[1] == diff[0] + 1
            and a[diff[0]] == b[diff[1]]
            and a[diff[1]] == b[diff[0]]
        )
    if len(a) > len(b):
        a, b = b, a
    i = j = 0
    skipped = False
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


def _unicode_host(host: str) -> str:
    value = (host or "").strip().lower().rstrip(".")
    if value.startswith("www."):
        value = value[4:]
    if "xn--" in value:
        try:
            value = value.encode("ascii").decode("idna")
        except UnicodeError:
            pass
    return value


def _sld(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


def is_related(domain: str, site_host: str) -> bool:
    """Домен адреса связан с сайтом: тот же/родственный/поддомен/общее имя."""
    left, right = _unicode_host(domain), _unicode_host(site_host)
    if not left or not right:
        return False
    if left == right or left.endswith("." + right) or right.endswith("." + left):
        return True
    a, b = _sld(left), _sld(right)
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= 5 and short in long


def email_relation(email: str, site_host: str) -> str:
    """'free' | 'same' | 'foreign' | 'unknown' — связь домена адреса с сайтом."""
    domain = email.rpartition("@")[2].lower()
    if domain in FREE_MAIL_HOSTS:
        return "free"
    if not site_host:
        return "unknown"
    return "same" if is_related(domain, site_host) else "foreign"


def sanitize_email(raw: object, site_host: str = "") -> tuple[str | None, str]:
    """(очищенный адрес | None, код). Код: '' без изменений; 'repaired'; 'rot13_decoded';
    иначе причина отсева: invalid_syntax, invalid_tld, platform_or_authority, hex_token,
    placeholder, spam_domain, provider_service_address, provider_typo."""
    original = str(raw or "").strip().strip("<>\"'").lower()
    text = unquote(original).strip().strip("<>\"'()[]{};, ").lstrip("._-%+ ").lower()
    note = "" if text == original else "repaired"
    if not _syntax_ok(text):
        return None, "invalid_syntax"
    tld = _tld(text.partition("@")[2])
    if tld in FILE_EXTENSION_TLDS or not _tld_plausible(tld):
        return None, "invalid_tld"
    if tld not in COMMON_TLDS and not tld.startswith("xn--"):
        decoded = rot13(text)
        decoded_domain = decoded.partition("@")[2]
        if (
            _syntax_ok(decoded)
            and _tld(decoded_domain) in COMMON_TLDS
            and (tld in ROT13_UNMISTAKABLE_TLDS or is_related(decoded_domain, site_host))
        ):
            text, note = decoded, "rot13_decoded"
    local, _, domain = text.partition("@")
    if any(domain == s or domain.endswith("." + s) for s in BLOCK_DOMAIN_SUFFIXES):
        return None, "platform_or_authority"
    if _HEX32_RE.match(local):
        return None, "hex_token"
    if local in PLACEHOLDER_LOCALS or _sld(domain) in PLACEHOLDER_SLDS:
        return None, "placeholder"
    related = is_related(domain, site_host)
    if not related and SPAM_DOMAIN_RE.search(domain):
        return None, "spam_domain"
    if any(domain.endswith("." + provider) for provider in FREE_MAIL_HOSTS):
        # corp.mail.ru, mail.yandex.ru…: служебные адреса самого почтового сервиса, не компании
        return None, "provider_subdomain"
    if domain in FREE_MAIL_HOSTS:
        if local in GENERIC_AT_PROVIDER:
            return None, "provider_service_address"
    elif not related and any(_one_edit_apart(domain, provider) for provider in TYPO_PROVIDERS):
        return None, "provider_typo"
    return text, note


def sanitize_row_emails(email: object, all_emails: object, site_host: str = "") -> tuple[str, str]:
    """(основной, все через ' | ') после очистки; основной берётся первым из годных."""
    candidates = [str(email or ""), *re.split(r"\s*\|\s*", str(all_emails or ""))]
    clean: list[str] = []
    for raw in candidates:
        if not raw.strip():
            continue
        value, _note = sanitize_email(raw, site_host)
        if value and value not in clean:
            clean.append(value)
    return (clean[0] if clean else ""), " | ".join(clean)
