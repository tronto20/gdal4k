"""Current policy-bound profile preparation and consumer fixture source bindings.

This verifies source identity, not runtime execution. Unuploaded original helper
files are represented by their exact prepared hashes and independently retained
consumer copies; they are never described as physically received originals.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

import c23_sdk_identity_gates_v1 as sdk_identity
import cohort_policy_v1 as intent

MiB = 1048576
GENERATED_BASE = 'gdal4k-runtime/src/'
TEMPLATE_BASE = 'verification/driver-profile/'
API_PATH = GENERATED_BASE + 'commonTest/kotlin/dev/gdal4k/DriverProfileApiTest.kt'
HTTP_PATH = GENERATED_BASE + 'commonTest/kotlin/dev/gdal4k/NativeHttpProfileTest.kt'
PROBE_PATH = GENERATED_BASE + 'jvmTest/kotlin/dev/gdal4k/CombinedDriverMemoryProbe.kt'
HELPERS = {
    GENERATED_BASE + 'commonTest/kotlin/dev/gdal4k/DriverProfileRuntimeFixture.kt':
        TEMPLATE_BASE + 'DriverProfileRuntimeFixture.kt',
    GENERATED_BASE + 'jvmTest/kotlin/dev/gdal4k/DriverProfileRuntimeFixtureJvm.kt':
        TEMPLATE_BASE + 'DriverProfileRuntimeFixtureJvm.kt',
    GENERATED_BASE + 'nativeTest/kotlin/dev/gdal4k/DriverProfileRuntimeFixtureNative.kt':
        TEMPLATE_BASE + 'DriverProfileRuntimeFixtureNative.kt',
}
NATIVE_DIRS = ('gdal4k/native/', 'gdal4k-engine/ports/', 'gdal4k-engine/engine/',
               'gdal4k-engine/tools/', 'gdal4k-engine/web/')


class ProfileSourceError(ValueError):
    pass


def need(value, message):
    if not value:
        raise ProfileSourceError(message)


def current_plan(c):
    """Use the immutable intent already validated by the caller's Support.

    This helper does not manufacture CI observations from static intent. Actual
    role/run/attempt identities still come from the qualified current scope.
    """
    plan = c.cohort
    need(type(plan) is dict and all(type(plan.get(key)) is dict for key in ('scope', 'source', 'contracts')),
         'Missing validated current cohort intent')
    for key in ('head', 'tree', 'base', 'testCheckout'):
        need(type(plan['scope'].get(key)) is str and plan['scope'][key] == c.scope.get(key),
             'Profile intent/current qualified scope differs: ' + key)
    source = plan['source']
    need(type(source.get('root')) is str and Path(source['root']) == c.source_root,
         'Profile source root differs from current intent')
    for key in ('trackedFileCount', 'nativeInputCount', 'portVersion'):
        need(type(source.get(key)) is int and source[key] > 0, 'Invalid current source policy: ' + key)
    need(type(source.get('defaultEngineFingerprint')) is str and
         re.fullmatch('[0-9a-f]{64}', source['defaultEngineFingerprint']),
         'Invalid current default engine fingerprint policy')
    contracts = plan['contracts']
    need(all(type(contracts.get(key)) is list and contracts[key] for key in ('common', 'wire')) and
         type(contracts.get('nc4')) is dict and
         set(contracts['nc4']) == {'templatePath', 'entrypoint', 'assertionFunctions'},
         'Current source contract inventory differs')
    nc4_policy(plan)
    return plan


def current_profile_role(c):
    """Select the actual ARM role from a qualified scope, without historical IDs."""
    plan = current_plan(c)
    run_id = plan['scope'].get('profileRun')
    need(type(run_id) is int and run_id > 0, 'Invalid current profile run intent')
    runs = [row for row in c.scope.get('runs', []) if type(row) is dict and row.get('id') == run_id]
    need(len(runs) == 1 and type(runs[0].get('id')) is int and
         type(runs[0].get('attempt')) is int and runs[0]['attempt'] > 0 and
         runs[0].get('status') == 'completed' and runs[0].get('conclusion') == 'success',
         'Current qualified profile run/attempt differs')
    roles = [row for row in c.scope.get('requiredRoleChecks', [])
             if type(row) is dict and row.get('name') == 'Actual combined JVM and Native on Linux ARM64']
    need(len(roles) == 1 and type(roles[0].get('runId')) is int and roles[0]['runId'] == run_id and
         type(roles[0].get('id')) is int and roles[0]['id'] > 0 and
         roles[0].get('head') == c.scope['head'] and
         type(roles[0].get('attempt')) is int and roles[0]['attempt'] == runs[0]['attempt'] and
         roles[0].get('status') == 'completed' and roles[0].get('conclusion') == 'success',
         'Current actual combined ARM role is not the qualified current run/attempt')
    return roles[0]


def current_test_methods(raw, class_name):
    """Read explicit declarations from already hash-qualified current sources.

    This is an inventory check, not a general Kotlin parser. The exact complete
    source bytes remain authoritative, including all generated/received copies.
    """
    need(type(class_name) is str and re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', class_name),
         'Invalid current Kotlin test class name')
    value = kotlin_code(text(raw).encode())
    classes = list(re.finditer(r'(?m)^class\s+' + re.escape(class_name) + r'\b', value))
    need(len(classes) == 1, 'Missing/duplicate current Kotlin test class')
    opening = value.find('{', classes[0].end())
    need(opening >= 0, 'Current Kotlin test class body is missing')
    depth, closing = 0, None
    for index in range(opening, len(value)):
        if value[index] == '{':
            depth += 1
        elif value[index] == '}':
            depth -= 1
            if depth == 0:
                closing = index
                break
    need(closing is not None, 'Current Kotlin test class body is unclosed')
    body = value[opening + 1:closing]
    declarations = list(re.finditer(r'(?m)^\s*@Test\s+fun\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(', body))
    need(all(body[:row.start()].count('{') == body[:row.start()].count('}')
             for row in declarations), 'Current Kotlin test is not declared directly in its named class')
    methods = [row[1] for row in declarations]
    need(methods and len(set(methods)) == len(methods), 'Missing/duplicate current Kotlin test methods')
    return methods


def kotlin_code(raw):
    """Reuse the current policy's reviewed literal/comment masking."""
    try:
        return intent._kotlin_code(raw)
    except intent.IntentError as exc:
        raise ProfileSourceError(str(exc)) from exc


