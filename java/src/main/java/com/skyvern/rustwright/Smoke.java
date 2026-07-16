package com.skyvern.rustwright;

import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/** Real Chromium smoke test for the Java binding. */
public final class Smoke {
    private static final String HTML = """
            <!doctype html>
            <html>
              <head><title>Rustwright Java Smoke</title></head>
              <body>
                <h1 id="message">ready</h1>
                <input id="name" />
                <button id="go" onclick="document.querySelector('#message').textContent = document.querySelector('#name').value">Go</button>
              </body>
            </html>
            """;

    private Smoke() {}

    public static void main(String[] arguments) {
        Path library = parseLibrary(arguments);
        Chromium chromium = new Chromium(library);
        Map<String, Object> output = new LinkedHashMap<>();
        try (Browser browser = chromium.launch(Map.of("headless", true));
                Page page = browser.newPage()) {
            page.goTo(Runner.caseHtmlUrl(HTML));
            output.put("title", page.title());
            output.put("before", page.textContent("#message"));
            page.fill("#name", "Rustwright for Java");
            page.click("#go");
            output.put("after", page.textContent("#message"));
            output.put("value", page.evaluate("document.querySelector('#name').value"));
            output.put("screenshotBytes", (long) page.screenshot().length);
        }
        System.out.println(Json.stringify(output));
    }

    private static Path parseLibrary(String[] arguments) {
        if (arguments.length == 0) {
            String extension = System.getProperty("os.name", "").toLowerCase().contains("linux")
                    ? ".so" : ".dylib";
            return Path.of("target/release/librustwright_capi" + extension);
        }
        if (arguments.length == 2 && arguments[0].equals("--lib") && !arguments[1].isEmpty()) {
            return Path.of(arguments[1]);
        }
        throw new IllegalArgumentException("usage: smoke [--lib <path>]");
    }
}
