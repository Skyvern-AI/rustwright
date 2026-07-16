#include "rustwright.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static void fail(const char *operation) {
  const char *error = rw_last_error();
  fprintf(stderr, "%s failed: %s\n", operation, error ? error : "unknown error");
  exit(1);
}

static void check(int32_t status, const char *operation) {
  if (status != 0) {
    fail(operation);
  }
}

int main(void) {
  static const char *url =
      "data:text/html;charset=utf-8,%3C%21doctype%20html%3E%3Chtml%3E%3Chead%3E"
      "%3Ctitle%3ERustwright%20C%20Smoke%3C%2Ftitle%3E%3C%2Fhead%3E%3Cbody%3E"
      "%3Ch1%20id%3D%22message%22%3Eready%3C%2Fh1%3E%3Cinput%20id%3D%22name%22%3E"
      "%3Cbutton%20id%3D%22go%22%20onclick%3D%22document.querySelector%28%27%23message%27%29.textContent%3Ddocument.querySelector%28%27%23name%27%29.value%22%3EGo%3C%2Fbutton%3E"
      "%3C%2Fbody%3E%3C%2Fhtml%3E";
  RwBrowser *browser = NULL;
  RwPage *page = NULL;
  char *response = NULL;
  char *title = NULL;
  char *before = NULL;
  char *after = NULL;
  char *value_json = NULL;
  uint8_t *screenshot = NULL;
  size_t screenshot_len = 0;

  check(rw_chromium_launch("{\"headless\":true}", &browser), "launch");
  check(rw_browser_new_page(browser, &page), "new_page");
  check(rw_page_goto(page, url, NULL, NAN, NULL, &response), "goto");
  rw_string_free(response);
  check(rw_page_title(page, NAN, &title), "title");
  check(rw_page_text_content(page, "#message", NAN, &before), "text_content(before)");
  check(rw_page_fill(page, "#name", "Rustwright C ABI", NAN), "fill");
  check(rw_page_click(page, "#go", NAN), "click");
  check(rw_page_text_content(page, "#message", NAN, &after), "text_content(after)");
  check(rw_page_evaluate(page,
                         "document.querySelector('#name').value",
                         NULL,
                         NAN,
                         &value_json),
        "evaluate");
  check(rw_page_screenshot(page, NULL, &screenshot, &screenshot_len), "screenshot");

  printf("{\"title\":\"%s\",\"before\":\"%s\",\"after\":\"%s\","
         "\"value\":%s,\"screenshotBytes\":%zu}\n",
         title,
         before,
         after,
         value_json,
         screenshot_len);

  rw_bytes_free(screenshot, screenshot_len);
  rw_string_free(value_json);
  rw_string_free(after);
  rw_string_free(before);
  rw_string_free(title);
  check(rw_page_close(page, NAN, 0), "page_close");
  rw_page_free(page);
  check(rw_browser_close(browser), "browser_close");
  rw_browser_free(browser);
  return 0;
}
