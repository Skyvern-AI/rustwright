(options) => {
  const MAX_NAME = 120;
  const MAX_LINES = 1200;
  const {
    startRef,
    target = null,
    maxDepth = null,
    boxes = false,
    // Deliberately undefaulted: the mask glyph is owned by SECRET_MASK on the
    // Rust side and passed in, so a second literal here cannot drift from it.
    mask,
  } = options;
  const root = target === null ? document.body : document.querySelector(target);
  let refCounter = startRef;
  const lines = [];
  const refs = [];
  const trackingKey = Symbol.for('rustwright.mcp.sensitiveSnapshot');
  const tracking = globalThis[trackingKey];
  const sensitiveNodes = new Set();
  const aggregateSensitive = new Set();
  if (tracking
      && tracking.sensitiveNodes instanceof WeakSet
      && tracking.sensitiveNodeRefs instanceof Set) {
    for (const reference of tracking.sensitiveNodeRefs) {
      const node = reference && typeof reference.deref === 'function'
        ? reference.deref()
        : null;
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

  for (const el of document.querySelectorAll('[data-mcp-ref]')) {
    el.removeAttribute('data-mcp-ref');
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
  const SKIP_TAGS = new Set([
    'SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'META', 'LINK', 'HEAD', 'SVG', 'PATH',
  ]);

  const isVisible = (el) => {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0 || el.tagName === 'OPTION';
  };

  const containsSensitiveNode = (el) => sensitiveNodes.has(el);
  // The resolver taints exactly the nodes whose rendered content holds the
  // secret, ancestors included. An ancestor it did not taint is unsafe only
  // where its name is *derived* from a tainted descendant's text, so the
  // precomputed ancestor closure guards the aggregate paths alone. Blanking
  // every ancestor outright also erased author-static labels, which cannot
  // contain a secret typed after the page was written and are the caller's
  // only handle on the field.
  const hasSensitiveDescendant = (el) => aggregateSensitive.has(el);

  const roleOf = (el) => {
    if (!containsSensitiveNode(el)) {
      const explicit = el.getAttribute('role');
      if (explicit) return explicit;
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

  const nameOf = (el) => {
    if (containsSensitiveNode(el)) return '';
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const labelledNodes = labelled.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean);
      if (labelledNodes.some(
        (node) => containsSensitiveNode(node) || hasSensitiveDescendant(node),
      )) return '';
      const parts = labelledNodes.map((node) => node.textContent.trim());
      if (parts.length) return parts.join(' ');
    }
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) return ariaLabel;
    if (el.labels && el.labels.length) {
      if (Array.from(el.labels).some(
        (label) => containsSensitiveNode(label) || hasSensitiveDescendant(label),
      )) return '';
      return el.labels[0].textContent.trim();
    }
    const direct = el.getAttribute('alt') || el.getAttribute('title')
      || el.getAttribute('placeholder');
    if (direct) return direct;
    if (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') {
      return el.getAttribute('name') || '';
    }
    if (hasSensitiveDescendant(el)) return '';
    return (el.textContent || '').trim().replace(/\s+/g, ' ');
  };

  const isInteractive = (el, role) =>
    ['link', 'button', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio',
      'slider', 'option', 'tab', 'menuitem', 'switch'].includes(role)
    || el.hasAttribute('onclick') || el.tabIndex >= 0;

  const enclosingBox = (rect) => {
    if (rect.width <= 0 || rect.height <= 0) return null;
    const left = Math.floor(rect.left);
    const top = Math.floor(rect.top);
    const right = Math.ceil(rect.right);
    const bottom = Math.ceil(rect.bottom);
    return [left, top, right - left, bottom - top];
  };

  const walk = (el, treeDepth) => {
    if (lines.length >= MAX_LINES) return;
    if (maxDepth !== null && treeDepth > maxDepth) return;
    const tag = String(el.tagName || '').toUpperCase();
    if (SKIP_TAGS.has(tag) || el.namespaceURI === 'http://www.w3.org/2000/svg') return;
    if (!isVisible(el)) return;
    if (tag === 'IFRAME' || tag === 'FRAME') {
      const label = containsSensitiveNode(el)
        ? ''
        : el.getAttribute('title') || el.getAttribute('name')
          || el.getAttribute('src') || '';
      lines.push(`${'  '.repeat(treeDepth)}- iframe "${label.slice(0, MAX_NAME)}" (content not captured)`);
      return;
    }

    const role = roleOf(el);
    let childDepth = treeDepth;
    if (role) {
      let name = nameOf(el);
      if (name.length > MAX_NAME) name = `${name.slice(0, MAX_NAME)}…`;
      const parts = [`${'  '.repeat(treeDepth)}- ${role}`];
      if (name) parts.push(`"${name}"`);
      if (/^H[1-6]$/.test(el.tagName)) parts.push(`[level=${el.tagName[1]}]`);
      if (el.tagName === 'A' && el.href && !containsSensitiveNode(el)) {
        parts.push(`[href=${el.getAttribute('href')}]`);
      }
      if (el.disabled) parts.push('[disabled]');
      if (el.checked) parts.push('[checked]');
      if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.value) {
        const isPassword = el.tagName === 'INPUT'
          && (el.getAttribute('type') || 'text').toLowerCase() === 'password';
        parts.push(isPassword || containsSensitiveNode(el)
          ? `[value=${mask}]`
          : `[value="${String(el.value).slice(0, 60)}"]`);
      }
      if (isInteractive(el, role)) {
        const ref = `e${refCounter}`;
        refCounter += 1;
        el.setAttribute('data-mcp-ref', ref);
        refs.push(ref);
        parts.push(`[ref=${ref}]`);
      }
      if (boxes) {
        const box = enclosingBox(el.getBoundingClientRect());
        if (box) parts.push(`[box=${box.join(',')}]`);
      }
      lines.push(parts.join(' '));
      childDepth = treeDepth + 1;
      const hasElementChildren = el.children.length > 0;
      if (!hasElementChildren || ['link', 'button', 'heading', 'option', 'label'].includes(role)) {
        return;
      }
    } else if (el.children.length === 0) {
      const text = (el.textContent || '').trim().replace(/\s+/g, ' ');
      if (text && !containsSensitiveNode(el)) {
        lines.push(`${'  '.repeat(treeDepth)}- text: ${text.slice(0, MAX_NAME)}`);
      }
      return;
    }
    for (const child of el.children) walk(child, childDepth);
  };

  if (!root) {
    return {
      outline: target === null
        ? '- (page has no body yet)'
        : '- (snapshot target is no longer available)',
      units: [target === null
        ? '- (page has no body yet)'
        : '- (snapshot target is no longer available)'],
      rendererIncomplete: null,
      nextRef: refCounter,
      refs,
    };
  }
  walk(root, 0);
  const rendererIncomplete = lines.length >= MAX_LINES ? '- … (snapshot truncated)' : null;
  if (rendererIncomplete !== null) lines.push(rendererIncomplete);
  return { outline: lines.join('\n'), units: lines, rendererIncomplete, nextRef: refCounter, refs };
}
