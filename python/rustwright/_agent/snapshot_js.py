"""Build the trusted in-page accessibility snapshot transformation."""

import json
from typing import Optional

from rustwright.sync_api import Locator


REF_HOOK = """  if (aiMode) {
    refCounter += 1;
    parts.push(`[ref=e${refCounter}]`);
  }"""

_WRAPPER_HEAD = "(function(el, maxDepth, aiMode) {"
_COUNTER = "let refCounter = 0;"
_STATE_SUFFIX_HEAD = "const stateSuffix = (node, role) => {"
_STATE_SUFFIX_CALL = "label += stateSuffix(node, role);"
_VALUE_DECLARATION = "let value = valueFor(node, role, semanticChildren, name);"
_RESULT_DECLARATION = "const result = [{ kind: 'node', label, children }];"
_SEMANTIC_CHILDREN = """const semanticElementChildren = node => {
  const result = Array.from(node.querySelectorAll ? node.querySelectorAll('*') : [])
    .filter(child => {
      const owner = ariaOwnerFor(child);
      return (!owner || owner === node) && roleOf(child) && !isHiddenForAria(child);
    });
  for (const child of ariaOwnedChildrenFor(node)) {
    if (roleOf(child) && !result.includes(child)) result.push(child);
  }
  return result;
};"""
_COLLECT_CHILD_LOOP = "  for (const child of Array.from(node.childNodes || [])) {"
_COLLECT_OWNED_LOOP = """  for (const child of ariaOwnedChildrenFor(node)) {
    if (child.parentNode === node) continue;
    for (const item of snapshotNodes(child, depth)) appendSnapshotNode(result, item);
  }"""
_SNAPSHOT_NODES_HEAD = """const snapshotNodes = (node, depth) => {
  if (!node || node.nodeType !== 1 || isHiddenForAria(node)) return [];"""
_RENDER_LOOP = "  for (const node of nodes) {"
_RENDER_NODE_BRANCH = """    } else {
      if (node.children && node.children.length) {"""
_TEXT_LINE = "lines.push(`${prefix}- ${specialText ? node.text : `text: ${yamlScalar(node.text)}`}`);"
_PARENT_LINE = "lines.push(`${prefix}- ${yamlLabel(label)}:`);"
_LEAF_LINE = "lines.push(`${prefix}- ${yamlLabel(node.label)}`);"
_FINAL_RETURN = "return render(snapshotNodes(el, 0)).join('\\n');"


def _canonical_helper() -> str:
    return Locator._aria_snapshot_helper_function_script()


def assert_hook_unique(helper: Optional[str] = None) -> None:
    """Fail loudly when the canonical renderer's ref hook has drifted."""

    source = _canonical_helper() if helper is None else helper
    occurrences = source.count(REF_HOOK)
    if occurrences != 1:
        raise RuntimeError(
            "Canonical accessibility snapshot ref hook drifted: "
            "expected exactly one occurrence, found %d" % occurrences
        )


def _replace_unique(source: str, needle: str, replacement: str, label: str) -> str:
    occurrences = source.count(needle)
    if occurrences != 1:
        raise RuntimeError(
            "Canonical accessibility snapshot %s drifted: expected exactly one occurrence, found %d"
            % (label, occurrences)
        )
    return source.replace(needle, replacement, 1)


