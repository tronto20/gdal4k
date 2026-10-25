package dev.gdal4k

internal actual fun driverProfileFixtureEnvironment(name: String): String? = System.getenv(name)

internal actual fun driverProfileFixturePathExists(path: String): Boolean = try {
    java.nio.file.Files.readAttributes(java.nio.file.Path.of(path), java.nio.file.attribute.BasicFileAttributes::class.java,
        java.nio.file.LinkOption.NOFOLLOW_LINKS)
    true
} catch (_: java.nio.file.NoSuchFileException) {
    false
} catch (_: java.nio.file.NotDirectoryException) {
    false
}
