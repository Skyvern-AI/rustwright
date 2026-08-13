'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const sourceRoot = path.resolve(__dirname, '..', '..', 'src');
const snapshotSource = fs.readFileSync(path.join(sourceRoot, 'snapshot.js'), 'utf8');
const fixtureLimitsKey = Symbol.for('rustwright.mcp.snapshotFixtureLimits');
const fixtureSnapshotSource = snapshotSource
  .replace('const MAX_ELEMENTS = 50000;',
    `const MAX_ELEMENTS = globalThis[Symbol.for('rustwright.mcp.snapshotFixtureLimits')].elements;`)
  .replace('const MAX_CONSTRUCTION_MS = 250;',
    `const MAX_CONSTRUCTION_MS = globalThis[Symbol.for('rustwright.mcp.snapshotFixtureLimits')].timeMs;`)
  .replace('const now = () => performance.now();',
    `const now = globalThis[Symbol.for('rustwright.mcp.snapshotFixtureLimits')].now;`);
assert.notEqual(fixtureSnapshotSource, snapshotSource);
const snapshot = eval(`(${fixtureSnapshotSource})`);
const productionSnapshot = eval(`(${snapshotSource})`);
const legacy = eval(`(${fs.readFileSync(path.join(sourceRoot, 'snapshot_legacy.js'), 'utf8')})`);
const trackingKey = Symbol.for('rustwright.mcp.sensitiveSnapshot');
const nativePerformance = global.performance;
const defaultFixtureLimits = { elements: 50000, timeMs: 250, now: () => 0 };

class TextNode {
  constructor(value) {
    this.nodeType = 3;
    this.nodeValue = value;
    this.parentElement = null;
  }
}

class Element {
  constructor(tag, attrs = {}, children = [], style = {}) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.namespaceURI = 'http://www.w3.org/1999/xhtml';
    this.parentElement = null;
    this.childNodes = [];
    this.attributes = new Map(Object.entries(attrs).map(([key, value]) => [key, String(value)]));
    this._style = { display: 'block', visibility: 'visible', cursor: 'auto', ...style };
    this.disabled = false;
    this.checked = false;
    this.value = '';
    this.labels = [];
    this.isConnected = true;
    for (const child of children) this.append(child);
  }

  append(child) {
    const node = typeof child === 'string' ? new TextNode(child) : child;
    node.parentElement = this;
    this.childNodes.push(node);
    return this;
  }

  get children() {
    return this.childNodes.filter((child) => child.nodeType === 1);
  }

  get textContent() {
    return this.childNodes.map((child) => child.nodeType === 3 ? child.nodeValue : child.textContent).join('');
  }

  get href() {
    return this.getAttribute('href') || '';
  }

  get tabIndex() {
    return this.hasAttribute('tabindex') ? Number(this.getAttribute('tabindex')) : -1;
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  getBoundingClientRect() {
    return { left: 0, top: 0, right: 10, bottom: 10, width: 10, height: 10 };
  }
}

// Compact DOM nodes keep the mandated 50,000-element fixture focused on the
// snapshot algorithm rather than Map-heavy test-double bookkeeping.
class FlatElement {
  constructor(tag, name = null, children = []) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.namespaceURI = 'http://www.w3.org/1999/xhtml';
    this.parentElement = null;
    this.childNodes = children;
    this._name = name;
    this._ref = null;
    this._style = { display: 'block', visibility: 'visible', cursor: 'auto' };
    this.disabled = false;
    this.checked = false;
    this.value = '';
    this.labels = [];
    this.isConnected = true;
    for (const child of children) child.parentElement = this;
  }

  get children() { return this.childNodes; }
  get textContent() { return ''; }
  get href() { return ''; }
  get tabIndex() { return -1; }
  getAttribute(name) {
    if (name === 'aria-label') return this._name;
    if (name === 'data-mcp-ref') return this._ref;
    return null;
  }
  hasAttribute(name) { return name === 'data-mcp-ref' && this._ref !== null; }
  setAttribute(name, value) { if (name === 'data-mcp-ref') this._ref = String(value); }
  removeAttribute(name) { if (name === 'data-mcp-ref') this._ref = null; }
  getBoundingClientRect() {
    return { left: 0, top: 0, right: 10, bottom: 10, width: 10, height: 10 };
  }
}

class Document {
  constructor(body) {
    this.body = body;
    this.missingSelectors = new Set();
  }

