"""Concrete pinned-cohort semantic gate runner. A successful byte audit is an input only."""
from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import xml.etree.ElementTree as ET

import cohort_policy_v1 as policy
COHORT = policy.initialize_for_process()
import c23_gate_support_v1 as support

HEAD, TREE, BASE = support.HEAD, support.TREE, support.BASE


def source_bytes(path, maximum=2 * support.core.MiB):
    with support.core.open_readonly(path) as source:
        before = support.core.full11(os.fstat(source.fileno()))
        support.require(before['st_size'] <= maximum, 'Current source exceeds declared read bound')
        raw = source.read(maximum + 1)
        support.require(len(raw) == before['st_size'] and support.core.full11(os.fstat(source.fileno())) == before and
                        support.core.full11(os.lstat(path)) == before, 'Current source full11 changed')
    return raw


def git(root, *arguments):
    result = subprocess.run(['git', '-C', str(root), *arguments], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    support.require(result.returncode == 0 and len(result.stdout) <= 2 * support.core.MiB,
                    'Readonly Git source identity command failed/exceeded bound')
    return result.stdout


def source_files(root):
    support.require(git(root, 'rev-parse', 'HEAD', 'HEAD^{tree}').decode().splitlines() == [HEAD, TREE],
                    'Actual current worktree HEAD/tree differs')
    entries = git(root, 'ls-tree', '-r', '-z', HEAD).split(b'\0')
    inventory, blobs = [], {}
    for entry in entries:
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        mode, kind, object_sha = metadata.decode().split()
        name = name.decode('utf-8')
        support.core.safe_name(name)
        support.require(kind == 'blob' and mode in ('100644', '100755'), 'Current source is a link/nonregular Git object')
        path = root / name
        current = os.lstat(path)
        support.require(stat.S_ISREG(current.st_mode) and bool(current.st_mode & 0o111) == (mode == '100755'),
                        'Current tracked source mode differs')
        raw = source_bytes(path, maximum=8 * support.core.MiB)
        sha1 = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        support.require(sha1 == object_sha, 'Current tracked source Git blob bytes differ: ' + name)
        inventory.append({'path': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                          'gitBlob': object_sha, 'gitMode': mode})
        blobs[name] = object_sha
    support.require(len(inventory) == COHORT['source']['trackedFileCount'], 'Current committed source inventory differs from static intent')
    return inventory, blobs


def engine_fingerprint(root, binary_version, inventory):
    directories = ['gdal4k/native/', 'gdal4k-engine/ports/', 'gdal4k-engine/engine/',
                   'gdal4k-engine/tools/', 'gdal4k-engine/web/']
    selected = []
    for prefix in directories:
        selected.extend(sorted((item for item in inventory if item['path'].startswith(prefix) and
                                '__pycache__' not in Path(item['path']).parts), key=lambda item: item['path']))
    digest = hashlib.sha256(binary_version.encode())
    for item in selected:
        raw = source_bytes(root / item['path'], maximum=8 * support.core.MiB)
        digest.update(item['path'].encode())
        digest.update(raw.replace(b'\r\n', b'\n'))
    return digest.hexdigest(), selected


def current_wire_cases(raw, expected, class_name):
    support.require(b'<!DOCTYPE' not in raw.upper() and b'<!ENTITY' not in raw.upper(),
                    'Current wire XML DTD/entities are outside contract')
    root = ET.fromstring(raw)
    cases = list(root.iter('testcase'))
    support.require(root.tag == 'testsuite' and all(root.get(name, '').isdigit() for name in
                    ('tests', 'failures', 'errors', 'skipped')), 'Current wire JUnit suite counters differ')
    support.require(int(root.get('tests')) == len(expected) and
                    all(int(root.get(name)) == 0 for name in ('failures', 'errors', 'skipped')) and
                    len(cases) == len(expected), 'Current source wire tests failed/skipped/missing')
    names = []
    for case in cases:
        support.require(case.get('classname') == 'dev.gdal4k.' + class_name and
                        not any(case.find(name) is not None for name in ('failure', 'error', 'skipped')),
                        'Current source wire case class/result differs')
        names.append(case.get('name', '').split('[', 1)[0])
    support.require(len(names) == len(set(names)) and set(names) == set(expected),
                    'Actual current source wire method inventory differs')
    return names


def _owned_generator_methods(raw):
    tree = ast.parse(raw)
    owned, special = set(), set()
    def value(node, env):
        if isinstance(node, ast.Name) and node.id in env: return env[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = value(node.left, env), value(node.right, env)
            if type(left) is str and type(right) is str: return left + right
            raise ValueError('Non-string generator key concatenation')
        if isinstance(node, (ast.Constant, ast.Tuple, ast.List, ast.Set)):
            if isinstance(node, (ast.Tuple, ast.List, ast.Set)): return tuple(value(n, env) for n in node.elts)
            return node.value
        raise ValueError('Nonliteral generator registration')
    def walk(nodes, env):
        for node in nodes:
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                try: items = value(node.iter, env)
                except ValueError: continue
                if isinstance(items, (tuple, list)):
                    for item in items: walk(node.body, {**env, node.target.id: item})
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call): continue
            call = node.value
            if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id == 'PARENT_OWNED' and call.func.attr == 'add':
                try: pair = value(call.args[0], env)
                except (ValueError, IndexError): continue
                if isinstance(pair, tuple) and len(pair) == 2 and pair[0] == 'MDArray': owned.add(pair[1])
            if isinstance(call.func, ast.Name) and call.func.id == 'special':
                try: pair = tuple(value(n, env) for n in call.args[:2])
                except ValueError: continue
                if len(pair) == 2 and pair[0] == 'MDArray': special.add(pair[1])
    walk(tree.body, {})
    return owned, special


def typed_generator_contracts(raw, methods):
    """Bind full wire signatures, output ownership and native acceptance separately."""
    policy.validate_api_methods(methods)
    owned, special = _owned_generator_methods(raw)
    facts = []
    ownership = {'MDArray': 'PARENT_OWNED', 'List<MDArray>': 'PARENT_OWNED_BATCH',
                 'MDStatistics?': 'DETACHED_VALUE_SNAPSHOT', 'MDStatistics': 'DETACHED_VALUE_SNAPSHOT',
                 'ULong': 'DETACHED_UNSIGNED_SCALAR', 'List<ULong>': 'DETACHED_UNSIGNED_VECTOR',
                 'Boolean': 'DETACHED_MUTATION_ACCEPTANCE'}
    full_only = {'setSpatialRef_SpatialReference', 'setScale_Double_Int', 'setOffset_Double_Int'}
    for method in methods:
        name, result, wire = method['name'], method['returnType'], method['wireKey']
        exact = wire in special
        native_key = wire if exact else name
        support.require(result in ownership and native_key in special,
                        'Current full-signature API has no generated special operation')
        support.require(wire not in full_only or exact,
                        'New setter must bind an exact full-signature special, not a legacy fallback')
        actual_owned = wire in owned or (native_key == name and name in owned)
        support.require(actual_owned is (result in ('MDArray', 'List<MDArray>')),
                        'Current generated full-signature output ownership differs')
        facts.append({'logicalId': method['logicalId'], 'name': name,
                      'parameterTypes': method['parameterTypes'], 'returnType': result,
                      'wireKey': wire, 'nativeSpecialKey': native_key,
                      'contractRole': method['contractRole'], 'suspend': True,
                      'ownership': ownership[result], 'generatorSpecialOperation': True,
                      'exactFullSignatureSpecial': exact})
    return facts


PLANNING_SOURCE_CONTRACTS = {
    'gdal4k-engine/tools/generate-runtime.py': '62c748728a9a23b13128f33a5df09d9a0b968882882849d24bfa30dc185207fb',
    'gdal4k/native/runtime-md.hpp': '4a6644c30158dff2a6913e4093af7701db0fbbe013eea22794c98465eb93b9ef',
    'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/RuntimeEngine.kt': '4877b9646da5c1b6607b635d7d15545135632aa58339578890dd9a20a71339a1',
    'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/MDCodec.kt': 'b5030ea71c80b7826d778abfa6f6b44d6fabc3cb771fe00b170471ae3b92ce81',
    'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/BundledRuntime.generated.kt': 'd78c6d4fc7ea8391389d0fe820d22ca7d976413e08f5e3decc49f60f0a045359',
}


def detached_planning_source_contract(inventory, source, methods, wire_methods, wire_executions):
    """Join the reviewed detached protocol implementation to current source/wire evidence.

    Exact reviewed source hashes bind the empty-ledger fence, canonical unsigned
    decoders and cleanup policy. This does not implement another Kotlin parser or
    infer native execution from source. Current wire executions remain explicit.
    """
    by_path = {row['path']: row for row in inventory}
    support.require(len(by_path) == len(inventory) and type(source) is dict and
                    set(source) == set(PLANNING_SOURCE_CONTRACTS),
                    'Current detached-planning source inventory differs')
    references = []
    for name, expected_sha in PLANNING_SOURCE_CONTRACTS.items():
        row, raw = by_path.get(name), source[name]
        support.require(type(row) is dict and type(raw) is bytes and
                        type(row.get('bytes')) is int and row['bytes'] == len(raw) and
                        row.get('sha256') == expected_sha == hashlib.sha256(raw).hexdigest(),
                        'Current detached-planning implementation requires a new reviewed source binding: ' + name)
        references.append({key: row[key] for key in ('path', 'bytes', 'sha256')})
    expected_methods = {'getTotalElementsCount': 'ULong', 'getProcessingChunkSize': 'List<ULong>'}
    current = {row['name']: row['returnType'] for row in methods if row['name'] in expected_methods}
    support.require(current == expected_methods, 'Current unsigned planning method kinds differ')
    support.require(type(wire_methods) is list and len(wire_methods) == 7 and
                    all(type(name) is str and re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name) for name in wire_methods) and
                    len(set(wire_methods)) == 7,
                    'Current detached-planning wire method source inventory differs')
    wire = [row for row in wire_executions if row.get('className') == 'MDArrayPlanningWireTest']
    support.require(len(wire) == 3 and {row.get('family') for row in wire} ==
                    {'evidence-linuxX64', 'evidence-macosArm64', 'evidence-mingwX64'} and
                    all(type(row.get('methods')) is list and len(row['methods']) == 7 and
                        all(type(name) is str for name in row['methods']) and
                        set(row['methods']) == set(wire_methods) for row in wire),
                    'Current detached-planning source JVM wire executions are incomplete')
    return {'methods': expected_methods, 'reviewedImplementationSources': references,
            'wireExecutions': wire, 'allocationLedgerRequiredExactlyEmpty': True,
            'canonicalUnsignedDecimalStringResults': True,
            'resultValuesAreOwnedHandles': False, 'resultValuesAdvanceHandleWatermark': False,
            'resultValuesUsedAsCleanupHandleIds': False,
            'nativeRuntimeQualifiedByThisSourceBinding': False,
            'scope': 'Exact reviewed current source and actual source JVM wire evidence; native/platform execution is qualified separately.'}


