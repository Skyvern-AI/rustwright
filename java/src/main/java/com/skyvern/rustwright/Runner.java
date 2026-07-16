package com.skyvern.rustwright;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Manifest-v1 benchmark runner. */
public final class Runner {
    private static final char[] HEX = "0123456789ABCDEF".toCharArray();

    private Runner() {}

    public static void main(String[] arguments) {
        int exitCode;
        try {
            exitCode = run(arguments);
        } catch (Exception error) {
            System.err.println("runner: " + usefulMessage(error));
            exitCode = 2;
        }
        if (exitCode != 0) {
            System.exit(exitCode);
        }
    }

    static int run(String[] arguments) throws IOException {
        Cli cli = Cli.parse(arguments);
        Object manifestJson;
        try {
            manifestJson = Json.parse(Files.readString(cli.manifest(), StandardCharsets.UTF_8));
        } catch (IllegalArgumentException error) {
            throw new IllegalArgumentException("invalid manifest JSON: " + error.getMessage(), error);
        }
        Manifest manifest = Manifest.validate(manifestJson);
        List<ManifestCase> selected = selectCases(manifest.cases(), cli.caseIds());

        Chromium chromium = new Chromium(cli.library());
        List<Map<String, Object>> results = new ArrayList<>(selected.size());
        try (Browser browser = chromium.launch(Map.of("headless", true))) {
            for (ManifestCase manifestCase : selected) {
                results.add(runCase(browser, manifestCase));
            }
        }

        Map<String, Object> output = new LinkedHashMap<>();
        output.put("lang", "java");
        output.put("results", results);
        Path parent = cli.output().toAbsolutePath().normalize().getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }
        Files.writeString(cli.output(), Json.stringify(output) + System.lineSeparator(),
                StandardCharsets.UTF_8);

