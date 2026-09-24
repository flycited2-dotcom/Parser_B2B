import asyncio

from utils.browser import _make_public_network_guard


class _Request:
    def __init__(self, url: str, navigation: bool = True):
        self.url = url
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class _Route:
    def __init__(self):
        self.continued = False
        self.aborted = ""

    async def continue_(self):
        self.continued = True

    async def abort(self, reason: str):
        self.aborted = reason


def test_browser_guard_blocks_private_navigation():
    route = _Route()
    guard = _make_public_network_guard()
    asyncio.run(guard(route, _Request("http://127.0.0.1/admin")))
    assert route.aborted == "blockedbyclient"
    assert not route.continued


def test_browser_guard_blocks_private_redirect_target():
    route = _Route()
    guard = _make_public_network_guard()
    # A redirect is presented to Playwright as another navigation request.
    asyncio.run(guard(route, _Request("http://169.254.169.254/latest/meta-data/")))
    assert route.aborted == "blockedbyclient"


def test_browser_guard_allows_non_network_browser_url():
    route = _Route()
    guard = _make_public_network_guard()
    asyncio.run(guard(route, _Request("about:blank")))
    assert route.continued
    assert not route.aborted
