"""Bind a newly invoked current cohort family byte audit to genuine service ZIPs.

This is new recovery source, not the lost pre-outage actor. It emits archive
byte evidence, not a platform or feature acceptance decision. No network or
product execution is performed. Large reconstructed AUDIT-REPACK ZIPs remain
virtual; at most one raw part is cached on opaque, accounted scratch.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from contextlib import contextmanager
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import types
import zipfile

import recovery_archive_core_v3 as core

import cohort_policy_v1 as policy
COHORT = policy.initialize_for_process()
HEAD, TREE, BASE = (COHORT['scope'][k] for k in ('head', 'tree', 'base'))
TEST_CHECKOUT = COHORT['scope']['testCheckout']
RUNS = {COHORT['scope']['primaryRun'], COHORT['scope']['profileRun']}
COLLECTOR_SHA = 'e85e02f65e7e5d808d865d6d9888853f9de2e61faa5d7ae9f27941ac60d5d8ba'
PART_BYTES = 24 * core.MiB - 65536
MAX_JSON = 2 * core.MiB


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        core.need(key not in result, 'Duplicate JSON object key')
        result[key] = value
    return result


def parse_json(raw):
    core.need(len(raw) <= MAX_JSON, 'Recovery JSON input exceeds declared bound')
    def invalid_constant(value):
        raise core.AuditError('Nonfinite JSON constant: ' + value)
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def pin_read(reference, *, json_value=True):
    core.need(isinstance(reference, dict) and set(reference) == {'path', 'bytes', 'sha256'},
              'Invalid immutable file reference')
    core.need(type(reference['bytes']) is int and 0 <= reference['bytes'] <= MAX_JSON and
              isinstance(reference['sha256'], str) and re.fullmatch('[0-9a-f]{64}', reference['sha256']),
              'Invalid bounded file reference identity')
    with core.open_readonly(reference['path']) as source:
        before = core.full11(os.fstat(source.fileno()))
        core.need(before['st_size'] == reference['bytes'], 'Pinned file length mismatch')
        raw = source.read(reference['bytes'] + 1)
        core.need(len(raw) == reference['bytes'] and hashlib.sha256(raw).hexdigest() == reference['sha256'],
                  'Pinned file bytes/SHA256 mismatch')
        core.need(core.full11(os.fstat(source.fileno())) == before and
                  core.full11(os.lstat(reference['path'])) == before, 'Pinned file full11 changed')
    return parse_json(raw) if json_value else raw


def collector_module():
    path = Path(__file__).with_name('h17_package_audit_parts.py')
    with core.open_readonly(path) as source:
        before = core.full11(os.fstat(source.fileno()))
        raw = source.read(65536)
        core.need(not source.read(1) and core.full11(os.fstat(source.fileno())) == before,
                  'Vendored collector source changed or exceeds bound')
    core.need(hashlib.sha256(raw).hexdigest() == COLLECTOR_SHA, 'Vendored H17 collector SHA mismatch')
    module = types.ModuleType('recovery_h17_collector')
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


collector = collector_module()


class FamilyInputs:
    def __init__(self, catalogue_ref, map_ref):
        self.catalogue_ref, self.map_ref = catalogue_ref, map_ref
        self.catalogue = pin_read(catalogue_ref)
        self.materialization = pin_read(map_ref)
        c, m = self.catalogue, self.materialization
        core.need(type(c['schemaVersion']) is int and type(m['schemaVersion']) is int and c['schemaVersion'] == m['schemaVersion'] == 1 and
                  c['head'] == m['head'] == HEAD and c['repository'] == m['repository'] == COHORT['scope']['repository'] and
                  policy.require_intent_reference(c.get('cohortManifest')) and policy.require_intent_reference(m.get('cohortManifest')),
                  'Current current cohort catalogue/materialization head or repository mismatch')
        policy.validate_reference(m['sourceCatalogue'])
        core.need(c['kind'] == 'GDAL-cohort-official-artifact-catalogue' and m['kind'] == 'GDAL-cohort-materialization-map' and m['sourceCatalogue'] == catalogue_ref,
                  'Materialization map is not bound to exact official catalogue')
        selected, families = c['selectedArtifacts'], c['logicalFamilies']
        core.need(isinstance(selected, list) and selected and isinstance(families, list) and
                  len(families) == len(COHORT['familyNames']) and
                  sorted(x['original']['name'] for x in families) == COHORT['familyNames'],
                  'Current selected ZIP/logical family inventory differs')
        runs = c.get('runs')
        core.need(isinstance(runs, list) and len(runs) == len(RUNS), 'Actual completed run bindings missing')
        self.run_attempts = {}
        for run in runs:
            core.need(set(run) == {'id', 'attempt', 'status', 'conclusion', 'head'} and
                      type(run['id']) is int and run['id'] in RUNS and run['id'] not in self.run_attempts and
                      type(run['attempt']) is int and run['attempt'] > 0 and
                      run['status'] == 'completed' and run['conclusion'] == 'success' and run['head'] == HEAD,
                      'Actual catalogue run identity/attempt/status differs')
            self.run_attempts[run['id']] = run['attempt']
        self.official = {}
        for item in selected:
            artifact_id = item['id']
            core.need(type(artifact_id) is int and artifact_id > 0 and artifact_id not in self.official,
                      'Duplicate or invalid current official artifact ID')
            core.need(item['expired'] is False and type(item['workflow_run']['id']) is int and item['workflow_run']['id'] in RUNS and
                      item['workflow_run']['head_sha'] == HEAD, 'Selected artifact run/head/expiration mismatch')
            core.need(type(item['size_in_bytes']) is int and 0 < item['size_in_bytes'] <= 32 * core.MiB and
                      re.fullmatch('sha256:[0-9a-f]{64}', item['digest']), 'Official service ZIP byte identity is invalid')
            self.official[artifact_id] = item
        core.need(set(m['artifacts']) == {str(i) for i in self.official}, 'Materialization map has missing/extra service IDs')
        self.families = {}
        used = []
        for family in families:
            original = family['original']
            core.need(type(original['id']) is int and original['id'] > 0 and original['id'] not in self.families and original['workflow_run']['head_sha'] == HEAD and
                      type(original['workflow_run']['id']) is int and original['workflow_run']['id'] in RUNS and original['expired'] is False,
                      'Original logical-family identity differs')
            core.need(family['route'] in ('original-service-zip', 'audit-repack') and
                      isinstance(family['selected'], list) and all(type(i) is int and i > 0 for i in family['selected']) and len(family['selected']) == len(set(family['selected'])),
                      'Invalid family source route')
            core.need(all(i in self.official for i in family['selected']), 'Family refers to an unselected service ZIP')
            used.extend(family['selected'])
            self.families[original['id']] = family
        core.need(sorted(used) == sorted(self.official), 'Families do not cover each current selected service ZIP exactly once')

    @contextmanager
    def raw(self, artifact_id):
        official = self.official[artifact_id]
        material = self.materialization['artifacts'][str(artifact_id)]
        core.need(type(material['artifactId']) is int and material['artifactId'] == artifact_id and type(material['bytes']) is int and material['bytes'] == official['size_in_bytes'] and
                  material['sha256'] == official['digest'][7:] and
                  material['materialization'] in ('retained-byte-identical-current-official', 'new-normal-service-download'),
                  'Raw materialization is not exact current official ZIP')
        core.need(type(material['full11']) is dict and all(type(value) is int for value in material['full11'].values()),
                  'Raw full11 metadata must preserve exact Python integer values')
        pin_read(material['receipt'], json_value=False)
        with core.open_readonly(material['path']) as source:
            before = core.full11(os.fstat(source.fileno()))
            core.need(before == material['full11'] and before['st_uid'] == os.getuid() and
                      before['st_nlink'] == 1 and stat.S_IMODE(before['st_mode']) == 0o400,
                      'Raw materialization full11/seal/owner/link count differs')
            digest, count = hashlib.sha256(), 0
            while data := source.read(core.CHUNK):
                count += len(data)
                core.need(count <= material['bytes'], 'Raw service ZIP grew')
                digest.update(data)
            core.need(count == material['bytes'] and digest.hexdigest() == material['sha256'],
                      'Raw service ZIP whole size/SHA mismatch')
            source.seek(0)
            yield source
            core.need(core.full11(os.fstat(source.fileno())) == before and
                      core.full11(os.lstat(material['path'])) == before, 'Raw service ZIP full11 changed')


def bounded_index_members(inputs, artifact_id, audit):
    values, records = {}, []
    with inputs.raw(artifact_id) as source:
        official = inputs.official[artifact_id]
        audit.audit_seekable(source, official['name'] + '.zip', official['digest'][7:], official['size_in_bytes'])
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            core.need({x.filename for x in archive.infolist()} == {'manifest.json', 'upload-index.json'} and
                      len(archive.infolist()) == 2, 'Index service ZIP member inventory differs')
            for name in ('manifest.json', 'upload-index.json'):
                info = archive.getinfo(name)
                core.need(0 < info.file_size <= MAX_JSON, 'Index JSON exceeds bound before read')
                with archive.open(info) as member:
                    raw = member.read(MAX_JSON + 1)
                    core.need(len(raw) == info.file_size, 'Index JSON length mismatch')
                values[name] = (parse_json(raw), hashlib.sha256(raw).hexdigest())
                records.append({'name': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    return values, records


def validate_index(inputs, family, values):
    manifest, manifest_sha = values['manifest.json']
    index, index_sha = values['upload-index.json']
    core.need(set(index) == {'schemaVersion', 'transport', 'metadata', 'originalArtifactZipBytesPreserved',
                            'collectorManifest', 'whole', 'partSizeBytes', 'parts'}, 'Unexpected upload-index schema')
    core.need(type(index['schemaVersion']) is int and index['schemaVersion'] == 1 and index['transport'] == 'gdal4k-audit-parts' and
              index['originalArtifactZipBytesPreserved'] is False and
              index['collectorManifest'] == {'name': 'manifest.json', 'sha256': manifest_sha},
              'Index transport or manifest digest differs')
    core.need(set(manifest) == {'schemaVersion', 'exported', 'metadata', 'publicAllowlist', 'transport',
                               'partSizeBytes', 'whole', 'parts', 'zipMembers', 'collector'}, 'Unexpected collector manifest schema')
    core.need(type(manifest['schemaVersion']) is int and manifest['schemaVersion'] == 1 and manifest['exported'] is True and
              manifest['transport'] == {'format': 'zip', 'contentIdentity': 'workspace-relative-repacked-files',
                                        'originalArtifactZipBytesPreserved': False}, 'Unexpected collector transport identity')
    metadata = manifest['metadata']
    collector.validate_metadata(metadata)
    original = family['original']
    core.need(type(metadata['source']['artifactId']) is int and metadata['source']['artifactId'] > 0 and index['metadata'] == metadata and metadata['headSha'] == HEAD and
              metadata['source'] == {'artifactName': original['name'], 'artifactId': original['id'],
                                     'artifactDigest': original['digest'][7:]} and
              metadata['run']['id'] == str(original['workflow_run']['id']) and metadata['run']['attempt'] == str(inputs.run_attempts[original['workflow_run']['id']]) and
              metadata['run']['repository'] == COHORT['scope']['repository'], 'Index provenance run/attempt/head/original identity differs')
    core.need(metadata['checkoutSha'] in (HEAD, TEST_CHECKOUT), 'Unexpected current checkout commit')
    if metadata['checkoutSha'] == TEST_CHECKOUT:
        core.need(metadata.get('checkoutParents') == [BASE, HEAD], 'Test-merge checkout parents differ')
    else:
        core.need(HEAD in [metadata['checkoutSha'], *metadata.get('checkoutParents', [])], 'Head is not actual checkout/parent')
    core.need(manifest['collector'] == {'path': 'verification/package-audit-parts.py', 'sha256': COLLECTOR_SHA},
              'Collector source is not exact H17 collector')
    patterns = manifest['publicAllowlist']
    core.need(isinstance(patterns, list) and patterns and
              collector.normalize_allowlist(patterns, Path('/')) == patterns and
              collector.normalize_allowlist(metadata['allowlist'], Path('/')) == patterns,
              'Noncanonical or divergent public upload allowlist')
    core.need(manifest['partSizeBytes'] == index['partSizeBytes'] == PART_BYTES and
              manifest['whole'] == index['whole'], 'Whole or part-size identity differs')
    whole = manifest['whole']
    core.need(set(whole) == {'bytes', 'sha256'} and type(whole['bytes']) is int and
              24 * core.MiB <= whole['bytes'] <= PART_BYTES * 100 and
              re.fullmatch('[0-9a-f]{64}', whole['sha256']), 'Invalid whole AUDIT-REPACK identity')
    parts, uploaded = manifest['parts'], index['parts']
    core.need(isinstance(parts, list) and 0 < len(parts) <= 100 and len(parts) == len(uploaded) ==
              (whole['bytes'] + PART_BYTES - 1) // PART_BYTES, 'Part count differs')
    used, offset = [], 0
    label = re.sub('[^A-Za-z0-9_.-]', '-', original['name'])[:100] or 'source'
    prefix = f"audit-parts--{label}--{metadata['run']['id']}--{metadata['run']['attempt']}--{original['id']}"
    for number, (part, actual) in enumerate(zip(parts, uploaded), 1):
        core.need(set(part) == {'index', 'name', 'offset', 'bytes', 'sha256'} and
                  set(actual) == set(part) | {'artifact'} and
                  {k: v for k, v in actual.items() if k != 'artifact'} == part and
                  type(part['index']) is int and part['index'] == number and part['name'] == f'part-{number:03d}.bin' and
                  type(part['offset']) is int and part['offset'] == offset and
                  type(part['bytes']) is int and part['bytes'] == min(PART_BYTES, whole['bytes'] - offset) and
                  re.fullmatch('[0-9a-f]{64}', part['sha256']), 'Part order/name/offset/bytes/SHA mismatch')
        artifact = actual['artifact']
        core.need(set(artifact) == {'name', 'id', 'bytes', 'digest'} and type(artifact['id']) is int and
                  artifact['id'] in family['selected'], 'Index part service ID is not a selected current artifact')
        official = inputs.official[artifact['id']]
        core.need(type(artifact['bytes']) is int and artifact['bytes'] > 0 and (artifact['name'], artifact['bytes'], artifact['digest']) ==
                  (official['name'], official['size_in_bytes'], official['digest'][7:]), 'Part service name/bytes/digest differ from official catalogue')
        core.need(artifact['name'] == prefix + f'--part-{number:03d}', 'Part service name differs from exact H17 artifact prefix')
        core.need(str(official['workflow_run']['id']) == metadata['run']['id'] and
                  official['workflow_run']['head_sha'] == HEAD, 'Part service run/head differs')
        used.append(artifact['id'])
        offset += part['bytes']
    core.need(len(set(used)) == len(used) and offset == whole['bytes'], 'Duplicate or incomplete part inventory')
    members, seen, total = manifest['zipMembers'], set(), 0
    core.need(isinstance(members, list) and 0 < len(members) <= 50000, 'Selected ZIP member count differs')
    for member in members:
        core.need(set(member) == {'path', 'bytes', 'sha256', 'crc32'}, 'Invalid selected ZIP member schema')
        name = collector.safe_relative(member['path'])
        core.need(name.casefold() not in seen and collector.allowed(name, patterns) and
                  type(member['bytes']) is int and member['bytes'] >= 0 and
                  re.fullmatch('[0-9a-f]{64}', member['sha256']) and
                  re.fullmatch('[0-9a-f]{8}', member['crc32']), 'Selected ZIP path/size/SHA/CRC differs')
        seen.add(name.casefold())
        total += member['bytes']
    core.need(total <= 8 * 1024 ** 3, 'Selected member total exceeds collector bound')
    return manifest, index, used


class VirtualParts(io.RawIOBase):
    """Seekable concatenation, with one accounted decoded-part scratch lease."""
    def __init__(self, inputs, manifest, index, audit, family_chain):
        super().__init__()
        self.inputs, self.manifest, self.index, self.audit = inputs, manifest, index, audit
        self.chain = family_chain
        self.position, self.part_index = 0, None
        self.cache, self.lease = None, None
        self.offsets = [part['offset'] for part in manifest['parts']]
        self.size = manifest['whole']['bytes']
        self.cache_loads = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        target = offset if whence == io.SEEK_SET else self.position + offset if whence == io.SEEK_CUR else self.size + offset
        core.need(whence in (io.SEEK_SET, io.SEEK_CUR, io.SEEK_END) and type(target) is int and target >= 0,
                  'Invalid virtual ZIP seek')
        self.position = target
        return target

    def _drop(self):
        if self.lease is not None:
            lease, self.lease = self.lease, None
            self.cache, self.part_index = None, None
            lease.__exit__(None, None, None)

    def _load(self, number):
        if number == self.part_index:
            return
        self._drop()
        actual = self.index['parts'][number]
        part, artifact_id = self.manifest['parts'][number], actual['artifact']['id']
        lease = None
        try:
            with self.inputs.raw(artifact_id) as source:
                # The complete wrapper bytes are official-SHA verified by raw().
                # Check its strict ZIP layout/CRC too, using the already bounded
                # ordinary part payload; no logical-family acceptance is inferred.
                expected = self.inputs.official[artifact_id]
                self.audit._zip(source, expected['size_in_bytes'], [*self.chain, 'service-part', str(artifact_id)], 0)
                source.seek(0)
                with zipfile.ZipFile(source) as archive:
                    infos = archive.infolist()
                    core.need(len(infos) == 1 and infos[0].filename == part['name'] and
                              infos[0].file_size == part['bytes'], 'Part service ZIP payload inventory differs')
                    with archive.open(infos[0]) as member:
                        lease = self.audit._spool(member, part['bytes'], [*self.chain, 'raw-part', str(number + 1)])
                        cache, copied, sha = lease.__enter__()
                        core.need(copied == part['bytes'] and sha == part['sha256'], 'Raw part payload size/SHA differs')
        except BaseException:
            if lease is not None:
                lease.__exit__(None, None, None)
            raise
        self.lease, self.cache, self.part_index = lease, cache, number
        self.cache_loads += 1

    def read(self, count=-1):
        core.need(type(count) is int, 'Invalid virtual source read length')
        if count == -1:
            # ZipFile's EOCD helper asks read() only after a bounded end seek.
            count = max(0, self.size - self.position)
            core.need(count <= core.MiB, 'Unbounded virtual source read-to-end')
        core.need(0 <= count <= self.audit.config.max_cd_bytes,
                  'Virtual source read exceeds declared central-directory bound')
        count = min(count, max(0, self.size - self.position))
        result = bytearray()
        while count:
            number = bisect_right(self.offsets, self.position) - 1
            core.need(0 <= number < len(self.offsets), 'Virtual source offset outside part inventory')
            self._load(number)
            part = self.manifest['parts'][number]
            local = self.position - part['offset']
            available = min(count, part['bytes'] - local)
            self.cache.seek(local)
            data = self.cache.read(available)
            core.need(len(data) == available and available > 0, 'Short virtual part read')
            result.extend(data)
            self.position += available
            count -= available
        return bytes(result)

    def close(self):
        if not self.closed:
            self._drop()
        super().close()


def audit_family(inputs, family_id, audit):
    core.need(type(family_id) is int and family_id > 0 and family_id in inputs.families, 'Unknown current logical-family ID')
    family = inputs.families[family_id]
    original = family['original']
    name = original['name']
    if family['route'] == 'original-service-zip':
        core.need(family['selected'] == [family_id], 'Direct family differs from original service identity')
        with inputs.raw(family_id) as source:
            summary = audit.audit_seekable(source, name + '.zip', original['digest'][7:], original['size_in_bytes'])
        return {'familyId': family_id, 'familyName': name, 'route': 'original-service-zip',
                'originalArtifactZipBytesPreserved': True, 'originalServiceZIP': original,
                'serviceIDsVerified': [family_id], 'archiveAudit': summary, 'capabilityAccepted': False}
    index_ids = [i for i in family['selected'] if inputs.official[i]['name'].endswith('--index')]
    core.need(len(index_ids) == 1, 'Family must have exactly one final upload index')
    index_id = index_ids[0]
    values, index_members = bounded_index_members(inputs, index_id, audit)
    manifest, index, parts = validate_index(inputs, family, values)
    label = re.sub('[^A-Za-z0-9_.-]', '-', name)[:100] or 'source'
    prefix = f"audit-parts--{label}--{manifest['metadata']['run']['id']}--{manifest['metadata']['run']['attempt']}--{family_id}"
    core.need(inputs.official[index_id]['name'] == prefix + '--index', 'Final index name differs from exact H17 artifact prefix')
    core.need(set(parts) | {index_id} == set(family['selected']) and len(parts) + 1 == len(family['selected']),
              'Final upload index does not cover every selected family service ZIP')
    expected_members = iter(manifest['zipMembers'])
    original_emit = audit.emit
    compared = 0
    def checking_emit(record):
        nonlocal compared
        # Only immediate entries of the reconstructed family ZIP correspond to
        # the collector inventory. Nested archive records remain separate.
        immediate = len(record.get('chain', [])) == 2 and record['chain'][0] == name + '.audit-repack.zip'
        core.need(not (immediate and record.get('kind') == 'directory'),
                  'Collector AUDIT-REPACK contains an unexpected directory entry')
        if record.get('kind') == 'zip-member' and immediate:
            try:
                expected = next(expected_members)
            except StopIteration as error:
                raise core.AuditError('Extra reconstructed selected member') from error
            actual = {'path': record['chain'][1], 'bytes': record['bytes'],
                      'sha256': record['sha256'], 'crc32': record['crc32']}
            core.need(actual == expected, 'Reconstructed selected member bytes/SHA/CRC/order differ')
            compared += 1
        original_emit(record)
    audit.emit = checking_emit
    try:
        with VirtualParts(inputs, manifest, index, audit, [name + '.audit-repack.zip']) as source:
            summary = audit.audit_seekable(source, name + '.audit-repack.zip',
                                           manifest['whole']['sha256'], manifest['whole']['bytes'])
            cache_loads = source.cache_loads
        core.need(compared == len(manifest['zipMembers']) and next(expected_members, None) is None,
                  'Missing reconstructed selected member')
    finally:
        audit.emit = original_emit
    return {'familyId': family_id, 'familyName': name, 'route': 'audit-repack',
            'originalArtifactZipBytesPreserved': False, 'originalArtifactMetadataOnly': original,
            'serviceIDsVerified': [index_id, *parts], 'indexMembers': index_members,
            'provenance': manifest['metadata'], 'collector': manifest['collector'],
            'wholeAUDITREPACK': manifest['whole'], 'collectorMembersCompared': compared,
            'partCacheLoads': cache_loads, 'archiveAudit': summary, 'capabilityAccepted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    policy.add_arguments(parser)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    size = os.lstat(args.plan).st_size
    plan_ref = {'path': str(args.plan), 'bytes': size, 'sha256': args.plan_sha256}
    plan = pin_read(plan_ref)
    core.need(set(plan) == {'schemaVersion', 'kind', 'head', 'tree', 'base', 'catalogue',
                           'materializationMap', 'familyId', 'scratchRoot', 'sourceApproval', 'cohortManifest'}, 'Unexpected invocation plan schema')
    core.need(type(plan['schemaVersion']) is int and plan['schemaVersion'] == 1 and plan['kind'] == 'GDAL-cohort-family-byte-audit-plan' and
              (plan['head'], plan['tree'], plan['base']) == (HEAD, TREE, BASE), 'Plan source H/T/B identity differs')
    policy.require_plan_intent(plan)
    approval = pin_read(plan['sourceApproval'])
    core.need(policy.require_intent_reference(approval.get('cohortManifest')), 'Body source approval has different static intent')
    core.need(approval['kind'] == 'GDAL-cohort-archive-source-approval' and
              approval['approvedForActualFamilyByteAudit'] is True, 'Actual body source approval absent')
    # All executable source is pinned before the invocation record is written.
    for reference in approval['sources']:
        pin_read(reference, json_value=False)
    local = {str(Path(__file__).resolve()), str(Path(core.__file__).resolve()),
             str(Path(__file__).with_name('strict_zip_stream_v1.py').resolve()),
             str(Path(__file__).with_name('h17_archive_transport.py').resolve()),
             str(Path(__file__).with_name('h17_package_audit_parts.py').resolve()), str(Path(policy.__file__).resolve())}
    core.need(local <= {reference['path'] for reference in approval['sources']}, 'Approval omits an actual executable source')
    os.mkdir(args.output, 0o700)
    parent = os.open(args.output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    os.fsync(parent); os.close(parent)
    invocation = {'schemaVersion': 1, 'kind': 'GDAL-cohort-family-byte-audit-invocation',
                  'startedAtUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'plan': plan_ref,
                  'head': HEAD, 'tree': TREE, 'base': BASE, 'cohortManifest': policy.current_ref(), 'sources': approval['sources'],
                  'pid': os.getpid(), 'uid': os.getuid(), 'capabilityAccepted': False}
    def sealed_json(name, value):
        raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        core.need(len(raw) <= MAX_JSON, 'Final metadata output exceeds record bound')
        path = args.output / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, 'wb') as destination:
            destination.write(raw)
            destination.flush()
            os.fsync(destination.fileno())
        directory = os.open(args.output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.fsync(directory); os.close(directory)
        return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    invocation_ref = sealed_json('invocation.json', invocation)
    records_path = args.output / 'inventory.jsonl'
    fd = os.open(records_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    records_hash, record_bytes, record_count = hashlib.sha256(), 0, 0
    with os.fdopen(fd, 'wb') as records:
        def emit(value):
            nonlocal record_bytes, record_count
            raw = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
            core.need(len(raw) <= MAX_JSON and record_bytes + len(raw) <= 96 * core.MiB,
                      'Family metadata JSONL byte limit exceeded')
            records.write(raw)
            records_hash.update(raw)
            record_bytes += len(raw)
            record_count += 1
            if record_count % 128 == 0:
                records.flush()
                os.fsync(records.fileno())
        audit = core.Audit(core.AuditConfig(scratch_root=plan['scratchRoot'], max_nested_bytes=COHORT['resources']['nestedArchiveLimitBytes'], max_scratch_bytes=COHORT['resources']['scratchLimitBytes'], rss_limit=COHORT['resources']['rssLimitBytes']), emit)
        try:
            inputs = FamilyInputs(plan['catalogue'], plan['materializationMap'])
            result = audit_family(inputs, plan['familyId'], audit)
            pin_read(plan_ref)
            pin_read(plan['catalogue'])
            pin_read(plan['materializationMap'])
            for reference in approval['sources']:
                pin_read(reference, json_value=False)
            outcome = {'status': 'passed-byte-audit', 'result': result}
        except BaseException as error:
            outcome = {'status': 'failed-byte-audit', 'errorType': type(error).__name__, 'error': str(error)}
            raise_after = error
        else:
            raise_after = None
        finally:
            records.flush()
            os.fsync(records.fileno())
            os.fchmod(records.fileno(), 0o400)
    final = {'schemaVersion': 1, 'kind': 'GDAL-cohort-family-byte-audit-result',
             'completedAtUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'invocation': invocation_ref,
             'cohortManifest': policy.current_ref(),
             'inventory': {'path': str(records_path), 'bytes': record_bytes,
                           'sha256': records_hash.hexdigest(), 'recordCount': record_count},
             **outcome, 'capabilityAccepted': False, 'overallGoalComplete': False}
    result_ref = sealed_json('result.json', final)
    directory = os.open(args.output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    os.fsync(directory)
    os.close(directory)
    print(json.dumps(result_ref))
    if raise_after is not None:
        raise raise_after


if __name__ == '__main__':
    main()
