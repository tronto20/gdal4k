package dev.gdal4k.runtime

/** Direct JS/wasmJs HTTP input policy. The timeout covers headers and the complete range body of
 * each attempt. Retries apply only to network/timeouts and HTTP408/425/429/500/502/503/504.
 * Authentication,404, full200 responses, invalid ranges/lengths/encoding and identity changes
 * are not retried. Backoff honors Retry-After up to maxRetryDelayMillis and can be cancelled.
 * No full-file fallback is performed. Defaults preserve the existing30s deadline and zero retries.
 * requireStrongETag rejects absent/weak ETags; otherwise Last-Modified can be used, and a server
 * without usable validators provides size checks only. Close/reopen a Dataset after identity errors.
 * Native/JNI HTTP policy support is not supplied by the default engine; explicit selection is rejected
 * there instead of being ignored. Local files, memory datasets and engine bootstrap are separate.
 */
data class HttpInputPolicy(
    val requestTimeoutMillis: Int = 30_000,
    val maxRetries: Int = 0,
    val retryDelayMillis: Int = 200,
    val maxRetryDelayMillis: Int = 3_000,
    val requireStrongETag: Boolean = false,
) {
    init {
        require(requestTimeoutMillis > 0) { "HTTP request timeout must be positive" }
        require(maxRetries >= 0 && retryDelayMillis >= 0 && maxRetryDelayMillis >= retryDelayMillis) { "Invalid HTTP retry policy" }
    }
}
