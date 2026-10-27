// Figma Bridge — plugin main thread.
// This side has the Figma Plugin API but no network. The UI iframe (ui.html) talks to the local
// relay and forwards each job here; the answer goes back the same way.

const PLUGIN_VERSION = '0.1.0';
const TOKEN_KEY = 'figma-bridge-token';
const MAX_RESULT_CHARS = 60 * 1024 * 1024;   // the relay accepts 64 MB per request

figma.showUI(__html__, { width: 320, height: 250, title: 'Figma Bridge', themeColors: true });

// ---------- serialisation of return values ----------

function isSceneLike(v) {
  return v !== null && typeof v === 'object' && typeof v.id === 'string' && typeof v.type === 'string' && 'removed' in v;
}

// `path` holds the objects on the current branch (true cycles); `done` caches finished objects so shared,
// non-circular references are serialised normally and only once.
// Lone UTF-16 surrogates (an emoji cut in half by .slice) cannot be encoded as UTF-8; replace them with U+FFFD.
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;
function wellFormed(s) {
  return typeof s.toWellFormed === 'function' ? s.toWellFormed() : s.replace(LONE_SURROGATE, '\uFFFD');
}

function serialize(value, depth, path, done) {
  if (value === undefined || value === null) return null;
  const t = typeof value;
  if (t === 'string') return wellFormed(value);
  if (t === 'boolean') return value;
  if (t === 'number') return Number.isFinite(value) ? value : null;
  if (t === 'bigint') return value.toString();
  if (t === 'function' || t === 'symbol') return undefined;
  if (depth > 25) return '[depth limit]';
  if (value instanceof Uint8Array) return { $bytes: figma.base64Encode(value) };
  if (value instanceof ArrayBuffer) return { $bytes: figma.base64Encode(new Uint8Array(value)) };
  if (isSceneLike(value)) {
    let name = null;
    try { name = value.removed ? null : wellFormed(value.name); } catch (e) { name = null; }
    return { id: value.id, type: value.type, name: name, removed: !!value.removed };
  }
  if (path.has(value)) return '[circular]';
  if (done.has(value)) return done.get(value);
  path.add(value);
  let out;
  try {
    if (value instanceof Error) out = { name: value.name, message: wellFormed(String(value.message)) };
    else if (value instanceof Date) out = value.toISOString();
    else if (Array.isArray(value)) out = value.map(v => { const s = serialize(v, depth + 1, path, done); return s === undefined ? null : s; });
    else if (value instanceof Map) {
      out = {};
      for (const [k, v] of value) { const s = serialize(v, depth + 1, path, done); if (s !== undefined) out[wellFormed(String(k))] = s; }
    } else if (value instanceof Set) out = [...value].map(v => serialize(v, depth + 1, path, done));
    else {
      out = {};
      for (const key of Object.keys(value)) {
        let v;
        try { v = value[key]; } catch (e) { v = '[unreadable: ' + e.message + ']'; }
        const s = serialize(v, depth + 1, path, done);
        if (s !== undefined) out[wellFormed(key)] = s;
      }
    }
  } finally {
    path.delete(value);
  }
  done.set(value, out);
  return out;
}

function toJsonable(value) {
  const s = serialize(value, 0, new Set(), new Map());
  return s === undefined ? null : s;
}

function formatLogArg(a) {
  if (typeof a === 'string') return wellFormed(a);
  try { return JSON.stringify(toJsonable(a)); } catch (e) { return wellFormed(String(a)); }
}

function errorText(err) {
  if (!err) return 'Unknown error';
  const msg = err.message || String(err);
  const stack = typeof err.stack === 'string' ? err.stack.split('\n').slice(0, 6).join('\n') : '';
  return wellFormed(stack && stack.indexOf(msg) === -1 ? msg + '\n' + stack : (stack || msg));
}

// ---------- jobs ----------

// Figma's sandbox restricts the Function/AsyncFunction constructors; indirect eval of an async function
// expression works. The newline before the closing brace keeps a trailing // comment from swallowing it.
function compile(code) {
  return (0, eval)('(async function (figma, input, console) {\n' + code + '\n})');
}

async function runCode(payload) {
  const logs = [];
  const capture = level => (...args) => {
    if (logs.length < 1000) logs.push((level === 'log' ? '' : level + ': ') + args.map(formatLogArg).join(' '));
  };
  const consoleProxy = { log: capture('log'), info: capture('info'), warn: capture('warn'), error: capture('error'), debug: capture('debug') };
  let fn;
  try {
    fn = compile(String(payload.code || ''));
  } catch (err) {
    return { ok: false, error: (err instanceof SyntaxError ? 'Syntax error: ' : 'Compile error: ') + errorText(err), logs };
  }
  try {
    const value = await fn(figma, payload.input === undefined ? null : payload.input, consoleProxy);
    return { ok: true, result: toJsonable(value), logs };
  } catch (err) {
    return { ok: false, error: errorText(err), logs };
  }
}

