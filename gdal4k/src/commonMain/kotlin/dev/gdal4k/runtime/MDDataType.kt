package dev.gdal4k.runtime

/** Portable multidimensional schema. The engine computes native offsets and alignment. */
sealed interface MDDataType {
    data class Numeric(val dataType: Int) : MDDataType
    /** Maximum UTF-8 byte length; zero means unlimited. */
    data class Text(val maxLength: Long = 0) : MDDataType {
        init { require(maxLength >= 0) }
    }
    /**
     * GDAL JSON string schema, carried as raw [MDValue.Text] values.
     * Maximum UTF-8 byte length; zero means unlimited. Values are not parsed or normalized.
     */
    data class JsonText(val maxLength: Long = 0) : MDDataType {
        init { require(maxLength >= 0) }
    }
    data class Compound(val name: String, val components: List<MDComponent>) : MDDataType {
        init { require(name.isNotEmpty()); require(components.isNotEmpty()); require(components.map { it.name }.distinct().size == components.size) }
    }
}
data class MDComponent(val name: String, val dataType: MDDataType) {
    init { require(name.isNotEmpty()) }
}

/** Values follow their array/attribute schema. Numeric text preserves Int64/UInt64 exactly. */
sealed interface MDValue {
    /** Imaginary part is required for complex numeric types and absent for real types. */
    data class Numeric(val real: String, val imaginary: String? = null) : MDValue {
        constructor(value: Long) : this(value.toString())
        constructor(value: Int) : this(value.toString())
        constructor(value: Double) : this(value.toString())
    }
    data class Text(val value: String?) : MDValue
    /** Fields are in the schema's component order, including nested records. */
    data class Record(val fields: List<MDValue>) : MDValue
}
