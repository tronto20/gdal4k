"""Inspect owned Windows probe images without executing them or loading DLLs."""
import hashlib
import json
from pathlib import Path
import re
import struct

# The JNI audit's existing policy, shared unchanged with standalone probes.
SYSTEM_DLLS = frozenset({'kernel32.dll', 'user32.dll', 'advapi32.dll', 'ws2_32.dll',
    'crypt32.dll', 'bcrypt.dll', 'secur32.dll', 'ole32.dll', 'oleaut32.dll', 'uuid.dll',
    'shell32.dll', 'msvcrt.dll', 'ntdll.dll', 'shlwapi.dll', 'version.dll', 'iphlpapi.dll'})
SYSTEM_PREFIXES = ('api-ms-win-', 'ext-ms-win-')


def validate_system_dependencies(dependencies):
    if not dependencies:
        raise ValueError('Could not inspect runtime dependencies')
    unexpected = [name for name in dependencies if name.lower() not in SYSTEM_DLLS
                  and not name.lower().startswith(SYSTEM_PREFIXES)]
    if unexpected:
        raise ValueError('Unbundled dependencies: ' + str(unexpected))
    return list(dependencies)


def inspect(data):
    """Read both import tables from actual complete PE32+ AMD64 bytes."""
    if len(data) < 64 or data[:2] != b'MZ':
        raise ValueError('Missing PE DOS header')
    pe = struct.unpack_from('<I', data, 60)[0]
    if pe + 24 > len(data) or data[pe:pe+4] != b'PE\0\0':
        raise ValueError('Missing PE signature')
    machine, count, _, _, _, optional_size, _ = struct.unpack_from('<HHIIIHH', data, pe+4)
    optional = pe + 24
    if (machine != 0x8664 or not count or optional_size < 112
            or optional + optional_size + count*40 > len(data)
            or struct.unpack_from('<H', data, optional)[0] != 0x20b):
        raise ValueError('Probe must be a complete PE32+ AMD64 image')
    image_base = struct.unpack_from('<Q', data, optional+24)[0]
    directories = struct.unpack_from('<I', data, optional+108)[0]
    sections = []
    for index in range(count):
        offset = optional + optional_size + index*40
        virtual_size, rva, raw_size, raw = struct.unpack_from('<IIII', data, offset+8)
        if raw + raw_size > len(data):
            raise ValueError('Truncated PE section')
        sections.append((rva, max(virtual_size, raw_size), raw, raw_size))

    def location(rva, size=1):
        for start, span, raw, raw_size in sections:
            if start <= rva and rva+size <= start+span and rva-start+size <= raw_size:
                return raw+rva-start, raw+raw_size
        raise ValueError('PE import reference is outside actual section bytes')

    def module_name(rva):
        start, boundary = location(rva)
        end = data.find(b'\0', start, min(boundary, start+1024))
        if end < 0:
            raise ValueError('Unterminated PE import module')
        try:
            name = data[start:end].decode('ascii')
        except UnicodeDecodeError:
            raise ValueError('Non-ASCII PE import module') from None
        if not re.fullmatch(r'[A-Za-z0-9_.+-]+', name) or name in ['.', '..']:
            raise ValueError('PE import must be a module basename')
        return name.lower()

    imports, receipts = {}, {}
    for label, index, width in [('regular', 1, 20), ('delay', 13, 32)]:
        names, descriptors = set(), bytearray()
        if directories > index and 112+(index+1)*8 <= optional_size:
            rva, size = struct.unpack_from('<II', data, optional+112+index*8)
        else:
            rva, size = 0, 0
        if rva:
            if size < width:
                raise ValueError('Invalid PE import-directory length')
            terminated = False
            for cursor in range(0, size, width):
                if cursor + width > size:
                    raise ValueError('Truncated PE import-directory descriptor')
                offset, _ = location(rva+cursor, width)
                entry = data[offset:offset+width]
                descriptors.extend(entry)
                if not any(entry):
                    terminated = True
                    break
                if label == 'delay':
                    attributes, name_rva = struct.unpack_from('<II', entry)
                    if not attributes & 1:
                        name_rva -= image_base
                else:
                    name_rva = struct.unpack_from('<I', entry, 12)[0]
                names.add(module_name(name_rva))
            if not terminated:
                raise ValueError('Unterminated PE import directory')
        imports[label] = sorted(names)
        receipts[label] = dict(rva=rva, declaredBytes=size,
            descriptorBytes=len(descriptors), descriptorSha256=hashlib.sha256(descriptors).hexdigest())
    imports['all'] = sorted(set(imports['regular'] + imports['delay']))
    return dict(format='PE32+', machine='AMD64', eMachine=machine, imports=imports, directories=receipts)


def _owned(root, path):
    if '..' in root.parts or not root.is_dir():
        raise ValueError('Probe source boundary must be an existing canonical directory')
    path = Path(path).absolute()
    relative = path.relative_to(root)
    if '..' in relative.parts:
        raise ValueError('Probe evidence must stay inside the owned source')
    for part in [root, *(root.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts)+1))]:
        if (part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
                or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400)):
            raise ValueError('Probe evidence must not traverse a symlink or reparse point')
    return path


def audit(program, *, root, output):
    """Write dependency evidence before the caller may execute the probe."""
    root = Path(root).absolute()
    program, output = _owned(root, program), _owned(root, output)
    source = _owned(root, Path(__file__))
    if output.exists():
        raise ValueError('Preserve existing Windows probe dependency evidence')
    raw = program.read_bytes()
    source_raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    receipt = dict(schemaVersion=1, target='windows-amd64', result='failed',
                   executablePath=program.relative_to(root).as_posix(), executableBytes=len(raw), executableSha256=digest,
                   inspectorSourcePath=source.relative_to(root).as_posix(), inspectorSourceSha256=hashlib.sha256(source_raw).hexdigest(),
                   systemDLLs=sorted(SYSTEM_DLLS), systemPrefixes=list(SYSTEM_PREFIXES),
                   scope='Actual PE32+ AMD64 image and regular/delay imports inspected before execution; unchanged JNI system-DLL policy')
    error = None
    try:
        receipt['pe'] = inspect(raw)
        validate_system_dependencies(receipt['pe']['imports']['all'])
        if program.read_bytes() != raw or source.read_bytes() != source_raw:
            raise ValueError('Probe executable or import inspector changed during audit')
        receipt['result'] = 'passed'
    except ValueError as failure:
        error = failure
        receipt['failure'] = str(failure)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2)+'\n', encoding='utf-8', newline='\n')
    if error:
        raise error
    return receipt
