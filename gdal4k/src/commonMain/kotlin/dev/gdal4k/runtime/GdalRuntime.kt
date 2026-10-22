package dev.gdal4k.runtime

import dev.gdal4k.GdalDriver
import dev.gdal4k.GdalFiles

/**
 * Common GDAL entry point for dataset, spatial reference, and transformation operations.
 */
interface GdalRuntime {
    /** Builds a vector index of the full affine extents of explicit raster files, in list order.
     * Sources must remain readable until the call completes. The result stores source paths,
     * not pixels or source leases. Relative paths are preserved relative to the process working
     * directory, not rebased to the index. absolutePaths resolves local relative paths instead.
     * Directories, wildcards, connection strings and network/VSI providers other than /vsimem/
     * are rejected. Web accepts /vsimem/ files only; no browser/Node filesystem or HTTP mounting
     * is implicit. Copy selected bytes and sidecars with writeMemoryFile when appropriate.
     * Mixed CRS requires explicit -t_srs; missing/invalid rasters fail instead of being skipped.
     * Options: -t_srs, -src_srs_name, -src_srs_format and repeated -lco. No GTI raster is created.
     * Returns independent complete output buffers; source files remain caller-owned.
     */
    suspend fun tileIndex(sources: List<String>, driver: String = "GPKG", name: String = "tiles.gpkg", layerName: String = "tiles", locationField: String = "location", absolutePaths: Boolean = false, arguments: List<String>): GdalFiles

    /** Writes a new vector index directly. Existing output is rejected; the caller owns
     * partial files and sidecars on failure/cancellation. Source paths follow tileIndex policy.
     */
    suspend fun tileIndexTo(sources: List<String>, path: String, driver: String = "GPKG", layerName: String = "tiles", locationField: String = "location", absolutePaths: Boolean = false, arguments: List<String>)

    /** Creates an owned geometry. Optional CRS is assigned without transforming coordinates.
     * Invalid/truncated input throws. Close the result with use.
     */
    suspend fun createGeometryFromWkt(wkt: String, spatialReference: SpatialReference? = null): Geometry
    suspend fun createGeometryFromWkb(wkb: ByteArray, spatialReference: SpatialReference? = null): Geometry
    /** Accepts a GeoJSON geometry object, not a Feature or FeatureCollection.
     * Without an override, CRS handling follows GDAL's GeoJSON geometry parser.
     */
    suspend fun createGeometryFromGeoJson(geoJson: String, spatialReference: SpatialReference? = null): Geometry

    /** Builds a lazy, in-memory VRT without copying pixels. Inputs are used in list order.
     * Keeps native inputs, URI/file mounts and their workspaces alive even after their Kotlin handles close;
     * closes those resources when the last dependent VRT closes. Close the returned Dataset.
     * Defaults to -strict; pass -non_strict explicitly to permit GDAL to skip incompatible inputs.
     * This returns a live dataset, not a portable XML file with transient source paths.
     */
    suspend fun buildVRT(sources: List<Dataset>, arguments: List<String>): Dataset

    /** Warps/mosaics in list order, with later valid pixels taking precedence.
     * Returns all generated files in memory. Use warpTo for large outputs.
     * Output must be a concrete file format, not VRT or MEM; arguments use GDAL utility syntax.
     */
    suspend fun warp(sources: List<Dataset>, target: String, driver: String = "GTiff", name: String = "warped.tif", arguments: List<String>): GdalFiles

    /** Writes directly to the GDAL/OS path (Node file / browser opfs:/ on web).
     * Does not return or stage the complete output in memory. Caller owns partial output on failure.
     * Uses a concrete output format; source handles remain owned by the caller.
     */
    suspend fun warpTo(sources: List<Dataset>, path: String, target: String, driver: String = "GTiff", arguments: List<String>)


    suspend fun createColorTable(paletteInterpretation: Int = 1): ColorTable
    suspend fun createRasterAttributeTable(): RasterAttributeTable
    suspend fun createFeature(definition: FeatureDefn): Feature
    suspend fun createFieldDefn(name: String, type: Int): FieldDefn

    suspend fun createCodedFieldDomain(name: String, description: String, fieldType: Int, values: List<CodedFieldValue>, fieldSubType: Int = 0): FieldDomain
    suspend fun createRangeFieldDomain(name: String, description: String, fieldType: Int, minimum: FieldDomainBound?, maximum: FieldDomainBound?, fieldSubType: Int = 0): FieldDomain
    suspend fun createGlobFieldDomain(name: String, description: String, fieldType: Int, glob: String, fieldSubType: Int = 0): FieldDomain
    suspend fun createRelationship(name: String, leftTable: String, rightTable: String, cardinality: Int = 0): Relationship

