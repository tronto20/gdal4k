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
    const missing = path.join(directory, 'missing.shp');
    for (const mode of ['rt', 'rb', 'r+t', 'rt+', 'r+b', 'rb+']) {
        assert.equal(await outputs.open(outputs.path(missing), mode), 0);
        assert.equal(outputs.lastError, null, `${mode}: a missing read/update probe is an ordinary miss`);
        assert.equal(fs.existsSync(missing), false, `${mode}: probing must not create a file`);
    }
    const created = await outputs.open(outputs.path(missing), 'w+b');
    assert.ok(created, 'A missing update probe must permit subsequent creation');
    assert.equal(outputs.write(created, Uint8Array.of(65), 0), 1);
    outputs.close(created);
    assert.equal(fs.readFileSync(missing, 'utf8'), 'A');
    assert.equal(outputs.lastError, null);
    assert.equal(await outputs.open(outputs.path(path.join(file, 'child.bin')), 'w+b'), 0);
    assert.equal(outputs.lastError?.code, 'ENOTDIR', 'A genuine creation failure must remain fatal');
    outputs.lastError = null;
    assert.equal(outputs.handles.size, 0);
    console.log('GDAL4K_NODE_OUTPUT_STDIO_MODES_OK');
} finally {
    try { outputs.closeAll(); } finally { fs.rmSync(directory, {recursive: true, force: true}); }
}
