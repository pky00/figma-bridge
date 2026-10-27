// Runs the <script> of plugin/ui.html (the real plugin UI loop) in Node with a fake DOM and a fake plugin main
// thread, against a relay on the given port. Prints one JSON object per line on stdout:
//   {"event":"state","text":...}   whenever the status line changes
//   {"event":"job","job":{...}}    whenever the UI forwards a job to the (fake) main thread
// Usage: node ui_harness.mjs <port> <token> [fileName]      — runs until killed
//        node ui_harness.mjs hmac <keyHex> <msgHex>          — prints HMAC-SHA256 of the UI's implementation
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(here, '..', 'plugin', 'ui.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];

if (process.argv[2] === 'hmac') {
  const block = script.split('// --- crypto start ---')[1].split('// --- crypto end ---')[0];
  const crypto = block.slice(block.indexOf('\n'));   // drop the rest of the marker line
  const fn = new Function(crypto + '\nreturn { hmacSha256, sha256, hex };');
  const { hmacSha256, hex } = fn();
  const key = Uint8Array.from(Buffer.from(process.argv[3], 'hex'));
  const msgHex = process.argv[4] === '-' ? fs.readFileSync(0, 'utf8').trim() : process.argv[4];   // '-' = read stdin
  const msg = Uint8Array.from(Buffer.from(msgHex, 'hex'));
  console.log(hex(hmacSha256(key, msg)));
  process.exit(0);
}

const [port, token, fileName = 'UI harness'] = process.argv.slice(2);
const emit = obj => process.stdout.write(JSON.stringify(obj) + '\n');

function element() {
  const node = {
    textContent: '', className: '', value: '', onclick: null, childNodes: [],
    prepend(child) { node.childNodes.unshift(child); },
    get lastChild() { return node.childNodes.length ? { remove() { node.childNodes.pop(); } } : null; },
    addEventListener() {},
  };
  return node;
}
const elements = {};
const document = { getElementById: id => (elements[id] ||= element()), createElement: () => element() };
const window = {};
const deliver = msg => setTimeout(() => window.onmessage({ data: { pluginMessage: msg } }), 5);
const parent = {
  postMessage(message) {
    const msg = message.pluginMessage;
    if (msg.type === 'file-info') deliver({ type: 'file-info', info: { fileName, editorType: 'figma' } });
    if (msg.type === 'job') {
      emit({ event: 'job', job: msg.job });
      deliver({ type: 'job-result', id: msg.job.id, jobType: msg.job.type, ms: 1, ok: true,
        result: { ran: msg.job.payload && msg.job.payload.code, file: fileName }, error: null, logs: [] });
    }
  },
};

const code = script.replace("const RELAY = 'http://localhost:3789';", `const RELAY = 'http://localhost:${port}';`);
new Function('document', 'parent', 'window', code)(document, parent, window);

let last = null;
setInterval(() => {
  const text = (elements.state && elements.state.textContent) || '';
  if (text !== last) { last = text; emit({ event: 'state', text }); }
}, 50);

window.onmessage({ data: { pluginMessage: { type: 'init', token } } });
