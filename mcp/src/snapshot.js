(options) => {
  // This script intentionally executes in the page's main world, matching the
  // established snapshot trust boundary: page JavaScript can observe or patch it.
  const MAX_NAME = 120;
  const MAX_LINES = 1200;
  const MAX_ELEMENTS = 50000;
  const MAX_CONSTRUCTION_MS = 250;
  const MAX_FIND_MATCHES = 20;
  const {
    startRef,
    target = null,
    maxDepth = null,
    boxes = false,
    mask,
    find = null,
  } = options;
  const elementLimit = MAX_ELEMENTS;
  const timeLimitMs = MAX_CONSTRUCTION_MS;
  const now = () => performance.now();
  // Compile fallible page-owned input before touching the previous snapshot's
  // DOM refs. If RegExp is invalid or page-patched to throw, actor and DOM ref
  // state both remain on the last committed snapshot.
  const findExpression = find && find.kind === 'regex'
    ? new RegExp(find.pattern, find.flags) : null;
  const root = target === null ? document.body : document.querySelector(target);
  const trackingKey = Symbol.for('rustwright.mcp.sensitiveSnapshot');
  const tracking = globalThis[trackingKey];
  const sensitiveNodes = new Set();
  const aggregateSensitive = new Set();
  if (tracking
      && tracking.sensitiveNodes instanceof WeakSet
      && tracking.sensitiveNodeRefs instanceof Set) {
    for (const reference of tracking.sensitiveNodeRefs) {
      const node = reference && typeof reference.deref === 'function'
        ? reference.deref() : null;
      if (!node) {
        tracking.sensitiveNodeRefs.delete(reference);
        continue;
      }
      if (!node.isConnected) continue;
      sensitiveNodes.add(node);
      for (let ancestor = node; ancestor; ancestor = ancestor.parentElement) {
        aggregateSensitive.add(ancestor);
      }
    }
  }

  const ROLE_BY_TAG = {
    A: 'link', BUTTON: 'button', SELECT: 'combobox', TEXTAREA: 'textbox',
    H1: 'heading', H2: 'heading', H3: 'heading', H4: 'heading', H5: 'heading',
    H6: 'heading', IMG: 'img', NAV: 'navigation', MAIN: 'main', HEADER: 'banner',
    FOOTER: 'contentinfo', FORM: 'form', TABLE: 'table', UL: 'list', OL: 'list',
    LI: 'listitem', DIALOG: 'dialog', SUMMARY: 'button', LABEL: 'label',
    OPTION: 'option', ARTICLE: 'article', SECTION: 'region', ASIDE: 'complementary',
  };
  const INPUT_ROLES = {
    button: 'button', submit: 'button', reset: 'button', checkbox: 'checkbox',
    radio: 'radio', range: 'slider', search: 'searchbox',
  };
  const ARIA_ROLES = new Set([
    'alert', 'alertdialog', 'application', 'article', 'banner', 'blockquote',
    'button', 'caption', 'cell', 'checkbox', 'code', 'columnheader', 'combobox',
    'complementary', 'contentinfo', 'definition', 'deletion', 'dialog', 'directory',
    'document', 'emphasis', 'feed', 'figure', 'form', 'generic', 'grid', 'gridcell',
    'group', 'heading', 'img', 'insertion', 'link', 'list', 'listbox', 'listitem',
    'log', 'main', 'marquee', 'math', 'menu', 'menubar', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'meter', 'navigation', 'none', 'note',
    'option', 'paragraph', 'presentation', 'progressbar', 'radio', 'radiogroup',
    'region', 'row', 'rowgroup', 'rowheader', 'scrollbar', 'search', 'searchbox',
    'separator', 'slider', 'spinbutton', 'status', 'strong', 'subscript',
    'superscript', 'switch', 'tab', 'table', 'tablist', 'tabpanel', 'term',
    'textbox', 'time', 'timer', 'toolbar', 'tooltip', 'tree', 'treegrid', 'treeitem',
  ]);
  const INTERACTIVE_ROLES = new Set([
    'button', 'checkbox', 'gridcell', 'link', 'menuitem', 'menuitemcheckbox',
    'menuitemradio', 'option', 'progressbar', 'radio', 'scrollbar', 'searchbox',
    'slider', 'spinbutton', 'switch', 'tab', 'tabpanel', 'textbox', 'treeitem',
    'combobox', 'grid', 'listbox', 'menu', 'menubar', 'radiogroup', 'tablist',
    'tree', 'treegrid',
  ]);
  const SKIP_TAGS = new Set([
    'SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'META', 'LINK', 'HEAD', 'SVG', 'PATH',
  ]);
  const cleanText = (value) => String(value || '').trim().replace(/\s+/g, ' ');
  const containsSensitiveNode = (el) => sensitiveNodes.has(el);
  const hasSensitiveDescendant = (el) => aggregateSensitive.has(el);
  const styleOf = (el) => getComputedStyle(el);
  const isVisible = (el, style) => {
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0 || el.tagName === 'OPTION';
  };
  const roleOf = (el) => {
    if (!containsSensitiveNode(el)) {
      const explicit = el.getAttribute('role');
      if (explicit) {
        const recognized = explicit.toLowerCase().split(/\s+/)
          .find((role) => ARIA_ROLES.has(role));
        if (recognized) return recognized;
      }
    }
    if (el.tagName === 'INPUT') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      return INPUT_ROLES[type] || 'textbox';
    }
    const nativeRole = ROLE_BY_TAG[el.tagName];
    if (nativeRole) return nativeRole;
    if (containsSensitiveNode(el)
        && (el.hasAttribute('onclick') || el.tabIndex >= 0)) return 'generic';
    return null;
  };
  const directRenderedText = (el) => Array.from(el.childNodes || [])
    .filter((child) => child.nodeType === 3)
    .map((child) => cleanText(child.nodeValue))
    .filter(Boolean)
    .join(' ');
  const nameOf = (el) => {
    if (containsSensitiveNode(el)) return { value: '', source: 'masked' };
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const labelledNodes = labelled.split(/\s+/)
        .map((id) => document.getElementById(id)).filter(Boolean);
      if (labelledNodes.some(
        (node) => containsSensitiveNode(node) || hasSensitiveDescendant(node),
      )) return { value: '', source: 'masked' };
      const parts = labelledNodes.map((node) => cleanText(node.textContent));
      if (parts.length) return { value: parts.join(' '), source: 'semantic' };
    }
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) return { value: ariaLabel, source: 'semantic' };
    if (el.labels && el.labels.length) {
      if (Array.from(el.labels).some(
        (label) => containsSensitiveNode(label) || hasSensitiveDescendant(label),
      )) return { value: '', source: 'masked' };
      return { value: cleanText(el.labels[0].textContent), source: 'semantic' };
    }
    const direct = el.getAttribute('alt') || el.getAttribute('title')
      || el.getAttribute('placeholder');
    if (direct) return { value: direct, source: 'semantic' };
    if (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') {
      return { value: el.getAttribute('name') || '', source: 'semantic' };
    }
    if (hasSensitiveDescendant(el)) return { value: '', source: 'masked' };
    return { value: directRenderedText(el), source: 'rendered' };
  };
  const enclosingBox = (rect) => {
    if (rect.width <= 0 || rect.height <= 0) return null;
    const left = Math.floor(rect.left);
    const top = Math.floor(rect.top);
    const right = Math.ceil(rect.right);
    const bottom = Math.ceil(rect.bottom);
    return [left, top, right - left, bottom - top];
  };

  // Phase 1: construct a masked, deterministic pre-order subset. This is
  // iterative so deeply nested lower-page controls do not hit the JS call stack.
  const construct = () => {
    if (!root) {
      return { roots: [], covered: 0, incomplete: false, reason: null };
    }
    const roots = [];
    const stack = [{ kind: 'element', el: root, parent: null, depth: 0 }];
    const started = now();
    let covered = 0;
    let incomplete = false;
    let reason = null;
    while (stack.length) {
      if (now() - started >= timeLimitMs) {
        incomplete = true;
        reason = 'wall time';
        break;
      }
      const item = stack.pop();
      if (item.kind === 'text') {
        if (!item.masked) {
          const text = cleanText(item.value);
          if (text) item.parent.children.push(text);
        }
        continue;
      }
      if (covered >= elementLimit) {
        incomplete = true;
        reason = 'element limit';
        break;
      }
      covered += 1;
      const { el, parent, depth } = item;
      if (maxDepth !== null && depth > maxDepth) continue;
      const tag = String(el.tagName || '').toUpperCase();
      if (SKIP_TAGS.has(tag) || el.namespaceURI === 'http://www.w3.org/2000/svg') continue;
      const style = styleOf(el);
      if (!isVisible(el, style)) continue;
      const role = roleOf(el);
      const name = nameOf(el);
      // Marker precedence is semantic role, then explicit DOM markers, then
      // the nearest cursor:pointer boundary. Listener-only targets are not
      // introspectable and intentionally remain a documented miss.
      let marker = null;
      if (INTERACTIVE_ROLES.has(role) || (role === 'separator' && el.tabIndex >= 0)) {
        marker = 'semantic';
      } else if (el.hasAttribute('onclick') || typeof el.onclick === 'function'
          || el.tabIndex >= 0) {
        marker = 'explicit';
      } else {
        const parentStyle = el.parentElement ? styleOf(el.parentElement) : null;
        if (style.cursor === 'pointer'
            && (!parentStyle || parentStyle.cursor !== 'pointer')) marker = 'pointer';
      }
      const node = {
        el,
        parent,
        role,
        name: name.value,
        nameSource: name.source,
        clickRoot: marker !== null,
        marker,
        children: [],
        attrs: [],
      };
      if (/^H[1-6]$/.test(tag)) node.attrs.push(`level=${tag[1]}`);
      if (tag === 'A' && el.href && !containsSensitiveNode(el)) {
        node.attrs.push(`href=${el.getAttribute('href')}`);
      }
      if (el.disabled) node.attrs.push('disabled');
      if (el.checked) node.attrs.push('checked');
      if ((tag === 'INPUT' || tag === 'TEXTAREA') && el.value) {
        const password = tag === 'INPUT'
          && (el.getAttribute('type') || 'text').toLowerCase() === 'password';
        node.attrs.push(password || containsSensitiveNode(el)
          ? `value=${mask}` : `value="${String(el.value).slice(0, 60)}"`);
      }
      if (boxes) {
        const box = enclosingBox(el.getBoundingClientRect());
        if (box) node.attrs.push(`box=${box.join(',')}`);
      }
      if (tag === 'IFRAME' || tag === 'FRAME') {
        node.role = 'iframe';
        node.name = containsSensitiveNode(el) ? ''
          : el.getAttribute('title') || el.getAttribute('name')
            || el.getAttribute('src') || '';
        node.nameSource = 'semantic';
        node.attrs.push('content not captured');
      }
      if (parent) parent.children.push(node); else roots.push(node);
      if (tag === 'IFRAME' || tag === 'FRAME') continue;
      const children = Array.from(el.childNodes || []);
      for (let index = children.length - 1; index >= 0; index -= 1) {
        const child = children[index];
        if (child.nodeType === 1) {
          stack.push({ kind: 'element', el: child, parent: node, depth: depth + 1 });
        } else if (child.nodeType === 3) {
          stack.push({
            kind: 'text', value: child.nodeValue, parent: node,
            masked: containsSensitiveNode(el),
          });
        }
      }
    }
    return { roots, covered, incomplete, reason };
  };

  const missing = target === null
    ? '- (page has no body yet)'
    : '- (snapshot target is no longer available)';
  for (const el of document.querySelectorAll('[data-mcp-ref]')) {
    el.removeAttribute('data-mcp-ref');
  }
  if (!root) {
    return find ? {
      find: { matches: [], totalMatches: 0, coveredElements: 0,
        incomplete: false, reason: null },
      nextRef: startRef, refs: [],
    } : {
      outline: missing, units: [missing], rendererIncomplete: null,
      nextRef: startRef, refs: [],
    };
  }
  const constructed = construct();

  // Phase 2: assign current refs only to the masked, constructed subset.
  let refCounter = startRef;
  const refs = [];
  const nodes = [];
  const assignment = constructed.roots.slice().reverse();
  while (assignment.length) {
    const node = assignment.pop();
    nodes.push(node);
    if (node.marker !== null) {
      const ref = `e${refCounter}`;
      refCounter += 1;
      node.ref = ref;
      node.el.setAttribute('data-mcp-ref', ref);
      refs.push(ref);
    }
    for (let index = node.children.length - 1; index >= 0; index -= 1) {
      if (typeof node.children[index] !== 'string') assignment.push(node.children[index]);
    }
  }

  const displayRole = (node) => node.role || 'generic';
  const clippedName = (name) => name.length > MAX_NAME ? `${name.slice(0, MAX_NAME)}…` : name;
  const summary = (node) => {
    const parts = [displayRole(node)];
    if (node.name) parts.push(`"${clippedName(node.name)}"`);
    for (const attr of node.attrs) parts.push(`[${attr}]`);
    if (node.ref) parts.push(`[ref=${node.ref}]`);
    return parts.join(' ');
  };
  const pathOf = (node) => {
    const path = [];
    for (let current = node; current; current = current.parent) path.push(summary(current));
    return path.reverse().join(' > ');
  };

  if (find) {
    const needle = find.kind === 'text' ? String(find.value).toLowerCase() : null;
    const matches = [];
    let totalMatches = 0;
    const accepts = (value) => {
      if (findExpression) {
        findExpression.lastIndex = 0;
        return findExpression.test(value);
      }
      return needle !== null && value.toLowerCase().includes(needle);
    };
    for (const node of nodes) {
      const own = [displayRole(node), node.name, ...node.attrs].join(' ');
      const strings = node.children.filter((child) => typeof child === 'string');
      if (!accepts([own, ...strings].join(' '))) continue;
      totalMatches += 1;
      if (matches.length < MAX_FIND_MATCHES) {
        matches.push({ path: pathOf(node), line: summary(node), ref: node.ref || null });
      }
    }
    return {
      find: {
        matches, totalMatches, coveredElements: constructed.covered,
        incomplete: constructed.incomplete, reason: constructed.reason,
      },
      nextRef: refCounter,
      refs,
    };
  }

  // Phase 3: distill only the render tree. Constructed nodes and refs stay intact.
  for (let index = nodes.length - 1; index >= 0; index -= 1) {
    const node = nodes[index];
    const merged = [];
    for (const original of node.children) {
      const child = typeof original === 'string' ? original : original.rendered;
      if (child === null) continue;
      if (typeof child === 'string' && typeof merged[merged.length - 1] === 'string') {
        merged[merged.length - 1] = cleanText(`${merged[merged.length - 1]} ${child}`);
      } else {
        merged.push(child);
      }
    }
    node.children = merged;
    if (displayRole(node) === 'img' && !node.name && !node.clickRoot) {
      node.rendered = null;
      continue;
    }
    if (node.nameSource === 'rendered') node.name = '';
    if (displayRole(node) === 'generic'
        && node.children.length === 1
        && typeof node.children[0] === 'string') {
      node.name = node.children[0];
      node.nameSource = 'inlined';
      node.children = [];
    }
    node.children = node.children.filter((child) => !(
      typeof child !== 'string'
      && displayRole(child) === 'generic'
      && !child.clickRoot
      && node.name && child.name === node.name
    ));
    if (displayRole(node) === 'generic'
        && !node.clickRoot && !node.name && node.attrs.length === 0
        && node.children.length === 1
        && typeof node.children[0] !== 'string') {
      node.rendered = node.children[0];
    } else {
      node.rendered = node;
    }
  }
  const distilled = constructed.roots
    .map((node) => node.rendered)
    .filter((node) => node !== null);
  const lines = [];
  const renderStack = distilled.map((node) => ({ node, depth: 0 })).reverse();
  let renderTruncated = false;
  while (renderStack.length) {
    if (lines.length >= MAX_LINES) {
      renderTruncated = true;
      break;
    }
    const { node, depth } = renderStack.pop();
    if (typeof node === 'string') {
      lines.push(`${'  '.repeat(depth)}- text: ${clippedName(node)}`);
      continue;
    }
    lines.push(`${'  '.repeat(depth)}- ${summary(node)}`);
    for (let index = node.children.length - 1; index >= 0; index -= 1) {
      renderStack.push({ node: node.children[index], depth: depth + 1 });
    }
  }
  let rendererIncomplete = null;
  if (constructed.incomplete || renderTruncated) {
    const causes = [];
    if (constructed.incomplete) {
      causes.push(`construction incomplete after ${constructed.covered} elements (${constructed.reason})`);
    }
    if (renderTruncated) causes.push('render truncated at 1200 lines');
    rendererIncomplete = `- … (snapshot ${causes.join('; ')})`;
    lines.push(rendererIncomplete);
  }
  return {
    outline: lines.join('\n'),
    units: lines,
    rendererIncomplete,
    nextRef: refCounter,
    refs,
  };
}
