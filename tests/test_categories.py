"""utils.categories — тонкая обёртка над config.segments."""
from config.segments import SEGMENTS
from utils.categories import CANONICAL, DEFAULT, normalize


def test_normalize_delegates_to_segments():
    assert normalize("Автосервис") == "avto"
    assert normalize("stroitelstvo") == "stroitelstvo"


def test_empty_and_unknown_return_default():
    assert DEFAULT == "прочее"
    assert normalize("") == "прочее"
    assert normalize(None) == "прочее"
    assert normalize("ресторан") == "прочее"


def test_canonical_lists_segment_keys():
    assert CANONICAL == [s.key for s in SEGMENTS]
