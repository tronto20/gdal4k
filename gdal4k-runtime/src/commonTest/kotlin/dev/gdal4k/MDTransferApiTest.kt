package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class MDTransferApiTest {
    private suspend fun memory(block: suspend (Group) -> Unit) {
        requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
            requireNotNull(driver.createMultidimensional("")).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { block(it) }
            }
        }
    }
    @Test fun float64WindowsAndScalarKeepShapeBitsAndConversion() = runTest(timeout = 120.seconds) { memory { group ->
        val dimensions = listOf(requireNotNull(group.createDimension("y", 5)), requireNotNull(group.createDimension("x", 7)))
        requireNotNull(group.createArray("values", dimensions, GdalDataType.Float64)).use { array ->
            val input = DoubleArray(12) { (it - 4) * 0.125 }; input[2] = -0.0
            assertTrue(array.write(longArrayOf(1, 2), longArrayOf(3, 4), input))
            val output = array.read(longArrayOf(1, 2), longArrayOf(3, 4))
            assertEquals(input.size, output.size)
            for (i in input.indices) assertEquals(input[i].toRawBits(), output[i].toRawBits())
            assertContentEquals(doubleArrayOf(0.0, 0.0), array.read(longArrayOf(0, 0), longArrayOf(1, 2)))
        }
        requireNotNull(group.createArray("scalar", emptyList(), GdalDataType.Int16)).use { array ->
            assertTrue(array.write(longArrayOf(), longArrayOf(), doubleArrayOf(123.0)))
            assertContentEquals(doubleArrayOf(123.0), array.read(longArrayOf(), longArrayOf()))
        }
    } }
    @Test fun typedInt64AndUInt64WindowsRemainExact() = runTest(timeout = 120.seconds) { memory { group ->
        val dimension = requireNotNull(group.createDimension("n", 6))
        for ((type, strings) in listOf(GdalDataType.Int64 to listOf(Long.MIN_VALUE.toString(), "-9007199254740993", "9007199254740993", Long.MAX_VALUE.toString()),
            GdalDataType.UInt64 to listOf("0", "9007199254740993", "18446744073709551614", "18446744073709551615"))) {
            requireNotNull(group.createArray("integer$type", listOf(dimension), type)).use { array ->
                val input = strings.map { MDValue.Numeric(it) }
                assertTrue(array.writeValues(longArrayOf(1), longArrayOf(4), input))
                assertEquals(input, array.readValues(longArrayOf(1), longArrayOf(4)))
                assertEquals(input.subList(1, 3), array.readValues(longArrayOf(2), longArrayOf(2)))
            }
        }
    } }
    @Test fun complexAndNonfiniteCanonicalComponentsStayTyped() = runTest(timeout = 120.seconds) { memory { group ->
        val dimension = requireNotNull(group.createDimension("n", 3))
        for (type in listOf(GdalDataType.Float64, GdalDataType.CFloat64)) {
            val complex = type == GdalDataType.CFloat64
            requireNotNull(group.createArray("floating$type", listOf(dimension), type)).use { array ->
                val input = listOf("NaN", "Infinity", "-Infinity").map { MDValue.Numeric(it, if (complex) "-0" else null) }
                assertTrue(array.writeValues(longArrayOf(0), longArrayOf(3), input))
                assertEquals(input, array.readValues(longArrayOf(0), longArrayOf(3)))
            }
        }
    } }
    @Test fun numericAttributesAndStructuredFallbackKeepOwnership() = runTest(timeout = 120.seconds) { memory { group ->
        val attribute = requireNotNull(group.createAttribute("ids", longArrayOf(2), MDDataType.Numeric(GdalDataType.UInt64)))
        val input = listOf(MDValue.Numeric("9007199254740993"), MDValue.Numeric("18446744073709551615"))
        attribute.use { assertTrue(it.writeValues(input)); assertEquals(input, it.readValues()) }
        requireNotNull(group.getAttribute("ids")).use { assertEquals(input, it.readValues()) }
        val type = MDDataType.Compound("record", listOf(MDComponent("id", MDDataType.Numeric(GdalDataType.UInt64)), MDComponent("text", MDDataType.Text())))
        requireNotNull(group.createAttribute("record", longArrayOf(), type)).use { a ->
            val value = MDValue.Record(listOf(input[1], MDValue.Text("한글 🛰")))
            assertTrue(a.writeValues(listOf(value))); assertEquals(listOf(value), a.readValues())
        }
    } }
    @Test fun textAndCompoundArrayFallbackStillUsesRequestedWindow() = runTest(timeout = 120.seconds) { memory { group ->
        val dimension = requireNotNull(group.createDimension("n", 3))
        requireNotNull(group.createArray("text", listOf(dimension), MDDataType.Text())).use { a ->
            val values = listOf(MDValue.Text(null), MDValue.Text(""), MDValue.Text("영상 😀"))
            assertTrue(a.writeValues(longArrayOf(0), longArrayOf(3), values))
            assertEquals(values.subList(1, 3), a.readValues(longArrayOf(1), longArrayOf(2)))
        }
        val type = MDDataType.Compound("observation", listOf(MDComponent("label", MDDataType.Text()), MDComponent("id", MDDataType.Numeric(GdalDataType.Int64))))
        requireNotNull(group.createArray("records", listOf(dimension), type)).use { a ->
            val values = (0..2).map { MDValue.Record(listOf(MDValue.Text("행$it"), MDValue.Numeric(Long.MAX_VALUE - it))) }
            assertTrue(a.writeValues(longArrayOf(0), longArrayOf(3), values))
            assertEquals(values.subList(1, 3), a.readValues(longArrayOf(1), longArrayOf(2)))
        }
    } }
    @Test fun invalidShapeAndNumericRangeFailBeforeOverwrite() = runTest(timeout = 120.seconds) { memory { group ->
        val dimension = requireNotNull(group.createDimension("n", 2))
        requireNotNull(group.createArray("values", listOf(dimension), GdalDataType.Int64)).use { a ->
            val original = listOf(MDValue.Numeric(Long.MIN_VALUE), MDValue.Numeric(Long.MAX_VALUE))
            assertTrue(a.writeValues(longArrayOf(0), longArrayOf(2), original))
            assertFails { a.writeValues(longArrayOf(0), longArrayOf(2), listOf(original[0])) }
            assertFails { a.writeValues(longArrayOf(0), longArrayOf(2), listOf(MDValue.Numeric(4), MDValue.Numeric("9223372036854775808"))) }
            assertFails { a.writeValues(longArrayOf(0), longArrayOf(2), listOf(MDValue.Numeric(4), MDValue.Numeric("1", "2"))) }
            assertFails { a.readValues(longArrayOf(), longArrayOf()) }
            assertFails { a.readValues(longArrayOf(1), longArrayOf(2)) }
            assertFails { a.write(longArrayOf(0), longArrayOf(2), doubleArrayOf(1.0)) }
            assertEquals(original, a.readValues(longArrayOf(0), longArrayOf(2)))
        }
    } }
    @Test fun cancellationAndParentClosureLeaveRuntimeUsable() = runTest(timeout = 120.seconds) { memory { group ->
        val dimension = requireNotNull(group.createDimension("n", 2))
        val a = requireNotNull(group.createArray("values", listOf(dimension), GdalDataType.Int64))
        val original = listOf(MDValue.Numeric(Long.MIN_VALUE), MDValue.Numeric(Long.MAX_VALUE))
        assertTrue(a.writeValues(longArrayOf(0), longArrayOf(2), original))
        assertFailsWith<CancellationException> { withGdalProgress({ false }) { a.readValues(longArrayOf(0), longArrayOf(2)) } }
        assertFailsWith<CancellationException> { withGdalProgress({ false }) { a.writeValues(longArrayOf(0), longArrayOf(2), original) } }
        assertEquals(original, a.readValues(longArrayOf(0), longArrayOf(2)))
        val attribute = requireNotNull(a.createAttribute("id", longArrayOf(), MDDataType.Numeric(GdalDataType.Int64)))
        a.close(); assertFails { a.read(longArrayOf(0), longArrayOf(2)) }; assertFails { attribute.readValues() }; attribute.close()
        requireNotNull(group.createArray("next", emptyList(), GdalDataType.Float64)).use { next ->
            assertTrue(next.write(longArrayOf(), longArrayOf(), doubleArrayOf(17.0)))
            assertContentEquals(doubleArrayOf(17.0), next.read(longArrayOf(), longArrayOf()))
        }
    } }
}