def fixture_source_block(raw, name, *, private):
    """Reference one actual reviewed four-space fixture definition."""
    need(type(name) is str and re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name),
         'Invalid current fixture function name')
    value = text(raw)
    code = kotlin_code(value.encode())
    marker = '    ' + ('private ' if private else '') + 'suspend fun ' + name + '('
    matches = list(re.finditer(r'(?m)^' + re.escape(marker), code))
    need(len(matches) == 1, 'Missing/duplicate current fixture definition: ' + name)
    start = matches[0].start()
    end = code.find('\n    }\n', matches[0].end())
    need(end >= 0, 'Current reviewed fixture block layout differs: ' + name)
    return {'source': value[start:end + len('\n    }\n')].encode(),
            'line': value.count('\n', 0, start) + 1}


def nc4_policy(plan):
    """Typed current assertion inventory; order is the direct scientific call order."""
    need(type(plan) is dict and type(plan.get('contracts')) is dict,
         'Current NC4 contract container differs')
    nc4 = plan['contracts'].get('nc4')
    need(type(nc4) is dict and set(nc4) == {'templatePath', 'entrypoint', 'assertionFunctions'},
         'Current NC4 contract fields differ')
    need(type(nc4['templatePath']) is str and
         nc4['templatePath'] == TEMPLATE_BASE + 'DriverProfileApiTest.kt',
         'Current NC4 template path differs')
    names = nc4['assertionFunctions']
    need(type(names) is list and len(names) == 7 and
         all(type(name) is str and re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name) for name in names) and
         len(set(names)) == 7, 'Current NC4 requires seven unique ordered assertion functions')
    need(type(nc4['entrypoint']) is str and
         re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', nc4['entrypoint']) and
         nc4['entrypoint'] not in names, 'Current NC4 entrypoint differs')
    return nc4


def nc4_assertions(template, nc4):
    """Bind reviewed private definitions to exact direct calls, without executing Kotlin."""
    methods = current_test_methods(template, 'DriverProfileApiTest')
    need(nc4['entrypoint'] in methods, 'Current NC4 entrypoint is not an actual declared test')
    scientific = fixture_source_block(template, 'scientific', private=False)
    scientific_code = kotlin_code(scientific['source'])
    assertions, positions = [], []
    for name in nc4['assertionFunctions']:
        definition = fixture_source_block(template, name, private=True)
        call = '        ' + name + '(r, name)'
        matches = list(re.finditer(r'(?m)^' + re.escape(call) + r'$', scientific_code))
        need(len(matches) == 1,
             'Current scientific fixture must directly call each NC4 assertion exactly once: ' + name)
        position = matches[0].start()
        prefix = scientific_code[:position]
        need(prefix.count('{') - prefix.count('}') == 1,
             'Current NC4 call is not directly in the scientific function body: ' + name)
        positions.append(position)
        assertions.append({'name': name, 'definitionSha256': sha(definition['source']),
                           'definitionLine': definition['line'],
                           'directCallLine': scientific['line'] + scientific_code[:position].count('\n')})
    need(positions == sorted(positions), 'Current NC4 assertion call order differs from immutable intent')
    expected_suffix = ''.join('        ' + name + '(r, name)\n'
                              for name in nc4['assertionFunctions']) + '    }\n'
    need(scientific_code[positions[0]:] == expected_suffix,
         'Current NC4 assertions are not the reviewed straight-line scientific call suffix')
    prefix = scientific_code[:positions[0]]
    need(prefix.rstrip().endswith(('{', '}', ')')) and
         prefix.count('(') == prefix.count(')') and prefix.count('[') == prefix.count(']'),
         'Current NC4 call segment continues an incomplete preceding expression')
    jumps = re.finditer(r'\b(?:if|else|when|for|while|do|try|catch|finally|return|throw|break|continue)\b', prefix)
    need(all(prefix[:row.start()].count('{') - prefix[:row.start()].count('}') != 1
             for row in jumps), 'Current scientific call segment has preceding top-level control or jump statements')
    return assertions


def verify_source_contracts(source, plan):
    """Check static current contracts; all execution verdicts remain separate."""
    facts, seen = [], set()
    for group in ('common', 'wire'):
        for contract in plan['contracts'][group]:
            need(type(contract) is dict and set(contract) == {'className', 'path', 'methods'} and
                 type(contract['path']) is str and contract['path'] in source and
                 type(contract['methods']) is list and contract['methods'] and
                 all(type(method) is str for method in contract['methods']) and
                 len(set(contract['methods'])) == len(contract['methods']), 'Malformed current test contract')
            need(contract['path'] not in seen, 'Duplicate current test contract source')
            seen.add(contract['path'])
            actual = current_test_methods(source[contract['path']], contract['className'])
            need(set(actual) == set(contract['methods']), 'Current source test methods differ from immutable intent')
            facts.append({'group': group, **contract, 'sha256': sha(source[contract['path']]),
                          'methodCount': len(actual), 'runtimeExecutionAccepted': False})
    consumer = ast.parse(text(source['verification/consumer.py']))
    copy_lists = [ast.literal_eval(node.iter) for node in ast.walk(consumer)
                  if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and
                  node.target.id == 'test_name' and isinstance(node.iter, (ast.List, ast.Tuple))]
    need(len(copy_lists) == 1 and all(type(name) is str for name in copy_lists[0]) and
         len(set(copy_lists[0])) == len(copy_lists[0]), 'Current consumer common test copy list differs')
    need(all(row['className'] in copy_lists[0] for row in plan['contracts']['common']),
         'Current consumer does not copy all required common API test classes')
    nc4 = nc4_policy(plan)
    need(nc4['templatePath'] in source, 'Current NC4 template source is missing')
    template = source[nc4['templatePath']]
    assertions = nc4_assertions(template, nc4)
    return {'tests': facts, 'consumerCopyClasses': copy_lists[0],
            'nc4': {**nc4, 'qualifiedTemplateSha256': sha(template),
                    'assertions': assertions, 'separateJUnitCaseClaimed': False,
                    'runtimeExecutionAccepted': False}}


