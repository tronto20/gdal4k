"""Bounded current SDK input-identity evidence, without ABI/cache-hit claims.

This module never imports or executes the producer.  Its two JSON leaves are
captured through the caller's already-qualified byte-audit reader.  SDK stdout
comes from Support's separately validated current-role normal job-log captures.
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

MiB = 1048576
LEAF_LIMIT = 2 * MiB
BATCH_LIMIT = 8 * MiB
OP_LIMIT = 8 * MiB - 65536
LOG_LIMIT = OP_LIMIT
LINE_LIMIT = 65536
FILE_LIMIT = MiB
SOURCE_LIMIT = 8 * MiB
TOOL_LIMIT = 128 * MiB
MAX_FILES = 10000
HELPER = 'gdal4k-engine/tools/sdk_cache_identity.py'
BUILDER = 'gdal4k-engine/tools/build-engine.py'
TOOLCHAINS = 'gdal4k-engine/tools/toolchains.py'
VERSIONS = 'build-logic/src/main/resources/dev/tronto/buildtools/versions.properties'
SUPPORTED_PRODUCERS = {
    HELPER: '74379f6edd332957a581329960ff2d33873f0e85c26fde0c90863ec38b1ffbfd',
    BUILDER: '732772d1a986bfde47a6c89b9bfa7a8aae5c580abc3b07674074712d3ff23cff',
    TOOLCHAINS: 'e58c5e2ad4440dfe87737d03703f5693c50896e6112df36a850c57c7c66c8602',
}
GENERATORS = (TOOLCHAINS, HELPER)
BOOTSTRAP = (BUILDER, 'gdal4k-engine/tools/versions.py', VERSIONS,
             'build-logic/src/main/resources/dev/tronto/buildtools/prepare_tool.py',
             'build-logic/src/main/resources/dev/tronto/buildtools/vcpkg_archive.py')
TOOLCHAIN_KEYS = {'schemaVersion', 'kind', 'target', 'baseTriplet', 'tripletTemplate',
                  'frameworkSources', 'sourceGenerators', 'toolBootstrapPins',
                  'nativeToolchain', 'nativeHeaderOverlay', 'nativeAuxiliaryTools'}
SOURCE_KEYS = {'schemaVersion', 'kind', 'target', 'vcpkgRevision', 'trackedVcpkgSourceClean',
              'toolchainInputsSha256', 'overlayPortSources', 'featureAndConfigurationSources',
              'bootstrapAndBuilderSources', 'selectivePortAbi'}
RECEIPT_KEYS = {'toolchainSha256', 'sdkSourceSha256', 'sourceFiles', 'sourceBytes',
                'actualVcpkgAbiOrCacheHitClaimed'}
SELECTIVE_ABI = ('Pinned vcpkg hashes each own port, features and dependency ABIs; '
                 'this overall inventory is evidence only.')
DEFAULT_ROLES = {
    'linux-amd64': 'verify / Native linuxX64',
    'linux-arm64': 'verify / Native linuxArm64',
    'macos-arm64': 'verify / Native macosArm64',
    'windows-amd64': 'verify / Native mingwX64',
    'ios-arm64': 'verify / Native iosArm64',
    'ios-simulator-arm64': 'verify / Native iosSimulatorArm64',
    'android-arm64-v8a': 'verify / Android arm64-v8a',
    'web': 'verify / web',
}
_F11 = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid', 'st_size',
        'st_atime_ns', 'st_mtime_ns', 'st_ctime_ns', 'st_blocks')
SDK_CAPTURE_KIND = 'GDAL-cohort-current-normal-plugin-joblog-captures'
SDK_DECODE_TRANSFORM = ('Exact UTF-8 encoding of actual returnedResult.structuredContent.content; '
                        'preserves BOM and newlines.')


class SDKIdentityError(ValueError):
    pass


def need(value, message):
    if not value:
        raise SDKIdentityError(message)


def integer(value, minimum=0, maximum=2**53 - 1):
    return type(value) is int and minimum <= value <= maximum


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                       allow_nan=False) + '\n').encode('utf-8')


def strict_json(raw, maximum=LEAF_LIMIT, require_canonical=False):
    need(type(raw) is bytes and 0 < len(raw) <= maximum, 'SDK JSON exceeds its bounded byte input')
    def pairs(rows):
        value = {}
        for key, item in rows:
            need(key not in value, 'Duplicate SDK JSON key')
            value[key] = item
        return value
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(SDKIdentityError('Nonfinite SDK JSON')))
    if require_canonical:
        need(canonical(value) == raw, 'SDK JSON differs from exact producer canonical bytes')
    return value


def safe_path(value):
    need(type(value) is str and 0 < len(value.encode('utf-8')) <= 4096 and
         '\\' not in value and ':' not in value and not any(ord(c) < 32 for c in value),
         'SDK logical path differs')
    path = PurePosixPath(value)
    need(not path.is_absolute() and path.as_posix() == value and
         all(p not in ('', '.', '..') for p in path.parts), 'SDK logical path is not canonical')
    return value


def ref(value, maximum=FILE_LIMIT):
    need(type(value) is dict and set(value) == {'path', 'bytes', 'sha256'},
         'SDK input reference fields differ')
    safe_path(value['path'])
    need(integer(value['bytes'], 0, maximum) and type(value['sha256']) is str and
         re.fullmatch('[0-9a-f]{64}', value['sha256']), 'SDK input byte identity differs')
    return value


def refs(values, prefix=None, *, ordered=True, maximum=FILE_LIMIT):
    need(type(values) is list and len(values) <= MAX_FILES, 'SDK reference list differs')
    for value in values:
        ref(value, maximum)
        if prefix is not None:
            need(value['path'].startswith(prefix), 'SDK logical reference escapes its category')
    names = [r['path'] for r in values]
    need(len(set(names)) == len(names), 'Duplicate SDK logical input')
    if ordered:
        need(names == sorted(names), 'SDK tree input order differs')
    return values


def expected_project_refs(values):
    """Accept a current source inventory, or exact rendered prepared byte map."""
    if type(values) is list:
        need(all(type(r) is dict and type(r.get('path')) is str for r in values), 'Project inventory differs')
        need(len({r['path'] for r in values}) == len(values), 'Duplicate expected project source')
        values = {r['path']: r for r in values}
    need(type(values) is dict and values, 'Current expected project sources are absent')
    result = {}
    for name, value in values.items():
        safe_path(name)
        if type(value) is bytes:
            value = {'path': name, 'bytes': len(value), 'sha256': digest(value)}
        else:
            need(type(value) is dict and value.get('path', name) == name,
                 'Expected project source path differs')
            value = {'path': name, 'bytes': value.get('bytes'), 'sha256': value.get('sha256')}
        ref(value, 8 * MiB)
        result[name] = value
    for name, sha in SUPPORTED_PRODUCERS.items():
        need(name in result and result[name]['sha256'] == sha,
             'Current SDK producer protocol source differs: ' + name)
    return result


def _project_list(actual, names, expected):
    need(all(name in expected for name in names), 'Current expected SDK project source is absent')
    wanted = [{'path': 'project/' + name, 'bytes': expected[name]['bytes'],
               'sha256': expected[name]['sha256']} for name in names]
    need(actual == wanted, 'SDK project inputs differ from current pinned/prepared source')


def tool_bootstrap_pins(raw):
    need(type(raw) is bytes and len(raw) <= FILE_LIMIT, 'Current tool pins exceed source bound')
    result = {}
    for line in raw.decode('utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        need('=' in line, 'Current tool pin property lacks equals')
        name, value = line.split('=', 1)
        need(name not in result, 'Duplicate current tool pin')
        result[name] = value
    return {name: value for name, value in sorted(result.items())
            if name not in ('vcpkg.version', 'vcpkg.revision')}


def producer_constants(raw):
    """Read literal pins only; never import the producer or run its helper."""
    need(type(raw) is bytes and len(raw) <= FILE_LIMIT and digest(raw) == SUPPORTED_PRODUCERS[BUILDER],
         'Current SDK builder protocol source differs')
    tree = ast.parse(raw.decode('utf-8'))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ('VCPKG_REV', 'TARGETS'):
                need(node.targets[0].id not in found, 'Duplicate SDK producer constant')
                found[node.targets[0].id] = ast.literal_eval(node.value)
    need(set(found) == {'VCPKG_REV', 'TARGETS'} and
         type(found['VCPKG_REV']) is str and re.fullmatch('[0-9a-f]{40}', found['VCPKG_REV']) and
         type(found['TARGETS']) is dict, 'Current SDK producer pin/target constants differ')
    return found


def _triplet_template(target, base_relative, host_system):
    text = ('include("${VCPKG_ROOT_DIR}/' + base_relative + '")\n'
            'set(VCPKG_BUILD_TYPE release)\nset(VCPKG_LIBRARY_LINKAGE static)\n'
            'list(APPEND VCPKG_CMAKE_CONFIGURE_OPTIONS -DCMAKE_POSITION_INDEPENDENT_CODE=ON)\n')
    if target == 'web':
        text += 'set(VCPKG_CXX_FLAGS "-fexceptions")\nset(VCPKG_C_FLAGS "-fexceptions")\n'
    if target.startswith('android'):
        text += ('set(VCPKG_CMAKE_SYSTEM_VERSION 28)\nset(VCPKG_CRT_LINKAGE static)\n'
                 'set(VCPKG_CMAKE_CONFIGURE_OPTIONS "${VCPKG_CMAKE_CONFIGURE_OPTIONS};-DANDROID_STL=c++_static")\n')
    if target.startswith('ios'):
        text += 'set(VCPKG_OSX_DEPLOYMENT_TARGET 15.0)\n'
    if target.startswith('macos'):
        text += 'set(VCPKG_OSX_DEPLOYMENT_TARGET 11.0)\n'
    if target.startswith('linux'):
        text += ('set(VCPKG_FIXUP_ELF_RPATH OFF)\nif(PORT STREQUAL "gdal")\n'
                 '  list(APPEND VCPKG_CMAKE_CONFIGURE_OPTIONS -DGDAL_OBJECT_LIBRARIES_POSITION_INDEPENDENT_CODE=ON)\nendif()\n')
        if host_system == 'Darwin':
            text += 'set(IS_CROSS TRUE)\n'
    if target.startswith(('linux', 'windows')):
        text += ('set(VCPKG_CHAINLOAD_TOOLCHAIN_FILE "${CMAKE_CURRENT_LIST_DIR}/../engine/' +
                 target + '/toolchain.cmake")\n')
    return text


def verify_sdk_identity_bytes(tool_raw, source_raw, receipt, *, target, expected_project,
                              vcpkg_revision, tool_pins, base_triplet, host_system):
    """Pure typed/source-joined identity proof; no filesystem/process effects."""
    need(type(target) is str and re.fullmatch('[a-z0-9][a-z0-9-]*', target) and
         host_system in ('Linux', 'Darwin', 'Windows'), 'SDK target/host context differs')
    expected = expected_project_refs(expected_project)
    tool = strict_json(tool_raw, require_canonical=True)
    source = strict_json(source_raw, require_canonical=True)
    need(type(tool) is dict and set(tool) == TOOLCHAIN_KEYS and type(source) is dict and set(source) == SOURCE_KEYS,
         'SDK identity JSON field inventory differs')
    need(type(tool['schemaVersion']) is int and tool['schemaVersion'] == 1 and
         type(source['schemaVersion']) is int and source['schemaVersion'] == 1 and
         tool['kind'] == 'gdal4k-sdk-toolchain-inputs' and source['kind'] == 'gdal4k-sdk-source-inputs' and
         tool['target'] == source['target'] == target, 'SDK identity target/kind/version differs')
    need(type(vcpkg_revision) is str and re.fullmatch('[0-9a-f]{40}', vcpkg_revision) and
         source['vcpkgRevision'] == vcpkg_revision and source['trackedVcpkgSourceClean'] is True,
         'SDK identity pinned clean vcpkg source differs')
    need(source['selectivePortAbi'] == SELECTIVE_ABI and source['toolchainInputsSha256'] == digest(tool_raw),
         'SDK source-to-toolchain identity link differs')
    framework = refs(tool['frameworkSources'])
    need(framework and all(r['path'].startswith(('vcpkg/scripts/', 'vcpkg/triplets/')) for r in framework) and
         any(r['path'].startswith('vcpkg/scripts/') for r in framework) and
         any(r['path'].startswith('vcpkg/triplets/') for r in framework), 'SDK framework category inventory differs')
    need(type(base_triplet) is str and re.fullmatch('[A-Za-z0-9_.-]+', base_triplet), 'SDK base triplet context differs')
    names = {r['path'] for r in framework}
    bases = [name for name in ('triplets/' + base_triplet + '.cmake',
                              'triplets/community/' + base_triplet + '.cmake') if 'vcpkg/' + name in names]
    need(bases and tool['baseTriplet'] == bases[0] and
         tool['tripletTemplate'] == _triplet_template(target, bases[0], host_system),
         'SDK actual target triplet/template differs from pinned builder policy')
    need(type(tool['toolBootstrapPins']) is dict and tool['toolBootstrapPins'] == tool_pins and
         all(type(k) is str and type(v) is str for k, v in tool_pins.items()), 'SDK tool bootstrap pin identity differs')
    generators = refs(tool['sourceGenerators'], 'project/', ordered=False)
    _project_list(generators, GENERATORS, expected)
    ports = refs(source['overlayPortSources'], 'project/gdal4k-engine/ports/')
    engine = refs(source['featureAndConfigurationSources'], 'project/gdal4k-engine/engine/')
    _project_list(ports, sorted(n for n in expected if n.startswith('gdal4k-engine/ports/')), expected)
    _project_list(engine, sorted(n for n in expected if n.startswith('gdal4k-engine/engine/')), expected)
    bootstrap = refs(source['bootstrapAndBuilderSources'], ordered=False)
    need(len(bootstrap) == len(BOOTSTRAP) + 2, 'SDK bootstrap inventory differs')
    _project_list(bootstrap[:-2], BOOTSTRAP, expected)
    need([r['path'] for r in bootstrap[-2:]] == ['vcpkg/bootstrap-vcpkg.sh', 'vcpkg/bootstrap-vcpkg.bat'],
         'SDK pinned vcpkg bootstrap names/order differ')
    headers = refs(tool['nativeHeaderOverlay'], 'generated/compiler-headers/')
    chain = tool['nativeToolchain']
    auxiliaries = refs(tool['nativeAuxiliaryTools'], 'native-auxiliary/', maximum=TOOL_LIMIT)
    if target.startswith(('linux', 'windows')):
        ref(chain)
        need(chain['path'] == 'generated/toolchain.cmake' and len(auxiliaries) == 1 and
             auxiliaries[0]['path'] == 'native-auxiliary/llvm-ranlib' + ('.exe' if target.startswith('windows') else '') and
             auxiliaries[0]['bytes'] > 0, 'SDK native toolchain/auxiliary context differs')
    else:
        need(chain is None and not auxiliaries, 'SDK non-native target has unexpected native toolchain inputs')
    inventory = framework + generators + headers + ports + engine + bootstrap + ([] if chain is None else [chain])
    need(len(inventory) <= MAX_FILES and len({r['path'] for r in inventory}) == len(inventory) and
         sum(r['bytes'] for r in inventory) <= SOURCE_LIMIT, 'SDK complete source inventory duplicates or exceeds bound')
    if type(receipt) is dict and set(receipt) == {'sdkCacheInputs'}:
        receipt = receipt['sdkCacheInputs']
    need(type(receipt) is dict and set(receipt) == RECEIPT_KEYS and
         receipt['actualVcpkgAbiOrCacheHitClaimed'] is False and
         integer(receipt['sourceFiles'], 1, MAX_FILES) and integer(receipt['sourceBytes'], 0, SOURCE_LIMIT) and
         receipt['toolchainSha256'] == digest(tool_raw) and receipt['sdkSourceSha256'] == digest(source_raw) and
         receipt['sourceFiles'] == len(inventory) and receipt['sourceBytes'] == sum(r['bytes'] for r in inventory),
         'Actual SDK stdout receipt SHA/count/false-claim identity differs')
    return {'target': target, 'toolchainInputsSha256': digest(tool_raw), 'sdkSourceInputsSha256': digest(source_raw),
            'sourceFiles': len(inventory), 'sourceBytes': sum(r['bytes'] for r in inventory),
            'currentProjectSourceJoins': len(generators) + len(ports) + len(engine) + len(BOOTSTRAP),
            'producerFrameworkSourceReferences': len(framework), 'producerGeneratedHeaderReferences': len(headers),
            'producerAuxiliaryToolReferences': len(auxiliaries), 'baseTriplet': bases[0],
            'actualVcpkgAbiOrCacheHitClaimed': False, 'compiledSDKABIVerified': False,
            'cacheHitVerified': False, 'speedupVerified': False,
            'scope': 'Actual canonical SDK configuration/source identity and current source joins; upstream/framework/header/auxiliary bytes are producer inventory evidence unless independently retained.'}


def _record_ref(record):
    return {'familyId': record['familyId'], 'chain': record['chain'], 'bytes': record['bytes'],
            'sha256': record['sha256']}


def _qualified_role(c, name):
    rows = [r for r in c.scope['requiredRoleChecks'] if r.get('name') == name]
    need(len(rows) == 1, 'Current SDK producer role is missing/ambiguous')
    role = rows[0]
    need(all(integer(role.get(k), 1) for k in ('id', 'runId', 'attempt')) and
         role.get('status') == 'completed' and role.get('conclusion') == 'success' and
         role.get('head') == c.scope['head'], 'SDK producer role is not current successful source/attempt')
    return role


def _clock(value):
    need(type(value) is str, 'SDK capture clock is absent')
    if value.endswith(' UTC'):
        return dt.datetime.strptime(value, '%Y-%m-%d %H:%M:%S UTC').replace(tzinfo=dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(value)
    need(parsed.tzinfo is not None, 'SDK capture clock lacks timezone')
    return parsed


def _read_pin(reference, maximum=OP_LIMIT):
    need(type(reference) is dict and set(reference) == {'path', 'bytes', 'sha256'} and
         type(reference['path']) is str and Path(reference['path']).is_absolute() and
         str(Path(reference['path'])) == reference['path'] and '..' not in Path(reference['path']).parts and
         integer(reference['bytes'], 1, maximum) and type(reference['sha256']) is str and
         re.fullmatch('[0-9a-f]{64}', reference['sha256']), 'SDK source-operation reference differs')
    path = Path(reference['path'])
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOATIME | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, 'rb') as source:
        state = os.fstat(source.fileno());before = tuple(getattr(state, k) for k in _F11)
        need(stat.S_ISREG(state.st_mode) and stat.S_IMODE(state.st_mode) == 0o400 and
             state.st_uid == os.getuid() and state.st_nlink == 1 and state.st_size == reference['bytes'],
             'SDK source-operation ownership/seal/length differs')
        raw = source.read(reference['bytes'] + 1)
        need(len(raw) == reference['bytes'] and digest(raw) == reference['sha256'] and
             before == tuple(getattr(os.fstat(source.fileno()), k) for k in _F11) ==
             tuple(getattr(os.lstat(path), k) for k in _F11), 'SDK source-operation bytes/full11 differ')
    return raw


def required_sdk_roles(c):
    """Current static workflow role names, joined later to actual job identities."""
    roles = c.cohort['ci']['requiredRoles']
    need(type(roles) is dict and set(roles) == {'primary', 'profile'} and
         all(type(v) is list and all(type(n) is str for n in v) for v in roles.values()),
         'Current SDK workflow role inventory differs')
    defaults = set(DEFAULT_ROLES.values())
    need(defaults <= set(roles['primary']), 'Current default SDK producer roles are incomplete')
    profiles = [name for name in roles['profile']
                if re.fullmatch(r'Profile [a-z0-9-]+ on [a-z0-9-]+', name)]
    expected_profile_count = c.cohort['packages']['profileFamilies'] - 2
    need(integer(expected_profile_count, 1) and len(profiles) == expected_profile_count,
         'Current SDK profile producer role matrix differs')
    cross = 'Cross-build combined drivers for Linux ARM64'
    need(roles['profile'].count(cross) == 1, 'Current ARM SDK cross-producer role is absent')
    expected = [name for name in roles['primary'] if name in defaults]
    expected += [name for name in roles['profile'] if name in profiles or name == cross]
    need(len(set(expected)) == len(expected), 'Duplicate SDK producer role')
    return expected


def _decode_sdk_capture(c, item, captured_at=None):
    need(type(item) is dict and set(item) == {'role', 'head', 'run', 'attempt', 'jobId', 'operation', 'decodedText'},
         'SDK capture row field inventory differs')
    role = _qualified_role(c, item['role'])
    need(type(item['head']) is str and item['head'] == role['head'] and
         all(integer(item[k], 1) for k in ('run', 'attempt', 'jobId')) and
         (item['run'], item['attempt'], item['jobId']) == (role['runId'], role['attempt'], role['id']),
         'SDK actual capture role/run/attempt/head differs')
    operation = strict_json(_read_pin(item['operation']), maximum=OP_LIMIT)
    need(type(operation) is dict and set(operation) == {'operation', 'arguments', 'returnedResult', 'before', 'after'} and
         operation['operation'] == 'github_fetch_workflow_job_logs' and
         type(operation['arguments']) is dict and set(operation['arguments']) == {'repo_full_name', 'job_id'} and
         type(operation['arguments']['job_id']) is int and
         operation['arguments'] == {'repo_full_name': c.cohort['scope']['repository'], 'job_id': item['jobId']},
         'SDK actual normal-plugin operation/arguments differ')
    for name in ('before', 'after'):
        need(type(operation[name]) is dict and set(operation[name]) == {'current_time'},
             'SDK actual operation clock fields differ')
    before, after = (_clock(operation[name]['current_time']) for name in ('before', 'after'))
    need(before <= after <= dt.datetime.now(dt.timezone.utc), 'SDK actual capture clocks are reversed/future')
    if captured_at is not None:
        need(after <= captured_at, 'SDK operation escapes capture manifest clock')
    returned = operation['returnedResult']
    need(type(returned) is dict and set(returned) <= {'content', 'structuredContent', 'isError', '_meta'} and
         returned.get('isError', False) is False and returned.get('_meta') in (None, {}),
         'SDK actual job-log operation failed or has an unsupported wrapper')
    need(type(returned.get('content')) is list and
         all(type(block) is dict and set(block) <= {'type', 'text', 'annotations', '_meta'} and
             block.get('type') == 'text' and type(block.get('text')) is str and
             block.get('_meta') in (None, {}) for block in returned['content']),
         'SDK actual job-log operation acknowledgement differs')
    structured = returned.get('structuredContent')
    need(type(structured) is dict and set(structured) == {'content'} and type(structured['content']) is str,
         'SDK actual decoded job-log content differs')
    raw = structured['content'].encode('utf-8')
    decoded = item['decodedText']
    need(type(decoded) is dict and set(decoded) == {'bytes', 'sha256', 'transform'} and
         integer(decoded['bytes'], 1, LOG_LIMIT) and type(decoded['sha256']) is str and
         re.fullmatch('[0-9a-f]{64}', decoded['sha256']) and decoded['transform'] == SDK_DECODE_TRANSFORM and
         decoded['bytes'] == len(raw) and decoded['sha256'] == digest(raw),
         'SDK exact UTF-8/BOM decoded byte identity differs')
    return raw, role


def verify_sdk_log_captures(c, manifest):
    """Verify one operation at a time, then retain only lazy immutable refs."""
    current_ref = manifest.get('cohortManifest') if type(manifest) is dict else None
    need(type(current_ref) is dict and set(current_ref) == {'path', 'bytes', 'sha256'} and
         type(current_ref['path']) is str and Path(current_ref['path']).is_absolute() and
         str(Path(current_ref['path'])) == current_ref['path'] and
         '..' not in Path(current_ref['path']).parts and integer(current_ref['bytes'], 1, 2 * MiB) and
         type(current_ref['sha256']) is str and re.fullmatch('[0-9a-f]{64}', current_ref['sha256']),
         'SDK capture immutable cohort reference differs')
    need(type(manifest) is dict and set(manifest) == {'schemaVersion', 'kind', 'cohortManifest', 'head',
         'capturedAtUtc', 'captures', 'sdkCaptures', 'noAcceptanceClaim'} and
         type(manifest['schemaVersion']) is int and manifest['schemaVersion'] == 1 and
         manifest['kind'] == SDK_CAPTURE_KIND and manifest['head'] == c.scope['head'] and
         manifest['cohortManifest'] == c.scope['cohortManifest'] == c.plan['cohortManifest'] and
         manifest['noAcceptanceClaim'] is True and type(manifest['captures']) is list,
         'SDK generic actual log-capture manifest differs')
    captured_at = _clock(manifest['capturedAtUtc'])
    need(captured_at <= dt.datetime.now(dt.timezone.utc), 'SDK capture manifest clock is future')
    expected = required_sdk_roles(c)
    rows = manifest['sdkCaptures']
    need(type(rows) is list and all(type(row) is dict for row in rows) and
         [row.get('role') for row in rows] == expected, 'SDK capture roles/order are incomplete or duplicated')
    lazy = {}
    jobs = set()
    for item in rows:
        raw, role = _decode_sdk_capture(c, item, captured_at)
        need(role['id'] not in jobs, 'SDK capture reuses a different role job identity')
        jobs.add(role['id'])
        # Do not keep operation JSON or any of nineteen decoded text buffers.
        lazy[item['role']] = dict(item)
        del raw
    return lazy


def _receipt_from_role(c, name, tool_sha, source_sha):
    role = _qualified_role(c, name)
    logs = getattr(c, 'sdk_job_logs', None)
    need(type(logs) is dict and name in logs, 'Validated current SDK role job-log capture is absent')
    capture = logs[name]
    raw, decoded_role = _decode_sdk_capture(c, capture)
    need(decoded_role == role, 'SDK lazy capture current-role identity differs')
    matches = []
    for number, line in enumerate(io.BytesIO(raw), 1):
        if b'{"sdkCacheInputs"' not in line:
            continue
        need(len(line) <= LINE_LIMIT, 'SDK stdout receipt line exceeds bound')
        index = line.index(b'{"sdkCacheInputs"')
        prefix = line[:index].decode('utf-8').lstrip('\ufeff')
        need(not prefix or re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s+', prefix),
             'SDK receipt line lacks normal raw/job-log stdout framing')
        value = strict_json(line[index:].strip(), maximum=LINE_LIMIT)
        if type(value) is not dict or set(value) != {'sdkCacheInputs'}:
            continue
        receipt = value['sdkCacheInputs']
        if type(receipt) is dict and receipt.get('toolchainSha256') == tool_sha and receipt.get('sdkSourceSha256') == source_sha:
            matches.append((number, value, digest(line)))
    need(1 <= len(matches) <= 32 and all(value == matches[0][1] for _, value, _ in matches),
         'Current SDK stdout receipt missing, excessive or inconsistent')
    return matches[0][1], {'role': role, 'actualOperation': capture['operation'],
                           'decodedText': capture['decodedText'], 'line': matches[0][0],
                           'lineSha256': matches[0][2], 'matchingOccurrences': len(matches)}


def _join(prefix, suffix):
    if prefix in ('', '.'):
        return safe_path(suffix)
    return safe_path(prefix.rstrip('/') + '/' + suffix)


def _member(records, name):
    found = [r for r in records if r['chain'][-1] == name and r.get('kind') in ('zip-member', 'tar-member')]
    need(len(found) == 1 and integer(found[0]['bytes'], 1, LEAF_LIMIT),
         'Current SDK sidecar absent/duplicate/oversized: ' + name)
    return found[0]


def _host(target):
    return 'Windows' if target.startswith('windows') else 'Darwin' if target.startswith(('macos', 'ios')) else 'Linux'


def verify_sdk_context(c, *, family_name, target, prefix, project, source, receipt_role, restored=False):
    """One exact context, one bounded pair; no inventory or framework echoing."""
    need(type(source) is dict and BUILDER in source and VERSIONS in source,
         'Current source bytes for SDK producer pins are absent')
    constants = producer_constants(source[BUILDER])
    need(target in constants['TARGETS'], 'SDK current producer target is unknown')
    records = list(c.records(family_name))
    base = _join(prefix, 'gdal4k-engine/build/engine/' + target)
    pair = [_member(records, base + '/' + name) for name in ('sdk-toolchain-inputs.json', 'sdk-source-inputs.json')]
    need(sum(r['bytes'] for r in pair) <= BATCH_LIMIT, 'SDK sidecar batch exceeds bound')
    values = c.read_many(pair, max_file_bytes=LEAF_LIMIT, max_total_bytes=BATCH_LIMIT)
    tool_raw, source_raw = (values[c.key(r)] for r in pair)
    receipt, evidence = _receipt_from_role(c, receipt_role, digest(tool_raw), digest(source_raw))
    result = verify_sdk_identity_bytes(tool_raw, source_raw, receipt, target=target,
        expected_project=project, vcpkg_revision=constants['VCPKG_REV'],
        tool_pins=tool_bootstrap_pins(source[VERSIONS]), base_triplet=constants['TARGETS'][target],
        host_system=_host(target))
    return {**result, 'family': family_name, 'sidecars': [_record_ref(r) for r in pair],
            'actualStdoutReceipt': evidence, 'restoredFromCurrentCrossBuild': restored,
            'generatedDuringThisRuntimeRoleClaimed': not restored}


def verify_profile_sdk_identity(c, *, family_name, evidence_name, profile, target, prefix,
                                prepared, source=None, report=None, identity=None):
    del report, identity
    need(type(profile) is str and type(target) is str and type(prepared) is dict,
         'Current SDK profile context differs')
    arm = profile == 'combined' and target == 'linux-arm64'
    role = ('Cross-build combined drivers for Linux ARM64' if arm else
            'Profile ' + profile + ' on ' + target)
    return verify_sdk_context(c, family_name=evidence_name, target=target,
        prefix=_join(prefix, 'source'), project=prepared, source=source,
        receipt_role=role, restored=arm and not family_name.startswith('profile-cross-'))


def _source_bytes(c, inventory, name):
    expected = next((r for r in inventory if r['path'] == name), None) if type(inventory) is list else inventory.get(name)
    need(type(expected) is dict and integer(expected.get('bytes'), 0, FILE_LIMIT),
         'Current SDK protocol source reference absent')
    path = Path(c.source_root) / name
    fd = os.open(path, os.O_RDONLY | os.O_NOATIME | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        state = os.fstat(f.fileno());before = tuple(getattr(state, k) for k in _F11)
        need(stat.S_ISREG(state.st_mode) and state.st_nlink == 1 and state.st_uid == os.getuid(),
             'Current SDK source file ownership differs')
        raw = f.read(FILE_LIMIT + 1)
        need(len(raw) == expected['bytes'] and digest(raw) == expected['sha256'] and
             before == tuple(getattr(os.fstat(f.fileno()), k) for k in _F11) ==
             tuple(getattr(os.lstat(path), k) for k in _F11), 'Current SDK source bytes/full11 differ')
    return raw


def verify_default_sdk_identities(c, source_inventory, targets):
    """Caller supplies its source-validated engine-family to target map."""
    need(type(targets) is dict and targets and len(set(targets.values())) == len(targets),
         'Current default SDK target matrix differs')
    source = {name: _source_bytes(c, source_inventory, name) for name in (BUILDER, VERSIONS)}
    results = {}
    for family_name, target in targets.items():
        need(target in DEFAULT_ROLES, 'Current default SDK role mapping is absent')
        results[target] = verify_sdk_context(c, family_name=family_name, target=target, prefix='',
            project=source_inventory, source=source, receipt_role=DEFAULT_ROLES[target])
    return results
