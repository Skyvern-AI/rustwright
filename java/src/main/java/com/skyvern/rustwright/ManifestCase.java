package com.skyvern.rustwright;

import java.util.List;
import java.util.Map;

record ManifestCase(String id, String html, List<Map<String, Object>> steps) {}
