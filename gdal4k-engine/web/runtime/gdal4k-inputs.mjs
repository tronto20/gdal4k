import {httpSource, MissingInput} from './gdal4k-http-input.mjs';
// Read-only random access. File contents never become a MEMFS or /vsimem file.
const CHUNK = 64 * 1024;
const CACHE_LIMIT = 8 * 1024 * 1024;
const nodeRuntime = typeof process !== 'undefined' && process.versions?.node;
const nodeFs = nodeRuntime ? process.getBuiltinModule('fs') : null;
const validName = name => typeof name === 'string' && name.length && name !== '.' && name !== '..' && !/[\/\\:\0]/.test(name);

export class Inputs {
    constructor() {
        this.sequence = 0;
        this.providers = new Map();
        this.directories = new Map();
        this.cache = new Map();
        this.cachedBytes = 0;
        this.lastError = null;
        this.closeResources = null;
        this.currentSignal = null;
    }
    beginCall(signal) { this.currentSignal = signal; }
    endCall() { this.currentSignal = null; }
    key(path) { return path.startsWith('/vsigdal4k/') ? path : '/vsigdal4k/' + path; }
    directory(path) {
        path = this.key(path);
        const match = /^\/vsigdal4k\/(\d+)(?:\/(.*))?$/.exec(path);
        return match ? [this.directories.get(match[1]), match[2] || ''] : [null, ''];
    }
    names(path) {
        const [directory] = this.directory(path);
        if (directory) return directory.names;
        path = this.key(path);
        if (this.providers.has(path)) path = path.slice(0, path.lastIndexOf('/') + 1);
        return this.list(path);
    }
    async provider(path) {
        path = this.key(path);
        if (this.providers.has(path)) return this.providers.get(path);
        const [directory, name] = this.directory(path);
        if (!directory || !validName(name) || directory.missing.has(name)) return null;
        let provider;
        try { provider = await directory.open(name); }
        catch (error) { if (error instanceof MissingInput) { directory.missing.add(name); return null; } throw error; }
        if (!provider) { directory.missing.add(name); return null; }
        if (!Number.isSafeInteger(provider.size) || provider.size < 0) { provider.close(); throw new Error('Unsupported input file size'); }
        this.providers.set(path, provider);
        return provider;
    }
    async size(path) {
        path = this.key(path);
        const file = this.providers.get(path);
        if (file) return file.size;
        const [directory, name] = this.directory(path);
        if (directory) return name ? (await this.provider(path))?.size ?? -1 : -2;
        const prefix = path.endsWith('/') ? path : path + '/';
        return [...this.providers.keys()].some(key => key.startsWith(prefix)) ? -2 : -1;
    }
    list(path) {
        path = this.key(path); const prefix = path.endsWith('/') ? path : path + '/';
        if (!prefix.startsWith('/vsigdal4k/resources/')) return [];
        return [...new Set([...this.providers.keys()].filter(key => key.startsWith(prefix)).map(key => key.slice(prefix.length).split('/')[0]))].sort();
    }
    registerResources(entries, url, size) {
        let source, opening;
        const open = async () => {
            if (!opening) opening = (async () => {
                if (nodeRuntime) {
                    const fd = nodeFs.openSync(url, 'r');
                    let stat;
                    try { stat = nodeFs.fstatSync(fd); } catch (error) { nodeFs.closeSync(fd); throw error; }
                    source = {size: stat.size, read(start, end) {
                        const bytes = new Uint8Array(end-start+1);
                        return bytes.subarray(0, nodeFs.readSync(fd, bytes, 0, bytes.length, start));
                    }, close() { nodeFs.closeSync(fd); }};
                } else source = await httpSource(url, {}, () => this.currentSignal);
                if (source.size !== size) throw new Error('Bundled GDAL resource asset size mismatch');
                return source;
            })().catch(error => { source?.close(); source = null; opening = null; throw error; });
            return opening;
        };
        for (const entry of entries) this.providers.set('/vsigdal4k/resources/' + entry.name, {
            size: entry.size,
            async read(start, end) { return (await open()).read(entry.offset + start, entry.offset + end); },
            close() {},
        });
        this.closeResources = () => { source?.close(); source = null; opening = null; };
    }
    close() {
        for (const provider of this.providers.values()) provider.close();
        this.providers.clear(); this.directories.clear(); this.cache.clear(); this.cachedBytes = 0;
        this.closeResources?.(); this.closeResources = null;
    }
    async read(path, position, length) {
        path = this.key(path);
        const provider = await this.provider(path);
        if (!provider) throw new Error('Input has been closed');
        provider.check?.();
        if (!Number.isSafeInteger(position) || position < 0) throw new Error('Invalid input offset');
        if (!Number.isSafeInteger(length) || length < 0) throw new Error('Invalid input length');
        const count = Math.min(length, Math.max(0, provider.size - position));
        const output = new Uint8Array(count);
        let copied = 0;
        while (copied < count) {
            const start = Math.floor((position + copied) / CHUNK) * CHUNK;
            const end = Math.min(start + CHUNK, provider.size) - 1;
            const key = path + ':' + start;
            let chunk = this.cache.get(key);
            if (chunk) this.cache.delete(key);
            else {
                chunk = await provider.read(start, end);
                if (chunk.length !== end - start + 1) throw new Error('Input changed or returned a short read');
                this.cachedBytes += chunk.length;
            }
            this.cache.set(key, chunk);
            while (this.cachedBytes > CACHE_LIMIT) {
                const oldest = this.cache.keys().next().value;
                this.cachedBytes -= this.cache.get(oldest).length;
                this.cache.delete(oldest);
            }
            const from = position + copied - start;
            const size = Math.min(count - copied, chunk.length - from);
            output.set(chunk.subarray(from, from + size), copied);
            copied += size;
        }
        return output;
    }
    async mount(source, attachment, policy = null) {
        if (policy && !/^https?:\/\//i.test(source)) throw new Error('HTTP input policy requires a direct HTTP(S) URL');
        let name, names, open;
        if (attachment && attachment.kind !== 'urls') {
            const files = attachment.kind === 'files' ? attachment.files : [attachment];
            const entries = new Map();
            for (const file of files) {
                if (!file || typeof file.slice !== 'function') throw new Error('Expected selected browser Files');
                if (!validName(file.name) || entries.has(file.name)) throw new Error('Invalid or duplicate input file name: ' + file.name);
                entries.set(file.name, file);
            }
            name = source;
            names = [...entries.keys()];
            open = async name => {
                const file = entries.get(name);
                return file ? {size: file.size, read: async (start, end) => new Uint8Array(await file.slice(start, end + 1).arrayBuffer()), close() {}} : null;
            };
        } else if (/^https?:\/\//i.test(source)) {
            const mainUrl = new URL(source);
            name = decodeURIComponent(mainUrl.pathname.split('/').pop()) || 'input';
            const mainName = name;
            const urls = attachment?.urls;
            names = urls ? [...new Set([name, ...Object.keys(urls)])] : null;
            if (names?.some(name => !validName(name))) throw new Error('Invalid related HTTP file name');
            open = name => {
                const url = name === mainName ? source
                    : urls ? (Object.hasOwn(urls, name) ? urls[name] : null) : new URL(encodeURIComponent(name), mainUrl).href;
                if (!url) return null;
                if (!/^https?:\/\//i.test(url)) throw new Error('Related inputs require HTTP(S) URLs');
                return httpSource(url, policy ?? {}, () => this.currentSignal);
            };
        } else {
            if (!nodeFs) throw new Error('Browser paths require selected Files or HTTP(S) URLs');
            const path = process.getBuiltinModule('path');
            const directory = path.dirname(source);
            name = path.basename(source);
            names = nodeFs.readdirSync(directory).filter(validName);
            open = name => {
                let fd;
                try {
                    fd = nodeFs.openSync(path.join(directory, name), 'r');
                    const stat = nodeFs.fstatSync(fd);
                    if (!stat.isFile()) { nodeFs.closeSync(fd); return null; }
                    return {size: stat.size, read(start, end) {
                        const buffer = new Uint8Array(end - start + 1);
                        const count = nodeFs.readSync(fd, buffer, 0, buffer.length, start);
                        return buffer.subarray(0, count);
                    }, close() { nodeFs.closeSync(fd); }};
                } catch (error) {
                    if (fd !== undefined) nodeFs.closeSync(fd);
                    if (error.code === 'ENOENT' || error.code === 'ENOTDIR') return null;
                    throw error;
                }
            };
        }
        if (!validName(name)) throw new Error('Invalid main input file name');
        const id = String(++this.sequence);
        const prefix = `/vsigdal4k/${id}/`;
        this.directories.set(id, {names, open, missing: new Set()});
        let closed = false;
        const mounted = {path: prefix + name, close: () => {
            if (closed) return;
            closed = true;
            this.directories.delete(id);
            let failure;
            for (const [path, provider] of this.providers) if (path.startsWith(prefix)) {
                this.providers.delete(path);
                try { provider.close(); } catch (error) { failure ??= error; }
            }
            for (const [key, value] of this.cache) if (key.startsWith(prefix)) {
                this.cache.delete(key); this.cachedBytes -= value.length;
            }
            if (failure) throw failure;
        }};
        try {
            if (!await this.provider(mounted.path)) throw new Error('Main input file was not found');
            return mounted;
        } catch (error) { mounted.close(); throw error; }
    }
}
