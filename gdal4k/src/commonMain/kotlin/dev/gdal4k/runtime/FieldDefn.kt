package dev.gdal4k.runtime

/**
 * OGR field definition. Definitions borrowed from a layer/feature are read-only.
 * Clone before editing; Layer schema changes invalidate borrowed definitions.
 */
interface FieldDefn {
    /** Independent editable copy, owned by the caller. */
    suspend fun cloneFieldDefn(): FieldDefn

    suspend fun close()

    suspend fun getName(): String

    suspend fun getNameRef(): String

    suspend fun setName(name: String)

    suspend fun getAlternativeName(): String

    suspend fun getAlternativeNameRef(): String

    suspend fun setAlternativeName(name: String)

    suspend fun getType(): Int

    suspend fun setType(type: Int)

    suspend fun getFieldType(): Int

    suspend fun getSubType(): Int

    suspend fun setSubType(subType: Int)

    suspend fun getJustify(): Int

    suspend fun setJustify(justify: Int)

    suspend fun getWidth(): Int

    suspend fun setWidth(width: Int)

    suspend fun getPrecision(): Int

    suspend fun setPrecision(precision: Int)

    suspend fun getTZFlag(): Int

    suspend fun setTZFlag(flag: Int)

    suspend fun getTypeName(): String

    suspend fun getFieldTypeName(type: Int): String

    suspend fun isIgnored(): Boolean

    suspend fun setIgnored(ignored: Boolean)

    suspend fun isNullable(): Boolean

    suspend fun setNullable(nullable: Boolean)

    suspend fun isUnique(): Boolean

    suspend fun setUnique(unique: Boolean)

    suspend fun isGenerated(): Boolean

    suspend fun setGenerated(generated: Boolean)

    suspend fun getDefault(): String

    suspend fun setDefault(defaultValue: String)

    suspend fun isDefaultDriverSpecific(): Boolean

    suspend fun getDomainName(): String

    suspend fun getDomainNameRef(): String

    suspend fun setDomainName(domainName: String)

    suspend fun getComment(): String

    suspend fun getCommentRef(): String

    suspend fun setComment(comment: String)
}

suspend inline fun <T : FieldDefn, R> T.use(block: (T) -> R): R {
    try {
        return block(this)
    } finally {
        kotlinx.coroutines.withContext(kotlinx.coroutines.NonCancellable) { close() }
    }
}