def verify_nc4_rendered(source, prepared, received, plan):
    nc4 = nc4_policy(plan)
    need(nc4['templatePath'] in source and API_PATH in prepared,
         'Current NC4 template or generated API source is missing')
    template = source[nc4['templatePath']]
    source_assertions = nc4_assertions(template, nc4)
    need(received == prepared[API_PATH],
         'Received current NC4 generated source differs from exact complete rendering')
    rendered_assertions = nc4_assertions(received, nc4)
    for original, rendered in zip(source_assertions, rendered_assertions):
        need(original['name'] == rendered['name'] and
             original['definitionSha256'] == rendered['definitionSha256'],
             'Current NC4 assertion definition differs within the rendered template')
    return {**nc4, 'qualifiedTemplateSha256': sha(template),
            'assertions': [{'name': row['name'], 'qualifiedDefinitionSha256': row['definitionSha256'],
                            'qualifiedDefinitionLine': row['definitionLine'],
                            'qualifiedDirectCallLine': row['directCallLine'],
                            'renderedDefinitionLine': rendered['definitionLine'],
                            'renderedDirectCallLine': rendered['directCallLine']}
                           for row, rendered in zip(source_assertions, rendered_assertions)],
            'expectedRenderedSourceSha256': sha(prepared[API_PATH]),
            'receivedGeneratedSourceSha256': sha(received),
            'completeRenderedSourceBytesEqual': True,
            'separateJUnitCaseClaimed': False, 'runtimeExecutionAccepted': False}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def unique_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, 'Duplicate profile metadata key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ProfileSourceError('Nonfinite profile JSON')))


def text(raw):
    return raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')


def no_time_source(path, maximum=8 * MiB):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        fields = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_uid, s.st_gid,
                            s.st_size, s.st_atime_ns, s.st_mtime_ns, s.st_ctime_ns, s.st_blocks)
        need(stat.S_ISREG(before.st_mode) and 0 <= before.st_size <= maximum, 'Current source shape/size differs')
        raw = source.read(maximum + 1)
        need(len(raw) == before.st_size and fields(os.fstat(source.fileno())) == fields(before) and
             fields(os.lstat(path)) == fields(before), 'Current source changed during NoTime read')
        return raw


def _prepare_policy(source):
    module = ast.parse(text(source))
    assignments = {}
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ('PROFILES', 'TARGETS'):
                    need(target.id not in assignments, 'Duplicate preparation source policy')
                    assignments[target.id] = ast.literal_eval(node.value)
    need(set(assignments) == {'PROFILES', 'TARGETS'}, 'Current preparation policy missing')
    prepare = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == 'prepare']
    need(len(prepare) == 1, 'Current prepare function missing/duplicate')
    writes = [ast.literal_eval(node.args[0]) for node in ast.walk(prepare[0])
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'write'
              and len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)]
    ordinary = [value for value in writes if 'Isolated optional driver profile: remote publication' in value]
    windows = [value for value in writes if 'Keep Windows Git configuration' in value]
    need(len(ordinary) == len(windows) == 1, 'Current profile build footer source differs')
    return assignments['PROFILES'], assignments['TARGETS'], ordinary[0].encode(), windows[0].encode()


def validate_identity(identity, source_hashes, profile, target, specs, targets, base_version):
    need(type(identity) is dict and identity.get('profile') == profile and identity.get('target') == target,
         'Profile input scope differs')
    need(profile in specs and target in specs[profile]['targets'] and target in targets, 'Unknown current profile matrix entry')
    version = base_version[:-9] + '-' + profile + '-SNAPSHOT' if base_version.endswith('-SNAPSHOT') else base_version + '-' + profile
    need(identity.get('version') == version and identity.get('sources') == source_hashes,
         'Profile input version/current complete source identity differs')
    runtime = target == 'linux-arm64'
    caps = specs[profile].get('capabilities', [profile])
    expected = {'profile', 'target', 'version', 'sources'}
    if runtime:
        expected.add('runtimeFixtures')
        need(profile == 'combined' and identity.get('runtimeFixtures') == 'required-on-execution-host',
             'ARM cross/runtime preparation contract differs')
    if not runtime and 'http' in caps:
        expected.add('httpFixtureUrls')
        urls = identity.get('httpFixtureUrls')
        need(type(urls) is dict and set(urls) == {'http', 'https', 'untrusted', 'wrong-name'}, 'Profile HTTP source identity differs')
        ports = []
        for key, value in urls.items():
            scheme = 'http' if key == 'http' else 'https'
            match = re.fullmatch(scheme + r'://127\.0\.0\.1:([1-9][0-9]{0,4})', value) if type(value) is str else None
            need(match is not None and int(match[1]) <= 65535, 'Profile HTTP source endpoint is not an owned loopback origin')
            ports.append(int(match[1]))
        need(len(set(ports)) == 4, 'Profile HTTP source ports overlap')
    if not runtime and 'postgresql' in caps and target in ('macos-arm64', 'windows-amd64'):
        expected.add('postgresqlFixture')
        pg = identity.get('postgresqlFixture')
        need(type(pg) is dict and set(pg) == {'target', 'dsn', 'archiveSha256'} and pg['target'] == target and
             type(pg['archiveSha256']) is str and re.fullmatch(r'[0-9a-f]{64}', pg['archiveSha256']),
             'Profile PostGIS public source identity differs')
        match = re.fullmatch(r'PG:host=127\.0\.0\.1 port=([1-9][0-9]{0,4}) dbname=([a-z][a-z0-9_]{0,62}) user=\2', pg['dsn'])
        need(match is not None and int(match[1]) <= 65535, 'Profile PostGIS source endpoint is not its public owned DSN')
    need(set(identity) == expected, 'Unexpected/missing profile input fields')
    digest = sha(json.dumps(identity, sort_keys=True).encode())
    relative = ('build/driver-profiles/w/' + digest[:24] if target == 'windows-amd64' else
                f'build/driver-profiles/{profile}/{target}/{digest}')
    return version, runtime, caps, digest, relative


