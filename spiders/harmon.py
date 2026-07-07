"""
Harmon Law Offices (harmonlawoffices.com) -- MA/NH/RI foreclosure auctions.

NOT IMPLEMENTED / NOT REGISTERED.

Their robots.txt disallows automated access to individual auction detail
pages (/auction/{id}). The listing page itself (/view-all-auctions) loads,
but it's rendered client-side via JS -- the raw HTML has no <table> rows,
only an API call the browser makes after load, which we haven't confirmed
we have permission to hit either.

Given the robots.txt disallow, this spider is intentionally left
unimplemented. AuctionSpider.allowed() would return False for any request
to /auction/*, and the base class's fail-closed behavior means scrape()
will just skip those URLs and log it -- so even if someone finishes this
class and registers it below, it won't touch disallowed paths.

If you get explicit permission from Harmon Law (or find their intended
public feed/API), fill in listing_urls() / parse_listing() here and add
HarmonSpider to run-scout.py's REGISTRY.
"""

# from base import AuctionSpider
#
# class HarmonSpider(AuctionSpider):
#     name = "harmon"
#     base_url = "https://www.harmonlawoffices.com"
#
#     def listing_urls(self):
#         return [f"{self.base_url}/view-all-auctions"]
#
#     def parse_listing(self, soup, listing_url):
#         raise NotImplementedError("blocked by robots.txt -- see module docstring")