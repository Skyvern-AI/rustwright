package com.skyvern.rustwright;

/** A JavaScript Error returned by Page.evaluate(). */
public record JavaScriptErrorValue(String name, String message, String stack) {}
