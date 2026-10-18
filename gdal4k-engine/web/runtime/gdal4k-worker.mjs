import createModule from './gdal4k.mjs';
import {Outputs} from './gdal4k-outputs.mjs';
import {Inputs} from './gdal4k-inputs.mjs';
import {resources, resourceUrl, resourceBytes} from './gdal4k-resources.mjs';

const node = typeof process !== 'undefined' && process.versions?.node;
const port = node ? process.getBuiltinModule('worker_threads').parentPort : globalThis;
const wasmUrl = new URL('./gdal4k.wasm', import.meta.url);
let module, context, inputs, outputs, queue = Promise.resolve();
const datasets = new Map();
function nodeWasmBinary() {
    const bytes = process.getBuiltinModule('fs').readFileSync(wasmUrl);
    // Emscripten makes a copy of a Buffer/Uint8Array, but only a view of an
    // ArrayBuffer. Preserve the exact file range if Node used a pooled buffer.
    return bytes.byteOffset === 0 && bytes.byteLength === bytes.buffer.byteLength
        ? bytes.buffer : bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}
async function execute(request) {
    const pointer = await module.ccall('gdal4k_execute', 'number', ['number', 'string'], [context, JSON.stringify(request)], {async: true});
    if (!pointer) throw new Error('GDAL allocation failed');
    try { return module.UTF8ToString(pointer); } finally { module._gdal4k_free(pointer); }
}
async function dispatch({id, operation, payload, file}) {
    let mounted;
    try {
        let result = null;
        if (operation === 'init') {
            const options = node ? {wasmBinary: nodeWasmBinary()} : {locateFile: () => wasmUrl.href};
            try { module = await createModule(options); } finally { delete options.wasmBinary; }
            inputs = new Inputs();
            outputs = new Outputs(); module.gdal4kOutputs = outputs;
            inputs.registerResources(resources, resourceUrl, resourceBytes);
            module.gdal4kInputs = inputs;
            context = await module.ccall('gdal4k_create', 'number', ['string', 'string'], ['', ''], {async: true});
            if (!context) throw inputs.lastError || new Error('Unable to initialize bundled GDAL');
        } else if (operation === 'call') {
            if (!context) throw new Error('GDAL Worker is closed');
            const request = JSON.parse(payload);
            const args = request.args;
            if (args?.type === 'GdalRuntime' && args.method === 'openDataset_String_DatasetOpenOptions') {
                let source = args.parameters.source;
                if (source.startsWith('opfs:/')) { source = outputs.path(source); args.parameters.source = source; }
                const virtual = source.startsWith('/vsigdal4k-output/') || /^\/vsi[a-z0-9_]+\//i.test(source);
                if (file || /^https?:\/\//i.test(source) || (node && !virtual)) {
                    if (args.parameters.options?.update) throw new Error('Direct web inputs are read-only');
                    mounted = await inputs.mount(source, file);
                    args.parameters.source = mounted.path;
                } else if (!virtual) {
                    throw new Error('Browser paths require a selected File; use openDataset(file) or an HTTP(S) URL');
                }
            } else if (file) throw new Error('A browser File can only be attached to openDataset');
            if ((args?.type === 'Dataset' || args?.type === 'GdalRuntime') && /^(translateTo|warpTo|translateVectorTo|rasterizeTo|demProcessingTo|polygonizeTo|gridTo|viewshedTo|contourTo)_/.test(args.method)) args.parameters.path = outputs.path(args.parameters.path);
            if (args?.type === 'Driver' && args.method === 'delete_String') args.parameters.name = outputs.path(args.parameters.name);
            inputs.lastError = null; outputs.lastError = null;
            result = await execute(request);
            const response = JSON.parse(result);
            if (inputs.lastError || outputs.lastError) {
                // RasterIO can return a numeric error; keep the transport error actionable.
                const failure = inputs.lastError || outputs.lastError;
                if (mounted && typeof response.result?.value === 'string') {
                    await execute({op: 'runtime', args: {type: 'Handle', handle: response.result.value, method: 'close', parameters: {}}});
                }
                throw failure;
            }
            if (mounted) {
                if (response.error) { mounted.close(); mounted = null; }
                else { datasets.set(response.result.value, mounted); mounted = null; }
            }
            // Native VRT leases may retain mounts after the public source handle closes.
            // Release only datasets whose native resources were actually destroyed.
            const released = response.releasedDatasets ?? (!response.error && args?.method === 'close' ? [args.handle] : []);
            for (const handle of released) {
                datasets.get(handle)?.close();
                datasets.delete(handle);
            }
        } else if (operation === 'close') {
            if (context) await module.ccall('gdal4k_destroy', null, ['number'], [context], {async: true});
            context = 0;
            for (const input of datasets.values()) input.close();
            datasets.clear();
            try { inputs?.close(); } finally { outputs?.closeAll(); }
        } else throw new Error('Unknown GDAL Worker operation');
        port.postMessage({id, result});
    } catch (error) {
        mounted?.close();
        port.postMessage({id, error: String(error.stack || error)});
    }
}
const receive = message => { queue = queue.then(() => dispatch(message)); };
if (node) port.on('message', receive); else port.onmessage = event => receive(event.data);
