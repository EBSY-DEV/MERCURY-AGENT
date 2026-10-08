"""Synthetic pages shaped like search.sunbiz.org, for registry tests.

Every entity, person and address here is fictional (example.com-style
placeholders); none of it comes from a real record or a real prospect list.
"""

from urllib.parse import parse_qs, urlparse

from mercury.registry.sunbiz import SunbizProvider

SEARCH_HEAD = """<html><body><div id="search-results"><table>
<thead><tr><th>Corporate Name</th><th>Document Number</th><th>Status</th></tr></thead><tbody>"""
SEARCH_TAIL = "</tbody></table></div></body></html>"
NO_RESULTS = "<html><body><div id='search-results'><p>No records found for this search.</p></div></body></html>"
CHALLENGE = ("<html><head><title>Just a moment...</title></head>"
             "<body><script>window._cf_chl_opt = {};</script></body></html>")


def search_page(*rows):
    """rows: (name, document_number, status)"""
    body = "".join(
        f'<tr><td class="large-width"><a href="/Inquiry/CorporationSearch/SearchResultDetail'
        f'?inquirytype=EntityName&amp;aggregateId=flal-{doc.lower()}-fixture&amp;'
        f'searchTerm=example">{name}</a></td><td class="medium-width">{doc}</td>'
        f'<td class="small-width">{status}</td></tr>'
        for name, doc, status in rows)
    return SEARCH_HEAD + body + SEARCH_TAIL


def _address(label, street, city_line):
    return (f'<div class="detailSection"><span>{label}</span>'
            f'<div>{street}<br/>{city_line}</div></div>')


def detail_page(name, doc, status="ACTIVE", principal_city="ORLANDO",
                mailing_city=None, people=(), heading="Officer/Director Detail",
                agent="EXAMPLE REGISTERED AGENTS INC"):
    """people: (title_code, "LAST, FIRST") pairs."""
    mailing_city = mailing_city or principal_city
    persons = "".join(
        f'<span>Title {code}</span><br/>{who}<br/>100 EXAMPLE ST<br/>{principal_city}, FL 32801<br/><br/>'
        for code, who in people)
    return f"""<html><body><div class="searchResultDetail">
<div class="detailSection corporationName"><p>Florida Limited Liability Company</p><p>{name}</p></div>
<div class="detailSection filingInformation"><span>Filing Information</span>
 <div><label>Document Number</label><span>{doc}</span></div>
 <div><label>FEI/EIN Number</label><span>00-0000000</span></div>
 <div><label>Date Filed</label><span>01/02/2015</span></div>
 <div><label>State</label><span>FL</span></div>
 <div><label>Status</label><span>{status}</span></div>
</div>
{_address("Principal Address", "100 EXAMPLE ST", f"{principal_city}, FL 32801")}
{_address("Mailing Address", "PO BOX 1", f"{mailing_city}, FL 32802")}
<div class="detailSection"><span>Registered Agent Name &amp; Address</span>
 <div>{agent}<br/>200 EXAMPLE AVE<br/>TAMPA, FL 33601</div></div>
<div class="detailSection"><span>{heading}</span><br/><br/><span>Name &amp; Address</span><br/>
{persons}</div>
<div class="detailSection"><span>Annual Reports</span><table><tr><td>2024</td></tr></table></div>
</div></body></html>"""


class FakeSunbiz:
    """Serves search and detail pages from a dict, and records every request."""

    def __init__(self, search_rows=(), details=None, search_html=None, status=200):
        self.search_rows = list(search_rows)
        self.details = details or {}          # doc number -> detail html
        self.search_html = search_html
        self.status = status
        self.requests: list[str] = []

    async def __call__(self, url):
        self.requests.append(url)
        if "SearchResults/EntityName" in url:
            if self.search_html is not None:
                return self.status, self.search_html
            html = search_page(*self.search_rows) if self.search_rows else NO_RESULTS
            return self.status, html
        doc = parse_qs(urlparse(url).query)["aggregateId"][0].split("-")[1].upper()
        return self.status, self.details[doc]


def provider(fake, sleeps=None):
    async def sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)
    return SunbizProvider(fake, min_interval=3.0, sleep=sleep)
