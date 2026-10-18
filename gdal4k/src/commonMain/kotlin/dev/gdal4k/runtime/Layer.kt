package dev.gdal4k.runtime

/**
 * Shared vector layer handle.
 *
 * Layer metadata, filtering, feature editing, cursor state and planar overlay operations.
 */
interface Layer {
    /** Starts a filtered column scan from the beginning. Close the stream before changing the
     * layer's cursor, filters or schema, including through aliases or dataset operations.
     * Supports primitive integer/real/string/binary attributes and WKB geometry; unsupported
     * Arrow schemas fail explicitly. Existing Feature APIs remain available for other types and
     * for distinguishing unset fields from explicit null. Requires a column-capable engine.
     * Closing the stream may reset the layer cursor; do not mix feature and column iteration.
     */
    suspend fun openColumnStream(batchSize: Int = 1024): FeatureColumnStream

    /** GEOS planar overlay of default geometry fields into a distinct, writable, empty result
     * layer with no attribute fields or active filters. operation is intersection/union/
     * symDifference/identity/update/clip/erase. Known CRS mismatches are rejected; no reprojection.
     * Input and method must be different native layers with independent cursors, including aliases.
     * Drivers sharing cursor state across layers may require separately opened input datasets.
     * Output schema aliases are invalidated before native execution, even if the driver fails.
     * Close all result Features first. Inputs remain owned; their read cursors may change.
     * Returns the native error code. Partial output/schema changes stay caller-owned; no
     * automatic transaction, rollback, geometry repair or failure skipping is supplied.
     */
    suspend fun overlayTo(method: Layer, result: Layer, operation: String, arguments: List<String>): Int

    /** Filters the default geometry field. Copies filter; null clears it. Changes reset reading;
     * use resetReading to rewind when the filter is already unchanged/cleared.
     * Coordinates must use the layer CRS; different explicitly assigned CRSs are rejected.
     * Combined with the attribute filter. GDAL/driver filtering may return envelope candidates;
     * getFeature(fid) is random access and does not apply this filter.
     */
    suspend fun setSpatialFilter(filter: Geometry?): Int
    suspend fun setSpatialFilterRect(minX: Double, minY: Double, maxX: Double, maxY: Double): Int
    /** Independent copy, unaffected by later filter changes or layer/dataset close. */
    suspend fun getSpatialFilter(): Geometry?

    /** Attribute schema changes require all Features using this layer definition (including clones)
     * to be closed first. Clear the attribute filter and close active SQL results first. Reacquire layer/field definitions after an attempted native change,
     * including a nonzero driver error. Driver capabilities and conversion rules apply;
     * no automatic transaction, conversion fallback or rollback is supplied.
     */
    suspend fun deleteField(index: Int): Int
    /** newToOld[i] is the old index of the field at new position i; requires a full permutation. */
    suspend fun reorderFields(newToOld: IntArray): Int
    suspend fun reorderField(oldIndex: Int, newIndex: Int): Int
    /** Changes only attributes selected by explicit FieldAlterFlags. The input is copied. */
    suspend fun alterFieldDefn(index: Int, definition: FieldDefn, flags: Int): Int

    /** Copies the definition; the schema lifetime rule above also applies to adding a field. */
    suspend fun createField(field: FieldDefn, approximate: Boolean = true): Int

    suspend fun close()

    suspend fun getDataset(): Dataset?

    suspend fun getLayerDefn(): FeatureDefn?

    suspend fun getName(): String

    suspend fun getGeomType(): Int

    suspend fun getGeometryColumn(): String

    suspend fun getFIDColumn(): String

    suspend fun rename(name: String): Int

    suspend fun getRefCount(): Int

    suspend fun createGeomField(geomFieldDefn: GeomFieldDefn, flags: Int = 0): Int

    suspend fun setAttributeFilter(filter: String): Int

    suspend fun resetReading()

    suspend fun getFeature(fid: Long): Feature?

    suspend fun getNextFeature(): Feature?

    suspend fun createFeature(feature: Feature): Int

    suspend fun setFeature(feature: Feature): Int

    suspend fun upsertFeature(feature: Feature): Int

    suspend fun deleteFeature(fid: Long): Int

    suspend fun setNextByIndex(index: Long): Int

    suspend fun getFeatureCount(force: Boolean = false): Long

    suspend fun getExtent(force: Boolean = false): DoubleArray?

    suspend fun getFeaturesRead(): Long

    suspend fun syncToDisk(): Int

    suspend fun testCapability(capability: String): Boolean
}

suspend inline fun <T : Layer, R> T.use(block: (T) -> R): R {
    try {
        return block(this)
    } finally {
        kotlinx.coroutines.withContext(kotlinx.coroutines.NonCancellable) { close() }
    }
}
