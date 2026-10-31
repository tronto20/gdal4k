#!/usr/bin/env python3
"""Copy an upload's explicit public files into a bounded audit-only transport.

The original GitHub artifact remains unchanged. This ZIP preserves each selected
workspace-relative member's bytes, not the original GitHub ZIP container bytes.
No credentials, networking, SDK calls or workflow dispatches are used here.
"""
import argparse
import fnmatch
import functools
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import zipfile

DOWNLOAD_LIMIT = 24 * 1024 * 1024
PART_BYTES = DOWNLOAD_LIMIT - 64 * 1024  # Room for the service's outer ZIP wrapper.
MAX_MANIFEST_BYTES = PART_BYTES
MAX_PARTS = 100
MAX_ARCHIVE_BYTES = PART_BYTES * MAX_PARTS
MAX_SOURCE_BYTES = 8 * 1024**3
MAX_FILES = 50000
BLOCK_BYTES = 1024 * 1024
SHA256 = re.compile(r'[0-9a-f]{64}\Z')
SHA1 = re.compile(r'[0-9a-f]{40}\Z')
RESERVED = re.compile(r'(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z')
PRIVATE_COMPONENTS = {'.git', '.hg', '.svn', 'v', 'pgdata', 'private', 'private-command-output', '.ssh'}
PRIVATE_NAMES = {'.env', '.pgpass', 'pgpass', 'pgpass.conf', 'passfile', 'initdb-password', 'id_rsa', 'id_ed25519'}


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def safe_relative(name, *, public=True):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('Noncanonical workspace-relative path')
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or path.as_posix() != name:
        raise ValueError('Absolute/noncanonical workspace path')
    for part in path.parts:
        if part in ('.', '..') or part.endswith(('.', ' ')) or RESERVED.fullmatch(part) or any(ord(c) < 32 or c in '<>"|?*' for c in part):
            raise ValueError('Unsafe workspace path component')
    if public and (any(p.casefold() in PRIVATE_COMPONENTS for p in path.parts)
                   or path.name.casefold() in PRIVATE_NAMES or path.name.casefold().endswith('.key')):
        raise ValueError('Private provider/credential/tool files are not audit inputs')
    return name


