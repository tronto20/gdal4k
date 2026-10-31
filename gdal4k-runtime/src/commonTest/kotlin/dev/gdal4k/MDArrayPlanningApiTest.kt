package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class MDArrayPlanningApiTest {
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
                dimensions.add(requireNotNull(group.createDimension("${name}_$index", size)))
            requireNotNull(group.createArray(name, dimensions, type)).use { block(it) }
        } finally {
            withContext(NonCancellable) { for (dimension in dimensions.asReversed()) dimension.close() }
        }
    }

    @Test fun memoryCountsAndChunksUseStoredTypeWithoutChangingSamples() = runTest(timeout = 120.seconds) {
        memory { group ->
            array(group, "numeric", MDDataType.Numeric(GdalDataType.Float64), longArrayOf(4, 6)) { source ->
                val samples = DoubleArray(24) { it.toDouble() }
                assertTrue(source.write(longArrayOf(0, 0), longArrayOf(4, 6), samples))
                assertEquals(listOf(0uL, 0uL), source.getBlockSize())
                assertEquals(24uL, source.getTotalElementsCount())
                assertEquals(listOf(1uL, 1uL), source.getProcessingChunkSize(0uL))
                assertEquals(listOf(1uL, 4uL), source.getProcessingChunkSize(32uL))
                assertEquals(listOf(2uL, 6uL), source.getProcessingChunkSize(96uL))
                assertEquals(MDDataType.Numeric(GdalDataType.Float64), source.getExtendedDataType())
                assertContentEquals(samples, source.read(longArrayOf(0, 0), longArrayOf(4, 6)))
            }
        }
    }

    @Test fun scalarAndNonnumericShapesDoNotRequireNumericValues() = runTest(timeout = 120.seconds) {
        memory { group ->
            val record = MDDataType.Compound("planning_record", listOf(
                MDComponent("id", MDDataType.Numeric(GdalDataType.Int64)),
                MDComponent("label", MDDataType.Text())))
            val schemas = listOf(MDDataType.Numeric(GdalDataType.Float64),
                MDDataType.Numeric(GdalDataType.CFloat64), MDDataType.Text(), record)
            val values = listOf<MDValue>(MDValue.Numeric("2"), MDValue.Numeric("1", "-2"),
                MDValue.Text("small text 영상"), MDValue.Record(listOf(
                    MDValue.Numeric("9007199254740993"), MDValue.Text("record"))))
            for ((index, schema) in schemas.withIndex()) {
                array(group, "scalar_$index", schema, longArrayOf()) { source ->
                    assertTrue(source.writeValues(longArrayOf(), longArrayOf(), listOf(values[index])))
                    assertEquals(1uL, source.getTotalElementsCount())
                    assertEquals(emptyList(), source.getProcessingChunkSize(0uL))
                    assertEquals(emptyList(), source.getProcessingChunkSize(UInt.MAX_VALUE.toULong()))
                    assertEquals(listOf(values[index]), source.readValues(longArrayOf(), longArrayOf()))
                }
            }
            for ((index, schema) in listOf(MDDataType.Text(), record).withIndex()) {
                array(group, "nonnumeric_$index", schema, longArrayOf(2, 3)) { source ->
                    assertEquals(6uL, source.getTotalElementsCount())
                    assertEquals(listOf(1uL, 1uL), source.getProcessingChunkSize(0uL))
                    val chunks = source.getProcessingChunkSize(64uL)
                    assertEquals(2, chunks.size)
                    assertTrue(chunks[0] in 1uL..2uL)
                    assertTrue(chunks[1] in 1uL..3uL)
                    // Dynamic text payloads are outside the native EDT record-size estimate.
                    assertEquals(schema, source.getExtendedDataType())
                }
            }
        }
    }

    @Test fun composedViewsAndDetachedSnapshotsFollowTheQueriedShape() = runTest(timeout = 120.seconds) {
        var detachedCount = 0uL
        var detachedChunks = emptyList<ULong>()
        var closedSource: MDArray? = null
        memory { group ->
            array(group, "composed", MDDataType.Numeric(GdalDataType.Float64), longArrayOf(4, 6)) { source ->
                closedSource = source
                val samples = DoubleArray(24) { it.toDouble() }
                assertTrue(source.write(longArrayOf(0, 0), longArrayOf(4, 6), samples))
                source.transpose(intArrayOf(1, 0)).use { transposed ->
                    assertEquals(24uL, transposed.getTotalElementsCount())
                    transposed.getView("[1:6:2,newaxis,::-2]").use { view ->
                        detachedCount = view.getTotalElementsCount()
                        detachedChunks = view.getProcessingChunkSize(0uL)
                        assertEquals(6uL, detachedCount)
                        assertEquals(listOf(1uL, 1uL, 1uL), detachedChunks)
                        assertContentEquals(doubleArrayOf(19.0, 7.0, 21.0, 9.0, 23.0, 11.0),
                            view.read(longArrayOf(0, 0, 0), longArrayOf(3, 1, 2)))
                    }
                }
                source.getView("[1,2]").use { scalar ->
                    assertEquals(1uL, scalar.getTotalElementsCount())
                    assertEquals(emptyList(), scalar.getProcessingChunkSize(0uL))
                }
                assertContentEquals(samples, source.read(longArrayOf(0, 0), longArrayOf(4, 6)))
            }
        }
        // The array, group, dataset and driver have closed; these values own no handles.
        assertEquals(6uL, detachedCount)
        assertEquals(listOf(1uL, 1uL, 1uL), detachedChunks)
        assertFails { requireNotNull(closedSource).getTotalElementsCount() }
        assertFails { requireNotNull(closedSource).getProcessingChunkSize(0uL) }
    }

    @Test fun metadataOnlyVrtCountsPreserveUnsignedRangeAndNativeOverflowSentinel() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        val path = "/vsimem/$datasetOutputPrefix-md-planning.vrt"
        val xml = """<VRTDataset><Group name="/">
            <Dimension name="y" size="4"/><Dimension name="x" size="6"/>
            <Dimension name="below32" size="4294967295"/><Dimension name="above32" size="4294967297"/>
            <Dimension name="power32" size="4294967296"/><Dimension name="above53" size="9007199254740993"/>
            <Array name="blocked"><DataType>Float64</DataType><DimensionRef ref="/y"/><DimensionRef ref="/x"/><BlockSize>2,3</BlockSize></Array>
            <Array name="maximum"><DataType>Byte</DataType><DimensionRef ref="/below32"/><DimensionRef ref="/above32"/></Array>
            <Array name="overflow"><DataType>Byte</DataType><DimensionRef ref="/power32"/><DimensionRef ref="/power32"/></Array>
            <Array name="exact"><DataType>Byte</DataType><DimensionRef ref="/above53"/></Array>
            </Group></VRTDataset>"""
        val bytes = xml.encodeToByteArray()
        assertTrue(bytes.size < 4096)
        runtime.writeMemoryFile(path, bytes)
        try {
            runtime.openDataset(path, DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { group ->
                    requireNotNull(group.openArray("blocked")).use { source ->
                        assertEquals(24uL, source.getTotalElementsCount())
                        assertEquals(listOf(2uL, 3uL), source.getBlockSize())
                        // The indivisible native block is 48 bytes, exceeding this advisory budget.
                        assertEquals(listOf(2uL, 3uL), source.getProcessingChunkSize(1uL))
                    }
                    // No sample I/O, statistics, view construction or huge allocation is requested.
                    for ((name, expected) in listOf("maximum" to ULong.MAX_VALUE,
                        "overflow" to 0uL, "exact" to 9_007_199_254_740_993uL)) {
                        requireNotNull(group.openArray(name)).use { assertEquals(expected, it.getTotalElementsCount(), name) }
                    }
                }
            }
        } finally { runtime.deleteMemoryFile(path) }
    }

    @Test fun nativeSizeTBudgetBoundariesAreCheckedBeforeNarrowing() = runTest(timeout = 120.seconds) {
        memory { group ->
            array(group, "bounds", MDDataType.Numeric(GdalDataType.Float64), longArrayOf(2, 3)) { source ->
                assertEquals(listOf(2uL, 3uL), source.getProcessingChunkSize(UInt.MAX_VALUE.toULong()))
                if (GdalPlatform.current().archName == "wasm32") {
                    assertFails { source.getProcessingChunkSize(UInt.MAX_VALUE.toULong() + 1uL) }
                    assertFails { source.getProcessingChunkSize(ULong.MAX_VALUE) }
                } else {
                    // Every current non-web engine target uses a 64-bit size_t ABI.
                    assertEquals(listOf(2uL, 3uL), source.getProcessingChunkSize(ULong.MAX_VALUE))
                }
                assertEquals(6uL, source.getTotalElementsCount())
                assertEquals(listOf(1uL, 1uL), source.getProcessingChunkSize(0uL))
            }
        }
    }
}
