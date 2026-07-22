import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
const nativePath = require.resolve('../native.cjs');
const cachedNative = require.cache[nativePath];

// The decoder is pure, so stub the native binding while loading its CJS export.
require.cache[nativePath] = {
  id: nativePath,
  filename: nativePath,
  loaded: true,
  exports: {}
};

let decodeWireValue;
try {
  ({ _decodeWireValue: decodeWireValue } = require('../index.cjs'));
} finally {
  if (cachedNative) require.cache[nativePath] = cachedNative;
  else delete require.cache[nativePath];
}

function decodeFixture(json) {
  return decodeWireValue(JSON.parse(json));
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
});

test('decodes RegExp, Date, URL, and Error wrappers', () => {
  const regexp = decodeFixture(
    '{"__rustwright_cdp_regexp__":{"p":"a+b\\\\s","f":"gi"}}'
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
    '{"__rustwright_cdp_error__":{"name":"TypeError","message":"boom","stack":"TypeError: boom\\n    at fixture.js:1:1"}}'
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

  assert.equal(decoded.label, 'root');
  assert.equal(decoded.items.length, 3);
  assert.equal(decoded.items[0], 1);
  assert.equal(decoded.items[1], undefined);
  assert.equal(decoded.items[2].big, 42n);
  assert.equal(decoded.items[2].date.toISOString(), '2026-01-02T03:04:05.000Z');
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
