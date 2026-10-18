package dev.gdal4k

import dev.gdal4k.GdalFile
import dev.gdal4k.GdalFiles
import dev.gdal4k.runtime.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.job
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.yield
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class VectorColumnsApiTest {
    @Test fun sqlResultStreamCancelsAndClosesBeforeItsDataset() = runTest(timeout = 120.seconds) { layer("MEM") { ds, _ ->
        requireNotNull(ds.executeSQL("SELECT * FROM records")).use { result ->
            result.openColumnStream(1).use { stream ->
                assertFailsWith<CancellationException> { withGdalProgress({ false }) { stream.read() } }
            }
            result.resetReading(); requireNotNull(result.getNextFeature()).close()
            val stream = result.openColumnStream(1)
            assertNotNull(stream.read())
            ds.close(); assertFails { stream.read() }; stream.close()
        }
    } }
    @Test fun geometryWkbAndCrsRemainUsableAfterDatasetClose() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime(); var retained: FeatureColumn.Binary? = null
        runtime.createSpatialReference("EPSG:4326").use { srs ->
            requireNotNull(runtime.getDriver("MEM")).use { driver -> requireNotNull(driver.createVector("")).use { ds ->
                requireNotNull(ds.createLayer("points", srs, geometryType = 1001)).use { layer ->
                    requireNotNull(layer.getLayerDefn()).use { definition -> runtime.createFeature(definition).use { feature ->
                        runtime.createGeometryFromWkt("POINT Z (12 45 7)", srs).use { geometry ->
                            assertEquals(0, feature.setGeometry(geometry)); assertEquals(0, layer.createFeature(feature))
                        }
                    } }
                    layer.forEachColumnBatch(1) { batch -> retained = batch.columns.single { it.geometry } as FeatureColumn.Binary }
                }
            } }
        }
        val column = requireNotNull(retained); assertTrue(column.valid[0])
        runtime.createSpatialReference(requireNotNull(column.spatialReferenceWkt)).use { crs ->
            assertTrue(crs.exportToWkt().contains("4326"))
            runtime.createGeometryFromWkb(column.values[0], crs).use { geometry ->
                assertContentEquals(doubleArrayOf(12.0, 45.0, 7.0), geometry.getPoint(0))
                requireNotNull(geometry.getSpatialReference()).use { assertTrue(it.exportToWkt().contains("4326")) }
            }
        }
    }
    private fun fixture() = GdalFiles(GdalFile("columns.geojson", """{"type":"FeatureCollection","features":[
        {"type":"Feature","properties":{"big":9007199254740993,"real":1.25,"text":"한글 😀"},"geometry":{"type":"Point","coordinates":[0,1]}},
        {"type":"Feature","properties":{"big":-9007199254740993,"real":-2.5,"text":""},"geometry":null},
        {"type":"Feature","properties":{"big":null,"real":null,"text":null},"geometry":{"type":"Point","coordinates":[2,1]}},
        {"type":"Feature","properties":{"big":9007199254740999,"real":3.0,"text":"last"},"geometry":{"type":"Point","coordinates":[3,1]}}
    ]}""".encodeToByteArray()))

    private suspend fun <T> layer(driverName: String = "GeoJSON", block: suspend (Dataset, Layer) -> T): T {
        val runtime = Gdal4k.runtime()
        return runtime.openDataset(fixture(), DatasetOpenOptions(kind = DatasetKind.Vector)).use { source ->
            requireNotNull(source.getLayer(0)).use { input ->
                if (driverName == "GeoJSON") block(source, input)
                else requireNotNull(runtime.getDriver(driverName)).use { driver ->
                    val path = if (driverName == "GPKG") "/vsimem/vector-columns-api.gpkg" else ""
                    try {
                        requireNotNull(driver.createVector(path)).use { dataset ->
                            requireNotNull(dataset.copyLayer(input, "records")).use { copied ->
                                assertEquals(0, dataset.flushCache()); block(dataset, copied)
                            }
                        }
                    } finally { if (path.isNotEmpty()) assertEquals(0, driver.delete(path)) }
                }
            }
        }
    }

    private data class Row(val fid: Long, val big: Long?, val real: Double?, val text: String?, val wkb: List<Byte>?)
    private suspend fun rows(layer: Layer): List<Row> {
        val rows = mutableListOf<Row>()
        layer.forEachFeatureBatch(2) { features -> for (feature in features) {
            val big = if (feature.isFieldSetAndNotNull(0)) feature.getFieldAsInteger64(0) else null
            val real = if (feature.isFieldSetAndNotNull(1)) feature.getFieldAsDouble(1) else null
            val text = if (feature.isFieldSetAndNotNull(2)) feature.getFieldAsString(2) else null
            val geometry = feature.getGeometryRef()?.use { it.exportToWkb().toList() }
            rows.add(Row(feature.getFID(), big, real, text, geometry))
        } }
        return rows
    }
    private fun rows(batch: FeatureColumnBatch): List<Row> {
        val fid = batch.columns.first() as FeatureColumn.Longs
        val big = batch.columns.single { it.name == "big" } as FeatureColumn.Longs
        val real = batch.columns.single { it.name == "real" } as FeatureColumn.Reals
        val text = batch.columns.single { it.name == "text" } as FeatureColumn.Strings
        val geometry = batch.columns.single { it.geometry } as FeatureColumn.Binary
        return (0 until batch.rowCount).map { i -> Row(fid.values[i], big.values[i].takeIf { big.valid[i] },
            real.values[i].takeIf { real.valid[i] }, text.values[i].takeIf { text.valid[i] }, geometry.values[i].toList().takeIf { geometry.valid[i] }) }
    }

    @Test fun detachedColumnsMatchFeaturesOnGeoJsonMemAndGeoPackage() = runTest(timeout = 180.seconds) {
        for (driver in listOf("GeoJSON", "MEM", "GPKG")) layer(driver) { _, layer ->
            val expected = rows(layer)
            assertEquals(9007199254740993L, expected[0].big)
            assertEquals(-9007199254740993L, expected[1].big); assertEquals(9007199254740999L, expected[3].big)
            val batches = mutableListOf<FeatureColumnBatch>()
            layer.openColumnStream(3).use { stream ->
                while (true) batches += stream.read() ?: break
                assertNull(stream.read())
            }
            assertTrue(batches.all { it.rowCount in 1..3 })
            assertEquals(expected, batches.flatMap(::rows))
            layer.close()
            assertEquals(expected, batches.flatMap(::rows))
        }
    }
    @Test fun filtersApplyAndColumnScanStartsAtBeginning() = runTest(timeout = 120.seconds) { layer { _, layer ->
        assertEquals(0, layer.setAttributeFilter("real > 0"))
        assertEquals(0, layer.setSpatialFilterRect(1.0, 0.0, 4.0, 2.0))
        val expected = rows(layer); assertEquals(1, expected.size)
        // The feature cursor is at EOF; opening the column scan rewinds it under the same filters.
        val actual = mutableListOf<Row>()
        layer.forEachColumnBatch(1) { actual += rows(it) }
        assertEquals(expected, actual)
    } }
    @Test fun aliasesCannotChangeCursorFiltersSchemaOrDatasetDuringStream() = runTest(timeout = 120.seconds) { layer("MEM") { ds, layer ->
        requireNotNull(ds.getLayer(0)).use { alias ->
            requireNotNull(layer.getLayerDefn()).use { schema ->
                requireNotNull(schema.getGeomFieldDefn(0)).use { geometryField ->
                    layer.openColumnStream(2).use { stream ->
                        assertFails { alias.getNextFeature() }; assertFails { alias.resetReading() }
                        assertFails { alias.setAttributeFilter("real > 0") }
                        assertFails { alias.setSpatialFilterRect(0.0, 0.0, 1.0, 1.0) }
                        assertFails { alias.openColumnStream(2) }; assertFails { alias.deleteField(0) }
                        assertFails { geometryField.setName("changed") }
                        assertFails { ds.executeSQL("SELECT * FROM records") }
                        assertFails { ds.deleteLayer(0) }; assertFails { ds.startTransaction() }
                        assertEquals(2, requireNotNull(stream.read()).rowCount)
                    }
                }
            }
            assertEquals(4L, alias.getFeatureCount(true)); alias.resetReading()
            requireNotNull(alias.getNextFeature()).close()
        }
    } }
    @Test fun parentAndAliasCloseInvalidateStreamButRetainedColumnsSurvive() = runTest(timeout = 120.seconds) {
        layer { ds, layer ->
            val stream = layer.openColumnStream(2); val batch = requireNotNull(stream.read())
            ds.close(); assertFails { stream.read() }; stream.close()
            assertEquals(9007199254740993L, (batch.columns[1] as FeatureColumn.Longs).values[0])
        }
        layer { ds, layer ->
            val stream = layer.openColumnStream(2)
            requireNotNull(ds.getLayer(0)).close()
            assertFails { stream.read() }; stream.close()
            layer.resetReading(); requireNotNull(layer.getNextFeature()).close()
        }
    }
    @Test fun callbackFailureAndCancellationReleaseScopedStream() = runTest(timeout = 120.seconds) { layer { _, layer ->
        assertFailsWith<IllegalStateException> { layer.forEachColumnBatch(1) { error("callback failed") } }
        layer.resetReading(); requireNotNull(layer.getNextFeature()).close()
        val job = launch { layer.forEachColumnBatch(1) { currentCoroutineContext().job.cancel(); yield() } }
        job.join(); assertTrue(job.isCancelled)
        val retained = mutableListOf<Row>(); layer.forEachColumnBatch(1) { retained += rows(it) }
        assertEquals(4, retained.size)
    } }
    @Test fun emptyFilteredLayerReturnsEofAndInvalidBatchSizesDoNotAcquireStream() = runTest(timeout = 120.seconds) { layer { _, layer ->
        assertFails { layer.openColumnStream(0) }; assertFails { layer.openColumnStream(65537) }
        assertEquals(0, layer.setAttributeFilter("real > 1000"))
        layer.openColumnStream(1).use { assertNull(it.read()); assertNull(it.read()) }
        assertEquals(0, layer.setAttributeFilter(""))
        assertEquals(4, rows(layer).size)
    } }
    @Test fun unsupportedListSchemaFailsExplicitlyAndReleasesNativeStream() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        requireNotNull(runtime.getDriver("MEM")).use { driver -> requireNotNull(driver.createVector("")).use { ds ->
            requireNotNull(ds.createLayer("unsupported", geometryType = 100)).use { layer ->
                runtime.createFieldDefn("list", 1).use { assertEquals(0, layer.createField(it)) }
                assertFails { layer.openColumnStream(1) }
                assertEquals(0L, layer.getFeatureCount(true)); layer.resetReading()
                assertEquals(0, layer.deleteField(0))
                layer.openColumnStream(1).use { assertNull(it.read()) }
            }
        } }
    }
    @Test fun explicitlyTypedInt64PreservesBothExtremesWithoutGeoJsonInference() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        val input = longArrayOf(Long.MIN_VALUE, Long.MAX_VALUE, -1, 0, 9007199254740993L)
        requireNotNull(runtime.getDriver("MEM")).use { driver -> requireNotNull(driver.createVector("")).use { ds ->
            requireNotNull(ds.createLayer("exact", geometryType = 100)).use { layer ->
                runtime.createFieldDefn("exact", 12).use { assertEquals(0, layer.createField(it)) }
                requireNotNull(layer.getLayerDefn()).use { definition ->
                    for (value in input) runtime.createFeature(definition).use { feature ->
                        feature.setFieldInteger64(0, value); assertEquals(0, layer.createFeature(feature))
                    }
                }
                val output = mutableListOf<Long>()
                layer.forEachColumnBatch(2) { batch ->
                    val column = batch.columns.single { it.name == "exact" } as FeatureColumn.Longs
                    assertTrue(column.valid.all { it }); output += column.values.toList()
                }
                assertContentEquals(input, output.toLongArray())
            }
        } }
    }
    @Test fun booleanIntegerAndBinaryAttributesKeepTheirTypedValues() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime(); val path = "/vsimem/column-attribute-types.gpkg"
        requireNotNull(runtime.getDriver("GPKG")).use { driver -> try {
            requireNotNull(driver.createVector(path)).use { ds ->
                requireNotNull(ds.createLayer("records", geometryType = 100)).use { layer ->
                    runtime.createFieldDefn("flag", 0).use { field -> field.setSubType(1); assertEquals(0, layer.createField(field)) }
                    runtime.createFieldDefn("number", 0).use { assertEquals(0, layer.createField(it)) }
                    runtime.createFieldDefn("payload", 8).use { assertEquals(0, layer.createField(it)) }
                    assertEquals(0, ds.flushCache())
                    ds.executeSQL("INSERT INTO records (flag,number,payload) VALUES (1,-2147483648,X'0001FF')")?.close()
                    ds.executeSQL("INSERT INTO records (flag,number,payload) VALUES (0,2147483647,X'')")?.close()
                    ds.executeSQL("INSERT INTO records (flag,number,payload) VALUES (NULL,NULL,NULL)")?.close()
                    layer.openColumnStream(3).use { stream ->
                        val batch = requireNotNull(stream.read()); assertEquals(3, batch.rowCount)
                        val flag = batch.columns.single { it.name == "flag" } as FeatureColumn.Booleans
                        val number = batch.columns.single { it.name == "number" } as FeatureColumn.Integers
                        val payload = batch.columns.single { it.name == "payload" } as FeatureColumn.Binary
                        assertContentEquals(booleanArrayOf(true, true, false), flag.valid)
                        assertContentEquals(booleanArrayOf(true, false, false), flag.values)
                        assertContentEquals(intArrayOf(Int.MIN_VALUE, Int.MAX_VALUE, 0), number.values)
                        assertContentEquals(byteArrayOf(0, 1, -1), payload.values[0]); assertTrue(payload.values[1].isEmpty())
                        assertContentEquals(booleanArrayOf(true, true, false), payload.valid); assertFalse(payload.geometry)
                        assertNull(stream.read())
                    }
                }
            }
        } finally { assertEquals(0, driver.delete(path)) } }
    }
}
