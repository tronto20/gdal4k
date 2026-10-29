package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class MDArrayTransposeApiTest {
    private suspend fun memory(block: suspend (Group) -> Unit) {
        requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
            requireNotNull(driver.createMultidimensional("")).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { block(it) }
            }
        }
    }

    private suspend fun matrix(group: Group, name: String, type: MDDataType,
                               rows: Long = 2, columns: Long = 4, block: suspend (MDArray) -> Unit) {
        requireNotNull(group.createDimension("${name}_y", rows)).use { y ->
            requireNotNull(group.createDimension("${name}_x", columns)).use { x ->
                requireNotNull(group.createArray(name, listOf(y, x), type)).use { block(it) }
            }
        }
    }

    private suspend fun shape(array: MDArray): LongArray {
        val sizes = mutableListOf<Long>()
        for (dimension in array.getDimensions()) dimension.use { sizes.add(it.getSize()) }
        return sizes.toLongArray()
    }

    @Test fun typedTransposeAliasesPreserveExactValuesInsertedAxesAndScalars() = runTest(timeout = 120.seconds) {
        memory { group ->
            val signed = listOf(Long.MIN_VALUE.toString(), "-9007199254740993", "-1", "0",
                "9007199254740993", Long.MAX_VALUE.toString(), "17", "-42").map { MDValue.Numeric(it) }
            matrix(group, "signed", MDDataType.Numeric(GdalDataType.Int64)) { array ->
                assertTrue(array.writeValues(longArrayOf(0, 0), longArrayOf(2, 4), signed))
                array.transpose(intArrayOf(1, 0)).use { transposed ->
                    array.transpose(intArrayOf(-1, 1, 0, -1)).use { expanded ->
                        val reordered = listOf(0, 4, 1, 5, 2, 6, 3, 7).map { signed[it] }
                        assertContentEquals(longArrayOf(4, 2), shape(transposed))
                        assertContentEquals(longArrayOf(1, 4, 2, 1), shape(expanded))
                        assertEquals(MDDataType.Numeric(GdalDataType.Int64), transposed.getExtendedDataType())
                        assertEquals(reordered, transposed.readValues(longArrayOf(0, 0), longArrayOf(4, 2)))
                        assertEquals(reordered, expanded.readValues(longArrayOf(0, 0, 0, 0), longArrayOf(1, 4, 2, 1)))

                        val changed = MDValue.Numeric("9007199254740995")
                        assertTrue(transposed.writeValues(longArrayOf(2, 1), longArrayOf(1, 1), listOf(changed)))
                        val sourceAfterWrite = signed.toMutableList().also { it[6] = changed }
                        assertEquals(sourceAfterWrite, array.readValues(longArrayOf(0, 0), longArrayOf(2, 4)))
                        assertEquals(listOf(changed), expanded.readValues(longArrayOf(0, 2, 1, 0), longArrayOf(1, 1, 1, 1)))
                        val parentWrite = MDValue.Numeric("-9007199254740995")
                        assertTrue(array.writeValues(longArrayOf(0, 3), longArrayOf(1, 1), listOf(parentWrite)))
                        assertEquals(listOf(parentWrite), transposed.readValues(longArrayOf(3, 0), longArrayOf(1, 1)))
                        assertEquals(listOf(parentWrite), expanded.readValues(longArrayOf(0, 3, 0, 0), longArrayOf(1, 1, 1, 1)))
                    }
                }
            }
            val unsigned = listOf("0", "9007199254740993", "18446744073709551614", "18446744073709551615").map { MDValue.Numeric(it) }
            matrix(group, "unsigned", MDDataType.Numeric(GdalDataType.UInt64), columns = 2) { array ->
                assertTrue(array.writeValues(longArrayOf(0, 0), longArrayOf(2, 2), unsigned))
                array.transpose(intArrayOf(1, 0)).use { view ->
                    assertEquals(MDDataType.Numeric(GdalDataType.UInt64), view.getExtendedDataType())
                    assertEquals(listOf(unsigned[0], unsigned[2], unsigned[1], unsigned[3]),
                        view.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                }
            }
            requireNotNull(group.createArray("scalar", emptyList(), GdalDataType.Int64)).use { scalar ->
                assertTrue(scalar.writeValues(longArrayOf(), longArrayOf(), listOf(MDValue.Numeric(Long.MAX_VALUE))))
                scalar.transpose(intArrayOf()).use { identity ->
                    assertContentEquals(longArrayOf(), shape(identity))
                    assertEquals(emptyList(), identity.getBlockSize())
                    assertEquals(listOf(MDValue.Numeric(Long.MAX_VALUE)), identity.readValues(longArrayOf(), longArrayOf()))
                }
                scalar.transpose(intArrayOf(-1)).use { expanded ->
                    assertContentEquals(longArrayOf(1), shape(expanded))
                    assertEquals(listOf(0uL), expanded.getBlockSize())
                    val changed = MDValue.Numeric("-9007199254740993")
                    assertTrue(expanded.writeValues(longArrayOf(0), longArrayOf(1), listOf(changed)))
                    assertEquals(listOf(changed), scalar.readValues(longArrayOf(), longArrayOf()))
                }
            }
        }
    }

    @Test fun complexAndJsonCompoundViewsForwardValuesAndMetadata() = runTest(timeout = 120.seconds) {
        memory { group ->
            val complex = listOf(MDValue.Numeric("1.25", "-2.5"), MDValue.Numeric("3.5", "4.75"),
                MDValue.Numeric("-5.25", "6.5"), MDValue.Numeric("7.75", "-8"))
            matrix(group, "complex", MDDataType.Numeric(GdalDataType.CFloat64), columns = 2) { array ->
                assertTrue(array.writeValues(longArrayOf(0, 0), longArrayOf(2, 2), complex))
                assertTrue(array.setNoDataValue(complex[0]))
                assertTrue(array.setScale(0.5)); assertTrue(array.setOffset(10.0)); assertTrue(array.setUnit("K"))
                array.transpose(intArrayOf(1, 0)).use { view ->
                    assertEquals(MDDataType.Numeric(GdalDataType.CFloat64), view.getExtendedDataType())
                    assertEquals(listOf(complex[0], complex[2], complex[1], complex[3]),
                        view.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                    assertEquals(complex[0], view.getNoDataValue())
                    assertEquals(MDNumericMetadata(0.5, GdalDataType.Unknown), view.getScaleEx())
                    assertEquals(MDNumericMetadata(10.0, GdalDataType.Unknown), view.getOffsetEx())
                    assertEquals("K", view.getUnit())
                    // These view metadata setters use GDAL's unsupported default implementation.
                    assertFalse(view.setNoDataValue(null)); assertFalse(view.setScale(7.0))
                    assertFalse(view.setOffset(99.0)); assertFalse(view.setUnit("changed"))
                    assertEquals(complex[0], array.getNoDataValue())
                    assertEquals(0.5, array.getScale()); assertEquals(10.0, array.getOffset()); assertEquals("K", array.getUnit())

                    val changed = MDValue.Numeric("9.5", "-11.25")
                    assertTrue(view.writeValues(longArrayOf(1, 0), longArrayOf(1, 1), listOf(changed)))
                    assertEquals(listOf(complex[0], changed, complex[2], complex[3]),
                        array.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                    assertTrue(array.setNoDataValue(changed)); assertEquals(changed, view.getNoDataValue())
                    assertTrue(array.setNoDataValue(null)); assertNull(view.getNoDataValue())
                }
            }
            val schema = MDDataType.Compound("transpose_record", listOf(
                MDComponent("id", MDDataType.Numeric(GdalDataType.Int64)), MDComponent("json", MDDataType.JsonText())))
            val records = listOf(
                MDValue.Record(listOf(MDValue.Numeric(Long.MIN_VALUE), MDValue.Text(null))),
                MDValue.Record(listOf(MDValue.Numeric("9007199254740993"), MDValue.Text("null"))),
                MDValue.Record(listOf(MDValue.Numeric(Long.MAX_VALUE), MDValue.Text(" \n" + """{"label":"영상 🛰","id":9007199254740993}""" + "\t "))),
                MDValue.Record(listOf(MDValue.Numeric("-9007199254740993"), MDValue.Text("{ raw text is not parsed }"))))
            matrix(group, "records", schema, columns = 2) { array ->
                assertTrue(array.writeValues(longArrayOf(0, 0), longArrayOf(2, 2), records))
                array.transpose(intArrayOf(1, 0)).use { view ->
                    assertEquals(schema, view.getExtendedDataType())
                    assertEquals(listOf(records[0], records[2], records[1], records[3]),
                        view.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                    val changed = MDValue.Record(listOf(MDValue.Numeric("9007199254740995"), MDValue.Text(" [true,null] ")))
                    assertTrue(view.writeValues(longArrayOf(1, 0), longArrayOf(1, 1), listOf(changed)))
                    assertEquals(listOf(records[0], changed, records[2], records[3]),
                        array.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                }
            }
        }
    }

    @Test fun vrtSignedMappingsBlocksAndCrsCopiesFollowTranspose() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        val path = "/vsimem/$datasetOutputPrefix-md-transpose.vrt"
        val mappings = listOf("negative" to "-2,1", "positive" to "2,1", "zero" to "0,1",
            "minimum" to "-2147483648,1", "outside" to "3,1")
        val xml = buildString {
            append("""<VRTDataset><Group name="/"><Dimension name="y" size="2"/><Dimension name="x" size="3"/>""")
            for ((name, mapping) in mappings) append("""<Array name="$name"><DataType>Float64</DataType><DimensionRef ref="/y"/><DimensionRef ref="/x"/><SRS dataAxisToSRSAxisMapping="$mapping">EPSG:4326</SRS><BlockSize>1,2</BlockSize><InlineValues>1 2 3 4 5 6</InlineValues></Array>""")
            append("""<Array name="unlocated"><DataType>Float64</DataType><DimensionRef ref="/y"/><DimensionRef ref="/x"/></Array></Group></VRTDataset>""")
        }
        runtime.writeMemoryFile(path, xml.encodeToByteArray())
        var detached: SpatialReference? = null
        var expandedCrs: SpatialReference? = null
        try {
            runtime.openDataset(path, DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { group ->
                    requireNotNull(group.openArray("negative")).use { array ->
                        array.transpose(intArrayOf(1, 0)).use { view ->
                            assertContentEquals(longArrayOf(3, 2), shape(view))
                            assertEquals(listOf(2uL, 1uL), view.getBlockSize())
                            assertContentEquals(doubleArrayOf(1.0, 4.0, 2.0, 5.0, 3.0, 6.0),
                                view.read(longArrayOf(0, 0), longArrayOf(3, 2)))
                            detached = requireNotNull(view.getSpatialRef())
                            assertContentEquals(intArrayOf(-1, 2), requireNotNull(detached).getDataAxisToSRSAxisMapping())
                            requireNotNull(view.getSpatialRef()).use { mutableCopy ->
                                mutableCopy.setAxisMappingStrategy(0)
                                assertContentEquals(intArrayOf(-1, 2), requireNotNull(detached).getDataAxisToSRSAxisMapping())
                            }
                        }
                        array.transpose(intArrayOf(-1, 1, 0, -1)).use { expanded ->
                            assertContentEquals(longArrayOf(1, 3, 2, 1), shape(expanded))
                            assertEquals(listOf(0uL, 2uL, 1uL, 0uL), expanded.getBlockSize())
                            expandedCrs = requireNotNull(expanded.getSpatialRef())
                            assertContentEquals(intArrayOf(-2, 3), requireNotNull(expandedCrs).getDataAxisToSRSAxisMapping())
                        }
                        requireNotNull(array.getSpatialRef()).use {
                            assertContentEquals(intArrayOf(-2, 1), it.getDataAxisToSRSAxisMapping())
                        }
                    }
                    for ((name, original) in mappings.drop(1)) requireNotNull(group.openArray(name)).use { array ->
                        array.transpose(intArrayOf(-1, 1, 0)).use { view ->
                            val expected = if (name == "positive") intArrayOf(2, 3) else intArrayOf(0, 3)
                            requireNotNull(view.getSpatialRef()).use { assertContentEquals(expected, it.getDataAxisToSRSAxisMapping()) }
                        }
                        requireNotNull(array.getSpatialRef()).use {
                            assertContentEquals(original.split(',').map { value -> value.toInt() }.toIntArray(), it.getDataAxisToSRSAxisMapping())
                        }
                    }
                    requireNotNull(group.openArray("unlocated")).use { array ->
                        array.transpose(intArrayOf(1, 0)).use { assertNull(it.getSpatialRef()) }
                    }
                }
            }
            // The two independently owned CRS copies survive every originating GDAL handle.
            assertTrue(requireNotNull(detached).isGeographic())
            assertContentEquals(intArrayOf(-1, 2), requireNotNull(detached).getDataAxisToSRSAxisMapping())
            assertTrue(requireNotNull(expandedCrs).isGeographic())
            assertContentEquals(intArrayOf(-2, 3), requireNotNull(expandedCrs).getDataAxisToSRSAxisMapping())
        } finally {
            detached?.close(); expandedCrs?.close()
            runtime.deleteMemoryFile(path)
        }
    }

    @Test fun invalidMappingsAndClosedParentsKeepOtherViewsUsable() = runTest(timeout = 120.seconds) {
        memory { group ->
            matrix(group, "owners", MDDataType.Numeric(GdalDataType.Int16), columns = 2) { array ->
                val values = listOf(1, 2, 3, 4).map { MDValue.Numeric(it) }
                assertTrue(array.writeValues(longArrayOf(0, 0), longArrayOf(2, 2), values))
                val identity = array.transpose(intArrayOf(0, 1))
                val nested = identity.transpose(intArrayOf(1, 0))
                val sibling = array.transpose(intArrayOf(1, 0))
                val descendant = sibling.transpose(intArrayOf(1, 0))
                try {
                    for (invalid in listOf(intArrayOf(-2, 0, 1), intArrayOf(0, 2), intArrayOf(0, 0), intArrayOf(0), intArrayOf())) {
                        assertFails { array.transpose(invalid) }
                        assertEquals(values, array.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                        assertEquals(listOf(values[0], values[2], values[1], values[3]),
                            sibling.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                    }
                    identity.close(); identity.close()
                    assertFails { nested.getName() }
                    assertEquals(values, array.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                    assertEquals(values, descendant.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                    array.close()
                    assertFails { sibling.getName() }; assertFails { descendant.readValues(longArrayOf(0, 0), longArrayOf(2, 2)) }
                    requireNotNull(group.createArray("survivor", emptyList(), GdalDataType.Int16)).use { survivor ->
                        assertTrue(survivor.writeValues(longArrayOf(), longArrayOf(), listOf(MDValue.Numeric(7))))
                        assertEquals(listOf(MDValue.Numeric(7)), survivor.readValues(longArrayOf(), longArrayOf()))
                    }
                } finally {
                    descendant.close(); sibling.close(); nested.close(); identity.close()
                }
            }
        }
    }
}
