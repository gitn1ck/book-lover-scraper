"""Watch a small online shop for the "Book Lover" mug and push a notification via ntfy.

Config via environment variables:
  CANDIDATE_URLS   Product URLs to try, comma or newline separated (variants welcome).
  SEARCH_URLS      Optional shop pages (search/category) to scan for product links matching KEYWORD.
  KEYWORD          Regex the product must match (default: book lover / book-lover / booklover).
  NTFY_TOPIC       ntfy.sh topic to publish to (required for notifications).
  NTFY_SERVER      Default https://ntfy.sh
  INTERVAL         Seconds between checks (default 30).
  RUN_MINUTES      How long to loop before exiting (default 0 = single check).
  RENOTIFY_MINUTES While available, repeat the notification this often (default 10).
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

KEYWORD = re.compile(os.environ.get("KEYWORD") or r"book[\s\-_]*lover", re.I)

SOLD_OUT = re.compile(
    r"ausverkauft|nicht\s+(mehr\s+)?(auf\s+lager|verf(ü|ue)gbar|lieferbar)|derzeit\s+nicht|"
    r"sold[\s\-]?out|out\s+of\s+stock|currently\s+unavailable",
    re.I,
)
ADD_TO_CART = re.compile(r"in\s+den\s+warenkorb|zum\s+warenkorb\s+hinzuf|jetzt\s+kaufen|add\s+to\s+cart|buy\s+now", re.I)


def split_list(value):
    return [v.strip() for v in re.split(r"[,\n]", value or "") if v.strip()]


def fetch(url, timeout=20):
    """Returns (status_code, final_url, body). status 0 means network error."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.geturl(), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception as e:  # timeouts, DNS, TLS ...
        print(f"  ! {url}: {e}")
        return 0, url, ""


def visible_text(body):
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", body)
    return html.unescape(re.sub(r"(?s)<[^>]+>", " ", body))


def jsonld_availability(body):
    """Looks for schema.org availability in JSON-LD / microdata. Returns True, False or None."""
    found = []
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', body):
        found += re.findall(r'"availability"\s*:\s*"([^"]+)"', block)
    found += re.findall(r'itemprop=["\']availability["\'][^>]*(?:href|content)=["\']([^"\']+)', body)
    if not found:
        return None
    states = [f.rsplit("/", 1)[-1].lower() for f in found]
    if any(s in ("instock", "limitedavailability", "preorder", "presale", "onlineonly") for s in states):
        return True
    if any(s in ("outofstock", "soldout", "discontinued") for s in states):
        return False
    return None


def shopify_availability(url, body):
    """Shopify shops expose /products/<handle>.js with an "available" flag."""
    if "cdn.shopify.com" not in body and "Shopify" not in body:
        return None
    m = re.search(r"(https?://[^/]+(?:/[a-z]{2}(?:-[a-z]{2})?)?/products/[^/?#]+)", url, re.I)
    if not m:
        return None
    status, _, js = fetch(m.group(1) + ".js")
    if status != 200:
        return None
    try:
        return bool(json.loads(js).get("available"))
    except ValueError:
        return None


def check_product(url):
    """Returns ("missing"|"other"|"available"|"soldout"|"unknown", final_url, reason)."""
    status, final_url, body = fetch(url)
    if status != 200:
        return "missing", final_url, f"HTTP {status}"
    if "/products/" in url and "/products/" not in final_url:
        # Shop redirects unknown products to a listing page that may mention the mug too.
        return "other", final_url, "redirected away from product"
    if not KEYWORD.search(body):
        # Probably redirected to the homepage or a different product.
        return "other", final_url, "keyword not on page"

    for source, avail in (("schema.org", jsonld_availability(body)), ("shopify", shopify_availability(final_url, body))):
        if avail is not None:
            return ("available" if avail else "soldout"), final_url, source

    text = visible_text(body)
    if SOLD_OUT.search(text):
        return "soldout", final_url, "text: " + SOLD_OUT.search(text).group(0)
    if ADD_TO_CART.search(text):
        return "available", final_url, "text: " + ADD_TO_CART.search(text).group(0)
    return "unknown", final_url, "no stock signal found"


