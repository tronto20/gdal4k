// Read-only HTTP ranges with explicit per-attempt deadlines and bounded caller-selected retries.
export class MissingInput extends Error {}
export class HttpProtocolError extends Error {}
class InputChanged extends HttpProtocolError {}
class RetryableStatus extends Error {
    constructor(status, retryAfter) { super(`HTTP input failed with status ${status}`); this.retryAfter = retryAfter; }
}
const abortError = () => new DOMException('HTTP input operation cancelled', 'AbortError');
export function httpPolicy(value = {}) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid HTTP input policy');
    const result = {requestTimeoutMillis: 30000, maxRetries: 0, retryDelayMillis: 200,
        maxRetryDelayMillis: 3000, requireStrongETag: false, ...value};
    for (const key of ['requestTimeoutMillis', 'maxRetries', 'retryDelayMillis', 'maxRetryDelayMillis']) {
        if (!Number.isSafeInteger(result[key]) || result[key] < 0 || result[key] > 2147483647) throw new Error(`Invalid HTTP input ${key}`);
    }
    if (!result.requestTimeoutMillis || result.maxRetryDelayMillis < result.retryDelayMillis || typeof result.requireStrongETag !== 'boolean') throw new Error('Invalid HTTP input retry/timeout policy');
    if (Object.keys(value).some(key => !Object.hasOwn(result, key) || !['requestTimeoutMillis', 'maxRetries', 'retryDelayMillis', 'maxRetryDelayMillis', 'requireStrongETag'].includes(key))) throw new Error('Unknown HTTP input policy option');
    return result;
}
function contentRange(header, start, end, expectedSize) {
    const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(header);
    if (!match || +match[1] !== start || +match[2] !== end || (expectedSize !== undefined && +match[3] !== expectedSize)) throw new InputChanged('Invalid or changed HTTP Content-Range; expose it through CORS');
    const size = +match[3];
    if (!Number.isSafeInteger(size) || size <= end) throw new HttpProtocolError('Invalid HTTP file size');
    return size;
}
async function readExact(response, expected) {
    if (!response.body) throw new HttpProtocolError('HTTP range response has no body');
    const reader = response.body.getReader();
    const bytes = new Uint8Array(expected);
    let count = 0;
    try {
        for (;;) {
            const {done, value} = await reader.read();
            if (done) break;
            if (count + value.length > expected) throw new HttpProtocolError('Invalid HTTP range payload length');
            bytes.set(value, count); count += value.length;
        }
        if (count !== expected) throw new HttpProtocolError('Invalid HTTP range payload length');
        return bytes;
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}
function wait(milliseconds, signal) {
    if (signal?.aborted) return Promise.reject(signal.reason || abortError());
    return new Promise((resolve, reject) => {
        const done = () => { signal?.removeEventListener('abort', cancel); resolve(); };
        const timer = setTimeout(done, milliseconds);
        const cancel = () => { clearTimeout(timer); signal.removeEventListener('abort', cancel); reject(signal.reason || abortError()); };
        signal?.addEventListener('abort', cancel, {once: true});
    });
}
function retryAfter(header, fallback, maximum) {
    if (header == null) return fallback;
    const milliseconds = /^\d+$/.test(header) ? Number(header) * 1000 : Date.parse(header) - Date.now();
    return Number.isFinite(milliseconds) ? Math.min(maximum, Math.max(0, milliseconds)) : fallback;
}

export async function httpSource(url, options = {}, currentSignal = () => null) {
    const policy = httpPolicy(options);
    const controllers = new Set();
    const lifetime = new AbortController();
    let fatal = null, validator = null;
    function check() {
        if (fatal) throw fatal;
        const signal = currentSignal();
        if (signal?.aborted) throw signal.reason || abortError();
    }
    async function fetchRange(start, end, expectedSize) {
        check();
        if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end < start) throw new HttpProtocolError('Invalid HTTP byte range');
        for (let attempt = 0;; attempt++) {
            check();
            const parent = currentSignal(), control = new AbortController();
            const cancel = () => control.abort(parent?.reason || abortError());
            const closed = () => control.abort(lifetime.signal.reason || abortError());
            parent?.addEventListener('abort', cancel, {once: true});
            lifetime.signal.addEventListener('abort', closed, {once: true});
            controllers.add(control);
            let timedOut = false, response, retry = null;
            const timer = setTimeout(() => { timedOut = true; control.abort(new DOMException('HTTP input request timed out', 'TimeoutError')); }, policy.requestTimeoutMillis);
            try {
                const headers = {Range: `bytes=${start}-${end}`};
                if (validator) headers['If-Range'] = validator.value;
                response = await fetch(url, {headers, credentials: 'same-origin', signal: control.signal});
                if (response.status === 404) throw new MissingInput('Input file was not found');
                if ([408, 425, 429, 500, 502, 503, 504].includes(response.status)) throw new RetryableStatus(response.status, response.headers.get('Retry-After'));
                if (expectedSize !== undefined && [200, 412, 416].includes(response.status)) throw new InputChanged('HTTP input identity/size changed or its range contract stopped being honored');
                if (response.status !== 206) throw new HttpProtocolError(`HTTP input requires206 byte-range responses (received ${response.status}); full-download fallback is disabled`);
                const total = contentRange(response.headers.get('Content-Range'), start, end, expectedSize);
                const encoding = response.headers.get('Content-Encoding');
                if (encoding && encoding.toLowerCase() !== 'identity') throw new HttpProtocolError('HTTP byte ranges require identity Content-Encoding');
                const length = response.headers.get('Content-Length');
                if (length != null && (!/^\d+$/.test(length) || Number(length) !== end - start + 1)) throw new HttpProtocolError('Invalid HTTP Content-Length');
                if (validator && response.headers.get(validator.header) !== validator.value) throw new InputChanged('HTTP input changed during reading');
                const bytes = await readExact(response, end - start + 1);
                return {total, bytes, etag: response.headers.get('ETag'), modified: response.headers.get('Last-Modified')};
            } catch (error) {
                if (error instanceof InputChanged) fatal = error;
                if (parent?.aborted || lifetime.signal.aborted || fatal) throw fatal || parent?.reason || lifetime.signal.reason || error;
                const transient = timedOut || error instanceof RetryableStatus || error instanceof TypeError;
                if (!transient || attempt >= policy.maxRetries) throw timedOut ? new DOMException('HTTP input request timed out', 'TimeoutError') : error;
                const delay = Math.min(policy.maxRetryDelayMillis, policy.retryDelayMillis * 2 ** attempt);
                retry = retryAfter(error.retryAfter, delay, policy.maxRetryDelayMillis);
            } finally {
                clearTimeout(timer); parent?.removeEventListener('abort', cancel); lifetime.signal.removeEventListener('abort', closed); controllers.delete(control);
                await response?.body?.cancel().catch(() => {});
            }
            // Cancellation and close also interrupt backoff; retries never outlive their owner.
            const backoff = new AbortController();
            const parentAbort = () => backoff.abort(parent?.reason || abortError());
            const closeAbort = () => backoff.abort(lifetime.signal.reason || abortError());
            parent?.addEventListener('abort', parentAbort, {once: true}); lifetime.signal.addEventListener('abort', closeAbort, {once: true});
            try { check(); await wait(retry, backoff.signal); }
            finally { parent?.removeEventListener('abort', parentAbort); lifetime.signal.removeEventListener('abort', closeAbort); }
        }
    }
    const initial = await fetchRange(0, 0);
    if (initial.etag?.startsWith('"') && initial.etag.endsWith('"')) validator = {header: 'ETag', value: initial.etag};
    else if (policy.requireStrongETag) throw new HttpProtocolError('HTTP input requires a strong ETag');
    else if (initial.modified && Number.isFinite(Date.parse(initial.modified))) validator = {header: 'Last-Modified', value: initial.modified};
    return {
        size: initial.total,
        check,
        async read(start, end) { return (await fetchRange(start, end, initial.total)).bytes; },
        close() {
            if (!fatal) fatal = new Error('HTTP input has been closed');
            lifetime.abort(fatal); for (const control of controllers) control.abort(fatal);
        },
    };
}
