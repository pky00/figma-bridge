// Runs plugin/code.js (the real plugin main thread) in a Node VM against a mock `figma` global
// and checks the job handling. Exits non-zero on the first failed check. Used by tests/test_plugin_js.py.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(here, '..', 'plugin', 'code.js'), 'utf8');

const posted = [];
let onmessage = null;
let stored = 'saved-token';
const frame = {
  id: '1:2', type: 'FRAME', name: 'Frame', removed: false, width: 100, height: 50,
  exportAsync: async opts => { frame.lastExport = JSON.parse(JSON.stringify(opts)); return new Uint8Array([137, 80, 78, 71]); },
  fills: [],
};
const created = [];
const figma = {
  showUI() {},
  ui: {
    set onmessage(f) { onmessage = f; },
    get onmessage() { return onmessage; },
    postMessage(m) { posted.push(JSON.parse(JSON.stringify(m))); },   // postMessage clones, like Figma does
  },
  clientStorage: { getAsync: async () => stored, setAsync: async (k, v) => { stored = v; } },
  base64Encode: b => Buffer.from(b).toString('base64'),
  base64Decode: s => new Uint8Array(Buffer.from(s, 'base64')),
  root: { name: 'Mock file' },
  editorType: 'figma',
  fileKey: 'KEY',
  currentPage: { id: '0:1', name: 'Page 1', selection: [] },
  viewport: { center: { x: 500, y: 300 } },
  getNodeByIdAsync: async id => (id === '1:2' ? frame : id === '0:1' ? { id: '0:1', type: 'PAGE', name: 'Page 1', removed: false, exportAsync: async () => new Uint8Array(0) } : null),
  createImage: () => ({ hash: 'hash-1', getSizeAsync: async () => ({ width: 20, height: 10 }) }),
  createRectangle: () => {
    const r = { id: '9:' + (created.length + 1), type: 'RECTANGLE', name: '', removed: false, x: 0, y: 0, width: 0, height: 0, fills: [],
      resize(w, h) { this.width = w; this.height = h; } };
    created.push(r);
    return r;
  },
  createNodeFromSvg: () => ({ id: '8:1', type: 'FRAME', name: '', removed: false, width: 24, height: 24, x: 0, y: 0 }),
  closePlugin() {},
};

const context = vm.createContext({ figma, __html__: '<html></html>', setTimeout, clearTimeout, console });
// Like Figma's sandbox: the Function / AsyncFunction constructors are unavailable, indirect eval works.
vm.runInContext(`
  Object.defineProperty(Object.getPrototypeOf(async function () {}), 'constructor', { value: function () { throw new TypeError('AsyncFunction is not available'); } });
  Object.defineProperty(Function.prototype, 'constructor', { value: function () { throw new TypeError('Function is not available'); } });
`, context);
vm.runInContext(source, context, { filename: 'code.js' });

const tick = () => new Promise(r => setTimeout(r, 30));
async function job(j) {
  onmessage({ type: 'job', job: j });
  for (let i = 0; i < 50; i++) {
    const m = posted.find(p => p.type === 'job-result' && p.id === j.id);
    if (m) return m;
    await tick();
  }
  throw new Error('no result for ' + j.id);
}

await tick();
const init = posted.find(p => p.type === 'init');
assert.equal(init.token, 'saved-token');
assert.equal(init.info.fileName, 'Mock file');

// run: return value serialisation, input, console capture
let r = await job({ id: 'a', type: 'run', payload: {
  code: 'console.log("hi", {a: 1}); console.warn("careful"); const n = await figma.getNodeByIdAsync("1:2");' +
        'return {file: figma.root.name, node: n, bytes: new Uint8Array([255]), input, undef: undefined, fn() {}, nan: NaN, list: [n, 1]};',
  input: { k: 5 },
} });
assert.equal(r.ok, true, r.error);
assert.deepEqual(r.result, {
  file: 'Mock file', node: { id: '1:2', type: 'FRAME', name: 'Frame', removed: false },
  bytes: { $bytes: '/w==' }, input: { k: 5 }, undef: null, nan: null,
  list: [{ id: '1:2', type: 'FRAME', name: 'Frame', removed: false }, 1],
});
assert.deepEqual(r.logs, ['hi {"a":1}', 'warn: careful']);

// run: undefined return, syntax error, runtime error with logs
r = await job({ id: 'b', type: 'run', payload: { code: 'const x = 1;' } });
assert.equal(r.ok, true); assert.equal(r.result, null);
r = await job({ id: 'c', type: 'run', payload: { code: 'return (' } });
assert.equal(r.ok, false); assert.match(r.error, /Syntax error/);
r = await job({ id: 'd', type: 'run', payload: { code: 'console.log("before"); throw new Error("boom")' } });
assert.equal(r.ok, false); assert.match(r.error, /boom/); assert.deepEqual(r.logs, ['before']);