def _transformed_helper() -> str:
    helper = _canonical_helper()
    assert_hook_unique(helper)

    helper = _replace_unique(
        helper,
        _WRAPPER_HEAD,
        "(function(el, maxDepth, aiMode, refOptions) {",
        "wrapper",
    )
    helper = _replace_unique(
        helper,
        _COUNTER,
        "let refCounter = refOptions ? refOptions.startAt : 0;",
        "counter",
    )
    helper = _replace_unique(
        helper,
        _STATE_SUFFIX_HEAD,
        "const stateSuffix = (node, role, name) => {",
        "state suffix signature",
    )
    helper = _replace_unique(
        helper,
        _STATE_SUFFIX_CALL,
        "label += stateSuffix(node, role, name);",
        "state suffix call",
    )
    helper = _replace_unique(
        helper,
        _VALUE_DECLARATION,
        _VALUE_DECLARATION
        + """
  if (
    refOptions &&
    refOptions.maskPasswordValues &&
    (node.tagName || '') === 'INPUT' &&
    String(node.getAttribute('type') || '').toLowerCase() === 'password'
  ) {
    value = '\u2022\u2022\u2022\u2022\u2022\u2022';
  }""",
        "value declaration",
    )
    helper = _replace_unique(
        helper,
        _SEMANTIC_CHILDREN,
        """const semanticElementChildren = node => {
  const result = [];
  const documentForNode = node && node.ownerDocument;
  if (documentForNode && documentForNode.createTreeWalker) {
    const walker = documentForNode.createTreeWalker(node, NodeFilter.SHOW_ELEMENT);
    for (let child = walker.nextNode(); child; child = walker.nextNode()) {
      if (refOptions && refOptions.semanticScanCount >= refOptions.maxWorkNodes) {
        refOptions.truncated = true;
        break;
      }
      if (refOptions) refOptions.semanticScanCount += 1;
      const owner = ariaOwnerFor(child);
      if ((!owner || owner === node) && roleOf(child) && !isHiddenForAria(child)) {
        result.push(child);
      }
    }
  }
  for (const child of ariaOwnedChildrenFor(node)) {
    if (refOptions && refOptions.semanticScanCount >= refOptions.maxWorkNodes) {
      refOptions.truncated = true;
      break;
    }
    if (refOptions) refOptions.semanticScanCount += 1;
    if (roleOf(child) && !result.includes(child)) result.push(child);
  }
  return result;
};""",
        "semantic children",
    )
    helper = _replace_unique(
        helper,
        _COLLECT_CHILD_LOOP,
        """  for (const child of node.childNodes || []) {
    if (refOptions && refOptions.visitedNodeCount >= refOptions.maxWorkNodes) {
      refOptions.truncated = true;
      break;
    }
    if (refOptions && child.nodeType === Node.TEXT_NODE) refOptions.visitedNodeCount += 1;""",
        "generic child loop",
    )
    helper = _replace_unique(
        helper,
        _COLLECT_OWNED_LOOP,
        """  for (const child of ariaOwnedChildrenFor(node)) {
    if (refOptions && refOptions.visitedNodeCount >= refOptions.maxWorkNodes) {
      refOptions.truncated = true;
      break;
    }
    if (child.parentNode === node) continue;
    for (const item of snapshotNodes(child, depth)) appendSnapshotNode(result, item);
  }""",
        "generic owned child loop",
    )
    helper = _replace_unique(
        helper,
        _SNAPSHOT_NODES_HEAD,
        """const snapshotNodes = (node, depth) => {
  if (!node || node.nodeType !== 1) return [];
  if (refOptions && refOptions.visitedNodeCount >= refOptions.maxWorkNodes) {
    refOptions.truncated = true;
    return [];
  }
  if (refOptions) refOptions.visitedNodeCount += 1;
  if (isHiddenForAria(node)) return [];""",
        "snapshot node budget",
    )
    helper = _replace_unique(
        helper,
        REF_HOOK,
        """  if (aiMode) {
    if (refOptions && refOptions.refs.length >= refOptions.maxRefs) {
      refOptions.truncated = true;
      refOptions.droppedNodes.add(node);
    } else {
      refCounter += 1;
      const ref = `e${refCounter}`;
      if (refOptions) {
        const entry = { ref, role, name };
        node.setAttribute(refOptions.attr, refOptions.generation + ':' + ref);
        refOptions.refs.push(entry);
        refOptions.tagged.push({ node, entry });
        refOptions.entryByNode.set(node, entry);
      }
      parts.push('[ref=' + ref + ']');
    }
  }""",
        "ref hook",
    )
    helper = _replace_unique(
        helper,
        _RESULT_DECLARATION,
        """const agentRef = refOptions ? refOptions.entryByNode.get(node) || null : null;
  const agentDropped = refOptions ? refOptions.droppedNodes.has(node) : false;
  const result = [{ kind: 'node', label, children, agentRef, agentDropped }];""",
        "snapshot node result",
    )
    helper = _replace_unique(
        helper,
        _RENDER_NODE_BRANCH,
        """    } else {
      if (refOptions && node.agentDropped && refOptions.refLimitLine == null) {
        refOptions.refLimitLine = refOptions.renderedLineCount;
        refOptions.renderLimitReached = true;
        break;
      }
      if (refOptions && node.agentRef) {
        refOptions.lineByRef[node.agentRef.ref] = refOptions.renderedLineCount;
      }
      if (node.children && node.children.length) {""",
        "render node branch",
    )
    helper = _replace_unique(
        helper,
        _RENDER_LOOP,
        """  for (const node of nodes) {
    if (refOptions && refOptions.renderLimitReached) break;""",
        "render work loop",
    )
    helper = _replace_unique(
        helper,
        _TEXT_LINE,
        """const renderedLine = `${prefix}- ${specialText ? node.text : `text: ${yamlScalar(node.text)}`}`;
      if (refOptions) {
        if (!refOptions.pushLine(lines, renderedLine)) break;
      } else {
        lines.push(renderedLine);
      }""",
        "text render line",
    )
    helper = _replace_unique(
        helper,
        _PARENT_LINE,
        """const renderedLine = `${prefix}- ${yamlLabel(label)}:`;
        if (refOptions) {
          if (!refOptions.pushLine(lines, renderedLine)) break;
        } else {
          lines.push(renderedLine);
        }""",
        "parent render line",
    )
    helper = _replace_unique(
        helper,
        _LEAF_LINE,
        """const renderedLine = `${prefix}- ${yamlLabel(node.label)}`;
        if (refOptions) {
          if (!refOptions.pushLine(lines, renderedLine)) break;
        } else {
          lines.push(renderedLine);
        }""",
        "leaf render line",
    )
    return helper


