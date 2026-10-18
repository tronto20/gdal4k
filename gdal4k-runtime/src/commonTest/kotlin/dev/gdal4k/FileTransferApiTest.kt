package dev.gdal4k

import dev.gdal4k.GdalFile
import dev.gdal4k.GdalFiles
import dev.gdal4k.runtime.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class FileTransferApiTest {
    @Test fun memoryFileRoundTripPreservesEveryByteAndDetachedResult() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime(); val path = "/vsimem/file-transfer-all-bytes.bin"
        val input = ByteArray(524305) { (it and 255).toByte() }
        try {
            runtime.writeMemoryFile(path, input)
            val first = runtime.readMemoryFile(path)
            assertContentEquals(input, first)
            first[0] = 99
            assertContentEquals(input, runtime.readMemoryFile(path))
            runtime.deleteMemoryFile(path)
            assertEquals(99, first[0].toInt())
            assertFails { runtime.readMemoryFile(path) }
        } finally { runtime.deleteMemoryFile(path) }
    }

    @Test fun emptyMemoryFileReplacesLongerContents() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime(); val path = "/vsimem/file-transfer-empty.bin"
        try {
            runtime.writeMemoryFile(path, byteArrayOf(1, -1, 0))
            runtime.writeMemoryFile(path, ByteArray(0))
            assertContentEquals(ByteArray(0), runtime.readMemoryFile(path))
        } finally { runtime.deleteMemoryFile(path) }
    }

    private suspend fun raster(block: suspend (Dataset, ByteArray) -> Unit) {
        val runtime = Gdal4k.runtime()
        requireNotNull(runtime.getDriver("MEM")).use { driver ->
            requireNotNull(driver.create("", 7, 5, 1, 1)).use { dataset ->
                val bytes = ByteArray(35) { (it * 7).toByte() }
                requireNotNull(dataset.getRasterBand(1)).use { assertEquals(0, it.writeRaster(bytes)) }
                block(dataset, bytes)
            }
        }
    }

    private suspend fun verify(dataset: Dataset, bytes: ByteArray) {
        assertEquals(7, dataset.getRasterXSize()); assertEquals(5, dataset.getRasterYSize())
        requireNotNull(dataset.getRasterBand(1)).use {
            val result = ByteArray(bytes.size); assertEquals(0, it.readRaster(result)); assertContentEquals(bytes, result)
        }
    }

    @Test fun fileBundleInputsStayIsolatedAndOutputsOutliveTheirSource() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime(); var retained: GdalFiles? = null
        raster { source, pixels ->
            val files = source.translate("GTiff", "사진.tif", emptyList())
            val original = files.mainFile().bytes.copyOf()
            runtime.openDataset(files, DatasetOpenOptions(accessMode = DatasetAccessMode.Update)).use { first ->
                runtime.openDataset(files).use { second ->
                    requireNotNull(first.getRasterBand(1)).use { assertEquals(0, it.fill(11.0)) }
                    assertEquals(0, first.flushCache()); verify(second, pixels)
                    retained = second.translate("GTiff", "retained.tif", emptyList())
                }
            }
            assertContentEquals(original, files.mainFile().bytes)
        }
        runtime.openDataset(requireNotNull(retained)).use { verify(it, ByteArray(35) { (it * 7).toByte() }) }
    }

    @Test fun shapefileSidecarsAndUtf8FieldsRemainPortableAfterClose() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        val input = GdalFiles(GdalFile("input.geojson", """{"type":"FeatureCollection","features":[
            {"type":"Feature","properties":{"label":"한글 😀"},"geometry":{"type":"Point","coordinates":[12,45]}}
        ]}""".encodeToByteArray()))
        val output = runtime.openDataset(input).use {
            it.translateVector("ESRI Shapefile", "records.shp", listOf("-lco", "ENCODING=UTF-8"))
        }
        assertTrue(output.files.map { it.name }.containsAll(listOf("records.shp", "records.shx", "records.dbf")))
        runtime.openDataset(output).use { dataset ->
            requireNotNull(dataset.getLayer(0)).use { layer ->
                assertEquals(1L, layer.getFeatureCount())
                requireNotNull(layer.getNextFeature()).use { feature ->
                    assertEquals("한글 😀", feature.getFieldAsString(0))
                    requireNotNull(feature.getGeometryRef()).use { assertContentEquals(doubleArrayOf(12.0, 45.0, 0.0), it.getPoint(0)) }
                }
            }
        }
    }

    @Test fun directOutputKeepsRandomAccessAndCallerOwnership() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime(); val path = datasetOutputPath("file-transfer-direct.tif")
        requireNotNull(runtime.getDriver("GTiff")).use { driver ->
            try {
                raster { source, pixels ->
                    source.translateTo(path, "GTiff", listOf("-co", "TILED=YES"))
                    runtime.openDataset(path).use { verify(it, pixels) }
                }
                // Closing the source leaves the caller's output available.
                runtime.openDataset(path, DatasetOpenOptions(accessMode = DatasetAccessMode.Update)).use { dataset ->
                    requireNotNull(dataset.getRasterBand(1)).use { assertEquals(0, it.fill(17.0)) }
                    assertEquals(0, dataset.flushCache())
                }
                runtime.openDataset(path).use { verify(it, ByteArray(35) { 17 }) }
            } finally { driver.delete(path) }
        }
    }

    @Test fun cancellationDoesNotPublishReadResultsAndRuntimeCanRecover() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime(); val path = "/vsimem/file-transfer-cancel.bin"
        val bytes = ByteArray(524305) { (it * 13).toByte() }
        try {
            runtime.writeMemoryFile(path, bytes)
            assertFailsWith<CancellationException> { withGdalProgress({ false }) { runtime.readMemoryFile(path) } }
            assertContentEquals(bytes, runtime.readMemoryFile(path))
            assertFailsWith<CancellationException> { withGdalProgress({ false }) { runtime.writeMemoryFile(path, bytes) } }
            // Explicit paths own any partial overwrite on cancellation; do not assume rollback.
            runtime.writeMemoryFile(path, bytes); assertContentEquals(bytes, runtime.readMemoryFile(path))
        } finally { runtime.deleteMemoryFile(path) }
    }

    @Test fun invalidInputAndConversionFailureDoNotPoisonNextWorkspace() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        assertFails { runtime.openDataset(GdalFiles(GdalFile("bad.tif", byteArrayOf(0, 1, -1)))) }
        assertFails { runtime.writeMemoryFile("not-memory.bin", byteArrayOf(1)) }
        raster { source, pixels ->
            assertFails { source.translate("driver-that-does-not-exist", "bad.bin", emptyList()) }
            val files = source.translate("GTiff", "valid.tif", emptyList())
            runtime.openDataset(files).use { verify(it, pixels) }
        }
    }
}