  all() {
    const found = [];
    const stack = this.body ? [this.body] : [];
    while (stack.length) {
      const node = stack.pop();
      found.push(node);
      for (let index = node.children.length - 1; index >= 0; index -= 1) {
        stack.push(node.children[index]);
      }
    }
    return found;
  }

  querySelectorAll(selector) {
    if (selector === '[data-mcp-ref]') {
      return this.all().filter((node) => node.hasAttribute('data-mcp-ref'));
    }
    if (selector === '*') return this.all();
    return [];
  }

  querySelector(selector) {
    if (this.missingSelectors.has(selector)) return null;
    const ref = selector.match(/^\[data-mcp-ref="([^"]+)"\]$/);
    if (ref) return this.all().find((node) => node.getAttribute('data-mcp-ref') === ref[1]) || null;
    if (selector.startsWith('#')) {
      return this.all().find((node) => node.getAttribute('id') === selector.slice(1)) || null;
    }
    return null;
  }

  getElementById(id) {
    return this.all().find((node) => node.getAttribute('id') === id) || null;
  }
}

const el = (tag, attrs, children, style) => new Element(tag, attrs, children, style);
const options = (extra = {}) => ({
  startRef: 1,
  target: null,
  maxDepth: null,
  boxes: false,
  mask: 'MASKED',
  ...extra,
});

function run(renderer, body, extra = {}) {
  delete global[trackingKey];
  return runDocument(renderer, new Document(body), extra);
}

function runDocument(renderer, fixtureDocument, extra = {}) {
  const { testLimits = defaultFixtureLimits, ...rendererOptions } = extra;
  global[fixtureLimitsKey] = testLimits;
  global.document = fixtureDocument;
  global.getComputedStyle = (node) => node._style;
  global.performance = { now: () => 0 };
  return renderer(options(rendererOptions));
}

function runProduction(renderer, body, extra = {}) {
  delete global[trackingKey];
  global.document = new Document(body);
  global.getComputedStyle = (node) => node._style;
  global.performance = nativePerformance;
  return renderer({
    startRef: 1, target: null, maxDepth: null, boxes: false, mask: 'MASKED', ...extra,
  });
}

// String children merge before render.
{
  const result = run(snapshot, el('body', {}, [el('button', {}, ['Hello', ' world'])]));
  assert.match(result.outline, /- text: Hello world/);
  assert.equal((result.outline.match(/- text:/g) || []).length, 1);
}

// Nameless decorative images disappear, but click-root images survive.
{
  const decorative = el('img');
  const clickable = el('img', { onclick: 'go()' });
  const result = run(snapshot, el('body', {}, [decorative, clickable]));
  assert.equal(result.outline.includes('- img\n'), false);
  assert.match(result.outline, /- img \[ref=e1\]/);
}

// A name derived from rendered content is dropped in favor of the child text.
{
  const result = run(snapshot, el('body', {}, [el('button', {}, ['Go'])]));
  assert.doesNotMatch(result.outline, /button "Go"/);
  assert.match(result.outline, /button \[ref=e1\]\n  - text: Go/);
}

// A generic's sole text child is inlined.
{
  const result = run(snapshot, el('body', {}, [el('div', {}, ['Inline me'])]));
  assert.equal(result.outline, '- generic "Inline me"');
}

// A name-repeating generic child is removed.
{
  const child = el('div', { role: 'generic', 'aria-label': 'Repeated' });
  const result = run(snapshot, el('main', { 'aria-label': 'Repeated' }, [child]));
  assert.equal(result.outline, '- main "Repeated"');
}

// Single-child generics unwrap bottom-up, while click roots remain wrappers.
{
  const button = el('button', { 'aria-label': 'Deep action' });
  const result = run(snapshot, el('body', {}, [el('div', {}, [el('div', {}, [button])])]));
  assert.equal(result.outline, '- button "Deep action" [ref=e1]');
  const protectedResult = run(snapshot,
    el('body', {}, [el('div', { onclick: 'go()' }, [el('button', { 'aria-label': 'Child' })])]));
  assert.match(protectedResult.outline, /generic \[ref=e1\]\n  - button "Child" \[ref=e2\]/);
}