function jpgSize(bytes) {
  if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return null;
  let i = 2;
  while (i + 1 < bytes.length) {
    if (bytes[i] !== 0xff) { i++; continue; }
    const m = bytes[i + 1];
    if (m === 0xff) { i++; continue; }                                   // fill byte
    if (m === 0x00 || m === 0x01 || (m >= 0xd0 && m <= 0xd8)) { i += 2; continue; }
    if (m === 0xd9 || m === 0xda) return null;                            // no frame header before the scan
    if (i + 9 > bytes.length) return null;
    const len = (bytes[i + 2] << 8) | bytes[i + 3];
    if ((m >= 0xc0 && m <= 0xc3) || (m >= 0xc5 && m <= 0xc7) || (m >= 0xc9 && m <= 0xcb) || (m >= 0xcd && m <= 0xcf)) {
      return { height: (bytes[i + 5] << 8) | bytes[i + 6], width: (bytes[i + 7] << 8) | bytes[i + 8] };
    }
    i += 2 + len;
  }
  return null;
}

function pngSize(bytes) {
  if (bytes.length >= 24 && bytes[0] === 0x89 && bytes[1] === 0x50) {
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    return { width: dv.getUint32(16), height: dv.getUint32(20) };
  }
  return null;
}

async function screenshot(payload) {
  let node = null;
  if (payload.nodeId) {
    node = await figma.getNodeByIdAsync(String(payload.nodeId));
    if (!node) throw new Error('No node with id ' + payload.nodeId);
  } else {
    node = figma.currentPage.selection[0] || null;
    if (!node) throw new Error('Nothing selected: pass nodeId or select a layer in Figma.');
  }
  if (node.type === 'PAGE' || node.type === 'DOCUMENT') throw new Error('Pages cannot be screenshotted; pass the id of a frame or other layer.');
  if (typeof node.exportAsync !== 'function') throw new Error('Node ' + node.id + ' (' + node.type + ') cannot be exported');
  const format = String(payload.format || 'PNG').toUpperCase() === 'JPG' ? 'JPG' : 'PNG';
  const maxDim = Math.min(Number(payload.maxDimension) > 0 ? Number(payload.maxDimension) : 4096, 8192);
  // Effects (shadows, blurs) and strokes can render outside width/height.
  // Estimate from the union of the bounding box and the render bounds (clipping can shrink the latter, effects
  // can grow it); the real size is checked after the export anyway.
  const boxes = [node.absoluteBoundingBox, node.absoluteRenderBounds].filter(Boolean);
  let w = node.width || 1, h = node.height || 1;
  if (boxes.length) {
    const x0 = Math.min(...boxes.map(b => b.x)), y0 = Math.min(...boxes.map(b => b.y));
    const x1 = Math.max(...boxes.map(b => b.x + b.width)), y1 = Math.max(...boxes.map(b => b.y + b.height));
    w = Math.max(x1 - x0, 1); h = Math.max(y1 - y0, 1);
  }
  let scale = Number(payload.scale) > 0 ? Number(payload.scale) : 1;
  scale = Math.min(scale, maxDim / Math.max(w, h));
  let bytes = await node.exportAsync({ format: format, constraint: { type: 'SCALE', value: scale }, contentsOnly: payload.contentsOnly !== false });
  const measure = b => (format === 'PNG' ? pngSize(b) : jpgSize(b));
  let size = measure(bytes);
  if (size && Math.max(size.width, size.height) > maxDim * 1.02) {      // still too big: export once more, smaller
    scale = scale * maxDim / Math.max(size.width, size.height);
    bytes = await node.exportAsync({ format: format, constraint: { type: 'SCALE', value: scale }, contentsOnly: payload.contentsOnly !== false });
    size = measure(bytes);
  }
  const b64 = figma.base64Encode(bytes);
  if (b64.length > MAX_RESULT_CHARS) throw new Error('The export is ' + Math.round(b64.length / 1e6) + ' MB; lower scale or maxDimension.');
  return {
    ok: true,
    result: {
      id: node.id, name: node.name, type: node.type,
      width: size ? size.width : Math.round(w * scale), height: size ? size.height : Math.round(h * scale), scale: scale,
      mimeType: format === 'JPG' ? 'image/jpeg' : 'image/png',
      base64: b64,
    },
    logs: [],
  };
}

