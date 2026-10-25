@file:OptIn(kotlinx.cinterop.ExperimentalForeignApi::class)
package dev.gdal4k

import kotlinx.cinterop.toKString

internal actual fun driverProfileFixtureEnvironment(name: String): String? =
    platform.posix.getenv(name)?.toKString()

internal actual fun driverProfileFixturePathExists(path: String): Boolean {
    // MinGW's bundled posix metadata has access/F_OK, but no common lstat binding.
    // This observes fixture-owned regular paths, not general symlink identity.
    val result = platform.posix.access(path, platform.posix.F_OK)
    if (result == 0) return true
    val error = platform.posix.posix_errno()
    check(result == -1 && (error == platform.posix.ENOENT || error == platform.posix.ENOTDIR)) {
        "Cannot inspect owned driver-profile output (errno=$error)"
    }
    return false
}