// Semantic, explicit roleless, and nearest pointer roots receive refs. A target
// known only to addEventListener has no discoverable marker and remains a miss.
{
  const pointerChild = el('span', {}, ['Pointer child'], { cursor: 'pointer' });
  const pointerRoot = el('div', {}, [pointerChild], { cursor: 'pointer' });
  const listenerOnly = el('div', {}, ['Listener only']);
  const propertyClick = el('div', {}, ['Property click']);
  propertyClick.onclick = () => {};
  const body = el('body', {}, [
    el('button', { 'aria-label': 'Semantic' }),
    el('div', { onclick: 'go()' }, ['Explicit']),
    pointerRoot,
    propertyClick,
    listenerOnly,
  ]);
  const result = run(snapshot, body);
  assert.deepEqual(result.refs, ['e1', 'e2', 'e3', 'e4']);
  assert.equal(pointerRoot.getAttribute('data-mcp-ref'), 'e3');
  assert.equal(pointerChild.getAttribute('data-mcp-ref'), null);
  assert.equal(propertyClick.getAttribute('data-mcp-ref'), 'e4');
  assert.equal(listenerOnly.getAttribute('data-mcp-ref'), null);
}

// Every concrete WAI-ARIA 1.2 widget role receives a semantic ref. Separator
// is interactive only when focusable, and role fallback lists use the first
// recognized token rather than treating the whole attribute as one role.
{
  const roles = [
    'button', 'checkbox', 'gridcell', 'link', 'menuitem', 'menuitemcheckbox',
    'menuitemradio', 'option', 'progressbar', 'radio', 'scrollbar', 'searchbox',
    'slider', 'spinbutton', 'switch', 'tab', 'tabpanel', 'textbox', 'treeitem',
    'combobox', 'grid', 'listbox', 'menu', 'menubar', 'radiogroup', 'tablist',
    'tree', 'treegrid',
  ];
  const widgets = roles.map((role) => el('div', { role, 'aria-label': role }));
  const separator = el('div', { role: 'separator', tabindex: '0' });
  const passiveSeparator = el('div', { role: 'separator' });
  const fallback = el('div', { role: 'unknown-token button', 'aria-label': 'Fallback' });
  const firstRecognized = el('div', { role: 'navigation button', 'aria-label': 'Navigation' });
  const result = run(snapshot, el('body', {}, [
    ...widgets, separator, passiveSeparator, fallback, firstRecognized,
  ]));
  assert.equal(result.refs.length, roles.length + 2);
  assert.equal(widgets.every((node) => node.hasAttribute('data-mcp-ref')), true);
  assert.equal(separator.hasAttribute('data-mcp-ref'), true);
  assert.equal(passiveSeparator.hasAttribute('data-mcp-ref'), false);
  assert.equal(fallback.hasAttribute('data-mcp-ref'), true);
  assert.equal(firstRecognized.hasAttribute('data-mcp-ref'), false);
  assert.match(result.outline, /button "Fallback" \[ref=e\d+\]/);
  assert.match(result.outline, /navigation "Navigation"/);
}

// Semantic marker precedence is observable: an element that also has explicit
// and pointer markers must not inspect either lower-priority marker.
{
  const overlapping = el('div', { role: 'button', onclick: 'go()', 'aria-label': 'Winner' });
  let explicitReads = 0;
  const hasAttribute = overlapping.hasAttribute.bind(overlapping);
  overlapping.hasAttribute = (name) => {
    if (name === 'onclick') explicitReads += 1;
    return hasAttribute(name);
  };
  let pointerReads = 0;
  overlapping._style = {
    display: 'block', visibility: 'visible',
    get cursor() { pointerReads += 1; return 'pointer'; },
  };
  const result = run(snapshot, el('body', {}, [overlapping]));
  assert.equal(result.outline, '- button "Winner" [ref=e1]');
  assert.equal(explicitReads, 0);
  assert.equal(pointerReads, 0);
}

// The element valve completes at the exact boundary and fires at boundary + 1.
{
  const first = el('button', { 'aria-label': 'First' });
  const exact = run(snapshot, el('body', {}, [first]), {
    testLimits: { elements: 2, timeMs: 250, now: () => 0 },
  });
  assert.equal(exact.rendererIncomplete, null);
  assert.deepEqual(exact.refs, ['e1']);

  const overflowFirst = el('button', { 'aria-label': 'First' });
  const second = el('button', { 'aria-label': 'Second' });
  const overflow = run(snapshot, el('body', {}, [overflowFirst, second]), {
    testLimits: { elements: 2, timeMs: 250, now: () => 0 },
  });
  assert.match(overflow.rendererIncomplete, /after 2 elements \(element limit\)/);
  assert.deepEqual(overflow.refs, ['e1']);
  assert.equal(second.getAttribute('data-mcp-ref'), null);
}

