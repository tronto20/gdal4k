// Real GDAL Shapefile reads through local, selected-file and HTTP directory adapters.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';

const root = path.resolve(import.meta.dirname, '..');
const assets = path.resolve(process.argv[2] || path.join(root, 'gdal4k-engine/build/generated/web/assets'));
const {createGdalWorker} = await import(pathToFileURL(path.join(assets, 'index.mjs')));
const {Inputs} = await import(pathToFileURL(path.join(assets, 'gdal4k-inputs.mjs')));
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'gdal4k-related-'));
const request = (type, handle, method, parameters = {}) => JSON.stringify({op: 'runtime', args: {type, handle, method, parameters}});
async function call(bridge, type, handle, method, parameters = {}, attachment) {
    const response = JSON.parse(await bridge.call(request(type, handle, method, parameters), attachment));
    if (response.error) throw new Error(response.error);
    return response.result.value;
}

// Self-contained so the same verification runs inside a real browser.
async function verifyShape(bridge, source, attachment) {
    async function call(type, handle, method, parameters = {}, file) {
        const response = JSON.parse(await bridge.call(JSON.stringify({op: 'runtime', args: {type, handle, method, parameters}}), file));
        if (response.error) throw new Error(response.error);
        return response.result.value;
    }
    const id = await call('GdalRuntime', '0', 'openDataset_String_DatasetOpenOptions', {source, options: {update: false, kind: 4, options: [], ...(attachment?.kind === 'urls' ? {httpInputPolicy: {requestTimeoutMillis:5000,maxRetries:1,retryDelayMillis:0,maxRetryDelayMillis:10,requireStrongETag:true}} : {})}}, attachment);
    try {
        const result = await call('Dataset', id, 'translateVector_String_String_List_String', {driver: 'GeoJSON', name: 'out.geojson', arguments: ['-t_srs', 'EPSG:4326']});
        const bytes = Uint8Array.from(atob(result.files.find(f => f.name === result.main).data), c => c.charCodeAt(0));
        const feature = JSON.parse(new TextDecoder().decode(bytes)).features[0];
        if (feature.properties.name !== 'road-one') throw new Error('DBF sidecar attributes were lost');
        const [x, y] = feature.geometry.coordinates;
        if (Math.abs(x - 10) > 1e-7 || Math.abs(y - 45) > 1e-7) throw new Error('PRJ sidecar coordinates were lost');
    } finally { await call('Handle', id, 'close'); }
}

const requests = [];
const signed = new Map();
const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Headers', 'Range, If-Range');
    res.setHeader('Access-Control-Expose-Headers', 'Content-Range, ETag');
    if (req.method === 'OPTIONS') { res.writeHead(204).end(); return; }
    if (url.pathname === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<input id="files" type="file" multiple>'); return; }
    let file;
    if (url.pathname.startsWith('/assets/')) {
        const name = path.basename(url.pathname);
        file = path.join(assets, name);
        if (!fs.existsSync(file)) { res.writeHead(404).end(); return; }
        res.setHeader('Content-Type', name.endsWith('.wasm') ? 'application/wasm' : 'text/javascript');
        res.setHeader('Content-Length', fs.statSync(file).size);
        fs.createReadStream(file).pipe(res); return;
    }
    if (url.pathname.startsWith('/data/')) file = path.join(work, path.basename(decodeURIComponent(url.pathname)));
    else file = signed.get(req.url);
    requests.push({url: req.url, range: req.headers.range});
    if (!file || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
    const range = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range || '');
    const total = fs.statSync(file).size;
    if (!range) { res.writeHead(400).end(); return; }
    const start = +range[1], end = +range[2];
    if (start > end || end >= total) { res.writeHead(416).end(); return; }
    res.writeHead(206, {'Content-Range': `bytes ${start}-${end}/${total}`, 'Content-Length': end - start + 1, ETag: '"related-v1"'});
    fs.createReadStream(file, {start, end}).pipe(res);
});

