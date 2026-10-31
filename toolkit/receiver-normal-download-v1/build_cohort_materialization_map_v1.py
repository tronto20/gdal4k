"""Seal a current cohort map from approved retained inputs and completed downloads.

Importing this module performs no initialization, raw reads, or CLI execution.
The receiver owns the manifest/config loader and the shared global flock.
"""
import argparse
import json
import os
import re
import resource
import stat
import types
from pathlib import Path


def _load_receiver():
    path = str(Path(__file__).resolve().with_name('receive_cohort_raw_v1.py'))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_NONBLOCK)
    fields = lambda value: tuple(getattr(value, key) for key in (
        'st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid', 'st_size',
        'st_atime_ns', 'st_mtime_ns', 'st_ctime_ns', 'st_blocks'))
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 2 * 1048576:
            raise RuntimeError('receiver source shape/bound')
        body = source.read(2 * 1048576 + 1)
        if (len(body) != before.st_size or fields(before) != fields(os.fstat(source.fileno())) or
                fields(before) != fields(os.lstat(path))):
            raise RuntimeError('receiver source changed during NoTime import')
    module = types.ModuleType('cohort_map_builder_receiver')
    module.__file__ = path
    exec(compile(body, path, 'exec'), module.__dict__)
    return module


r = _load_receiver()


FULL11_KEYS = {
    'st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
    'st_size', 'st_atime_ns', 'st_mtime_ns', 'st_ctime_ns', 'st_blocks',
}


def _need(condition, message):
    r.require(bool(condition), message)


def _reference(value, description):
    _need(type(value) is dict and set(value) == {'path', 'bytes', 'sha256'},
          description + ' exact reference fields')
    _need(type(value['path']) is str and os.path.isabs(value['path']) and
          os.path.normpath(value['path']) == value['path'] and
          type(value['bytes']) is int and value['bytes'] > 0 and
          type(value['sha256']) is str and re.fullmatch(r'[0-9a-f]{64}', value['sha256']),
          description + ' reference primitive types')
    _need(r.pin(value['path']) == value, description + ' exact current byte pin')


def _official_artifacts(catalogue, plan):
    _need(type(catalogue.get('schemaVersion')) is int and catalogue['schemaVersion'] == 1 and
          catalogue.get('kind') == 'GDAL-cohort-official-artifact-catalogue',
          'current generic official catalogue schema')
    _need(r.canonical(catalogue.get('cohortManifest')) == r.canonical(plan['cohortManifest']) and
          catalogue.get('repository') == plan['repository'] and catalogue.get('head') == plan['head'],
          'current catalogue manifest/repository/head binding')
    runs = catalogue.get('runs')
    _need(type(runs) is list and runs, 'actual catalogue runs')
    run_ids = set()
    for run in runs:
        _need(type(run) is dict and type(run.get('id')) is int and run['id'] > 0 and
              run['id'] not in run_ids and type(run.get('attempt')) is int and run['attempt'] > 0 and
              run.get('head') == plan['head'] and run.get('status') == 'completed' and
              run.get('conclusion') == 'success', 'actual completed catalogue run binding')
        run_ids.add(run['id'])
    selected = catalogue.get('selectedArtifacts')
    _need(type(selected) is list and selected, 'current selected official artifacts')
    official = {}
    for artifact in selected:
        _need(type(artifact) is dict and type(artifact.get('id')) is int and artifact['id'] > 0 and
              artifact['id'] not in official, 'unique positive current official service ID')
        _need(type(artifact.get('size_in_bytes')) is int and
              0 < artifact['size_in_bytes'] <= r.MAX_ZIP and artifact.get('expired') is False and
              type(artifact.get('digest')) is str and
              re.fullmatch(r'sha256:[0-9a-f]{64}', artifact['digest']), 'current official byte identity')
        run = artifact.get('workflow_run')
        _need(type(run) is dict and type(run.get('id')) is int and run['id'] in run_ids and
              run.get('head_sha') == plan['head'], 'current selected artifact run/head binding')
        official[artifact['id']] = artifact
    families = catalogue.get('logicalFamilies')
    _need(type(families) is list and families, 'current logical family partition')
    used = []
    for family in families:
        _need(type(family) is dict and family.get('route') in
              ('original-service-zip', 'audit-repack') and type(family.get('selected')) is list and
              family['selected'], 'current logical family route/selection')
        _need(all(type(aid) is int and aid > 0 and aid in official for aid in family['selected']) and
              len(family['selected']) == len(set(family['selected'])), 'typed unique family service IDs')
        used.extend(family['selected'])
    _need(sorted(used) == sorted(official), 'selected service IDs partition exactly once')
    return official