// The wall-time valve is deterministic under the fixture clock.
{
  let tick = 0;
  const result = run(snapshot, el('body', {}, [el('button', { 'aria-label': 'Late' })]), {
    testLimits: { elements: 50000, timeMs: 2, now: () => tick++ },
  });
  assert.match(result.rendererIncomplete, /after 1 elements \(wall time\)/);
  assert.deepEqual(result.refs, []);
}

// Find searches the constructed subset, caps paths, carries current refs, and
// reports incomplete coverage without returning an outline/tree.
{
  const exactButtons = Array.from({ length: 20 }, (_, index) =>
    el('button', { 'aria-label': `Needle ${index}` }));
  const exact = run(snapshot, el('body', {}, exactButtons), {
    find: { kind: 'text', value: 'needle' },
  });
  assert.equal(exact.find.matches.length, 20);
  assert.equal(exact.find.totalMatches, 20);

  const overflowButtons = Array.from({ length: 21 }, (_, index) =>
    el('button', { 'aria-label': `Needle ${index}` }));
  const capped = run(snapshot, el('body', {}, overflowButtons), {
    find: { kind: 'text', value: 'needle' },
  });
  assert.equal(capped.find.matches.length, 20);
  assert.equal(capped.find.totalMatches, 21);
  assert.equal(Object.hasOwn(capped, 'outline'), false);
  for (const match of capped.find.matches) {
    assert.notEqual(match.ref, null);
    assert.equal(capped.refs.includes(match.ref), true);
    assert.equal(match.line.includes(`[ref=${match.ref}]`), true);
    assert.equal(global.document.querySelector(`[data-mcp-ref="${match.ref}"]`) !== null, true);
  }

  const regex = run(snapshot, el('body', {}, [
    el('button', { 'aria-label': 'Alpha 42' }),
    el('button', { 'aria-label': 'Beta' }),
  ]), { find: { kind: 'regex', pattern: '^button Alpha \\d+$', flags: 'i' } });
  assert.equal(regex.find.totalMatches, 1);
  assert.equal(regex.find.matches[0].ref, regex.refs[0]);

  const subsetButtons = Array.from({ length: 10 }, (_, index) =>
    el('button', { 'aria-label': `Subset ${index}` }));
  const subset = run(snapshot, el('body', {}, subsetButtons), {
    find: { kind: 'text', value: 'subset' },
    testLimits: { elements: 5, timeMs: 250, now: () => 0 },
  });
  assert.equal(subset.find.incomplete, true);
  assert.equal(subset.find.coveredElements, 5);
  assert.equal(subset.find.matches.length, 4);
  assert.deepEqual(subset.refs, ['e1', 'e2', 'e3', 'e4']);
}

// Ref lifecycle is transactional across repeated calls on the same document.
// Excluded nodes lose stale DOM refs, and a target disappearing between actor
// validation and page evaluation clears every surviving old ref.
{
  const excluded = el('button', { 'aria-label': 'Excluded' });
  const survivor = el('button', { 'aria-label': 'Survivor' });
  const fixtureDocument = new Document(el('body', {}, [excluded, survivor]));
  const first = runDocument(snapshot, fixtureDocument);
  assert.deepEqual(first.refs, ['e1', 'e2']);
  excluded.setAttribute('aria-hidden', 'true');
  const second = runDocument(snapshot, fixtureDocument, { startRef: first.nextRef });
  assert.equal(excluded.getAttribute('data-mcp-ref'), null);
  assert.equal(second.refs.includes('e1'), false);
  assert.deepEqual(second.refs, ['e3']);

  const targetSelector = '[data-mcp-ref="e3"]';
  fixtureDocument.missingSelectors.add(targetSelector);
  const missing = runDocument(snapshot, fixtureDocument, {
    startRef: second.nextRef,
    target: targetSelector,
  });
  assert.equal(missing.outline, '- (snapshot target is no longer available)');
  assert.deepEqual(missing.refs, []);
  assert.equal(survivor.getAttribute('data-mcp-ref'), null);
}

