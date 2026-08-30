import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
const { _decodeWireValue: decodeWireValue } = require('../index.cjs');

function decodeFixture(json) {
  return decodeWireValue(json);
}

test('decodes core unserializable number and bigint markers', () => {
  assert.ok(Number.isNaN(decodeFixture(
    '{"__rustwright_cdp_unserializable_value__":"NaN"}'
  )));
  assert.equal(decodeFixture(
    '{"__rustwright_cdp_unserializable_value__":"Infinity"}'
  ), Infinity);
  assert.equal(decodeFixture(
    '{"__rustwright_cdp_unserializable_value__":"-Infinity"}'
  ), -Infinity);
  assert.ok(Object.is(decodeFixture(
    '{"__rustwright_cdp_unserializable_value__":"-0"}'
  ), -0));
  assert.equal(decodeFixture(
    '{"__rustwright_cdp_unserializable_value__":"9007199254740993n"}'
  ), 9007199254740993n);
  assert.equal(decodeFixture(
    '{"__rustwright_cdp_bigint__":"9007199254740993n"}'
  ), 9007199254740993n);
  assert.deepEqual(
    decodeFixture('{"__rustwright_cdp_unserializable_value__":"future-marker"}'),
    { __rustwright_cdp_unserializable_value__: 'future-marker' }
  );
});

test('decodes RegExp, Date, URL, and Error wrappers', () => {
  const regexp = decodeFixture(
    '{"__rustwright_cdp_regexp__":{"f":"gi","p":"a+b\\\\s"}}'
  );
  assert.ok(regexp instanceof RegExp);
  assert.equal(regexp.source, 'a+b\\s');
  assert.equal(regexp.flags, 'gi');

  const date = decodeFixture(
    '{"__rustwright_cdp_date__":"2026-07-21T12:34:56.789Z"}'
  );
  assert.ok(date instanceof Date);
  assert.equal(date.toISOString(), '2026-07-21T12:34:56.789Z');

  const url = decodeFixture(
    '{"__rustwright_cdp_url__":"https://example.com/path?q=wire#value"}'
  );
  assert.ok(url instanceof URL);
  assert.equal(url.href, 'https://example.com/path?q=wire#value');

  const error = decodeFixture(
    '{"__rustwright_cdp_error__":{"stack":"TypeError: boom\\n    at fixture.js:1:1","message":"boom","name":"TypeError"}}'
  );
  assert.ok(error instanceof Error);
  assert.equal(error.name, 'TypeError');
  assert.equal(error.message, 'boom');
  assert.equal(error.stack, 'TypeError: boom\n    at fixture.js:1:1');

  const emptyStackError = decodeFixture(
    '{"__rustwright_cdp_error__":{"name":"Error","message":"","stack":""}}'
  );
  assert.equal(emptyStackError.stack, '');
});

test('decodes undefined, symbol, and function wrappers as undefined', () => {
  assert.equal(decodeFixture('{"__rustwright_cdp_undefined__":true}'), undefined);
  assert.equal(decodeFixture('{"__rustwright_cdp_symbol__":true}'), undefined);
  assert.equal(decodeFixture('{"__rustwright_cdp_function__":true}'), undefined);
});

test('decodes nested array items and object entries wrappers', () => {
  const decoded = decodeFixture(`{
    "__rustwright_cdp_object__": 1,
    "entries": {
      "z": "last",
      "a": "first",
      "label": "root",
      "items": {
        "__rustwright_cdp_array__": 2,
        "items": [
          1,
          {"__rustwright_cdp_undefined__": true},
          {
            "__rustwright_cdp_object__": 3,
            "entries": {
              "big": {"__rustwright_cdp_unserializable_value__": "42n"},
              "date": {"__rustwright_cdp_date__": "2026-01-02T03:04:05.000Z"}
            }
          }
        ]
      }
    }
  }`);

  assert.deepEqual(Object.keys(decoded).slice(0, 2), ['z', 'a']);
  assert.equal(decoded.label, 'root');
  assert.equal(decoded.items.length, 3);
  assert.equal(decoded.items[0], 1);
  assert.equal(decoded.items[1], undefined);
  assert.equal(decoded.items[2].big, 42n);
  assert.equal(decoded.items[2].date.toISOString(), '2026-01-02T03:04:05.000Z');
});

test('defines decoded object entries as own data properties', () => {
  const decoded = decodeFixture(`{
    "__rustwright_cdp_object__": 1,
    "entries": {
      "__proto__": {
        "__rustwright_cdp_object__": 2,
        "entries": {
          "isAdmin": true
        }
      },
      "nul\\u0000key": "nul"
    }
  }`);
  const nulKey = `nul${String.fromCharCode(0)}key`;

  assert.equal(Object.hasOwn(decoded, '__proto__'), true);
  assert.equal(Object.hasOwn(decoded.__proto__, 'isAdmin'), true);
  assert.equal(decoded.__proto__.isAdmin, true);
  assert.equal(Object.getPrototypeOf(decoded), Object.prototype);
  assert.equal(decoded.isAdmin, undefined);
  assert.equal(Object.hasOwn(decoded, nulKey), true);
  assert.equal(decoded[nulKey], 'nul');
});

test('resolves ref wrappers for shared values and cycles', () => {
  const decoded = decodeFixture(`{
    "__rustwright_cdp_object__": 1,
    "entries": {
      "self": {"__rustwright_cdp_ref__": 1},
      "children": {
        "__rustwright_cdp_array__": 2,
        "items": [
          {"__rustwright_cdp_ref__": 1},
          {"__rustwright_cdp_ref__": 2}
        ]
      },
      "sameChildren": {"__rustwright_cdp_ref__": 2}
    }
  }`);

  assert.equal(decoded.self, decoded);
  assert.equal(decoded.children[0], decoded);
  assert.equal(decoded.children[1], decoded.children);
  assert.equal(decoded.sameChildren, decoded.children);
});