def _materialization(artifact_id, official, material, receipt, label, new_download):
    _need(type(material) is dict and type(material.get('path')) is str and
          os.path.isabs(material['path']) and os.path.normpath(material['path']) == material['path'],
          'approved materialization canonical absolute path')
    if new_download:
        _need(material['path'] == r.RAW + '/' + str(artifact_id) + '.zip',
              'new current download exact owned raw destination')
    _need(type(material.get('bytes')) is int and material['bytes'] == official['size_in_bytes'] and
          material.get('sha256') == official['digest'][7:], 'materialization current official size/SHA')
    expected = material.get('full11')
    _need(type(expected) is dict and set(expected) == FULL11_KEYS and
          all(type(value) is int and value >= 0 for value in expected.values()),
          'materialization exact Python integer st_* full11')
    _reference(receipt, 'approved materialization receipt')
    parent = r.open_dir(os.path.dirname(material['path']), True)
    try:
        verified = r.digest_regular(parent, os.path.basename(material['path']),
                                    official['size_in_bytes'], official['digest'][7:],
                                    expected, seal_required=True)
    finally:
        os.close(parent)
    _need(type(verified) is dict and set(verified) == FULL11_KEYS and verified == expected and
          all(type(value) is int for value in verified.values()), 'fresh canonical full11 verification')
    return {'artifactId': artifact_id, 'path': material['path'], 'bytes': official['size_in_bytes'],
            'sha256': official['digest'][7:], 'full11': verified, 'receipt': receipt,
            'materialization': label}