SETTER_SOURCE_CONTRACTS = {'gdal4k/src/commonMain/kotlin/dev/gdal4k/runtime/MDArray.kt': '3fad83f88096c4931ac57ede985bb7cdb28e204e3dffbe5e3dd6263e0f274130', 'gdal4k-engine/tools/generate-runtime.py': '62c748728a9a23b13128f33a5df09d9a0b968882882849d24bfa30dc185207fb', 'gdal4k/native/runtime-md-metadata.hpp': '06226cdf9faaa10674752c7c2b160a0f0b38efa5974f5b20a7630033726f750b', 'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/RuntimeEngine.kt': '4877b9646da5c1b6607b635d7d15545135632aa58339578890dd9a20a71339a1', 'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/BundledRuntime.generated.kt': 'd78c6d4fc7ea8391389d0fe820d22ca7d976413e08f5e3decc49f60f0a045359', 'gdal4k/native/runtime.generated.inc': 'ce085530440f1b033806598cff959d05eb521349040d0bb974b1ed21f4428ce1', 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/MDArraySettersApiTest.kt': '8f08ac1781b18643c7ef74c508953213b0c74536ca1cfec5e2cd96a0ef04cef7', 'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/MDArraySettersWireTest.kt': '1a15343618595cdc55653235db34d47341522c47412aac04070be1691ea5f270', 'verification/consumer.py': 'ab7a56998ac32e0c0e69adde034dfb4402d402b031a5daf5f68aa29b60653836', 'verification/driver-profile/DriverProfileApiTest.kt': 'd3d1166e72f4627fc182bf660fef19f60f9d14cbecc605aeee847a0003574bc5', 'verification/mdarray-setters.md': 'dd1d3c70783b15bbcc130e904dbb82a9c7029864f5049b7cf6c578ab3b7ce532'}

def detached_setter_source_contract(inventory, source, methods, wire_methods, wire_executions):
    """Exact reviewed setter source/fixtures and actual current JVM evidence; no native inference."""
    policy.validate_api_methods(methods)
    rows = {row['path']: row for row in inventory}
    support.require(len(rows) == len(inventory) and type(source) is dict and
                    set(source) == set(SETTER_SOURCE_CONTRACTS),
                    'Current setter source inventory differs')
    references = []
    for path, expected_sha in SETTER_SOURCE_CONTRACTS.items():
        record, raw = rows.get(path), source[path]
        support.require(type(record) is dict and type(raw) is bytes and
                        type(record.get('bytes')) is int and record['bytes'] == len(raw) and
                        record.get('sha256') == expected_sha == hashlib.sha256(raw).hexdigest(),
                        'Current setter source requires a new reviewed binding: ' + path)
        references.append({key: record[key] for key in ('path', 'bytes', 'sha256')})
    selected = [row for row in methods if row['returnType'] == 'Boolean']
    expected = {'MDArray.setScale(Double)', 'MDArray.setOffset(Double)',
                'MDArray.setScale(Double,Int)', 'MDArray.setOffset(Double,Int)',
                'MDArray.setSpatialRef(SpatialReference?)'}
    support.require(len(selected) == 5 and {row['logicalId'] for row in selected} == expected and
                    sum(row['contractRole'] == 'feature' for row in selected) == 3 and
                    sum(row['contractRole'] == 'legacyCompatibility' for row in selected) == 2,
                    'Current setter full-signature feature/legacy contract differs')
    support.require(type(wire_methods) is list and len(wire_methods) == 7 and
                    all(type(name) is str and re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name)
                        for name in wire_methods) and len(set(wire_methods)) == 7,
                    'Current setter wire method inventory differs')
    wire = [row for row in wire_executions if row.get('className') == 'MDArraySettersWireTest']
    support.require(len(wire) == 3 and {row.get('family') for row in wire} ==
                    {'evidence-linuxX64', 'evidence-macosArm64', 'evidence-mingwX64'} and
                    all(type(row.get('methods')) is list and len(row['methods']) == 7 and
                        set(row['methods']) == set(wire_methods) for row in wire),
                    'Current setter source JVM wire executions are incomplete')
    return {'methods': selected, 'reviewedSourceFixturesAndDocs': references,
            'wireExecutions': wire, 'newBooleanMeansNativeMutationAcceptance': True,
            'newSetterAllocatedLedgerRequiredExactlyEmpty': True,
            'outputValuesAreOwnedHandles': False, 'legacyOneArgRoutesRemainSeparate': True,
            'sourceNullabilityRequiredForClear': True,
            'driverPersistenceOrStorageConversionGuaranteed': False,
            'boundaryCancellationRollsBackMutation': False,
            'nativeRuntimeQualifiedByThisSourceBinding': False,
            'scope': 'Reviewed source plus actual source JVM wire evidence; real MEM/VRT/NC4 and other platform scopes are qualified separately.'}