    suspend fun versionInfo(request: String = "--version"): String

    /** Mount bytes in GDAL's process-local virtual filesystem. Path must begin with /vsimem/.
     * Replaces existing contents; partial contents after failure/cancellation belong to the caller.
     * This API holds complete byte buffers. Use existing dataset *To operations for large direct outputs.
     */
    suspend fun writeMemoryFile(path: String, bytes: ByteArray)
    /** Returns an independent complete buffer, including an empty buffer for an empty file. */
    suspend fun readMemoryFile(path: String): ByteArray
    suspend fun deleteMemoryFile(path: String)

    suspend fun getDriver(name: String): Driver?

    suspend fun drivers(): List<GdalDriver>

    suspend fun createGCP(
        gcpX: Double = 0.0,
        gcpY: Double = 0.0,
        gcpZ: Double = 0.0,
        gcpPixel: Double = 0.0,
        gcpLine: Double = 0.0,
        info: String = "",
        id: String = "",
    ): GCP

    suspend fun createSpatialReference(): SpatialReference

    suspend fun createSpatialReference(definition: String): SpatialReference

    suspend fun createCoordinateTransformation(
        source: SpatialReference,
        target: SpatialReference,
    ): CoordinateTransformation?

    suspend fun openDataset(
        source: String,
        options: DatasetOpenOptions = DatasetOpenOptions(),
    ): Dataset

    /** Opens an isolated memory workspace; closing the returned dataset removes its files. */
    suspend fun openDataset(
        files: GdalFiles,
        options: DatasetOpenOptions = DatasetOpenOptions(),
    ): Dataset

    suspend fun datasetInfo(
        dataset: Dataset,
        options: DatasetInfoOptions = DatasetInfoOptions(),
    ): String
}

/**
 * Shared dataset handle. Platform-specific implementations will wrap the native dataset object.
 */
interface Dataset {
    /** Converts multidimensional arrays/groups with GDALMultiDimTranslate into an owned MEM
     * dataset. The result is materialized and remains usable after the input is closed.
     * An empty option list copies the full tree. Explicit array/group selection, transpose,
     * index views, coordinate subsets and positive integer scale-axis steps follow GDAL syntax.
     * Selectors require a reopenable file-backed input; anonymous MEM selection is rejected.
     * Numeric coordinate subsets use GDAL's Float64 comparison domain. Int64/UInt64 and complex
     * coordinate arrays are rejected for value-based subsets, even when individual values fit;
     * use an index view instead. String coordinates use lexicographic selection. Output-format
     * overrides, resampling and type coercion are rejected.
     * A single -group lifts that group's contents/attributes into the output root and ignores
     * dstname; repeated -group selections create named output groups. Attributes on an unselected
     * source root are excluded by group selection, following pinned GDAL semantics.
     * Source files and any VRT dependencies must remain readable until this call completes.
     */
    suspend fun multiDimTranslate(arguments: List<String>): Dataset

    /** Converts to complete independent output buffers. The initial GTiff path accepts a
     * single numeric 2D array without attributes or unsupported metadata; representation losses
     * fail explicitly. Multidimensional hierarchy/coordinate metadata are not silently discarded.
     * Use multiDimTranslate for a full in-memory multidimensional result, and the direct variant
     * for large file outputs. Driver creation options use repeated -co KEY=VALUE arguments.
     */
    suspend fun multiDimTranslateFiles(driver: String = "GTiff", name: String = "converted.tif", arguments: List<String>): GdalFiles

    /** Writes directly to an OS/Node file or browser opfs:/ path. Existing output is rejected.
     * Source remains caller-owned; partial files and sidecars after failure/cancellation are
     * caller-owned. GTiff follows multiDimTranslateFiles restrictions. The optional scientific
     * profile also permits netCDF NC4 output to real OS paths; netCDF is not accepted by the
     * buffer-returning variant or /vsimem/ output. Driver availability requires the profile.
     */
    suspend fun multiDimTranslateTo(path: String, driver: String = "GTiff", arguments: List<String>)

    /** Interpolates vector points using GDALGrid utility arguments, e.g. -a, -zfield,
     * -txe/-tye, -outsize, -ot and -co. CRS assignment and layer/filter/cursor handling
     * follow GDAL; -a_srs assigns output CRS without transforming coordinates.
     * Produces a new concrete raster file; source and partial output stay caller-owned.
     */
    suspend fun grid(driver: String = "GTiff", name: String = "grid.tif", arguments: List<String>): GdalFiles
    /** Direct OS/Node/opfs:/ file output, without a complete in-memory output copy. */
    suspend fun gridTo(path: String, driver: String = "GTiff", arguments: List<String>)