def render_prepared(source, identity, *, hdf5_path):
    """Reproduce only current prepare() byte transforms; never execute the builder."""
    specs, targets, footer, win_footer = _prepare_policy(source['gdal4k-engine/tools/driver-profile.py'])
    profile, target = identity['profile'], identity['target']
    properties = text(source['gradle.properties'])
    bases = [line.split('=', 1)[1] for line in properties.splitlines() if line.startswith('gdal4kVersion=')]
    need(len(bases) == 1, 'Current source has no unique publication version')
    version, runtime, caps, digest, relative = validate_identity(
        identity, {name: sha(raw) for name, raw in source.items()}, profile, target, specs, targets, bases[0])
    if runtime:
        need(hdf5_path == '', 'ARM prepared sources must not capture producer HDF5 endpoints')
    else:
        need(type(hdf5_path) is str and hdf5_path.isascii() and
             not any(value in hdf5_path for value in ('\0', '\r', '\n', '"', '\\')),
             'Unsafe rendered HDF5 source path')
        need(hdf5_path.startswith('/') or re.match(r'^[A-Za-z]:/', hdf5_path), 'HDF5 source path is not absolute')
        need('..' not in PurePosixPath(hdf5_path).parts and '.' not in PurePosixPath(hdf5_path).parts and
             hdf5_path.endswith('/' + relative + '/source/verification/build/driver-profile.h5'),
             'HDF5 path does not bind the exact identity-derived profile snapshot')
    output = dict(source)
    manifest = unique_json(source['gdal4k-engine/engine/vcpkg.json'])
    dependencies = [entry for entry in manifest['dependencies'] if type(entry) is dict and entry.get('name') == 'gdal']
    need(len(dependencies) == 1, 'Current native manifest GDAL dependency differs')
    dependencies[0]['features'] = sorted(set(dependencies[0]['features'] + specs[profile]['features']))
    manifest['dependencies'].extend(copy.deepcopy(specs[profile].get('dependencies', [])))
    output['gdal4k-engine/engine/vcpkg.json'] = (json.dumps(manifest, indent=2) + '\n').encode()
    output['gradle.properties'] = ('\n'.join('gdal4kVersion=' + version if line.startswith('gdal4kVersion=') else line
                                           for line in properties.splitlines()) + '\n').encode()
    output['build.gradle.kts'] = source['build.gradle.kts'] + footer + (win_footer if target == 'windows-amd64' else b'')
    replacements = {'@PROFILE@': profile, '@RUNTIME_FIXTURES_REQUIRED@': str(runtime).lower(),
                    '@HDF5_PATH@': hdf5_path,
                    '@POSTGRESQL_DSN@': (identity.get('postgresqlFixture', {}).get('dsn') or
                                        ('' if runtime else 'PG:host=127.0.0.1 port=5432 dbname=gdal4k user=gdal4k password=profile-fixture')),
                    '@HTTP_BASE@': '', '@HTTPS_BASE@': '', '@UNTRUSTED_BASE@': '', '@WRONG_NAME_BASE@': ''}
    if 'http' in caps and not runtime:
        for key, name in [('@HTTP_BASE@', 'http'), ('@HTTPS_BASE@', 'https'),
                          ('@UNTRUSTED_BASE@', 'untrusted'), ('@WRONG_NAME_BASE@', 'wrong-name')]:
            replacements[key] = identity['httpFixtureUrls'][name]
    def render(template):
        value = text(source[template])
        for key, replacement in replacements.items():
            value = value.replace(key, replacement)
        need(re.search(r'@[A-Z_]+@', value) is None, 'Unrendered current profile source placeholder')
        return value.encode()
    output[API_PATH] = render(TEMPLATE_BASE + 'DriverProfileApiTest.kt')
    for destination, template in HELPERS.items():
        output[destination] = render(template)
    if 'http' in caps:
        output[HTTP_PATH] = render(TEMPLATE_BASE + 'NativeHttpProfileTest.kt')
    if profile == 'combined':
        output[PROBE_PATH] = text(source[TEMPLATE_BASE + 'CombinedDriverMemoryProbe.kt']).encode()
    consumer = text(source['verification/consumer.py'])
    before = "for test_name in ['DatasetApiTest'"
    need(consumer.count(before) == 1, 'Current consumer source copy-list transform is ambiguous')
    consumer = consumer.replace(before, "for test_name in ['DriverProfileApiTest', 'DatasetApiTest'")
    if 'http' in caps:
        consumer = consumer.replace("'DriverProfileApiTest', 'DatasetApiTest'", "'DriverProfileApiTest', 'NativeHttpProfileTest', 'DatasetApiTest'")
    output['verification/consumer.py'] = consumer.encode()
    return output, {'version': version, 'runtimeFixtures': runtime, 'capabilities': caps,
                    'identityDigest': digest, 'relativeOutput': relative, 'nativeTarget': targets[target][2][0].lower() + targets[target][2][1:]}


def engine_fingerprint(prepared, version):
    digest = hashlib.sha256(version.encode())
    names = []
    for prefix in NATIVE_DIRS:
        for name in sorted(path for path in prepared if path.startswith(prefix) and '__pycache__' not in PurePosixPath(path).parts):
            digest.update(name.encode())
            digest.update(prepared[name].replace(b'\r\n', b'\n'))
            names.append(name)
    return digest.hexdigest(), names


