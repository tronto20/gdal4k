package dev.gdal4k

import dev.gdal4k.GdalException
import dev.gdal4k.runtime.*
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class MDArrayViewApiTest {
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

    private suspend fun shape(array: MDArray): LongArray {
        val sizes = mutableListOf<Long>()
        for (dimension in array.getDimensions()) dimension.use { sizes.add(it.getSize()) }
        return sizes.toLongArray()
    }

    private fun values(vararg text: String): List<MDValue> = text.map { MDValue.Numeric(it) }

    @Test fun exactSlicesAliasReversedAndNoncontiguousSourceCells() = runTest(timeout = 120.seconds) {
        memory { group ->
            array(group, "exact", MDDataType.Numeric(GdalDataType.Int64), longArrayOf(2, 4)) { source ->
                val raw = values("9007199254740993", "2", "3", "4", "5", "6", "7", "8")
                assertTrue(source.writeValues(longArrayOf(0, 0), longArrayOf(2, 4), raw))
                source.getView("[::-1,::-1]").use { view ->
                    assertEquals(MDDataType.Numeric(GdalDataType.Int64), view.getExtendedDataType())
                    assertContentEquals(longArrayOf(2, 4), shape(view))
                    assertEquals(raw.reversed(), view.readValues(longArrayOf(0, 0), longArrayOf(2, 4)))
                }
                source.getView("[-1,1:4:2]").use { view ->
                    assertEquals(values("6", "8"), view.readValues(longArrayOf(0), longArrayOf(2)))
                    assertTrue(view.writeValues(longArrayOf(0), longArrayOf(2), values("9007199254740995", "-9")))
                }
                assertEquals(values("9007199254740993", "2", "3", "4", "5", "9007199254740995", "7", "-9"),
                    source.readValues(longArrayOf(0, 0), longArrayOf(2, 4)))
                source.getView("[::-1,::-2]").use { view ->
                    assertTrue(view.writeValues(longArrayOf(0, 0), longArrayOf(2, 2), values("10", "11", "12", "13")))
                }
                assertEquals(values("9007199254740993", "13", "3", "12", "5", "11", "7", "10"),
                    source.readValues(longArrayOf(0, 0), longArrayOf(2, 4)))
            }
        }
    }

    @Test fun ellipsisNewAxesAndScalarResultsRetainRankAndOwnership() = runTest(timeout = 120.seconds) {
        memory { group ->
            array(group, "matrix", MDDataType.Numeric(GdalDataType.Int16), longArrayOf(2, 4)) { source ->
                val raw = (0..7).map { MDValue.Numeric(it) }
                assertTrue(source.writeValues(longArrayOf(0, 0), longArrayOf(2, 4), raw))
                for ((expression, expected) in listOf("[...,newaxis]" to longArrayOf(2, 4, 1),
                    "[newaxis,...,np.newaxis]" to longArrayOf(1, 2, 4, 1))) {
                    source.getView(expression).use { view ->
                        assertContentEquals(expected, shape(view))
                        assertEquals(raw, view.readValues(LongArray(expected.size), expected))
                    }
                }
                source.getView("[1,2]").use { scalar ->
                    assertTrue(shape(scalar).isEmpty())
                    assertEquals(values("6"), scalar.readValues(longArrayOf(), longArrayOf()))
                    assertTrue(scalar.writeValues(longArrayOf(), longArrayOf(), values("17")))
                    assertFailsWith<GdalException> { scalar.getView("[newaxis]") }
                }
                source.getView("[1][2]").use { scalar ->
                    assertEquals(values("17"), scalar.readValues(longArrayOf(), longArrayOf()))
                }
                val sibling = source.getView("[:]")
                val closedView = source.getView("[:]")
                try {
                    closedView.close(); closedView.close()
                    assertEquals("matrix", source.getName())
                    assertEquals(8, sibling.readValues(longArrayOf(0, 0), longArrayOf(2, 4)).size)
                    source.close()
                    assertFailsWith<IllegalStateException> { sibling.getName() }
                } finally { sibling.close(); closedView.close() }
            }
            array(group, "vector", MDDataType.Numeric(GdalDataType.Int16), longArrayOf(2)) { source ->
                source.getView("[...,newaxis,newaxis]").use {
                    assertContentEquals(longArrayOf(2, 1, 1), shape(it))
                }
            }
        }
    }

    @Test fun clippedRangesAndMalformedChainsAreSafe() = runTest(timeout = 120.seconds) {
        memory { group ->
            array(group, "ranges", MDDataType.Numeric(GdalDataType.Int16), longArrayOf(4)) { source ->
                val raw = values("0", "1", "2", "3")
                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(4), raw))
                val cases = listOf("[::-2]" to values("3", "1"), "[-1::-2]" to values("3", "1"),
                    "[:999]" to raw, "[:,]" to raw, "[-999:999]" to raw, "[-9223372036854775808:]" to raw,
                    "[:0:-1]" to values("3", "2", "1"), "[1:-999:-1]" to values("1", "0"))
                for ((expression, expected) in cases) source.getView(expression).use {
                    assertEquals(expected, it.readValues(longArrayOf(0), longArrayOf(expected.size.toLong())), expression)
                }
                // These expressions must only run against the fresh patched engine.
                for (expression in listOf("", "[]", "[4][0]", "[...,0,0]", "[...,...,:]", "[0,,1]", "[0,,]",
                    "[99:]", "[:0]", "[::0]", "[:: -9223372036854775808]", "[-9223372036854775808]",
                    "[9223372036854775808]", "[bad:3]", "[0:1:2:3]", "[0")) {
                    assertFailsWith<GdalException>(expression) { source.getView(expression) }
                    assertEquals(raw, source.readValues(longArrayOf(0), longArrayOf(4)), expression)
                }
            }
        }
    }

    @Test fun textJsonCompoundFieldsAndComplexValuesStayTyped() = runTest(timeout = 120.seconds) {
        memory { group ->
            for ((index, type) in listOf(MDDataType.Text(), MDDataType.JsonText()).withIndex()) {
                array(group, "text_$index", type, longArrayOf(3)) { source ->
                    val raw = listOf(MDValue.Text(null), MDValue.Text(""), MDValue.Text(" { raw 영상 😀 } "))
                    assertTrue(source.writeValues(longArrayOf(0), longArrayOf(3), raw))
                    source.getView("[::-1]").use { view ->
                        assertEquals(type, view.getExtendedDataType())
                        assertEquals(raw.reversed(), view.readValues(longArrayOf(0), longArrayOf(3)))
                    }
                }
            }
            val complex = MDDataType.Numeric(GdalDataType.CFloat64)
            val inner = MDDataType.Compound("inner", listOf(MDComponent("label", MDDataType.Text()), MDComponent("z", complex)))
            val record = MDDataType.Compound("outer", listOf(MDComponent("id", MDDataType.Numeric(GdalDataType.UInt64)),
                MDComponent("detail", inner), MDComponent("quote'field", MDDataType.Numeric(GdalDataType.Int16))))
            array(group, "records", record, longArrayOf(2)) { source ->
                val raw = (0..1).map { MDValue.Record(listOf(MDValue.Numeric("1844674407370955161$it"),
                    MDValue.Record(listOf(MDValue.Text("row$it"), MDValue.Numeric("${it + 1}", "${-it - 2}"))), MDValue.Numeric(it))) }
                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(2), raw))
                source.getView("[1]['id']").use { field ->
                    assertEquals(values("18446744073709551611"), field.readValues(longArrayOf(), longArrayOf()))
                    assertFalse(field.writeValues(longArrayOf(), longArrayOf(), values("3")))
                }
                source.getView("[\"detail\"]['z'][::-1]").use { field ->
                    assertEquals(complex, field.getExtendedDataType())
                    assertEquals(listOf(MDValue.Numeric("2", "-3"), MDValue.Numeric("1", "-2")),
                        field.readValues(longArrayOf(0), longArrayOf(2)))
                    assertFailsWith<GdalException> { field.getView("['real']") }
                }
                source.getView("['quote\\'field']").use {
                    assertEquals(values("0", "1"), it.readValues(longArrayOf(0), longArrayOf(2)))
                }
                assertFailsWith<GdalException> { source.getView("['absent'][:]") }
                assertEquals(raw, source.readValues(longArrayOf(0), longArrayOf(2)))
            }
        }
    }

    @Test fun slicedCrsMappingsBlocksAndDetachedCopiesFollowSourceAxes() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        val path = "/vsimem/$datasetOutputPrefix-md-view.vrt"
        val mappings = listOf("negative" to "-2,1", "zero" to "0,1", "minimum" to "-2147483648,1", "outside" to "3,1")
        val xml = buildString {
            append("""<VRTDataset><Group name="/"><Dimension name="y" size="2"/><Dimension name="x" size="3"/>""")
            for ((name, mapping) in mappings) append("""<Array name="$name"><DataType>Float64</DataType><DimensionRef ref="/y"/><DimensionRef ref="/x"/><SRS dataAxisToSRSAxisMapping="$mapping">EPSG:4326</SRS><BlockSize>1,2</BlockSize><InlineValues>1 2 3 4 5 6</InlineValues></Array>""")
            append("</Group></VRTDataset>")
        }
        var detached: SpatialReference? = null
        runtime.writeMemoryFile(path, xml.encodeToByteArray())
        try {
            runtime.openDataset(path, DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { group ->
                    for ((name, _) in mappings) requireNotNull(group.openArray(name)).use { source ->
                        source.getView("[:,newaxis,::-1]").use { view ->
                            assertContentEquals(longArrayOf(2, 1, 3), shape(view))
                            assertEquals(listOf(1uL, 0uL, 2uL), view.getBlockSize())
                            val expected = if (name == "negative") intArrayOf(-3, 1) else intArrayOf(0, 1)
                            requireNotNull(view.getSpatialRef()).use { assertContentEquals(expected, it.getDataAxisToSRSAxisMapping()) }
                            assertContentEquals(doubleArrayOf(3.0, 2.0, 1.0, 6.0, 5.0, 4.0),
                                view.read(longArrayOf(0, 0, 0), longArrayOf(2, 1, 3)))
                            assertFalse(view.write(longArrayOf(0, 0, 0), longArrayOf(1, 1, 1), doubleArrayOf(8.0)))
                            assertContentEquals(doubleArrayOf(1.0, 2.0, 3.0, 4.0, 5.0, 6.0),
                                source.read(longArrayOf(0, 0), longArrayOf(2, 3)))
                        }
                        source.getView("[newaxis,0,:]").use { view ->
                            val expected = if (name == "negative") intArrayOf(-2, 0) else intArrayOf(0, 0)
                            requireNotNull(view.getSpatialRef()).use { assertContentEquals(expected, it.getDataAxisToSRSAxisMapping()) }
                            if (name == "negative") detached = requireNotNull(view.getSpatialRef())
                        }
                    }
                }
            }
            assertTrue(requireNotNull(detached).isGeographic())
            assertContentEquals(intArrayOf(-2, 0), requireNotNull(detached).getDataAxisToSRSAxisMapping())
        } finally { detached?.close(); runtime.deleteMemoryFile(path) }
    }
}
