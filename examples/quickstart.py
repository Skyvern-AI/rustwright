"""Minimal, network-independent Rustwright smoke example."""

from rustwright.sync_api import sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.goto("data:text/html,%3Ctitle%3ERustwright%20works%3C/title%3E")
        print(page.title())
    finally:
        browser.close()