// Invalid regex syntax and other page-side RegExp throws happen before old DOM
// refs are cleared, so an actor that receives the error can retain its registry.
{
  const button = el('button', { 'aria-label': 'Stable' });
  const fixtureDocument = new Document(el('body', {}, [button]));
  const first = runDocument(snapshot, fixtureDocument);
  assert.equal(button.getAttribute('data-mcp-ref'), 'e1');
  // parse_regex("/[/") produces this exact page payload.
  assert.throws(() => runDocument(snapshot, fixtureDocument, {
    startRef: first.nextRef,
    find: { kind: 'regex', pattern: '[', flags: '' },
  }), SyntaxError);
  assert.equal(button.getAttribute('data-mcp-ref'), 'e1');

  const NativeRegExp = global.RegExp;
  global.RegExp = function PagePatchedRegExp() { throw new Error('page-side RegExp failure'); };
  try {
    assert.throws(() => runDocument(snapshot, fixtureDocument, {
      startRef: first.nextRef,
      find: { kind: 'regex', pattern: 'stable', flags: 'i' },
    }), /page-side RegExp failure/);
  } finally {
    global.RegExp = NativeRegExp;
  }
  assert.equal(button.getAttribute('data-mcp-ref'), 'e1');
}

// Frozen legacy corpus: exact outline bytes, registry/ref map, nextRef, DOM
// attributes, missing-root responses, truncation metadata, and multi-call refs.
{
  const record = (result, fixtureDocument) => ({
    outline: result.outline,
    units: result.units,
    rendererIncomplete: result.rendererIncomplete,
    refs: result.refs,
    refMap: Object.fromEntries(fixtureDocument.all()
      .filter((node) => node.hasAttribute('data-mcp-ref'))
      .map((node) => [node.getAttribute('data-mcp-ref'), node.getAttribute('id')])),
    domRefs: fixtureDocument.all().filter((node) => node.getAttribute('id'))
      .map((node) => [node.getAttribute('id'), node.getAttribute('data-mcp-ref')]),
    nextRef: result.nextRef,
  });
  const semanticDocument = new Document(el('body', {}, [
    el('main', { id: 'workspace', 'aria-label': 'Workspace' }, [
      el('h1', { id: 'heading' }, ['Dashboard']),
      el('button', { id: 'save', 'aria-label': 'Save' }),
      el('a', { id: 'details', href: '/full/path?x=1' }, ['Details']),
    ]),
  ]));
  const semantic = record(runDocument(legacy, semanticDocument), semanticDocument);
  assert.deepEqual(semantic, {
    outline: '- main "Workspace"\n  - heading "Dashboard" [level=1]\n  - button "Save" [ref=e1]\n  - link "Details" [href=/full/path?x=1] [ref=e2]',
    units: [
      '- main "Workspace"',
      '  - heading "Dashboard" [level=1]',
      '  - button "Save" [ref=e1]',
      '  - link "Details" [href=/full/path?x=1] [ref=e2]',
    ],
    rendererIncomplete: null,
    refs: ['e1', 'e2'],
    refMap: { e1: 'save', e2: 'details' },
    domRefs: [
      ['workspace', null], ['heading', null], ['save', 'e1'], ['details', 'e2'],
    ],
    nextRef: 3,
  });

  const structureDocument = new Document(el('body', {}, [
    el('div', { id: 'loose' }, ['Loose text']),
    el('img', { id: 'chart', alt: 'Chart' }),
    el('iframe', { id: 'frame', src: '/frame' }),
  ]));
  const structure = record(runDocument(legacy, structureDocument), structureDocument);
  assert.deepEqual(structure, {
    outline: '- text: Loose text\n- img "Chart"\n- iframe "/frame" (content not captured)',
    units: ['- text: Loose text', '- img "Chart"', '- iframe "/frame" (content not captured)'],
    rendererIncomplete: null,
    refs: [],
    refMap: {},
    domRefs: [['loose', null], ['chart', null], ['frame', null]],
    nextRef: 1,
  });

  const missingBody = record(runDocument(legacy, new Document(null)), new Document(null));
  assert.deepEqual(missingBody, {
    outline: '- (page has no body yet)', units: ['- (page has no body yet)'],
    rendererIncomplete: null, refs: [], refMap: {}, domRefs: [], nextRef: 1,
  });
  const missingTargetDocument = new Document(el('body', {}, [
    el('button', { id: 'old', 'data-mcp-ref': 'e6', 'aria-label': 'Old' }),
  ]));
  const missingTarget = record(runDocument(legacy, missingTargetDocument, {
    startRef: 7, target: '#gone',
  }), missingTargetDocument);
  assert.deepEqual(missingTarget, {
    outline: '- (snapshot target is no longer available)',
    units: ['- (snapshot target is no longer available)'],
    rendererIncomplete: null, refs: [], refMap: {}, domRefs: [['old', null]], nextRef: 7,
  });

  const truncatedDocument = new Document(el('body', {},
    Array.from({ length: 1200 }, () => el('main', { 'aria-label': 'Repeated' }))));
  const truncated = runDocument(legacy, truncatedDocument);
  const expectedLines = Array.from({ length: 1200 }, () => '- main "Repeated"');
  assert.equal(truncated.outline, `${expectedLines.join('\n')}\n- … (snapshot truncated)`);
  assert.deepEqual(truncated.units, [...expectedLines, '- … (snapshot truncated)']);
  assert.equal(truncated.rendererIncomplete, '- … (snapshot truncated)');
  assert.deepEqual(truncated.refs, []);
  assert.equal(truncated.nextRef, 1);

  const multiDocument = new Document(el('body', {}, [
    el('button', { id: 'first', 'aria-label': 'First' }),
    el('a', { id: 'second', href: '/second', 'aria-label': 'Second' }),
  ]));
  const first = record(runDocument(legacy, multiDocument), multiDocument);
  const second = record(runDocument(legacy, multiDocument, { startRef: first.nextRef }), multiDocument);
  assert.deepEqual([first, second], [
    {
      outline: '- button "First" [ref=e1]\n- link "Second" [href=/second] [ref=e2]',
      units: ['- button "First" [ref=e1]', '- link "Second" [href=/second] [ref=e2]'],
      rendererIncomplete: null, refs: ['e1', 'e2'], refMap: { e1: 'first', e2: 'second' },
      domRefs: [['first', 'e1'], ['second', 'e2']], nextRef: 3,
    },
    {
      outline: '- button "First" [ref=e3]\n- link "Second" [href=/second] [ref=e4]',
      units: ['- button "First" [ref=e3]', '- link "Second" [href=/second] [ref=e4]'],
      rendererIncomplete: null, refs: ['e3', 'e4'], refMap: { e3: 'first', e4: 'second' },
      domRefs: [['first', 'e3'], ['second', 'e4']], nextRef: 5,
    },
  ]);
}

