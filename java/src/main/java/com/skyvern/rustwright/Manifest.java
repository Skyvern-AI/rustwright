package com.skyvern.rustwright;

import java.math.BigDecimal;
import java.net.URI;
import java.net.URISyntaxException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

record Manifest(List<ManifestCase> cases) {
    static Manifest validate(Object value) {
        Map<String, Object> root = object(value, "manifest");
        allowed(root, "manifest", Set.of("version", "cases"));
        required(root, "manifest", "version", "cases");

        Object version = root.get("version");
        if (!(version instanceof Number number) || !numericOne(number)) {
            throw invalid("unsupported manifest version (expected 1)");
        }
        List<Object> rawCases = array(root.get("cases"), "manifest.cases");
        if (rawCases.isEmpty()) {
            throw invalid("manifest.cases must not be empty");
        }

        Set<String> ids = new HashSet<>();
        List<ManifestCase> cases = new ArrayList<>(rawCases.size());
        for (int i = 0; i < rawCases.size(); i++) {
            String context = "manifest.cases[" + i + "]";
            Map<String, Object> rawCase = object(rawCases.get(i), context);
            allowed(rawCase, context, Set.of("id", "description", "html", "url", "steps"));
            required(rawCase, context, "id", "steps");
            String id = nonEmptyString(rawCase.get("id"), context + ".id");
            if (!ids.add(id)) {
                throw invalid("duplicate case id: " + id);
            }
            optionalString(rawCase, "description", context);
            String html = optionalString(rawCase, "html", context);
            String sourceUrl = optionalString(rawCase, "url", context);
            if (sourceUrl != null) {
                try {
                    new URI(sourceUrl);
                } catch (URISyntaxException error) {
                    throw invalid(context + ".url is not a URI reference: " + error.getMessage());
                }
            }
            List<Object> rawSteps = array(rawCase.get("steps"), context + ".steps");
            if (rawSteps.isEmpty()) {
                throw invalid(context + ".steps must not be empty");
            }
            List<Map<String, Object>> steps = new ArrayList<>(rawSteps.size());
            Set<String> captures = new HashSet<>();
            for (int stepIndex = 0; stepIndex < rawSteps.size(); stepIndex++) {
                String stepContext = context + ".steps[" + stepIndex + "]";
                Map<String, Object> step = object(rawSteps.get(stepIndex), stepContext);
                validateStep(step, stepContext, html, captures);
                steps.add(new LinkedHashMap<>(step));
            }
            cases.add(new ManifestCase(id, html, List.copyOf(steps)));
        }
        return new Manifest(List.copyOf(cases));
    }

