package dev.gdal4k

import kotlinx.serialization.json.*

/** Test-only public fixture settings. Authentication remains in the child environment. */
internal data class DriverProfileRuntimeFixture(
    val hdf5Path: String,
    val postgresqlDsn: String,
    val httpUrls: Map<String, String>,
) {
    val httpBase: String get() = httpUrls.getValue("http")
    val httpsBase: String get() = httpUrls.getValue("https")
    val untrustedBase: String get() = httpUrls.getValue("untrusted")
    val wrongNameBase: String get() = httpUrls.getValue("wrong-name")
}

internal expect fun driverProfileFixtureEnvironment(name: String): String?

// Capture one immutable endpoint set, including cleanup connections, for this process.
internal val driverProfileRuntimeFixture: DriverProfileRuntimeFixture by lazy {
    if (!@RUNTIME_FIXTURES_REQUIRED@) {
        DriverProfileRuntimeFixture("@HDF5_PATH@", "@POSTGRESQL_DSN@", mapOf(
            "http" to "@HTTP_BASE@", "https" to "@HTTPS_BASE@",
            "untrusted" to "@UNTRUSTED_BASE@", "wrong-name" to "@WRONG_NAME_BASE@"))
    } else {
        val raw = requireNotNull(driverProfileFixtureEnvironment("GDAL4K_TEST_DRIVER_FIXTURE")) {
            "Required test runtime fixture configuration is missing"
        }
        val value = try {
            Json.parseToJsonElement(raw) as? JsonObject
        } catch (_: Exception) {
            null
        }
        requireNotNull(value) { "Invalid test runtime fixture JSON" }
        require(value.keys == setOf("schemaVersion", "profile", "target", "runId",
            "postgresqlDsn", "httpUrls", "hdf5Path")) { "Unexpected test runtime fixture keys" }
        fun text(name: String): String {
            val item = value[name] as? JsonPrimitive
            require(item?.isString == true) { "Invalid test runtime fixture field: $name" }
            return item.content
        }
        val schema = value["schemaVersion"] as? JsonPrimitive
        require(schema?.isString == false && schema.intOrNull == 1) { "Invalid test runtime fixture schema" }
        require(text("profile") == "combined" && text("target") == "linux-arm64") {
            "Test runtime fixture profile or target mismatch"
        }
        require(Regex("[a-z][a-z0-9_-]{15,79}").matches(text("runId"))) { "Invalid test runtime fixture runId" }
        val dsn = text("postgresqlDsn")
        val pg = Regex("PG:host=127\\.0\\.0\\.1 port=([1-9][0-9]{0,4}) dbname=([a-z][a-z0-9_]{0,62}) user=\\2").matchEntire(dsn)
        require(pg != null && pg.groupValues[1].toInt() < 65536) { "Invalid public test PostgreSQL endpoint" }
        val urls = value["httpUrls"] as? JsonObject
        require(urls != null && urls.keys == setOf("http", "https", "untrusted", "wrong-name")) {
            "Invalid test HTTP origin inventory"
        }
        val publicUrls = urls.mapValues { (name, item) ->
            require(item is JsonPrimitive && item.isString) { "Invalid test HTTP origin" }
            val scheme = if (name == "http") "http" else "https"
            val match = Regex("$scheme://127\\.0\\.0\\.1:([1-9][0-9]{0,4})").matchEntire(item.content)
            require(match != null && match.groupValues[1].toInt() < 65536) { "Invalid test HTTP origin" }
            item.content
        }
        require(publicUrls.values.map { it.substringAfterLast(':') }.toSet().size == 4) {
            "Test fixture servers must use distinct ports"
        }
        val path = text("hdf5Path")
        require(path.startsWith('/') && path.none { it == '\u0000' || it == '\r' || it == '\n' || it == '"' }) {
            "Invalid absolute test HDF5 path"
        }
        println("GDAL4K_TEST_DRIVER_FIXTURE " + buildJsonObject {
            put("configuration", value)
            put("contextKey", datasetOutputPrefix)
        })
        DriverProfileRuntimeFixture(path, dsn, publicUrls)
    }
}