// Sensitive tracking masks before construction-derived names, attrs, paths,
// refs, find results, and final rendering. Hrefs remain exact when not masked.
{
  const secret = 'do-not-render-anywhere';
  const sensitive = el('input', {
    type: 'text', role: 'textbox', 'aria-label': secret, title: secret,
  });
  sensitive.value = secret;
  const fixtureDocument = new Document(el('body', {}, [
    el('div', {}, [sensitive]),
  ]));
  global[trackingKey] = {
    sensitiveNodes: new WeakSet([sensitive]),
    sensitiveNodeRefs: new Set([new WeakRef(sensitive)]),
  };
  const found = runDocument(snapshot, fixtureDocument, {
    find: { kind: 'text', value: secret },
  });
  assert.equal(found.find.totalMatches, 0);
  assert.equal(JSON.stringify(found).includes(secret), false);
  const rendered = runDocument(snapshot, fixtureDocument, { startRef: found.nextRef });
  assert.equal(JSON.stringify(rendered).includes(secret), false);
  assert.match(rendered.outline, /textbox \[value=MASKED\] \[ref=e2\]/);

  const href = `https://example.invalid/${'x'.repeat(400)}`;
  const exactUrl = run(snapshot, el('body', {}, [el('a', { href }, ['Long URL'])]));
  assert.equal(exactUrl.outline.includes(`[href=${href}]`), true);
}

// Iterative construction reaches a deeply nested lower-page control.
{
  const button = el('button', { 'aria-label': 'Lower page target' });
  let root = button;
  for (let index = 0; index < 20000; index += 1) root = el('div', {}, [root]);
  const result = run(snapshot, el('body', {}, [root]));
  assert.equal(result.outline, '- button "Lower page target" [ref=e1]');
  assert.equal(result.rendererIncomplete, null);
  assert.equal(result.units.length, 1);
}

