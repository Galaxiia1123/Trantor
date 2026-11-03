"""Discover public sources and keep excerpts from the actual document or deposited metadata."""
import hashlib
import html
import ipaddress
import os
import re
import socket
import ssl
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPSConnection, HTTPException
from urllib.parse import quote, urljoin, urlsplit
from xml.etree import ElementTree

import httpx

from .schema import PublicReference, Source


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.pieces = []
        self.published = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1
        data = dict(attrs)
        if tag == "meta" and (data.get("property") or data.get("name")) in {"article:published_time", "datePublished", "citation_publication_date"}:
            self.published = (data.get("content") or "")[:32]

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.pieces.append(data.strip())


def read_public_page(url):
    """Pin each HTTPS connection to a validated public address while verifying the requested hostname."""
    for _ in range(3):
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port not in (None, 443):
            raise ValueError("只读取公开 HTTPS 网页")
        host = parts.hostname.encode("idna").decode()
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        public = []
        for answer in addresses:
            address = ipaddress.ip_address(answer[4][0])
            address = getattr(address,"ipv4_mapped",None) or address
            public.append(address.is_global and not address.is_multicast and not address.is_reserved)
        if not public or not all(public):
            raise ValueError("网址未解析到公共地址")
        conn = HTTPSConnection(host, timeout=5)
        try:
            with socket.create_connection((addresses[0][4][0], 443), timeout=5) as raw:
                conn.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
                path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
                conn.request("GET", quote(path, safe="/%:?=&;+,$@!~*'()_-"), headers={"User-Agent":"Chuantuo/1.1 public research","Accept":"text/html,text/plain"})
                response = conn.getresponse()
                if 300 <= response.status < 400 and response.getheader("Location"):
                    url = urljoin(url, response.getheader("Location"))
                    continue
                mime = response.headers.get_content_type()
                if response.status != 200 or mime not in {"text/html", "text/plain"}:
                    raise ValueError("原页未提供可核验正文")
                data = response.read(512_000)
                charset = response.headers.get_content_charset() or "utf-8"
                source = data.decode(charset, errors="replace")
                parser = PageText()
                if mime == "text/html":
                    parser.feed(source)
                    source = " ".join(parser.pieces)
                return url, source, parser.published
        finally:
            conn.close()
    raise ValueError("原页重定向次数过多")


def relevant_excerpt(title, body, query):
    stop = {"the","with","for","and","from","this","does","what","site","www","org","com"}
    terms = set(re.findall(r"[a-z0-9]{2,}", query.lower())) - stop
    for phrase in re.findall(r"[\u3400-\u9fff]+", query):
        terms.update(phrase[i:i+2] for i in range(max(1,len(phrase)-1)))
    if not terms or not body.strip():
        return 0, ""
    body = body[:160_000]
    ranks = []
    for start in range(0, len(body), 1600):
        span = body[start:start + 2600].lower()
        matched = sum(word in span for word in terms)
        ranks.append((matched, -start))
    coverage, offset = max(ranks)
    score = coverage + sum(t in title.lower() for t in terms)
    if not score:
        return 0, ""
    start = max(0, -offset - 350)
    return score, body[start:start + 4300].strip()


def _source(title, url, query, body, published, service):
    identity = hashlib.sha256((url + "\n" + body).encode()).hexdigest()
    return Source(id="public_" + identity[:16], name=title, text=body, sha256=hashlib.sha256(body.encode()).hexdigest(),
                  public_reference=PublicReference(url=url, query=query, published=published, service=service,
                                                    retrieved_at=datetime.now(timezone.utc).isoformat()))


class PublicSearch:
    def __init__(self, transport=None, read_page=read_public_page):
        self.transport = transport
        self.read_page = read_page

    def search(self, query, collection):
        errors, sources = [], []
        with httpx.Client(timeout=8, transport=self.transport, headers={"User-Agent":"Chuantuo/1.1 public research"}) as client:
            try:
                if collection == "research":
                    doi = re.search(r"\b10\.\d{4,9}/[-._;()/:\w]+", query, re.I | re.ASCII)
                    if doi:
                        reference = doi.group().rstrip(".,;")
                        response = client.get("https://api.crossref.org/works/" + quote(reference, safe="/"))
                        response.raise_for_status()
                        item = response.json()["message"]
                        items = [item] if item.get("DOI","").casefold() == reference.casefold() else []
                    else:
                        response = client.get("https://api.crossref.org/works", params={
                            "query.bibliographic":query, "rows":3, "select":"DOI,title,abstract,published,publisher"})
                        response.raise_for_status()
                        items = response.json()["message"]["items"]
                    for item in items:
                        if not item.get("title") or not item.get("DOI"):
                            continue
                        title = item["title"][0]
                        abstract = html.unescape(re.sub(r"<[^>]+>", " ", item.get("abstract", ""))).strip()
                        parts = (item.get("published", {}).get("date-parts") or [[]])[0]
                        date_text = "-".join(f"{v:02d}" for v in parts) or None
                        text = "\n".join(filter(None, [title, item.get("publisher"), date_text, abstract]))[:4800]
                        sources.append(_source(title, "https://doi.org/" + quote(item["DOI"], safe="/"), query, text, date_text, "Crossref"))
                else:
                    endpoint = os.environ.get("CHUANTUO_SEARCH_ENDPOINT")
                    if endpoint:
                        response = client.get(endpoint, params={"q":query,"format":"json"})
                        response.raise_for_status()
                        links = [(i.get("url"),i.get("title")) for i in response.json()["results"][:5]]
                    else:
                        response = client.get("https://www.bing.com/search", params={"q":query,"format":"rss"})
                        response.raise_for_status()
                        links = [(i.findtext("link"), i.findtext("title")) for i in ElementTree.fromstring(response.content[:300_000]).findall(".//item")[:5]]
                    ranked = []
                    for url, title in links:
                        if not url or not title:
                            continue
                        try:
                            address, body, published = self.read_page(url)
                            score, excerpt = relevant_excerpt(title, body, query)
                            if score:
                                ranked.append((score, _source(title, address, query, excerpt, published, "网页原文")))
                        except (OSError, ValueError, HTTPException, LookupError):
                            errors.append("候选网页未取得可核验正文")
                    sources.extend(s for _,s in sorted(ranked, key=lambda entry:-entry[0])[:2])
            except (httpx.HTTPError, KeyError, ValueError, ElementTree.ParseError):
                errors.append("公开索引暂不可用")
        return sources, list(dict.fromkeys(errors))
