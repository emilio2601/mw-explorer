const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const CoverageFrames = vm.runInNewContext(fs.readFileSync('static/frame-cache.js', 'utf8') + '\nCoverageFrames',
  { AbortController, DataView, setTimeout, clearTimeout, fetch });
const tick = () => new Promise(resolve => setTimeout(resolve, 10));
function setup() {
  const calls = [], loaded = [], errors = [];
  const cache = new CoverageFrames({ base: '/build/ch/530', coarseBytes: 12, detailBytes: 24, delay: 0,
    onLoad: () => loaded.push(true), onError: e => errors.push(e),
    fetcher: (url, { signal }) => new Promise(resolve => calls.push({ url, signal,
      resolve: (bytes = url.includes('.detail.') ? 24 : 12, status = 200) => resolve({
        ok: status === 200, status, arrayBuffer: async () => new ArrayBuffer(bytes) }) })) });
  return { cache, calls, loaded, errors };
}
test('selected hour is shown before adjacent hours and prefetch stays sequential', async () => {
  const { cache, calls, loaded } = setup();
  try {
    cache.select('h0', true, ['h1', 'h2']);
    assert.equal(calls.length, 2);
    assert.ok(calls.every(c => c.url.includes('/h0')));
    calls[0].resolve(); await tick();
    assert.equal(loaded.length, 1);
    assert.equal(calls.length, 2);
    calls[1].resolve(); await tick();
    assert.equal(calls.length, 3);
    assert.match(calls[2].url, /h1.bin.gz$/);
    calls[2].resolve(); await tick();
    assert.equal(calls.length, 4);
    assert.match(calls[3].url, /h1.detail.bin.gz$/);
    assert.equal(loaded.length, 2, 'prefetch must not redraw the selected frame');
  } finally { cache.close(); }
});
test('scrubbing cancels older requests and late responses cannot replace the new frame', async () => {
  const { cache, calls, loaded } = setup();
  try {
    cache.select('h0', true);
    cache.select('h4', true);
    assert.ok(calls[0].signal.aborted && calls[1].signal.aborted);
    calls[0].resolve(); calls[1].resolve(); await tick();
    assert.equal(loaded.length, 0);
    assert.equal(cache.get('h0'), undefined);
    calls[2].resolve(); calls[3].resolve(); await tick();
    assert.equal(cache.get('h4').byteLength, 12);
    assert.equal(cache.get('h4', true).byteLength, 24);
    assert.equal(loaded.length, 2);
  } finally { cache.close(); }
});
test('zooming out cancels fine downloads and cached frames are reused', async () => {
  const { cache, calls } = setup();
  try {
    cache.select('h0', true);
    calls[0].resolve(); await tick();
    cache.select('h0', false);
    assert.equal(calls.length, 2);
    assert.ok(calls[1].signal.aborted);
    cache.select('h1', false);
    calls[2].resolve(); await tick();
    cache.select('h0', false); await tick();
    assert.equal(calls.length, 3);
  } finally { cache.close(); }
});
test('changing channels closes downloads and suppresses stale failures', async () => {
  const { cache, calls, loaded, errors } = setup();
  cache.select('h0', true);
  cache.close();
  assert.ok(calls.every(c => c.signal.aborted));
  calls[0].resolve(12, 404); calls[1].resolve(); await tick();
  assert.equal(loaded.length, 0); assert.equal(errors.length, 0);
});
test('malformed and missing selected frames are reported and not cached', async () => {
  const { cache, calls, errors } = setup();
  try {
    cache.select('h0', false); calls[0].resolve(1); await tick();
    assert.match(errors[0].message, /Incomplete/);
    assert.equal(cache.get('h0'), undefined);
    cache.select('h1', false); calls[1].resolve(12, 404); await tick();
    assert.equal(errors[1].status, 404);
    cache.select('h0', false); calls[2].resolve(); await tick();
    assert.equal(cache.get('h0').byteLength, 12);
  } finally { cache.close(); }
});
test('old prefetch cannot block or redraw a newly selected hour', async () => {
  const { cache, calls, loaded } = setup();
  try {
    cache.select('h0', false, ['h1', 'h2']); calls[0].resolve(); await tick();
    assert.equal(calls.length, 2);
    cache.select('h4', false);
    assert.ok(calls[1].signal.aborted);
    calls[1].resolve(); await tick();
    assert.equal(calls.length, 3);
    calls[2].resolve(); await tick();
    assert.equal(loaded.length, 2);
  } finally { cache.close(); }
});
test('cache memory stays bounded across the timeline', async () => {
  const { cache, calls } = setup();
  try {
    for (let i = 0; i < 25; i++) {
      cache.select(`h${i}`, true);
      calls.at(-2).resolve(); calls.at(-1).resolve(); await tick();
      assert.ok(cache.cache.size <= 12);
      assert.ok(cache.get(`h${i}`)); assert.ok(cache.get(`h${i}`, true));
    }
  } finally { cache.close(); }
});
test('simultaneous missing coarse and fine frames share one build refresh', async () => {
  const html = fs.readFileSync('static/index.html', 'utf8');
  const recovery = html.slice(html.indexOf('let rebuildPromise = null;'), html.indexOf('\nasync function load(freq'));
  let finish, reads = 0;
  const loads = [], notices = [];
  const recover = vm.runInNewContext(recovery + '\nrebuildSwitch', {
    IDX: { build: 'old' }, CH: { freq: 530 }, target: null,
    getJSON: () => { reads++; return new Promise(resolve => { finish = resolve; }); },
    $: () => ({}), esc: x => x, callOption: x => x,
    load: (...args) => loads.push(args), say: text => notices.push(text), setTimeout
  });
  const coarse = recover(), fine = recover();
  assert.equal(coarse, fine);
  assert.equal(reads, 1);
  finish({ build: 'new', calls: [] });
  await Promise.all([coarse, fine]);
  assert.equal(loads.length, 1);
  assert.equal(loads[0][0], 530);
  assert.deepEqual(notices, []);
});