// The production-default branch uses performance.now and completes a small DOM
// without either the 50,000-element or 250ms construction valve firing.
{
  const result = runProduction(productionSnapshot,
    el('body', {}, [el('button', { 'aria-label': 'Production defaults' })]));
  assert.equal(result.rendererIncomplete, null);
  assert.equal(result.outline, '- button "Production defaults" [ref=e1]');
}

// The 1,200-line valve is post-distillation: 1,200 completes and 1,201 fires.
// The 20,002-node deep case above separately proves >1,200 raw nodes may
// distill to one fully rendered line.
{
  const exactButtons = Array.from({ length: 1199 }, (_, index) =>
    el('button', { 'aria-label': `Action ${index}` }));
  const exact = run(snapshot, el('main', { 'aria-label': 'Actions' }, exactButtons));
  assert.equal(exact.rendererIncomplete, null);
  assert.equal(exact.units.length, 1200);

  const overflowButtons = Array.from({ length: 1200 }, (_, index) =>
    el('button', { 'aria-label': `Action ${index}` }));
  const overflow = run(snapshot, el('main', { 'aria-label': 'Actions' }, overflowButtons));
  assert.match(overflow.rendererIncomplete, /render truncated at 1200 lines/);
  assert.equal(overflow.units.length, 1201);
}

// Production element-limit truth at 49,999 / 50,000 / 50,001 elements. The
// The exact-cap DOM also satisfies the spec's 50,000-element snapshot/find
// fixture: construction must be complete and find must cover the full set. The
// historical wall-clock check is opt-in because shared CI load is not semantic.
{
  const limits = { elements: 50000, timeMs: 250, now: () => 0 };
  const allChildren = Array.from({ length: 50000 }, () => new FlatElement('button', 'Full set'));
  const root = new FlatElement('body');
  const fixtureDocument = new Document(root);
  const setSize = (count) => {
    root.childNodes = allChildren.slice(0, count);
    for (const child of root.childNodes) child.parentElement = root;
  };

  setSize(49998);
  const below = runDocument(snapshot, fixtureDocument, { testLimits: limits });
  assert.equal((below.rendererIncomplete || '').includes('construction incomplete'), false);

  setSize(49999);
  let started = nativePerformance.now();
  const exact = runDocument(snapshot, fixtureDocument, { testLimits: limits });
  const snapshotMs = nativePerformance.now() - started;
  assert.equal((exact.rendererIncomplete || '').includes('construction incomplete'), false);
  assert.equal(exact.refs.length, 49999);
  assert.equal(exact.nextRef, 50000);
  assert.equal(allChildren.slice(0, 49999).every((child, index) =>
    child.getAttribute('data-mcp-ref') === `e${index + 1}`), true);
  if (process.env.RUSTWRIGHT_MCP_SNAPSHOT_PERF_ASSERT === '1') {
    assert.ok(snapshotMs < 30000, `50,000-element snapshot took ${snapshotMs}ms`);
  }

  started = nativePerformance.now();
  const found = runDocument(snapshot, fixtureDocument, {
    startRef: 1,
    find: { kind: 'text', value: 'full set' },
    testLimits: limits,
  });
  const findMs = nativePerformance.now() - started;
  assert.equal(found.find.incomplete, false);
  assert.equal(found.find.coveredElements, 50000);
  assert.equal(found.find.totalMatches, 49999);
  assert.equal(found.find.matches.length, 20);
  assert.equal(found.refs.length, 49999);
  assert.equal(found.nextRef, 50000);
  for (const match of found.find.matches) {
    assert.notEqual(match.ref, null);
    assert.equal(found.refs.includes(match.ref), true);
    assert.equal(match.line.includes(`[ref=${match.ref}]`), true);
    assert.equal(fixtureDocument.querySelector(`[data-mcp-ref="${match.ref}"]`) !== null, true);
  }
  assert.equal(allChildren.slice(0, 49999).every((child, index) =>
    child.getAttribute('data-mcp-ref') === `e${index + 1}`), true);
  if (process.env.RUSTWRIGHT_MCP_SNAPSHOT_PERF_ASSERT === '1') {
    assert.ok(findMs < 30000, `50,000-element find took ${findMs}ms`);
  }

  setSize(50000);
  const overflow = runDocument(snapshot, fixtureDocument, { testLimits: limits });
  assert.match(overflow.rendererIncomplete, /construction incomplete after 50000 elements/);
}

console.log('snapshot W2 deterministic fixtures: PASS');
