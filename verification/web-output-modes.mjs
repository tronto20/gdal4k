// Real Node files exercise GDAL's C stdio modes and positional output semantics.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {Outputs} from '../gdal4k-engine/web/runtime/gdal4k-outputs.mjs';

const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gdal4k-output-modes-'));
const file = path.join(directory, 'data.bin');
const outputs = new Outputs();
const virtual = outputs.path(file);
try {
    const seed = () => fs.writeFileSync(file, 'ABC');
    seed();
    for (const mode of ['rt', 'rb', 'r']) {
        const id = await outputs.open(virtual, mode);
        assert.ok(id, `${mode}: ${outputs.lastError}`);
        const bytes = new Uint8Array(3);
        assert.equal(outputs.read(id, bytes, 0), 3);
        assert.deepEqual([...bytes], [65, 66, 67]);
        assert.throws(() => outputs.write(id, Uint8Array.of(88), 0), /read-only/);
        outputs.close(id);
    }
    for (const mode of ['r+t', 'rt+', 'r+b', 'rb+']) {
        seed();
        const id = await outputs.open(virtual, mode);
        assert.ok(id, `${mode}: ${outputs.lastError}`);
        assert.equal(outputs.write(id, Uint8Array.of(88), 1), 1);
        outputs.close(id);
        assert.equal(fs.readFileSync(file, 'utf8'), 'AXC');
    }
    for (const mode of ['w+t', 'wt+', 'w+b', 'wb+']) {
        seed();
        const id = await outputs.open(virtual, mode);
        assert.ok(id, `${mode}: ${outputs.lastError}`);
        assert.equal(outputs.handles.get(id).handle.size(), 0);
        assert.equal(outputs.write(id, Uint8Array.of(88), 0), 1);
        outputs.close(id);
        assert.equal(fs.readFileSync(file, 'utf8'), 'X');
    }
    for (const mode of ['a+t', 'at+', 'a+b', 'ab+']) {
        seed();
        const id = await outputs.open(virtual, mode);
        assert.ok(id, `${mode}: ${outputs.lastError}`);
        assert.equal(outputs.write(id, Uint8Array.of(88), outputs.handles.get(id).handle.size()), 1);
        outputs.close(id);
        assert.equal(fs.readFileSync(file, 'utf8'), 'ABCX');
    }
    assert.equal(await outputs.open(outputs.path(path.join(directory, 'missing.bin')), 'rt'), 0);
    assert.equal(outputs.lastError, null, 'Missing optional text sidecar is an ordinary miss');
    assert.equal(outputs.handles.size, 0);
    console.log('GDAL4K_NODE_OUTPUT_STDIO_MODES_OK');
} finally {
    try { outputs.closeAll(); } finally { fs.rmSync(directory, {recursive: true, force: true}); }
}
