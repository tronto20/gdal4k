"""Retain a completed consumer invocation's results, including failed invocations."""
import errno
import json
import os
from pathlib import Path
import shutil


def preserve_consumer_evidence(consumer, destination, failure=None):
    consumer, destination = Path(consumer), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    preservation = {'hardlinked_files': 0, 'copied_files': 0}

    def preserve_file(source, output):
        # The Gradle invocation has returned. Only its owned temporary files may
        # be hardlinked; SDK/cache files retain the existing ordinary-copy path.
        source, output = Path(source), Path(output)
        if output.is_symlink():
            raise RuntimeError('Refusing symlinked consumer evidence file: ' + str(output))
        if output.exists():
            output.unlink()
        if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(consumer.resolve()):
            preservation['copied_files'] += 1
            return shutil.copy2(source, output)
        try:
            os.link(source, output)
            preservation['hardlinked_files'] += 1
            return str(output)
        except OSError as error:
            if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EINVAL}:
                raise
            preservation['copied_files'] += 1
            return shutil.copy2(source, output)

    for folder in ['build/test-results', 'build/outputs/androidTest-results', 'build/bin']:
        if (consumer / folder).exists():
            shutil.copytree(consumer / folder, destination / Path(folder).name,
                            dirs_exist_ok=True, copy_function=preserve_file)
    for name in ['build/jvm-runtime-files.txt', 'build/jvm-runtime-coordinates.txt']:
        if (consumer / name).is_file():
            preserve_file(consumer / name, destination / Path(name).name)
    if failure is not None:
        previous = destination / 'result.json'
        if previous.is_symlink():
            raise RuntimeError('Refusing symlinked consumer result: ' + str(previous))
        if previous.exists():
            index = 0
            while (destination / f'result-before-failure-{index}.json').exists():
                index += 1
            previous.rename(destination / f'result-before-failure-{index}.json')
        for name in ['build.gradle.kts', 'gradle.properties']:
            if (consumer / name).is_file():
                preserve_file(consumer / name, destination / name)
        (destination / 'failure.json').write_text(json.dumps({
            'result': 'failed',
            'exception': type(failure).__name__,
            'returncode': getattr(failure, 'returncode', None),
            'artifact_preservation': preservation,
        }, indent=2) + '\n')
    return preservation
