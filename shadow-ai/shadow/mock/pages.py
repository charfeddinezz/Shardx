"""A handful of small sites, expressed as plain HTML the mock engine parses."""

from __future__ import annotations

from typing import Dict

PAGES: Dict[str, Dict[str, str]] = {
    "http://shop.local/": {
        "title": "Sundial Shop",
        "html": """
        <html><head><title>Sundial Shop</title></head>
        <body>
          <h1 id="title">Sundial Shop</h1>
          <p class="lede">Everyday objects, one at a time.</p>
          <form id="search" action="/search" method="get">
            <input type="search" name="q" id="q" placeholder="Search products" aria-label="Search products">
            <button type="submit" id="go">Search</button>
          </form>
          <ul class="products">
            <li class="product"><a href="/product/lamp" class="name">Linen Lamp</a> <span class="price">$48</span></li>
            <li class="product"><a href="/product/desk" class="name">Oak Desk</a> <span class="price">$340</span></li>
            <li class="product"><a href="/product/chair" class="name">Cane Chair</a> <span class="price">$190</span></li>
          </ul>
          <nav><a href="/about">About</a> <a href="/contact">Contact</a></nav>
        </body></html>
        """,
    },
    "http://shop.local/about": {
        "title": "About — Sundial Shop",
        "html": """
        <html><head><title>About — Sundial Shop</title></head>
        <body><h1>About</h1><p>We make a few things.</p><a href="/">Back to the shop</a></body></html>
        """,
    },
    "http://shop.local/contact": {
        "title": "Contact — Sundial Shop",
        "html": """
        <html><head><title>Contact — Sundial Shop</title></head>
        <body>
          <h1>Contact</h1>
          <form id="contact" action="/thanks">
            <label for="email">Email</label>
            <input id="email" name="email" type="email" required>
            <label for="message">Message</label>
            <textarea id="message" name="message"></textarea>
            <button id="send" type="submit" name="send">Send message</button>
          </form>
        </body></html>
        """,
    },
    "http://shop.local/thanks": {
        "title": "Thanks",
        "html": "<html><head><title>Thanks</title></head><body><h1>Thanks, we got it.</h1></body></html>",
    },
    "http://shop.local/product/lamp": {
        "title": "Linen Lamp",
        "html": """
        <html><head><title>Linen Lamp</title></head><body>
          <h1>Linen Lamp</h1><p class="price">$48</p>
          <label for="qty">Quantity</label>
          <select id="qty" name="qty"><option>1</option><option>2</option><option>3</option></select>
          <button id="buy" class="buy" name="buy">Add to cart</button>
          <button id="checkout" name="checkout">Checkout</button>
        </body></html>
        """,
    },
    "http://login.local/": {
        "title": "Sign in",
        "html": """
        <html><head><title>Sign in</title></head><body>
          <h1>Sign in</h1>
          <form id="login" action="/app">
            <input id="user" name="user" aria-label="Username" autocomplete="username">
            <input id="pass" name="pass" type="password" aria-label="Password" autocomplete="current-password">
            <button id="submit" type="submit">Sign in</button>
          </form>
          <p id="err" class="error" hidden>Wrong credentials.</p>
        </body></html>
        """,
    },
    "http://login.local/app": {
        "title": "Dashboard",
        "html": """
        <html><head><title>Dashboard</title></head><body>
          <h1>Dashboard</h1>
          <table id="rows"><tr><th>Order</th><th>Total</th></tr>
            <tr><td>A-1042</td><td>$120</td></tr>
            <tr><td>A-1043</td><td>$64</td></tr>
          </table>
          <button id="logout" name="logout">Sign out</button>
        </body></html>
        """,
    },
    "http://slow.local/": {
        "title": "Slow page",
        "html": """
        <html><head><title>Slow page</title></head><body>
          <h1>Loading…</h1>
          <div id="content"></div>
          <script>
            setTimeout(function(){
              document.getElementById('content').innerHTML =
                '<button id="ready" class="ready" name="ready">Ready</button>';
            }, 1200);
          </script>
        </body></html>
        """,
    },
    "http://flaky.local/": {
        "title": "Flaky",
        "html": """
        <html><head><title>Flaky</title></head><body>
          <h1>Flaky</h1><button id="retry" name="retry">Try again</button>
        </body></html>
        """,
    },
}

ALIASES = {
    "about:blank": {"title": "New tab", "html": "<html><head><title>New tab</title></head><body></body></html>"},
}


SEARCH_PAGE = """
<html><head><title>Results for {q} — Sundial Shop</title></head><body>
  <h1>Results for {q}</h1>
  <form id="search" action="/search" method="get">
    <input type="search" name="q" id="q" placeholder="Search products" value="{q}">
    <button type="submit" id="go">Search</button>
  </form>
  <ul class="products">
    <li class="product"><a href="/product/lamp" class="name">Linen Lamp</a> <span class="price">$48</span></li>
    <li class="product"><a href="/product/desk" class="name">Oak Desk</a> <span class="price">$340</span></li>
  </ul>
  <p class="count">2 results</p>
</body></html>
"""


def page_for(url: str) -> Dict[str, str]:
    from urllib.parse import urlsplit, parse_qs
    parts = urlsplit(url)
    if parts.path.rstrip("/").endswith("/search"):
        query = (parse_qs(parts.query).get("q") or [""])[0]
        return {"title": f"Results for {query}", "html": SEARCH_PAGE.format(q=query or "everything")}
    variants = [url]
    bare = url.split("?")[0].rstrip("/")
    variants += [bare, bare + "/"]
    if bare.startswith("https://"):
        variants.append("http://" + bare[len("https://"):])
    elif bare.startswith("http://"):
        variants.append("https://" + bare[len("http://"):])
    for candidate in variants:
        if candidate in PAGES:
            return PAGES[candidate]
        if candidate in ALIASES:
            return ALIASES[candidate]
    base = bare + "/"
    for candidate in (base, base.replace("https://", "http://"), base.replace("http://", "https://")):
        if candidate in PAGES:
            return PAGES[candidate]
    return {"title": "Not found", "html": f"<html><head><title>Not found</title></head>"
            f"<body><h1>404</h1><p>No page at {url}</p><a href='http://shop.local/'>Home</a></body></html>"}
