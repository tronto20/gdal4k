package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class MDArraySettersApiTest {
    private suspend fun memory(block: suspend (Group) -> Unit) {
        requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
            requireNotNull(driver.createMultidimensional("")).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { block(it) }
            }
        }
    }

    private suspend fun array(group: Group, name: String, type: MDDataType, sizes: LongArray,
                              block: suspend (MDArray) -> Unit) {
        val dimensions = mutableListOf<Dimension>()
        try {
            for ((index, size) in sizes.withIndex())
                dimensions += requireNotNull(group.createDimension("${name}_$index", size))
            requireNotNull(group.createArray(name, dimensions, type)).use { block(it) }
        } finally {
            withContext(NonCancellable) { for (dimension in dimensions.asReversed()) dimension.close() }
        }
    }

    /** A current public getter supplies a signed mapping without adding an OSR setter API. */
    private suspend fun signedSrs(): SpatialReference {
        val runtime = Gdal4k.runtime()
        val path = "/vsimem/$datasetOutputPrefix-md-setters-srs.vrt"
        runtime.writeMemoryFile(path, """<VRTDataset><Group name="/">
            <Dimension name="y" size="2"/><Dimension name="x" size="3"/>
            <Array name="source"><DataType>Float64</DataType><DimensionRef ref="/y"/><DimensionRef ref="/x"/>
            <SRS dataAxisToSRSAxisMapping="-2,1">EPSG:4326</SRS></Array>
            </Group></VRTDataset>""".encodeToByteArray())
        try {
            return runtime.openDataset(path, DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { group ->
                    requireNotNull(group.openArray("source")).use { requireNotNull(it.getSpatialRef()) }
                }
            }
        } finally { runtime.deleteMemoryFile(path) }
    }

    @Test fun memoryHintsDoNotConvertValuesAndLegacyCallsRetainTheirMeaning() = runTest(timeout = 120.seconds) {
        val precise = 1.234567890123
        assertNotEquals(precise, precise.toFloat().toDouble())
        memory { group ->
            array(group, "numeric", MDDataType.Numeric(GdalDataType.Float64), longArrayOf(2, 3)) { source ->
                val samples = doubleArrayOf(1.0, -2.0, 3.5, 4.0, 5.0, -6.0)
                assertTrue(source.write(longArrayOf(0, 0), longArrayOf(2, 3), samples))
                assertNull(source.getScaleEx()); assertNull(source.getOffsetEx())
                assertTrue(source.setScale(precise, GdalDataType.Float32))
                assertTrue(source.setOffset(-precise, GdalDataType.Int16))
                // MEM records a hint beside the Double; it does not quantize that value.
                assertEquals(MDNumericMetadata(precise, GdalDataType.Float32), source.getScaleEx())
                assertEquals(MDNumericMetadata(-precise, GdalDataType.Int16), source.getOffsetEx())
                assertTrue(source.setScale(0.5, GdalDataType.CFloat32))
                assertEquals(MDNumericMetadata(0.5, GdalDataType.CFloat32), source.getScaleEx())
                assertTrue(source.setOffset(0.25, GdalDataType.Float16))
                assertEquals(MDNumericMetadata(0.25, GdalDataType.Float16), source.getOffsetEx())
                assertTrue(source.setScale(1.0)); assertTrue(source.setOffset(0.0))
                assertEquals(MDNumericMetadata(1.0, GdalDataType.Unknown), source.getScaleEx())
                assertEquals(MDNumericMetadata(0.0, GdalDataType.Unknown), source.getOffsetEx())
                assertEquals(MDDataType.Numeric(GdalDataType.Float64), source.getExtendedDataType())
                assertContentEquals(samples, source.read(longArrayOf(0, 0), longArrayOf(2, 3)))
            }
            array(group, "scalar", MDDataType.Numeric(GdalDataType.Float64), longArrayOf()) { source ->
                assertNull(source.getScaleEx()); assertNull(source.getOffsetEx())
                assertTrue(source.setScale(Double.NaN, GdalDataType.Float32))
                assertTrue(requireNotNull(source.getScaleEx()).value.isNaN())
                assertEquals(GdalDataType.Float32, requireNotNull(source.getScaleEx()).storageType)
                assertTrue(source.setOffset(Double.NEGATIVE_INFINITY, GdalDataType.Float64))
                assertEquals(MDNumericMetadata(Double.NEGATIVE_INFINITY, GdalDataType.Float64), source.getOffsetEx())
                assertTrue(source.setScale(1.0, GdalDataType.Float32))
                assertTrue(source.setOffset(0.0, GdalDataType.Float64))
                assertEquals(MDNumericMetadata(1.0, GdalDataType.Float32), source.getScaleEx())
                assertEquals(MDNumericMetadata(0.0, GdalDataType.Float64), source.getOffsetEx())
            }
            for ((index, schema) in listOf(MDDataType.Text(), MDDataType.Numeric(GdalDataType.UInt64)).withIndex()) {
                array(group, "typed_$index", schema, longArrayOf()) { source ->
                    val sample = if (index == 0) MDValue.Text("unchanged 영상") else MDValue.Numeric(ULong.MAX_VALUE.toString())
                    assertTrue(source.writeValues(longArrayOf(), longArrayOf(), listOf(sample)))
                    assertTrue(source.setScale(precise, GdalDataType.Float32))
                    assertTrue(source.setOffset(-precise, GdalDataType.Float64))
                    assertEquals(MDNumericMetadata(precise, GdalDataType.Float32), source.getScaleEx())
                    assertEquals(schema, source.getExtendedDataType())
                    assertEquals(listOf(sample), source.readValues(longArrayOf(), longArrayOf()))
                }
            }
        }
    }

    @Test fun memoryCrsCopiesSurviveInputMutationClearAndOwnerClosure() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        var saved: SpatialReference? = null
        var closedSource: MDArray? = null
        try {
            memory { group ->
                array(group, "crs", MDDataType.Numeric(GdalDataType.Float64), longArrayOf(2, 3)) { source ->
                    closedSource = source
                    val samples = doubleArrayOf(1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
                    assertTrue(source.write(longArrayOf(0, 0), longArrayOf(2, 3), samples))
                    assertTrue(source.setSpatialRef(null)); assertNull(source.getSpatialRef())
                    signedSrs().use { input ->
                        assertTrue(source.setSpatialRef(input))
                        input.setAxisMappingStrategy(1)
                        assertContentEquals(intArrayOf(1, 2), input.getDataAxisToSRSAxisMapping())
                        requireNotNull(source.getSpatialRef()).use {
                            assertContentEquals(intArrayOf(-2, 1), it.getDataAxisToSRSAxisMapping())
                            assertEquals("4326", it.getAuthorityCode("GEOGCS"))
                        }
                    }
                    saved = requireNotNull(source.getSpatialRef())
                    runtime.createSpatialReference().use { replacement ->
                        assertEquals(0, replacement.importFromEPSG(3857))
                        assertTrue(source.setSpatialRef(replacement))
                    }
                    requireNotNull(source.getSpatialRef()).use { assertEquals("3857", it.getAuthorityCode("PROJCS")) }
                    assertTrue(source.setSpatialRef(null)); assertNull(source.getSpatialRef())
                    val closed = runtime.createSpatialReference(); closed.close()
                    assertFails { source.setSpatialRef(closed) }
                    assertNull(source.getSpatialRef())
                    assertContentEquals(samples, source.read(longArrayOf(0, 0), longArrayOf(2, 3)))
                }
            }
            assertEquals("4326", requireNotNull(saved).getAuthorityCode("GEOGCS"))
            assertContentEquals(intArrayOf(-2, 1), requireNotNull(saved).getDataAxisToSRSAxisMapping())
            assertFails { requireNotNull(closedSource).setSpatialRef(null) }
            assertFails { requireNotNull(closedSource).setScale(2.0, GdalDataType.Float32) }
        } finally { saved?.close() }
    }

    @Test fun nonidentityUnscaledViewsKeepTheirCapturedTransformAfterMetadataChanges() = runTest(timeout = 120.seconds) {
        memory { group ->
            array(group, "capture", MDDataType.Numeric(GdalDataType.Float64), longArrayOf(2)) { source ->
                val samples = doubleArrayOf(2.0, -3.0)
                assertTrue(source.write(longArrayOf(0), longArrayOf(2), samples))
                assertTrue(source.setScale(2.0)); assertTrue(source.setOffset(1.0))
                source.getUnscaled().use { before ->
                    assertTrue(source.setScale(3.0, GdalDataType.Float32))
                    assertTrue(source.setOffset(-4.0, GdalDataType.Float64))
                    assertContentEquals(doubleArrayOf(5.0, -5.0), before.read(longArrayOf(0), longArrayOf(2)))
                    source.getUnscaled().use { after ->
                        assertContentEquals(doubleArrayOf(2.0, -13.0), after.read(longArrayOf(0), longArrayOf(2)))
                    }
                    assertContentEquals(samples, source.read(longArrayOf(0), longArrayOf(2)))
                }
            }
        }
    }

    @Test fun reopenedVrtStoresValuesAndSignedCrsButIgnoresStorageTypeHints() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        val path = "/vsimem/$datasetOutputPrefix-md-setters.vrt"
        val samples = doubleArrayOf(1.0, -2.0, 3.5, 4.0, 5.0, -6.0)
        val scale = 1.234567890123
        val offset = -9.876543210123
        val update = DatasetOpenOptions(accessMode = DatasetAccessMode.Update, kind = DatasetKind.Multidimensional)
        val read = DatasetOpenOptions(kind = DatasetKind.Multidimensional)
        runtime.writeMemoryFile(path, """<VRTDataset><Group name="/">
            <Dimension name="y" size="2"/><Dimension name="x" size="3"/>
            <Array name="values"><DataType>Float64</DataType><DimensionRef ref="/y"/><DimensionRef ref="/x"/>
            <InlineValues>1 -2 3.5 4 5 -6</InlineValues></Array>
            </Group></VRTDataset>""".encodeToByteArray())
        suspend fun open(options: DatasetOpenOptions, block: suspend (MDArray) -> Unit) {
            runtime.openDataset(path, options).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { group ->
                    requireNotNull(group.openArray("values")).use { block(it) }
                }
            }
        }
        var saved: SpatialReference? = null
        try {
            open(update) { source ->
                assertContentEquals(samples, source.read(longArrayOf(0, 0), longArrayOf(2, 3)))
                assertTrue(source.setScale(scale, GdalDataType.Float32))
                assertTrue(source.setOffset(offset, GdalDataType.Int16))
                assertEquals(MDNumericMetadata(scale, GdalDataType.Unknown), source.getScaleEx())
                assertEquals(MDNumericMetadata(offset, GdalDataType.Unknown), source.getOffsetEx())
                signedSrs().use { assertTrue(source.setSpatialRef(it)) }
            }
            // All writer owners are closed before reading the serialized XML and reopening.
            val serialized = runtime.readMemoryFile(path).decodeToString()
            assertContains(serialized, "<Scale>"); assertContains(serialized, "<Offset>")
            assertContains(serialized, "dataAxisToSRSAxisMapping=\"-2,1\"")
            open(read) { source ->
                assertEquals(MDNumericMetadata(scale, GdalDataType.Unknown), source.getScaleEx())
                assertEquals(MDNumericMetadata(offset, GdalDataType.Unknown), source.getOffsetEx())
                saved = requireNotNull(source.getSpatialRef())
                assertContentEquals(intArrayOf(-2, 1), requireNotNull(saved).getDataAxisToSRSAxisMapping())
                assertContentEquals(samples, source.read(longArrayOf(0, 0), longArrayOf(2, 3)))
            }
            open(update) { assertTrue(it.setSpatialRef(null)); assertNull(it.getSpatialRef()) }
            open(read) { source ->
                assertNull(source.getSpatialRef())
                assertEquals(MDNumericMetadata(scale, GdalDataType.Unknown), source.getScaleEx())
                assertContentEquals(samples, source.read(longArrayOf(0, 0), longArrayOf(2, 3)))
            }
            assertEquals("4326", requireNotNull(saved).getAuthorityCode("GEOGCS"))
            assertContentEquals(intArrayOf(-2, 1), requireNotNull(saved).getDataAxisToSRSAxisMapping())
        } finally { saved?.close(); runtime.deleteMemoryFile(path) }
    }
}