def consumer_copy(raw):
    value = text(raw)
    need(value.count('package dev.gdal4k\n') == 1, 'Generated runtime helper package transform is ambiguous')
    return value.replace('package dev.gdal4k\n', 'package consumer\n').encode()


def _one(records, label):
    records = list(records)
    need(len(records) == 1, 'Missing/duplicate current profile ' + label)
    return records[0]


def _ref(record):
    return {'familyId': record['familyId'], 'chain': record['chain'], 'bytes': record['bytes'], 'sha256': record['sha256']}


def _parent_record(records, basename, label):
    return _one((row for row in records if row['chain'][-1].endswith('/' + basename) and
                 '/source/' not in row['chain'][-1]), label)


def _prefixes(relative):
    return {relative, relative.removeprefix('build/driver-profiles/')}


def _matches_path(record, path):
    return record['chain'][-1] == path


def verify_arm_transferred_probe(c, *, identity, prepared, prefix, report, evidence, value):
    """ARM runtime upload omits this original; require the exact transferred source.

    These are source/execution bindings, not a claim that the runtime original
    source was received or a performance/runtime feature acceptance predicate.
    """
    need(identity['profile'] == 'combined' and identity['target'] == 'linux-arm64' and
         identity.get('runtimeFixtures') == 'required-on-execution-host', 'Transferred probe fallback is ARM combined only')
    expected = prepared[PROBE_PATH]
    digest = sha(expected)
    need(value(evidence['probe']) == expected and evidence['probe']['sha256'] == digest,
         'Transferred ARM probe is not the exact current generated source')
    need(unique_json(value(evidence['inputs'])) == identity and
         unique_json(value(evidence['prepared'])) == {name: sha(raw) for name, raw in prepared.items()},
         'Transferred ARM probe source belongs to a different input/prepared identity')
    full_probe = prefix + '/source/' + PROBE_PATH
    need(evidence['probe']['chain'][-1] == full_probe, 'Transferred probe snapshot differs')
    restored = unique_json(value(evidence['restored']))
    transfer = restored.get('transfer', {})
    transfer_file = transfer.get('files', {}).get(full_probe, {})
    need(transfer.get('profile') == 'combined' and transfer.get('target') == 'linux-arm64' and
         transfer.get('output') == prefix and
         transfer.get('git') == {'commit': c.scope['testCheckout'], 'tree': c.scope['tree']} and
         transfer_file.get('sha256') == digest and transfer_file.get('bytes') == len(expected),
         'Restored ARM transfer does not bind the current probe source/snapshot')
    need(restored.get('archiveSha256') == evidence['archive']['sha256'], 'Restored ARM transfer archive identity differs')
    report_transfer = report.get('arm64RuntimeBindings', {}).get('transfer', {})
    need(report_transfer.get('restoredReceiptSha256') == evidence['restored']['sha256'] and
         report_transfer.get('archiveSha256') == restored['archiveSha256'] and
         report_transfer.get('transfer') == transfer,
         'Runtime report does not bind the actual restored transfer receipt')
    diagnostic = unique_json(value(evidence['diagnostic']))
    combined = report.get('combinedRegression', {})
    bindings = combined.get('currentBindings', {})
    expected_sources = {name: sha(raw) for name, raw in prepared.items()}
    need(combined.get('diagnosticsSha256') == evidence['diagnostic']['sha256'] and
         bindings.get('sourceSha256') == diagnostic.get('sourceSha256') == expected_sources,
         'Runtime diagnostic/current report does not bind the complete current prepared source')
    need(diagnostic.get('testedHead') == c.scope['testCheckout'] and diagnostic.get('target') == 'linux-arm64' and
         diagnostic.get('profile') == 'combined' and diagnostic.get('system') == 'Linux' and
         diagnostic.get('machine') == 'aarch64' and diagnostic.get('result') == 'passed' and
         diagnostic.get('version') == identity['version'], 'Runtime diagnostic checkout/host/version differs')
    class_prefix = 'gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k/'
    class_paths = {class_prefix + 'CombinedDriverMemoryProbe.class',
                   class_prefix + 'CombinedDriverMemoryProbe$main$1.class'}
    classes = {row['chain'][-1].removeprefix(prefix + '/source/'): row for row in evidence['classes']}
    need(set(classes) == class_paths, 'Exactly two actual retained ARM probe classes are required')
    for mapping in [bindings.get('compiledProbeSha256'), diagnostic.get('compiledProbeSha256')]:
        need(type(mapping) is dict and {path for path in mapping if 'CombinedDriverMemoryProbe' in path} == class_paths and
             all(mapping[path] == row['sha256'] == sha(value(row)) for path, row in classes.items()),
             'Actual retained ARM probe class bytes differ from compiled diagnostic/report bindings')
    classpath = unique_json(value(evidence['classpath']))
    need(set(classpath) == {'java', 'classpath'} and type(classpath['java']) is str and
         classpath['java'].startswith('/') and PurePosixPath(classpath['java']).name == 'java' and
         type(classpath['classpath']) is str, 'Actual ARM probe classpath metadata differs')
    records = diagnostic.get('records')
    need(type(records) is list and len(records) == 3 and all(type(row.get('trial')) is int for row in records) and
         {row.get('trial') for row in records} == {0, 1, 2} and
         combined.get('diagnosticRecords') == records, 'Runtime report/diagnostic three actual probe command records differ')
    settings = ['-Xms32m', '-Xmx1536m', '-XX:+UseG1GC']
    need(diagnostic.get('settings', {}).get('jvm') == settings, 'Current ARM probe JVM command settings differ')
    command_hashes = bindings.get('executionLogSha256', {})
    actual_commands = {row['chain'][-1].removeprefix(prefix + '/source/'): row for row in evidence['commands']}
    logs = {row['chain'][-1].removeprefix(prefix + '/source/'): row for row in evidence['logs']}
    need(set(command_hashes) == set(actual_commands) and len(actual_commands) == 3, 'Actual ARM probe command receipt set differs')
    for item in records:
        path = item['executionLog']
        row = actual_commands[path]
        execution = unique_json(value(row))
        need(row['sha256'] == command_hashes[path] == item['executionLogSha256'] and execution == item['execution'],
             'Actual ARM probe command receipt bytes differ from report/diagnostic')
        cwd = execution.get('cwd')
        need(type(cwd) is str and cwd.startswith('/') and cwd.endswith('/' + prefix + '/source') and
             '..' not in PurePosixPath(cwd).parts and not any(ch in cwd for ch in ('\0', '\r', '\n', '"')),
             'ARM probe command is outside the identity-derived producer snapshot')
        need(classpath['classpath'].split(':')[0] == cwd + '/' + class_prefix.removesuffix('dev/gdal4k/').rstrip('/') and
             cwd + '/gdal4k-runtime/build/classes/kotlin/jvm/main' in classpath['classpath'].split(':'),
             'ARM classpath does not resolve the actual received probe/test and current production classes')
        expected_command = ['/usr/bin/time', '-v', '-o', cwd + '/' + item['rssLog'], classpath['java'], *settings,
                            '-cp', classpath['classpath'], 'dev.gdal4k.CombinedDriverMemoryProbe',
                            cwd + '/gdal4k-engine/build/jvm-resources/gdal4k/linux-arm64/libgdal4k.so']
        need(execution.get('command') == expected_command and execution.get('status') == 'passed' and
             type(execution.get('returncode')) is int and execution['returncode'] == 0 and
             type(execution.get('pid')) is int and execution['pid'] > 0 and
             execution.get('observedProcessGroupId') == execution['pid'] and
             execution.get('requestedNewSession') is True and execution.get('environmentOverrides') == {'LC_ALL': 'C'} and
             execution.get('timeoutSeconds') == 600,
             'Actual ARM probe command/owned execution binding differs')
        for path_key, sha_key in [('log', 'logSha256'), ('rssLog', 'rssLogSha256')]:
            received = logs.get(item[path_key])
            need(received is not None and received['sha256'] == item[sha_key] == sha(value(received)),
                 'Actual ARM probe command log differs')
        markers = re.findall(r'^GDAL4K_COMBINED_DRIVER_MEMORY (.+)$', value(logs[item['log']]).decode('utf-8'), re.M)
        marker_keys = {'rounds', 'completedRounds', 'contextKey', 'elapsedNanos', 'jvmAllocatedBytes',
                       'sampledPeakHeapUsedBytes', 'javaVersion'}
        marker = unique_json(markers[0]) if len(markers) == 1 else None
        need(type(marker) is dict and set(marker) == marker_keys and marker == {key: item[key] for key in marker_keys},
             'Actual ARM probe stdout is not the reported diagnostic invocation')
    role = current_profile_role(c)
    provenance = c.families['profile-evidence-combined-linux-arm64']['provenance']
    need(provenance['run']['id'] == str(role['runId']) and provenance['run']['attempt'] == str(role['attempt']) and
         provenance['run']['job'] == 'combined-arm64-runtime' and provenance['headSha'] == c.scope['head'] and
         provenance['checkoutSha'] == c.scope['testCheckout'], 'ARM probe logs are not bound to the current actual runtime role')
    return {'runtimeOriginalProbeSourcePhysicallyReceived': False,
            'sourceBinding': 'Exact same-identity cross-transferred current source plus runtime prepared/compiled/command evidence',
            'transferredProbeSource': _ref(evidence['probe']), 'restoredTransfer': _ref(evidence['restored']),
            'runtimeDiagnostic': _ref(evidence['diagnostic']), 'actualProbeClasses': [_ref(row) for row in classes.values()],
            'actualClasspath': _ref(evidence['classpath']), 'actualCommandReceipts': [_ref(row) for row in actual_commands.values()],
            'currentRoleJobId': role['id'], 'runtimeExecutionAcceptedByThisSourceBinding': False}


