package dev.gdal4k.runtime

/** A GDAL handle. Close with use; children cannot outlive their parent. */
interface MDArray {
    suspend fun close()
    suspend fun getName(): String
    suspend fun getFullName(): String
    suspend fun getDimensions(): List<Dimension>
    /**
     * Detached native unsigned element count, without reading samples. Scalars contain one value.
     * A dimension product exceeding UInt64 returns the native zero sentinel, without an error;
     * zero therefore does not universally mean an empty array. A count is not an allocation limit.
     */
    suspend fun getTotalElementsCount(): ULong
    /**
     * Detached native processing shape, in elements per axis; scalars return an empty list.
     *
     * The byte budget is advisory: a natural block may exceed it. Zero requests no expansion,
     * not a default budget. The budget must fit native size_t (32 bits on current web engines).
     * Planning uses stored element size; variable-length text/compound payloads are not included
     * in a peak-memory bound. Shapes may differ between native pointer widths. No samples are read.
     * Cancellation is checked at operation boundaries; there is no native progress callback.
     */
    suspend fun getProcessingChunkSize(maxChunkMemory: ULong): List<ULong>
    /**
     * Current driver-defined auxiliary coordinate arrays, in native order.
     *
     * These are distinct from each Dimension's indexing variable. Repeated references remain
     * repeated entries; types, ranks and shapes are not restricted or converted. Drivers may
     * return an empty list, and may warn and omit unresolved names while returning valid arrays.
     * Native failures throw. CF name parsing and view behavior remain driver-defined.
     *
     * Each returned wrapper aliases its actual array and depends on this array and its ancestors.
     * Closing one leaves the source and other wrappers usable. Later queries see current metadata;
     * already returned arrays remain live aliases. Use typed value I/O for exact stored values.
     * Cancellation is checked at operation boundaries; there is no native progress callback.
     */
    suspend fun getCoordinateVariables(): List<MDArray>
    /**
     * Native cached statistics, or null when a nonforced lookup has no eligible cache.
     *
     * force permits computation but may still use a cache; computeStatistics always requests
     * recomputation. Caching and invalidation are driver-defined: MEM does not retain a cache.
     * Moments describe this array's values and native validity mask, with no extra scale/offset
     * transform. Query an unscaled view for its physical values. Approximation is permitted when
     * requested; the C API does not report whether a returned result is approximate.
     *
     * Failures and cancellation throw. Both methods support sampled progress; a fraction of 1
     * does not guarantee cache or metadata work has completed. Cancellation does not roll back
     * native side effects. Native PAM count persistence above Long.MAX_VALUE is unqualified.
     */
    suspend fun getStatistics(approximate: Boolean = false, force: Boolean = false): MDStatistics?
    /**
     * Recompute a detached native statistics snapshot, including the unsigned valid-sample count.
     *
     * Complex, text and compound arrays are unsupported by generic computation. No valid samples
     * is a successful count-zero snapshot, distinct from getStatistics returning null. Integer
     * moments can round through Double. Options are snapshotted before queued work and cannot
     * contain NUL. UPDATE_METADATA=YES requests driver actual_range updates where supported;
     * default computation does not request them. Success alone does not prove persistence.
     */
    suspend fun computeStatistics(approximate: Boolean = false, options: List<String> = emptyList()): MDStatistics
    /**
     * Lazy aliased view with the requested output dimension order.
     *
     * Each entry selects a zero-based source axis. Every source axis must appear exactly once;
     * repeated -1 entries insert size-one axes. Other negative, duplicate, missing or out-of-range
     * source axes fail. Scalars accept an empty map or maps containing only -1.
     *
     * The view and its descendants depend on this array and its ancestors. Closing the view does
     * not close its source or siblings. Typed value I/O preserves the stored type and aliases the
     * source when the underlying array permits writes; numeric read/write retain Float64 conversion.
     * Metadata setters may be unsupported by a view even when its source supports them.
     *
     * CRS data-axis mappings follow the reordered axes, including valid negative mappings.
     * A CRS copy returned by getSpatialRef remains independently owned.
     * The map is snapshotted before queued engine work. Cancellation is checked at operation
     * boundaries; this operation does not supply a native progress callback.
     */
    suspend fun transpose(newAxisToOldAxis: IntArray): MDArray
    /**
     * Read-only numeric resampled or reprojected view, retaining the source's stored type.
     *
     * Pass one entry per source dimension (rank at least two). Null spatial entries let GDAL
     * infer the output size; normally the final two axes are Y/X. Explicit nonspatial entries
     * must match source names and sizes. Spatial dimensions are rebuilt by GDAL. If either
     * target spatial axis has an indexing variable, both must have regular numeric 1D axes.
     * Gauss is unsupported. CubicSpline need not preserve values for an unchanged grid.
     *
     * Options follow native GetResampled semantics, including its EMIT-specific options;
     * they are not arbitrary gdalwarp arguments. Inputs are snapshotted before queued work.
     * Construction may materialize geolocation data and has no native progress callback.
     * Cancellation is checked at operation boundaries, without inner-warp preemption.
     *
     * Closing this view leaves source and siblings usable. This view and its descendants
     * depend on the source and ancestors. Target dimension/SRS wrappers can close after
     * construction; an explicit target SRS is cloned. getSpatialRef returns an owned copy.
     * Native metadata/cache behavior after source mutation is driver-defined.
     *
     * The bundled GDAL port carries an extra-axis step correction for composed slices.
     * Signed CRS axis mappings and exact
     * Int64/UInt64 NoData classification through the warp also require separate verification.
     */
    suspend fun getResampled(newDimensions: List<Dimension?>, resampling: RasterResampling = RasterResampling.Nearest, targetSRS: SpatialReference? = null, options: List<String> = emptyList()): MDArray
    /**
     * Lazy numeric view that applies the source's scale and offset to each value.
     *
     * Nonidentity views capture those metadata values and expose Float64 or CFloat64 values;
     * complex real and imaginary components each use component * scale + offset. Writes apply
     * the inverse conversion when the source permits them. Binary64 arithmetic can round large
     * integers, and zero or nonfinite metadata can produce NaN or infinite results.
     *
     * A nonidentity view initially has NaN NoData when its source has NoData. Remapping requires
     * both view and current source NoData; clearing either disables it. Source presence is initially
     * captured, so adding source NoData later does not by itself enable the original view's NoData.
     * Exact Int64/UInt64 sentinel comparisons precede conversion, and missing writes restore the
     * exact source value. Complex missingness follows the real component; a real NaN write is also
     * missing while remapping is enabled. A finite view NoData may collide with a valid physical
     * value. Clearing this view's NoData changes the view, not its source.
     *
     * Effective identity metadata (scale 1 and offset 0) returns another owned wrapper for the
     * original numeric array, retaining its type and shared metadata. Nonnumeric arrays fail.
     * Every returned wrapper depends on this array and its ancestors; closing it leaves its
     * source and siblings usable. A CRS copy returned by getSpatialRef remains independent.
     * Cancellation is checked at operation boundaries; there is no native progress callback.
     */
    suspend fun getUnscaled(): MDArray
    /**
     * Lazy typed alias using GDAL basic slicing or quoted compound-field access.
     *
     * Brackets accept integer indexes, start:stop:step ranges, one ellipsis, and newaxis or
     * np.newaxis. Negative indexes and steps are supported; empty-result ranges fail. Brackets
     * can be chained. Integer indexing can produce a scalar, whose I/O uses empty start/count
     * arrays. Slicing an already scalar source is unsupported. Expressions cannot contain NUL.
     *
     * Basic slices write through when the source permits writes. Extracted fields are readable;
     * field writes are unsupported. Metadata setters may also be unsupported. The view and its
     * descendants depend on this array and its ancestors; closing it leaves source and siblings
     * usable. CRS copies remain independent. Cancellation is checked at operation boundaries;
     * this constructor does not supply a native progress callback.
     */
    suspend fun getView(viewExpr: String): MDArray
    /**
     * Read-only numeric validity view, with Byte values 0 (invalid) and 1 (valid).
     *
     * Combines source NoData, NaN and CF missing_value, _FillValue, valid_min, valid_max and
     * valid_range attributes. CF criteria are captured at construction; source values and raw
     * NoData remain live. Int64/UInt64 samples and integer criteria are compared exactly.
     * Complex sources use only the real component, including NaN and NoData classification.
     *
     * UNMASK_FLAGS selects comma-separated CF flag_meanings. Selected flags match by OR, while
     * all other validity criteria still apply. Flag metadata supports integer types up to 32 bits.
     * Options are snapshotted before queued work and cannot contain NUL. CF attributes inherited
     * by an unscaled view may still use raw units; thresholds are not implicitly transformed.
     *
     * The returned view and descendants depend on this array and its ancestors. Closing it leaves
     * source and siblings usable. CRS copies remain independent. Cancellation is checked at
     * operation boundaries; this constructor does not supply a native progress callback.
     */
    suspend fun getMask(options: List<String> = emptyList()): MDArray
    /** Independent CRS copy, usable after this array closes; null means no CRS. Close the copy.
     * Native failures throw instead of returning null.
     */
    suspend fun getSpatialRef(): SpatialReference?
    /**
     * Assign or request clearing the driver's CRS metadata, without transforming samples.
     *
     * The input remains caller-owned and must stay open until this call finishes. MEM/VRT copy
     * it; file drivers may serialize it and reconstruct axis mappings differently. Existing CRS
     * copies returned by getSpatialRef remain independent after assignment or clearing.
     *
     * Returns the native acceptance result and throws surfaced native failures. Success alone
     * does not prove durable assignment or clearing; reopen/read back when persistence matters.
     * Cancellation is checked at operation boundaries and does not roll back native changes.
     */
    suspend fun setSpatialRef(spatialRef: SpatialReference?): Boolean
    /** Detached natural block sizes in dimension order. Zero means no hint; scalars return an empty list. */
    suspend fun getBlockSize(): List<ULong>
    /** Detached structural KEY=VALUE entries; empty when the driver provides no information. */
    suspend fun getStructuralInfo(): List<String>
    /** Typed NoData, preserving exact integers and complex/string values; null means unset. */
    suspend fun getNoDataValue(): MDValue?
    /** null removes NoData. A non-null value must match the array's exact stored type. */
    suspend fun setNoDataValue(value: MDValue?): Boolean
    suspend fun getUnit(): String
    suspend fun setUnit(unit: String): Boolean
    /** null means no explicit scale/offset. Values are metadata, not applied to read/write. */
    suspend fun getScale(): Double?
    suspend fun getOffset(): Double?
    /** Explicit scale and its storage type, or null when unset. Native failures throw. */
    suspend fun getScaleEx(): MDNumericMetadata?
    /** Explicit offset and its storage type, or null when unset. Native failures throw. */
    suspend fun getOffsetEx(): MDNumericMetadata?
    suspend fun setScale(scale: Double): Boolean
    suspend fun setOffset(offset: Double): Boolean
    /**
     * Set scale metadata with a requested backing-attribute type, without changing raw samples.
     * Unknown lets the driver choose. Drivers can ignore the request or retain an existing
     * attribute's type; getScaleEx reports the actual metadata. The value still has Double
     * precision. Returns native acceptance, throws surfaced failures, and does not promise
     * persistence or rollback on cancellation. There is no native progress callback.
     */
    suspend fun setScale(scale: Double, storageType: Int): Boolean
    /** Same driver-native storage request and mutation rules as the typed scale overload. */
    suspend fun setOffset(offset: Double, storageType: Int): Boolean
    /** GDAL numeric type; non-numeric arrays return GdalDataType.Unknown. */
    suspend fun getDataType(): Int
    /** Numeric slices are converted to Float64, in row-major dimension order.
     * Pass one start and count per dimension; scalar arrays use empty arrays.
     * Use readValues/writeValues to preserve integer precision or complex values.
     */
    suspend fun read(start: LongArray, count: LongArray): DoubleArray
    suspend fun write(start: LongArray, count: LongArray, values: DoubleArray): Boolean
    suspend fun getAttribute(name: String): Attribute?
    suspend fun getAttributes(options: List<String> = emptyList()): List<Attribute>
    /** Empty dimensions create a scalar attribute. */
    suspend fun createAttribute(name: String, dimensions: LongArray, dataType: MDDataType, options: List<String> = emptyList()): Attribute?
    suspend fun deleteAttribute(name: String, options: List<String> = emptyList()): Boolean
    suspend fun getExtendedDataType(): MDDataType
    /** Reads only the requested window, in row-major order, preserving the stored type. */
    suspend fun readValues(start: LongArray, count: LongArray): List<MDValue>
    suspend fun writeValues(start: LongArray, count: LongArray, values: List<MDValue>): Boolean

}

suspend inline fun <T : MDArray, R> T.use(block: (T) -> R): R {
    try { return block(this) } finally {
        kotlinx.coroutines.withContext(kotlinx.coroutines.NonCancellable) { close() }
    }
}