// shared (non-circular) references, real cycles, trailing comments
r = await job({ id: 'sh', type: 'run', payload: { code: 'const s = {r: 1}; return [s, s, {deep: s}]' } });
assert.deepEqual(r.result, [{ r: 1 }, { r: 1 }, { deep: { r: 1 } }]);
r = await job({ id: 'cy', type: 'run', payload: { code: 'const a = {}; a.self = a; return a' } });
assert.deepEqual(r.result, { self: '[circular]' });
r = await job({ id: 'cm', type: 'run', payload: { code: 'return 5 // trailing comment' } });
assert.equal(r.ok, true, r.error); assert.equal(r.result, 5);

// a job that never settles is abandoned at its timeout and does not block later jobs
r = await job({ id: 'to', type: 'run', timeoutMs: 200, payload: { code: 'await new Promise(() => {})' } });
assert.equal(r.ok, false); assert.match(r.error, /did not finish/);
r = await job({ id: 'after', type: 'run', payload: { code: 'return "still alive"' } });
assert.equal(r.result, 'still alive');

// a job that waited past its absolute deadline is not run at all
r = await job({ id: 'exp', type: 'run', timeoutMs: 60000, expiresAt: Date.now() - 1, payload: { code: 'globalThis.ranExpired = true; return 1' } });
assert.equal(r.ok, false); assert.match(r.error, /expired before it could start/);
assert.equal(vm.runInContext('globalThis.ranExpired', context), undefined);

// lone surrogates (an emoji cut in half) are repaired in results, logs and errors
r = await job({ id: 'sur', type: 'run', payload: { code: 'console.log("x\\uD83D"); return {t: "cut \\uD83D", k: {"\\uDC00": 1}}' } });
assert.equal(r.ok, true, r.error);
assert.equal(r.result.t, 'cut \uFFFD'); assert.deepEqual(Object.keys(r.result.k), ['\uFFFD']); assert.equal(r.logs[0], 'x\uFFFD');

// JPG exports report their real size
frame.exportAsync = async opts => {
  frame.lastExport = JSON.parse(JSON.stringify(opts));
  return new Uint8Array([0xff, 0xd8, 0xff, 0xff, 0xc0, 0x00, 0x11, 0x08, 0x00, 0xc8, 0x01, 0x2c, 0x03, 0x01, 0x22, 0x00]);
};
r = await job({ id: 'jpg', type: 'screenshot', payload: { nodeId: '1:2', format: 'JPG' } });
assert.equal(r.ok, true, r.error); assert.equal(r.result.width, 300); assert.equal(r.result.height, 200);
frame.exportAsync = async opts => { frame.lastExport = JSON.parse(JSON.stringify(opts)); return new Uint8Array([137, 80, 78, 71]); };

// pages cannot be exported
r = await job({ id: 'pg', type: 'screenshot', payload: { nodeId: '0:1' } });
assert.equal(r.ok, false); assert.match(r.error, /Pages cannot be screenshotted/);

// screenshot: scale capped by maxDimension
r = await job({ id: 'e', type: 'screenshot', payload: { nodeId: '1:2', scale: 4, maxDimension: 200 } });
assert.equal(r.ok, true, r.error);
assert.equal(r.result.scale, 2); assert.equal(r.result.width, 200); assert.equal(r.result.mimeType, 'image/png');
assert.equal(r.result.base64, Buffer.from([137, 80, 78, 71]).toString('base64'));
assert.deepEqual(frame.lastExport, { format: 'PNG', constraint: { type: 'SCALE', value: 2 }, contentsOnly: true });
r = await job({ id: 'f', type: 'screenshot', payload: {} });
assert.equal(r.ok, false); assert.match(r.error, /Nothing selected/);
r = await job({ id: 'g', type: 'screenshot', payload: { nodeId: '404:1' } });
assert.equal(r.ok, false); assert.match(r.error, /No node/);

// image: fill an existing node, create a new rectangle, svg
r = await job({ id: 'h', type: 'image', payload: { base64: 'AAEC', nodeId: '1:2', scaleMode: 'FIT' } });
assert.equal(r.ok, true, r.error);
assert.deepEqual(JSON.parse(JSON.stringify(frame.fills)), [{ type: 'IMAGE', imageHash: 'hash-1', scaleMode: 'FIT' }]);
r = await job({ id: 'i', type: 'image', payload: { base64: 'AAEC', name: 'Logo' } });
assert.equal(r.ok, true, r.error);
const rect = created[created.length - 1];
assert.equal(rect.name, 'Logo'); assert.equal(rect.width, 20); assert.equal(rect.x, 490); assert.equal(rect.y, 295);
r = await job({ id: 'j', type: 'image', payload: { svg: '<svg/>', x: 10, y: 20 } });
assert.equal(r.ok, true, r.error); assert.equal(r.result.nodeId, '8:1');

// status and unknown jobs
r = await job({ id: 'k', type: 'status', payload: {} });
assert.equal(r.ok, true); assert.equal(r.result.currentPage.name, 'Page 1'); assert.equal(r.result.fileKey, 'KEY');
r = await job({ id: 'l', type: 'nope', payload: {} });
assert.equal(r.ok, false); assert.match(r.error, /Unknown job type/);

// token persistence
onmessage({ type: 'save-token', token: 'new-token' });
await tick();
assert.equal(stored, 'new-token');

console.log('plugin harness: all checks passed');
