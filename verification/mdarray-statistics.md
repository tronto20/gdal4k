# Multidimensional array statistics

`MDArray.getStatistics(approximate = false, force = false)` returns a detached
`MDStatistics?`. Null is native nonforced cache absence. `force = true` permits
computation and may still use an eligible cache. `computeStatistics(approximate =
false, options = emptyList())` always requests recomputation through
`GDALMDArrayComputeStatisticsEx`. Native failure and cancellation throw.

The result contains native Double minimum, maximum, mean, population standard
deviation and an exact `ULong` valid-sample count. It owns no handle and remains
usable after source closure. Generic GDAL computation uses the queried array's
validity mask, including NoData, CF criteria and NaN. It rejects complex, text and
compound data; a nonforced lookup can still return null for those types.

Scale/offset metadata do not separately transform source statistics. An actual
`getUnscaled()` view computes statistics of its own transformed values and mask.
Integer samples can round through Float64, so exact mask/count handling does not
imply arbitrary-precision integer moments. Infinity can produce nonfinite
aggregates. With no valid samples, generic GDAL succeeds with count 0, minimum
`Double.MAX_VALUE`, maximum `-Double.MAX_VALUE`, mean 0 and deviation 0. These
sentinels are preserved and do not mean cache absence.

Caching, cache contexts and invalidation remain in GDAL. MEM does not cache;
PAM-backed drivers may persist statistics in the dataset's `.aux.xml`. An
approximate request can label a cache approximate even after a full generic scan,
and an exact lookup may then be absent. Use `Dataset.clearStatistics()` for
explicit invalidation. Do not assume every metadata/data mutation clears caches.

Options are copied before queued work and reject NUL. `UPDATE_METADATA=YES`
requests `actual_range` on supporting full-array, writable, nonapproximate driver
paths; default computation does not request that update. Native success alone
does not establish successful cache or metadata persistence. Both methods use
the existing sampled progress and cancellation protocol. Fractions are clamped
and can reach 1 before native side effects finish; cancellation does not roll back
already completed cache or metadata work.

The bridge carries counts 0 through `ULong.MAX_VALUE` without a Double/signed-Long
conversion. GDAL 3.12.4's PAM reload parses `ValidSampleCount` through a signed
integer, so persistence above `Long.MAX_VALUE` is unqualified. Full-width wire
tests establish transport only; they do not scan a huge array or qualify that
native persistence range.

Coverage prepared for later CI includes six actual MEM fixture methods, seven
pure-wire methods, and a tiny independently owned/reopened NC4 fixture in each
scientific/combined source and published consumer. The NC4 fixture checks cache
absence, source/view cache persistence, explicit clearing, approximate-cache
eligibility, default metadata behavior and `UPDATE_METADATA=YES` after reopen.
Source preparation and Kotlin-only verification do not establish native runtime,
driver persistence, SDK ABI, external publication or whole-platform acceptance.
