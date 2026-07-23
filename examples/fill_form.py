"""Demonstrate filling and submitting a form with Rustwright's sync API."""

from urllib.parse import quote

from rustwright.sync_api import sync_playwright


# Keeping the page inline makes this example deterministic and safe to run offline.
html = """
<!doctype html>
<html>
  <body>
    <form id="profile-form">
      <label>Name <input name="name"></label>
      <label>Email <input name="email" type="email"></label>
      <button type="submit">Submit</button>
    </form>
    <output id="submitted"></output>
    <script>
      document.querySelector("#profile-form").addEventListener("submit", event => {
        event.preventDefault();
        const form = new FormData(event.currentTarget);
        document.querySelector("#submitted").textContent =
          `${form.get("name")} (${form.get("email")})`;
      });
    </script>
  </body>
</html>
"""
page_url = "data:text/html;charset=utf-8," + quote(html)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.goto(page_url)

        # Label locators describe the form the same way a user does.
        page.get_by_label("Name").fill("Ada Lovelace")
        page.get_by_label("Email").fill("ada@example.test")

        # Submit through the visible button, then read the result rendered by the page.
        page.get_by_role("button", name="Submit").click()
        submitted = page.locator("#submitted").inner_text()
        print(f"submitted: {submitted}")
    finally:
        browser.close()
