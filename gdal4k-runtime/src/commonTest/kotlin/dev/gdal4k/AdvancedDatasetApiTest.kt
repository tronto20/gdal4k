package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class AdvancedDatasetApiTest {
    private suspend fun <T> memory(block: suspend (GdalRuntime, Group) -> T): T {
        val r=Gdal4k.runtime()
        return requireNotNull(r.getDriver("MEM")).use { driver -> requireNotNull(driver.createMultidimensional("")).use { ds ->
            requireNotNull(ds.getRootGroup()).use { block(r,it) }
        } }
    }
    @Test fun textWindowsAndValidation() = runTest(timeout=120.seconds) { memory { _,group ->
        val dimension=requireNotNull(group.createDimension("n",4))
        val type=MDDataType.Text()
        requireNotNull(group.createArray("labels",listOf(dimension),type)).use { a ->
            assertEquals(type,a.getExtendedDataType());assertEquals(GdalDataType.Unknown,a.getDataType())
            val values=listOf(MDValue.Text(null),MDValue.Text(""),MDValue.Text("영상 🛰"),MDValue.Text("last"))
            assertTrue(a.writeValues(longArrayOf(0),longArrayOf(4),values))
            assertEquals(values,a.readValues(longArrayOf(0),longArrayOf(4)))
            assertEquals(values.subList(1,3),a.readValues(longArrayOf(1),longArrayOf(2)))
            assertFails { a.writeValues(longArrayOf(0),longArrayOf(2),listOf(MDValue.Text("changed"),MDValue.Numeric(1))) }
            assertEquals(values,a.readValues(longArrayOf(0),longArrayOf(4)))
            assertFails { a.readValues(longArrayOf(3),longArrayOf(2)) }
            assertFails { a.writeValues(longArrayOf(0),longArrayOf(1),listOf(MDValue.Text("a\u0000b"))) }
        }
        requireNotNull(group.createArray("short",listOf(dimension),MDDataType.Text(3))).use { a ->
            assertFails { a.writeValues(longArrayOf(0),longArrayOf(1),listOf(MDValue.Text("🛰"))) }
        }
    } }
    @Test fun jsonStringSchemasPreserveRawValuesAndRejectWritesAtomically() = runTest(timeout=120.seconds) { memory { _,group ->
        val schema=MDDataType.JsonText()
        val values=listOf(MDValue.Text(null),MDValue.Text("null"),
            MDValue.Text(" \n\t" + """{"id":9007199254740993,"name":"영상 🛰","escaped":"quote\" \\ \n"}""" + "\t "),
            MDValue.Text("""[1,true,null,{"雪":"🛰"}]"""),MDValue.Text("\"quoted \\uC601 scalar\""),
            MDValue.Text("9007199254740993"),MDValue.Text("{ invalid JSON is still raw text }"),MDValue.Text(""))
        requireNotNull(group.createDimension("json_n",values.size.toLong())).use { dimension ->
            requireNotNull(group.createArray("json",listOf(dimension),schema)).use { array ->
                assertEquals(schema,array.getExtendedDataType());assertEquals(GdalDataType.Unknown,array.getDataType())
                assertTrue(array.writeValues(longArrayOf(0),longArrayOf(values.size.toLong()),values))
                assertEquals(values,array.readValues(longArrayOf(0),longArrayOf(values.size.toLong())))
                assertEquals(values.take(2),array.readValues(longArrayOf(0),longArrayOf(2))) // Missing cell differs from the literal "null".
                assertFails { array.writeValues(longArrayOf(0),longArrayOf(2),values.take(1)) }
                assertFails { array.writeValues(longArrayOf(0),longArrayOf(2),listOf(MDValue.Text("changed"),MDValue.Numeric(1))) }
                assertFails { array.writeValues(longArrayOf(0),longArrayOf(2),listOf(MDValue.Text("changed"),MDValue.Text("bad\u0000text"))) }
                assertFails { array.writeValues(longArrayOf(values.size.toLong()),longArrayOf(1),listOf(MDValue.Text("outside"))) }
                assertEquals(schema,array.getExtendedDataType());assertEquals(values,array.readValues(longArrayOf(0),longArrayOf(values.size.toLong())))
                val groupAttribute=requireNotNull(group.createAttribute("json",longArrayOf(values.size.toLong()),schema))
                val arrayAttribute=requireNotNull(array.createAttribute("json",longArrayOf(values.size.toLong()),schema))
                try { for(attribute in listOf(groupAttribute,arrayAttribute)) {
                    assertEquals(schema,attribute.getDataType());assertTrue(attribute.writeValues(values));assertEquals(values,attribute.readValues())
                    assertFails { attribute.writeValues(values.dropLast(1)) }
                    assertFails { attribute.writeValues(listOf(MDValue.Numeric(1))+values.drop(1)) }
                    assertFails { attribute.writeValues(listOf(MDValue.Text("bad\u0000text"))+values.drop(1)) }
                    assertEquals(schema,attribute.getDataType());assertEquals(values,attribute.readValues())
                } } finally { arrayAttribute.close();groupAttribute.close() }
            }
        }
        requireNotNull(group.createDimension("short_json_n",2)).use { dimension ->
            val bounded=MDDataType.JsonText(3);val original=listOf(MDValue.Text("雪"),MDValue.Text("7"))
            requireNotNull(group.createArray("short_json",listOf(dimension),bounded)).use { array ->
                assertEquals(bounded,array.getExtendedDataType());assertTrue(array.writeValues(longArrayOf(0),longArrayOf(2),original))
                for (tooLong in listOf("🛰","null")) {
                    assertFails { array.writeValues(longArrayOf(0),longArrayOf(2),listOf(MDValue.Text("a"),MDValue.Text(tooLong))) }
                    assertEquals(original,array.readValues(longArrayOf(0),longArrayOf(2)));assertEquals(bounded,array.getExtendedDataType())
                }
            }
        }
    } }
    @Test fun jsonAttributeAliasesAndParentLifetimeKeepTheirSchema() = runTest(timeout=120.seconds) { memory { _,root ->
        val schema=MDDataType.JsonText();val original=listOf(MDValue.Text(""" {"id":9007199254740993,"label":"영상"} """))
        requireNotNull(root.createGroup("json_owner")).use { owner ->
            requireNotNull(root.createGroup("json_other")).use { other ->
                requireNotNull(other.createAttribute("same",longArrayOf(),schema)).use { independent ->
                    val independentValues=listOf(MDValue.Text("[\"independent\"]"))
                    assertTrue(independent.writeValues(independentValues))
                    requireNotNull(owner.createAttribute("same",longArrayOf(),schema)).use { attribute ->
                        assertTrue(attribute.writeValues(original))
                        requireNotNull(owner.getAttribute("same")).use { alias ->
                            requireNotNull(root.openGroup("json_owner")).use { reopened ->
                                requireNotNull(reopened.getAttribute("same")).use { reopenedAttribute ->
                                    for (handle in listOf(attribute,alias,reopenedAttribute)) {
                                        assertEquals(schema,handle.getDataType());assertEquals(original,handle.readValues())
                                    }
                                    assertTrue(owner.deleteAttribute("same"))
                                    for (handle in listOf(attribute,alias,reopenedAttribute)) {
                                        assertFails { handle.getDataType() };assertFails { handle.readValues() }
                                    }
                                    assertNull(reopened.getAttribute("same"))
                                }
                            }
                        }
                    }
                    requireNotNull(owner.createArray("json_array",emptyList(),schema)).use { array ->
                        assertTrue(array.writeValues(longArrayOf(),longArrayOf(),original))
                        requireNotNull(owner.openArray("json_array")).use { reopened ->
                            assertEquals(schema,reopened.getExtendedDataType());assertEquals(original,reopened.readValues(longArrayOf(),longArrayOf()))
                            requireNotNull(array.createAttribute("same",longArrayOf(),schema)).use { attribute ->
                                assertTrue(attribute.writeValues(original))
                                requireNotNull(reopened.getAttribute("same")).use { alias ->
                                    assertEquals(schema,alias.getDataType());assertEquals(original,alias.readValues())
                                    assertTrue(array.deleteAttribute("same"));assertFails { attribute.readValues() };assertFails { alias.getDataType() }
                                }
                            }
                        }
                        requireNotNull(array.createAttribute("retained",longArrayOf(),schema)).use { retained ->
                            assertTrue(retained.writeValues(original));array.close()
                            assertFails { retained.getDataType() };assertFails { retained.readValues() }
                        }
                    }
                    requireNotNull(owner.createAttribute("retained",longArrayOf(),schema)).use { retained ->
                        assertTrue(retained.writeValues(original));owner.close()
                        assertFails { retained.getDataType() };assertFails { retained.readValues() }
                    }
                    assertEquals(schema,independent.getDataType());assertEquals(independentValues,independent.readValues())
                }
            }
        }
    } }
    @Test fun numericPrecisionAndComplex() = runTest(timeout=120.seconds) { memory { _,g ->
        val types=listOf(GdalDataType.Byte to listOf("0","255"),GdalDataType.Int8 to listOf("-128","127"),GdalDataType.UInt16 to listOf("0","65535"),GdalDataType.Int16 to listOf("-32768","32767"),GdalDataType.UInt32 to listOf("0","4294967295"),GdalDataType.Int32 to listOf("-2147483648","2147483647"),GdalDataType.UInt64 to listOf("0","18446744073709551615"),GdalDataType.Int64 to listOf(Long.MIN_VALUE.toString(),Long.MAX_VALUE.toString()))
        val d=requireNotNull(g.createDimension("n",2))
        for ((type,strings) in types) requireNotNull(g.createArray("v$type",listOf(d),MDDataType.Numeric(type))).use { a ->
            val values=strings.map { MDValue.Numeric(it) };assertTrue(a.writeValues(longArrayOf(0),longArrayOf(2),values));assertEquals(values,a.readValues(longArrayOf(0),longArrayOf(2)))
            assertFails { a.writeValues(longArrayOf(0),longArrayOf(1),listOf(MDValue.Numeric("18446744073709551616"))) }
            assertEquals(values,a.readValues(longArrayOf(0),longArrayOf(2)))
        }
        for (type in listOf(GdalDataType.Float16,GdalDataType.Float32,GdalDataType.Float64,GdalDataType.CInt16,GdalDataType.CInt32,GdalDataType.CFloat16,GdalDataType.CFloat32,GdalDataType.CFloat64)) {
            requireNotNull(g.createArray("c$type",emptyList(),MDDataType.Numeric(type))).use { a ->
                val v=MDValue.Numeric("2",if(type in listOf(8,9,10,11,16)) "-3" else null)
                assertTrue(a.writeValues(longArrayOf(),longArrayOf(),listOf(v)));assertEquals(listOf(v),a.readValues(longArrayOf(),longArrayOf()))
            }
        }
    } }
    @Test fun compoundRecordsAndPartialWindows() = runTest(timeout=120.seconds) { memory { _,g ->
        val schema=MDDataType.Compound("observation",listOf(MDComponent("id",MDDataType.Numeric(GdalDataType.UInt64)),MDComponent("payload",MDDataType.Compound("payload",listOf(MDComponent("label",MDDataType.Text()),MDComponent("value",MDDataType.Numeric(GdalDataType.Float64)))))))
        val dimensions=listOf(requireNotNull(g.createDimension("y",2)),requireNotNull(g.createDimension("x",2)))
        requireNotNull(g.createArray("records",dimensions,schema)).use { a ->
            assertEquals(schema,a.getExtendedDataType())
            val values=(0..3).map { MDValue.Record(listOf(MDValue.Numeric("1844674407370955161$it"),MDValue.Record(listOf(MDValue.Text("영상 $it"),MDValue.Numeric(it))))) }
            assertTrue(a.writeValues(longArrayOf(0,0),longArrayOf(2,2),values));assertEquals(values,a.readValues(longArrayOf(0,0),longArrayOf(2,2)))
            assertEquals(listOf(values[1],values[3]),a.readValues(longArrayOf(0,1),longArrayOf(2,1)))
            repeat(8) { assertFails { a.writeValues(longArrayOf(0,0),longArrayOf(1,2),listOf(values[0],MDValue.Record(listOf(MDValue.Text("wrong"))))) } }
            assertEquals(values,a.readValues(longArrayOf(0,0),longArrayOf(2,2)))
        }
    } }
    @Test fun attributesAndAliasLifetime() = runTest(timeout=120.seconds) { memory { _,g ->
        val schema=MDDataType.Text()
        val attr=requireNotNull(g.createAttribute("title",longArrayOf(),schema))
        assertEquals("title",attr.getName());assertTrue(attr.getFullName().contains("title"));assertContentEquals(longArrayOf(),attr.getDimensions());assertEquals(schema,attr.getDataType())
        assertTrue(attr.writeValues(listOf(MDValue.Text("영상"))));assertEquals(listOf(MDValue.Text("영상")),attr.readValues())
        val alias=requireNotNull(g.getAttribute("title"));val all=g.getAttributes();assertEquals(listOf("title"),all.map { it.getName() })
        memory { _,other -> requireNotNull(other.createAttribute("title",longArrayOf(),schema)).use { independent ->
            assertTrue(independent.writeValues(listOf(MDValue.Text("independent"))))
            assertTrue(g.deleteAttribute("title"));assertFails { alias.readValues() };assertFails { attr.readValues() }
            assertEquals(listOf(MDValue.Text("independent")),independent.readValues())
        } }
        assertNull(g.getAttribute("title"));alias.close();attr.close();all.forEach { it.close() }
        val array=requireNotNull(g.createArray("a",emptyList(),GdalDataType.Byte))
        val numbers=requireNotNull(array.createAttribute("ids",longArrayOf(2),MDDataType.Numeric(GdalDataType.Int64)))
        val values=listOf(MDValue.Numeric(Long.MIN_VALUE),MDValue.Numeric(Long.MAX_VALUE))
        assertTrue(numbers.writeValues(values));assertEquals(values,numbers.readValues());assertContentEquals(longArrayOf(2),numbers.getDimensions())
        assertFails { numbers.writeValues(listOf(values[0])) };assertEquals(values,numbers.readValues())
        array.close();assertFails { numbers.readValues() };numbers.close()
        val recordType=MDDataType.Compound("metadata",listOf(MDComponent("name",schema),MDComponent("id",MDDataType.Numeric(GdalDataType.UInt64))))
        requireNotNull(g.createAttribute("record",longArrayOf(),recordType)).use { a ->
            val value=MDValue.Record(listOf(MDValue.Text("🛰"),MDValue.Numeric("18446744073709551615")))
            assertTrue(a.writeValues(listOf(value)));assertEquals(listOf(value),a.readValues())
        }
    } }
    @Test fun codedAndRangeDomains() = runTest(timeout=120.seconds) {
        val r=Gdal4k.runtime();val values=listOf(CodedFieldValue("9223372036854775807","最大"),CodedFieldValue("-9223372036854775808"),CodedFieldValue("0",""))
        r.createCodedFieldDomain("codes","enum",12,values).use { d ->
            assertEquals(values,d.getCodedValues());assertNull(d.getMinimum());assertNull(d.getMaximum());assertNull(d.getGlob())
        }
        assertFails { r.createCodedFieldDomain("duplicate","",4,listOf(CodedFieldValue("a"),CodedFieldValue("a"))) }
        val lo=FieldDomainBound(Long.MIN_VALUE.toString(),false);val hi=FieldDomainBound(Long.MAX_VALUE.toString())
        r.createRangeFieldDomain("integers","",12,lo,hi).use { d -> assertEquals(lo,d.getMinimum());assertEquals(hi,d.getMaximum());assertNull(d.getCodedValues()) }
        r.createRangeFieldDomain("unbounded","",2,null,FieldDomainBound("1.5",false)).use { d -> assertNull(d.getMinimum());assertEquals(FieldDomainBound("1.5",false),d.getMaximum()) }
        r.createRangeFieldDomain("dates","",11,FieldDomainBound("2026-01-02T03:04:05Z"),null).use { d -> assertEquals("2026-01-02T03:04:05Z",d.getMinimum()?.value);assertNull(d.getMaximum()) }
        assertFails { r.createRangeFieldDomain("overflow","",0,FieldDomainBound("2147483648"),null) }
        assertFails { r.createRangeFieldDomain("type","",4,null,null) }
    }
    @Test fun persistedAdvancedDomains() = runTest(timeout=120.seconds) {
        val r=Gdal4k.runtime();val path="/vsimem/$datasetOutputPrefix-advanced.gpkg";val coded=listOf(CodedFieldValue("1","one"),CodedFieldValue("2","two"))
        requireNotNull(r.getDriver("GPKG")).use { driver -> try {
            requireNotNull(driver.createVector(path)).use { ds ->
                r.createCodedFieldDomain("codes","enum",0,coded).use { ds.addFieldDomain(it) }
                r.createRangeFieldDomain("unsafe","",12,FieldDomainBound("9007199254740993"),null).use { assertFails { ds.addFieldDomain(it) };assertNull(ds.getFieldDomain("unsafe")) }
                r.createRangeFieldDomain("range","bounds",12,FieldDomainBound("9007199254740992"),null).use { ds.addFieldDomain(it) }
                requireNotNull(ds.createLayer("items",geometryType=100)).close()
            }
            r.openDataset(path,DatasetOpenOptions(accessMode=DatasetAccessMode.Update)).use { ds ->
                requireNotNull(ds.getFieldDomain("codes")).use { assertEquals(coded,it.getCodedValues()) }
                requireNotNull(ds.getFieldDomain("range")).use { assertEquals(FieldDomainBound("9007199254740992"),it.getMinimum());assertNull(it.getMaximum()) }
                ds.deleteFieldDomain("codes");assertNull(ds.getFieldDomain("codes"))
            }
        } finally { driver.delete(path) } }
    }
}
