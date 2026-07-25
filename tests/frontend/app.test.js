import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const source = fs.readFileSync(new URL('../../web/js/app.js', import.meta.url), 'utf8');

test('network failures are visible and spoken locally', () => {
  assert.match(source, /el\.hidden = false/);
  assert.match(source, /speakInBrowser\(fallback, \{ loud: true \}\)/);
  assert.match(source, /call 911 now/);
});