def gate_source_bindings(c):
    inventory, blobs = source_files(c.source_root)
    support.require(inventory == COHORT['source']['references'], 'Complete current source bytes/Git map differs from intent')
    changed = git(c.source_root, 'diff', '--name-only', BASE, HEAD).decode().splitlines()
    support.require(sorted(changed) == sorted(item['path'] for item in COHORT['source']['changedFiles']) ==
                    sorted(item['path'] for item in c.scope['prFiles']), 'Current PR source/base diff differs')
    support.require(all(blobs[item['path']] == item['sha'] for item in c.scope['prFiles']),
                    'Current official PR file blob differs from actual worktree Git object')
    properties = dict(line.strip().split('=', 1) for line in source_bytes(c.source_root / 'gradle.properties').decode().splitlines()
                      if '=' in line and not line.lstrip().startswith('#'))
    support.require(properties['gdalVersion'] == '3.12.4', 'Current pinned GDAL version differs')
    api = source_bytes(c.source_root / 'gdal4k/src/commonMain/kotlin/dev/gdal4k/runtime/MDArray.kt').decode()
    policy.verify_api_source(COHORT['apiMethods'], api.encode('utf-8'))
    generator = source_bytes(c.source_root / 'gdal4k-engine/tools/generate-runtime.py').decode()
    generated_contracts = typed_generator_contracts(generator, COHORT['apiMethods'])
    policy.verify_current_source(COHORT)
    cases = {}
    consumer = source_bytes(c.source_root / 'verification/consumer.py').decode()
    for kind in ('common', 'wire'):
        cases[kind] = []
        for group in COHORT['contracts'][kind]:
            raw = source_bytes(c.source_root / group['path'])
            methods = re.findall(r'@Test\s+fun\s+(\w+)\s*\(', raw.decode())
            support.require(methods == group['methods'], 'Current source test inventory differs')
            if kind == 'common': support.require("'" + group['className'] + "'" in consumer, 'Consumer copy list omits a current common contract class')
            cases[kind].append({**group, 'sha256': hashlib.sha256(raw).hexdigest()})
    fingerprint, fingerprint_inputs = engine_fingerprint(c.source_root, properties['gdal4kVersion'], inventory)
    support.require(len(fingerprint_inputs) == COHORT['source']['nativeInputCount'] and
                    fingerprint == COHORT['source']['defaultEngineFingerprint'], 'Current native input/fingerprint differs')
    targets = {'engine-linuxX64': 'linux-amd64', 'engine-linuxArm64': 'linux-arm64',
               'engine-macosArm64': 'macos-arm64', 'engine-mingwX64': 'windows-amd64',
               'engine-iosArm64': 'ios-arm64', 'engine-iosSimulatorArm64': 'ios-simulator-arm64',
               'engine-web': 'web', 'engine-android-arm64-v8a': 'android-arm64-v8a'}
    manifest_records, by_family = [], {}
    for name, target in targets.items():
        support.require(name in c.families, 'Missing actual current default engine family')
        records = list(c.records(name))
        path = f'gdal4k-engine/build/engine/{target}/manifest.json'
        found = [record for record in records if record['chain'][-1] == path]
        support.require(len(found) == 1, 'Actual default engine manifest missing/duplicate')
        manifest_records.append(found[0])
        by_family[name] = records
    raw_manifests = c.read_many(manifest_records)
    engines = {}
    for record in manifest_records:
        manifest = support.family_source.parse_json(raw_manifests[c.key(record)])
        name = record['familyName']
        support.require(manifest['target'] == targets[name] and manifest['gdal'] == '3.12.4' and
                        manifest['vcpkg'] == '3cd21e48a75f67acfef289da52893ce79144aaa1' and manifest['jxl'] is True and
                        manifest['source_fingerprint'] == fingerprint, 'Actual default engine current source fingerprint/version differs')
        artifacts = manifest['artifacts']
        support.require(isinstance(artifacts, dict) and artifacts, 'Actual engine artifact manifest is empty')
        anchors = {}
        for path, sha in artifacts.items():
            support.core.safe_name(path)
            support.require(re.fullmatch('[0-9a-f]{64}', sha), 'Engine declared artifact SHA is malformed')
            found = [item for item in by_family[name] if item['chain'][-1] == 'gdal4k-engine/build/' + path]
            support.require(len(found) == 1 and found[0]['sha256'] == sha, 'Actual portable engine artifact differs from manifest')
            item = found[0]
            anchors[path] = {'bytes': item['bytes'], 'sha256': item['sha256'],
                             'payloadKind': item.get('payloadKind'), 'headerFacts': item.get('details')}
        support.require(any(path.endswith('licenses/gdal.txt') for path in artifacts), 'Default engine omits actual GDAL license')
        engines[targets[name]] = {'familyId': record['familyId'], 'familyName': name,
                                 'manifestSha256': record['sha256'], 'sourceFingerprint': fingerprint,
                                 'artifactAnchors': anchors, 'compiledSdkAbiVerified': False}
    sdk_module = importlib.import_module('c23_sdk_identity_gates_v1')
    sdk_default = sdk_module.verify_default_sdk_identities(c, inventory, targets)
    wire_records, wire_groups = [], {}
    for family in ('evidence-linuxX64', 'evidence-macosArm64', 'evidence-mingwX64'):
        records = list(c.records(family))
        for group in COHORT['contracts']['wire']:
            rows = [record for record in records if '/jvmTest/' in '/' + record['chain'][-1] and
                    record['chain'][-1].endswith('/TEST-dev.gdal4k.' + group['className'] + '.xml')]
            support.require(len(rows) == 1, 'Current source JVM wire XML missing/duplicate')
            wire_records.append(rows[0]);wire_groups[c.key(rows[0])] = group
    wire_executions = []
    # Respect the same 2/8 MiB capture limits while grouping sibling leaves.
    for start in range(0, len(wire_records), 32):
        batch = wire_records[start:start+32]
        support.require(sum(r['bytes'] for r in batch) <= COHORT['resources']['captureBatchLimitBytes'], 'Wire XML batch exceeds reviewed bound')
        wire_raw = c.read_many(batch)
        for record in batch:
            group = wire_groups[c.key(record)]
            wire_executions.append({'family': record['familyName'], 'className': group['className'],
                                    'sourceXML': {'chain': record['chain'], 'bytes': record['bytes'], 'sha256': record['sha256']},
                                    'methods': current_wire_cases(wire_raw[c.key(record)], group['methods'], group['className']),
                                    'scope': 'Actual source JVM current wire/control regression tests'})
    planning_sources = {name: source_bytes(c.source_root / name, maximum=8 * support.core.MiB)
                        for name in PLANNING_SOURCE_CONTRACTS}
    planning_groups = [group for group in COHORT['contracts']['wire'] if group['className'] == 'MDArrayPlanningWireTest']
    support.require(len(planning_groups) == 1, 'Current detached-planning wire contract is absent/ambiguous')
    planning = detached_planning_source_contract(inventory, planning_sources, COHORT['apiMethods'],
                                                planning_groups[0]['methods'], wire_executions)
    setter_sources = {name: source_bytes(c.source_root / name, maximum=8 * support.core.MiB)
                      for name in SETTER_SOURCE_CONTRACTS}
    setter_groups = [group for group in COHORT['contracts']['wire'] if group['className'] == 'MDArraySettersWireTest']
    support.require(len(setter_groups) == 1, 'Current setter wire contract is absent/ambiguous')
    setters = detached_setter_source_contract(inventory, setter_sources, COHORT['apiMethods'],
                                             setter_groups[0]['methods'], wire_executions)
    profile_module = importlib.import_module('c23_profile_source_bindings_v1')
    profile_sources = profile_module.verify_profile_source_bindings(c, inventory)
    support.require(isinstance(profile_sources, (list, dict)) and len(profile_sources) == COHORT['packages']['profileFamilies'],
                    'Current profile/cross prepared-source bindings are incomplete')
    return {'sourceFilesVerified': len(inventory), 'sourceInventory': inventory, 'changedFiles': changed,
            'head': HEAD, 'tree': TREE, 'base': BASE, 'cohortManifest': policy.current_ref(),
            'publicAPI': {'owner': 'MDArray', 'methods': COHORT['apiMethods'], 'returns': {m['logicalId']: m['returnType'] for m in COHORT['apiMethods']}, 'suspend': True, 'generatorContracts': generated_contracts},
            'featureTests': cases, 'actualSourceJvmWireExecutions': wire_executions,
            'currentProfilePreparedSourceBindings': profile_sources, 'detachedUnsignedPlanningSourceBinding': planning,
            'detachedSetterSourceBinding': setters,
            'engineSourceFingerprint': fingerprint,
            'engineFingerprintInputs': fingerprint_inputs, 'defaultEngines': engines, 'sdkDefaultInputIdentities': sdk_default,
            'sourceGitBlobAndNoTimeReadsVerified': True, 'previousCohortClassBaselineUsed': False,
            'limits': ['Current source and producer bytes only; runtime is established by later platform gates.',
                       'Native format headers do not establish compiled SDK ABI compatibility.']}


