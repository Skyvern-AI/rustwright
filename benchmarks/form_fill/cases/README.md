# Benchmark case catalog

100 comparable cases across four categories, plus tracked parity-gap repros.
All live targets are dedicated automation/scraping sandboxes (selenium.dev
test fixtures, the-internet, practice.expandtesting.com, books/quotes
.toscrape.com, httpbin). Cases use dummy data; login/submit flows exist only
on sandboxes built for them, and no real-world form is ever submitted.

| File | Cases | Lane |
|---|---:|---|
| `controlled.json` | 10 | Deterministic `data:` URL fixtures (click, evaluate, DOM) |
| `controlled_more.json` | 9 | Second controlled op mix (replication lane) |
| `controlled_breadth.json` | 11 | Form controls, iframe, shadow-DOM fixtures |
| `forms_live.json` | 14 | Live form interaction (fill/type/press/check/select/hover) |
| `selenium_pages.json` | 10 | selenium.dev static test fixtures |
| `nav_the_internet.json` | 17 | the-internet navigation journeys |
| `nav_more.json` | 7 | Additional live navigation |
| `nav_breadth.json` | 11 | Multi-hop journeys (toscrape, expandtesting, httpbin) |
| `smoke_nav.json` | 2 | Smoke |
| `downloads_more.json` + `smoke_download.json` | 3 | File downloads |
| `downloads_breadth.json` | 6 | Download breadth (txt/jpg/png/pdf, two hosts) |
| **Comparable total** | **100** | |
| `parity_gaps.json` | 7 | **Known rustwright/Playwright divergences — excluded from comparison lanes** |

## Validation

Every case was executed live against remote Skyvern cloud browser sessions on
both backends (1 rep each) before inclusion; cases that could not complete
identically on both backends were either fixed (ambiguous selectors,
fragile hover targets) or moved to `parity_gaps.json`.

## parity_gaps.json

Minimal repro cases for real engine divergences discovered during validation
(each passes on reference Playwright and fails on rustwright, except where
noted). Do not include this file in comparison runs; it exists as a tracked
work-list for engine parity fixes:

1. `parity_uncheck_selenium_web_form_all_controls` — `Page.uncheck`:
   "Not a checkbox or radio button" on a stock checkbox.
2. `parity_uncheck_expandtesting_checkbox_state_cycle` — `Page.uncheck`:
   "Clicking the checkbox did not change its state".
3. `parity_click_receives_events_books_sidebar` — actionability wait times
   out with `receives_events=false` on a plainly clickable link
   (count=1, visible, stable).
4. `expand_dynamic_home_redirect_chain` — `wait_for_selector` timeout mid
   multi-hop redirect journey.
5. `expandtesting_some_file_json_download` — the `download` event never
   fires for a `.json` link that Playwright downloads.
6. `selenium_web_form_all_controls` — multi-symptom form-control divergence
   (`uncheck`: "Not a checkbox or radio button"; on a later run `check`:
   "no element matched"); Playwright completes all 14 steps.
7. `selenium_click_bubbling_and_history` — `Page.go_back` 30 s timeout,
   reproduced twice; Playwright completes the journey.

Also observed inversely (rustwright laxer than Playwright): `hover` proceeded
on an element Playwright correctly blocks on as not-visible.

## Case schema

Navigation/form cases: `{"id", "category": "navigation", "url", "nav_steps":
[...]}` with ops `goto(url)`, `wait(selector)`, `click`, `fill(value)`,
`type(text)`, `press(key)`, `check`, `uncheck`, `select(value)`, `hover`,
`back`, `forward`, `eval(expression)`. Download cases: `{"id", "category":
"download", "url", "download_trigger": {"op": "click", "selector"},
"expect_download": true}`. Controlled fixtures are embedded as
`data:text/html;base64,` URLs so remote browsers need no network.