async function addImage(payload) {
  if (payload.svg) {
    const svgNode = figma.createNodeFromSvg(String(payload.svg));
    if (payload.name) svgNode.name = String(payload.name);
    placeNew(svgNode, payload);
    return { ok: true, result: { nodeId: svgNode.id, type: svgNode.type, width: svgNode.width, height: svgNode.height }, logs: [] };
  }
  const bytes = figma.base64Decode(String(payload.base64 || ''));
  const image = figma.createImage(bytes);
  const size = await image.getSizeAsync();
  const scaleMode = ['FILL', 'FIT', 'CROP', 'TILE'].indexOf(String(payload.scaleMode)) >= 0 ? String(payload.scaleMode) : 'FILL';
  const paint = { type: 'IMAGE', imageHash: image.hash, scaleMode: scaleMode };
  let target = null;
  if (payload.nodeId) {
    target = await figma.getNodeByIdAsync(String(payload.nodeId));
    if (!target) throw new Error('No node with id ' + payload.nodeId);
    if (!('fills' in target)) throw new Error('Node ' + target.id + ' (' + target.type + ') has no fills');
    target.fills = [paint];
  } else if (payload.createNode !== false) {
    target = figma.createRectangle();
    target.resize(size.width, size.height);
    target.fills = [paint];
    target.name = String(payload.name || 'Image');
    placeNew(target, payload);
  }
  return { ok: true, result: { imageHash: image.hash, width: size.width, height: size.height, nodeId: target ? target.id : null }, logs: [] };
}

function placeNew(node, payload) {
  if (typeof payload.x === 'number' && typeof payload.y === 'number') { node.x = payload.x; node.y = payload.y; return; }
  const c = figma.viewport.center;
  node.x = Math.round(c.x - node.width / 2);
  node.y = Math.round(c.y - node.height / 2);
}

function fileInfo() {
  let fileKey = null;
  try { fileKey = figma.fileKey || null; } catch (e) { fileKey = null; }
  return {
    pluginVersion: PLUGIN_VERSION,
    fileName: figma.root.name,
    fileKey: fileKey,
    editorType: figma.editorType,
    currentPage: { id: figma.currentPage.id, name: figma.currentPage.name },
    selection: figma.currentPage.selection.slice(0, 50).map(n => ({ id: n.id, name: n.name, type: n.type })),
  };
}

async function handleJob(job) {
  const payload = job.payload || {};
  try {
    switch (job.type) {
      case 'run': return await runCode(payload);
      case 'screenshot': return await screenshot(payload);
      case 'image': return await addImage(payload);
      case 'status': return { ok: true, result: fileInfo(), logs: [] };
      default: return { ok: false, error: 'Unknown job type: ' + job.type, logs: [] };
    }
  } catch (err) {
    return { ok: false, error: errorText(err), logs: [] };
  }
}

// Jobs run one at a time, in arrival order. Each is bounded by its timeout so a promise that never
// settles cannot block the queue (the abandoned code may keep running inside Figma).
let chain = Promise.resolve();

function post(job, started, out) {
  let msg = { type: 'job-result', id: job.id, jobType: job.type, ms: Date.now() - started, ok: !!out.ok, result: out.result ?? null, error: out.error ?? null, logs: out.logs || [] };
  try {
    figma.ui.postMessage(msg);
  } catch (err) {          // result not cloneable: report instead of hanging
    msg = { type: 'job-result', id: job.id, jobType: job.type, ms: Date.now() - started, ok: false, result: null, error: 'Could not send the result back: ' + errorText(err), logs: [] };
    figma.ui.postMessage(msg);
  }
}

figma.ui.onmessage = msg => {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'save-token') {
    figma.clientStorage.setAsync(TOKEN_KEY, String(msg.token || '')).catch(() => {});
  } else if (msg.type === 'job' && msg.job) {
    const job = msg.job;
    const arrived = Date.now();
    const deadline = Number(job.expiresAt) > 0 ? Number(job.expiresAt) : arrived + (Number(job.timeoutMs) > 0 ? Number(job.timeoutMs) : 60000);
    chain = chain.then(async () => {
      const started = Date.now();
      if (started >= deadline) {             // waited behind another job past its deadline: the caller gave up
        post(job, started, { ok: false, error: 'The job expired before it could start (another job was still running); it did not run.', logs: [] });
        return;
      }
      const limitMs = deadline - started;
      let timer;
      const limit = new Promise(resolve => {
        timer = setTimeout(() => resolve({ ok: false, error: 'The job did not finish within ' + limitMs + ' ms and was abandoned; it may still be running in Figma and changing the file.', logs: [] }), limitMs);
      });
      const out = await Promise.race([handleJob(job), limit]);
      clearTimeout(timer);
      post(job, started, out);
    }).catch(() => {});
  } else if (msg.type === 'file-info') {
    figma.ui.postMessage({ type: 'file-info', info: fileInfo() });
  } else if (msg.type === 'close') {
    figma.closePlugin();
  }
};

figma.clientStorage.getAsync(TOKEN_KEY).then(token => {
  figma.ui.postMessage({ type: 'init', token: token || '', info: fileInfo() });
}).catch(() => {
  figma.ui.postMessage({ type: 'init', token: '', info: fileInfo() });
});