def current_sdk_identity_summary(c):
    """Carry the exact source gate's bounded SDK facts into final adoption."""
    source = c.prior['sourceBindings']
    defaults = source.get('sdkDefaultInputIdentities')
    profiles = source.get('currentProfilePreparedSourceBindings')
    support.require(type(defaults) is dict and set(defaults) == set(source['defaultEngines']) and
                    type(profiles) is list and len(profiles) == COHORT['packages']['profileFamilies'],
                    'Current SDK identity source contexts are incomplete')
    contexts, families = [], set()
    expected_roles = set(support.sdk_identity.required_sdk_roles(c))
    joined = []
    for target, binding in defaults.items():
        engine = source['defaultEngines'][target]
        support.require(type(engine) is dict and type(engine.get('familyName')) is str and
                        target in support.sdk_identity.DEFAULT_ROLES,
                        'Current SDK default target/family policy is missing')
        joined.append((target, binding, engine['familyName'], support.sdk_identity.DEFAULT_ROLES[target], False))
    for parent in profiles:
        support.require(type(parent) is dict and type(parent.get('profile')) is str and
                        type(parent.get('target')) is str and type(parent.get('crossCompilationOnlyProducer')) is bool,
                        'Current SDK profile parent identity is malformed')
        profile, target, cross = parent['profile'], parent['target'], parent['crossCompilationOnlyProducer']
        arm = profile == 'combined' and target == 'linux-arm64'
        family = ('profile-cross-' if cross else 'profile-') + profile + '-' + target
        support.require(parent.get('family') == family and (not cross or arm),
                        'Current SDK profile family differs from its producer identity')
        role = ('Cross-build combined drivers for Linux ARM64' if arm else 'Profile ' + profile + ' on ' + target)
        evidence_family = ('profile-cross-evidence-' if cross else 'profile-evidence-') + profile + '-' + target
        joined.append((target, parent.get('sdkCacheIdentitySourceBinding'), evidence_family, role, arm and not cross))
    for expected_target, binding, expected_family, expected_role, restored in joined:
        support.require(type(binding) is dict and type(expected_target) is str and
                        binding.get('target') == expected_target and type(binding.get('family')) is str and
                        binding['family'] == expected_family and binding['family'] not in families,
                        'Current SDK identity context target/family differs or repeats')
        families.add(binding['family'])
        for field in ('toolchainInputsSha256', 'sdkSourceInputsSha256'):
            support.require(type(binding.get(field)) is str and re.fullmatch('[0-9a-f]{64}', binding[field]),
                            'Current SDK identity context has no exact canonical SHA')
        support.require(all(type(binding.get(field)) is int and binding[field] > 0 for field in
                            ('sourceFiles', 'sourceBytes', 'currentProjectSourceJoins')) and
                        all(binding.get(field) is False for field in
                            ('actualVcpkgAbiOrCacheHitClaimed', 'compiledSDKABIVerified', 'cacheHitVerified', 'speedupVerified')),
                        'Current SDK identity context counts or claim limits differ')
        evidence = binding.get('actualStdoutReceipt')
        support.require(type(evidence) is dict and type(evidence.get('role')) is dict and
                        all(type(evidence['role'].get(field)) is int and evidence['role'][field] > 0
                            for field in ('id', 'runId', 'attempt')) and
                        evidence['role'].get('name') == expected_role and expected_role in expected_roles and
                        evidence['role'] == next((row for row in c.role_checks if row['name'] == evidence['role']['name']), None) and
                        binding.get('restoredFromCurrentCrossBuild') is restored and
                        binding.get('generatedDuringThisRuntimeRoleClaimed') is (not binding['restoredFromCurrentCrossBuild']),
                        'Current SDK identity stdout/current-role/restore join differs')
        support.require(type(binding.get('sidecars')) is list and len(binding['sidecars']) == 2,
                        'Current SDK identity sidecar pair is missing')
        contexts.append({'family': binding['family'], 'target': expected_target,
                         'toolchainInputsSha256': binding['toolchainInputsSha256'],
                         'sdkSourceInputsSha256': binding['sdkSourceInputsSha256'],
                         'producerRole': evidence['role']['name'], 'producerJobId': evidence['role']['id'],
                         'restoredFromCurrentCrossBuild': binding['restoredFromCurrentCrossBuild']})
    support.require({row['producerRole'] for row in contexts} == expected_roles,
                    'Current SDK identity producer role coverage is incomplete')
    return {'defaultContexts': len(defaults), 'profileAndCrossContexts': len(profiles),
            'distinctCurrentProducerRoles': len(expected_roles), 'contexts': contexts,
            'actualCanonicalJsonAndStdoutIdentitiesJoined': True,
            'compiledSDKABIVerified': False, 'cacheHitVerified': False, 'speedupVerified': False}


