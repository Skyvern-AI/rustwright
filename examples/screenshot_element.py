"""Demonstrate saving a screenshot of one CSS-selected element with Rustwright's sync API."""

from pathlib import Path
from urllib.parse import quote

from rustwright.sync_api import sync_playwright


# A styled inline card gives the element screenshot a visible, repeatable target.
html = """
<!doctype html>
<html>
  <body>
    <article class="receipt" style="width: 260px; padding: 24px; background: #eef6ff;">
      <h1>Order ready</h1>
      <p>Your notebook and pen are packed.</p>
    </article>
  </body>
</html>
"""
page_url = "data:text/html;charset=utf-8," + quote(html)
output_path = Path("screenshot_element.png")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.goto(page_url)

        # Locator screenshots capture only the matching element, not the full page.
        page.locator(".receipt").screenshot(path=str(output_path))
        print(f"saved: {output_path}")
    finally:
        browser.close()
