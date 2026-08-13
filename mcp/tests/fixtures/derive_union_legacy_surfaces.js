'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const archiveRoot = process.argv[2];
assert.ok(archiveRoot, 'usage: node derive_union_legacy_surfaces.js <baseline-archive-root>');
const sourceRoot = path.join(archiveRoot, 'mcp', 'src');
const unionConsoleSource = fs.readFileSync(
  path.join(__dirname, 'rustwright-union-console.js'),
  'utf8',
);
const unionConsoleEmissionCount = unionConsoleSource
  .split('\n')
  .filter((line) => line.trimStart().startsWith('console.warn('))
  .length;
assert.equal(unionConsoleEmissionCount, 2);
// The baseline console Proxy forwards every call through an anonymous injected
// frame. Legacy presentation intentionally preserves that raw top-frame artifact.
const unionConsoleTemplate = Array.from({ length: unionConsoleEmissionCount })
  .map(() => 'WARNING (unknown):169 union duplicate')
  .join('\n');

const readPinned = (name, expectedHash) => {
  const bytes = fs.readFileSync(path.join(sourceRoot, name));
  assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'), expectedHash);
  return bytes.toString('utf8');
};

const snapshotSource = readPinned(
  'snapshot.js',
  'eca60d84bb5570bb20e6313d06acde2a6b36c0db8e5970ca594b31ee5cd98922',
);
const actorSource = readPinned(
  'actor.rs',
  '36b28ddf1f8698f30988c3b9d15e1724115c7a456eac3c29d19afface603f5fe',
);
assert.match(actorSource, /format!\(\s*"\{level\} \{location\} \{\}"/);
assert.match(actorSource, /"\[\{\}\] \{\} \{status\} \{\} \(\{\}\)"/);

class TextNode {
  constructor(value) {
    this.nodeType = 3;
    this.nodeValue = value;
    this.parentElement = null;
  }
}

class Element {
  constructor(tag, attrs = {}, children = []) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.namespaceURI = 'http://www.w3.org/1999/xhtml';
    this.parentElement = null;
    this.childNodes = [];
    this.attributes = new Map(Object.entries(attrs));
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
  }

  get children() { return this.childNodes.filter((child) => child.nodeType === 1); }
  get textContent() {
    return this.childNodes
      .map((child) => child.nodeType === 3 ? child.nodeValue : child.textContent)
      .join('');
  }
  get href() { return this.getAttribute('href') || ''; }
  get tabIndex() { return -1; }
  getAttribute(name) { return this.attributes.has(name) ? String(this.attributes.get(name)) : null; }
  hasAttribute(name) { return this.attributes.has(name); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  getBoundingClientRect() {
    return { left: 0, top: 0, right: 10, bottom: 10, width: 10, height: 10 };
  }
}

class Document {
  constructor(body) { this.body = body; }
  all() {
    const found = [];
    const stack = [this.body];
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
    return [];
  }
  querySelector() { return null; }
  getElementById() { return null; }
}

const el = (tag, attrs, children = []) => new Element(tag, attrs, children);
global.document = new Document(el('body', {}, [
  el('main', {}, [
    el('h1', {}, ['Network records']),
    el('img', { src: 'union-static.svg' }),
  ]),
]));
global.getComputedStyle = () => ({ display: 'block', visibility: 'visible', cursor: 'auto' });
const renderSnapshot = eval(`(${snapshotSource})`);
const rendered = renderSnapshot({
  startRef: 1,
  target: null,
  maxDepth: null,
  boxes: false,
  mask: '<masked>',
});
assert.deepEqual(rendered.refs, []);

process.stdout.write(`${JSON.stringify({
  schema_version: 1,
  baseline_commit: '815a6616b227c3a6373180c0528d19a96296a62b',
  navigation: rendered.outline,
  snapshot: rendered.outline,
  console_template: unionConsoleTemplate,
  network_template: '[1] GET 200 {PAGE_URL} (document)',
}, null, 2)}\n`);