def gate_aggregate_current(c):
    required = support.GATE_ORDER[:7]
    support.require(all(name in c.prior and isinstance(c.prior[name], dict) for name in required),
                    'Aggregate lacks seven concrete actual predecessor verdicts')
    checks = sum(len(v) for v in COHORT['ci']['requiredRoles'].values())
    support.require(len(c.family_results) == len(COHORT['familyNames']) and len(c.inputs.official) > 0 and len(c.role_checks) == checks,
                    'Aggregate current evidence counts differ')
    maven = c.prior['maven'];packages = COHORT['packages']
    support.require(maven.get('completeCoordinatesObserved') == packages['defaultCoordinates'] and
                    maven.get('profilePackageFamiliesObserved') == packages['profileFamilies'] and
                    maven.get('explicitJvmClassifiersObserved') == packages['jvmClassifiers'] and
                    maven.get('previousCohortClassBaselineUsed') is False, 'Aggregate current Maven facts differ')
    alias = c.prior['mavenAliasIndependent'];maps = alias.get('baselineClassMaps', {})
    support.require(set(maps) == {'gdal4k', 'gdal4k-runtime'} and all(type(v.get('classCount')) is int and v['classCount'] > 0 for v in maps.values()) and
                    len(alias.get('independentlyDerivedComparisons', [])) == packages['profileFamilies'] + 1 and
                    len(alias.get('currentPublishedSuspendMethodSignatures', [])) == 2 * len(COHORT['apiMethods']) and
                    alias.get('previousCohortClassBaselineUsed') is False, 'Current producer/published class or method facts differ')
    counts = {'original-service-zip': 0, 'audit-repack': 0}
    for family in c.families.values(): counts[family['route']] += 1
    support.require(counts == c.scope['familyRoutes'] and sum(counts.values()) == len(COHORT['familyNames']), 'Current genuine/repack route partition differs')
    sdk_summary = current_sdk_identity_summary(c)
    return {'head': HEAD, 'tree': TREE, 'base': BASE, 'cohortManifest': policy.current_ref(),
            'actualFamilyByteAudits': len(c.family_results), 'actualGenuineServiceZIPs': len(c.inputs.official),
            'genuineServiceZIPBytes': sum(x['size_in_bytes'] for x in c.inputs.official.values()),
            'requiredCurrentSuccessChecks': checks, 'familyRoutes': counts,
            'currentPublishedClassCounts': {name: value['classCount'] for name, value in maps.items()},
            'actualPriorGateVerdicts': {name: c.prior_verdict_refs[name] for name in required},
             'currentSdkInputIdentities': sdk_summary,
            'runtimeScopes': c.prior['platformScopes'], 'previousCohortVerdictsReused': False,
            'externalMavenCentralPublicationVerified': False}


