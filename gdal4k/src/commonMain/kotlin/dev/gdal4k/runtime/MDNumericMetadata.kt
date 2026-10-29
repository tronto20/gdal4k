package dev.gdal4k.runtime

/** Scale or offset metadata. [value] has GDAL's Double precision; [storageType] is its exact
 * [GdalDataType] code, or [GdalDataType.Unknown] when the driver does not report a storage type.
 * The storage type does not increase the value's precision. Array reads and writes remain unscaled.
 */
data class MDNumericMetadata(val value: Double, val storageType: Int)
