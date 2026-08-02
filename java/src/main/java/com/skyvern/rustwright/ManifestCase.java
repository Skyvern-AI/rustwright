package com.skyvern.rustwright;

import java.util.List;
import java.util.Map;

record ManifestCase(String id, String html, long repeat, List<Map<String, Object>> steps) {}