    /** Computes a viewshed from one DEM band. Requires a north-up raster in a projected CRS.
     * Coordinates use that CRS; DEM heights and horizontal units must be consistent.
     * Input NoData is not masked by GDAL and can produce erroneous visibility results.
     * Output is Byte for visibility, Float64 for minimum target height. Driver must support
     * direct raster creation (e.g. GTiff); use translate for a later COG conversion.
     * Creation options are KEY=VALUE. No scale/offset, reprojection or repair is applied.
     */
    suspend fun viewshed(options: ViewshedOptions, band: Int = 1, driver: String = "GTiff", name: String = "viewshed.tif", creationOptions: List<String>): GdalFiles
    /** Writes a new direct file; partial output belongs to the caller on failure. */
    suspend fun viewshedTo(path: String, options: ViewshedOptions, band: Int = 1, driver: String = "GTiff", creationOptions: List<String>)

    /** Generates a new contour line layer using GDALContourGenerateEx. Raw band values,
     * source CRS and geotransform are used, without scale/offset or reprojection.
     * Output fields are id (Integer64) and elevation (Real). interval must be positive.
     * KEY=VALUE arguments: FIXED_LEVELS, LEVEL_EXP_BASE, NODATA; -dsco/-lco KEY=VALUE.
     * FIXED_LEVELS overrides interval; otherwise LEVEL_EXP_BASE overrides interval.
     * Default NoData comes from the source. Input and partial outputs remain caller-owned.
     */
    suspend fun contour(interval: Double, base: Double = 0.0, band: Int = 1, driver: String = "GPKG", name: String = "contours.gpkg", layerName: String = "contours", arguments: List<String>): GdalFiles

    /** Direct OS/Node/opfs:/ output. Existing files are rejected. No full-output staging. */
    suspend fun contourTo(path: String, interval: Double, base: Double = 0.0, band: Int = 1, driver: String = "GPKG", layerName: String = "contours", arguments: List<String>)

    /** Polygonizes a numeric raster band into a new vector file. Default mask excludes NoData.
     * Integer values use Int64 fields; floating values use Real fields without integer truncation.
     * UInt64 and complex bands are rejected. Output layer uses the source CRS and geotransform.
     * Arguments: -8 (diagonal connectivity), -nomask, -dsco KEY=VALUE, -lco KEY=VALUE.
     * An explicit mask must match raster dimensions; it cannot be combined with -nomask.
     * Source and mask remain caller-owned. Use polygonizeTo for large results.
     */
    suspend fun polygonize(driver: String = "GPKG", name: String = "polygons.gpkg", band: Int = 1, mask: Band? = null, layerName: String = "polygons", fieldName: String = "value", arguments: List<String>): GdalFiles

    /** Writes a new vector file directly to an OS/Node or browser opfs:/ path.
     * Existing outputs are rejected; partial outputs/sidecars after failure belong to the caller.
     */
    suspend fun polygonizeTo(path: String, driver: String = "GPKG", band: Int = 1, mask: Band? = null, layerName: String = "polygons", fieldName: String = "value", arguments: List<String>)

    /** Computes valid-data polygons with GDALFootprint, using band masks/NoData rather
     * than the rectangular dataset extent. All bands are combined by union unless
     * -b/-combine_bands select otherwise. Holes and disconnected regions are retained;
     * -max_points defaults to unlimited, unlike the GDAL CLI's automatic simplification.
     * Other options: -srcnodata, -ovr, -t_cs, -t_srs, -split_polys, -convex_hull,
     * -densify, -simplify, -min_ring_area, -dsco and -lco. Geometry units follow GDAL.
     * Source locations are omitted from attributes, including HTTP URLs/credentials.
     * Returns independent vector files. Source remains caller-owned. GDAL can still
     * materialize intermediate geometries; this API does not bound total memory.
     */
    suspend fun footprint(driver: String = "GPKG", name: String = "footprint.gpkg", layerName: String = "footprint", arguments: List<String>): GdalFiles

    /** Writes a new OS/Node or browser opfs:/ vector file without copying the complete
     * output through the bridge. Existing paths are rejected. Partial output/sidecars
     * after failure or cancellation belong to the caller; input ownership is unchanged.
     */
    suspend fun footprintTo(path: String, driver: String = "GPKG", layerName: String = "footprint", arguments: List<String>)

