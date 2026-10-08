"""Synthetic pages shaped like search.sunbiz.org, for registry tests.

Every entity, person and address here is fictional (example.com-style
placeholders); none of it comes from a real record or a real prospect list.
"""

from urllib.parse import parse_qs, urlparse

from mercury.registry.sunbiz import SunbizProvider

SEARCH_HEAD = """<html><body><div id="search-results">
    <h2>Entity Name List</h2>
        <table>
            <thead>
                <tr>
                    <th class="large-width">Corporate Name</th>
                    <th class="medium-width">Document Number</th>
                    <th class="small-width">Status</th>
                </tr>
            </thead>
            <tbody>"""
SEARCH_TAIL = """</tbody>
        </table>
        <div class="navigationBar"><div class="navigationBarPaging"><span><a href="/Inquiry/CorporationSearch/SearchResults?InquiryType=EntityName&amp;inquiryDirectionType=ForwardList" title="Next List">Next List</a></span></div></div>
</div></body></html>"""
NO_RESULTS = "<html><body><div id='search-results'><p>No records found for this search.</p></div></body></html>"
CHALLENGE = ("<html><head><title>Just a moment...</title></head>"
             "<body><script>window._cf_chl_opt = {};</script></body></html>")


def search_page(*rows):
    """rows: (name, document_number, status) or (name, document_number, status, kind).

    ``kind`` is the prefix of the detail link's aggregateId. Entities are
    ``flal`` (default), ``domp``, ``domnp``, ``forp``; ``trade`` is a trademark
    and ``reject`` a rejected filing, which the site lists beside entities
    (a rejected filing can even read "Active").
    """
    body = ""
    for row in rows:
        name, doc, status, *rest = row
        kind = rest[0] if rest else "flal"
        order = "EXAMPLE%20" + doc
        body += f"""
                    <tr>
                        <td class="large-width"><a href="/Inquiry/CorporationSearch/SearchResultDetail?inquirytype=EntityName&amp;directionType=Initial&amp;searchNameOrder={order}&amp;aggregateId={kind}-{doc.lower()}-00000000-0000-4000-8000-000000000000&amp;searchTerm=example&amp;listNameOrder={order}" title="Go to Detail Screen">{name.replace("&", "&amp;")}</a></td>
                        <td class="medium-width">{doc}</td>
                        <td class="small-width">{status}</td>

                    </tr>"""
    return SEARCH_HEAD + body + SEARCH_TAIL


def _address(label, street, city_line, changed="Changed: 01/02/2020"):
    return f"""
        <div class="detailSection">
            <span>{label}</span>
            <span>

<div>
{street}<br>
            {city_line}<br>
</div>


</span>
                <br>
                <span>{changed} </span>
        </div>"""


def detail_page(name, doc, status="ACTIVE", principal_city="ORLANDO",
                mailing_city=None, people=(), heading="Officer/Director Detail",
                agent="Example Registered Agents Inc."):
    """people: (title, "LAST, FIRST") pairs. A title is a code ("MGR") or the
    wording a filing used ("President", "VP, Facilities")."""
    mailing_city = mailing_city or principal_city
    persons = "".join(f"""
         <span>Title&nbsp;{title.replace("&", "&amp;")}</span>
         <br>
         <br>
{who}    <span>

<div>
100 EXAMPLE ST<br>
            {principal_city}, FL 32801<br>
</div>


</span>
    <br>""" for title, who in people)
    return f"""<html><body><div class="searchResultDetail">
    <h2>Detail by Entity Name</h2>
    <div class="detailSection corporationName">
        <p>Florida Limited Liability Company</p>
        <p>{name.replace("&", "&amp;")}</p>
    </div>

    <div class="detailSection filingInformation">
        <span>Filing Information</span>
        <span><div>
    <label for="Detail_DocumentId">Document Number</label><span>{doc}</span>
    <label for="Detail_FeiEinNumber">FEI/EIN Number</label><span>00-0000000</span>
    <label for="Detail_FileDate">Date Filed</label><span>01/02/2015</span>
    <label for="Detail_EntityStateCountry">State</label><span>FL</span>
    <label for="Detail_Status">Status</label><span>{status}</span>
<label for="Detail_LastEvent">Last Event</label><span>AMENDMENT</span>
</div></span>
    </div>
{_address("Principal Address", "100 EXAMPLE ST", f"{principal_city}, FL 32801")}
{_address("Mailing Address", "PO BOX 1", f"{mailing_city}, FL 32802", "Changed: 03/04/2021")}

        <div class="detailSection">
            <span>Registered Agent Name &amp; Address</span>
                <span>{agent}
</span>
                <span>

<div>
200 Example Ave<br>
            Tampa, FL 33601<br>
</div>


</span>
                    <br>
                    <span>Name Changed: 04/02/2026</span><br>
        </div>

        <div class="detailSection">
            <span>{heading}</span>
                <span><b>Name &amp; Address</b></span>
                <br>
                <br>{persons}
        </div>

        <div class="detailSection">
            <span>Annual Reports</span>
            <table>
                    <tbody><tr><td class="AnnualReportHeader">Report Year</td><td class="AnnualReportHeader">Filed Date</td></tr>
    <tr>
        <td>2025</td>
        <td>01/03/2025</td>
    </tr>
            </tbody></table>
        </div>
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
        if "CorporationSearch/SearchResults?" in url:
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
