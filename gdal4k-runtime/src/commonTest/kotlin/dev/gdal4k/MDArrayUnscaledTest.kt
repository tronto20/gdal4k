package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class MDArrayUnscaledTest {
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
            for ((index, size) in sizes.withIndex()) {
                dimensions.add(requireNotNull(group.createDimension("${name}_$index", size)))
            }
            requireNotNull(group.createArray(name, dimensions, type)).use { block(it) }
        } finally {
            withContext(NonCancellable) { for (dimension in dimensions.asReversed()) dimension.close() }
        }
    }

    private fun numbers(vararg values: Int): List<MDValue> = values.map { MDValue.Numeric(it) }

    private fun assertNumbers(expected: DoubleArray, actual: DoubleArray) {
        assertEquals(expected.size, actual.size)
        for (index in expected.indices) {
            if (expected[index].isNaN()) assertTrue(actual[index].isNaN(), "sample $index must be NaN")
            else assertEquals(expected[index], actual[index], "sample $index")
        }
    }

    private suspend fun shape(value: MDArray): LongArray {
        val sizes = mutableListOf<Long>()
        for (dimension in value.getDimensions()) dimension.use { sizes.add(it.getSize()) }
        return sizes.toLongArray()
    }

    @Test fun identityViewsPreserveExactTypeAliasMetadataAndLifetime() = runTest(timeout = 120.seconds) {
        memory { group ->
            val cases = listOf(
                GdalDataType.Int64 to listOf("-9007199254740993", Long.MAX_VALUE.toString()),
                GdalDataType.UInt64 to listOf("9007199254740993", ULong.MAX_VALUE.toString()))
            for ((index, case) in cases.withIndex()) {
                val (type, texts) = case
                array(group, "identity_$index", MDDataType.Numeric(type), longArrayOf(2)) { source ->
                    val values = texts.map { MDValue.Numeric(it) }
                    assertTrue(source.writeValues(longArrayOf(0), longArrayOf(2), values))
                    // Missing metadata and explicit identity metadata both use GDAL's identity shortcut.
                    source.getUnscaled().use { view ->
                        assertEquals(MDDataType.Numeric(type), view.getExtendedDataType())
                        assertEquals(values, view.readValues(longArrayOf(0), longArrayOf(2)))
                    }
                    assertTrue(source.setScale(1.0)); assertTrue(source.setOffset(0.0))
                    assertTrue(source.setNoDataValue(values.last()))
                    val identity = source.getUnscaled()
                    val sibling = source.getUnscaled()
                    try {
                        assertEquals(values.last(), identity.getNoDataValue())
                        assertTrue(identity.setNoDataValue(null)); assertNull(source.getNoDataValue())
                        assertTrue(identity.setUnit("identity K")); assertEquals("identity K", source.getUnit())
                        // An identity wrapper aliases metadata as well as the exact stored samples.
                        assertTrue(identity.setScale(4.0)); assertEquals(4.0, source.getScale())
                        assertEquals(MDDataType.Numeric(type), identity.getExtendedDataType())
                        assertEquals(values, identity.readValues(longArrayOf(0), longArrayOf(2)))
                        identity.close(); identity.close()
                        assertEquals(values, source.readValues(longArrayOf(0), longArrayOf(2)))
                        assertEquals(values, sibling.readValues(longArrayOf(0), longArrayOf(2)))
                        source.close()
                        assertFails { sibling.getName() }
                    } finally {
                        sibling.close(); identity.close()
                    }
                }
            }
        }
    }

    @Test fun physicalReadsWritesCaptureScaleOffsetAndForwardSelectedMetadata() = runTest(timeout = 120.seconds) {
        memory { group ->
            array(group, "physical", MDDataType.Numeric(GdalDataType.Int16), longArrayOf(2, 3)) { source ->
                val raw = numbers(-4, 0, 2, 4, 6, 8)
                assertTrue(source.writeValues(longArrayOf(0, 0), longArrayOf(2, 3), raw))
                assertTrue(source.setScale(0.5)); assertTrue(source.setOffset(10.0)); assertTrue(source.setUnit("K"))
                source.getUnscaled().use { view ->
                    assertEquals(MDDataType.Numeric(GdalDataType.Float64), view.getExtendedDataType())
                    assertContentEquals(longArrayOf(2, 3), shape(view))
                    assertEquals(source.getBlockSize(), view.getBlockSize())
                    assertEquals("K", view.getUnit()); assertNull(view.getSpatialRef())
                    assertNull(view.getScale()); assertNull(view.getOffset())
                    assertNull(view.getScaleEx()); assertNull(view.getOffsetEx())
                    assertFalse(view.setScale(2.0)); assertFalse(view.setOffset(3.0))
                    assertNumbers(doubleArrayOf(8.0, 10.0, 11.0, 12.0, 13.0, 14.0),
                        view.read(longArrayOf(0, 0), longArrayOf(2, 3)))
                    assertTrue(view.write(longArrayOf(1, 1), longArrayOf(1, 1), doubleArrayOf(20.0)))
                    assertEquals(numbers(-4, 0, 2, 4, 20, 8), source.readValues(longArrayOf(0, 0), longArrayOf(2, 3)))
                    assertTrue(view.setUnit("C")); assertEquals("C", source.getUnit())
                    requireNotNull(view.createAttribute("from_view", longArrayOf(), MDDataType.Text())).use {
                        assertTrue(it.writeValues(listOf(MDValue.Text("shared attribute"))))
                    }
                    requireNotNull(source.getAttribute("from_view")).use {
                        assertEquals(listOf(MDValue.Text("shared attribute")), it.readValues())
                    }
                    assertTrue(source.setScale(3.0)); assertTrue(source.setOffset(1.0))
                    assertNumbers(doubleArrayOf(8.0, 10.0, 11.0, 12.0, 20.0, 14.0),
                        view.read(longArrayOf(0, 0), longArrayOf(2, 3)))
                    source.getUnscaled().use { fresh ->
                        assertNumbers(doubleArrayOf(-11.0, 1.0, 7.0, 13.0, 61.0, 25.0),
                            fresh.read(longArrayOf(0, 0), longArrayOf(2, 3)))
                    }
                    assertFails { view.read(longArrayOf(1, 2), longArrayOf(2, 1)) }
                    assertFails { view.write(longArrayOf(0, 3), longArrayOf(1, 1), doubleArrayOf(1.0)) }
                    assertEquals(numbers(-4, 0, 2, 4, 20, 8), source.readValues(longArrayOf(0, 0), longArrayOf(2, 3)))
                }
            }
        }
        val runtime = Gdal4k.runtime()
        val path = "/vsimem/$datasetOutputPrefix-md-unscaled-crs.vrt"
        val xml = """<VRTDataset><Group name="/"><Dimension name="n" size="2"/>
            <Array name="values"><DataType>Float64</DataType><DimensionRef ref="/n"/>
            <SRS dataAxisToSRSAxisMapping="1,0">EPSG:4326</SRS><Scale>2</Scale><Offset>1</Offset>
            <InlineValues>1 2</InlineValues></Array></Group></VRTDataset>"""
        runtime.writeMemoryFile(path, xml.encodeToByteArray())
        var detached: SpatialReference? = null
        try {
            runtime.openDataset(path, DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { group ->
                    requireNotNull(group.openArray("values")).use { source ->
                        source.getUnscaled().use { view -> detached = requireNotNull(view.getSpatialRef()) }
                    }
                }
            }
            assertTrue(requireNotNull(detached).isGeographic())
            assertContentEquals(intArrayOf(1, 0), requireNotNull(detached).getDataAxisToSRSAxisMapping())
        } finally {
            detached?.close(); runtime.deleteMemoryFile(path)
        }
    }

    @Test fun complexAndScalarPhysicalValuesFollowNativeComponentRule() = runTest(timeout = 120.seconds) {
        memory { group ->
            array(group, "complex", MDDataType.Numeric(GdalDataType.CFloat64), longArrayOf(2)) { source ->
                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(2),
                    listOf(MDValue.Numeric("1", "2"), MDValue.Numeric("3", "-4"))))
                assertTrue(source.setScale(2.0)); assertTrue(source.setOffset(1.0))
                source.getUnscaled().use { view ->
                    assertEquals(MDDataType.Numeric(GdalDataType.CFloat64), view.getExtendedDataType())
                    // GDAL applies both scale and offset to each complex component.
                    assertEquals(listOf(MDValue.Numeric("3", "5"), MDValue.Numeric("7", "-7")),
                        view.readValues(longArrayOf(0), longArrayOf(2)))
                    assertTrue(view.writeValues(longArrayOf(0), longArrayOf(1), listOf(MDValue.Numeric("9", "-3"))))
                    assertEquals(listOf(MDValue.Numeric("4", "-2"), MDValue.Numeric("3", "-4")),
                        source.readValues(longArrayOf(0), longArrayOf(2)))
                }
            }
            for ((index, type) in listOf(GdalDataType.Float64, GdalDataType.Int64).withIndex()) {
                array(group, "scalar_$index", MDDataType.Numeric(type), longArrayOf()) { source ->
                    assertTrue(source.writeValues(longArrayOf(), longArrayOf(), numbers(3)))
                    assertTrue(source.setScale(2.0)); assertTrue(source.setOffset(1.0))
                    source.getUnscaled().use { view ->
                        assertEquals(MDDataType.Numeric(GdalDataType.Float64), view.getExtendedDataType())
                        assertContentEquals(longArrayOf(), shape(view)); assertEquals(emptyList(), view.getBlockSize())
                        assertNumbers(doubleArrayOf(7.0), view.read(longArrayOf(), longArrayOf()))
                        assertTrue(view.write(longArrayOf(), longArrayOf(), doubleArrayOf(9.0)))
                        assertEquals(numbers(4), source.readValues(longArrayOf(), longArrayOf()))
                    }
                }
            }
        }
    }

    @Test fun viewAndSourceNoDataPresenceChangesRemainSafe() = runTest(timeout = 120.seconds) {
        memory { group ->
            for (initiallyPresent in listOf(true, false)) {
                for ((rank, sizes) in listOf(longArrayOf(), longArrayOf(3)).withIndex()) {
                    array(group, "presence_${initiallyPresent}_$rank", MDDataType.Numeric(GdalDataType.Int16), sizes) { source ->
                        val start = LongArray(sizes.size)
                        val count = if (sizes.isEmpty()) 1 else 3
                        val raw = numbers(-9999, 0, 2).take(count)
                        assertTrue(source.writeValues(start, sizes, raw))
                        assertTrue(source.setScale(0.5)); assertTrue(source.setOffset(10.0))
                        if (initiallyPresent) assertTrue(source.setNoDataValue(MDValue.Numeric(-9999)))
                        source.getUnscaled().use { view ->
                            if (initiallyPresent) {
                                assertNumbers(doubleArrayOf(Double.NaN, 10.0, 11.0).take(count).toDoubleArray(), view.read(start, sizes))
                            } else {
                                assertNull(view.getNoDataValue())
                                assertTrue(source.setNoDataValue(MDValue.Numeric(-9999)))
                                assertNumbers(doubleArrayOf(-4989.5, 10.0, 11.0).take(count).toDoubleArray(), view.read(start, sizes))
                            }
                            assertTrue(view.setNoDataValue(MDValue.Numeric(-777)))
                            assertNumbers(doubleArrayOf(-777.0, 10.0, 11.0).take(count).toDoubleArray(), view.read(start, sizes))
                            assertTrue(view.setNoDataValue(null)); assertNull(view.getNoDataValue())
                            assertEquals(MDValue.Numeric(-9999), source.getNoDataValue())
                            assertNumbers(doubleArrayOf(-4989.5, 10.0, 11.0).take(count).toDoubleArray(), view.read(start, sizes))
                            assertTrue(view.setNoDataValue(MDValue.Numeric(-777)))
                            assertTrue(source.setNoDataValue(null))
                            assertEquals(MDValue.Numeric(-777), view.getNoDataValue())
                            assertNumbers(doubleArrayOf(-4989.5, 10.0, 11.0).take(count).toDoubleArray(), view.read(start, sizes))
                            // Without parent NoData, the view marker follows ordinary inverse arithmetic.
                            val firstCell = LongArray(sizes.size) { 1 }
                            assertTrue(view.write(start, firstCell, doubleArrayOf(-777.0)))
                            assertEquals(numbers(-1574, 0, 2).take(count), source.readValues(start, sizes))
                            assertTrue(source.writeValues(start, sizes, raw))
                            assertTrue(source.setNoDataValue(MDValue.Numeric(0)))
                            assertNumbers(doubleArrayOf(-4989.5, -777.0, 11.0).take(count).toDoubleArray(), view.read(start, sizes))
                            assertTrue(source.setNoDataValue(MDValue.Numeric(2)))
                            assertNumbers(doubleArrayOf(-4989.5, 10.0, -777.0).take(count).toDoubleArray(), view.read(start, sizes))
                            assertTrue(source.setNoDataValue(MDValue.Numeric(-9999)))
                            assertNumbers(doubleArrayOf(-777.0, 10.0, 11.0).take(count).toDoubleArray(), view.read(start, sizes))
                        }
                    }
                }
            }
        }
    }

    @Test fun int64InverseArrayWritesUseCorrectNativeBufferType() = runTest(timeout = 120.seconds) {
        memory { group ->
            for ((index, type) in listOf(GdalDataType.Int64, GdalDataType.UInt64).withIndex()) {
                array(group, "inverse_$index", MDDataType.Numeric(type), longArrayOf(4)) { source ->
                    assertTrue(source.writeValues(longArrayOf(0), longArrayOf(4), numbers(1, 2, 3, 4)))
                    assertTrue(source.setScale(2.0)); assertTrue(source.setOffset(0.0))
                    source.getUnscaled().use { view ->
                        assertTrue(view.write(longArrayOf(0), longArrayOf(2), doubleArrayOf(6.0, 8.0)))
                        assertEquals(numbers(3, 4, 3, 4), source.readValues(longArrayOf(0), longArrayOf(4)))
                    }
                }
                array(group, "strided_$index", MDDataType.Numeric(type), longArrayOf(2, 3)) { source ->
                    assertTrue(source.writeValues(longArrayOf(0, 0), longArrayOf(2, 3), numbers(1, 2, 3, 4, 5, 6)))
                    assertTrue(source.setScale(2.0)); assertTrue(source.setOffset(0.0))
                    source.transpose(intArrayOf(1, 0)).use { transposed ->
                        transposed.getUnscaled().use { view ->
                            assertTrue(view.write(longArrayOf(0, 0), longArrayOf(3, 2),
                                doubleArrayOf(20.0, 40.0, 60.0, 80.0, 100.0, 120.0)))
                            assertEquals(numbers(10, 30, 50, 20, 40, 60), source.readValues(longArrayOf(0, 0), longArrayOf(2, 3)))
                            view.transpose(intArrayOf(1, 0)).use { nested ->
                                assertNumbers(doubleArrayOf(20.0, 60.0, 100.0, 40.0, 80.0, 120.0),
                                    nested.read(longArrayOf(0, 0), longArrayOf(2, 3)))
                            }
                            assertTrue(view.write(longArrayOf(1, 0), longArrayOf(1, 2), doubleArrayOf(14.0, 18.0)))
                            assertEquals(numbers(10, 7, 50, 20, 9, 60), source.readValues(longArrayOf(0, 0), longArrayOf(2, 3)))
                        }
                    }
                }
            }
        }
    }

    @Test fun invalidSourcesAndNestedViewsPreserveOwnership() = runTest(timeout = 120.seconds) {
        memory { group ->
            val schemas = listOf(MDDataType.Text(), MDDataType.JsonText(),
                MDDataType.Compound("unscaled_record", listOf(MDComponent("id", MDDataType.Numeric(GdalDataType.Int64)))))
            val values = listOf<MDValue>(MDValue.Text("still usable"), MDValue.Text(" { not parsed } "),
                MDValue.Record(listOf(MDValue.Numeric("9007199254740993"))))
            for ((index, schema) in schemas.withIndex()) {
                array(group, "invalid_$index", schema, longArrayOf()) { source ->
                    assertTrue(source.writeValues(longArrayOf(), longArrayOf(), listOf(values[index])))
                    assertFails { source.getUnscaled() }
                    assertEquals(listOf(values[index]), source.readValues(longArrayOf(), longArrayOf()))
                }
            }
            array(group, "owners", MDDataType.Numeric(GdalDataType.Int16), longArrayOf(2, 2)) { source ->
                assertTrue(source.writeValues(longArrayOf(0, 0), longArrayOf(2, 2), numbers(1, 2, 3, 4)))
                assertTrue(source.setScale(2.0)); assertTrue(source.setOffset(1.0))
                val first = source.getUnscaled()
                val nested = first.transpose(intArrayOf(1, 0))
                val descendant = nested.getUnscaled()
                val sibling = source.getUnscaled()
                try {
                    first.close(); first.close()
                    assertFails { nested.getName() }; assertFails { descendant.getName() }
                    assertNumbers(doubleArrayOf(3.0, 5.0, 7.0, 9.0), sibling.read(longArrayOf(0, 0), longArrayOf(2, 2)))
                    assertEquals(numbers(1, 2, 3, 4), source.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                    source.close()
                    assertFails { sibling.read(longArrayOf(0, 0), longArrayOf(2, 2)) }
                } finally {
                    sibling.close(); descendant.close(); nested.close(); first.close()
                }
            }
        }
    }

    @Test fun exactIntegerNoDataNeighborsMapAndRestoreOnlyTheSentinel() = runTest(timeout = 120.seconds) {
        memory { group ->
            val cases = listOf(
                Triple(GdalDataType.Int64, "9007199254740993", "9007199254740992"),
                Triple(GdalDataType.Int64, "-9007199254740993", "-9007199254740992"),
                Triple(GdalDataType.UInt64, ULong.MAX_VALUE.toString(), "18446744073709551614"),
                // This sentinel also rejects restoration through a rounded Double that saturates to UInt64 max.
                Triple(GdalDataType.UInt64, "18446744073709551613", "18446744073709551612"))
            for ((caseIndex, case) in cases.withIndex()) {
                val (type, sentinel, neighbor) = case
                for ((rank, sizes) in listOf(longArrayOf(), longArrayOf(3), longArrayOf(2, 2)).withIndex()) {
                    array(group, "exact_${caseIndex}_$rank", MDDataType.Numeric(type), sizes) { source ->
                        val count = sizes.fold(1L) { product, size -> product * size }.toInt()
                        val raw = listOf(sentinel, "7", neighbor, "11").take(count).map { MDValue.Numeric(it) }
                        val start = LongArray(sizes.size)
                        assertTrue(source.writeValues(start, sizes, raw))
                        assertTrue(source.setNoDataValue(MDValue.Numeric(sentinel)))
                        assertTrue(source.setScale(2.0)); assertTrue(source.setOffset(1.0))
                        source.getUnscaled().use { view ->
                            for (finite in listOf(false, true)) {
                                assertTrue(source.writeValues(start, sizes, raw))
                                if (finite) assertTrue(view.setNoDataValue(MDValue.Numeric(-777)))
                                val physical = view.read(start, sizes)
                                if (finite) assertEquals(-777.0, physical[0]) else assertTrue(physical[0].isNaN())
                                for (index in 1 until count) {
                                    assertEquals(raw[index].let { (it as MDValue.Numeric).real.toDouble() } * 2.0 + 1.0, physical[index])
                                    assertFalse(physical[index].isNaN(), "valid integer neighbor must not be classified as NoData")
                                }
                                val window = when (sizes.size) {
                                    0 -> longArrayOf()
                                    1 -> longArrayOf(2)
                                    else -> longArrayOf(1, 2)
                                }
                                val missing = if (finite) -777.0 else Double.NaN
                                val writes = if (count == 1) doubleArrayOf(missing) else doubleArrayOf(missing, 9.0)
                                assertTrue(view.write(start, window, writes))
                                val expected = raw.toMutableList().also { if (count > 1) it[1] = MDValue.Numeric(4) }
                                assertEquals(expected, source.readValues(start, sizes))
                                assertEquals(MDValue.Numeric(sentinel), source.getNoDataValue())
                            }
                        }
                    }
                }
            }
            array(group, "finite_collision", MDDataType.Numeric(GdalDataType.Float64), longArrayOf()) { source ->
                assertTrue(source.writeValues(longArrayOf(), longArrayOf(), numbers(3)))
                assertTrue(source.setScale(2.0)); assertTrue(source.setOffset(1.0))
                assertTrue(source.setNoDataValue(MDValue.Numeric(-9999)))
                source.getUnscaled().use { view ->
                    assertTrue(view.setNoDataValue(MDValue.Numeric(7)))
                    assertNumbers(doubleArrayOf(7.0), view.read(longArrayOf(), longArrayOf()))
                    // A finite view marker can collide with valid physical data: inverse writes are ambiguous.
                    assertTrue(view.write(longArrayOf(), longArrayOf(), doubleArrayOf(7.0)))
                    assertEquals(numbers(-9999), source.readValues(longArrayOf(), longArrayOf()))
                }
            }
        }
    }

    @Test fun complexAndNaNSentinelsPreserveUpstreamClassification() = runTest(timeout = 120.seconds) {
        memory { group ->
            for ((rank, sizes) in listOf(longArrayOf(), longArrayOf(3)).withIndex()) {
                array(group, "complex_nodata_$rank", MDDataType.Numeric(GdalDataType.CFloat64), sizes) { source ->
                    val raw = listOf(MDValue.Numeric("1", if (sizes.isEmpty()) "3" else "2"), MDValue.Numeric("1", "3"), MDValue.Numeric("2", "4"))
                        .take(if (sizes.isEmpty()) 1 else 3)
                    val start = LongArray(sizes.size)
                    assertTrue(source.writeValues(start, sizes, raw))
                    assertTrue(source.setNoDataValue(MDValue.Numeric("1", "2")))
                    assertTrue(source.setScale(2.0)); assertTrue(source.setOffset(1.0))
                    source.getUnscaled().use { view ->
                        val marker = MDValue.Numeric("-777", "-888")
                        assertTrue(view.setNoDataValue(marker))
                        val physical = view.readValues(start, sizes)
                        assertEquals(marker, physical[0])
                        if (raw.size > 1) {
                            // GDAL's NoData predicate uses the real component only.
                            assertEquals(marker, physical[1]); assertEquals(MDValue.Numeric("5", "9"), physical[2])
                        }
                        val writes = listOf(MDValue.Numeric("-777", "999"), MDValue.Numeric("NaN", "42"), MDValue.Numeric("9", "13"))
                            .take(raw.size)
                        assertTrue(view.writeValues(start, sizes, writes))
                        val expected = listOf(MDValue.Numeric("1", "2"), MDValue.Numeric("1", "2"), MDValue.Numeric("4", "6"))
                            .take(raw.size)
                        assertEquals(expected, source.readValues(start, sizes))
                        assertTrue(view.writeValues(start, sizes, List(raw.size) { MDValue.Numeric("NaN", "42") }))
                        assertEquals(List(raw.size) { MDValue.Numeric("1", "2") }, source.readValues(start, sizes))
                        assertTrue(view.setNoDataValue(null)); assertNull(view.getNoDataValue())
                        assertEquals(MDValue.Numeric("1", "2"), source.getNoDataValue())
                        assertEquals(MDValue.Numeric("3", "5"), view.readValues(start, sizes).first())
                    }
                }
            }
            array(group, "source_nan", MDDataType.Numeric(GdalDataType.CFloat64), longArrayOf(2)) { source ->
                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(2),
                    listOf(MDValue.Numeric("NaN", "2"), MDValue.Numeric("3", "4"))))
                assertTrue(source.setNoDataValue(MDValue.Numeric("NaN", "2")))
                assertTrue(source.setScale(2.0)); assertTrue(source.setOffset(1.0))
                source.getUnscaled().use { view ->
                    assertTrue(view.setNoDataValue(MDValue.Numeric("-777", "-888")))
                    val first = view.readValues(longArrayOf(0), longArrayOf(1)).single() as MDValue.Numeric
                    // Ordinary read equality does not introduce NaN-equals-NaN remapping.
                    assertTrue(first.real.toDouble().isNaN()); assertEquals(5.0, requireNotNull(first.imaginary).toDouble())
                    assertTrue(view.writeValues(longArrayOf(0), longArrayOf(1), listOf(MDValue.Numeric("NaN", "42"))))
                    val restored = source.readValues(longArrayOf(0), longArrayOf(1)).single() as MDValue.Numeric
                    assertTrue(restored.real.toDouble().isNaN()); assertEquals(2.0, requireNotNull(restored.imaginary).toDouble())
                }
            }
        }
    }

    @Test fun zeroAndNonfiniteMetadataPreserveIEEEAndMissingWrites() = runTest(timeout = 120.seconds) {
        memory { group ->
            val cases = listOf(
                0.0 to 10.0, Double.POSITIVE_INFINITY to 1.0, 2.0 to Double.POSITIVE_INFINITY,
                Double.NaN to 1.0, 2.0 to Double.NaN)
            for ((caseIndex, metadata) in cases.withIndex()) {
                val (scale, offset) = metadata
                for ((rank, sizes) in listOf(longArrayOf(), longArrayOf(2)).withIndex()) {
                    array(group, "ieee_${caseIndex}_$rank", MDDataType.Numeric(GdalDataType.Float64), sizes) { source ->
                        val start = LongArray(sizes.size)
                        val count = if (sizes.isEmpty()) 1 else 2
                        val raw = doubleArrayOf(0.0, 2.0).take(count).toDoubleArray()
                        assertTrue(source.write(start, sizes, raw))
                        assertTrue(source.setNoDataValue(MDValue.Numeric(-9999)))
                        assertTrue(source.setScale(scale)); assertTrue(source.setOffset(offset))
                        source.getUnscaled().use { view ->
                            assertNumbers(raw.map { it * scale + offset }.toDoubleArray(), view.read(start, sizes))
                            val physical = (if (caseIndex == 0) doubleArrayOf(10.0, 12.0) else doubleArrayOf(5.0, 6.0))
                                .take(count).toDoubleArray()
                            assertTrue(view.write(start, sizes, physical))
                            assertNumbers(physical.map { (it - offset) / scale }.toDoubleArray(), source.read(start, sizes))
                            // Classified missing writes restore NoData without inverse arithmetic, even for scale zero/NaN.
                            assertTrue(view.write(start, sizes, DoubleArray(count) { Double.NaN }))
                            assertNumbers(DoubleArray(count) { -9999.0 }, source.read(start, sizes))
                        }
                    }
                }
            }
        }
    }
}