    /**
     * Compute terrain derivatives with GDAL DEMProcessing. [processing] is hillshade,
     * slope, aspect, TRI, TPI or roughness (case insensitive). [arguments] are gdaldem
     * mode options, e.g. -b, -compute_edges, -alg, -s/-xscale/-yscale and -co.
     * GDAL controls NoData, edge handling and CRS/band-unit scaling; scale/offset are
     * not applied by this wrapper. Use explicit scaling when units are ambiguous.
     * Returns independent output files after processing and closing the output dataset.
     * Only concrete file formats are supported; MEM/VRT/stream and color-relief are excluded.
     * The source remains caller-owned. Progress callbacks are not exposed.
     */
    suspend fun demProcessing(processing: String, driver: String = "GTiff", name: String = "terrain.tif", arguments: List<String>): GdalFiles

    /**
     * Like [demProcessing], writing directly to an OS/Node path or browser opfs:/ path.
     * Output and sidecars, including partial files after failure/cancellation, belong
     * to the caller. No output bytes are copied back into a GdalFiles result.
     */
    suspend fun demProcessingTo(path: String, processing: String, driver: String = "GTiff", arguments: List<String>)

    /** Burns vector layers into a new raster using GDAL rasterize arguments (-burn/-a, -te, -ts/-tr, etc.).
     * Returns generated files in memory; use rasterizeTo for large outputs. Source remains caller-owned.
     * Requires a concrete file format (not MEM/VRT). Layer selection/filter/cursor behavior follows GDAL.
     */
    suspend fun rasterize(driver: String = "GTiff", name: String = "rasterized.tif", arguments: List<String>): GdalFiles

    /** Creates a raster directly at the GDAL/OS path (Node file or browser opfs:/ on web).
     * Does not copy the complete output through the bridge. Caller owns partial output on failure/cancellation.
     * This creates output rather than updating an existing Dataset handle; arguments follow gdal_rasterize.
     */
    suspend fun rasterizeTo(path: String, driver: String = "GTiff", arguments: List<String>)

    suspend fun getRootGroup(): Group?
    /** Returns an independent domain/relationship copy, owned by the caller. */
    suspend fun getFieldDomain(name: String): FieldDomain?
    suspend fun getFieldDomainNames(): List<String>
    suspend fun addFieldDomain(domain: FieldDomain)
    suspend fun updateFieldDomain(domain: FieldDomain)
    suspend fun deleteFieldDomain(name: String)
    suspend fun getRelationship(name: String): Relationship?
    suspend fun getRelationshipNames(): List<String>
    suspend fun addRelationship(relationship: Relationship)
    suspend fun updateRelationship(relationship: Relationship)
    suspend fun deleteRelationship(name: String)

    /** Description and metadata follow GDAL domains; XML domains retain their string-list representation. */
    suspend fun getDescription(): String
    suspend fun setDescription(description: String)
    suspend fun getMetadataDomainList(): List<String>
    suspend fun getMetadata(domain: String = ""): List<String>
    suspend fun setMetadata(metadata: List<String>, domain: String = ""): Int
    suspend fun getMetadataItem(name: String, domain: String = ""): String?
    /** A null value deletes the item. */
    suspend fun setMetadataItem(name: String, value: String?, domain: String = ""): Int
    /** Returns an independent copy, which the caller closes. */
    suspend fun getSpatialRef(): SpatialReference?
    suspend fun setSpatialRef(spatialReference: SpatialReference?): Int
    suspend fun getAccess(): Int
    suspend fun addBand(dataType: Int, options: List<String> = emptyList()): Int
    suspend fun createMaskBand(flags: Int): Int
    suspend fun clearStatistics()

    /** Writes directly to a GDAL/OS path. No output bytes are returned or copied through the bridge.
     * The caller owns output files, including any partial files after failure or cancellation.
     */
    suspend fun translateTo(path: String, driver: String = "GTiff", arguments: List<String>)
    suspend fun warpTo(path: String, target: String, driver: String = "GTiff", arguments: List<String>)
    suspend fun translateVectorTo(path: String, driver: String = "GeoJSON", arguments: List<String>)

    suspend fun createLayer(name: String, spatialReference: SpatialReference? = null, geometryType: Int = 0, options: List<String> = emptyList()): Layer?
    suspend fun copyLayer(source: Layer, name: String, options: List<String> = emptyList()): Layer?
    /** Invalidates handles to the deleted layer and its borrowed children. Closes active SQL result sets before deleting. */
    suspend fun deleteLayer(index: Int): Int
    /** Close the returned result with use; SQL without a result set returns null. */
    suspend fun executeSQL(statement: String, spatialFilter: Geometry? = null, dialect: String = ""): Layer?
    suspend fun startTransaction(force: Boolean = false): Int
    suspend fun commitTransaction(): Int
    suspend fun rollbackTransaction(): Int

