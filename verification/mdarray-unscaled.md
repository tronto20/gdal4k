# Multidimensional unscaled views

`MDArray.getUnscaled()` returns a lazy view of physical numeric values. It uses
GDAL 3.12.4's captured `raw * scale + offset` operation, with default scale `1`
and offset `0`. For complex data GDAL applies both scale and offset to each
component. Writes through a writable nonidentity view apply the inverse
operation to its source.

Numeric sources with effective scale `1` and offset `0` retain their stored type
and exact values. Their independently owned wrappers alias the source metadata
and samples. Other numeric sources produce `Float64` or `CFloat64` views. These
views capture scale and offset at creation; later source metadata changes affect
new views. Their own scale and offset are absent because the transform has
already been applied, and their scale/offset setters are unsupported. Units and
attributes follow the parent. Natural blocks and dimensions retain their order;
the source's compression entries are not promised through the view's structural
information. Spatial-reference getters retain their independent clone contract.

A view and its descendants depend on the source array and its ancestors.
Closing a view leaves its source and siblings usable. Closing a source or dataset
invalidates derived wrappers. Cancellation is checked at operation boundaries;
the call supplies no native progress callback or atomic caller-handoff guarantee.

## NoData and precision

A nonidentity view initially captures whether its source has NoData. Its initial
destination marker is NaN. Setting or clearing the view's marker changes only
the nonidentity view; an identity wrapper can change the source metadata. Each
read or write captures the current parent marker once. Remapping occurs only
when both the view and that captured parent marker are present. Removing parent
NoData safely disables remapping; replacing it changes the next operation.
Adding parent NoData after a view was created without it does not enable that
view's marker until the view marker is explicitly set.

Int64 and UInt64 samples are compared with the exact parent sentinel before
conversion to physical Float64 values. Classified missing writes restore the
exact parent-native sentinel. This distinguishes an integer sentinel from its
neighbor without promising lossless Float64 arithmetic or arbitrary valid
64-bit roundtrips. A finite view marker can coincide with valid physical data;
the inverse write predicate cannot distinguish that collision.

GDAL's existing complex/NaN policy is preserved: reads use ordinary equality of
the real component, so the imaginary component does not affect missingness and
a NaN source sentinel does not gain a NaN-equals-NaN predicate. When remapping is
enabled, a real NaN write or a real component equal to the view marker restores
the full parent sentinel, including its imaginary component. Zero and nonfinite
scale/offset values use ordinary IEEE arithmetic. Missing writes can bypass
that arithmetic and restore the sentinel; normal inverse writes are not promised
finite or invertible results. A parent driver's write failure is not an atomic
transaction guarantee, and concurrent unsupported metadata mutation is not made
thread safe by the operation-local marker snapshot.

## Verification scope

`MDArrayUnscaledTest` has nine common tests for identity type/metadata aliasing,
physical metadata capture, real/complex scalar and array reads and inverse
writes, source/view marker transitions, 64-bit type conversion, exact sentinel
neighbors/restoration, nested view lifetimes and invalid source types, complex
and NaN compatibility, and zero/nonfinite IEEE behavior. Transpose compositions
exercise non-contiguous parent layouts through the exposed start/count API;
direct native `arrayStep` and caller-buffer-stride behavior needs separate native
implementation review or probes.

The scientific and combined profile template adds a helper inside the existing
`requiredDriversAndRealDataRoundTrip` entrypoint. It closes a compressed,
chunked NC4 Int16 writer with typed Float32 scale and Float64 offset, reopens it
independently, verifies physical Float64 values and marker changes, and checks
parent closure and unchanged source data. Positive inverse writes use writable
MEM fixtures; the independently reopened NC4 source is read-only. The explicit
consumer copy list includes the common class for source and published fixture
transformations. These source additions do not establish runtime success until
the fresh patched engine and applicable current-head platform checks run.