def gate_final_acceptance(c):
    required = support.GATE_ORDER[:8]
    support.require(all(name in c.prior and isinstance(c.prior[name], dict) for name in required),
                    'Final acceptance lacks eight concrete actual predecessor verdicts')
    aggregate = c.prior['aggregateCurrent'];checks = sum(len(v) for v in COHORT['ci']['requiredRoles'].values())
    support.require(aggregate['actualFamilyByteAudits'] == len(COHORT['familyNames']) and
                    aggregate['actualGenuineServiceZIPs'] == len(c.inputs.official) and aggregate['requiredCurrentSuccessChecks'] == checks and
                    policy.require_intent_reference(aggregate.get('cohortManifest')) and
                    (aggregate['head'], aggregate['tree'], aggregate['base']) == (HEAD, TREE, BASE),
                    'Final aggregate current source/count joins differ')
    c._verify_scope()
    sdk_summary = current_sdk_identity_summary(c)
    support.require(aggregate.get('currentSdkInputIdentities') == sdk_summary,
                    'Final SDK identity projection differs from current source evidence')
    return {'scopedAcceptance': COHORT['feature'] + ' current source, packages and actual documented platform runtime scopes',
            'head': HEAD, 'tree': TREE, 'base': BASE, 'cohortManifest': policy.current_ref(),
            'testCheckout': support.TEST_CHECKOUT, 'checkoutParents': [BASE, HEAD], 'checkoutTree': TREE,
            'actualPredecessorVerdicts': {name: c.prior_verdict_refs[name] for name in required},
            'currentScope': c.plan['scope'], 'platformScopes': c.prior['platformScopes'],
            'actualFamilyByteAudits': len(c.family_results), 'actualGenuineServiceZIPs': len(c.inputs.official),
            'requiredCurrentSuccessChecks': checks, 'compiledSdkAbiVerified': False,
             'currentSdkInputIdentities': sdk_summary,
            'externalMavenCentralPublicationVerified': False, 'overallGdal3124FeatureGoalComplete': False,
            'limits': ['Root adoption/merge still requires unexpired current head/main/reviews.',
                       'iOS physical-device qualification is compile/link; Android uses an emulator bridge.',
                       'Default source JVM ARM64 is not executed; combined source ARM JVM has separate actual proof.',
                       'Repacked payload identity is separate from original service ZIP identity.',
                       'No previous cohort verdict/class baseline, SDK ABI, performance or external publication claim is inherited.']}


