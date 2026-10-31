"""Bounded metadata and selected-evidence reads for current-cohort semantic gates."""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import types
import xml.etree.ElementTree as ET
import zipfile

import recovery_archive_core_v3 as core
import cohort_policy_v1 as policy
import c23_sdk_identity_gates_v1 as sdk_identity
COHORT = policy.initialize_for_process()
import c23_family_source_v1 as family_source

HEAD, TREE, BASE = family_source.HEAD, family_source.TREE, family_source.BASE
TEST_CHECKOUT = family_source.TEST_CHECKOUT
BASE_TREE = COHORT['scope']['baseTree']
GATE_ORDER = ('sourceBindings', 'maven', 'mavenAliasIndependent', 'defaultArm64', 'combinedArm64',
              'windowsReadiness', 'platformScopes', 'aggregateCurrent', 'finalAcceptance')


class GateError(ValueError):
    pass


def require(value, message):
    if not value:
        raise GateError(message)


def verify_current_joblog_manifest(capture):
    require(type(capture) is dict and set(capture) == {'schemaVersion', 'kind', 'cohortManifest', 'head', 'capturedAtUtc', 'captures', 'sdkCaptures', 'noAcceptanceClaim'} and
            type(capture['schemaVersion']) is int and capture['schemaVersion'] == 1 and
            capture['kind'] == 'GDAL-cohort-current-normal-plugin-joblog-captures' and
            capture['head'] == HEAD and capture['noAcceptanceClaim'] is True and
            policy.require_intent_reference(capture['cohortManifest']) and
            type(capture['captures']) is list and len(capture['captures']) == 2 and
            type(capture['sdkCaptures']) is list,
            'Unexpected current platform/SDK job log capture manifest')


def ref3(value):
    return {key: value[key] for key in ('path', 'bytes', 'sha256')}


def pin_json(reference):
    return family_source.pin_read(ref3(reference))


def pin_bytes(reference):
    return family_source.pin_read(ref3(reference), json_value=False)


REGISTRATION_FIELDS = ('gate', 'head', 'tree', 'base', 'scope', 'byteAuditCloseout', 'serialDriverSource',
                       'familyResults', 'catalogue', 'materializationMap', 'bodySourceApprovals',
                       'sourceApproval', 'sourceRoot', 'jobLogs', 'priorVerdicts', 'cohortManifest', 'scratchRoot')


def verify_input_registration(registration, plan_ref, plan):
    expected_keys = {'schemaVersion', 'kind', 'rootApprovedActualInputs', 'registeredAtUtc', 'plan', *REGISTRATION_FIELDS}
    require(set(registration) == expected_keys and type(registration['schemaVersion']) is int and
            registration['schemaVersion'] == 1 and registration['kind'] == 'GDAL-cohort-actual-semantic-input-registration' and
            registration['rootApprovedActualInputs'] is True and registration['plan'] == ref3(plan_ref),
            'Actual gate input registration kind/approval/exact plan differs')
    policy.validate_reference(registration['plan'])
    policy.require_plan_intent(registration)
    policy.require_plan_intent(plan)
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    require(all(canonical(registration[field]) == canonical(plan[field]) for field in REGISTRATION_FIELDS),
            'Root registered current gate inputs differ from the actual gate plan')
    registered = parse_utc(registration['registeredAtUtc'])
    require(registered <= datetime.datetime.now(datetime.timezone.utc), 'Input registration is in the future')
    # No invocation/result refs are permitted: the registration is sealed after
    # the plan and before the invocation, so this input graph is acyclic.
    return registration


def parse_utc(value):
    require(isinstance(value, str), 'Timestamp must be a string')
    parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'Timestamp must include timezone')
    return parsed.astimezone(datetime.timezone.utc)


def verify_inventory(reference):
    require(type(reference['bytes']) is int and 0 < reference['bytes'] <= 96 * core.MiB,
            'Family inventory exceeds declared metadata bound')
    with core.open_readonly(reference['path']) as source:
        before = core.full11(os.fstat(source.fileno()))
        require(before['st_size'] == reference['bytes'], 'Inventory length differs')
        digest, count = hashlib.sha256(), 0
        while data := source.read(core.CHUNK):
            count += len(data)
            require(count <= reference['bytes'], 'Inventory grew')
            digest.update(data)
        require(count == reference['bytes'] and digest.hexdigest() == reference['sha256'], 'Inventory SHA256 differs')
        require(core.full11(os.fstat(source.fileno())) == before and
                core.full11(os.lstat(reference['path'])) == before, 'Inventory full11 changed')
    return before


