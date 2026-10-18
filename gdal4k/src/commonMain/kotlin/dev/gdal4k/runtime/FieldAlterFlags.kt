package dev.gdal4k.runtime

/** GDAL attribute-field alteration flags. Supported combinations depend on the driver. */
object FieldAlterFlags {
    const val Name = 0x1
    const val Type = 0x2
    const val WidthPrecision = 0x4
    const val Nullable = 0x8
    const val Default = 0x10
    const val Unique = 0x20
    const val Domain = 0x40
    const val AlternativeName = 0x80
    const val Comment = 0x100
    const val All = 0x1ff
}