def build_map():
    plan, plan_pin = r.load_plan()
    with r.lock():
        active, done, remaining = r.state(plan)
        missing = plan['missingArtifacts']
        retained = plan['retainedArtifacts']
        _need(type(missing) is list and type(retained) is dict, 'approved missing/retained partition')
        missing_ids = []
        for artifact in missing:
            _need(type(artifact) is dict and type(artifact.get('id')) is int and artifact['id'] > 0,
                  'approved missing service ID type')
            missing_ids.append(artifact['id'])
        _need(len(missing_ids) == len(set(missing_ids)), 'approved missing service IDs unique')
        _need(all(type(key) is str and re.fullmatch(r'[1-9][0-9]*', key) for key in retained),
              'approved retained map canonical decimal service IDs')
        retained_ids = {int(key) for key in retained}
        _need(not active and type(remaining) is int and remaining == 0 and
              len(done) == len(missing) and
              set(done) == {str(aid) + '.json' for aid in missing_ids},
              'all and only approved missing current downloads completed')
        catalogue = r.read_pinned_json(plan['sourceCatalogue'])
        official = _official_artifacts(catalogue, plan)
        _need(not (set(missing_ids) & retained_ids) and
              set(missing_ids) | retained_ids == set(official),
              'approved missing/explicit retained inputs exactly cover selected service IDs')
        _need(all(artifact == official[artifact['id']] for artifact in missing),
              'approved missing metadata exactly equals selected current official metadata')
        artifacts = {}
        for artifact_id in sorted(official):
            key = str(artifact_id)
            if key in retained:
                material = retained[key]
                _need(type(material) is dict and type(material.get('artifactId')) is int and
                      material['artifactId'] == artifact_id and
                      material.get('materialization') == 'retained-byte-identical-current-official',
                      'explicit retained artifact ID/label binding')
                receipt = material['receipt']
                label = 'retained-byte-identical-current-official'
                new_download = False
            else:
                path = r.BASE + '/completed/' + key + '.json'
                receipt = r.pin(path)
                completed = r.read_pinned_json(receipt)
                _need(completed.get('kind') == 'GDAL-cohort-new-current-raw-completed' and
                      completed.get('noFamilyAcceptanceClaim') is True and
                      type(completed.get('artifactId')) is int and completed['artifactId'] == artifact_id and
                      completed.get('landingRetired') is True, 'owned completed current download identity')
                promotion_ref = completed['promotion']
                _reference(promotion_ref, 'owned completed promotion')
                _need(promotion_ref['path'] == r.BASE + '/promotions/' + key + '.json',
                      'owned completed exact promotion path')
                promotion = r.read_pinned_json(promotion_ref)
                _need(promotion.get('kind') == 'GDAL-cohort-new-current-raw-promotion' and
                      promotion.get('noFamilyAcceptanceClaim') is True and
                      type(promotion.get('artifactId')) is int and promotion['artifactId'] == artifact_id and
                      r.canonical(promotion.get('plan')) == r.canonical(plan_pin) and
                      r.canonical(promotion.get('officialArtifact')) == r.canonical(official[artifact_id]) and
                      r.canonical(promotion.get('materialization')) == r.canonical(completed['materialization']),
                      'owned completed promotion plan/current official/materialization binding')
                transport_ref = promotion['transport']
                _reference(transport_ref, 'owned completed transport')
                _need(transport_ref['path'] == r.BASE + '/transports/' + key + '.json',
                      'owned completed exact transport path')
                transport = r.read_pinned_json(transport_ref)
                _need(transport.get('kind') == 'GDAL-cohort-normal-artifact-download-observation' and
                      type(transport.get('artifactId')) is int and transport['artifactId'] == artifact_id and
                      transport.get('orchestratorSource') == plan['orchestratorSource'],
                      'owned completed current transport binding')
                plugin = transport['pluginTool']
                download = transport['fileDownloadTool']
                landing = r.validate_tool_observations(plugin, download, artifact_id,
                                                       official[artifact_id]['size_in_bytes'])
                _need(landing == transport.get('landingPath'),
                      'owned completed exact normal-tool landing path')
                _need('landingFull11' not in transport, 'rounded transport landing full11 object forbidden')
                landing_full11 = r.decode_full11_json(transport.get('landingFull11JSON'))
                _need(type(promotion['materialization']) is dict and
                      'landingFull11' in promotion['materialization'] and
                      r.canonical(promotion['materialization']['landingFull11']) ==
                      r.canonical(landing_full11), 'owned completed landing full11 provenance binding')
                for role, stage, operation in (
                        ('pluginObservation', 'plugin', plugin),
                        ('fileDownloadObservation', 'file', download)):
                    operation_ref = transport.get(role)
                    _need(type(operation_ref) is dict and operation_ref.get('path') ==
                          r.BASE + '/operations/' + key + '-' + stage + '.json',
                          'owned completed exact normal-tool operation path')
                    observed = r.read_pinned_json(operation_ref)
                    _need(observed.get('kind') == 'GDAL-cohort-actual-normal-download-tool-operation' and
                          type(observed.get('artifactId')) is int and observed['artifactId'] == artifact_id and
                          observed.get('stage') == stage and
                          r.canonical(observed.get('orchestratorSource')) == r.canonical(plan['orchestratorSource']) and
                          r.canonical(observed.get('operation')) == r.canonical(operation),
                          'owned completed exact normal-tool operation metadata binding')
                material = completed['materialization']
                label = 'new-normal-service-download'
                new_download = True
            artifacts[key] = _materialization(artifact_id, official[artifact_id], material,
                                               receipt, label, new_download)
        _need(set(artifacts) == {str(aid) for aid in official} and
              len(artifacts) == len(official) and
              sum(value['bytes'] for value in artifacts.values()) ==
              sum(value['size_in_bytes'] for value in official.values()),
              'dynamic final selected artifact partition/count/bytes')
        output = {'schemaVersion': 1, 'kind': 'GDAL-cohort-materialization-map',
                  'cohortManifest': plan['cohortManifest'], 'repository': plan['repository'],
                  'head': plan['head'], 'sourceCatalogue': plan['sourceCatalogue'],
                  'artifacts': artifacts, 'recoveryPlan': plan_pin, 'atUtc': r.now(),
                  'selectedArtifactCount': len(official),
                  'selectedArtifactBytes': sum(value['bytes'] for value in artifacts.values()),
                  'allSelectedWholeOfficialSHAsFreshlyVerified': True,
                  'noFamilyAcceptanceClaim': True}
        return r.seal(r.BASE + '/materialization-map-v1.json', output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    r.add_arguments(parser)
    args = parser.parse_args(argv)
    resource.setrlimit(resource.RLIMIT_AS, (r.WORKER_RSS, r.WORKER_RSS))
    r.initialize(args)
    print(json.dumps(build_map()))


if __name__ == '__main__':
    main()
