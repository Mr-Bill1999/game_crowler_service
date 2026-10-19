"""Full critic reviews: Metacritic gives a one-sentence quote and the link; the article itself is on the
publication's site. In the 20-game test 68% of links gave a readable text (paywalls, Cloudflare and stubs the rest).

The text is kept only for analysis: the page shows short quotes with the link, as for Metacritic quotes.
"""

import trafilatura

from app.sources.web import WebClient

MIN_WORDS = 200  # shorter is a stub, a paywall teaser or a video page
MAX_WORDS = 4000


class Critics:
    def __init__(self, rps: float):
        self.web = WebClient("critics", rps, attempts=2)

    async def aclose(self) -> None:
        await self.web.aclose()

    async def full_text(self, url: str) -> str | None:
        page = await self.web.get_text(url)
        if page["status"] != 200 or not page["text"]:
            return None
        text = trafilatura.extract(page["text"], include_comments=False, include_tables=False)
        if not text or len(text.split()) < MIN_WORDS:
            return None
        return " ".join(text.split()[:MAX_WORDS])
