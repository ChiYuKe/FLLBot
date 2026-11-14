"""Extract the public contributor sidebar from a GitHub repository page."""

import asyncio
import re
from html.parser import HTMLParser

import httpx

_MAX_PAGE_BYTES = 2_000_000
_PROFILE = re.compile(r"/users/([A-Za-z0-9-]{1,39})/hovercard")


class _ContributorSidebar(HTMLParser):
    def __init__(self, repository_path: str) -> None:
        super().__init__()
        self.graph_path = repository_path.casefold() + "/graphs/contributors"
        self.depth = 0
        self.is_contributor_cell = False
        self.cell_accounts: dict[str, str] = {}
        self.accounts: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif "BorderGrid-cell" in (attributes.get("class") or "").split():
                self.depth = 1
                self.is_contributor_cell = False
                self.cell_accounts.clear()
        if not self.depth or tag != "a":
            return
        href = (attributes.get("href") or "").casefold().removeprefix("https://github.com")
        if href == self.graph_path:
            self.is_contributor_cell = True
        hovercard = _PROFILE.fullmatch(attributes.get("data-hovercard-url") or "")
        if hovercard:
            login = hovercard[1]
            if href == "/" + login.casefold():
                self.cell_accounts.setdefault(login.casefold(), login)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.depth:
            self.depth -= 1
            if not self.depth and self.is_contributor_cell:
                self.accounts.update(self.cell_accounts)


async def read_page_accounts(
    client: httpx.AsyncClient, owner: str, repo: str, *, timeout: float
) -> list[str]:
    path = f"/{owner}/{repo}"
    parser = _ContributorSidebar(path)
    try:
        async with asyncio.timeout(timeout), client.stream(
            "GET",
            f"https://github.com{path}",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        ) as response:
            if response.status_code != 200 or "text/html" not in response.headers.get("content-type", ""):
                return []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_PAGE_BYTES:
                    return []
                # The parser only needs ASCII markup, paths, and account names.
                parser.feed(chunk.decode("ascii", errors="replace"))
            parser.close()
    except (httpx.HTTPError, TimeoutError, ValueError):
        return []
    return list(parser.accounts.values())