def build_fingerprint_expression() -> str:
    """Return a locator expression that reuses the renderer's role/name logic."""

    helper = _canonical_helper()
    replacement = """const role = roleOf(el);
return { role: role || '', name: role ? nameFor(el, role) : '' };"""
    return _replace_unique(helper, _FINAL_RETURN, replacement, "final return")


def _require_int(name: str, value: int, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("%s must be an integer greater than or equal to %d" % (name, minimum))


def build_tagging_expression(
    *,
    attr: str,
    generation: str,
    start_at: int,
    max_refs: int,
    max_chars: int,
    mask_password_values: bool,
    max_depth: int = 8,
) -> str:
    """Build an arrow-function expression that snapshots and tags one document."""

    if not isinstance(attr, str) or not attr:
        raise ValueError("attr must be a non-empty string")
    if not isinstance(generation, str) or not generation:
        raise ValueError("generation must be a non-empty string")
    if not isinstance(mask_password_values, bool):
        raise ValueError("mask_password_values must be a boolean")
    _require_int("start_at", start_at, 0)
    _require_int("max_refs", max_refs, 1)
    _require_int("max_chars", max_chars, 1)
    _require_int("max_depth", max_depth, 0)

    helper = _transformed_helper()
    attr_literal = json.dumps(attr)
    generation_literal = json.dumps(generation)
    start_literal = json.dumps(start_at)
    max_refs_literal = json.dumps(max_refs)
    max_chars_literal = json.dumps(max_chars)
    mask_literal = json.dumps(mask_password_values)
    depth_literal = json.dumps(max_depth)

    return (
        "(root) => {\n"
        "  const attr = " + attr_literal + ";\n"
        "  const generation = " + generation_literal + ";\n"
        "  const startAt = " + start_literal + ";\n"
        "  const maxRefs = " + max_refs_literal + ";\n"
        "  const maxChars = " + max_chars_literal + ";\n"
        "  const maskPasswordValues = " + mask_literal + ";\n"
        "  const maxDepth = " + depth_literal + ";\n"
        "  const generationMarker = ':g';\n"
        "  const generationIndex = generation.lastIndexOf(generationMarker);\n"
        "  const sessionPrefix = (generationIndex < 0 ? generation : generation.slice(0, generationIndex)) + ':';\n"
        "  for (const node of document.querySelectorAll('[' + attr + ']')) {\n"
        "    const prior = node.getAttribute(attr);\n"
        "    if (prior != null && prior.startsWith(sessionPrefix)) node.removeAttribute(attr);\n"
        "  }\n"
        "  const helper = " + helper + ";\n"
        "  const refOptions = {\n"
        "    attr, generation, startAt, maxRefs, maskPasswordValues, refs: [], tagged: [],\n"
        "    entryByNode: new WeakMap(), droppedNodes: new WeakSet(), lineByRef: Object.create(null),\n"
        "    maxWorkNodes: 5000, visitedNodeCount: 0, semanticScanCount: 0,\n"
        "    renderedLineCount: 0, renderedChars: 0, renderLimitReached: false,\n"
        "    refLimitLine: null, truncated: false,\n"
        "    pushLine(lines, line) {\n"
        "      const separator = this.renderedLineCount === 0 ? 0 : 1;\n"
        "      if (this.renderedChars + separator + line.length > maxChars) {\n"
        "        this.truncated = true;\n"
        "        this.renderLimitReached = true;\n"
        "        return false;\n"
        "      }\n"
        "      lines.push(line);\n"
        "      this.renderedChars += separator + line.length;\n"
        "      this.renderedLineCount += 1;\n"
        "      return true;\n"
        "    }\n"
        "  };\n"
        "  const snapshotRoot = root || document.body || document.documentElement;\n"
        "  const rendered = helper(snapshotRoot, maxDepth, true, refOptions);\n"
        "  const lines = rendered ? rendered.split('\\n') : [];\n"
        "  let keepLineCount = lines.length;\n"
        "  if (refOptions.refLimitLine != null) {\n"
        "    keepLineCount = Math.min(keepLineCount, refOptions.refLimitLine);\n"
        "  }\n"
        "  let truncated = refOptions.truncated || keepLineCount < lines.length;\n"
        "  const candidate = lines.slice(0, keepLineCount).join('\\n');\n"
        "  const suffix = '\\n... [snapshot truncated]';\n"
        "  if (truncated || candidate.length > maxChars) {\n"
        "    truncated = true;\n"
        "    let used = 0;\n"
        "    let fittingLines = 0;\n"
        "    for (let index = 0; index < keepLineCount; index += 1) {\n"
        "      const nextUsed = used + (index === 0 ? 0 : 1) + lines[index].length;\n"
        "      if (nextUsed + suffix.length > maxChars) break;\n"
        "      used = nextUsed;\n"
        "      fittingLines = index + 1;\n"
        "    }\n"
        "    keepLineCount = fittingLines;\n"
        "  }\n"
        "  const refs = [];\n"
        "  for (const tagged of refOptions.tagged) {\n"
        "    const line = refOptions.lineByRef[tagged.entry.ref];\n"
        "    if (line != null && line < keepLineCount) {\n"
        "      refs.push({ ref: tagged.entry.ref, role: tagged.entry.role, name: tagged.entry.name });\n"
        "    } else if (tagged.node.getAttribute(attr) === generation + ':' + tagged.entry.ref) {\n"
        "      tagged.node.removeAttribute(attr);\n"
        "    }\n"
        "  }\n"
        "  let text = lines.slice(0, keepLineCount).join('\\n');\n"
        "  if (truncated) text = text ? text + suffix : suffix.slice(1, maxChars + 1);\n"
        "  return { text, refs, truncated };\n"
        "}"
    )