def discover(search_urls):
    """Collects product links matching KEYWORD from search/category pages."""
    links = set()
    for page in search_urls:
        status, final_url, body = fetch(page)
        if status != 200:
            continue
        for href in re.findall(r'href=["\']([^"\'#]+)["\']', body):
            if KEYWORD.search(urllib.parse.unquote(href)):
                links.add(urllib.parse.urljoin(final_url, html.unescape(href)).split("?")[0])
    return links


def shopify_catalog(shop_url):
    """Scans a Shopify shop's public /products.json for products matching KEYWORD.
    Returns {product_url: available} - also catches newly listed variants (other colors, new handles)."""
    found = {}
    base = shop_url.rstrip("/")
    for page in range(1, 11):
        status, _, body = fetch(f"{base}/products.json?limit=250&page={page}")
        if status != 200:
            break
        try:
            products = json.loads(body).get("products", [])
        except ValueError:
            break
        for p in products:
            if KEYWORD.search(p.get("title", "") + " " + p.get("handle", "")):
                found[f"{base}/products/{p['handle']}"] = any(v.get("available") for v in p.get("variants", []))
        if len(products) < 250:
            break
    return found


def notify(title, message, click_url=None, priority=5):
    topic = os.environ.get("NTFY_TOPIC")
    print(f"  >> NOTIFY: {title} | {message}")
    if not topic:
        print("  (NTFY_TOPIC not set, skipping push)")
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    # JSON publishing keeps umlauts/emoji intact (HTTP headers would need encoding).
    payload = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": ["coffee", "books"]}
    if click_url:
        payload["click"] = click_url
    req = urllib.request.Request(server, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"  ! ntfy failed: {e}")


def main():
    candidates = split_list(os.environ.get("CANDIDATE_URLS"))
    search_urls = split_list(os.environ.get("SEARCH_URLS"))
    shop_url = os.environ.get("SHOPIFY_SHOP", "").strip()
    if not candidates and not search_urls and not shop_url:
        sys.exit("Set CANDIDATE_URLS, SEARCH_URLS and/or SHOPIFY_SHOP")

    interval = int(os.environ.get("INTERVAL", "30"))
    deadline = time.time() + int(os.environ.get("RUN_MINUTES", "0")) * 60
    renotify = int(os.environ.get("RENOTIFY_MINUTES", "10")) * 60
    last_notified = {}  # url -> timestamp of last "available" push
    seen_pages = set()  # pages that became reachable during this run

    while True:
        catalog = shopify_catalog(shop_url) if shop_url else {}
        urls = (set(candidates) | discover(search_urls)) - set(catalog)
        print(time.strftime("%H:%M:%S"), f"checking {len(catalog)} catalog match(es), {len(urls)} url(s)")
        results = [("available" if avail else "soldout", url, "shopify catalog") for url, avail in catalog.items()]
        results += [check_product(url) for url in sorted(urls)]
        for state, final_url, reason in results:
            print(f"  {state:9} {final_url} ({reason})")
            if state == "available":
                if time.time() - last_notified.get(final_url, 0) >= renotify:
                    notify("☕ Book Lover Tasse verfügbar!", f"Schnell zuschlagen: {final_url}", final_url)
                    last_notified[final_url] = time.time()
            elif state == "unknown" and final_url not in seen_pages:
                # Page exists but stock state is unclear - better one push too many than none.
                notify("Book Lover Seite erreichbar", f"Status unklar, bitte prüfen: {final_url}", final_url, 4)
            if state in ("available", "soldout", "unknown"):
                seen_pages.add(final_url)

        if time.time() + interval >= deadline:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