        return results.stream().allMatch(result -> Boolean.TRUE.equals(result.get("ok"))) ? 0 : 1;
    }

    private static Map<String, Object> runCase(Browser browser, ManifestCase manifestCase) {
        long start = System.nanoTime();
        Map<String, Object> captures = new LinkedHashMap<>();
        boolean ok = true;
        String errorMessage = null;
        Page page = null;
        int stepIndex = -1;
        try {
            page = browser.newPage();
            for (stepIndex = 0; stepIndex < manifestCase.steps().size(); stepIndex++) {
                executeStep(page, manifestCase, manifestCase.steps().get(stepIndex), captures);
            }
        } catch (RuntimeException error) {
            ok = false;
            String prefix = stepIndex < 0 ? "new page: " : "step " + (stepIndex + 1) + ": ";
            errorMessage = prefix + usefulMessage(error);
        } finally {
            if (page != null) {
                try {
                    page.close();
                } catch (RuntimeException closeError) {
                    if (ok) {
                        ok = false;
                        errorMessage = "page close: " + usefulMessage(closeError);
                    } else {
                        errorMessage += "; page close: " + usefulMessage(closeError);
                    }
                }
            }
        }

        double elapsedMs = (System.nanoTime() - start) / 1_000_000.0d;
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("id", manifestCase.id());
        result.put("ok", ok);
        result.put("captures", captures);
        result.put("ms", elapsedMs);
        if (!ok) {
            result.put("error", errorMessage);
        }
        return result;
    }

    private static void executeStep(Page page, ManifestCase manifestCase, Map<String, Object> step,
            Map<String, Object> captures) {
        String operation = (String) step.get("op");
        switch (operation) {
            case "goto" -> {
                String url = step.containsKey("url")
                        ? (String) step.get("url")
                        : caseHtmlUrl(manifestCase.html());
                page.goTo(url, (String) step.get("waitUntil"));
            }
            case "click" -> page.click((String) step.get("selector"));
            case "fill" -> page.fill((String) step.get("selector"), (String) step.get("value"));
            case "title" -> captures.put((String) step.get("capture"), page.title());
            case "textContent" -> captures.put((String) step.get("capture"),
                    page.textContent((String) step.get("selector")));
            case "evaluate" -> {
                Object result = step.containsKey("arg")
                        ? page.evaluate((String) step.get("expression"), step.get("arg"))
                        : page.evaluate((String) step.get("expression"));
                captures.put((String) step.get("capture"), result);
            }
            case "screenshot" -> captures.put((String) step.get("capture"),
                    (long) page.screenshot().length);
            case "assertTitle" -> assertString("title", page.title(), step);
            case "assertText" -> assertString("textContent", page.textContent((String) step.get("selector")), step);
            case "assertEval" -> {
                Object actual = page.evaluate((String) step.get("expression"));
                Object expected = step.get("equals");
                if (!Json.structuralEquals(actual, expected)) {
                    throw new CaseFailure("evaluation mismatch: expected " + Json.stringify(expected)
                            + ", got " + Json.stringify(actual));
                }
            }
            default -> throw new CaseFailure("unknown operation after validation: " + operation);
        }
    }

    private static void assertString(String label, String actual, Map<String, Object> step) {
        if (actual == null) {
            throw new CaseFailure(label + " was null");
        }
        if (step.containsKey("equals")) {
            String expected = (String) step.get("equals");
            if (!actual.equals(expected)) {
                throw new CaseFailure(label + " mismatch: expected " + Json.stringify(expected)
                        + ", got " + Json.stringify(actual));
            }
        } else {
            String expected = (String) step.get("contains");
            if (!actual.contains(expected)) {
                throw new CaseFailure(label + " did not contain " + Json.stringify(expected)
                        + ": " + Json.stringify(actual));
            }
        }
    }

    static String caseHtmlUrl(String html) {
        byte[] bytes = html.getBytes(StandardCharsets.UTF_8);
        StringBuilder encoded = new StringBuilder("data:text/html;charset=utf-8,");
        for (byte raw : bytes) {
            int value = raw & 0xff;
            if ((value >= 'A' && value <= 'Z') || (value >= 'a' && value <= 'z')
                    || (value >= '0' && value <= '9') || value == '-' || value == '.'
                    || value == '_' || value == '~') {
                encoded.append((char) value);
            } else {
                encoded.append('%').append(HEX[value >>> 4]).append(HEX[value & 0xf]);
            }
        }
        return encoded.toString();
    }

    private static List<ManifestCase> selectCases(List<ManifestCase> cases, List<String> requested) {
        if (requested == null) {
            return cases;
        }
        Set<String> available = new HashSet<>();
        for (ManifestCase manifestCase : cases) {
            available.add(manifestCase.id());
        }
        for (String id : requested) {
            if (!available.contains(id)) {
                throw new IllegalArgumentException("unknown requested case id: " + id);
            }
        }
        Set<String> selected = Set.copyOf(requested);
        return cases.stream().filter(manifestCase -> selected.contains(manifestCase.id())).toList();
    }

    private static String usefulMessage(Throwable error) {
        String message = error.getMessage();
        return message == null || message.isBlank() ? error.getClass().getSimpleName() : message;
    }

    private static final class CaseFailure extends RuntimeException {
        private static final long serialVersionUID = 1L;

        private CaseFailure(String message) {
            super(message);
        }
    }

    private record Cli(Path manifest, Path library, Path output, List<String> caseIds) {
        private static Cli parse(String[] arguments) {
            Map<String, String> values = new LinkedHashMap<>();
            for (int i = 0; i < arguments.length; i++) {
                String option = arguments[i];
                if (!Set.of("--manifest", "--lib", "--out", "--cases").contains(option)) {
                    throw usage("unknown argument: " + option);
                }
                if (i + 1 >= arguments.length) {
                    throw usage("missing value for " + option);
                }
                if (values.putIfAbsent(option, arguments[++i]) != null) {
                    throw usage("duplicate option: " + option);
                }
            }
            for (String required : List.of("--manifest", "--lib", "--out")) {
                if (!values.containsKey(required) || values.get(required).isEmpty()) {
                    throw usage("missing required option: " + required);
                }
            }

            List<String> caseIds = null;
            if (values.containsKey("--cases")) {
                String caseValue = values.get("--cases");
                if (caseValue.isEmpty()) {
                    throw usage("--cases must contain at least one id");
                }
                caseIds = List.of(caseValue.split(",", -1));
                Set<String> unique = new HashSet<>();
                for (String id : caseIds) {
                    if (id.isEmpty()) {
                        throw usage("--cases contains an empty id");
                    }
                    if (!unique.add(id)) {
                        throw usage("--cases contains duplicate id: " + id);
                    }
                }
            }
            return new Cli(Path.of(values.get("--manifest")), Path.of(values.get("--lib")),
                    Path.of(values.get("--out")), caseIds);
        }

        private static IllegalArgumentException usage(String problem) {
            return new IllegalArgumentException(problem
                    + "; usage: runner --manifest <path> --lib <path> --out <path> [--cases id1,id2]");
        }
    }
}
