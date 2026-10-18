package dev.gdal4k

import dev.gdal4k.GdalFiles
import dev.gdal4k.runtime.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class OgrSchemaApiTest {
    private suspend fun table(geometryType: Int = 100, block: suspend (Dataset, Layer) -> Unit) {
        val r = Gdal4k.runtime(); val path = "/vsimem/$datasetOutputPrefix-schema.gpkg"
        requireNotNull(r.getDriver("GPKG")).use { driver ->
            try {
                requireNotNull(driver.createVector(path)).use { dataset ->
                    requireNotNull(dataset.createLayer("items", geometryType = geometryType)).use { layer ->
                        for ((name, type) in listOf("name" to 4, "number" to 0, "extra" to 4))
                            r.createFieldDefn(name, type).use { assertEquals(0, layer.createField(it, false)) }
                        requireNotNull(layer.getLayerDefn()).use { definition ->
                            r.createFeature(definition).use { feature ->
                                if (geometryType != 100) r.createGeometryFromWkt("POINT (0 0)").use { assertEquals(0, feature.setGeometry(it)) }
                                feature.setField("name", "alpha"); feature.setField("number", 42); feature.setField("extra", "tail")
                                assertEquals(0, layer.createFeature(feature))
                            }
                        }
                        block(dataset, layer)
                    }
                }
            } finally { driver.delete(path) }
        }
    }

    private suspend fun names(layer: Layer): List<String> = requireNotNull(layer.getLayerDefn()).use { definition ->
        (0 until definition.getFieldCount()).map { index -> requireNotNull(definition.getFieldDefn(index)).use { it.getName() } }
    }

    @Test fun independentFieldCopyKeepsMetadataAndOutlivesItsSource() = runTest(timeout = 120.seconds) {
        val copy = Gdal4k.runtime().createFieldDefn("source", 4).use { field ->
            field.setAlternativeName("alias"); field.setWidth(30); field.setNullable(false)
            field.setDefault("'fallback'"); field.setComment("metadata")
            field.setIgnored(true); field.setUnique(true); field.setGenerated(true)
            field.setDomainName("category"); field.setSubType(4); field.setJustify(1)
            field.cloneFieldDefn()
        }
        copy.use {
            assertEquals("source", it.getName()); assertEquals("alias", it.getAlternativeName())
            assertEquals(30, it.getWidth()); assertFalse(it.isNullable()); assertEquals("'fallback'", it.getDefault())
            assertEquals("metadata", it.getComment()); assertTrue(it.isIgnored()); assertTrue(it.isUnique()); assertTrue(it.isGenerated())
            assertEquals("category", it.getDomainName()); assertEquals(4, it.getSubType()); assertEquals(1, it.getJustify())
            it.setName("copy"); assertEquals("copy", it.getName())
        }
        assertFails { copy.cloneFieldDefn() }
    }

    @Test fun reorderAndDeletePreserveValuesAndPersist() = runTest(timeout = 120.seconds) {
        var output: GdalFiles? = null
        table { dataset, layer ->
            assertTrue(layer.testCapability("ReorderFields")); assertTrue(layer.testCapability("DeleteField"))
            assertEquals(0, layer.reorderFields(intArrayOf(2, 0, 1)))
            assertEquals(listOf("extra", "name", "number"), names(layer))
            assertEquals(0, layer.reorderField(0, 2)); assertEquals(listOf("name", "number", "extra"), names(layer))
            assertEquals(0, layer.deleteField(2)); assertEquals(listOf("name", "number"), names(layer))
            requireNotNull(layer.getNextFeature()).use { assertEquals("alpha", it.getFieldAsString(0)); assertEquals(42, it.getFieldAsInteger(1)) }
            output = dataset.translateVector(driver = "GPKG", name = "schema.gpkg", arguments = emptyList())
        }
        Gdal4k.runtime().openDataset(requireNotNull(output)).use { ds ->
            requireNotNull(ds.getLayer(0)).use { layer ->
                assertEquals(listOf("name", "number"), names(layer))
                requireNotNull(layer.getNextFeature()).use { assertEquals("alpha", it.getFieldAsString("name")); assertEquals(42, it.getFieldAsInteger("number")) }
            }
        }
    }

    @Test fun alterExplicitNameAndTypeWithoutConsumingTheReplacement() = runTest(timeout = 120.seconds) {
        table { _, layer ->
            val r = Gdal4k.runtime()
            val copy = requireNotNull(layer.getLayerDefn()).use { requireNotNull(it.getFieldDefn(1)).use { field -> field.cloneFieldDefn() } }
            copy.use { field ->
                field.setName("count"); field.setType(12)
                assertEquals(0, layer.alterFieldDefn(1, field, FieldAlterFlags.Name or FieldAlterFlags.Type))
                assertEquals("count", field.getName()); assertEquals(12, field.getType())
                assertEquals(listOf("name", "count", "extra"), names(layer))
                requireNotNull(layer.getNextFeature()).use { assertEquals(42L, it.getFieldAsInteger64("count")) }
            }
            r.createFieldDefn("renamed", 0).use { field ->
                assertEquals(0, layer.alterFieldDefn(0, field, FieldAlterFlags.Name))
                requireNotNull(layer.getLayerDefn()).use { requireNotNull(it.getFieldDefn(0)).use { actual -> assertEquals(4, actual.getType()) } }
            }
        }
    }

    @Test fun liveFeatureAndDetachedCloneBlockAllSchemaChanges() = runTest(timeout = 120.seconds) {
        table { _, layer ->
            val r = Gdal4k.runtime(); val feature = requireNotNull(layer.getNextFeature()); val clone = feature.cloneFeature()
            r.createFieldDefn("new", 4).use { field ->
                assertFails { layer.createField(field) }; assertFails { layer.deleteField(0) }
                assertFails { layer.reorderFields(intArrayOf(2, 1, 0)) }; assertFails { layer.reorderField(0, 1) }
                assertFails { layer.alterFieldDefn(0, field, FieldAlterFlags.Name) }
                feature.close(); assertFails { layer.deleteField(0) }; assertEquals("alpha", clone.getFieldAsString("name"))
                clone.close(); assertEquals(0, layer.createField(field)); assertEquals(4, names(layer).size)
            }
        }
    }

    @Test fun aliasesAreReadOnlyAndInvalidatedWhileCopiesStayIndependent() = runTest(timeout = 120.seconds) {
        var retained: FieldDefn? = null
        table { dataset, layer ->
            val alias = requireNotNull(dataset.getLayer("items"))
            val definition = requireNotNull(layer.getLayerDefn()); val other = requireNotNull(alias.getLayerDefn())
            val borrowed = requireNotNull(definition.getFieldDefn(0)); retained = borrowed.cloneFieldDefn()
            assertFails { borrowed.setName("unsafe") }; assertFails { borrowed.setType(0) }
            Gdal4k.runtime().createFieldDefn("unsafe", 4).use { assertFails { definition.addFieldDefn(it) } }
            assertEquals(0, alias.deleteField(2))
            assertFails { definition.getFieldCount() }; assertFails { other.getFieldCount() }; assertFails { borrowed.getName() }
            assertEquals(listOf("name", "number"), names(layer)); definition.close(); other.close(); borrowed.close(); alias.close()
        }
        requireNotNull(retained).use { it.setName("still-owned"); assertEquals("still-owned", it.getName()) }
    }

    @Test fun invalidArgumentsLeaveTheCurrentDefinitionUsable() = runTest(timeout = 120.seconds) {
        table { _, layer ->
            requireNotNull(layer.getLayerDefn()).use { definition ->
                assertFails { layer.deleteField(-1) }; assertFails { layer.deleteField(3) }
                for (order in listOf(intArrayOf(), intArrayOf(0, 1), intArrayOf(0, 0, 2), intArrayOf(0, 1, 3)))
                    assertFails { layer.reorderFields(order) }
                assertFails { layer.reorderField(-1, 0) }; assertFails { layer.reorderField(0, 3) }
                Gdal4k.runtime().createFieldDefn("name", 4).use { field ->
                    assertFails { layer.alterFieldDefn(0, field, 0) }; assertFails { layer.alterFieldDefn(0, field, 0x1000) }
                    field.close(); assertFails { layer.alterFieldDefn(0, field, FieldAlterFlags.Name) }
                }
                assertEquals(3, definition.getFieldCount())
            }
            layer.close(); assertFails { layer.deleteField(0) }
        }
    }

    @Test fun filtersAndSqlResultsMustBeClosedBeforeChangingSchema() = runTest(timeout = 120.seconds) {
        table { dataset, layer ->
            assertEquals(0, layer.setAttributeFilter("number = 42")); assertFails { layer.deleteField(1) }
            assertEquals(0, layer.setAttributeFilter(""))
            requireNotNull(dataset.executeSQL("SELECT * FROM items")).use { assertFails { layer.deleteField(1) } }
            assertEquals(0, layer.deleteField(1)); assertEquals(listOf("name", "extra"), names(layer))
        }
    }

    @Test fun borrowedReplacementIsCopiedAndReadOnlyDriverErrorsRemainErrors() = runTest(timeout = 120.seconds) {
        var files: GdalFiles? = null
        table { dataset, layer ->
            val definition = requireNotNull(layer.getLayerDefn()); val field = requireNotNull(definition.getFieldDefn(0))
            assertEquals(0, layer.alterFieldDefn(0, field, FieldAlterFlags.Name))
            assertFails { field.getName() }; assertFails { definition.getFieldCount() }; field.close(); definition.close()
            files = dataset.translateVector(driver = "GPKG", name = "readonly.gpkg", arguments = emptyList())
        }
        Gdal4k.runtime().openDataset(requireNotNull(files)).use { dataset ->
            requireNotNull(dataset.getLayer(0)).use { layer ->
                val definition = requireNotNull(layer.getLayerDefn())
                assertNotEquals(0, layer.deleteField(0)); assertFails { definition.getFieldCount() }; definition.close()
                assertEquals(listOf("name", "number", "extra"), names(layer))
            }
        }
    }
    @Test fun schemaChangesPreserveSpatialFiltersAndIndependentFilterCopies() = runTest(timeout = 120.seconds) {
        var retained: Geometry? = null
        table(geometryType = 1) { _, layer ->
            assertEquals(0, layer.setSpatialFilterRect(-1.0, -1.0, 1.0, 1.0))
            retained = requireNotNull(layer.getSpatialFilter())
            assertEquals(0, layer.deleteField(2))
            assertEquals(1, layer.getFeatureCount(true))
            requireNotNull(layer.getNextFeature()).use { feature ->
                assertEquals("alpha", feature.getFieldAsString("name"))
                requireNotNull(feature.getGeometryRef()).use { assertContentEquals(doubleArrayOf(0.0, 0.0, 0.0), it.getPoint(0)) }
            }
            assertEquals(0, layer.setSpatialFilterRect(9.0, 9.0, 11.0, 11.0)); assertEquals(0, layer.getFeatureCount(true))
            assertEquals(0, layer.setSpatialFilter(null)); assertEquals(1, layer.getFeatureCount(true))
        }
        requireNotNull(retained).use { assertEquals(4.0, it.getArea(), 1e-9) }
    }

}
