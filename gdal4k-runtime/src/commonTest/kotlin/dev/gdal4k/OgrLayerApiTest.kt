package dev.gdal4k

import dev.gdal4k.GdalFile
import dev.gdal4k.GdalFiles
import dev.gdal4k.runtime.*
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.job
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.yield
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class OgrLayerApiTest {
    private suspend fun <T> vector(block: suspend (Dataset) -> T): T =
        requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver -> requireNotNull(driver.createVector("")).use { block(it) } }
    private suspend fun layer(ds: Dataset, name: String, wkt: String? = null, value: Int = 1, srs: SpatialReference? = null): Layer {
        val result = requireNotNull(ds.createLayer(name, srs, geometryType = 0))
        if (wkt != null) {
            Gdal4k.runtime().createFieldDefn("value", 0).use { assertEquals(0, result.createField(it)) }
            requireNotNull(result.getLayerDefn()).use { definition -> Gdal4k.runtime().createFeature(definition).use { feature ->
                feature.setField(0, value)
                Gdal4k.runtime().createGeometryFromWkt(wkt, srs).use { assertEquals(0, feature.setGeometry(it)) }
                assertEquals(0, result.createFeature(feature))
            } }
        }
        return result
    }
    private val first = "POLYGON ((0 0,2 0,2 2,0 2,0 0))"
    private val second = "POLYGON ((1 0,3 0,3 2,1 2,1 0))"
    private suspend fun area(layer: Layer): Double {
        var sum = 0.0; layer.forEachFeatureBatch(2) { batch -> for (f in batch) requireNotNull(f.getGeometryRef()).use { sum += it.getArea() } }; return sum
    }
    @Test fun aliasedInputCursorsAreRejectedBeforeChangingOutputOrIteration() = runTest(timeout = 120.seconds) { vector { ds ->
        layer(ds, "input", first).use { input -> layer(ds, "method", second).use { method -> layer(ds, "output").use { output ->
            requireNotNull(input.getLayerDefn()).use { schema ->
                repeat(3) { Gdal4k.runtime().createFeature(schema).use { feature ->
                    Gdal4k.runtime().createGeometryFromWkt(first).use { feature.setGeometry(it) }
                    assertEquals(0, input.createFeature(feature))
                } }
            }
            requireNotNull(ds.getLayer(0)).use { alias ->
                assertNotSame(input, alias)
                input.resetReading(); requireNotNull(input.getNextFeature()).close()
                requireNotNull(output.getLayerDefn()).use { definition ->
                    for (operation in LayerOverlayOperation.entries) {
                        val failure = assertFails { input.overlayTo(alias, output, operation) }
                        assertTrue(failure.message.orEmpty().contains("independent"))
                        assertEquals(0, definition.getFieldCount()); assertEquals(0, output.getFeatureCount(true))
                    }
                    requireNotNull(input.getNextFeature()).use { assertEquals(1L, it.getFID()) }
                }
            }
            assertEquals(0, input.overlayTo(method, output, LayerOverlayOperation.Intersection))
            assertEquals(4, output.getFeatureCount(true))
        } } }
    } }
    @Test fun allSevenLayerOverlaysHaveExpectedAreas() = runTest(timeout = 120.seconds) { vector { ds ->
        layer(ds, "input", first).use { input -> layer(ds, "method", second).use { method ->
            val expected = mapOf(LayerOverlayOperation.Intersection to 2.0, LayerOverlayOperation.Union to 6.0, LayerOverlayOperation.SymDifference to 4.0,
                LayerOverlayOperation.Identity to 4.0, LayerOverlayOperation.Update to 6.0, LayerOverlayOperation.Clip to 2.0, LayerOverlayOperation.Erase to 2.0)
            for ((operation, expectedArea) in expected) layer(ds, "out-${operation.name}").use { output ->
                assertEquals(0, input.overlayTo(method, output, operation, "PROMOTE_TO_MULTI=YES"))
                assertEquals(expectedArea, area(output), 0.000001)
            }
            assertEquals(4.0, area(input)); assertEquals(4.0, area(method))
        } }
    } }
    @Test fun overlayPreservesAttributesAndInvalidatesBorrowedOutputSchema() = runTest(timeout = 120.seconds) { vector { ds ->
        layer(ds, "input", first, 11).use { input -> layer(ds, "method", second, 22).use { method -> layer(ds, "output").use { output ->
            val oldDefinition = requireNotNull(output.getLayerDefn())
            assertEquals(0, input.overlayTo(method, output, LayerOverlayOperation.Intersection, "INPUT_PREFIX=in_", "METHOD_PREFIX=method_"))
            assertFails { oldDefinition.getFieldCount() }; oldDefinition.close()
            requireNotNull(output.getNextFeature()).use { feature ->
                assertEquals(11, feature.getFieldAsInteger("in_value")); assertEquals(22, feature.getFieldAsInteger("method_value"))
            }
            input.resetReading(); requireNotNull(input.getNextFeature()).use { assertEquals(11, it.getFieldAsInteger("value")) }
        } } }
    } }
    @Test fun overlayValidationAndLiveResultFeaturesAreSafe() = runTest(timeout = 120.seconds) { vector { ds ->
        layer(ds, "input", first).use { input -> layer(ds, "method", second).use { method -> layer(ds, "output").use { output ->
            requireNotNull(output.getLayerDefn()).use { definition ->
                assertFails { input.overlayTo(method, input, LayerOverlayOperation.Union) }
                assertFails { input.overlayTo(method, output, "missing", emptyList()) }
                assertFails { input.overlayTo(method, output, LayerOverlayOperation.Union, "UNKNOWN=YES") }
                assertEquals(0, definition.getFieldCount())
                Gdal4k.runtime().createFeature(definition).use { pending -> assertFails { input.overlayTo(method, output, LayerOverlayOperation.Intersection) }; assertEquals(-1L, pending.getFID()) }
            }
            output.setSpatialFilterRect(0.0, 0.0, 1.0, 1.0)
            assertFails { input.overlayTo(method, output, LayerOverlayOperation.Clip) }; output.setSpatialFilter(null)
            Gdal4k.runtime().createSpatialReference("EPSG:4326").use { a -> Gdal4k.runtime().createSpatialReference("EPSG:3857").use { b ->
                layer(ds, "a", first, srs = a).use { one -> layer(ds, "b", second, srs = b).use { two -> assertFails { one.overlayTo(two, output, LayerOverlayOperation.Intersection) } } }
            } }
            assertEquals(0, input.overlayTo(method, output, LayerOverlayOperation.Clip))
            assertFails { input.overlayTo(method, output, LayerOverlayOperation.Clip) }
            output.close(); assertFails { input.overlayTo(method, output, LayerOverlayOperation.Clip) }
        } } }
    } }
    @Test fun nativeOverlayFailureStillInvalidatesOutputSchema() = runTest(timeout = 120.seconds) { vector { ds ->
        layer(ds, "invalid", "POLYGON ((0 0,2 2,2 0,0 2,0 0))").use { input -> layer(ds, "method", second).use { method -> layer(ds, "output").use { output ->
            val borrowed = requireNotNull(output.getLayerDefn())
            assertNotEquals(0, input.overlayTo(method, output, LayerOverlayOperation.Intersection))
            assertFails { borrowed.getFieldCount() }; borrowed.close()
            assertEquals(1L, input.getFeatureCount())
        } } }
    } }
    @Test fun schemaMutationWaitsForBatchFeaturesAndRetainedClones() = runTest(timeout = 120.seconds) { vector { ds ->
        layer(ds, "schema-batch", first, 11).use { input ->
            var retained: Feature? = null
            input.forEachFeatureBatch(1) { features ->
                assertFails { input.deleteField(0) }
                retained = features.single().cloneFeature()
            }
            // Scoped batch features have closed, but their independent clone still shares the schema.
            val clone = requireNotNull(retained)
            assertFails { input.deleteField(0) }
            assertEquals(11, clone.getFieldAsInteger("value")); clone.close()
            val borrowed = requireNotNull(input.getLayerDefn())
            assertEquals(0, input.deleteField(0))
            assertFails { borrowed.getFieldCount() }; borrowed.close()
            requireNotNull(input.getLayerDefn()).use { assertEquals(0, it.getFieldCount()) }
            assertEquals(4.0, area(input))
        }
    } }
    private suspend fun <T> points(block: suspend (Layer) -> T): T {
        val features = (0..4).joinToString(",") { """{"type":"Feature","properties":{"value":$it},"geometry":{"type":"Point","coordinates":[$it,0]}}""" }
        val json = """{"type":"FeatureCollection","features":[$features]}"""
        return Gdal4k.runtime().openDataset(GdalFiles(GdalFile("batch.geojson", json.encodeToByteArray()))).use { ds -> requireNotNull(ds.getLayer(0)).use { block(it) } }
    }
    @Test fun nativeReadFailureIsNotReportedAsEndOfBatch() = runTest(timeout = 120.seconds) {
        val vrt = """<OGRVRTDataSource><OGRVRTLayer name="broken"><SrcDataSource relativeToVRT="0">/vsimem/gdal4k-nonexistent-batch-source.geojson</SrcDataSource><SrcLayer>missing</SrcLayer><GeometryType>wkbPoint</GeometryType></OGRVRTLayer></OGRVRTDataSource>"""
        Gdal4k.runtime().openDataset(GdalFiles(GdalFile("broken.vrt", vrt.encodeToByteArray())), DatasetOpenOptions(kind = DatasetKind.Vector)).use { ds ->
            requireNotNull(ds.getLayer(0)).use { layer -> assertFails { layer.readFeatureBatch(1) } }
        }
    }
    @Test fun pagingUsesCurrentFilteredCursor() = runTest(timeout = 120.seconds) { points { layer ->
        layer.setAttributeFilter("value >= 1"); layer.resetReading()
        val values = mutableListOf<Int>(); val sizes = mutableListOf<Int>()
        repeat(3) { val batch = layer.readFeatureBatch(3); sizes += batch.size
            try { for (feature in batch) values += feature.getFieldAsInteger("value") } finally { for (feature in batch) feature.close() } }
        assertEquals(listOf(3, 1, 0), sizes); assertEquals(listOf(1, 2, 3, 4), values)
        assertFails { layer.readFeatureBatch(0) }
    } }
    @Test fun batchScopeClosesFeaturesAndRetainedCloneSurvivesDataset() = runTest(timeout = 120.seconds) {
        var clone: Feature? = null; val retained = mutableListOf<Feature>(); val sizes = mutableListOf<Int>()
        points { layer -> layer.forEachFeatureBatch(2) { batch -> sizes += batch.size; retained += batch; if (clone == null) clone = batch.first().cloneFeature() } }
        assertEquals(listOf(2, 2, 1), sizes)
        for (feature in retained) assertFails { feature.getFID() }
        requireNotNull(clone).use { assertEquals(0, it.getFieldAsInteger("value")) }
    }
    @Test fun batchErrorAndCancellationCloseTemporaryFeatures() = runTest(timeout = 120.seconds) { points { layer ->
        var captured = emptyList<Feature>()
        assertFailsWith<IllegalStateException> { layer.forEachFeatureBatch(2) { captured = it; error("callback failed") } }
        for (feature in captured) assertFails { feature.getFID() }
        val job = launch { layer.forEachFeatureBatch(2) { captured = it; currentCoroutineContext().job.cancel(); yield() } }
        job.join(); assertTrue(job.isCancelled)
        for (feature in captured) assertFails { feature.getFID() }
        assertEquals(5L, layer.getFeatureCount()); assertFails { layer.forEachFeatureBatch(0) {} }
    } }
    private suspend fun <T> gpkg(block: suspend (Dataset, Layer) -> T): T {
        val path = "/vsimem/ogr-layer-batch-transaction.gpkg"
        return requireNotNull(Gdal4k.runtime().getDriver("GPKG")).use { driver -> try {
            requireNotNull(driver.createVector(path)).use { ds -> requireNotNull(ds.createLayer("records", geometryType = 100)).use { layer -> assertEquals(0, ds.flushCache()); block(ds, layer) } }
        } finally { driver.delete(path) } }
    }
    @Test fun batchWritesReportPartialFailuresAndKeepCallerFeaturesOpen() = runTest(timeout = 120.seconds) { gpkg { _, layer ->
        requireNotNull(layer.getLayerDefn()).use { definition -> Gdal4k.runtime().createFeature(definition).use { one -> Gdal4k.runtime().createFeature(definition).use { two ->
            one.setFID(42); two.setFID(42)
            val result = layer.writeFeatureBatch(listOf(one, two)); assertEquals(1, result.completed); assertFalse(result.successful); assertNotEquals(0, result.errorCode)
            assertEquals(1L, layer.getFeatureCount()); assertEquals(42L, one.getFID()); assertEquals(42L, two.getFID())
            assertTrue(layer.writeFeatureBatch(listOf(one), FeatureWriteMode.Replace).successful)
            one.setFID(43); assertTrue(layer.writeFeatureBatch(listOf(one), FeatureWriteMode.Upsert).successful)
            assertEquals(2L, layer.getFeatureCount())
        } } }
    } }
    @Test fun transactionsCommitRollbackAndCancellationAreExplicit() = runTest(timeout = 120.seconds) { gpkg { ds, layer ->
        requireNotNull(layer.getLayerDefn()).use { definition -> Gdal4k.runtime().createFeature(definition).use { feature ->
            feature.setFID(1)
            assertFailsWith<IllegalStateException> { ds.withTransaction { check(layer.writeFeatureBatch(listOf(feature, feature)).successful) } }
            assertEquals(0L, layer.getFeatureCount())
            ds.withTransaction { assertTrue(layer.writeFeatureBatch(listOf(feature)).successful) }
            assertEquals(1L, layer.getFeatureCount())
            feature.setFID(2)
            val job = launch { ds.withTransaction { assertTrue(layer.writeFeatureBatch(listOf(feature)).successful); currentCoroutineContext().job.cancel(); yield() } }
            job.join(); assertTrue(job.isCancelled); assertEquals(1L, layer.getFeatureCount())
            assertEquals(2L, feature.getFID())
        } }
    } }
}
