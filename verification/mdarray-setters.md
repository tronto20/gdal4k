# MDArray metadata and CRS setters

This source workstream adds `setSpatialRef(SpatialReference?)` and the
`setScale(Double, Int)` / `setOffset(Double, Int)` overloads. They bind
`GDALMDArraySetSpatialRef`, `GDALMDArraySetScaleEx` and
`GDALMDArraySetOffsetEx` from pinned GDAL 3.12.4. The existing one-argument
setters, JVM caller descriptors and wire routes retain their previous behavior.
The new overloads require `storageType`; they do not replace an old method with
a defaulted parameter.

These operations change metadata. They do not reproject an array, convert its
stored datatype, or scale its raw samples. Scale and offset values have Double
precision even when their requested storage type is Int64 or UInt64. Explicit
scale 1 and offset 0 are present metadata, distinct from an unset getter. These
setters have no numeric-metadata removal argument.

`Unknown` requests the driver's automatic choice. Other declared GDAL numeric
type codes are requests whose support remains driver-defined. The bridge does
not delete and recreate an existing attribute to force its type. It rejects
invalid enum codes and malformed new arguments, captures surfaced CPL failures,
and otherwise returns the native Boolean. A true result alone does not prove
durable persistence, complete CRS removal, or atomic mutation.

| Driver | Scale/offset storage | CRS assignment and clearing |
| --- | --- | --- |
| MEM | Records the incoming Double and requested type hint without quantizing the value. A later legacy one-argument setter records `Unknown` again. | Clones the input, including signed axis mapping; null clears it. In-memory metadata has no file-persistence claim. |
| Multidimensional VRT | Ignores the hint and reports `Unknown`. XML stores value only; parsing uses the legacy value setter. | Clones and marks the document dirty. XML stores WKT and signed `dataAxisToSRSAxisMapping`; null omits the SRS when serialized. |
| netCDF/NC4 | Uses the request only when first creating `scale_factor` / `add_offset`; `Unknown` creates Float64. Existing attributes retain their type and writes convert to that actual storage. | Serializes a grid-mapping association and may write CF coordinate metadata. Getter mappings are reconstructed from dimension roles. Null clears the association, not necessarily every coordinate attribute or SRS variable. |

GDAL 3.12.4's netCDF null-clear path ignores the `nc_del_att` return code and
returns true. A readonly clear can therefore return true while `grid_mapping`
remains present. The wrapper reports a CPL failure if one is surfaced, but
does not fabricate a stronger mutation guarantee for this unchecked case.
Readback and independent reopen qualify the actual association. Signed input
axis mappings are checked for MEM/VRT; their persistence is not promised for
netCDF's dimension-derived mapping.

The SRS is a same-engine live input borrowed for the awaited call. MEM/VRT
clone it and netCDF serializes it; ownership never transfers to the setter.
The caller keeps it live through completion and can then close or mutate it.
Every `getSpatialRef()` result remains an independently owned copy, usable
after the array or dataset closes. Setter results create no registered handles.
They have boundary cancellation checks and no native progress callback.
Cancellation after native mutation does not roll that mutation back.

Prepared default-engine common fixtures cover:

- Nonzero Float64 and exact UInt64 samples left unchanged; scalar presence and
  canonical nonfinite Double arguments,
  Float32/half/complex MEM hints, text metadata, and legacy one-argument behavior.
- Signed CRS cloning, input mutation/closure, replacement, null clear, saved CRS
  copies after owner closure, and rejection of closed inputs/owners.
- Captured transforms in preexisting nonidentity unscaled views versus views
  made after metadata changes.
- A bounded, explicitly update-opened `/vsimem` VRT with real inline values,
  serialized and independently reopened scale/offset and signed CRS metadata,
  `Unknown` type readback, and persisted CRS clearing.

The existing scientific/combined profile fixture now prepares a tiny NC4
writer/read/update/read sequence. It checks Float32 conversion and attribute
types after reopening, fresh `Unknown` metadata as Float64, retention of an
existing attribute type, projected CRS readback with explicit `PROJCS`, the
readonly null-clear behavior, persisted removal of `grid_mapping`, and raw
sample preservation. It adds no dependency to default or web packages.

The common fixture class is registered for independent published-consumer
source copying. Source compilation, fake transport checks, actual native
execution and actual published-artifact consumption are distinct evidence.
At source preparation these MEM/VRT/NC4 fixtures are unexecuted. They establish
no new platform or package acceptance. Current iOS device compile/link,
simulator, host execution and Android native-bridge labels remain distinct.

Native source references are pinned to GDAL commit
`0e3e27c90f57130232d215d783ff49cc332cd950`: MEM `memmultidim.h` lines
339–387, VRT `vrtdataset.h` lines 2322–2363 and `vrtmultidim.cpp` lines
2692–2701 / 2774–2818, netCDF `netcdfmultidim.cpp` lines 2691–2900 /
3999–4074. Native `NULL` root-authority queries remain a separate API gap;
the explicit `GEOGCS`/`PROJCS` calls in these fixtures use the existing API.
