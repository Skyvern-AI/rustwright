"""In-page snapshot scripts.

The page and targeted variants share one implementation. They produce a
compact accessibility-style outline, stamp current ``data-mcp-ref`` handles,
optionally bound traversal depth, and optionally add viewport-relative boxes.
"""

_SNAPSHOT_BODY = r"""
  const MAX_NAME = 120;
  const MAX_LINES = 1200;
  const {startRef, maxDepth = null, boxes = false} = options;
  let refCounter = startRef;
  const lines = [];
  const refs = [];

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
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'META', 'LINK', 'HEAD', 'SVG', 'PATH']);

  const isVisible = (el) => {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0 || el.tagName === 'OPTION';
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    if (el.tagName === 'INPUT') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      return INPUT_ROLES[type] || 'textbox';
    }
    return ROLE_BY_TAG[el.tagName] || null;
  };

  const nameOf = (el) => {
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const parts = labelled.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((node) => node.textContent.trim());
      if (parts.length) return parts.join(' ');
    }
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) return ariaLabel;
    if (el.labels && el.labels.length) return el.labels[0].textContent.trim();
    const direct = el.getAttribute('alt') || el.getAttribute('title')
      || el.getAttribute('placeholder');
    if (direct) return direct;
    if (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') {
      return el.getAttribute('name') || '';
    }
    return (el.textContent || '').trim().replace(/\s+/g, ' ');
  };

  const isInteractive = (el, role) =>
    ['link', 'button', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio',
     'slider', 'option', 'tab', 'menuitem', 'switch'].includes(role)
    || el.hasAttribute('onclick') || el.tabIndex >= 0;

  const enclosingBox = (rect) => {
    if (rect.width <= 0 || rect.height <= 0) return null;
    // Enclose fractional CSS-pixel geometry: floor the origin and ceil the far
    // edge. This produces integer viewport-relative bounds without shrinking
    // the rendered area.
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
      const label = el.getAttribute('title') || el.getAttribute('name') || el.getAttribute('src') || '';
      lines.push(`${'  '.repeat(treeDepth)}- iframe "${label.slice(0, MAX_NAME)}" (content not captured)`);
      return;
    }

    const role = roleOf(el);
    let childDepth = treeDepth;
    if (role) {
      let name = nameOf(el);
      if (name.length > MAX_NAME) name = name.slice(0, MAX_NAME) + '…';
      const parts = [`${'  '.repeat(treeDepth)}- ${role}`];
      if (name) parts.push(`"${name}"`);
      if (/^H[1-6]$/.test(el.tagName)) parts.push(`[level=${el.tagName[1]}]`);
      if (el.tagName === 'A' && el.href) parts.push(`[href=${el.getAttribute('href')}]`);
      if (el.disabled) parts.push('[disabled]');
      if (el.checked) parts.push('[checked]');
      if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.value) {
        const isPassword = el.tagName === 'INPUT'
          && (el.getAttribute('type') || 'text').toLowerCase() === 'password';
        parts.push(isPassword
          ? '[value=••••••]'
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
      if (text) lines.push(`${'  '.repeat(treeDepth)}- text: ${text.slice(0, MAX_NAME)}`);
      return;
    }
    for (const child of el.children) walk(child, childDepth);
  };

  if (!root) {
    return {outline: '- (page has no body yet)', nextRef: refCounter, refs};
  }
  walk(root, 0);
  if (lines.length >= MAX_LINES) lines.push('- … (snapshot truncated)');
  return {outline: lines.join('\n'), nextRef: refCounter, refs};
"""

SNAPSHOT_JS = "(options) => { const root = document.body;" + _SNAPSHOT_BODY + "}"
TARGET_SNAPSHOT_JS = "(root, options) => {" + _SNAPSHOT_BODY + "}"
