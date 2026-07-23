"""Demonstrate scraping an HTML table into dictionaries with Rustwright's sync API."""

from urllib.parse import quote

from rustwright.sync_api import sync_playwright


# This inline fixture keeps the scraping example independent of external websites.
html = """
<!doctype html>
<html>
  <body>
    <table>
      <thead>
        <tr><th>Product</th><th>Price</th><th>Stock</th></tr>
      </thead>
      <tbody>
        <tr><td>Notebook</td><td>$4.50</td><td>12</td></tr>
        <tr><td>Pen</td><td>$1.25</td><td>40</td></tr>
      </tbody>
    </table>
  </body>
</html>
"""
page_url = "data:text/html;charset=utf-8," + quote(html)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.goto(page_url)

        # Read the headings once; they become the keys in every result dictionary.
        headers = [text.strip() for text in page.locator("thead th").all_inner_texts()]

        # A locator can represent many matches. Use count() and nth() to visit each row.
        body_rows = page.locator("tbody tr")
        rows = []
        for index in range(body_rows.count()):
            cells = [text.strip() for text in body_rows.nth(index).locator("td").all_inner_texts()]
            rows.append(dict(zip(headers, cells)))

        print(f"rows: {rows}")
    finally:
        browser.close()
