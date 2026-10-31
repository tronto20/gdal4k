package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class MDArrayMaskApiTest {
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

    private fun values(vararg text: String): List<MDValue> = text.map { MDValue.Numeric(it) }
    private fun flags(vararg flags: Int): List<MDValue> = flags.map { MDValue.Numeric(it) }

    private suspend fun attribute(source: MDArray, name: String, type: Int, dimensions: LongArray,
                                  values: List<MDValue>) {
        requireNotNull(source.createAttribute(name, dimensions, MDDataType.Numeric(type))).use {
            assertTrue(it.writeValues(values))
        }
    }

    @Test fun criteriaAreCombinedAndAttributesAreCapturedWhileNoDataStaysLive() = runTest(timeout = 120.seconds) {
        memory { group ->
            array(group, "criteria", MDDataType.Numeric(GdalDataType.Float64), longArrayOf(8)) { source ->
                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(8),
                    values("-9999", "-9998", "-9997", "-1", "0", "4", "5", "NaN")))
                assertTrue(source.setNoDataValue(MDValue.Numeric(-9999)))
                attribute(source, "missing_value", GdalDataType.Float64, longArrayOf(), values("-9998"))
                attribute(source, "_FillValue", GdalDataType.Float64, longArrayOf(1), values("-9997"))
                attribute(source, "valid_min", GdalDataType.Float64, longArrayOf(), values("2"))
                attribute(source, "valid_max", GdalDataType.Float64, longArrayOf(), values("3"))
                attribute(source, "valid_range", GdalDataType.Float64, longArrayOf(2), values("0", "4"))
                source.getMask().use { mask ->
                    assertEquals(MDDataType.Numeric(GdalDataType.Byte), mask.getExtendedDataType())
                    assertEquals(flags(0, 0, 0, 0, 1, 1, 0, 0), mask.readValues(longArrayOf(0), longArrayOf(8)))
                    assertContentEquals(doubleArrayOf(0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0),
                        mask.read(longArrayOf(0), longArrayOf(8)))
                    assertFailsWith<GdalException> { mask.writeValues(longArrayOf(0), longArrayOf(1), flags(1)) }
                }
            }
            array(group, "live", MDDataType.Numeric(GdalDataType.Int16), longArrayOf(2)) { source ->
                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(2), values("2", "4")))
                attribute(source, "valid_range", GdalDataType.Int16, longArrayOf(2), values("2", "4"))
                source.getMask().use { old ->
                    requireNotNull(source.getAttribute("valid_range")).use { assertTrue(it.writeValues(values("3", "4"))) }
                    assertEquals(flags(1, 1), old.readValues(longArrayOf(0), longArrayOf(2)))
                    source.getMask().use { fresh -> assertEquals(flags(0, 1), fresh.readValues(longArrayOf(0), longArrayOf(2))) }
                    assertTrue(source.setNoDataValue(MDValue.Numeric(4)))
                    assertEquals(flags(1, 0), old.readValues(longArrayOf(0), longArrayOf(2)))
                    assertTrue(source.setNoDataValue(null))
                    assertEquals(flags(1, 1), old.readValues(longArrayOf(0), longArrayOf(2)))
                    assertEquals(values("2", "4"), source.readValues(longArrayOf(0), longArrayOf(2)))
                }
            }
        }
    }

    @Test fun exactIntegerNoDataDistinguishesNeighborsAndExtremaAcrossViews() = runTest(timeout = 120.seconds) {
        memory { group ->
            val cases = listOf(
                Triple(GdalDataType.Int64, "9007199254740993", "9007199254740992"),
                Triple(GdalDataType.Int64, "-9007199254740993", "-9007199254740992"),
                Triple(GdalDataType.Int64, Long.MAX_VALUE.toString(), (Long.MAX_VALUE - 1).toString()),
                Triple(GdalDataType.Int64, Long.MIN_VALUE.toString(), (Long.MIN_VALUE + 1).toString()),
                Triple(GdalDataType.UInt64, ULong.MAX_VALUE.toString(), (ULong.MAX_VALUE - 1uL).toString()),
                Triple(GdalDataType.UInt64, (ULong.MAX_VALUE - 2uL).toString(), (ULong.MAX_VALUE - 3uL).toString()))
            for ((index, case) in cases.withIndex()) {
                val (type, sentinel, neighbor) = case
                array(group, "exact_$index", MDDataType.Numeric(type), longArrayOf(2, 2)) { source ->
                    val raw = values(sentinel, neighbor, neighbor, sentinel)
                    assertTrue(source.writeValues(longArrayOf(0, 0), longArrayOf(2, 2), raw))
                    assertTrue(source.setNoDataValue(MDValue.Numeric(sentinel)))
                    source.getMask().use { mask ->
                        assertEquals(flags(0, 1, 1, 0), mask.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                        assertTrue(source.setNoDataValue(MDValue.Numeric(neighbor)))
                        assertEquals(flags(1, 0, 0, 1), mask.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                        assertTrue(source.setNoDataValue(null))
                        assertEquals(flags(1, 1, 1, 1), mask.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                    }
                    assertTrue(source.setNoDataValue(MDValue.Numeric(sentinel)))
                    source.getView("[::-1,0]").use { slice -> slice.getMask().use { mask ->
                        assertEquals(flags(1, 0), mask.readValues(longArrayOf(0), longArrayOf(2)))
                    } }
                    source.transpose(intArrayOf(1, 0)).use { transposed -> transposed.getMask().use { mask ->
                        assertEquals(flags(0, 1, 1, 0), mask.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                    } }
                    source.getView("[0,0]").use { scalar -> scalar.getMask().use { mask ->
                        assertEquals(flags(0), mask.readValues(longArrayOf(), longArrayOf()))
                    } }
                    assertEquals(raw, source.readValues(longArrayOf(0, 0), longArrayOf(2, 2)))
                }
            }
        }
    }

    @Test fun typed64BitCfSentinelsAndRangeBoundsRemainExact() = runTest(timeout = 120.seconds) {
        memory { group ->
            for ((index, type) in listOf(GdalDataType.Int64, GdalDataType.UInt64).withIndex()) {
                array(group, "cf_$index", MDDataType.Numeric(type), longArrayOf(5)) { source ->
                    val raw = values("9007199254740992", "9007199254740993", "9007199254740994", "9007199254740995", "9007199254740996")
                    assertTrue(source.writeValues(longArrayOf(0), longArrayOf(5), raw))
                    attribute(source, "missing_value", type, longArrayOf(), values("9007199254740993"))
                    attribute(source, "_FillValue", type, longArrayOf(1), values("9007199254740995"))
                    attribute(source, "valid_range", type, longArrayOf(2), values("9007199254740993", "9007199254740995"))
                    source.getMask().use { assertEquals(flags(0, 0, 1, 0, 0), it.readValues(longArrayOf(0), longArrayOf(5))) }
                    assertTrue(source.deleteAttribute("valid_range"))
                    assertTrue(source.deleteAttribute("missing_value")); assertTrue(source.deleteAttribute("_FillValue"))
                    attribute(source, "valid_min", type, longArrayOf(), values("9007199254740993"))
                    attribute(source, "valid_max", type, longArrayOf(), values("9007199254740995"))
                    source.getMask().use { assertEquals(flags(0, 1, 1, 1, 0), it.readValues(longArrayOf(0), longArrayOf(5))) }
                }
            }
            for ((index, case) in listOf(GdalDataType.Int64 to Long.MAX_VALUE.toString(),
                GdalDataType.UInt64 to ULong.MAX_VALUE.toString()).withIndex()) {
                val (type, maximum) = case
                array(group, "cf_max_$index", MDDataType.Numeric(type), longArrayOf(1)) { source ->
                    assertTrue(source.writeValues(longArrayOf(0), longArrayOf(1), values(maximum)))
                    attribute(source, "missing_value", type, longArrayOf(), values(maximum))
                    source.getMask().use { assertEquals(flags(0), it.readValues(longArrayOf(0), longArrayOf(1))) }
                    assertTrue(source.deleteAttribute("missing_value"))
                    // These doubles equal the exclusive integer upper bound and must not be cast.
                    val outside = if (type == GdalDataType.Int64) "9223372036854775808" else "18446744073709551616"
                    attribute(source, "missing_value", GdalDataType.Float64, longArrayOf(), values(outside))
                    source.getMask().use { assertEquals(flags(1), it.readValues(longArrayOf(0), longArrayOf(1))) }
                }
            }
            array(group, "cross_signed", MDDataType.Numeric(GdalDataType.UInt64), longArrayOf(1)) { source ->
                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(1), values("0")))
                attribute(source, "missing_value", GdalDataType.Int64, longArrayOf(), values("-1"))
                source.getMask().use { assertEquals(flags(1), it.readValues(longArrayOf(0), longArrayOf(1))) }
            }
        }
    }

    @Test fun exact64CriteriaCanCoexistWithSupported32BitFlagMetadata() = runTest(timeout = 120.seconds) {
        memory { group ->
            array(group, "mixed", MDDataType.Numeric(GdalDataType.UInt64), longArrayOf(2)) { source ->
                val raw = values("9007199254740993", "9007199254740992")
                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(2), raw))
                attribute(source, "missing_value", GdalDataType.Int64, longArrayOf(), values("9007199254740993"))
                requireNotNull(source.createAttribute("flag_meanings", longArrayOf(), MDDataType.Text())).use {
                    assertTrue(it.writeValues(listOf(MDValue.Text("one zero"))))
                }
                attribute(source, "flag_values", GdalDataType.UInt32, longArrayOf(2), values("1", "0"))
                source.getMask(listOf("UNMASK_FLAGS=one,zero")).use {
                    assertEquals(flags(0, 1), it.readValues(longArrayOf(0), longArrayOf(2)))
                }
                assertTrue(source.deleteAttribute("flag_values"))
                attribute(source, "flag_values", GdalDataType.UInt64, longArrayOf(2), values("1", "0"))
                assertFailsWith<GdalException> { source.getMask(listOf("UNMASK_FLAGS=one,zero")) }
                assertEquals(raw, source.readValues(longArrayOf(0), longArrayOf(2)))
            }
            array(group, "unsigned_criterion", MDDataType.Numeric(GdalDataType.Int64), longArrayOf(1)) { source ->
                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(1), values("0")))
                attribute(source, "missing_value", GdalDataType.UInt64, longArrayOf(), values(ULong.MAX_VALUE.toString()))
                source.getMask().use { assertEquals(flags(1), it.readValues(longArrayOf(0), longArrayOf(1))) }
            }
        }
    }

    @Test fun nanAndComplexMaskingUseTheRealComponentAndFlagsStayDefined() = runTest(timeout = 120.seconds) {
        memory { group ->
            for ((index, type) in listOf(GdalDataType.Float16, GdalDataType.Float32, GdalDataType.Float64).withIndex()) {
                array(group, "floating_$index", MDDataType.Numeric(type), longArrayOf(3)) { source ->
                    assertTrue(source.writeValues(longArrayOf(0), longArrayOf(3), values("NaN", "Infinity", "2")))
                    source.getMask().use { assertEquals(flags(0, 1, 1), it.readValues(longArrayOf(0), longArrayOf(3))) }
                }
            }
            array(group, "complex", MDDataType.Numeric(GdalDataType.CFloat64), longArrayOf(4)) { source ->
                val raw = listOf(MDValue.Numeric("1", "2"), MDValue.Numeric("1", "3"),
                    MDValue.Numeric("2", "NaN"), MDValue.Numeric("NaN", "2"))
                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(4), raw))
                assertTrue(source.setNoDataValue(MDValue.Numeric("1", "2")))
                source.getMask().use { assertEquals(flags(0, 0, 1, 0), it.readValues(longArrayOf(0), longArrayOf(4))) }
            }
            array(group, "flag_floating", MDDataType.Numeric(GdalDataType.Float64), longArrayOf(6)) { source ->
                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(6), values("Infinity", "NaN", "-2", "4294967296", "1.5", "-0.5")))
                requireNotNull(source.createAttribute("flag_meanings", longArrayOf(), MDDataType.Text())).use {
                    assertTrue(it.writeValues(listOf(MDValue.Text("selected"))))
                }
                attribute(source, "flag_values", GdalDataType.UInt32, longArrayOf(1), values("1"))
                source.getMask(listOf("UNMASK_FLAGS=selected")).use {
                    assertEquals(flags(0, 0, 0, 0, 1, 0), it.readValues(longArrayOf(0), longArrayOf(6)))
                }
            }
        }
    }

    @Test fun cfFlagFormsRejectInvalidMetadataAndPreserveSourceLifetime() = runTest(timeout = 120.seconds) {
        memory { group ->
            array(group, "flags", MDDataType.Numeric(GdalDataType.UInt16), longArrayOf(6)) { source ->
                val raw = values("0", "1", "2", "3", "4", "8")
                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(6), raw))
                requireNotNull(source.createAttribute("flag_meanings", longArrayOf(), MDDataType.Text())).use {
                    assertTrue(it.writeValues(listOf(MDValue.Text("land ice water"))))
                }
                attribute(source, "flag_masks", GdalDataType.UInt16, longArrayOf(3), values("1", "2", "4"))
                val mask = source.getMask(listOf("UNMASK_FLAGS=land,water"))
                val sibling = source.getView("[:]")
                try {
                    assertEquals(flags(0, 1, 0, 1, 1, 0), mask.readValues(longArrayOf(0), longArrayOf(6)))
                    attribute(source, "flag_values", GdalDataType.UInt16, longArrayOf(3), values("1", "2", "4"))
                    source.getMask(listOf("UNMASK_FLAGS=land,water")).use {
                        assertEquals(flags(0, 1, 0, 1, 1, 0), it.readValues(longArrayOf(0), longArrayOf(6)))
                    }
                    assertTrue(source.deleteAttribute("flag_masks"))
                    source.getMask(listOf("UNMASK_FLAGS=land,water")).use {
                        assertEquals(flags(0, 1, 0, 0, 1, 0), it.readValues(longArrayOf(0), longArrayOf(6)))
                    }
                    assertFailsWith<GdalException> { source.getMask(listOf("UNMASK_FLAGS=absent")) }
                    assertTrue(source.deleteAttribute("flag_values"))
                    attribute(source, "flag_values", GdalDataType.UInt64, longArrayOf(3), values("1", "2", "4"))
                    assertFailsWith<GdalException> { source.getMask(listOf("UNMASK_FLAGS=land")) }
                    assertEquals(raw, source.readValues(longArrayOf(0), longArrayOf(6)))
                    source.close()
                    assertFailsWith<IllegalStateException> { mask.getName() }
                    assertFailsWith<IllegalStateException> { sibling.getName() }
                } finally { mask.close(); mask.close(); sibling.close() }
            }
            for ((index, type) in listOf(MDDataType.Text(), MDDataType.JsonText(),
                MDDataType.Compound("record", listOf(MDComponent("id", MDDataType.Numeric(GdalDataType.Int64))))).withIndex()) {
                array(group, "nonnumeric_$index", type, longArrayOf(1)) { source ->
                    assertFailsWith<GdalException> { source.getMask() }
                    assertEquals("nonnumeric_$index", source.getName())
                }
            }
        }
    }
}
