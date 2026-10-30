#!/usr/bin/env python3
"""Verify published Maven artifacts from a fresh, unrelated Kotlin consumer build."""
import argparse
import importlib.util
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from jvm_binaries import JVM_BINARIES, host_binary
from publication_versions import ENGINE_TARGETS, library_version_from_repo
from consumer_evidence import preserve_consumer_evidence, consumer_workspace, PhaseRecorder, consumer_failure_guard, restore_hidden_paths
from command_evidence import run_with_evidence
from jvm_hang_diagnostics import prepare_jvm_hang_diagnostics

ROOT=Path(__file__).resolve().parent.parent
p=argparse.ArgumentParser()
p.add_argument('--target', required=True, choices=['jvm','js','wasmJs','macosArm64','linuxX64','linuxArm64','mingwX64','iosArm64','iosSimulatorArm64','android'])
p.add_argument('--command-timeout-seconds', type=int, help='Bound the consumer Gradle command and retain its output and XML on timeout')
p.add_argument('--completion-deadline-monotonic', type=float, help='Internal absolute evidence/cleanup deadline inside the outer CI command bound')
p.add_argument('--windows-native-consumer-diagnostics', action='store_true')
p.add_argument('--native-diagnostic-budget-bytes', type=int)
p.add_argument('--repo',type=Path,default=ROOT/'verification/build/consumer-maven')
p.add_argument('--no-publish',action='store_true')
p.add_argument('--compile-only',action='store_true',help='Link Native tests or compile Android device tests without running them')
p.add_argument('--browser',action='store_true')
p.add_argument('--ios-device')
p.add_argument('--jvm-binary', action='append', choices=JVM_BINARIES, help='Explicit payload; repeat to verify a multi-OS application')
p.add_argument('--expect-missing-jvm-binary', action='store_true', help='Verify actionable failure in a fresh JVM without the host payload')
a=p.parse_args()
if a.command_timeout_seconds is not None and a.command_timeout_seconds <= 0:p.error('--command-timeout-seconds must be positive')
if a.completion_deadline_monotonic is not None and (not math.isfinite(a.completion_deadline_monotonic) or a.completion_deadline_monotonic <= time.monotonic() or a.command_timeout_seconds is None):p.error('Completion deadline must be finite, future and bounded')
if a.windows_native_consumer_diagnostics:
 if os.name != 'nt' or a.target != 'mingwX64' or a.compile_only or a.command_timeout_seconds is None or a.completion_deadline_monotonic is None:p.error('Native diagnostics require an actual bounded Windows mingwX64 consumer')
 from native_consumer_diagnostics import valid_budget
 try:valid_budget(a.native_diagnostic_budget_bytes)
 except ValueError as error:p.error(str(error))
elif a.native_diagnostic_budget_bytes is not None:p.error('Native diagnostic budget requires its explicit opt-in')
if (a.jvm_binary or a.expect_missing_jvm_binary) and a.target != 'jvm': p.error('JVM binary options require --target jvm')
binaries = a.jvm_binary or ([] if a.expect_missing_jvm_binary or a.target != 'jvm' else [host_binary()])
if a.expect_missing_jvm_binary and host_binary() in binaries: p.error('The missing-binary test cannot include the host payload')
repo=a.repo.resolve(); target=a.target; title=target[0].upper()+target[1:]
gradle=ROOT/('gradlew.bat' if os.name=='nt' else 'gradlew')
def run(args,**kwargs):subprocess.run([str(x) for x in args],check=True,**kwargs)
if not a.no_publish:
 tasks=[]
 modules=['gdal4k','gdal4k-runtime']
 for module in modules:
  tasks += [f':{module}:publishKotlinMultiplatformPublicationToMavenLocal',f':{module}:publish{title}PublicationToMavenLocal']
 tasks += [':gdal4k-engine:publishKotlinMultiplatformPublicationToMavenLocal']
 if target.lower().replace('wasmjs','wasm-js') in ENGINE_TARGETS:tasks += [f':gdal4k-engine:publish{title}PublicationToMavenLocal']
 if target=='jvm' and binaries:tasks += ['-Pgdal4kJvmClassifiers='+','.join(binaries)]
 if target=='android':tasks+=[':gdal4k-android-runtime:publishAndroidRuntimePublicationToMavenLocal']
 run([gradle,'--console=plain',f'-Dmaven.repo.local={repo}',*tasks],cwd=ROOT)
