"""Pinned portable Windows PostGIS fixture, confined to one owned private cluster.

Archive hashes establish exact first-observed official HTTPS bytes. They are not
a claim of a publisher's signature or independently published strong checksum.
"""
from contextlib import contextmanager
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
LOCK = Path(__file__).with_name('postgis-windows-fixture-lock.json')
TARGET = 'windows-amd64'
BINARY_NAMES = ('postgres', 'initdb', 'pg_ctl', 'psql')
RESERVED = re.compile(r'(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z')


def sha(path, algorithm='sha256'):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8', newline='\n')


def process_api():
    spec = importlib.util.spec_from_file_location('postgis_windows_process', ROOT / 'verification/windows_process.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def safe_name(name):
    """ZIP names must have one unambiguous ordinary Windows file interpretation."""
    if not isinstance(name, str) or not name or '\\' in name or any(c in name for c in '\0\r\n:'):
        raise ValueError('Unsafe portable-provider member name')
    name = name.removesuffix('/')
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or not path.parts:
        raise ValueError('Noncanonical portable-provider member path')
    for part in path.parts:
        if part in ('.', '..') or part[-1:] in ('.', ' ') or RESERVED.fullmatch(part):
            raise ValueError('Unsafe Windows member component')
        if any(ord(c) < 32 or c in '<>"|?*' for c in part):
            raise ValueError('Unsupported Windows member component')
    return path.as_posix()


def regular(path, boundary):
    path, boundary = Path(path).absolute(), Path(boundary).absolute()
    if '..' in path.parts or '..' in boundary.parts:
        raise ValueError('Noncanonical owned provider path')
    try:
        parts = path.relative_to(boundary).parts
    except ValueError:
        raise ValueError('Provider path is outside its owned boundary') from None
    for current in [boundary, *(boundary.joinpath(*parts[:i]) for i in range(1, len(parts)+1))]:
        if current.is_symlink() or (current.exists() and getattr(current.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Provider path traverses a link/reparse point')
    if not path.is_file():
        raise ValueError('Expected a regular owned provider file')
    return path


def pe_info(data, *, require_amd64=True):
    """Inspect PE architecture and regular/delay-load import names without execution."""
    if len(data) < 64 or data[:2] != b'MZ':
        raise ValueError('Missing PE DOS header')
    offset = struct.unpack_from('<I', data, 60)[0]
    if offset + 24 > len(data) or data[offset:offset+4] != b'PE\0\0':
        raise ValueError('Missing PE signature')
    machine, count, _, _, _, optional_size, _ = struct.unpack_from('<HHIIIHH', data, offset+4)
    optional = offset + 24
    if optional + optional_size + count*40 > len(data) or optional_size < 96:
        raise ValueError('Truncated PE image')
    magic = struct.unpack_from('<H', data, optional)[0]
    if require_amd64 and (machine != 0x8664 or magic != 0x20b):
        raise ValueError('Portable provider must use PE32+ AMD64 images')
    if magic == 0x20b and optional_size >= 112:
        directory_start, count_offset = 112, 108
        image_base = struct.unpack_from('<Q', data, optional+24)[0]
    elif magic == 0x10b:
        directory_start, count_offset = 96, 92
        image_base = struct.unpack_from('<I', data, optional+28)[0]
    else:
        raise ValueError('Unrecognized PE optional header')
    directories = struct.unpack_from('<I', data, optional+count_offset)[0]
    sections = []
    for i in range(count):
        start = optional + optional_size + i*40
        virtual_size, rva, raw_size, raw = struct.unpack_from('<IIII', data, start+8)
        if raw + raw_size > len(data):
            raise ValueError('Truncated PE section')
        sections.append((rva, max(virtual_size, raw_size), raw, raw_size))
    def position(rva, length=1):
        for start, size, raw, raw_size in sections:
            if start <= rva and rva+length <= start+size and rva-start+length <= raw_size:
                return raw+rva-start
        raise ValueError('PE import reference is outside a file section')
    def string(rva):
        start = position(rva)
        end = data.find(b'\0', start, min(len(data), start+1024))
        if end < 0:
            raise ValueError('Unterminated PE import name')
        name = data[start:end].decode('ascii')
        # PE imports are module basenames, not necessarily .dll: PostgreSQL
        # imports postgres.exe, Windows uses WINSPOOL.DRV, and MinGW uses '+'.
        if not re.fullmatch(r'[A-Za-z0-9_.+-]+', name) or safe_name(name) != name:
            raise ValueError('Unexpected PE dependency name')
        return name.lower()
    imports = set()
    for index, width, delay in [(1, 20, False), (13, 32, True)]:
        if directories <= index or directory_start+(index+1)*8 > optional_size:
            continue
        rva, size = struct.unpack_from('<II', data, optional+directory_start+index*8)
        if not rva:
            continue
        if size < width:
            raise ValueError('Invalid PE import-directory size')
        base = position(rva, min(width, size))
        ended = False
        for cursor in range(0, size, width):
            entry = data[base+cursor:base+cursor+width]
            if len(entry) != width:
                raise ValueError('Truncated PE import descriptor')
            if not any(entry):
                ended = True
                break
            if delay:
                attrs, name_rva = struct.unpack_from('<II', entry)
                if not attrs & 1:
                    name_rva -= image_base
            else:
                name_rva = struct.unpack_from('<I', entry, 12)[0]
            imports.add(string(name_rva))
        if not ended:
            raise ValueError('Unterminated PE import descriptor inventory')
    return {'machine': {0x8664:'AMD64',0x14c:'I386',0xaa64:'ARM64'}.get(machine,str(machine)),
            'eMachine':machine,'format':'PE32+' if magic==0x20b else 'PE32','imports':sorted(imports)}


def archive_inventory(path):
    files, directories, seen = {}, set(), {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            name = safe_name(info.filename)
            folded = name.casefold()
            if folded in seen:
                raise ValueError('Duplicate/case-colliding ZIP member')
            seen[folded] = name
            mode = info.external_attr >> 16
            if info.flag_bits & 1 or stat.S_ISLNK(mode) or info.external_attr & 0x400:
                raise ValueError('Encrypted/link/reparse ZIP member')
            if mode & 0o170000 and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError('Nonregular ZIP member')
            if info.is_dir():
                directories.add(folded)
                continue
            digest = hashlib.sha256()
            data = bytearray() if name.lower().endswith(('.exe', '.dll')) else None
            with archive.open(info) as stream:
                for block in iter(lambda: stream.read(1024*1024), b''):
                    digest.update(block)
                    if data is not None:
                        data.extend(block)
            value = {'bytes': info.file_size, 'sha256': digest.hexdigest(), 'crc32': f'{info.CRC:08x}'}
            if data is not None:
                try:
                    value['pe'] = pe_info(data, require_amd64=False)
                except ValueError as error:
                    value['peInspectionError'] = str(error)
            files[name] = value
        file_names = {x.casefold() for x in files}
        for name in files:
            parts = PurePosixPath(name.casefold()).parts
            if any('/'.join(parts[:i]) in file_names for i in range(1, len(parts))):
                raise ValueError('ZIP file shadows a parent directory')
    return files


def archive_identity(lock):
    inputs = {name: {key: value[key] for key in ['url', 'bytes', 'sha256', 'sha512']}
              for name, value in lock['archives'].items()}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def inventory_identity(files):
    canonical = {name: {key: item[key] for key in ('bytes', 'sha256')}
                 for name, item in files.items()}
    encoded = json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()
    return dict(fileCount=len(files), totalBytes=sum(v['bytes'] for v in files.values()),
                sha256=hashlib.sha256(encoded).hexdigest())


def validate_lock(lock):
    if lock.get('schemaVersion') != 1 or lock.get('target') != TARGET or set(lock.get('archives', {})) != {'postgres', 'postgis', 'microsoft'}:
        raise ValueError('Invalid portable Windows provider lock schema')
    if lock.get('serverVersionNum') != 170011 or lock.get('postgisVersion') != '3.6.2':
        raise ValueError('Unexpected pinned Windows database versions')
    prefixes = {'postgres': 'https://get.enterprisedb.com/postgresql/',
                'postgis': 'https://ftp.postgresql.org/pub/postgis/pg17/v3.6.2/win64/',
                'microsoft': 'https://download.microsoft.com/download/'}
    for name, value in lock['archives'].items():
        if type(value['bytes']) is not int or value['bytes'] <= 0 or value.get('format') not in ('zip', 'nsis', 'appx'):
            raise ValueError('Actual pinned archive size/format is required')
        for key, width in [('sha256', 64), ('sha512', 128)]:
            if not isinstance(value.get(key), str) or not re.fullmatch('[0-9a-f]{'+str(width)+'}', value[key]):
                raise ValueError('Actual strong archive digest is required')
        if not value['url'].startswith(prefixes[name]) or '?' in value['url'] or '#' in value['url']:
            raise ValueError('Provider archive must have exact official HTTPS provenance')
        identity = value.get('inventory', {})
        if type(identity.get('fileCount')) is not int or identity['fileCount'] <= 0 or type(identity.get('totalBytes')) is not int or not re.fullmatch('[0-9a-f]{64}', identity.get('sha256', '')):
            raise ValueError('Complete actual archive inventory identity is required')
    files = lock.get('providerFiles', {})
    if not files:
        raise ValueError('Actual final provider inventory is required')
    folded = set()
    for relative, item in files.items():
        safe_name(relative); safe_name(item['member'])
        if relative.casefold() in folded:
            raise ValueError('Case-colliding final provider paths')
        folded.add(relative.casefold())
        if item['archive'] not in lock['archives'] or type(item['bytes']) is not int or item['bytes'] < 0 or not re.fullmatch('[0-9a-f]{64}', item['sha256']):
            raise ValueError('Provider file has no exact original archive member identity')
        if relative.lower().endswith(('.exe', '.dll')) and item.get('pe', {}).get('machine') != 'AMD64':
            raise ValueError('Every selected provider executable image must be AMD64')
    for name in BINARY_NAMES:
        path = lock['binaryRelativePath']+'/'+name+'.exe'
        if path not in files or files[path]['archive'] != 'postgres':
            raise ValueError('Original PostgreSQL provider binary must be retained')
    if dependency_closure(files, lock['dependencyRoots'], lock['windowsSystemImports']) != lock['dependencyClosure']:
        raise ValueError('Provider dependency closure differs from the locked selected images')
    tool = lock.get('extractor')
    if tool:
        for name in ('bootstrap', 'bundle'):
            entry = tool[name]
            if not entry['url'].startswith('https://github.com/ip7z/7zip/releases/download/26.05/') or type(entry['bytes']) is not int or not re.fullmatch('[0-9a-f]{64}', entry['sha256']):
                raise ValueError('Full NSIS parser must have exact official release identity')
        if set(tool['payloads']) != {'7z.exe', '7z.dll', 'License.txt'}:
            raise ValueError('Full parser executable, DLL and license are required')
    if any(v['format']=='nsis' for v in lock['archives'].values()) and not tool:
        raise ValueError('NSIS requires the pinned full 7-Zip parser, never installer execution')
    return lock


def dependency_closure(files, roots, system_imports):
    images = {name: item for name, item in files.items() if 'pe' in item}
    by_name = {}
    for name in images:
        by_name.setdefault(PurePosixPath(name).name.lower(), []).append(name)
    pending, reached, edges = list(roots), set(), {}
    system = set(system_imports)
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        if name not in images or images[name]['pe']['machine'] != 'AMD64':
            raise ValueError('Missing/non-AMD64 portable dependency root')
        reached.add(name); edges[name] = {}
        for module in images[name]['pe']['imports']:
            matches = by_name.get(module, [])
            if len(matches) == 1:
                edges[name][module] = matches[0]; pending.append(matches[0])
            elif not matches and (module in system or module.startswith(('api-ms-win-', 'ext-ms-win-'))):
                edges[name][module] = 'Windows OS'
            else:
                raise ValueError('Unresolved/ambiguous portable dependency: '+module)
    return dict(roots=roots, reachableImages=sorted(reached), moduleEdges=edges)


def verify_archive(path, expected):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size != expected['bytes']:
        raise ValueError('Provider archive size/path differs from lock')
    for algorithm in ['sha256', 'sha512']:
        if algorithm in expected and sha(path, algorithm) != expected[algorithm]:
            raise ValueError('Provider archive strong digest differs from lock')
    if expected.get('md5') and sha(path, 'md5') != expected['md5']:
        raise ValueError('Provider archive differs from published corruption checksum')


def download_locked(path, expected):
    # Redirects are transport only: the exact downloaded bytes must match the
    # locked release digest. No authentication, installer or host installation.
    with urllib.request.urlopen(expected['url'], timeout=90) as response, Path(path).open('xb') as output:
        if response.status != 200 or not response.url.startswith('https://'):
            raise ValueError('Unexpected official archive response')
        count = 0
        for block in iter(lambda: response.read(1024*1024), b''):
            count += len(block)
            if count > expected['bytes']:
                raise ValueError('Official input exceeds its locked size')
            output.write(block)
    verify_archive(path, expected)


def parser_tool(work, lock, commands):
    tool = lock['extractor']; folder = work/'archive-parser'; folder.mkdir()
    for name in ('bootstrap', 'bundle'):
        entry = tool[name]; path = folder/entry['name']
        download_locked(path, entry)
    # 7zr is a release-pinned archive parser. The setup EXE is DATA passed to
    # that parser; neither provider installer nor 7-Zip installer is executed.
    payload = folder/'full'; payload.mkdir()
    commands.run('extract-full-nsis-parser', [folder/tool['bootstrap']['name'], 'x', '-y',
        '-o'+str(payload), folder/tool['bundle']['name'], '7z.exe', '7z.dll', 'License.txt'])
    for name, item in tool['payloads'].items():
        path = regular(payload/name, payload)
        if path.stat().st_size != item['bytes'] or sha(path) != item['sha256']:
            raise ValueError('Full archive parser payload differs from release')
        if name.endswith(('.exe', '.dll')) and pe_info(path.read_bytes()) != item['pe']:
            raise ValueError('Full archive parser architecture/import identity differs')
    public = commands.output/'parser'; public.mkdir()
    actual = {}
    for name, item in {**tool['payloads'], tool['bootstrap']['name']:tool['bootstrap']}.items():
        source = folder/name if name==tool['bootstrap']['name'] else payload/name
        destination = public/name; shutil.copyfile(source, destination)
        actual[name] = dict(path=str(source), publicPath='parser/'+name,
                            bytes=destination.stat().st_size, sha256=sha(destination))
    commands.receipt['extractor'] = dict(version=tool['version'],
        sourceInputs={name:tool[name] for name in ('bootstrap','bundle')}, payloads=actual)
    return payload/'7z.exe'


def nsis_names(text):
    names = []
    for block in text.replace('\r\n', '\n').strip().split('\n\n'):
        fields = dict(line.split(' = ', 1) for line in block.splitlines() if ' = ' in line)
        if not fields:
            continue
        name = safe_name(fields.get('Path', '').replace('\\', '/'))
        if fields.get('Attributes', '') or fields.get('Symbolic Link') or fields.get('Hard Link') or fields.get('Encrypted', '-') != '-':
            raise ValueError('NSIS parser reported nonordinary member attributes')
        names.append(name)
    if not names or len({v.casefold() for v in names}) != len(names):
        raise ValueError('Empty/duplicate NSIS member inventory')
    names_set = {v.casefold() for v in names}
    for name in names:
        parts = PurePosixPath(name.casefold()).parts
        if any('/'.join(parts[:i]) in names_set for i in range(1, len(parts))):
            raise ValueError('NSIS file shadows a parent directory')
    return names


def extract_provider(archives, work, lock, commands=None):
    """Audit every input member; copy only strongly locked portable files."""
    provider = work/'provider'; provider.mkdir()
    opened, nsis_roots, inventories = {}, {}, {}
    try:
        parser = None
        for name, path in archives.items():
            expected = lock['archives'][name]; verify_archive(path, expected)
            if expected['format'] == 'nsis':
                if commands is None:
                    raise ValueError('NSIS extraction requires recorded owned parser commands')
                if parser is None:
                    parser = parser_tool(work, lock, commands)
                listing = commands.run('list-nsis-input', [parser, 'l', '-slt', '-ba', '-sccUTF-8', path])
                names = nsis_names(listing.stdout.decode('utf-8-sig'))
                folder = work/('input-members-'+name); folder.mkdir()
                commands.run('extract-nsis-data', [parser, 'x', '-y', '-o'+str(folder), path], timeout=180)
                actual = {}
                for member in names:
                    source = regular(folder/member, folder)
                    actual[member] = dict(bytes=source.stat().st_size, sha256=sha(source))
                actual_names = {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
                if actual_names != set(names):
                    raise ValueError('NSIS extraction differs from validated member list')
                nsis_roots[name] = folder
            else:
                actual = archive_inventory(path)
                opened[name] = zipfile.ZipFile(path)
            if inventory_identity(actual) != expected['inventory']:
                raise ValueError('Actual complete input member inventory differs from lock')
            inventories[name] = actual
            if commands is not None:
                commands.receipt.setdefault('inputInventoryVerified', {})[name] = inventory_identity(actual)
        for name, value in lock['providerFiles'].items():
            original = inventories[value['archive']][value['member']]
            if any(original[key] != value[key] for key in ('bytes', 'sha256')):
                raise ValueError('Selected provider member differs from full input audit')
            destination = provider/safe_name(name); destination.parent.mkdir(parents=True, exist_ok=True)
            source_archive = value['archive']
            if source_archive in nsis_roots:
                with regular(nsis_roots[source_archive]/value['member'], nsis_roots[source_archive]).open('rb') as source, destination.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024*1024)
            else:
                with opened[source_archive].open(value['member']) as source, destination.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024*1024)
            if destination.stat().st_size != value['bytes'] or sha(destination) != value['sha256']:
                raise ValueError('Owned provider extraction differs from original member')
            if 'pe' in value and pe_info(destination.read_bytes()) != value['pe']:
                raise ValueError('Selected provider PE dependency identity differs')
        if {p.relative_to(provider).as_posix() for p in provider.rglob('*') if p.is_file()} != set(lock['providerFiles']):
            raise ValueError('Unexpected extra file in the owned provider')
        return provider
    finally:
        for archive in opened.values():
            archive.close()


def safe_dsn(user, database, port):
    if user != database or not re.fullmatch('[a-z][a-z0-9_]{0,62}', user):
        raise ValueError('Unsafe owned fixture database/user')
    if type(port) is not int or not 0 < port < 65536:
        raise ValueError('Invalid private fixture port')
    return f'PG:host=127.0.0.1 port={port} dbname={database} user={user}'


def server_environment(binary, provider, work):
    # Only the server and its clients see these EDB/PostGIS DLL and data paths.
    excluded = {'path', 'gdal_data', 'gdal_driver_path', 'proj_data', 'proj_lib',
                'curl_ca_bundle', 'ssl_cert_file', 'ld_library_path'}
    result = {key:value for key,value in os.environ.items()
              if key.lower() not in excluded and not key.upper().startswith(('PG', 'DYLD_'))}
    system = Path(os.environ['SystemRoot'])
    result.update(PATH=os.pathsep.join([str(binary), str(system/'System32'), str(system)]),
                  LC_ALL='C', LANG='C', PGCLIENTENCODING='UTF8', PROJ_NETWORK='OFF',
                  PROJ_USER_WRITABLE_DIRECTORY=str(work/'proj-user'),
                  GDAL_DATA=str(provider/'gdal-data'), PROJ_DATA=str(provider/'share/contrib/postgis-3.6/proj'))
    return result


class Commands:
    def __init__(self, output, work, receipt, api):
        self.output, self.work, self.receipt, self.api = output, work, receipt, api
        self.private_values = []
        self.persistent_jobs = []
        (output/'commands').mkdir()

    def sanitize(self, value):
        for secret in self.private_values:
            value = value.replace(secret.encode('utf-8'), b'[fixture-secret-redacted]')
        return value

    def run(self, label, argv, *, env=None, timeout=60, check=True, retain_tree=False):
        label = re.sub('[^a-zA-Z0-9_-]', '-', label)
        prefix = f'commands/{len(self.receipt["commands"]):03d}-{label}'
        argv = [str(value) for value in argv]
        record = dict(label=label, argv=[self.sanitize(value.encode()).decode() for value in argv],
                      cwd=str(self.work), startedAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      stdout=prefix+'.stdout', stderr=prefix+'.stderr', timeoutSeconds=timeout)
        failure = None
        owned = None
        result = None
        stdout,stderr=b'',b''
        capture = []
        try:
            if retain_tree:
                # pg_ctl's surviving Windows shell can inherit its output handles.
                # Wait for the CLI PID, not PIPE EOF from the whole retained tree.
                for stream in ('stdout', 'stderr'):
                    capture.append((self.work/('retained-'+Path(record[stream]).name)).open('xb+'))
                record['outputCapture'] = 'owned-files-direct-process-wait'
            owned = self.api.OwnedProcess(argv,cwd=self.work,env=env,
                stdout=capture[0] if capture else subprocess.PIPE,
                stderr=capture[1] if capture else subprocess.PIPE)
            record['windowsProcess'] = owned.identity
            try:
                if capture:
                    owned.process.wait(timeout=timeout)
                else:
                    stdout,stderr = owned.process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                owned.terminate_tree()
                if capture:
                    owned.process.wait(timeout=30)
                else:
                    stdout,stderr = owned.process.communicate(timeout=30)
                record['timedOut'] = True
                raise subprocess.TimeoutExpired(argv,timeout,output=stdout,stderr=stderr)
            result = subprocess.CompletedProcess(argv,owned.process.returncode,stdout,stderr)
            record['returncode'] = result.returncode
            if retain_tree and result.returncode==0:
                self.persistent_jobs.append(owned)
                record['ownedTreeRetainedForFixtureLifetime'] = True
                owned = None
        except BaseException as error:
            result, failure = None, error
            stdout = getattr(error,'stdout',None) or stdout
            stderr = getattr(error,'stderr',None) or stderr
            record.update(returncode=None, executionFailureType=type(error).__name__)
            if isinstance(error, subprocess.TimeoutExpired):
                record['timedOut'] = True
        finally:
            if capture:
                try:
                    for stream in capture: stream.flush(); stream.seek(0)
                    stdout,stderr = (stream.read() for stream in capture)
                finally:
                    for stream in capture: stream.close()
            if owned is not None:
                try:
                    record['ownedTreeCleanup'] = owned.close()
                except BaseException as error:
                    record['ownedTreeCleanupFailureType']=type(error).__name__
                    if failure is None:failure=error
                    else:failure.add_note('Owned command cleanup also failed: '+str(error))
        if record.get('ownedTreeCleanup',{}).get('forcedTreeTermination') and failure is None:
            failure = RuntimeError('Unexpected surviving descendant after a short provider command')
        record['failed']=failure is not None or (result is not None and result.returncode!=0)
        for stream, data in [('stdout', stdout), ('stderr', stderr)]:
            public = self.sanitize(data)
            if public != data:
                folder = self.work/'private-command-output'
                folder.mkdir(exist_ok=True)
                self.api.private_directory(folder)
                path = folder/Path(record[stream]).name
                path.write_bytes(data)
                self.api.private_file(path)
                record['fixtureSecretRedacted'] = True
            path = self.output/record[stream]
            path.write_bytes(public)
            record[stream+'Sha256'] = sha(path)
        record['completedAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.receipt['commands'].append(record)
        write_json(self.output/'fixture.json', self.receipt)
        if isinstance(failure,(KeyboardInterrupt,SystemExit)):
            raise failure
        if failure is not None or (check and result.returncode):
            raise RuntimeError('Owned Windows PostGIS command failed: '+label+'; inspect '+prefix) from failure
        return result

    def close_persistent_trees(self):
        results=[]
        while self.persistent_jobs:
            owned=self.persistent_jobs.pop()
            # The direct postgres PID can exit just before its retained cmd.exe
            # ancestor. Allow this owned Job a bounded graceful drain first.
            deadline = time.monotonic()+10
            while owned.api.active_processes(owned.job) and time.monotonic()<deadline:
                time.sleep(0.05)
            drained = owned.api.active_processes(owned.job)==0
            results.append(dict(windowsProcess=owned.identity, gracefulDrainCompleted=drained,
                                gracefulDrainTimeoutSeconds=10, **owned.close()))
        return results


class Server:
    def __init__(self, work, provider, lock, commands, receipt, api):
        self.work, self.provider, self.lock = work, provider, lock
        self.commands, self.receipt, self.api = commands, receipt, api
        self.binary = provider/lock['binaryRelativePath']
        self.pgdata, self.private = work/'pgdata', work/'private'
        self.pgdata.mkdir(); self.private.mkdir()
        receipt['privateAcl'] = {'work':api.private_directory(work),
                                 'pgdata':api.private_directory(self.pgdata),
                                 'private':api.private_directory(self.private)}
        self.token, self.password = secrets.token_hex(16), secrets.token_hex(24)
        commands.private_values.append(self.password)
        self.owner = {'token':self.token, 'dataDirectory':str(self.pgdata), 'providerRoot':str(provider)}
        write_json(work/'ownership.json', self.owner)
        api.private_file(work/'ownership.json')
        self.user = self.database = 'g4k_'+secrets.token_hex(8)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); self.port = listener.getsockname()[1]
        self.dsn = safe_dsn(self.user, self.database, self.port)
        self.password_file, self.passfile = self.private/'initdb-password', self.private/'pgpass'
        self.password_file.write_text(self.password+'\n', encoding='utf-8', newline='\n')
        self.passfile.write_text(f'127.0.0.1:{self.port}:*:{self.user}:{self.password}\n', encoding='utf-8', newline='\n')
        receipt['privateAcl'].update(initdbPassword=api.private_file(self.password_file), pgpass=api.private_file(self.passfile))
        self.env = server_environment(self.binary, provider, work)
        self.env.update(PGPASSFILE=str(self.passfile), PGCONNECT_TIMEOUT='10')
        self.identity = None
        self.start_requested = False
        self.started_after = None
        receipt['connection'] = {'dsn':self.dsn, 'passwordIncluded':False}
        receipt['server'] = {'dataDirectory':str(self.pgdata), 'port':self.port,
                             'listenAddresses':['127.0.0.1']}

    def run(self, label, argv, **kwargs):
        return self.commands.run(label, argv, env=self.env, **kwargs)

    def sql(self, label, statement, database=None):
        return self.run(label, [self.binary/'psql.exe', '-X', '-h', '127.0.0.1', '-p', str(self.port),
                '-U', self.user, '-d', database or self.database, '-v', 'ON_ERROR_STOP=1', '-At', '-c', statement]
                ).stdout.decode('utf-8').strip()

    def owned_identity(self):
        if json.loads(regular(self.work/'ownership.json', self.work).read_text(encoding='utf-8')) != self.owner:
            raise ValueError('Owned cluster marker changed; refusing process mutation')
        pidfile = self.pgdata/'postmaster.pid'
        if not pidfile.exists():
            return None
        lines = regular(pidfile, self.work).read_text(encoding='utf-8').splitlines()
        if (len(lines) < 4 or not lines[0].isdigit() or Path(lines[1]).resolve() != self.pgdata.resolve()
                or lines[3] != str(self.port)):
            raise ValueError('Postmaster PID file does not identify the owned cluster')
        try:
            identity = self.api.process_identity(int(lines[0]))
        except ProcessLookupError:
            return None
        if identity is None:
            return None
        if Path(identity['imagePath']).resolve() != (self.binary/'postgres.exe').resolve():
            raise ValueError('Postmaster image is outside the owned provider')
        if self.started_after is None or identity['creationTime100ns'] < self.started_after:
            raise ValueError('Postmaster predates this owned start request')
        if self.identity is not None and identity != self.identity:
            raise ValueError('Postmaster PID identity changed; refusing a different process')
        return identity

    def start(self):
        self.run('initdb', [self.binary/'initdb.exe', '-D', self.pgdata, '--username='+self.user,
                 '--encoding=UTF8', '--locale=C', '--auth-local=scram-sha-256', '--auth-host=scram-sha-256',
                 '--pwfile='+str(self.password_file)], timeout=120)
        # No nested -o shell quoting, service registration, account or registry mutation.
        with (self.pgdata/'postgresql.auto.conf').open('a', encoding='utf-8', newline='\n') as config:
            config.write("\nlisten_addresses = '127.0.0.1'\nport = "+str(self.port)+"\nssl = off\nlogging_collector = off\n")
        self.start_requested = True
        self.started_after = int((datetime.datetime.now(datetime.timezone.utc).timestamp()+11644473600)*10000000)
        self.run('pg-ctl-start', [self.binary/'pg_ctl.exe', '-D', self.pgdata, '-l', self.work/'server.log',
                                '-w', '-t', '60', 'start'], timeout=90,retain_tree=True)
        self.identity = self.owned_identity()
        if self.identity is None or not self.api.same_process(self.identity):
            raise RuntimeError('Owned postmaster did not start')
        listener = self.api.port_owner(self.port)
        if (listener is None or listener.get('localAddress') != '127.0.0.1'
                or any(listener.get(key) != self.identity[key] for key in ['pid','creationTime100ns','imagePath'])):
            raise RuntimeError('Private fixture listener does not belong to its postmaster')
        self.receipt['server'].update(postmasterPid=self.identity['pid'], processIdentity=self.identity, listener=listener)
        self.sql('create-owned-database', f'CREATE DATABASE {self.database} OWNER {self.user};', 'postgres')
        self.sql('create-postgis-extension', 'CREATE EXTENSION postgis;')
        observed = json.loads(self.sql('server-identity', "SELECT json_build_object("
            "'dataDirectory',current_setting('data_directory'),'port',current_setting('port'),"
            "'serverVersion',current_setting('server_version'),'serverVersionNum',current_setting('server_version_num'),"
            "'encoding',current_setting('server_encoding'),'collation',(SELECT datcollate FROM pg_database WHERE datname=current_database()),"
            "'listenAddresses',current_setting('listen_addresses'),'version',version(),'postgisFullVersion',PostGIS_Full_Version(),"
            "'postgisVersion',(SELECT extversion FROM pg_extension WHERE extname='postgis'))::text;"))
        if (Path(observed['dataDirectory']).resolve() != self.pgdata.resolve() or observed['port'] != str(self.port)
                or observed['listenAddresses'] != '127.0.0.1' or observed['encoding'] != 'UTF8'
                or int(observed['serverVersionNum']) != self.lock['serverVersionNum']
                or observed['postgisVersion'] != self.lock['postgisVersion']):
            raise RuntimeError('Actual portable PostgreSQL/PostGIS identity differs from its lock')
        self.receipt['server'].update({key:value for key,value in observed.items() if key not in ['port','dataDirectory','listenAddresses']})
        self.receipt['server']['serverVersionNum'] = int(observed['serverVersionNum'])
        self.receipt['startup'] = dict(completed=True, healthy=True, dataDirectoryVerified=True, postgisExtensionVerified=True)
        table = 'availability_'+secrets.token_hex(8)
        if self.sql('owned-table-absent', f"SELECT to_regclass('public.{table}') IS NULL;") != 't':
            raise RuntimeError('Refusing an existing availability table')
        try:
            self.sql('point-write-commit', f'BEGIN; CREATE TABLE {table}(value integer NOT NULL,geom geometry(Point,4326)); '
                     f'INSERT INTO {table} VALUES(42,ST_SetSRID(ST_Point(3,4),4326)); COMMIT;')
            value = json.loads(self.sql('point-new-connection-read', "SELECT json_build_object('value',value,'x',ST_X(geom),"
                     f"'y',ST_Y(geom),'srid',ST_SRID(geom),'count',(SELECT count(*) FROM {table}))::text FROM {table};"))
            if value != {'value':42,'x':3,'y':4,'srid':4326,'count':1}:
                raise RuntimeError('Owned point persistence roundtrip failed')
            self.receipt['roundTrip'] = dict(table=table, values=value, newConnectionVerified=True)
        finally:
            self.sql('drop-exact-owned-table', f'DROP TABLE IF EXISTS {table};')
            if self.sql('owned-table-absent-after', f"SELECT to_regclass('public.{table}') IS NULL;") != 't':
                raise RuntimeError('Owned table remains after cleanup')
            self.receipt.setdefault('roundTrip', {})['tableDeletedAndAbsentNewConnection'] = True

    def _stop_postmaster(self):
        current = self.owned_identity()
        if self.identity is not None and self.api.same_process(self.identity) and current is None:
            raise RuntimeError('Live owned postmaster lost its PID file; refusing unverified stop')
        if current is not None and self.identity is None:
            self.identity = current  # Verified exact owned image/time/PGDATA after a start timeout.
        code = None
        if current is not None:
            if not self.api.same_process(current):
                raise RuntimeError('Owned postmaster identity changed immediately before stop')
            result = self.run('pg-ctl-stop', [self.binary/'pg_ctl.exe', '-D', self.pgdata,
                              '-m', 'fast', '-w', '-t', '60', 'stop'], timeout=90, check=False)
            code = result.returncode
        exited = self.identity is None or self.api.wait_process_exit(self.identity, 10)
        closed = self.api.port_closed(self.port)
        stopped = exited and closed and not (self.pgdata/'postmaster.pid').exists()
        self.receipt['cleanup'] = dict(serverStopped=stopped, ownedPostmasterExited=exited,
            portClosed=closed, pgCtlStopReturncode=code, privateCredentialFilesRemoved=False)
        if not stopped or (code is not None and code != 0) or (self.identity is not None and code is None):
            raise RuntimeError('Owned Windows PostgreSQL shutdown is incomplete')
    def stop(self):
        error=None
        try:
            self._stop_postmaster()
        except BaseException as failure:
            error=failure
        finally:
            try:
                jobs=self.commands.close_persistent_trees()
                cleanup=self.receipt.setdefault('cleanup',{})
                cleanup['retainedCommandTrees']=jobs
                cleanup['ownedCommandTreesClosed']=all(j['jobClosed'] and j['activeProcessesAfter']==0 and j['gracefulDrainCompleted'] for j in jobs)
                cleanup['forcedOwnedTreeTermination']=any(j.get('forcedTreeTermination') for j in jobs)
                if not cleanup['ownedCommandTreesClosed'] or cleanup['forcedOwnedTreeTermination']:
                    raise RuntimeError('Owned server process tree required forced or incomplete cleanup')
            except BaseException as failure:
                if error is None:error=failure
                else:error.add_note('Owned server command-tree cleanup also failed: '+str(failure))
        if error is not None:raise error
        for path in [self.password_file,self.passfile]:regular(path,self.work).unlink()
        self.receipt['cleanup']['privateCredentialFilesRemoved']=True


@contextmanager
def server_scope(server):
    failure = None
    try:
        server.start()
        yield server
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            server.stop()
        except BaseException as cleanup:
            if failure is None:
                raise
            failure.add_note('Owned Windows PostGIS cleanup also failed: '+str(cleanup))


def claim_output(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Noncanonical public fixture output')
    boundary = ROOT/'verification/results'
    try:
        relative = path.relative_to(boundary)
    except ValueError:
        raise ValueError('Public fixture output must remain in owned verification/results') from None
    for current in [boundary, *(boundary.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts)+1))]:
        if current.is_symlink() or (current.exists() and getattr(current.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Public fixture output traverses a link/reparse point')
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError('Refusing nonempty fixture output')
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def fixture(output, target=TARGET, archives=None):
    """Keep the owned server alive across source, independent consumer and probe work."""
    if target != TARGET or platform.system() != 'Windows' or platform.machine().lower() not in ['amd64','x86_64']:
        raise ValueError('Portable PostGIS fixture requires actual Windows AMD64')
    lock = validate_lock(json.loads(LOCK.read_text(encoding='utf-8')))
    output = claim_output(output)
    parent = ROOT/'verification/build/postgis-fixtures'/TARGET
    parent.mkdir(parents=True, exist_ok=True)
    required = sum(item['bytes'] for item in lock['providerFiles'].values()) + 512*1024**2 + sum(v['inventory']['totalBytes'] for v in lock['archives'].values() if v['format']=='nsis')
    if archives is None:
        required += sum(item['bytes'] for item in lock['archives'].values())
    if shutil.disk_usage(ROOT).free < required:
        raise RuntimeError('Insufficient owned portable-provider/cluster headroom')
    work = Path(tempfile.mkdtemp(prefix='owned-', dir=parent)).resolve()
    api = process_api()
    acl = api.private_directory(work)
    source_names = [Path(__file__).resolve(), LOCK, ROOT/'verification/windows_process.py']
    source_hashes = {p.relative_to(ROOT).as_posix():sha(p) for p in source_names}
    receipt = dict(schemaVersion=1, target=target, result='pending', sourceSha256=source_hashes,
        archiveIdentitySha256=archive_identity(lock),
        archives={name:{key:value[key] for key in ['url','bytes','sha256','sha512','provenance','md5','format','inventory'] if key in value}
                  for name,value in lock['archives'].items()},
        startup=dict(completed=False,healthy=False,dataDirectoryVerified=False,postgisExtensionVerified=False),
        privateAcl={'work':acl}, cleanup={}, commands=[])
    commands = Commands(output, work, receipt, api)
    completed, failure = False, None
    provider = None
    try:
        if archives is None:
            archives = {}
            for name, value in lock['archives'].items():
                archive = work/('input-'+name+'.'+value.get('format','zip'))
                download_locked(archive, value)
                archives[name] = archive
        else:
            if set(archives) != set(lock['archives']):
                raise ValueError('Exact three official input archives required')
            archives = {name:Path(path).absolute() for name,path in archives.items()}
        provider = extract_provider(archives, work, lock, commands)
        inventory = {name:{'bytes':regular(provider/name, provider).stat().st_size,
                           'sha256':sha(provider/name)} for name in lock['providerFiles']}
        write_json(output/'provider-files.json', inventory)
        (output/'binaries').mkdir()
        binaries = {}
        for name in BINARY_NAMES:
            path = regular(provider/lock['binaryRelativePath']/(name+'.exe'), provider)
            data = path.read_bytes(); pe = pe_info(data)
            public = output/'binaries'/(name+'.exe');public.write_bytes(data)
            binaries[name] = dict(path=str(path),bytes=len(data),sha256=sha(path),
                                  publicPath=public.relative_to(output).as_posix(),**pe)
        receipt['provider'] = dict(root=str(provider),binaryDirectory=str(provider/lock['binaryRelativePath']),
                                  inventoryPath='provider-files.json',inventorySha256=sha(output/'provider-files.json'),
                                  binaries=binaries)
        server = Server(work, provider, lock, commands, receipt, api)
        with server_scope(server):
            write_json(output/'fixture.json', receipt)
            yield dict(dsn=server.dsn, env={'PGPASSFILE':str(server.passfile),'PGCONNECT_TIMEOUT':'10'},
                       identity=dict(target=target,safeDSN=server.dsn,archiveSha256=archive_identity(lock)),output=str(output))
        completed = True
    except BaseException as error:
        failure = error
        receipt['failure'] = dict(type=type(error).__name__,message=commands.sanitize(str(error).encode()).decode())
        raise
    finally:
        log = work/'server.log'
        if log.is_file():
            public = output/'server.log';public.write_bytes(commands.sanitize(log.read_bytes()))
            receipt['serverLogSha256'] = sha(public)
        try:
            unchanged = all(sha(ROOT/name)==digest for name,digest in source_hashes.items())
            provider_unchanged = provider is not None and {p.relative_to(provider).as_posix() for p in provider.rglob('*') if p.is_file()}==set(lock['providerFiles']) and all(
                regular(provider/name,provider).stat().st_size==item['bytes'] and sha(provider/name)==item['sha256']
                for name,item in lock['providerFiles'].items())
        except (OSError,ValueError):
            unchanged,provider_unchanged=False,False
        receipt.update(result='passed' if completed and unchanged and provider_unchanged else 'failed',sourceUnchanged=unchanged,
                       providerUnchanged=provider_unchanged,
                       privateDataRetained=True,completedAt=datetime.datetime.now(datetime.timezone.utc).isoformat())
        write_json(output/'fixture.json',receipt)
        if not unchanged and failure is None:
            raise RuntimeError('Provider source changed during the owned lifecycle')
        if completed and not provider_unchanged and failure is None:
            raise RuntimeError('Provider files changed during the owned lifecycle')