def require_profile_family_count(plan, observed):
    """Bind producer-derived matrix/completion counts to typed current intent."""
    need(type(plan.get('packages')) is dict, 'Current package policy is missing')
    expected = plan['packages'].get('profileFamilies')
    need(type(expected) is int and expected > 0, 'Current profile family count is not a positive integer')
    need(type(observed) is int and observed == expected,
         'Current profile family count differs from producer-derived matrix or completion')
    return expected


def verify_profile_source_bindings(c, source_inventory):
    plan = current_plan(c)
    count = plan['source']['trackedFileCount']
    need(len(source_inventory) == count and len({row['path'] for row in source_inventory}) == count,
         'Current profile bindings require the exact current source inventory')
    source = {}
    for row in source_inventory:
        raw = no_time_source(c.source_root / row['path'])
        need(len(raw) == row['bytes'] and sha(raw) == row['sha256'], 'Profile binding source changed after inventory: ' + row['path'])
        source[row['path']] = raw
    contract_facts = verify_source_contracts(source, plan)
    port = unique_json(source['gdal4k-engine/ports/gdal/vcpkg.json'])
    need(port.get('port-version') == plan['source']['portVersion'], 'Current GDAL port version differs')
    properties = text(source['gradle.properties'])
    versions = [line.split('=', 1)[1] for line in properties.splitlines() if line.startswith('gdal4kVersion=')]
    need(len(versions) == 1, 'Current profile base publication version differs')
    default_fingerprint, native_names = engine_fingerprint(source, versions[0])
    need(len(native_names) == plan['source']['nativeInputCount'] and
         default_fingerprint == plan['source']['defaultEngineFingerprint'],
         'Current default native input count/fingerprint differs from immutable intent')
    source_hashes = {name: sha(raw) for name, raw in source.items()}
    specs, targets, _, _ = _prepare_policy(source['gdal4k-engine/tools/driver-profile.py'])
    profiles = {'profile-' + profile + '-' + target: (profile, target, False)
                for profile, specification in specs.items() for target in specification['targets']}
    profiles['profile-cross-combined-linux-arm64'] = ('combined', 'linux-arm64', True)
    profile_count = require_profile_family_count(plan, len(profiles))
    facts = []
    for family_name, (profile, target, cross) in sorted(profiles.items()):
        evidence_name = ('profile-cross-evidence-combined-linux-arm64' if cross else
                         'profile-evidence-' + profile + '-' + target)
        need(family_name in c.families and evidence_name in c.families, 'Current profile family/evidence missing')
        records = list(c.records(evidence_name))
        producer_records = list(c.records(family_name))
        inputs_record = _parent_record(records, 'inputs.json', 'inputs.json')
        prepared_record = _parent_record(records, 'prepared.json', 'prepared.json')
        prefix = inputs_record['chain'][-1].rsplit('/', 1)[0]
        need(prepared_record['chain'][-1] == prefix + '/prepared.json', 'Prepared map is outside its input identity snapshot')
        selected = [inputs_record, prepared_record]
        by_path = {}
        def select(path, label, candidates=records, required=True):
            found = [row for row in candidates if _matches_path(row, path)]
            need(len(found) <= 1 and (found or not required), 'Missing/duplicate ' + label)
            if found:
                row = found[0];selected.append(row);by_path[path] = row;return row
            return None
        report_record = None
        report = None
        if cross:
            manifest_path = prefix + '/source/gdal4k-engine/build/engine/linux-arm64/manifest.json'
            select(manifest_path, 'cross current profile engine manifest')
        else:
            report_record = _parent_record(producer_records, 'report.json', 'producer report.json')
            selected.append(report_record)
        source_records = producer_records if cross else records
        generated_paths = [API_PATH, *HELPERS, HTTP_PATH, PROBE_PATH]
        generated_records = {}
        for path in generated_paths:
            found = [row for row in source_records if any(row['chain'][-1] == p + '/source/' + path for p in _prefixes(prefix))]
            # Both prefix spellings are checked against the identity below.
            need(len(found) <= 1, 'Duplicate generated profile source')
            if found:
                selected.append(found[0]);generated_records[path] = found[0]
        arm_probe = None
        if profile == 'combined' and target == 'linux-arm64' and not cross and PROBE_PATH not in generated_records:
            cross_rows = list(c.records('profile-cross-combined-linux-arm64'))
            cross_evidence = list(c.records('profile-cross-evidence-combined-linux-arm64'))
            def add_one(rows, path, label):
                row = _one((item for item in rows if item['chain'][-1] == path), label)
                selected.append(row)
                return row
            arm_probe = {
                'probe': add_one(cross_rows, prefix + '/source/' + PROBE_PATH, 'same-identity transferred ARM probe'),
                'inputs': add_one(cross_evidence, prefix + '/inputs.json', 'cross ARM input identity'),
                'prepared': add_one(cross_evidence, prefix + '/prepared.json', 'cross ARM prepared source map'),
                'restored': add_one(records, prefix + '/source/verification/results/arm64-transfer-restored.json', 'runtime restored transfer'),
                'diagnostic': add_one(records, prefix + '/source/verification/results/combined-driver-memory-linux-arm64.json', 'runtime probe diagnostic'),
                'classpath': add_one(records, prefix + '/source/gdal4k-runtime/build/combined-driver-memory-classpath.json', 'runtime probe classpath'),
                'archive': _one((item for item in cross_rows if item['chain'][-1] == 'verification/build/combined-linux-arm64.tar.xz'), 'actual cross transfer archive'),
            }
            classes = [row for row in records if row['chain'][-1].startswith(prefix + '/source/gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k/CombinedDriverMemoryProbe') and row['chain'][-1].endswith('.class')]
            commands = [row for row in records if row['chain'][-1].startswith(prefix + '/source/verification/results/combined-driver-memory/') and row['chain'][-1].endswith('.command.json')]
            logs = [row for row in records if row['chain'][-1].startswith(prefix + '/source/verification/results/combined-driver-memory/') and row['chain'][-1].endswith(('.log', '.time.txt'))]
            need(len(classes) == 2 and len(commands) == 3 and len(logs) == 6, 'Actual ARM probe classes/command/log inventory differs')
            selected.extend([*classes, *commands, *logs])
            arm_probe.update(classes=classes, commands=commands, logs=logs)
        native = targets[target][2]
        native = native[0].lower() + native[1:]
        consumer_targets = [native] if cross else ['jvm', native]
        consumers = {}
        for consumer_target in consumer_targets:
            folder = prefix + '/source/verification/results/published-consumer-' + consumer_target
            result_record = select(folder + '/result.json', 'consumer fixture source receipt')
            suffix = 'Jvm' if consumer_target == 'jvm' else 'Native'
            mappings = {
                GENERATED_BASE + 'commonTest/kotlin/dev/gdal4k/DriverProfileRuntimeFixture.kt':
                    'src/commonTest/kotlin/consumer/DriverProfileRuntimeFixture.kt',
                GENERATED_BASE + ('jvmTest' if suffix == 'Jvm' else 'nativeTest') + '/kotlin/dev/gdal4k/DriverProfileRuntimeFixture' + suffix + '.kt':
                    'src/' + consumer_target + 'Test/kotlin/consumer/DriverProfileRuntimeFixture' + suffix + '.kt',
            }
            copied = {name: select(folder + '/fixture-sources/' + name, 'retained actual consumer helper') for name in mappings.values()}
            consumers[consumer_target] = (result_record, mappings, copied)
        unique = {c.key(row): row for row in selected}
        raw = c.read_many(list(unique.values()), max_file_bytes=2 * MiB, max_total_bytes=8 * MiB)
        value = lambda row: raw[c.key(row)]
        identity, prepared_hashes = unique_json(value(inputs_record)), unique_json(value(prepared_record))
        need(identity.get('sources') == source_hashes, 'Current profile inputs do not bind all current sources')
        hdf5 = ''
        if target != 'linux-arm64':
            common_path = 'src/commonTest/kotlin/consumer/DriverProfileRuntimeFixture.kt'
            common_record = next(iter(consumers.values()))[2][common_path]
            matches = re.findall(r'DriverProfileRuntimeFixture\("([^"\r\n\x00]*)",', value(common_record).decode('utf-8'))
            need(len(matches) == 1, 'Unable to recover unique current runtime-helper HDF5 source parameter')
            hdf5 = matches[0]
        prepared, details = render_prepared(source, identity, hdf5_path=hdf5)
        relative = details['relativeOutput']
        need(prefix in _prefixes(relative), 'Actual profile evidence path does not bind source identity digest')
        expected_hashes = {name: sha(data) for name, data in prepared.items()}
        need(type(prepared_hashes) is dict and prepared_hashes == expected_hashes,
             'Prepared source map differs from exact authorized current source transforms')
        if not cross:
            need(report_record['chain'][-1] in {p + '/report.json' for p in _prefixes(relative)}, 'Producer report snapshot differs')
            report = unique_json(value(report_record))
            need(report.get('profile') == profile and report.get('target') == target and
                 report.get('version') == details['version'] and report.get('sourceInputs') == identity,
                 'Current profile producer report input identity differs')
        need(API_PATH in generated_records, 'Current generated profile fixture source was not retained')
        required = {API_PATH, *HELPERS}
        if 'http' in details['capabilities']:
            required.add(HTTP_PATH)
            need(HTTP_PATH in generated_records, 'Current generated HTTP fixture source missing')
        if profile == 'combined':
            required.add(PROBE_PATH)
            if not cross and arm_probe is None:
                need(PROBE_PATH in generated_records, 'Current generated combined probe source missing')
        directly_received = []
        for path, row in generated_records.items():
            need(path in required and row['sha256'] == expected_hashes[path] and value(row) == prepared[path],
                 'Retained generated profile source differs from exact current template transform')
            directly_received.append(path)
        nc4_binding = verify_nc4_rendered(source, prepared, value(generated_records[API_PATH]), plan)
        transferred_probe = (verify_arm_transferred_probe(c, identity=identity, prepared=prepared, prefix=prefix,
                                                         report=report, evidence=arm_probe, value=value)
                             if arm_probe is not None else None)
        if cross:
            for path, expected in expected_hashes.items():
                rows = [row for row in producer_records if row['chain'][-1] == relative + '/source/' + path]
                row = _one(rows, 'cross-transferred prepared source ' + path)
                need(row['sha256'] == expected and row['bytes'] == len(prepared[path]),
                     'Cross-transferred prepared source bytes differ')
        consumer_facts = {}
        for consumer_target, (result_record, mappings, copied_records) in consumers.items():
            result = unique_json(value(result_record))
            receipts = result.get('profile_runtime_fixture_sources')
            need(type(receipts) is list and len(receipts) == 2 and
                 {row.get('source'): row.get('consumer') for row in receipts} == mappings,
                 'Consumer runtime-helper source receipt inventory differs')
            copied_facts = []
            for receipt in receipts:
                source_name, copied_name = receipt['source'], receipt['consumer']
                expected_copy = consumer_copy(prepared[source_name])
                row = copied_records[copied_name]
                need(receipt.get('sourceSha256') == expected_hashes[source_name] and
                     receipt.get('consumerSha256') == sha(expected_copy) and
                     receipt.get('preservedSource') == 'fixture-sources/' + copied_name and
                     row['sha256'] == sha(expected_copy) and value(row) == expected_copy,
                     'Independent consumer helper copy differs from current generated source')
                copied_facts.append({'source': source_name, 'consumer': copied_name,
                                     'sourceSha256': expected_hashes[source_name], 'consumerEvidence': _ref(row)})
            consumer_facts[consumer_target] = {'result': _ref(result_record), 'sources': copied_facts}
        fingerprint, fingerprint_files = engine_fingerprint(prepared, details['version'])
        need(len(fingerprint_files) == plan['source']['nativeInputCount'],
             'Current prepared native fingerprint input count differs')
        if cross:
            manifest = unique_json(value(by_path[manifest_path]))
            need(manifest.get('target') == target and manifest.get('gdal') == '3.12.4' and
                 manifest.get('source_fingerprint') == fingerprint, 'Current cross profile engine source fingerprint differs')
            fingerprint_evidence = _ref(by_path[manifest_path])
        else:
            need(report.get('measured', {}).get('profile', {}).get('fingerprint') == fingerprint,
                 'Current profile engine fingerprint differs from exact prepared native source')
            fingerprint_evidence = _ref(report_record)
        sdk_identity_binding = sdk_identity.verify_profile_sdk_identity(
            c, family_name=family_name, evidence_name=evidence_name,
            profile=profile, target=target, prefix=prefix, prepared=prepared,
            source=source, report=report, identity=identity)
        facts.append({'family': family_name, 'profile': profile, 'target': target, 'crossCompilationOnlyProducer': cross,
                      'inputEvidence': _ref(inputs_record), 'preparedEvidence': _ref(prepared_record),
                      'currentSourceFiles': len(source), 'preparedSourceFiles': len(prepared),
                      'identityDigest': details['identityDigest'], 'version': details['version'],
                      'engineSourceFingerprint': fingerprint, 'engineFingerprintInputCount': len(fingerprint_files),
                      'engineFingerprintEvidence': fingerprint_evidence,
                      'generatedOriginalSourcesPhysicallyReceived': sorted(directly_received),
                       'currentContractSourceBindings': contract_facts,
                       'nc4GeneratedAssertionSourceBinding': nc4_binding,
                      'transferredArmProbeSourceBinding': transferred_probe,
                      'independentConsumerCopies': consumer_facts,
                      'sdkCacheIdentitySourceBinding': sdk_identity_binding,
                      'runtimeExecutionAcceptedByThisHelper': False})
    need(len(facts) == profile_count, 'Current prepared source bindings did not cover the current profile/cross inventory')
    return facts
