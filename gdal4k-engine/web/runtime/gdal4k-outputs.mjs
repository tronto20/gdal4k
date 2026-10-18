// Seekable disk-backed VSI files. Browser storage is private to this origin (OPFS).
// No whole-file byte array or MEMFS staging is involved.
const fs = typeof process !== 'undefined' && process.versions?.node ? process.getBuiltinModule('fs') : null;
const prefix = '/vsigdal4k-output/';
export class Outputs {
    constructor() { this.handles = new Map(); this.accesses = new Map(); this.sequence = 0; this.lastError = null; }
    path(path) {
        if (path.startsWith(prefix) || /^\/vsi[a-z0-9_]+\//i.test(path)) return path;
        if (fs) return prefix + path;
        if (!path.startsWith('opfs:/')) throw new Error('Browser disk output requires an opfs:/ path');
        const name = path.slice(6);
        this.parts(name);
        return prefix + name;
    }
    parts(name) {
        const parts = name.split('/');
        if (parts.some(p => !p || p === '.' || p === '..' || p.includes('\0') || p.includes('\\'))) throw new Error('Invalid OPFS path');
        return parts;
    }
    nativePath(path) { return path.startsWith(prefix) ? path.slice(prefix.length) : path; }
    async parent(path, create = false) {
        if (!globalThis.navigator?.storage?.getDirectory) throw new Error('This browser does not support OPFS');
        const parts = this.parts(this.nativePath(path));
        let directory = await navigator.storage.getDirectory();
        for (const name of parts.slice(0, -1)) directory = await directory.getDirectoryHandle(name, {create});
        return {directory, name: parts.at(-1)};
    }
    async open(path, mode) {
        const write = /[wa+]/.test(mode);
        try {
            // VRT proxies and COG CreateCopy can reopen a live dataset for reading.
            // Share positional reads even with the single writer's OPFS access handle.
            // Logical handles keep their own offset and read/write permissions.
            let shared = !fs && !write ? this.accesses.get(path) : null;
            if (shared) {
                shared.handle.flush();
                ++shared.references;
                const id = ++this.sequence;
                this.handles.set(id, {path, handle: shared.handle, write, shared});
                return id;
            }
            let handle;
            if (fs) {
                const fd = fs.openSync(this.nativePath(path), mode.replace(/[bt]/g, ''));
                handle = {read: (view, at) => fs.readSync(fd, view, 0, view.length, at), write: (view, at) => fs.writeSync(fd, view, 0, view.length, at), size: () => fs.fstatSync(fd).size, truncate: n => fs.ftruncateSync(fd, n), flush: () => fs.fsyncSync(fd), close: () => fs.closeSync(fd)};
            } else {
                const create = /^[wa]/.test(mode);
                const {directory, name} = await this.parent(path, create);
                const file = await directory.getFileHandle(name, {create});
                const access = await file.createSyncAccessHandle();
                handle = {read: (view, at) => access.read(view, {at}), write: (view, at) => access.write(view, {at}), size: () => access.getSize(), truncate: n => access.truncate(n), flush: () => access.flush(), close: () => access.close()};
                if (mode.startsWith('w')) handle.truncate(0);
            }
            if (!fs) {
                shared = {handle, references: 1};
                this.accesses.set(path, shared);
            }
            const id = ++this.sequence; this.handles.set(id, {path, handle, write, shared}); return id;
        } catch (error) {
            if (!write && (error.name === 'NotFoundError' || error.code === 'ENOENT' || error.code === 'ENOTDIR')) return 0;
            this.lastError = error; return 0;
        }
    }
    async size(path) {
        try {
            for (const entry of this.handles.values()) if (entry.path === path) return entry.handle.size();
            if (fs) return fs.statSync(this.nativePath(path)).size;
            const {directory, name} = await this.parent(path);
            return (await (await directory.getFileHandle(name)).getFile()).size;
        } catch (error) {
            if (error.name === 'NotFoundError' || error.code === 'ENOENT' || error.code === 'ENOTDIR') return -1;
            this.lastError = error; return -1;
        }
    }
    read(id, view, offset) { return this.handles.get(id).handle.read(view, offset); }
    write(id, view, offset) {
        const entry = this.handles.get(id);
        if (!entry.write) throw new Error('Output file is read-only');
        return entry.handle.write(view, offset);
    }
    truncate(id, size) {
        const entry = this.handles.get(id);
        if (!entry.write) throw new Error('Output file is read-only');
        entry.handle.truncate(size);
    }
    flush(id) { const e = this.handles.get(id); if (e.write) e.handle.flush(); }
    close(id) {
        const entry = this.handles.get(id);
        if (!entry) return;
        this.handles.delete(id);
        let failure;
        try { if (entry.write) entry.handle.flush(); } catch (error) { failure = error; }
        if (entry.shared) {
            if (--entry.shared.references === 0) {
                this.accesses.delete(entry.path);
                try { entry.handle.close(); } catch (error) { failure ??= error; }
            }
        } else {
            try { entry.handle.close(); } catch (error) { failure ??= error; }
        }
        if (failure) throw failure;
    }
    async unlink(path) {
        try {
            if (fs) fs.unlinkSync(this.nativePath(path));
            else { const {directory, name} = await this.parent(path); await directory.removeEntry(name); }
            return 0;
        } catch (error) { if (error.name !== 'NotFoundError' && error.code !== 'ENOENT') this.lastError = error; return -1; }
    }
    closeAll() { let failure; for (const id of [...this.handles.keys()]) { try { this.close(id); } catch (error) { failure ??= error; } } if (failure) throw failure; }
}