let browser;
try {
    const bridge = await createGdalWorker();
    let fixture;
    try {
        const geojson = new TextEncoder().encode('{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[10,45]},"properties":{"name":"road-one"}}]}');
        const id = await call(bridge, 'GdalRuntime', '0', 'openDataset_GdalFiles_DatasetOpenOptions', {
            files: {main: 'points.geojson', files: [{name: 'points.geojson', data: Buffer.from(geojson).toString('base64')}]}, options: {update: false, kind: 4, options: []},
        });
        try { fixture = await call(bridge, 'Dataset', id, 'translateVector_String_String_List_String', {driver: 'ESRI Shapefile', name: 'roads.shp', arguments: ['-t_srs', 'EPSG:3857']}); }
        finally { await call(bridge, 'Handle', id, 'close'); }
        for (const f of fixture.files) fs.writeFileSync(path.join(work, f.name), Buffer.from(f.data, 'base64'));
        fs.writeFileSync(path.join(work, 'unused.bin'), 'not part of the dataset');
        await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
        const base = `http://127.0.0.1:${server.address().port}`;
        const relatedFiles = {};
        for (const [index, f] of fixture.files.entries()) {
            const route = `/private/${index}?signature=${index}`;
            signed.set(route, path.join(work, f.name));
            relatedFiles[f.name] = base + route;
        }
        // The main URL needs its real extension, while companion URLs may be opaque.
        const mainRoute = '/private/roads.shp?signature=main';
        signed.set(mainRoute, path.join(work, 'roads.shp'));
        delete relatedFiles['roads.shp'];
        await verifyShape(bridge, path.join(work, 'roads.shp'));
        await verifyShape(bridge, base + '/data/roads.shp');
        await verifyShape(bridge, base + mainRoute, {kind: 'urls', urls: relatedFiles});

        const sliced = [];
        const selected = fixture.files.map(f => {
            const bytes = Buffer.from(f.data, 'base64');
            return {name: f.name, size: bytes.length, slice(start, end) { sliced.push(f.name); return new Blob([bytes.subarray(start, end)]); }};
        });
        selected.push({name: 'unused.bin', size: 256 * 1024 * 1024, slice() { throw new Error('Unused file was read'); }});
        const inputs = new Inputs();
        const mounted = await inputs.mount('roads.shp', {kind: 'files', files: selected});
        assert.equal(sliced.length, 0, 'Mounting must not read file contents');
        assert.equal(inputs.providers.size, 1, 'Companions must not open eagerly');
        await inputs.read(mounted.path.replace('.shp', '.dbf'), 0, 8);
        assert.deepEqual(sliced, ['roads.dbf']);
        assert.equal(inputs.providers.size, 2);
        mounted.close();
        assert.equal(inputs.providers.size + inputs.directories.size + inputs.cachedBytes, 0);
        await assert.rejects(inputs.mount('roads.shp', {kind: 'files', files: [selected[0], selected[0]]}), /duplicate/);

        const require = createRequire(import.meta.url);
        const {chromium} = require(path.join(root, 'verification/node_modules/playwright'));
        browser = await chromium.launch({headless: true, ...(process.env.CHROME_BIN ? {executablePath: process.env.CHROME_BIN} : {}), args: process.platform === 'linux' ? ['--no-sandbox'] : []});
        const page = await browser.newPage();
        await page.goto(base);
        await page.locator('#files').setInputFiles([...fixture.files.map(f => path.join(work, f.name)), path.join(work, 'unused.bin')]);
        await page.evaluate(async ({verify, base, mainRoute, relatedFiles}) => {
            const {createGdalWorker} = await import('/assets/index.mjs');
            const bridge = await createGdalWorker();
            const run = (0, eval)('(' + verify + ')');
            try {
                const files = [...document.querySelector('#files').files];
                await run(bridge, 'roads.shp', {kind: 'files', files});
                await run(bridge, 'roads.shp', {kind: 'files', files});
                await run(bridge, base + '/data/roads.shp');
                await run(bridge, base + mainRoute, {kind: 'urls', urls: relatedFiles});
            } finally { await bridge.close(); }
        }, {verify: verifyShape.toString(), base, mainRoute, relatedFiles});
        assert(!requests.some(r => r.url.includes('unused.bin')));
        assert(requests.filter(r => r.range).every(r => /^bytes=\d+-\d+$/.test(r.range)));
        const label = process.argv[3];
        if (label && !['js','wasmJs'].includes(label)) throw new Error('Invalid related input target');
        const destination = path.join(root, 'verification/results/related-inputs' + (label ? '-' + label : ''));
        fs.mkdirSync(destination, {recursive: true});
        fs.writeFileSync(path.join(destination, 'web.json'), JSON.stringify({status: 'passed', platforms: ['Node', 'Chromium'], scenarios: ['Shapefile SHP/SHX/DBF/PRJ', 'native Node siblings', 'selected browser File collection', 'automatic HTTP relatives', 'individually signed HTTP sidecars with explicit timeout/retry/strong-ETag policy', 'lazy reads and bounded cache cleanup', 'duplicate names rejected'], requests}, null, 2));
        console.log('GDAL4K_RELATED_INPUTS_OK');
    } finally { await bridge.close(); }
} finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(work, {recursive: true, force: true});
}
