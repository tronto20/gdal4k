package dev.gdal4k

import dev.gdal4k.runtime.*
import dev.gdal4k.GdalFile
import dev.gdal4k.GdalFiles
import dev.gdal4k.GdalException
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class GdalProgressApiTest {
    @Test fun rasterRequestsLargerThanCStackStayOnHeap() = runTest(timeout = 120.seconds) {
        val r = Gdal4k.runtime()
        requireNotNull(r.getDriver("MEM")).use { driver ->
            requireNotNull(driver.create("", 640, 640, 1, GdalDataType.Byte)).use { source ->
                // Decimal byte JSON exceeds the 1MiB C stack; there is no arbitrary input cap.
                val expected = ByteArray(640 * 640) { (it % 251).toByte() }
                assertEquals(0, source.writeRaster(expected))
                val actual = ByteArray(expected.size); assertEquals(0, source.readRaster(actual))
                assertContentEquals(expected, actual)
                val files = source.translate(driver = "GTiff")
                // The memory-open request also exceeds that stack independently of raster I/O.
                val large = GdalFiles(files.main, files.files + GdalFile("padding.bin", ByteArray(1024 * 1024)))
                r.openDataset(large).use { assertEquals(640, it.getRasterXSize()); assertEquals(640, it.getRasterYSize()) }
            }
        }
    }

    private suspend fun raster(): Dataset {
        val r = Gdal4k.runtime()
        return requireNotNull(r.getDriver("MEM")).use { driver ->
            requireNotNull(driver.create("", 128, 128, 1, GdalDataType.Byte)).also { ds ->
                assertEquals(0, ds.writeRaster(ByteArray(128 * 128) { (it % 251).toByte() }))
                assertEquals(0, ds.setGeoTransform(doubleArrayOf(0.0, 1.0, 0.0, 128.0, 0.0, -1.0)))
                Gdal4k.runtime().createSpatialReference("EPSG:3857").use { assertEquals(0, ds.setSpatialRef(it)) }
            }
        }
    }

    @Test fun actualTranslateProgressAndOutputStayUsable() = runTest(timeout = 120.seconds) {
        val events = mutableListOf<GdalProgress>()
        raster().use { source ->
            val files = withGdalProgress({ events += it; true }) {
                source.translate("-outsize", "256", "256", driver = "GTiff", name = "progress.tif")
            }
            assertTrue(events.isNotEmpty()); assertTrue(events.all { it.operation == "Dataset.translate" && it.fraction in 0.0..1.0 })
            assertEquals(1.0, events.last().fraction)
            Gdal4k.runtime().openDataset(files).use { assertEquals(256, it.getRasterXSize()) }
            assertEquals(128, source.getRasterXSize())
        }
    }

    @Test fun observerCancellationWaitsForCleanupAndRuntimeCanContinue() = runTest(timeout = 120.seconds) {
        raster().use { source ->
            assertFailsWith<CancellationException> {
                withGdalProgress({ false }) { source.warp("EPSG:3857") }
            }
            assertEquals(128, source.getRasterYSize())
            val files = source.translate(driver = "GTiff")
            Gdal4k.runtime().openDataset(files).use { assertEquals(128, it.getRasterXSize()) }
        }
    }

    @Test fun observerErrorIsPreservedInsteadOfNativeUserInterrupt() = runTest(timeout = 120.seconds) {
        val expected = IllegalArgumentException("observer failure")
        raster().use { source ->
            val error = assertFailsWith<IllegalArgumentException> {
                withGdalProgress({ throw expected }) { source.translate(driver = "GTiff") }
            }
            assertTrue(error === expected || error.cause === expected) // Coroutine stack-trace recovery may copy JVM exceptions.
            assertEquals(128, source.getRasterXSize())
        }
    }

    @Test fun coroutineCancellationRequestsNativeStopAndKeepsCallerInputs() = runTest(timeout = 120.seconds) {
        raster().use { source ->
            val task = async {
                val job = currentCoroutineContext().job
                withGdalProgress({ job.cancel(); true }) { source.translate("-outsize", "512", "512", driver = "GTiff", name = "cancel.tif") }
            }
            assertFailsWith<CancellationException> { task.await() }
            assertEquals(128, source.getRasterXSize())
            assertTrue(requireNotNull(source.getRasterBand(1)).use { it.checksum() } > 0)
        }
    }

    @Test fun lateCreateCopyCancellationDoesNotPoisonSubsequentCalls() = runTest(timeout = 120.seconds) {
        raster().use { source ->
            requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
                assertFailsWith<CancellationException> {
                    withGdalProgress({ it.fraction < 1.0 }) { driver.createCopy("", source) }
                }
                requireNotNull(driver.createCopy("", source)).use { copy ->
                    assertEquals(128, copy.getRasterXSize())
                    assertEquals(source.getRasterCount(), copy.getRasterCount())
                }
            }
        }
    }

    @Test fun statisticsHistogramsAndPolygonizeUseRealCallbacks() = runTest(timeout = 120.seconds) {
        raster().use { source ->
            requireNotNull(source.getRasterBand(1)).use { band ->
                val events = mutableListOf<GdalProgress>()
                val stats = withGdalProgress({ events += it; true }) { band.computeStatistics() }
                assertTrue(stats.maximum > stats.minimum); assertTrue(events.isNotEmpty())
                assertFailsWith<CancellationException> { withGdalProgress({ false }) { band.getHistogram(0.0, 256.0, 256) } }
                assertEquals(256, band.getHistogram(0.0, 256.0, 256).size)
            }
            assertFailsWith<CancellationException> { withGdalProgress({ false }) { source.polygonize() } }
            val files = source.polygonize()
            Gdal4k.runtime().openDataset(files).use { ds -> requireNotNull(ds.getLayer(0)).use { assertTrue(it.getFeatureCount(true) > 0) } }
        }
    }

    @Test fun nativeValidationFailureAndDirectPartialOutputOwnership() = runTest(timeout = 120.seconds) {
        raster().use { source ->
            assertFailsWith<GdalException> { withGdalProgress({ true }) { source.translate("-invalid-option", driver = "GTiff", name = "bad.tif") } }
            val path = datasetOutputPath("progress-cancel.tif")
            try {
                assertFailsWith<CancellationException> {
                    withGdalProgress({ it.fraction < 1.0 }) { source.translateTo(path, driver = "GTiff") }
                }
                // No automatic deletion/rollback is promised; overwrite the caller's partial path.
                source.translateTo(path, driver = "GTiff")
                Gdal4k.runtime().openDataset(path).use { assertEquals(128, it.getRasterXSize()) }
            } finally { requireNotNull(Gdal4k.runtime().getDriver("GTiff")).use { it.delete(path) } }
        }
    }
    @Test fun newBandAnalysisCancellationKeepsCallerBandsAndContourInputUsable() = runTest(timeout = 120.seconds) {
        raster().use { source -> raster().use { target ->
            requireNotNull(source.getRasterBand(1)).use { band -> requireNotNull(target.getRasterBand(1)).use { out ->
                val operations: List<Pair<String, suspend () -> Int>> = listOf(
                    "sieve" to { band.sieveTo(out, 2) }, "proximity" to { band.proximityTo(out) },
                    "fillNoData" to { band.fillNoData(maxSearchDistance = 2.0) })
                for ((name, operation) in operations) {
                    assertFailsWith<CancellationException> { withGdalProgress({ false }) { operation() } }
                    assertEquals(0, operation(), "$name after cancellation")
                    assertEquals(128, source.getRasterXSize()); assertEquals(128, target.getRasterYSize())
                }
            } }
            assertFailsWith<CancellationException> { withGdalProgress({ false }) { source.contour(interval = 100.0) } }
            Gdal4k.runtime().openDataset(source.contour(interval = 100.0)).use { ds ->
                requireNotNull(ds.getLayer(0)).use { assertTrue(it.getFeatureCount(true) > 0) }
            }
        } }
    }

    @Test fun gridAndViewshedCancellationReleaseOutputsAndKeepInputs() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        val json = """{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},"geometry":{"type":"Point","coordinates":[0,0,3]}}]}"""
        runtime.openDataset(GdalFiles(GdalFile("progress-points.geojson", json.encodeToByteArray()))).use { points ->
            val args = listOf("-a", "nearest", "-txe", "0", "2", "-tye", "0", "2", "-outsize", "2", "2")
            assertFailsWith<CancellationException> { withGdalProgress({ false }) { points.grid(arguments = args) } }
            runtime.openDataset(points.grid(arguments = args)).use { assertEquals(2, it.getRasterXSize()) }
            requireNotNull(points.getLayer(0)).use { assertEquals(1L, it.getFeatureCount(true)) }
        }
        raster().use { source ->
            val options = ViewshedOptions(64.5, 64.5, curvatureCoefficient = 0.0)
            assertFailsWith<CancellationException> { withGdalProgress({ false }) { source.viewshed(options) } }
            runtime.openDataset(source.viewshed(options)).use { assertEquals(128, it.getRasterYSize()) }
            assertEquals(128, source.getRasterXSize())
        }
    }

    @Test fun overlayCancellationPreservesInputHandlesAndAllowsFreshOutput() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        requireNotNull(runtime.getDriver("MEM")).use { driver -> requireNotNull(driver.createVector("")).use { ds ->
            suspend fun layer(name: String, x: Int?): Layer {
                val layer = requireNotNull(ds.createLayer(name, geometryType = 0))
                if (x != null) requireNotNull(layer.getLayerDefn()).use { schema -> runtime.createFeature(schema).use { feature ->
                    runtime.createGeometryFromWkt("POLYGON (($x 0,${x+2} 0,${x+2} 2,$x 2,$x 0))").use { feature.setGeometry(it) }
                    assertEquals(0, layer.createFeature(feature))
                } }
                return layer
            }
            layer("input", 0).use { input -> layer("method", 1).use { method ->
                for (operation in LayerOverlayOperation.entries) {
                    layer("cancel-${operation.name}", null).use { output ->
                        assertFailsWith<CancellationException> { withGdalProgress({ false }) { input.overlayTo(method, output, operation) } }
                        // Cancellation does not roll back schema/cursor/partial output: use a fresh output.
                    }
                    layer("ok-${operation.name}", null).use { output -> assertEquals(0, input.overlayTo(method, output, operation)) }
                    assertEquals(1L, input.getFeatureCount(true)); assertEquals(1L, method.getFeatureCount(true))
                }
            } }
        } }
    }

}
