package dev.gdal4k.runtime

/**
 * Shared GDAL/OSR spatial reference handle.
 *
 * This first pass focuses on import/export, basic CRS classification, and the core
 * setters that will be needed by dataset/vector geometry workflows.
 */
interface SpatialReference {
    suspend fun close()

    suspend fun cloneSpatialReference(): SpatialReference

    suspend fun exportToWkt(): String

    suspend fun exportToPrettyWkt(): String

    suspend fun exportToProj4(): String

    suspend fun exportToProjJson(): String

    suspend fun exportToXml(rootElement: String = ""): String

    suspend fun exportToMICoordSys(): String

    suspend fun importFromWkt(wkt: String): Int

    suspend fun importFromProj4(proj4: String): Int

    suspend fun importFromEPSG(epsg: Int): Int

    suspend fun importFromEPSGA(epsga: Int): Int

    suspend fun setFromUserInput(definition: String): Int

    suspend fun setWellKnownGeogCS(name: String): Int

    suspend fun setProjection(name: String): Int

    suspend fun setProjParm(name: String, value: Double): Int

    suspend fun getProjParm(name: String, defaultValue: Double = 0.0): Double

    suspend fun setNormProjParm(name: String, value: Double): Int

    suspend fun getNormProjParm(name: String, defaultValue: Double = 0.0): Double

    suspend fun getName(): String

    suspend fun getCelestialBodyName(): String

    suspend fun isSame(other: SpatialReference): Boolean

    suspend fun isGeographic(): Boolean

    suspend fun isProjected(): Boolean

    suspend fun isGeocentric(): Boolean

    suspend fun isCompound(): Boolean

    suspend fun isVertical(): Boolean

    suspend fun isLocal(): Boolean

    suspend fun isDynamic(): Boolean

    suspend fun hasPointMotionOperation(): Boolean

    suspend fun getCoordinateEpoch(): Double

    suspend fun setCoordinateEpoch(epoch: Double)

    suspend fun getAuthorityName(targetKey: String): String?

    suspend fun getAuthorityCode(targetKey: String): String?

    suspend fun getAttrValue(name: String, child: Int = 0): String?

    suspend fun setAttrValue(name: String, value: String): Int

    suspend fun setAuthority(targetKey: String, authority: String, code: Int): Int

    suspend fun getAngularUnits(): Double

    suspend fun getAngularUnitsName(): String

    suspend fun setAngularUnits(name: String, radiansPerUnit: Double): Int

    suspend fun getLinearUnits(): Double

    suspend fun getLinearUnitsName(): String

    suspend fun setLinearUnits(name: String, metersPerUnit: Double): Int

    suspend fun setLinearUnitsAndUpdateParameters(name: String, metersPerUnit: Double): Int

    suspend fun getAxisName(targetKey: String, index: Int): String?

    suspend fun getAxesCount(): Int

    suspend fun getAxisOrientation(targetKey: String, index: Int): Int

    suspend fun getAxisMappingStrategy(): Int

    suspend fun setAxisMappingStrategy(strategy: Int)

    /** Detached mapping for each CRS axis: 1-based data-axis numbers, negative for sign reversal. */
    suspend fun getDataAxisToSRSAxisMapping(): IntArray

    suspend fun setUTM(zone: Int, north: Int = 1): Int

    suspend fun getUTMZone(): Int

    suspend fun autoIdentifyEPSG(): Int

    suspend fun validate(): Int

    suspend fun morphToESRI(): Int

    suspend fun morphFromESRI(): Int

    suspend fun stripVertical(): Int

    suspend fun cloneGeogCS(): SpatialReference?

    suspend fun convertToOtherProjection(projection: String): SpatialReference?

    suspend fun promoteTo3D(): Int

    suspend fun demoteTo2D(): Int
}

suspend inline fun <T : SpatialReference, R> T.use(block: (T) -> R): R {
    try {
        return block(this)
    } finally {
        kotlinx.coroutines.withContext(kotlinx.coroutines.NonCancellable) { close() }
    }
}