    private static void validateStep(Map<String, Object> step, String context, String html,
            Set<String> captures) {
        required(step, context, "op");
        String operation = nonEmptyString(step.get("op"), context + ".op");
        switch (operation) {
            case "goto" -> {
                allowed(step, context, Set.of("op", "url", "useCaseHtml", "waitUntil"));
                boolean hasUrl = step.containsKey("url");
                boolean usesHtml = step.containsKey("useCaseHtml");
                if (hasUrl == usesHtml) {
                    throw invalid(context + " goto requires exactly one of url or useCaseHtml");
                }
                if (hasUrl) {
                    nonEmptyString(step.get("url"), context + ".url");
                } else {
                    if (!Boolean.TRUE.equals(step.get("useCaseHtml"))) {
                        throw invalid(context + ".useCaseHtml must be true");
                    }
                    if (html == null) {
                        throw invalid(context + " uses case HTML but the case has no html field");
                    }
                }
                if (step.containsKey("waitUntil")) {
                    String wait = string(step.get("waitUntil"), context + ".waitUntil");
                    if (!Set.of("load", "domcontentloaded", "networkidle", "commit").contains(wait)) {
                        throw invalid(context + ".waitUntil has an unsupported value: " + wait);
                    }
                }
            }
            case "click" -> {
                allowed(step, context, Set.of("op", "selector"));
                required(step, context, "selector");
                nonEmptyString(step.get("selector"), context + ".selector");
            }
            case "fill" -> {
                allowed(step, context, Set.of("op", "selector", "value"));
                required(step, context, "selector", "value");
                nonEmptyString(step.get("selector"), context + ".selector");
                string(step.get("value"), context + ".value");
            }
            case "title" -> validateCapture(step, context, Set.of("op", "capture"), captures);
            case "textContent" -> {
                allowed(step, context, Set.of("op", "selector", "capture"));
                required(step, context, "selector", "capture");
                nonEmptyString(step.get("selector"), context + ".selector");
                uniqueCapture(step, context, captures);
            }
            case "evaluate" -> {
                allowed(step, context, Set.of("op", "expression", "arg", "capture"));
                required(step, context, "expression", "capture");
                nonEmptyString(step.get("expression"), context + ".expression");
                uniqueCapture(step, context, captures);
            }
            case "screenshot" -> validateCapture(step, context, Set.of("op", "capture"), captures);
            case "assertTitle" -> {
                allowed(step, context, Set.of("op", "equals", "contains"));
                validateStringPredicate(step, context);
            }
            case "assertText" -> {
                allowed(step, context, Set.of("op", "selector", "equals", "contains"));
                required(step, context, "selector");
                nonEmptyString(step.get("selector"), context + ".selector");
                validateStringPredicate(step, context);
            }
            case "assertEval" -> {
                allowed(step, context, Set.of("op", "expression", "equals"));
                required(step, context, "expression", "equals");
                nonEmptyString(step.get("expression"), context + ".expression");
            }
            default -> throw invalid(context + " has unknown op: " + operation);
        }
    }

    private static void validateCapture(Map<String, Object> step, String context,
            Set<String> allowed, Set<String> captures) {
        allowed(step, context, allowed);
        required(step, context, "capture");
        uniqueCapture(step, context, captures);
    }

    private static void uniqueCapture(Map<String, Object> step, String context, Set<String> captures) {
        String capture = nonEmptyString(step.get("capture"), context + ".capture");
        if (!captures.add(capture)) {
            throw invalid(context + " uses duplicate capture name: " + capture);
        }
    }

    private static void validateStringPredicate(Map<String, Object> step, String context) {
        boolean equals = step.containsKey("equals");
        boolean contains = step.containsKey("contains");
        if (equals == contains) {
            throw invalid(context + " requires exactly one of equals or contains");
        }
        string(step.get(equals ? "equals" : "contains"),
                context + "." + (equals ? "equals" : "contains"));
    }

    private static boolean numericOne(Number number) {
        try {
            return new BigDecimal(number.toString()).compareTo(BigDecimal.ONE) == 0;
        } catch (NumberFormatException error) {
            return false;
        }
    }

    private static void allowed(Map<String, Object> object, String context, Set<String> allowed) {
        for (String key : object.keySet()) {
            if (!allowed.contains(key)) {
                throw invalid(context + " has unknown property: " + key);
            }
        }
    }

    private static void required(Map<String, Object> object, String context, String... names) {
        for (String name : names) {
            if (!object.containsKey(name)) {
                throw invalid(context + " is missing required property: " + name);
            }
        }
    }

    private static String optionalString(Map<String, Object> object, String key, String context) {
        return object.containsKey(key) ? string(object.get(key), context + "." + key) : null;
    }

    private static String nonEmptyString(Object value, String context) {
        String string = string(value, context);
        if (string.isEmpty()) {
            throw invalid(context + " must not be empty");
        }
        return string;
    }

    private static String string(Object value, String context) {
        if (!(value instanceof String string)) {
            throw invalid(context + " must be a string");
        }
        return string;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> object(Object value, String context) {
        if (!(value instanceof Map<?, ?> map)) {
            throw invalid(context + " must be an object");
        }
        return (Map<String, Object>) map;
    }

    @SuppressWarnings("unchecked")
    private static List<Object> array(Object value, String context) {
        if (!(value instanceof List<?> list)) {
            throw invalid(context + " must be an array");
        }
        return (List<Object>) list;
    }

    private static IllegalArgumentException invalid(String message) {
        return new IllegalArgumentException(message);
    }
}
