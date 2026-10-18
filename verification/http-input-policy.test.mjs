import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {httpSource, MissingInput, HttpProtocolError, httpPolicy} from '../gdal4k-engine/web/runtime/gdal4k-http-input.mjs';

const bytes = Buffer.from('0123456789abcdef');
async function fixture(block, handler) {
    const records = [];
    const server = http.createServer((request, response) => {
        const match = /^bytes=(\d+)-(\d+)$/.exec(request.headers.range || '');
        const start = match ? +match[1] : 0, end = match ? +match[2] : 0;
        records.push({start, end, headers: request.headers});
        const send = (headers = {}, content = bytes.subarray(start, end + 1)) => {
            response.writeHead(206, {'Content-Range': `bytes ${start}-${end}/${bytes.length}`, ETag: '"one"', ...headers}); response.end(content);
        };
        handler?.({request, response, start, end, records, send}) ?? send();
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    try { await block(`http://127.0.0.1:${server.address().port}/file`, records); }
    finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
}

test('range reads stay bounded and strong If-Range validator follows the file', async () => {
    await fixture(async (url, records) => {
        const source = await httpSource(url, {requireStrongETag: true});
        assert.equal(source.size, 16); assert.deepEqual(await source.read(3, 5), Uint8Array.from(bytes.subarray(3, 6)));
        assert.deepEqual(records.map(r => [r.start, r.end]), [[0, 0], [3, 5]]);
        assert.equal(records[1].headers['if-range'], '"one"'); source.close(); assert.throws(source.check, /closed/);
    });
});

test('retries503 with Retry-After but never downloads a full response', async () => {
    await fixture(async (url, records) => {
        const source = await httpSource(url, {maxRetries: 2, retryDelayMillis: 1, maxRetryDelayMillis: 5});
        assert.equal(records.length, 3); source.close();
    }, ({response, records, send}) => {
        if (records.length < 3) { response.writeHead(503, {'Retry-After': '0'}); response.end(); }
        else send();
        return true;
    });
});

for (const status of [200, 401, 403]) test(`HTTP${status} is permanent even when retries are selected`, async () => {
    await fixture(async (url, records) => {
        await assert.rejects(httpSource(url, {maxRetries: 3, retryDelayMillis: 1}), HttpProtocolError);
        assert.equal(records.length, 1);
    }, ({response}) => { response.writeHead(status); response.end('not a range'); return true; });
});

test('missing optional sidecars404 do not retry', async () => {
    await fixture(async (url, records) => { await assert.rejects(httpSource(url, {maxRetries: 4}), MissingInput); assert.equal(records.length, 1); },
        ({response}) => { response.writeHead(404); response.end(); return true; });
});

test('attempt deadline covers a stalled response body', async () => {
    await fixture(async (url, records) => {
        await assert.rejects(httpSource(url, {requestTimeoutMillis: 30, maxRetries: 1, retryDelayMillis: 1}), error => error.name === 'TimeoutError');
        assert.equal(records.length, 2);
    }, ({response}) => { response.writeHead(206, {'Content-Range': 'bytes 0-0/16', 'Content-Length': '1'}); response.flushHeaders(); return true; });
});

test('caller cancellation interrupts backoff instead of waiting for retry delay', async () => {
    await fixture(async (url, records) => {
        const controller = new AbortController();
        const pending = httpSource(url, {maxRetries: 5, retryDelayMillis: 10000, maxRetryDelayMillis: 10000}, () => controller.signal);
        setTimeout(() => controller.abort(new Error('caller cancelled')), 30);
        await assert.rejects(pending, /caller cancelled/); assert.equal(records.length, 1);
    }, ({response}) => { response.writeHead(503); response.end(); return true; });
});

test('changed ETag poisons later reads, including cached-read checks', async () => {
    await fixture(async (url, records) => {
        const source = await httpSource(url, {maxRetries: 3});
        await assert.rejects(source.read(2, 3), /changed/); assert.throws(source.check, /changed/);
        await assert.rejects(source.read(0, 0), /changed/); assert.equal(records.length, 2); source.close();
    }, ({records, send}) => { send({ETag: records.length === 1 ? '"one"' : '"two"'}); return true; });
});

test('If-Range full200 response poisons the existing provider without full-download fallback', async () => {
    await fixture(async url => {
        const source = await httpSource(url);
        await assert.rejects(source.read(2, 3), /identity\/size changed/); assert.throws(source.check, /identity\/size changed/); source.close();
    }, ({records, response, send}) => {
        if (records.length === 1) send(); else { response.writeHead(200); response.end(bytes); }
        return true;
    });
});

test('operation cancellation does not close the caller-owned source for its next operation', async () => {
    await fixture(async url => {
        let operation = new AbortController(); const source = await httpSource(url, {}, () => operation.signal);
        operation.abort(new Error('first operation cancelled'));
        await assert.rejects(source.read(2, 3), /first operation cancelled/);
        operation = new AbortController(); assert.deepEqual(await source.read(2, 3), Uint8Array.from(bytes.subarray(2, 4))); source.close();
    });
});

test('Last-Modified fallback is explicit and strong requirement rejects weak validators', async () => {
    const modified = 'Wed, 21 Oct 2015 07:28:00 GMT';
    await fixture(async (url, records) => {
        const source = await httpSource(url); await source.read(1, 2);
        assert.equal(records[1].headers['if-range'], modified); source.close();
        await assert.rejects(httpSource(url, {requireStrongETag: true}), /strong ETag/);
    }, ({send}) => { send({ETag: 'W/"one"', 'Last-Modified': modified}); return true; });
});

test('close aborts in-flight read and a later read cannot revive its provider', async () => {
    await fixture(async url => {
        const source = await httpSource(url); const pending = source.read(1, 4);
        setTimeout(() => source.close(), 30); await assert.rejects(pending, /closed/); await assert.rejects(source.read(0, 0), /closed/);
    }, ({records, response, start, end, send}) => {
        if (records.length === 1) send();
        else { response.writeHead(206, {'Content-Range': `bytes ${start}-${end}/16`, ETag: '"one"'}); response.flushHeaders(); }
        return true;
    });
});

test('protocol/length/encoding failures do not retry and offsets remain safe integers', async () => {
    for (const headers of [{'Content-Range': 'bytes 1-1/16'}, {'Content-Length': '2'}, {'Content-Encoding': 'gzip'}]) {
        await fixture(async (url, records) => {
            await assert.rejects(httpSource(url, {maxRetries: 3}), HttpProtocolError); assert.equal(records.length, 1);
        }, ({send}) => { send(headers); return true; });
    }
    assert.throws(() => httpPolicy({requestTimeoutMillis: 0}), /Invalid/);
    assert.throws(() => httpPolicy({maxRetries: -1}), /Invalid/);
    assert.throws(() => httpPolicy({extra: true}), /Unknown/);
});
