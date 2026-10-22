import createModule from './gdal4k.mjs';
import {Outputs} from './gdal4k-outputs.mjs';
import {Inputs} from './gdal4k-inputs.mjs';
import {resources, resourceUrl, resourceBytes} from './gdal4k-resources.mjs';

const node = typeof process !== 'undefined' && process.versions?.node;
const port = node ? process.getBuiltinModule('worker_threads').parentPort : globalThis;
const wasmUrl = new URL('./gdal4k.wasm', import.meta.url);
let module, context, inputs, outputs, queue = Promise.resolve();
const datasets = new Map();
const pendingCalls = new Set(), cancelledCalls = new Set();
let activeControl, activeCall, activeAbort;
function nodeWasmBinary() {
    const bytes = process.getBuiltinModule('fs').readFileSync(wasmUrl);
    // Emscripten makes a copy of a Buffer/Uint8Array, but only a view of an
    // ArrayBuffer. Preserve the exact file range if Node used a pooled buffer.
    return bytes.byteOffset === 0 && bytes.byteLength === bytes.buffer.byteLength
        ? bytes.buffer : bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}
async function execute(request, control = 0) {
    // ccall's string conversion uses the C stack. A valid memory dataset request can be
    // several MiB; keep its UTF-8 bytes on the heap across every Asyncify suspension.
    const input = module.stringToNewUTF8(JSON.stringify(request));
    if (!input) throw new Error('GDAL request allocation failed');
    let pointer;
    try {
        pointer = control
            ? await module.ccall('gdal4k_execute_controlled', 'number', ['number', 'number', 'number'], [context, input, control], {async: true})
            : await module.ccall('gdal4k_execute', 'number', ['number', 'number'], [context, input], {async: true});
    } finally { module._free(input); }
    if (!pointer) throw new Error('GDAL allocation failed');
    try { return module.UTF8ToString(pointer); } finally { module._gdal4k_free(pointer); }
}
async function executeBinary(request, bytes, control) {
    const text = module.stringToNewUTF8(JSON.stringify(request));
    if (!text) throw new Error('GDAL request allocation failed');
    let input = 0, result = 0;
    try {
        if (bytes?.byteLength) {
            input = module._malloc(bytes.byteLength);
            if (!input) throw new Error('GDAL raster allocation failed');
            module.HEAPU8.set(new Uint8Array(bytes), input);
        }
        result = await module.ccall('gdal4k_execute_binary', 'number', ['number','number','number','number','number'],
            [context,text,input,bytes?.byteLength ?? 0,control ?? 0], {async:true});
        if (!result) throw new Error('Binary GDAL bridge failed');
        const json = module.UTF8ToString(module._gdal4k_binary_json(result));
        const size = module._gdal4k_binary_size(result);
        const pointer = module._gdal4k_binary_data(result);
        // Wasm linear memory stays in the Worker. Transfer only this exact owned range.
        const output = module.HEAPU8.slice(pointer,pointer+size);
        return {json,bytes:output.buffer};
    } finally {
        if (result) module._gdal4k_binary_destroy(result);
        if (input) module._free(input);
        module._free(text);
    }
}
function releaseInputs(response, args) {
    // Public handles may close before native VRT dependencies release their source mounts.
    const released = response.releasedDatasets ?? (!response.error && args?.method === 'close' ? [args.handle] : []);
    for (const handle of released) { datasets.get(handle)?.close(); datasets.delete(handle); }
}
async function cleanupAllocated(response, failure) {
    for (const handle of [...(response.allocatedHandles ?? [])].reverse()) {
        try {
            const closed = JSON.parse(await execute({op: 'runtime', args: {type: 'Handle', handle, method: 'close', parameters: {}}}));
            releaseInputs(closed);
            if (closed.error) throw new Error(closed.error);
        } catch (cleanup) {
            (failure.cleanupErrors ??= []).push(String(cleanup.stack || cleanup));
        }
    }
}
async function dispatch({id, operation, payload, file, controlled, binary}) {
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
            result = {binaryRasterVersion: module._gdal4k_binary_version?.() ?? 0};
        } else if (operation === 'call') {
            if (!context) throw new Error('GDAL Worker is closed');
            activeCall = id; activeAbort = new AbortController();
            if (cancelledCalls.has(id)) activeAbort.abort(new DOMException('HTTP input operation cancelled', 'AbortError'));
            inputs.beginCall(activeAbort.signal);
            const request = JSON.parse(payload);
            const args = request.args;
            if (args?.type === 'GdalRuntime' && args.method === 'openDataset_String_DatasetOpenOptions') {
                let source = args.parameters.source;
                const policy = args.parameters.options?.httpInputPolicy;
                if (policy && ((file && file.kind !== 'urls') || !/^https?:\/\//i.test(source))) throw new Error('HTTP input policy requires a direct HTTP(S) URL');
                if (source.startsWith('opfs:/')) { source = outputs.path(source); args.parameters.source = source; }
                const virtual = source.startsWith('/vsigdal4k-output/') || /^\/vsi[a-z0-9_]+\//i.test(source);
                if (file || /^https?:\/\//i.test(source) || (node && !virtual)) {
                    if (args.parameters.options?.update) throw new Error('Direct web inputs are read-only');
                    mounted = await inputs.mount(source, file, policy);
                    args.parameters.source = mounted.path;
                } else if (!virtual) {
                    throw new Error('Browser paths require a selected File; use openDataset(file) or an HTTP(S) URL');
                }
            } else if (file) throw new Error('A browser File can only be attached to openDataset');
            if ((args?.type === 'Dataset' || args?.type === 'GdalRuntime') && /^(translateTo|warpTo|translateVectorTo|rasterizeTo|demProcessingTo|polygonizeTo|footprintTo|tileIndexTo|gridTo|viewshedTo|contourTo)_/.test(args.method)) args.parameters.path = outputs.path(args.parameters.path);
            if (args?.type === 'Driver' && args.method === 'delete_String') args.parameters.name = outputs.path(args.parameters.name);
            inputs.lastError = null; outputs.lastError = null;
            if (controlled) {
                activeControl = module._gdal4k_control_create();
                if (!activeControl) throw new Error('Unable to allocate GDAL control');
                module.gdal4kProgress = event => port.postMessage({id, progress: event});
                if (cancelledCalls.has(id)) module._gdal4k_control_cancel(activeControl);
            }
            try { result = binary ? await executeBinary(request, binary.input, activeControl) : await execute(request, activeControl); }
            finally {
                module.gdal4kProgress = undefined;
                if (activeControl) module._gdal4k_control_destroy(activeControl);
                activeControl = 0;
            }
            const response = JSON.parse(binary ? result.json : result);
            if (mounted) {
                if (response.error) { mounted.close(); mounted = null; }
                else { datasets.set(response.result.value, mounted); mounted = null; }
            }
            releaseInputs(response, args);
            if (inputs.lastError || outputs.lastError) {
                // A backend read can fail even when GDAL returns a numeric error or an
                // allocated result. Release every new handle, retaining the primary failure.
                const failure = inputs.lastError || outputs.lastError;
                if (!response.allocatedHandles && args?.method === 'openDataset_String_DatasetOpenOptions' &&
                    typeof response.result?.value === 'string') response.allocatedHandles = [response.result.value];
                await cleanupAllocated(response, failure);
                throw failure;
            }
        } else if (operation === 'close') {
            if (context) await module.ccall('gdal4k_destroy', null, ['number'], [context], {async: true});
            context = 0;
            for (const input of datasets.values()) input.close();
            datasets.clear();
            try { inputs?.close(); } finally { outputs?.closeAll(); }
        } else throw new Error('Unknown GDAL Worker operation');
        port.postMessage({id, result}, binary && result?.bytes ? [result.bytes] : []);
    } catch (error) {
        mounted?.close();
        port.postMessage({id, error: String(error.stack || error) + (error.cleanupErrors?.length ? '\nCleanup: ' + error.cleanupErrors.join('\n') : '')});
    } finally {
        if (activeCall === id) {
            inputs?.endCall(); activeAbort = undefined; activeCall = undefined;
        }
        pendingCalls.delete(id); cancelledCalls.delete(id);
    }
}
const receive = message => {
    // Native progress yields ordinary single-thread Wasm. Cancellation must bypass the serial
    // GDAL call queue and touches only the independent atomic control, never the context.
    if (message.operation === 'cancel') {
        if (pendingCalls.has(message.id)) cancelledCalls.add(message.id);
        if (activeCall === message.id) {
            activeAbort?.abort(new DOMException('HTTP input operation cancelled', 'AbortError'));
            if (activeControl) module._gdal4k_control_cancel(activeControl);
        }
        return;
    }
    if (message.operation === 'call') pendingCalls.add(message.id);
    queue = queue.then(() => dispatch(message));
};
if (node) port.on('message', receive); else port.onmessage = event => receive(event.data);