def safe_path(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Filesystem path traversal')
    for current in [path, *path.parents]:
        if current.is_symlink() or (current.exists() and getattr(current.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Link/reparse point in audit path')
    return path


def regular(path):
    path = safe_path(path)
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Audit input must be a regular file')
    return path


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def read_json(path):
    return json.loads(regular(path).read_text(encoding='utf-8'), object_pairs_hook=unique_object)


def validate_metadata(value):
    required = {'schemaVersion', 'headSha', 'checkoutSha', 'source', 'run', 'allowlist'}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - required - {'checkoutParents'}:
        raise ValueError('Invalid audit provenance schema')
    if type(value['schemaVersion']) is not int or value['schemaVersion'] != 1:
        raise ValueError('Unsupported audit provenance version')
    for key in ['headSha', 'checkoutSha']:
        if not isinstance(value[key], str) or not SHA1.fullmatch(value[key]):
            raise ValueError('Actual head and checkout SHA are required')
    parents = value.get('checkoutParents', [])
    if not isinstance(parents, list) or len(parents) > 8 or any(not isinstance(p, str) or not SHA1.fullmatch(p) for p in parents):
        raise ValueError('Invalid checkout parents')
    if value['headSha'] not in [value['checkoutSha'], *parents]:
        raise ValueError('Expected head is not the checkout or its direct parent')
    source = value['source']
    if not isinstance(source, dict) or set(source) != {'artifactName', 'artifactId', 'artifactDigest'}:
        raise ValueError('Original artifact identity is required')
    if not isinstance(source['artifactName'], str) or not source['artifactName'] or any(ord(c) < 32 for c in source['artifactName']):
        raise ValueError('Invalid original artifact name')
    if isinstance(source['artifactId'], bool) or not re.fullmatch('[1-9][0-9]*', str(source['artifactId'])):
        raise ValueError('Actual original artifact ID is required')
    if not isinstance(source['artifactDigest'], str) or not SHA256.fullmatch(source['artifactDigest']):
        raise ValueError('Actual original artifact SHA256 is required')
    run = value['run']
    if not isinstance(run, dict) or not {'id', 'attempt', 'job', 'repository'}.issubset(run) or set(run) - {'id', 'attempt', 'job', 'repository', 'githubSha'}:
        raise ValueError('Actual workflow run identity is required')
    for key in ['id', 'attempt']:
        if isinstance(run[key], bool) or not re.fullmatch('[1-9][0-9]*', str(run[key])):
            raise ValueError('Invalid workflow run/attempt')
    if not isinstance(run['repository'], str) or not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', run['repository']):
        raise ValueError('Invalid source repository')
    if not isinstance(run['job'], str) or not run['job'] or any(ord(c) < 32 for c in run['job']):
        raise ValueError('Invalid workflow job')
    if 'githubSha' in run and (not isinstance(run['githubSha'], str) or not SHA1.fullmatch(run['githubSha'])):
        raise ValueError('Invalid workflow event SHA')
    if not isinstance(value['allowlist'], list) or not value['allowlist'] or any(not isinstance(p, str) or not p for p in value['allowlist']):
        raise ValueError('Original public upload allowlist is required')
    return value


def normalize_allowlist(patterns, workspace):
    root = str(workspace).replace('\\', '/').rstrip('/')
    result = []
    for raw in patterns:
        pattern = raw.strip().replace('\\', '/')
        excluded = pattern.startswith('!')
        if excluded:
            pattern = pattern[1:]
        if pattern.casefold().startswith(root.casefold() + '/'):
            pattern = pattern[len(root) + 1:]
        if not pattern or pattern.startswith('/') or ':' in pattern or any(p in ('.', '..', '') for p in pattern.rstrip('/').split('/')) or any(ord(c) < 32 for c in pattern):
            raise ValueError('Public allowlist escapes the workspace')
        result.append(('!' if excluded else '') + pattern)
    return result


def matches_pattern(name, pattern):
    # Unlike fnmatch on a whole path, '*' cannot cross directory boundaries.
    if pattern.endswith('/'):
        pattern += '**'
    if not any(c in pattern for c in '*?['):
        return name == pattern.rstrip('/') or name.startswith(pattern.rstrip('/') + '/')
    wanted, actual = pattern.rstrip('/').split('/'), name.split('/')
    @functools.lru_cache(None)
    def match(i, j):
        if i == len(wanted):
            return j == len(actual)
        if wanted[i] == '**':
            return match(i + 1, j) or (j < len(actual) and match(i, j + 1))
        return j < len(actual) and fnmatch.fnmatchcase(actual[j], wanted[i]) and match(i + 1, j + 1)
    return match(0, 0)


def allowed(name, patterns):
    return (any(matches_pattern(name, p) for p in patterns if not p.startswith('!'))
            and not any(matches_pattern(name, p[1:]) for p in patterns if p.startswith('!')))


def zip_inventory(path):
    result, folded = [], set()
    with zipfile.ZipFile(regular(path)) as archive:
        if len(archive.infolist()) > MAX_FILES or sum(i.file_size for i in archive.infolist()) > MAX_SOURCE_BYTES:
            raise ValueError('Actual ZIP inventory exceeds the audit source bound')
        for info in archive.infolist():
            name = safe_relative(info.filename)
            if info.is_dir() or name.casefold() in folded or info.flag_bits & 1:
                raise ValueError('Unexpected directory/duplicate/encrypted audit ZIP member')
            folded.add(name.casefold())
            mode = info.external_attr >> 16
            if mode & 0o170000 and not stat.S_ISREG(mode):
                raise ValueError('Nonregular audit ZIP member')
            size, sha = 0, hashlib.sha256()
            with archive.open(info) as source:
                for block in iter(lambda: source.read(BLOCK_BYTES), b''):
                    size += len(block)
                    if size > info.file_size:
                        raise ValueError('Unexpected ZIP member size')
                    sha.update(block)
            if size != info.file_size:
                raise ValueError('Audit ZIP member size mismatch')
            result.append({'path': name, 'bytes': size, 'sha256': sha.hexdigest(), 'crc32': f'{info.CRC:08x}'})
    return result


def collect(workspace, file_list, output, metadata):
    workspace, output = safe_path(workspace), safe_path(output)
    metadata = validate_metadata(metadata)
    actual_checkout = subprocess.check_output(['git', '-C', str(workspace), 'rev-parse', 'HEAD'], text=True).strip()
    if actual_checkout != metadata['checkoutSha']:
        raise ValueError('Current checkout differs from audit provenance')
    commit = subprocess.check_output(['git', '-C', str(workspace), 'cat-file', '-p', 'HEAD'], text=True)
    actual_parents = [line.removeprefix('parent ') for line in commit.split('\n\n', 1)[0].splitlines() if line.startswith('parent ')]
    if actual_parents != metadata.get('checkoutParents', []):
        raise ValueError('Actual checkout parents differ from audit provenance')
    patterns = normalize_allowlist(metadata['allowlist'], workspace)
    if not isinstance(file_list, list) or not file_list or any(not isinstance(n, str) for n in file_list):
        raise ValueError('Explicit glob-selected file list is required')
    if len(set(file_list)) > MAX_FILES:
        raise ValueError('Too many public audit input files')
    files, folded, total = [], {}, 0
    for name in sorted(set(file_list)):
        safe_relative(name)
        if not allowed(name, patterns):
            raise ValueError('File is outside the original public upload allowlist: ' + name)
        if name.casefold() in folded:
            raise ValueError('Case-colliding public input paths')
        folded[name.casefold()] = name
        path = regular(workspace / name)
        if output == path or output.is_relative_to(path) or path.is_relative_to(output):
            raise ValueError('Audit output overlaps its selected input')
        total += path.stat().st_size
        if total > MAX_SOURCE_BYTES:
            raise ValueError('Uncompressed audit inputs exceed the 8 GiB source bound')
        files.append((name, path))
    output.mkdir(parents=True, exist_ok=False)
    archive_path = output / 'audit-content.zip'
    entries = []
    try:
        with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for name, path in files:
                before = path.stat()
                sha, size = hashlib.sha256(), 0
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                with path.open('rb') as source, archive.open(info, 'w', force_zip64=True) as destination:
                    for block in iter(lambda: source.read(BLOCK_BYTES), b''):
                        size += len(block)
                        if size > before.st_size:
                            raise ValueError('Public input grew during collection')
                        sha.update(block)
                        destination.write(block)
                        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
                            raise ValueError('Audit archive exceeds 100 bounded parts')
                after = regular(path).stat()
                if size != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or digest(path) != sha.hexdigest():
                    raise ValueError('Public input changed during collection')
                entries.append({'path': name, 'bytes': size, 'sha256': sha.hexdigest(), 'crc32': f'{archive.getinfo(name).CRC:08x}'})
        whole = {'bytes': archive_path.stat().st_size, 'sha256': digest(archive_path)}
        if whole['bytes'] > MAX_ARCHIVE_BYTES:
            raise ValueError('Audit archive exceeds 100 bounded parts')
        if zip_inventory(archive_path) != entries:
            raise ValueError('Collected ZIP differs from the selected original file bytes')
        exported, parts, offset = whole['bytes'] >= DOWNLOAD_LIMIT, [], 0
        if exported:
            with archive_path.open('rb') as source:
                while block := source.read(PART_BYTES):
                    index = len(parts) + 1
                    name = f'part-{index:03d}.bin'
                    with (output / name).open('xb') as destination:
                        destination.write(block)
                    parts.append({'index': index, 'name': name, 'offset': offset, 'bytes': len(block), 'sha256': hashlib.sha256(block).hexdigest()})
                    offset += len(block)
        manifest = {'schemaVersion': 1, 'exported': exported, 'metadata': metadata, 'publicAllowlist': patterns,
                    'transport': {'format': 'zip', 'contentIdentity': 'workspace-relative-repacked-files',
                                  'originalArtifactZipBytesPreserved': False},
                    'partSizeBytes': PART_BYTES, 'whole': whole, 'parts': parts, 'zipMembers': entries,
                    'collector': {'path': 'verification/package-audit-parts.py', 'sha256': digest(__file__)}}
        manifest_path = output / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8', newline='\n')
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError('Audit manifest exceeds its bounded transport size')
        return {**manifest, 'manifest': str(manifest_path), 'manifestSha256': digest(manifest_path),
                'parts': [{**item, 'path': str(output / item['name'])} for item in parts]}
    finally:
        archive_path.unlink(missing_ok=True)


def validate_manifest(directory, expected_sha256):
    path = regular(directory / 'manifest.json')
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('Audit manifest exceeds its bounded transport size')
    if not isinstance(expected_sha256, str) or not SHA256.fullmatch(expected_sha256) or digest(path) != expected_sha256:
        raise ValueError('Manifest differs from the independently bound SHA256')
    value = read_json(path)
    expected = {'schemaVersion', 'exported', 'metadata', 'publicAllowlist', 'transport', 'partSizeBytes', 'whole', 'parts', 'zipMembers', 'collector'}
    if not isinstance(value, dict) or set(value) != expected or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 or value['exported'] is not True:
        raise ValueError('Invalid/export-skipped audit manifest')
    validate_metadata(value['metadata'])
    if value['transport'] != {'format': 'zip', 'contentIdentity': 'workspace-relative-repacked-files', 'originalArtifactZipBytesPreserved': False}:
        raise ValueError('Unexpected audit container identity')
    if type(value['partSizeBytes']) is not int or value['partSizeBytes'] != PART_BYTES:
        raise ValueError('Unexpected bounded part size')
    whole, parts = value['whole'], value['parts']
    if not isinstance(whole, dict) or set(whole) != {'bytes', 'sha256'} or type(whole['bytes']) is not int or not DOWNLOAD_LIMIT <= whole['bytes'] <= MAX_ARCHIVE_BYTES or not isinstance(whole['sha256'], str) or not SHA256.fullmatch(whole['sha256']):
        raise ValueError('Invalid whole audit archive identity')
    if not isinstance(parts, list) or not parts or len(parts) > MAX_PARTS or len(parts) != (whole['bytes'] + PART_BYTES - 1) // PART_BYTES:
        raise ValueError('Invalid audit part count')
    offset = 0
    for index, item in enumerate(parts, 1):
        if not isinstance(item, dict) or set(item) != {'index', 'name', 'offset', 'bytes', 'sha256'}:
            raise ValueError('Invalid audit part schema')
        if type(item['index']) is not int or item['index'] != index or item['name'] != f'part-{index:03d}.bin' or type(item['offset']) is not int or item['offset'] != offset or type(item['bytes']) is not int or item['bytes'] != min(PART_BYTES, whole['bytes'] - offset) or not isinstance(item['sha256'], str) or not SHA256.fullmatch(item['sha256']):
            raise ValueError('Invalid audit part name/order/offset/size/digest')
        offset += item['bytes']
    patterns = value['publicAllowlist']
    if not isinstance(patterns, list) or not patterns or normalize_allowlist(patterns, directory) != patterns:
        raise ValueError('Noncanonical audit public allowlist')
    members, folded, total = value['zipMembers'], set(), 0
    if not isinstance(members, list) or not members or len(members) > MAX_FILES:
        raise ValueError('Missing selected-file byte identities')
    for item in members:
        if not isinstance(item, dict) or set(item) != {'path', 'bytes', 'sha256', 'crc32'}:
            raise ValueError('Invalid selected-file schema')
        name = safe_relative(item['path'])
        if not allowed(name, patterns) or name.casefold() in folded or type(item['bytes']) is not int or item['bytes'] < 0 or not isinstance(item['sha256'], str) or not SHA256.fullmatch(item['sha256']) or not isinstance(item['crc32'], str) or not re.fullmatch('[0-9a-f]{8}', item['crc32']):
            raise ValueError('Invalid selected-file path/byte identity')
        total += item['bytes']
        folded.add(name.casefold())
    if total > MAX_SOURCE_BYTES:
        raise ValueError('Declared source bytes exceed the audit bound')
    collector = value['collector']
    if not isinstance(collector, dict) or collector.get('path') != 'verification/package-audit-parts.py' or not isinstance(collector.get('sha256'), str) or not SHA256.fullmatch(collector['sha256']):
        raise ValueError('Missing actual collector source binding')
    if {p.name for p in directory.iterdir()} != {'manifest.json', *(item['name'] for item in parts)}:
        raise ValueError('Missing or extra audit part files')
    for item in parts:
        regular(directory / item['name'])
    return value


def reassemble(directory, output, manifest_sha256):
    directory, output = safe_path(directory), safe_path(output)
    manifest = validate_manifest(directory, manifest_sha256)
    if output.exists() or output.is_relative_to(directory) or not output.parent.is_dir():
        raise ValueError('Output must be a new file outside the part directory')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='gdal4k-audit-', dir=output.parent, delete=False) as destination:
            temporary = Path(destination.name)
            sha, total = hashlib.sha256(), 0
            for item in manifest['parts']:
                path = regular(directory / item['name'])
                if path.stat().st_size != item['bytes']:
                    raise ValueError('Audit part size mismatch')
                part_hash, size = hashlib.sha256(), 0
                with path.open('rb') as source:
                    for block in iter(lambda: source.read(BLOCK_BYTES), b''):
                        size += len(block)
                        if size > item['bytes']:
                            raise ValueError('Audit part grew during reassembly')
                        destination.write(block)
                        sha.update(block)
                        part_hash.update(block)
                if size != item['bytes'] or part_hash.hexdigest() != item['sha256']:
                    raise ValueError('Audit part SHA256 mismatch')
                total += size
        if {'bytes': total, 'sha256': sha.hexdigest()} != manifest['whole']:
            raise ValueError('Whole audit ZIP size/SHA256 mismatch')
        if zip_inventory(temporary) != manifest['zipMembers']:
            raise ValueError('Audit ZIP member bytes/CRC differ from the collected files')
        os.link(temporary, output)  # Publish without replacing a concurrent file.
        return {'result': 'passed', 'metadata': manifest['metadata'], 'whole': manifest['whole'],
                'zipMemberCount': len(manifest['zipMembers']), 'zipCrcAndMemberBytesVerified': True,
                'originalArtifactZipBytesPreserved': False}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    collect_args = sub.add_parser('collect')
    for name in ['workspace', 'file-list', 'output', 'metadata']:
        collect_args.add_argument('--' + name, type=Path, required=True)
    restore = sub.add_parser('reassemble')
    restore.add_argument('--directory', type=Path, required=True)
    restore.add_argument('--output', type=Path, required=True)
    restore.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    if args.command == 'collect':
        result = collect(args.workspace, read_json(args.file_list), args.output, read_json(args.metadata))
    else:
        result = reassemble(args.directory, args.output, args.manifest_sha256)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