def dispatch(name):
    direct = {'sourceBindings': gate_source_bindings, 'aggregateCurrent': gate_aggregate_current,
              'finalAcceptance': gate_final_acceptance}
    if name in direct:
        return direct[name]
    if name in ('maven', 'mavenAliasIndependent'):
        module = importlib.import_module('c23_maven_gates_v1')
        return module.gate_maven if name == 'maven' else module.gate_maven_alias_independent
    module = importlib.import_module('c23_platform_gates_v1')
    return {'defaultArm64': module.gate_default_arm64, 'combinedArm64': module.gate_combined_arm64,
            'windowsReadiness': module.gate_windows_readiness, 'platformScopes': module.gate_platform_scopes}[name]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    policy.add_arguments(parser)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--registration', type=Path, required=True)
    parser.add_argument('--registration-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan_ref = {'path': str(args.plan), 'bytes': os.lstat(args.plan).st_size, 'sha256': args.plan_sha256}
    plan = support.pin_json(plan_ref)
    support.require(set(plan) == {'schemaVersion', 'kind', *support.REGISTRATION_FIELDS} and
                    type(plan['schemaVersion']) is int and plan['schemaVersion'] == 1 and plan['kind'] == 'GDAL-cohort-semantic-gate-plan' and
                    plan['gate'] in support.GATE_ORDER, 'Unexpected semantic gate plan')
    gate = plan['gate']
    registration_ref = {'path': str(args.registration), 'bytes': os.lstat(args.registration).st_size,
                        'sha256': args.registration_sha256}
    registration = support.pin_json(registration_ref)
    support.verify_input_registration(registration, plan_ref, plan)
    policy.require_plan_intent(plan)
    approval = support.pin_json(plan['sourceApproval'])
    support.require(policy.require_intent_reference(approval.get('cohortManifest')), 'Semantic source approval has different static intent')
    support.require(approval['kind'] == 'GDAL-cohort-semantic-source-approval' and
                    approval['approvedForActualSemanticGates'] is True, 'Root semantic source approval missing')
    for reference in approval['sources']:
        support.pin_bytes(reference)
    required_sources = ['c23_semantic_gates_v1.py', 'c23_gate_support_v1.py', 'c23_maven_gates_v1.py',
                        'c23_profile_source_bindings_v1.py', 'c23_platform_gates_v1.py', 'c23_family_source_v1.py',
                        'cohort_policy_v1.py', 'recovery_archive_core_v3.py', 'strict_zip_stream_v1.py',
                        'h17_archive_transport.py', 'h17_package_audit_parts.py', 'c23_serial_family_driver_v1.py', 'c23_scope_qualifier_v1.py',
                        'c23_remote_scope_collector_v1.js', 'c23_sdk_identity_gates_v1.py']
    paths = {str(Path(__file__).with_name(name)) for name in required_sources}
    support.require(paths <= {reference['path'] for reference in approval['sources']}, 'Approval omits actual semantic executable closure')
    os.mkdir(args.output, 0o700)
    def sealed_json(name, value):
        raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        support.require(len(raw) <= 2 * support.core.MiB, 'Gate output exceeds declared metadata record bound')
        path = args.output / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, 'wb') as destination:
            destination.write(raw)
            destination.flush()
            os.fsync(destination.fileno())
        return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    invocation = {'schemaVersion': 1, 'kind': 'GDAL-cohort-semantic-gate-invocation',
                  'gate': gate, 'startedAtUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'head': HEAD, 'tree': TREE, 'base': BASE, 'cohortManifest': policy.current_ref(), 'plan': plan_ref,
                  'registration': registration_ref,
                  'sources': approval['sources'], 'pid': os.getpid(), 'uid': os.getuid()}
    invocation_ref = sealed_json('invocation.json', invocation)
    failure = None
    try:
        context = support.GateContext(plan)
        prior_required = {'sourceBindings': (), 'maven': ('sourceBindings',),
                          'mavenAliasIndependent': ('sourceBindings', 'maven'),
                          'defaultArm64': ('sourceBindings', 'maven'), 'combinedArm64': ('sourceBindings', 'maven'),
                          'windowsReadiness': ('sourceBindings', 'maven'),
                          'platformScopes': ('sourceBindings', 'maven', 'defaultArm64', 'combinedArm64', 'windowsReadiness'),
                          'aggregateCurrent': support.GATE_ORDER[:7], 'finalAcceptance': support.GATE_ORDER[:8]}[gate]
        support.require(all(name in context.prior for name in prior_required), 'Required actual predecessor gate missing')
        result = dispatch(gate)(context)
        support.require(isinstance(result, dict), 'Semantic gate must return concrete verified facts')
        context._verify_scope()
        support.pin_json(plan_ref)
        support.verify_input_registration(support.pin_json(registration_ref), plan_ref, plan)
        for reference in approval['sources']:
            support.pin_bytes(reference)
    except BaseException as error:
        failure = error
        result = {'errorType': type(error).__name__, 'error': str(error)}
    verdict = {'schemaVersion': 1, 'kind': 'GDAL-cohort-semantic-gate-verdict', 'gate': gate,
               'status': 'passed' if failure is None else 'failed',
               'completedAtUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'head': HEAD, 'tree': TREE, 'base': BASE, 'cohortManifest': policy.current_ref(), 'invocation': invocation_ref, 'plan': plan_ref,
               'currentScope': plan['scope'], 'sources': approval['sources'], 'result': result,
               'overallGdal3124FeatureGoalComplete': False}
    reference = sealed_json('verdict.json', verdict)
    directory = os.open(args.output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    os.fsync(directory)
    os.close(directory)
    print(json.dumps(reference))
    if failure is not None:
        raise failure


if __name__ == '__main__':
    main()