VERSION=library_version_from_repo(repo)
source='''package consumer

import dev.gdal4k.Gdal4k
import dev.gdal4k.runtime.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class PublishedConsumerTest {
    @Test fun defaultRuntimeAndJxl() = runTest(timeout = 120.seconds) {
        try {
            val runtime = Gdal4k.runtime()
            assertEquals("3120400", runtime.versionInfo("VERSION_NUM"))
            assertTrue(runtime.versionInfo("THIRD_PARTY_LICENSES").contains("JPEG"))
            val expected = byteArrayOf(0, 1, 127, -1)
            requireNotNull(runtime.getDriver("MEM")).use { driver ->
                requireNotNull(driver.create("", 2, 2, 1, GdalDataType.Byte)).use { source ->
                    assertEquals(0, requireNotNull(source.getRasterBand(1)).writeRaster(expected))
                    runtime.createSpatialReference("EPSG:4326").use { crs ->
                        assertEquals(0, source.setProjection(crs.exportToWkt()))
                    }
                    assertEquals(0, source.setGeoTransform(doubleArrayOf(100.0,0.5,0.0,50.0,0.0,-0.5)))
                    // Convenience conversion works on the original Dataset, not a second wrapper.
                    val converted = source.translate("COG", "unified.tif", listOf("-co", "COMPRESS=JXL", "-co", "JXL_LOSSLESS=YES"))
                    runtime.openDataset("unified.tif", converted.mainFile().bytes).use { reopened ->
                        assertContentEquals(doubleArrayOf(0.0, 1.0, 127.0, 255.0), reopened.readRaster())
                        val actual = ByteArray(4)
                        requireNotNull(reopened.getRasterBand(1)).use { band ->
                            assertEquals(0, band.readRaster(actual))
                        }
                        assertContentEquals(expected, actual)
                    }
                    for (format in listOf("JPEGXL", "GTiff", "COG")) {
                        val path = "/vsimem/consumer-" + format + if(format=="JPEGXL") ".jxl" else ".tif"
                        val options = if(format=="JPEGXL") listOf("LOSSLESS=YES") else listOf("COMPRESS=JXL", "JXL_LOSSLESS=YES")
                        requireNotNull(runtime.getDriver(format)).use { outputDriver ->
                            requireNotNull(outputDriver.createCopy(path, source, options=options)).close()
                        }
                        runtime.openDataset(path).use { reopened ->
                            val actual=ByteArray(4)
                            assertEquals(0,requireNotNull(reopened.getRasterBand(1)).readRaster(actual))
                            assertContentEquals(expected, actual)
                            if(format!="JPEGXL") assertTrue(runtime.datasetInfo(reopened,DatasetInfoOptions(listOf("-json"))).contains("JXL"))
                        }
                        runtime.deleteMemoryFile(path)
                    }
                }
            }
        } finally { Gdal4k.close() }
        println("GDAL4K_PUBLISHED_CONSUMER_OK")
    }
}
'''
if a.expect_missing_jvm_binary:
 source = '''package consumer
import dev.gdal4k.Gdal4k
import kotlinx.coroutines.test.runTest
import kotlin.test.*
class PublishedConsumerTest {
    @Test fun missingBinaryIsActionable() = runTest {
        repeat(2) {
            val failure = assertFailsWith<IllegalStateException> { Gdal4k.runtime() }
            assertTrue(failure.message.orEmpty().contains("dev.tronto.gdal4k:gdal4k-engine:<gdal4k-version>:EXPECTED_BINARY"), failure.message)
            assertTrue(failure.message.orEmpty().contains("No JVM binary is included by default"), failure.message)
        }
        Gdal4k.close()
    }
}
'''.replace('EXPECTED_BINARY', host_binary())
case = '-compile' if target=='android' and a.compile_only else '-missing' if a.expect_missing_jvm_binary and not binaries else '-wrong' if a.expect_missing_jvm_binary else '-multiple' if len(binaries)>1 else ''
destination=ROOT/'verification/results'/('published-consumer-'+target+('-browser' if a.browser else '')+case)
phases=PhaseRecorder(destination/'retention', max_total_bytes=(960*1024 if a.windows_native_consumer_diagnostics else None))
with consumer_failure_guard(destination,phase_recorder=phases), consumer_workspace(ROOT, phase_recorder=phases, deadline=a.completion_deadline_monotonic) as temporary:
 consumer=Path(temporary)
 (consumer/'settings.gradle.kts').write_text('pluginManagement { repositories { mavenCentral(); google(); gradlePluginPortal() } }\nrootProject.name="published-gdal4k-consumer"\n')
 android='id("com.android.kotlin.multiplatform.library") version "8.11.2"' if target=='android' else ''
 if target=='jvm':declaration='jvm()';task='jvmTest'
 elif target in ['js','wasmJs']:
  declaration=target+' { nodejs { testTask { useMocha { timeout="180s" } } }; browser { testTask { useKarma { useChromeHeadless() } } } }'
  task=target+('BrowserTest' if a.browser else 'NodeTest')
 elif target=='android':
  declaration='''androidLibrary { namespace="consumer.gdal4k"; compileSdk=35; minSdk=28
withDeviceTestBuilder { sourceSetTreeName="test" }.configure { instrumentationRunner="androidx.test.runner.AndroidJUnitRunner" } }'''
  task='compileAndroidDeviceTest' if a.compile_only else 'connectedAndroidDeviceTest'
 else:
  declaration=target+'()'
  if target == 'iosSimulatorArm64' and a.ios_device:
   declaration=target+' { testRuns["test"].deviceId = '+json.dumps(a.ios_device)+' }'
  task='linkDebugTest'+title if a.compile_only else target+'Test'
 extra='getByName("androidDeviceTest").dependencies { implementation("androidx.test:runner:1.6.2") }\n' if target=='android' else ''
 build='''plugins { kotlin("multiplatform") version "2.4.21"; '''+android+''' }
repositories {
    exclusiveContent { forRepository { maven { url=uri('''+json.dumps(repo.as_uri())+''') } }; filter { includeGroup("dev.tronto.gdal4k") } }
    google(); mavenCentral()
}
kotlin {
    jvmToolchain(21)
    '''+declaration+'''
    sourceSets {
        commonMain.dependencies { implementation("dev.tronto.gdal4k:gdal4k-runtime:'''+VERSION+'''"); implementation("dev.tronto.gdal4k:gdal4k-engine:'''+VERSION+'''") }
        commonTest.dependencies { implementation(kotlin("test")); implementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.10.2") }
        '''+extra+'''
    }
}
'''
 if target=='jvm':
  hang_diagnostics = prepare_jvm_hang_diagnostics(consumer)
  build = 'import java.time.Duration\n' + build
  build += '\nkotlin.sourceSets.named("jvmMain") { dependencies {\n' + ''.join('    runtimeOnly("dev.tronto.gdal4k:gdal4k-engine:'+VERSION+':'+b+'")\n' for b in binaries) + '} }\n'
  build += '''
val recordJvmRuntime = tasks.register("recordJvmRuntime") {
    val runtimeFiles = configurations.named("jvmTestRuntimeClasspath")
    inputs.files(runtimeFiles)
    val destination = layout.buildDirectory.file("jvm-runtime-files.txt")
    val coordinates = layout.buildDirectory.file("jvm-runtime-coordinates.txt")
    outputs.files(destination, coordinates)
    doLast {
        destination.get().asFile.apply { parentFile.mkdirs(); writeText(runtimeFiles.get().files.joinToString("\\n") { it.name }) }
        val modules = runtimeFiles.get().incoming.artifacts.artifacts.mapNotNull {
            (it.id.componentIdentifier as? org.gradle.api.artifacts.component.ModuleComponentIdentifier)?.let { id -> "${id.group}:${id.module}:${id.version}" }
        }.distinct()
        coordinates.get().asFile.writeText(modules.joinToString("\\n"))
    }
}
tasks.named("jvmTest") { dependsOn(recordJvmRuntime) }
tasks.withType<org.gradle.api.tasks.testing.Test>().configureEach {
    jvmArgs(''' + json.dumps(hang_diagnostics['jvm_arg']).replace('$', '\\$') + ''')
    // A stuck test worker must fail before the outer CI job kills Python and
    // discards its temporary test results. This is a functional hang bound.
    timeout.set(Duration.ofMinutes(8))
    testLogging {
        events("started", "passed", "failed", "skipped")
        showStandardStreams = true
    }
}
'''
 (consumer/'build.gradle.kts').write_text(build)
 (consumer/'gradle.properties').write_text('org.gradle.jvmargs=-Xmx2g\nandroid.useAndroidX=true\n')
 path=consumer/'src/commonTest/kotlin/consumer/PublishedConsumerTest.kt';path.parent.mkdir(parents=True);path.write_text(source, encoding="utf-8")
 profile_fixture_sources = []
 fixture_spec = importlib.util.spec_from_file_location('driver_profile_runtime_fixture', ROOT/'verification/driver-profile/runtime-fixture.py')
 fixture_module = importlib.util.module_from_spec(fixture_spec);fixture_spec.loader.exec_module(fixture_module)
 if not a.expect_missing_jvm_binary:
  # Exercise the expanded API from published coordinates, not producer project dependencies.
  for test_name in ['DatasetApiTest', 'AdvancedDatasetApiTest', 'MDArrayMetadataApiTest', 'MDArrayTransposeApiTest', 'BandApiTest', 'BandStructuresTest', 'MosaicApiTest', 'RasterizeApiTest', 'DemProcessingApiTest', 'PolygonizeApiTest', 'FootprintApiTest', 'TileIndexApiTest', 'MultiDimTranslateApiTest', 'OgrVectorApiTest', 'OgrSchemaApiTest', 'RasterAnalysisApiTest', 'GridViewshedApiTest', 'RasterMathApiTest', 'OgrLayerApiTest', 'GdalProgressApiTest', 'HttpInputPolicyTest', 'BinaryRasterApiTest', 'RasterWindowsApiTest', 'CoordinateArraysApiTest', 'VectorColumnsApiTest', 'FileTransferApiTest', 'MDTransferApiTest']:
   expanded = (ROOT/f'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/{test_name}.kt').read_text(encoding="utf-8")
   expanded = expanded.replace('package dev.gdal4k\n','package consumer\nimport dev.gdal4k.Gdal4k\n')
   expanded_path = consumer/f'src/commonTest/kotlin/consumer/{test_name}.kt'
   expanded_path.write_text(expanded, encoding="utf-8")
  test_set = 'androidDeviceTest' if target == 'android' else target+'Test' if target in ['jvm','js','wasmJs'] else 'nativeTest'
  if target == 'jvm':
   local_paths = (ROOT/'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/TileIndexLocalPathsTest.kt').read_text(encoding='utf-8')
   local_destination = consumer/'src/jvmTest/kotlin/consumer/TileIndexLocalPathsTest.kt'
   local_destination.parent.mkdir(parents=True, exist_ok=True)
   local_destination.write_text(local_paths.replace('package dev.gdal4k\n', 'package consumer\nimport dev.gdal4k.Gdal4k\n'), encoding='utf-8')
   md_paths = (ROOT/'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/MultiDimTranslateLocalPathsTest.kt').read_text(encoding='utf-8')
   md_destination = consumer/'src/jvmTest/kotlin/consumer/MultiDimTranslateLocalPathsTest.kt'
   md_destination.write_text(md_paths.replace('package dev.gdal4k\n', 'package consumer\nimport dev.gdal4k.Gdal4k\n'), encoding='utf-8')
  platform_path = ROOT/f'gdal4k-runtime/src/{test_set}/kotlin/dev/gdal4k/DatasetOutputPath.kt'
  actual_set = test_set if target in ['android','jvm','js','wasmJs'] else target+'Test'
  actual_path = consumer/f'src/{actual_set}/kotlin/consumer/DatasetOutputPath.kt'
  actual_path.parent.mkdir(parents=True,exist_ok=True)
  actual_path.write_text(platform_path.read_text(encoding="utf-8").replace('package dev.gdal4k','package consumer'), encoding="utf-8")
  profile_fixture_sources = fixture_module.copy_to_consumer(ROOT, consumer, target)

 if a.browser and target in ['js','wasmJs']:
  browser_source = (ROOT/f'gdal4k-runtime/src/{target}Test/kotlin/dev/gdal4k/BrowserFileTest.kt').read_text(encoding="utf-8")
  browser_source = browser_source.replace('package dev.gdal4k\n', 'package consumer\nimport dev.gdal4k.Gdal4k\n')
  browser_test = consumer/f'src/{target}Test/kotlin/consumer/BrowserFileTest.kt'
  browser_test.parent.mkdir(parents=True,exist_ok=True);browser_test.write_text(browser_source, encoding="utf-8")
 config=consumer/'karma.config.d/timeout.js';config.parent.mkdir();config.write_text('config.browserNoActivityTimeout=240000; config.client.mocha={...config.client.mocha,timeout:180000};\n')
 if target=='android' and (ROOT/'local.properties').exists():shutil.copy2(ROOT/'local.properties',consumer/'local.properties')
 # The native consumer must link the archive stored inside its published KLIB.
 platform={'macosArm64':'macos-arm64','linuxX64':'linux-amd64','linuxArm64':'linux-arm64','mingwX64':'windows-amd64','iosArm64':'ios-arm64','iosSimulatorArm64':'ios-simulator-arm64'}.get(target)
 hidden=[]
 fixture_module.preserve_consumer_sources(consumer, destination, profile_fixture_sources)
 native_directory=None; command_env=None; native_callback_check=None
 if a.windows_native_consumer_diagnostics:
  from native_consumer_diagnostics import write_record, prepare_native_diagnostics
  native_directory=phases.directory/'native';native_directory.mkdir()
  source_bindings=[]
  for path in [consumer/'settings.gradle.kts',consumer/'build.gradle.kts',consumer/'gradle.properties',*sorted((consumer/'src').rglob('*.kt'))]:
   raw=path.read_bytes();source_bindings.append({'path':str(path.relative_to(consumer)),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
  for name in ['native_consumer_diagnostics.py','windows_dump_quota.py','windows_native_stall.py','windows_process.py','consumer.py','consumer_evidence.py','command_evidence.py','ci-diagnostics.init.gradle']:
   path=ROOT/'verification'/name;raw=path.read_bytes()
   source_bindings.append({'path':'verification/'+name,'sourceScope':'verification-helper','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
  write_record(native_directory/'context.json',{'consumerRoot':str(consumer.resolve()),'evidenceRoot':str(destination.resolve()),'budgetBytes':a.native_diagnostic_budget_bytes,'completionDeadlineMonotonic':a.completion_deadline_monotonic,'sourceBindings':source_bindings})
  native_callback_check=prepare_native_diagnostics(native_directory,budget=a.native_diagnostic_budget_bytes,deadline=a.completion_deadline_monotonic)
  command_env=dict(os.environ,GDAL4K_NATIVE_DIAGNOSTIC_DIR=str(native_directory.resolve()),GDAL4K_NATIVE_DIAGNOSTIC_PYTHON=sys.executable,GDAL4K_NATIVE_DIAGNOSTIC_HELPER=str(ROOT/'verification/native_consumer_diagnostics.py'))
 try:
  if platform:
   for original in [ROOT/f'gdal4k-engine/build/engine/{platform}',ROOT/f'gdal4k-engine/build/classes/kotlin/{target}/main/cinterop',ROOT/f'gdal4k-runtime/build/classes/kotlin/{target}/main/cinterop']:
    if original.exists():
     moved=original.with_name(original.name+'-consumer-hidden')
     if moved.exists():raise RuntimeError('Previous hidden producer files must be restored: '+str(moved))
     phases.record('producer-hide','before',path=str(original))
     original.rename(moved);hidden.append((original,moved))
     phases.record('producer-hide','after',path=str(original))
  try:
   diagnostic_options = ['--no-daemon', '--init-script', ROOT/'verification/ci-diagnostics.init.gradle'] if a.command_timeout_seconds is not None else []
   command = [gradle,'-p',consumer,'--console=plain',*diagnostic_options,'--refresh-dependencies',task]
   if a.command_timeout_seconds is not None:
    run_with_evidence(command, cwd=consumer, directory=destination/'commands', timeout=a.command_timeout_seconds,
                      env=command_env, windows_native_diagnostics=native_directory,
                      native_diagnostic_budget=a.native_diagnostic_budget_bytes,
                      completion_deadline=a.completion_deadline_monotonic,
                      native_callback_self_check=native_callback_check)
   else:
    run(command,cwd=consumer)
  finally:
   preservation=preserve_consumer_evidence(consumer,destination,sys.exc_info()[1],
                                        deadline=a.completion_deadline_monotonic,phase_recorder=phases)
  runtime_files = []
  runtime_coordinates = []
  if target=='jvm':
   runtime_files = (consumer/'build/jvm-runtime-files.txt').read_text(encoding="utf-8").splitlines()
   runtime_coordinates = (consumer/'build/jvm-runtime-coordinates.txt').read_text(encoding="utf-8").splitlines()
   # Snapshot filenames may be timestamped; classifiers are stable suffixes.
   engine_files = [name for name in runtime_files if name.startswith('gdal4k-engine-') and name.endswith('.jar')]
   actual_payloads = {b for b in JVM_BINARIES if any(name.endswith('-'+b+'.jar') for name in engine_files)}
   if actual_payloads != set(binaries) or len(engine_files) != len(binaries):
    raise RuntimeError(f'Unexpected JVM classifier files: {engine_files}; expected {binaries}')
  consumer_result={'target':target,'version':VERSION,'browser':a.browser,'compiled_only':a.compile_only,'producer_paths_hidden':bool(hidden),'producer_paths_hidden_count':len(hidden),'producer_paths_hidden_names':[str(original.relative_to(ROOT)) for original,_ in hidden],'artifact_preservation':preservation,'profile_runtime_fixture_sources':profile_fixture_sources,'jvm_binaries':binaries,'runtime_files':runtime_files,'runtime_coordinates':runtime_coordinates,'expected_missing_binary':a.expect_missing_jvm_binary,'result':'passed', **({'jvm_hang_diagnostics': {'interval_millis':hang_diagnostics['interval_millis'], 'retained_directory':'test-results/jvm-hang-diagnostics'}} if target=='jvm' else {})}
 finally:
  restore_hidden_paths(hidden,phase_recorder=phases,failure=sys.exc_info()[1])
with consumer_failure_guard(destination,phase_recorder=phases):
 (destination/'result.json').write_text(json.dumps(consumer_result,indent=2)+'\n')