    suspend fun readRaster(buffer: ByteArray, options: RasterIOOptions = RasterIOOptions()): Int
    suspend fun readRaster(buffer: ShortArray, options: RasterIOOptions = RasterIOOptions()): Int
    suspend fun readRaster(buffer: IntArray, options: RasterIOOptions = RasterIOOptions()): Int
    suspend fun readRaster(buffer: LongArray, options: RasterIOOptions = RasterIOOptions()): Int
    suspend fun readRaster(buffer: FloatArray, options: RasterIOOptions = RasterIOOptions()): Int
    suspend fun readRaster(buffer: DoubleArray, options: RasterIOOptions = RasterIOOptions()): Int
    suspend fun writeRaster(buffer: ByteArray, options: RasterIOOptions = RasterIOOptions()): Int
    suspend fun writeRaster(buffer: ShortArray, options: RasterIOOptions = RasterIOOptions()): Int
    suspend fun writeRaster(buffer: IntArray, options: RasterIOOptions = RasterIOOptions()): Int
    suspend fun writeRaster(buffer: LongArray, options: RasterIOOptions = RasterIOOptions()): Int
    suspend fun writeRaster(buffer: FloatArray, options: RasterIOOptions = RasterIOOptions()): Int
    suspend fun writeRaster(buffer: DoubleArray, options: RasterIOOptions = RasterIOOptions()): Int

    suspend fun close()

    /** GDAL JSON information for a raster or vector dataset. */
    suspend fun info(arguments: List<String>): String

    suspend fun translate(
        driver: String = "GTiff",
        name: String = "output.tif",
        arguments: List<String>,
    ): GdalFiles

    suspend fun warp(
        target: String,
        driver: String = "GTiff",
        name: String = "warped.tif",
        arguments: List<String>,
    ): GdalFiles

    suspend fun translateVector(
        driver: String = "GeoJSON",
        name: String = "output.geojson",
        arguments: List<String>,
    ): GdalFiles

    suspend fun flushCache(): Int

    suspend fun getDriver(): Driver?

    /**
     * GDAL raster band indices are 1-based.
     */
    suspend fun getRasterBand(index: Int): Band?

    suspend fun getDriverShortName(): String

    suspend fun getDriverLongName(): String

    suspend fun getRasterXSize(): Int

    suspend fun getRasterYSize(): Int

    suspend fun getRasterCount(): Int

    suspend fun getLayerCount(): Int

    suspend fun getLayer(index: Int): Layer?

    suspend fun getLayer(name: String): Layer?

    suspend fun getProjection(): String

    suspend fun getProjectionRef(): String

    suspend fun setProjection(projectionWkt: String): Int

    suspend fun getGeoTransform(): DoubleArray?

    suspend fun setGeoTransform(geoTransform: DoubleArray): Int

    suspend fun getExtent(): DoubleArray?

    suspend fun getGCPCount(): Int

    suspend fun getGCPProjection(): String

    suspend fun getGCPSpatialRef(): SpatialReference?

    suspend fun getGCPs(): List<GCP>

    suspend fun setGCPs(gcps: List<GCP>, projection: String): Int

    suspend fun setGCPs(gcps: List<GCP>, spatialReference: SpatialReference): Int

    suspend fun getFileList(): List<String>

    suspend fun buildOverviews(
        resampling: String = "NEAREST",
        overviewBands: IntArray = intArrayOf(),
    ): Int

    suspend fun resetReading()

    suspend fun testCapability(capability: String): Boolean
}

suspend inline fun <T : Dataset, R> T.use(block: (T) -> R): R {
    try {
        return block(this)
    } finally {
        kotlinx.coroutines.withContext(kotlinx.coroutines.NonCancellable) { close() }
    }
}

enum class DatasetAccessMode {
    ReadOnly,
    Update,
}

enum class DatasetKind {
    Any,
    Raster,
    Vector,
    Multidimensional,
}

data class DatasetOpenOptions(
    val accessMode: DatasetAccessMode = DatasetAccessMode.ReadOnly,
    val kind: DatasetKind = DatasetKind.Any,
    val openOptions: List<String> = emptyList(),
    val httpInputPolicy: HttpInputPolicy? = null,
)

data class DatasetInfoOptions(
    val arguments: List<String> = emptyList(),
)