class GateContext:
    """The plan is pinned by the gate runner before this context is created.

    A body result proves bytes only. This context verifies its invocation and
    source approval, exact current official identities, and a fresh independently
    qualified GitHub observation. Gate predicates still determine capability.
    """
    require = staticmethod(require)

    def __init__(self, plan):
        self.plan = plan
        policy.require_plan_intent(plan)
        self.cohort = policy.current()
        require((plan['head'], plan['tree'], plan['base']) == (HEAD, TREE, BASE), 'Gate plan H/T/B differs')
        self.source_root = Path(plan['sourceRoot'])
        require(str(self.source_root) == self.cohort['source']['root'], 'Gate source root differs from static intent')
        self.inputs = family_source.FamilyInputs(ref3(plan['catalogue']), ref3(plan['materializationMap']))
        self.scope = pin_json(plan['scope'])
        self._verify_scope()
        self.role_checks, self.official_artifacts = self.scope['requiredRoleChecks'], self.scope['artifacts']
        semantic_approval = pin_json(plan['sourceApproval'])
        driver_ref = ref3(plan['serialDriverSource'])
        require(driver_ref in semantic_approval['sources'] and
                driver_ref['path'] == str(Path(__file__).with_name('c23_serial_family_driver_v1.py')),
                'Actual serial driver reference is outside the exact reviewed current source closure')
        pin_bytes(driver_ref)
        self.driver_events = self._verify_driver_closeout()
        self.families, self.family_results, self.inventory_refs, self.inventory_full11 = {}, {}, {}, {}
        approved_sources = []
        for reference in plan['bodySourceApprovals']:
            approval = pin_json(reference)
            require(approval['kind'] == 'GDAL-cohort-archive-source-approval' and
                    approval['approvedForActualFamilyByteAudit'] is True and policy.require_intent_reference(approval.get('cohortManifest')), 'Body source approval differs')
            for source in approval['sources']:
                pin_bytes(source)
            approved_sources.append(approval['sources'])
        require(approved_sources, 'No exact body source approval supplied')
        require(len(plan['familyResults']) == len(COHORT['familyNames']), 'Exact current family byte results are required')
        all_service_ids = []
        for reference in plan['familyResults']:
            result = pin_json(reference)
            require(result['kind'] == 'GDAL-cohort-family-byte-audit-result' and
                    result['status'] == 'passed-byte-audit' and result['capabilityAccepted'] is False and policy.require_intent_reference(result.get('cohortManifest')),
                    'Family result is not a completed bytes-only audit')
            invocation = pin_json(result['invocation'])
            require(invocation['kind'] == 'GDAL-cohort-family-byte-audit-invocation' and
                    (invocation['head'], invocation['tree'], invocation['base']) == (HEAD, TREE, BASE) and policy.require_intent_reference(invocation.get('cohortManifest')),
                    'Actual family invocation source differs')
            require(invocation['sources'] in approved_sources, 'Family used an unapproved executable source closure')
            actual_plan = pin_json(invocation['plan'])
            require(policy.require_intent_reference(actual_plan.get('cohortManifest')) and actual_plan['catalogue'] == ref3(plan['catalogue']) and
                    actual_plan['materializationMap'] == ref3(plan['materializationMap']),
                    'Family invocation inputs differ from current gate inputs')
            family = result['result']
            fid, name = family['familyId'], family['familyName']
            event = self.driver_events[fid]
            require(event['started']['pid'] == invocation['pid'] and event['started']['plan'] == invocation['plan'] and
                    event['sealed']['result'] == ref3(reference), 'Family result has no exact actual driver child/plan/result event')
            require(fid in self.inputs.families and fid == actual_plan['familyId'] and
                    name == self.inputs.families[fid]['original']['name'] and name not in self.families,
                    'Duplicate/unknown family or actual invocation family differs')
            expected_family = self.inputs.families[fid]
            require(family['route'] == expected_family['route'] and
                    set(family['serviceIDsVerified']) == set(expected_family['selected']),
                    'Family route or actually verified service IDs differ')
            original = expected_family['original']
            expected_archive = ({'bytes': original['size_in_bytes'], 'sha256': original['digest'][7:]}
                                if family['route'] == 'original-service-zip' else family['wholeAUDITREPACK'])
            require(family['archiveAudit']['bytes'] == expected_archive['bytes'] and
                    family['archiveAudit']['sha256'] == expected_archive['sha256'] and
                    family['originalArtifactZipBytesPreserved'] is (family['route'] == 'original-service-zip'),
                    'Family whole-container identity/retention claim differs')
            self.inventory_full11[fid] = verify_inventory(result['inventory'])
            self.inventory_refs[fid] = result['inventory']
            self.families[name], self.family_results[fid] = family, result
            all_service_ids.extend(family['serviceIDsVerified'])
        require(set(self.family_results) == set(self.inputs.families) and
                sorted(all_service_ids) == sorted(self.inputs.official), 'Current families do not partition the exact genuine service ZIPs')
        self.prior, self.prior_verdict_refs = {}, {}
        for reference in plan.get('priorVerdicts', []):
            verdict = pin_json(reference)
            require(verdict['kind'] == 'GDAL-cohort-semantic-gate-verdict' and verdict['status'] == 'passed' and policy.require_intent_reference(verdict.get('cohortManifest')) and
                    (verdict['head'], verdict['tree'], verdict['base']) == (HEAD, TREE, BASE), 'Prior verdict source/status differs')
            gate = verdict['gate']
            require(gate in GATE_ORDER and gate not in self.prior, 'Duplicate or unknown prior gate')
            actual = pin_json(verdict['invocation'])
            require(actual['kind'] == 'GDAL-cohort-semantic-gate-invocation' and actual['gate'] == gate and policy.require_intent_reference(actual.get('cohortManifest')) and
                    actual['plan'] == verdict['plan'] and
                    (actual['head'], actual['tree'], actual['base']) == (HEAD, TREE, BASE),
                    'Prior verdict has no exact actual gate invocation')
            prior_plan = pin_json(verdict['plan'])
            registration = pin_json(actual['registration'])
            verify_input_registration(registration, verdict['plan'], prior_plan)
            require(parse_utc(registration['registeredAtUtc']) <= parse_utc(actual['startedAtUtc']),
                    'Prior actual input registration was created after the invocation started')
            require(policy.require_intent_reference(prior_plan.get('cohortManifest')) and prior_plan['familyResults'] == plan['familyResults'] and
                    prior_plan['catalogue'] == plan['catalogue'] and
                    prior_plan['materializationMap'] == plan['materializationMap'] and
                    prior_plan['sourceRoot'] == plan['sourceRoot'] and prior_plan['gate'] == gate and
                    GATE_ORDER.index(gate) < GATE_ORDER.index(plan['gate']),
                    'Prior gate used different current byte evidence or violates acyclic gate order')
            prior_approval = pin_json(prior_plan['sourceApproval'])
            require(prior_approval['kind'] == 'GDAL-cohort-semantic-source-approval' and
                    prior_approval['approvedForActualSemanticGates'] is True and policy.require_intent_reference(prior_approval.get('cohortManifest')) and
                    actual['sources'] == verdict['sources'] == prior_approval['sources'] and
                    isinstance(verdict['result'], dict), 'Prior actual executable source/result closure differs')
            for source in prior_approval['sources']:
                pin_bytes(source)
            self.prior[gate], self.prior_verdict_refs[gate] = verdict['result'], ref3(reference)
        self.default_version = self.prior.get('maven', {}).get('defaultVersion')
        self.job_logs, self.job_log_refs = {}, {}
        capture = pin_json(plan['jobLogs'])
        verify_current_joblog_manifest(capture)
        for item in capture['captures']:
            role = item['role']
            require(role in ('defaultArm64', 'windowsReadiness') and role not in self.job_logs and
                    item['head'] == HEAD and item['run'] == COHORT['scope']['primaryRun'] and type(item['attempt']) is int and
                    item['attempt'] == self.inputs.run_attempts[item['run']],
                    'Job log source role/run/head differs')
            operation = pin_json(item['operation'])
            require(operation['operation'] == 'github_fetch_workflow_job_logs' and
                    operation['arguments'] == {'repo_full_name': COHORT['scope']['repository'], 'job_id': item['jobId']},
                    'Job log actual operation arguments differ')
            returned = operation['returnedResult']
            require(returned.get('isError') in (None, False), 'Actual job log operation failed')
            text = returned['structuredContent']['content']
            require(type(text) is str and text.encode('utf-8') == pin_bytes(item['decodedText']),
                    'Decoded job log differs from exact actual operation text/BOM/newlines')
            role_name = 'verify / linux-arm64-runtime' if role == 'defaultArm64' else 'verify / Native mingwX64'
            checks = [check for check in self.role_checks if check['name'] == role_name and check['runId'] == COHORT['scope']['primaryRun']]
            require(len(checks) == 1 and type(item['jobId']) is int and checks[0]['id'] == item['jobId'] and checks[0]['attempt'] == item['attempt'],
                    'Actual job log capture is not the exact currently qualified role/job')
            self.job_logs[role], self.job_log_refs[role] = text, item
        require(set(self.job_logs) == {'defaultArm64', 'windowsReadiness'}, 'Both actual role job logs are required')
        self.sdk_job_logs = sdk_identity.verify_sdk_log_captures(self, capture)

    def _verify_driver_closeout(self):
        closed = pin_json(self.plan['byteAuditCloseout'])
        require(closed['kind'] == 'GDAL-cohort-family-byte-audit-closeout' and
                closed['allFamilyByteAuditsCompleted'] is True and closed['familiesCompleted'] == len(COHORT['familyNames']) and
                closed['failure'] is None and closed['capabilityAccepted'] is False and
                (closed['head'], closed['tree'], closed['base']) == (HEAD, TREE, BASE),
                'Actual serial driver did not close all current family byte audits')
        require(closed['familyResults'] == self.plan['familyResults'] and
                len(closed['familyResults']) == len(COHORT['familyNames']) and
                closed['catalogue'] == self.plan['catalogue'] and
                closed['materializationMap'] == self.plan['materializationMap'] and
                closed['sourceApproval'] in self.plan['bodySourceApprovals'],
                'Serial driver closed different current family byte evidence')
        actual = pin_json(closed['invocation'])
        require(actual['kind'] == 'GDAL-cohort-serial-driver-invocation' and actual['config'] == closed['config'] and
                actual['driverSource'] == self.plan['serialDriverSource'] and policy.require_intent_reference(actual.get('cohortManifest')), 'Missing exact actual serial-driver invocation/source')
        pin_bytes(self.plan['serialDriverSource'])
        config = pin_json(closed['config'])
        require(policy.require_intent_reference(config.get('cohortManifest')) and config['catalogue'] == self.plan['catalogue'] and config['materializationMap'] == self.plan['materializationMap'] and
                config['sourceApproval'] == closed['sourceApproval'] and
                actual['actualFamilyOrder'] == config['orderedFamilyIds'] and
                len(actual['actualFamilyOrder']) == len(COHORT['familyNames']) and set(actual['actualFamilyOrder']) == set(self.inputs.families),
                'Actual serial driver configuration/family order differs')
        raw = pin_bytes(closed['events'])
        events, ordering = {}, []
        for line in raw.splitlines():
            row = family_source.parse_json(line)
            event = row['event']
            require(event in ('family-admitted', 'actual-family-child-started', 'family-byte-proof-sealed'),
                    'Actual serial driver recorded failure/unknown event')
            fid = row['familyId']
            require(fid in self.inputs.families, 'Driver event has unknown family')
            slots = events.setdefault(fid, {})
            slot = {'family-admitted': 'admitted', 'actual-family-child-started': 'started',
                    'family-byte-proof-sealed': 'sealed'}[event]
            require(slot not in slots, 'Duplicate actual driver family event')
            if slot == 'started':
                require('admitted' in slots and row['source'] == config['bodySource'] and
                        type(row['pid']) is int and row['pid'] > 0, 'Actual driver start source/ordering differs')
            if slot == 'sealed':
                require('started' in slots and row['completed'] == len(ordering) + 1 and
                        row['result'] == closed['familyResults'][len(ordering)],
                        'Actual driver completion ordering/result pin differs')
                ordering.append(fid)
            slots[slot] = row
        require(ordering == actual['actualFamilyOrder'] and
                all(set(event) == {'admitted', 'started', 'sealed'} for event in events.values()),
                'Actual serial driver events do not prove all ordered current child executions')
        return events

    def _verify_scope(self):
        scope = self.scope
        require(type(scope['schemaVersion']) is int and scope['schemaVersion'] == 1 and scope['kind'] == 'GDAL-cohort-qualified-current-scope' and policy.require_intent_reference(scope.get('cohortManifest')), 'Unexpected fresh scope schema')
        require((scope['head'], scope['tree'], scope['base'], scope['baseTree'], scope['testCheckout'],
                 scope['checkoutTree'], scope['checkoutParents']) ==
                (HEAD, TREE, BASE, BASE_TREE, TEST_CHECKOUT, TREE, [BASE, HEAD]), 'Fresh Git/checkout source join differs')
        start, completed, expiry = map(parse_utc, (scope['observedStartedAtUtc'], scope['observedCompletedAtUtc'], scope['expiresAtUtc']))
        now = datetime.datetime.now(datetime.timezone.utc)
        require(start <= completed <= now < expiry and (expiry - start).total_seconds() <= COHORT['ci']['maximumAgeSeconds'] and
                (now - start).total_seconds() < COHORT['ci']['maximumAgeSeconds'], 'Current official scope expired or timing differs')
        self._verify_scope_authority()
        require(scope['pr']['number'] == COHORT['scope']['pr'] and scope['pr']['state'] == 'open' and
                scope['pr']['head'] == HEAD and scope['pr']['base'] == BASE, 'Current PR source differs')
        roles = scope['requiredRoleChecks']
        expected = {name: COHORT['scope'][which+'Run'] for which,names in COHORT['ci']['requiredRoles'].items() for name in names}
        runs = scope['runs']
        require(isinstance(runs,list) and len(runs)==len(family_source.RUNS), 'Qualified current run bindings missing')
        attempts = {}
        for run in runs:
            require(set(run)=={'id','attempt','status','conclusion','head'} and type(run['id']) is int and
                    run['id'] in family_source.RUNS and run['id'] not in attempts and type(run['attempt']) is int and run['attempt']>0 and
                    run['status']=='completed' and run['conclusion']=='success' and run['head']==HEAD,
                    'Qualified current run attempt/status differs')
            attempts[run['id']] = run['attempt']
        require(attempts == self.inputs.run_attempts, 'Current run attempt differs from immutable body catalogue')
        require(len(roles)==len(expected) and len({x['id'] for x in roles})==len(expected) and
                {x['name'] for x in roles}==set(expected) and all(type(x['id']) is int and x['id']>0 and
                type(x['runId']) is int and x['runId']==expected[x['name']] and type(x['attempt']) is int and
                x['attempt']==attempts[x['runId']] and x['head']==HEAD and x['status']=='completed' and x['conclusion']=='success'
                for x in roles), 'Current required role/run/attempt source differs')
        current = {x['id']: x for x in scope['artifacts']}
        require(set(current) == set(self.inputs.official), 'Fresh current artifact inventory differs from audited ZIPs')
        for artifact_id, original in self.inputs.official.items():
            actual = current[artifact_id]
            require((actual['name'], actual['size'], actual['digest'], actual['runId'], actual['head'], actual['expired']) ==
                    (original['name'], original['size_in_bytes'], original['digest'], original['workflow_run']['id'], HEAD, False),
                    'Current official artifact identity changed')
        for reference in scope['sources']:
            pin_bytes(reference)
        policy.read_pin(scope['actualObservationRecord'], maximum=8*core.MiB, sealed=True)
        require(isinstance(scope['observations'], list) and scope['observations'], 'Actual current observation operation inventory differs')
        for observation in scope['observations']:
            pin_bytes(observation['operation'])
            pin_bytes(observation['body'])

    def _verify_scope_authority(self):
        """Replay the reviewed pure qualifier on the actual17 operation record.

        The original qualification instant is used only for exact derivation;
        _verify_scope separately enforces freshness against the current clock.
        """
        scope = self.scope
        approval = pin_json(self.plan['sourceApproval'])
        require(approval['kind'] == 'GDAL-cohort-semantic-source-approval' and
                approval['approvedForActualSemanticGates'] is True and policy.require_intent_reference(approval.get('cohortManifest')) and len(scope['sources']) == 3 and
                all(reference in approval['sources'] for reference in scope['sources']),
                'Current scope executable authority is outside root approved source closure')
        qualifiers = [reference for reference in scope['sources']
                      if Path(reference['path']).name == 'c23_scope_qualifier_v1.py']
        require(len(qualifiers) == 1 and scope['catalogue'] == ref3(self.plan['catalogue']),
                'Current qualifier source/catalogue binding differs')
        raw_source = pin_bytes(qualifiers[0])
        module = types.ModuleType('cohort_reviewed_current_scope_replay')
        module.__file__ = qualifiers[0]['path']
        exec(compile(raw_source, qualifiers[0]['path'], 'exec'), module.__dict__)
        require(scope['sources'] == [module.COLLECTOR_REF, qualifiers[0], module.POLICY_REF],
                'Actual current collector/qualifier/intent-loader source closure differs')
        pin_bytes(module.POLICY_REF)
        for reference in scope['sources']:
            pin_bytes(reference)
        record = policy.parse_json(policy.read_pin(scope['actualObservationRecord'], maximum=8*core.MiB, sealed=True))
        catalogue = pin_json(self.plan['catalogue'])
        derived, entries = module.qualify_record(record, catalogue, parse_utc(scope['qualifiedAtUtc']))
        require(all(scope.get(key) == value for key, value in derived.items()),
                'Qualified current scope fields differ from replayed actual GitHub observations')
        require(len(scope['observations']) == len(entries) and {row['role'] for row in scope['observations']} == set(entries),
                'Qualified observation projection role inventory differs')
        for observation in scope['observations']:
            entry = entries[observation['role']]
            require(observation['actualObservationRecord'] == scope['actualObservationRecord'] and
                    observation['url'] == entry['url'] and observation['before'] == entry['before'] and
                    observation['after'] == entry['after'] and
                    pin_bytes(observation['operation']) == module.canonical(entry) and
                    pin_bytes(observation['body']) == entry['result']['structuredContent']['content'].encode('utf-8'),
                    'Actual observation projection bytes/timing/URL differ from complete genuine operation record')

    @staticmethod
    def key(record):
        return record['familyId'], tuple(record['chain'])

    def raw_records(self, family=None):
        for fid, reference in self.inventory_refs.items():
            name = self.family_results[fid]['result']['familyName']
            if family is not None and family not in (name, fid):
                continue
            with core.open_readonly(reference['path']) as source:
                before = core.full11(os.fstat(source.fileno()))
                require(before == self.inventory_full11[fid], 'Inventory full11 differs from initial qualified readback')
                digest, size, count = hashlib.sha256(), 0, 0
                reader = io.BufferedReader(source, buffer_size=core.CHUNK)
                try:
                    while raw := reader.readline(2 * core.MiB + 1):
                        require(len(raw) <= 2 * core.MiB and raw.endswith(b'\n'), 'Inventory JSONL record bound/framing differs')
                        digest.update(raw)
                        size += len(raw)
                        count += 1
                        value = family_source.parse_json(raw)
                        require(isinstance(value, dict), 'Inventory record must be an object')
                        yield {**value, 'familyId': fid, 'familyName': name}
                    require(size == reference['bytes'] and digest.hexdigest() == reference['sha256'] and
                            count == reference['recordCount'], 'Inventory record count/bytes/SHA differs')
                    require(core.full11(os.fstat(source.fileno())) == before and
                            core.full11(os.lstat(reference['path'])) == before, 'Inventory changed while iterating')
                finally:
                    require(core.full11(os.fstat(source.fileno())) == before and
                            core.full11(os.lstat(reference['path'])) == before, 'Inventory full11 changed before reader closure')
                    reader.detach()

    def records(self, family=None):
        for record in self.raw_records(family):
            if record.get('kind') in ('zip-member', 'tar-member'):
                yield record

    def find_records(self, family=None, suffix=None, kind=None):
        for record in self.records(family):
            if suffix is not None and not record['chain'][-1].endswith(suffix):
                continue
            if kind is not None and record['kind'] != kind:
                continue
            yield record

    @contextmanager
    def _family_zip(self, fid, audit):
        family = self.inputs.families[fid]
        if family['route'] == 'original-service-zip':
            with self.inputs.raw(fid) as source:
                yield source
        else:
            indices = [i for i in family['selected'] if self.inputs.official[i]['name'].endswith('--index')]
            require(len(indices) == 1, 'Missing current final index')
            values, _ = family_source.bounded_index_members(self.inputs, indices[0], audit)
            manifest, index, parts = family_source.validate_index(self.inputs, family, values)
            require(manifest['whole'] == self.family_results[fid]['result']['wholeAUDITREPACK'], 'Current virtual whole identity differs')
            with family_source.VirtualParts(self.inputs, manifest, index, audit,
                                            [family['original']['name'] + '.audit-repack.zip']) as source:
                yield source

    def read_many(self, records, max_file_bytes=2 * core.MiB, max_total_bytes=8 * core.MiB):
        require(type(max_file_bytes) is int and 0 < max_file_bytes <= 2 * core.MiB and
                type(max_total_bytes) is int and 0 < max_total_bytes <= 8 * core.MiB, 'Selected evidence read budget differs')
        wanted, grouped = {}, defaultdict(list)
        total = 0
        for record in records:
            key = self.key(record)
            require(key not in wanted and record['kind'] in ('zip-member', 'tar-member') and
                    type(record['bytes']) is int and 0 <= record['bytes'] <= max_file_bytes, 'Selected evidence member exceeds bound or duplicate')
            total += record['bytes']
            require(total <= max_total_bytes, 'Selected evidence batch exceeds total read bound')
            wanted[key] = record
            grouped[record['familyId']].append(record)
        output = {}
        for fid, members in grouped.items():
            prefixes = {tuple(record['chain'][:length]) for record in members for length in range(2, len(record['chain']) + 1)}
            expected = {tuple(record['chain']): record for record in self.records(fid)
                        if tuple(record['chain']) in prefixes}
            require(prefixes <= set(expected), 'Selected evidence ancestor/member lacks byte-audit record')
            for member in members:
                actual = expected[tuple(member['chain'])]
                require(all(member.get(field) == actual.get(field) for field in ('kind', 'chain', 'bytes', 'sha256')),
                        'Selected evidence descriptor differs from immutable inventory')
            trie = {}
            for record in members:
                node = trie
                for component in record['chain'][1:]:
                    node = node.setdefault(component, {})
                node['__record__'] = record
            with tempfile.TemporaryDirectory(prefix='cohort-gate-read-', dir=self.plan['scratchRoot']) as private:
                audit = core.Audit(core.AuditConfig(scratch_root=private,
                                                   max_nested_bytes=128 * core.MiB,
                                                   max_scratch_bytes=192 * core.MiB), lambda value: None)
                with self._family_zip(fid, audit) as source:
                    root_name = members[0]['chain'][0]
                    require(all(record['chain'][0] == root_name for record in members), 'Family root chain differs')
                    self._walk_zip(source, trie, [root_name], expected, output, fid, audit, max_file_bytes)
        require(set(output) == set(wanted), 'Missing selected evidence bytes')
        return output

    def _walk_zip(self, source, trie, chain, expected, output, fid, audit, maximum):
        with zipfile.ZipFile(source) as archive:
            for name, node in trie.items():
                require(name != '__record__', 'Cannot select archive and descendant in same evidence batch')
                infos = [info for info in archive.infolist() if info.filename == name]
                require(len(infos) == 1 and not infos[0].is_dir(), 'Selected ZIP member missing/duplicate/directory')
                info = infos[0]
                record = expected[tuple([*chain, name])]
                require(info.file_size == record['bytes'], 'Selected ZIP declared size differs')
                with archive.open(info) as member:
                    self._walk_member(member, info.file_size, node, [*chain, name], expected,
                                      output, fid, audit, maximum)

    def _walk_member(self, source, size, node, chain, expected, output, fid, audit, maximum):
        record = expected[tuple(chain)]
        if '__record__' in node:
            require(len(node) == 1 and size <= maximum, 'Evidence selection overlaps descendants or exceeds bound')
            data, digest = bytearray(), hashlib.sha256()
            while chunk := source.read(core.CHUNK):
                data.extend(chunk)
                require(len(data) <= size, 'Selected evidence grew')
                digest.update(chunk)
            require(len(data) == size == record['bytes'] and digest.hexdigest() == record['sha256'],
                    'Selected evidence bytes/SHA differ from actual body audit')
            output[(fid, tuple(chain))] = bytes(data)
            return
        suffix = chain[-1].lower()
        if suffix.endswith(('.zip', '.jar', '.aar', '.klib')):
            with audit._spool(source, size, chain) as (copied, count, sha):
                require(count == record['bytes'] and sha == record['sha256'], 'Selected ZIP ancestor bytes/SHA differ')
                self._walk_zip(copied, node, chain, expected, output, fid, audit, maximum)
        elif suffix.endswith(('.tar.gz', '.tar.xz')):
            codec = 'xz' if suffix.endswith('.xz') else 'gzip'
            decoder = core.transport._Decoder(source, codec, memory_limit=audit.config.xz_memory_limit,
                                               max_decoded_bytes=audit.config.max_decoded_bytes)
            buffered = io.BufferedReader(core.transport._ChunkReader(decoder.chunks()), buffer_size=core.CHUNK)
            seen, last_end = set(), 0
            try:
                with tarfile.open(fileobj=buffered, mode='r|', tarinfo=core.transport._LimitedTarInfo) as archive:
                    for member in archive:
                        directory = member.type == tarfile.DIRTYPE
                        name = core.safe_name(member.name[:-1] if directory and member.name.endswith('/') else member.name)
                        require(name.casefold() not in seen, 'Duplicate selected TAR ancestor path')
                        seen.add(name.casefold())
                        require(len(seen) <= audit.config.max_entries and
                                member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE) and member.sparse is None,
                                'Invalid selected TAR ancestor member')
                        if directory:
                            require(member.size == 0, 'Selected TAR directory has payload')
                        elif name in node:
                            ancestor = expected[tuple([*chain, name])]
                            require(member.size == ancestor['bytes'], 'Selected TAR member declared size differs')
                            with archive.extractfile(member) as child:
                                self._walk_member(child, member.size, node[name], [*chain, name], expected,
                                                  output, fid, audit, maximum)
                        last_end = member.offset_data + ((member.size + 511) // 512) * 512
                        archive.members.clear()
                        audit._checkpoint()
                    padding = archive.fileobj.tell() - last_end
                    while tail := archive.fileobj.read(core.CHUNK):
                        require(not tail.strip(b'\x00'), 'Nonzero selected TAR trailing bytes')
                        padding += len(tail)
                    require(padding >= 1024, 'Selected TAR end markers truncated')
            finally:
                buffered.close()
            summary = decoder.summary()
            require(summary['archiveBytes'] == record['bytes'] and summary['archiveSha256'] == record['sha256'],
                    'Selected TAR compressed ancestor identity differs')
        else:
            raise GateError('Selected evidence has a nonarchive ancestor')

    def read_json(self, record, max_bytes=2 * core.MiB):
        raw = self.read_many([record], max_file_bytes=max_bytes)[self.key(record)]
        return family_source.parse_json(raw)

    def read_xml(self, record, max_bytes=2 * core.MiB):
        raw = self.read_many([record], max_file_bytes=max_bytes)[self.key(record)]
        require(b'<!DOCTYPE' not in raw.upper() and b'<!ENTITY' not in raw.upper(), 'XML DTD/entities are outside evidence contract')
        return ET.fromstring(raw)
