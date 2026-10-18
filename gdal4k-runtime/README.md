# 공통 API

`gdal4k-runtime`은 `gdal4k`의 공통 인터페이스를 모든 지원 타깃에 구현하고 `Gdal4k.runtime()` 진입점과 플랫폼별 로더를 제공합니다. 앱은 `gdal4k-runtime`과 필요한 `gdal4k-engine-*`만 추가합니다. 인터페이스 모듈 `gdal4k`는 전이 의존성으로 제공하며, 엔진은 자동으로 포함하지 않습니다.

## 초기화와 수명

```kotlin
import dev.gdal4k.Gdal4k
import dev.gdal4k.runtime.*

suspend fun readBand(bytes: ByteArray): ByteArray {
    val runtime = Gdal4k.runtime() // 초기화되지 않았다면 자동 준비
    return runtime.openDataset("image.tif", bytes).use { dataset ->
        val result = ByteArray(dataset.getRasterXSize() * dataset.getRasterYSize())
        requireNotNull(dataset.getRasterBand(1)).use { band ->
            check(band.readRaster(result) == 0)
        }
        result
    }
}
```

이 예시는 작은 Byte 래스터용입니다. 큰 영상은 밴드의 window 인자를 사용해 나눠 읽습니다. `GdalDataType`은 공통 GDAL 데이터 타입 상수를 제공합니다. JVM의 `org.gdal.*` SWIG 클래스에는 의존하지 않습니다.

`runtime()`은 `suspend` 함수입니다. 최초 호출이 내장 엔진을 초기화하고, 동시에 호출한 coroutine들은 하나의 초기화 결과를 공유합니다. 준비된 뒤에는 같은 런타임을 반환합니다. 초기화와 명시적 종료는 같은 잠금으로 직렬화하며, 닫은 뒤 다음 `runtime()` 호출은 새 엔진을 만듭니다. 기존 핸들은 새 엔진으로 자동 이관되지 않습니다.

`prepare()`는 앱 시작 시 엔진을 미리 로딩하고 싶은 경우만 호출하는 선택 기능입니다. GDAL 호출과 런타임 초기화는 `suspend`이고, 이미 확보한 Kotlin 값의 프로퍼티 접근은 동기입니다.

`Gdal4k.close()`는 앱 종료 시 반드시 호출해야 하는 함수가 아닙니다. 앱이 살아 있는 동안 엔진 컨텍스트·데이터셋 핸들·Worker를 명시적으로 반환하거나 테스트 사이 상태를 초기화할 때 사용합니다. 프로세스가 종료되면 메모리는 회수되지만, 이는 출력 파일 쓰기의 정상 완료를 보장하지 않습니다. **데이터셋은 계속 `use`로 닫아 flush와 자원 정리를 보장해야 합니다.**

공유 런타임을 각 작업이나 Android Activity 종료마다 닫으면 다른 작업의 핸들도 무효화됩니다. 다른 사용자가 없음을 아는 소유자가 명시적 종료를 결정합니다. Android 실제 기기에서는 [`Application.onTerminate()`가 호출되지 않으므로](https://developer.android.com/reference/android/app/Application#onTerminate()), 여기에 정리를 의존하지 않습니다.

## 제공 기능

| 영역 | 공통 API에서 가능한 작업 |
| --- | --- |
| 래스터 | 데이터셋·밴드 열기/생성, Byte/Short/Int/Long/Float/Double 배열 읽기·쓰기, window 읽기·쓰기 |
| 공간 정보 | 영상 크기·밴드 타입·투영·GeoTransform·GCP, NoData·색 해석·마스크·overview 조회/생성 |
| 포맷 변환 | `Driver.createCopy` 또는 `Dataset.translate`, GTiff·COG·JPEGXL 출력과 JXL 압축 |
| 재투영 | `Dataset.warp`, CRS 생성·변환 및 좌표 목록 변환 |
| 벡터 | 레이어·피처·필드 조회/편집, 속성·공간 필터, `Dataset.translateVector`(GDAL 공간 필터 옵션 포함) |
| Geometry | WKT/WKB/GeoJSON 생성·내보내기, CRS 할당, intersection/union/difference, 면적·길이·거리, 공간 관계·buffer·단순화·유효성 복구 |
| 래스터→벡터 | `Dataset.polygonize` / `polygonizeTo`, 밴드·마스크·4/8방향 연결, GPKG·GeoJSON 등 새 벡터 파일 생성 |
| 메모리 파일 | `/vsimem/` 바이트 입출력, 다중 파일 묶음(예: Shapefile) |

다음은 객체 API를 사용하는 COG/JXL 무손실 변환 예시입니다. `runtime()`이 필요할 때 자동으로 초기화합니다. 호출자는 서로 겹치지 않는 `/vsimem/` 경로를 사용합니다.

```kotlin
suspend fun toJxlCog(input: ByteArray, inputPath: String, outputPath: String): ByteArray {
    val runtime = Gdal4k.runtime()
    try {
        runtime.writeMemoryFile(inputPath, input)
        runtime.openDataset(inputPath).use { source ->
            requireNotNull(runtime.getDriver("COG")).use { driver ->
                requireNotNull(driver.createCopy(
                    outputPath, source,
                    options = listOf("COMPRESS=JXL", "JXL_LOSSLESS=YES"),
                )).close()
            }
        }
        return runtime.readMemoryFile(outputPath)
    } finally {
        kotlinx.coroutines.withContext(kotlinx.coroutines.NonCancellable) {
            runtime.deleteMemoryFile(inputPath)
            runtime.deleteMemoryFile(outputPath)
        }
    }
}
```

단독 JPEG XL 파일은 드라이버를 `JPEGXL`, 옵션을 `LOSSLESS=YES`로 바꿉니다. 입력 타입·밴드 구성은 선택한 드라이버가 지원해야 합니다. GDAL 전체 C/C++ API를 모두 노출한 것은 아니며, 객체 API의 정확한 메서드는 `gdal4k` 인터페이스가 기준입니다.

## 실행과 취소

| 플랫폼 | GDAL 실행 위치 | 내장 방식 |
| --- | --- | --- |
| JVM | `Dispatchers.IO` | 명시적으로 선택한 플랫폼 리소스 JAR의 JNI 라이브러리를 해시별 임시 디렉터리에 자동 추출 |
| Android | `Dispatchers.IO` | 선택한 엔진 AAR의 `jniLibs`를 Android가 설치·로드 |
| Native | `Dispatchers.Default` | 게시 KLIB 안의 GDAL·의존성 정적 archive를 최종 앱에 링크 |
| JS / wasmJs | 전용 Worker | 같은 자체 GDAL Wasm을 정적 자산으로 배포하고 Worker에서 로드·초기화 |

JVM은 기본 바이너리가 없으며 `gdal4k-engine:<version>:<classifier>`을 명시적으로 추가해야 합니다. [플랫폼별 의존성 설정](../README.md)을 참고하세요.

Android는 API 28 이상에서 `arm64-v8a`만 지원합니다. x86_64와 32비트 ARM/x86 바이너리는 배포하지 않으며, ARM64 실행을 지원하지 않는 환경과 32비트 프로세스는 초기화 시 거절합니다.

한 런타임의 GDAL 호출과 종료는 직렬화합니다. 다른 런타임 핸들을 섞으면 실패합니다. 데이터셋을 닫은 뒤 그 밴드·레이어 핸들을 쓰면 실패합니다. `use`의 정리는 취소된 coroutine에서도 실행합니다.

내장 엔진의 래스터 배열은 바이너리 채널을 사용하지만, staging·디코딩 복사는 남아 있어 큰 입력은 window 단위 처리가 유리할 수 있습니다. 파일·다차원 값과 구형 엔진의 JSON 경로는 별도입니다. 응답성 테스트는 GDAL의 JXL 연산이 호출 스레드·웹 이벤트 루프 밖에서 실행되는지 검증합니다.

대기 중인 호출은 취소할 수 있습니다. 이미 시작한 GDAL C 호출은 중간에 강제 중단하지 않고 완료한 뒤 핸들/결과를 정리합니다. 따라서 coroutine 취소가 오래 걸리는 GDAL 연산을 즉시 멈추지는 않습니다. 웹 Worker를 강제로 종료해서 데이터셋 상태를 손상시키는 동작도 하지 않습니다.

JS와 wasmJs가 사용하는 **GDAL 엔진 자체는 일반 WebAssembly**입니다. JS 경로는 Kotlin/Wasm의 Wasm GC를 지원하지 않는 브라우저에서도 기본 Wasm·Worker·ES module을 지원하면 사용할 수 있습니다. WebAssembly 자체가 없는 환경에서는 동작하지 않습니다. wasmJs로 컴파일한 Kotlin 앱은 Kotlin/Wasm의 브라우저 요구 사항도 충족해야 합니다. Node 24에서 검증합니다. Kotlin/JS UMD 소비는 [기본 활성화된 `require(ESM)`](https://nodejs.org/en/blog/release/v20.19.0)을 제공하는 Node 20.19+ / 22.12+ 등이 필요합니다. 웹 CSP는 앱의 정적 Worker·ES module·Wasm 실행과 입력 URL의 연결을 허용해야 합니다. 엔진은 pthread/SharedArrayBuffer를 요구하지 않습니다.

## 웹의 URL·선택한 파일 직접 읽기

JS/wasmJs에서는 URL을 그대로 전달합니다. GDAL이 요청하는 구간을 HTTP Range로 읽고, 다운로드한 파일 전체를 `/vsimem`에 올리지 않습니다. JVM·Native·Android의 일반 경로 입력은 원래 OS 파일을 직접 읽습니다. 이 빌드의 HTTP 어댑터는 JS/wasmJs에 적용합니다.

```kotlin
val runtime = Gdal4k.runtime()
runtime.openDataset("https://example.com/image.tif").use { dataset ->
    requireNotNull(dataset.getRasterBand(1)).use { band ->
        val pixels = ByteArray(256 * 256)
        band.readRaster(0, 0, 256, 256, pixels)
    }
}
```

브라우저에서는 파일 선택기 또는 권한을 얻은 FileSystemFileHandle의 `getFile()`로 받은 `org.w3c.files.File`을 전달합니다. 아래 코드는 `jsMain` / `wasmJsMain`에서 사용할 수 있습니다.

```kotlin
import dev.gdal4k.runtime.*

suspend fun describeSelected(file: org.w3c.files.File): String =
    Gdal4k.runtime().openDataset(file).use { dataset -> dataset.info() }
```

Worker는 File의 필요한 `slice`만 읽습니다. 임의의 로컬 경로 문자열은 브라우저 파일 접근 권한을 제공하지 않습니다. Node는 `openDataset("/data/image.tif")`로 프로세스 권한에 따라 디스크를 직접 읽습니다.

직접 입력은 읽기 전용 단일 파일이며, 별도 sidecar 파일을 자동으로 탐색하거나 권한을 확장하지 않습니다. 다중 파일 바이트 입력은 기존 `GdalFiles`를 계속 사용할 수 있습니다. 직접 입력의 구간 캐시는 런타임 전체 8 MiB, 개별 요청은 최대 64 KiB입니다. 데이터셋을 닫으면 해당 파일의 캐시·파일 참조·Node descriptor를 반환합니다. GDAL 블록 캐시와 디코딩·출력·사용자가 요청한 래스터 배열 메모리는 별도로 필요합니다.

웹의 GDAL 보조 데이터도 별도 자산에서 사용하는 구간만 읽고 같은 구간 캐시를 공유합니다. 정적 자산 `gdal4k-data.wasm`에도 HTTP Range 지원이 필요합니다. GDAL 블록 캐시에 별도의 고정 상한을 추가하지 않으며, 초기 Wasm 작업 힙은 작게 시작해 실제 필요에 따라 증가합니다. 래스터 읽기는 출력 배열의 길이만 전달하므로, 읽기 전에 빈 출력 배열 전체를 엔진에 복사하지 않습니다. PROJ 데이터베이스·실행 코드와 디코딩/결과 메모리는 여전히 필요합니다.

HTTP 서버는 `206 Partial Content`와 정확한 `Content-Range`를 제공해야 합니다. 다른 origin이면 CORS 접근을 허용하고 `Content-Range`·`ETag`를 노출하며, 필요 시 `Range`·`If-Range` 요청 헤더를 허용해야 합니다. 강한 ETag가 있으면 읽는 중 파일 변경을 검사합니다. 버전이 바뀌면 데이터셋을 다시 엽니다. 인증은 서명된 URL 또는 같은 origin의 쿠키를 사용할 수 있습니다. Range 미지원·오류·CORS 차단 시 전체 다운로드로 fallback하지 않습니다. COG처럼 부분 읽기에 적합한 레이아웃에서 효과가 크며, 파일 포맷이나 요청한 연산 자체가 전체 데이터를 필요로 할 수도 있습니다.

## 데이터와 드라이버

GDAL 3.12.4, PROJ의 `proj.db`, 전체 GDAL 데이터 파일과 종속 라이브러리의 라이선스 고지를 바이너리에 포함합니다. 외부 설치나 `GDAL_DATA` / `PROJ_DATA` 지정이 필요하지 않습니다. 선택적 지역 변환 grid 파일은 포함하지 않습니다.

공통 빌드 프로필은 GDAL 기본 드라이버와 GEOS, JPEG, PNG, SQLite, WebP, Zstd, Expat, OpenJPEG, JPEG XL입니다. JPEGXL 드라이버와 GTiff/COG의 `COMPRESS=JXL`을 검증합니다. **모든 GDAL 선택 드라이버를 포함한다는 뜻은 아닙니다.** HDF5/netCDF/Parquet/PostgreSQL 등의 추가 SDK와 CURL 기반 원격 VFS는 이 프로필에 포함하지 않습니다. JS/wasmJs의 HTTP 입력은 자체 VSI 읽기 어댑터로 지원합니다. 원래 Conda 배포와 드라이버 목록이 같다고 가정하지 마세요.

`runtime.versionInfo("THIRD_PARTY_LICENSES")`로 실제 포함된 고지를 조회합니다. 기능 감지는 `getDriver(name)`의 결과로 수행합니다.

## 하나의 진입점과 바이트 입력

GDAL 유틸리티 옵션은 `import dev.gdal4k.runtime.*` 후 vararg로 전달할 수 있습니다.
`info`, `translate`/`translateTo`, `warp`/`warpTo`, `translateVector`/`translateVectorTo`,
`rasterize`/`rasterizeTo`, `demProcessing`/`demProcessingTo`와 런타임의
`buildVRT`, 다중 입력 `warp`/`warpTo`에 적용합니다.

```kotlin
dem.demProcessing("slope", "-compute_edges", "-s", "1", driver = "COG")
dataset.translateTo("result.tif", "-co", "COMPRESS=JXL", driver = "COG")
runtime.warpTo(listOf(left, right), "mosaic.tif", "EPSG:3857", "-r", "bilinear")

// 동적으로 만든 List도 기존 방식으로 전달합니다.
dem.demProcessing("slope", arguments = listOf("-compute_edges", "-s", "1"))
```

vararg 뒤의 `driver`·`name`은 이름으로 지정합니다. 이전의 `translate("COG")`는
`translate(driver = "COG")`로, `translateVector("GPKG", "out.gpkg")`는
`translateVector(driver = "GPKG", name = "out.gpkg")`로 옮깁니다. 기존 List를 명시한
호출과 옵션 생략 호출은 지원합니다. 옵션을 생략하는 호출도 확장 함수를 사용하므로
명시적 import를 쓰는 앱은 해당 함수 또는 `dev.gdal4k.runtime.*`를 import하고 다시 컴파일합니다.
기존 바이너리의 기본 인자 호출과의 호환성을 보장하는 변경은 아닙니다.

vararg 확장 함수는 `arguments.asList()`로 배열 뷰를 전달합니다. 이 단계에서 요소를
복사하지 않으며, 내장 런타임은 첫 suspend 지점 전에 옵션을 직렬화합니다. Kotlin의
vararg 배열 생성이나 spread 전달 자체의 할당·복사까지 없애는 것은 아닙니다.

공개 사용 진입점은 `Gdal4k`입니다. `Gdal.create()`, `GdalSession`, 별도의 `GdalDataset` 및 Android 전용 세션 팩토리는 제거했습니다. 초기화·Worker·호출 직렬화·종료를 기존 runtime에 통합했습니다.

경로, 단일 바이트 파일, 다중 파일 입력 모두 `runtime.openDataset(...)`으로 열고 **같은 `Dataset` 인터페이스**를 반환합니다. 따라서 바이트 입력에서도 `getRasterBand`, `getLayer` 같은 기존 API와 편의 변환을 함께 사용합니다.

```kotlin
import dev.gdal4k.Gdal4k
import dev.gdal4k.runtime.*

suspend fun toCog(bytes: ByteArray): ByteArray {
    val runtime = Gdal4k.runtime()
    return runtime.openDataset("input.tif", bytes).use { dataset ->
        dataset.translate(
            driver = "COG",
            name = "output.tif",
            arguments = listOf("-co", "COMPRESS=JXL", "-co", "JXL_LOSSLESS=YES"),
        ).mainFile().bytes
    }
}
```

`Dataset.info()`는 JSON 문자열, `translate` / `warp` / `translateVector`는 `GdalFiles`를 반환합니다. `Dataset.readRaster(band, window)`는 기존 `Band.readRaster`를 통해 `DoubleArray`를 읽습니다. `runtime.drivers()`는 포함된 드라이버 목록을, `runtime.transform(source, target, points)`는 기존 SRS·좌표변환 API를 이용한 좌표 목록 변환을 제공합니다.

`GdalFile`은 이름과 바이트, `GdalFiles`는 Shapefile 같은 다중 파일 묶음을 담는 값입니다. 두 타입 모두 `gdal4k`에 있으며, 별도 엔진이나 데이터셋 핸들이 아닙니다. 바이트 입력은 서로 다른 임시 `/vsimem/` 디렉터리를 사용하고 데이터셋을 닫을 때 정리됩니다. 직접 지정한 경로나 `writeMemoryFile`의 파일은 호출자가 관리합니다.

JSON/Base64 전송 코드는 binary 모듈 내부에만 있습니다. 공개 `GdalOptions` 없이 내장 엔진을 사용합니다. GDAL·PROJ 경로나 웹 엔진 URL을 앱에서 지정하지 않습니다.

## 검증

테스트는 `gdal4k-runtime/src/commonTest`에 있습니다. 원래 객체 API smoke, 래스터·벡터·좌표변환, JXL 왕복, 오류·핸들 수명, 취소·재초기화, 호출 스레드/웹 이벤트 루프 응답성을 검사합니다. `verification/consumer.py`는 별도 Gradle 프로젝트에서 게시 Maven 아티팩트만 받아 실행합니다. Native 소비 테스트는 원래 producer 디렉터리를 숨기고 링크합니다.

지원 타깃 선언과 실행 검증은 다릅니다. 최신 실행 결과는 PR의 플랫폼별 CI와 JUnit 보고서를 확인합니다. iOS 디바이스 타깃은 서명된 실제 기기가 없어 링크까지, iOS 시뮬레이터는 실제 실행까지 검사합니다. Android CI는 ARM64 바이너리만 빌드하고 Google APIs x86_64 에뮬레이터의 native bridge로 ARM64 코드를 실행합니다. 에뮬레이터가 ARM64 호환을 보고하지 않으면 실패하며 번역 실행 여부를 결과에 기록합니다. 실제 ARM64 실행 검증과 이 CI 번역 실행 검증은 구분합니다. WASI는 타깃이나 테스트에 포함하지 않습니다.

## Dataset 확장 API

Dataset의 모든 호출은 공통 `suspend` API이며 기존 플랫폼별 실행 경로를 사용합니다.

- `getDescription` / `setDescription`, `getMetadataDomainList`, `getMetadata` / `setMetadata`, `getMetadataItem` / `setMetadataItem`: domain별 메타데이터. 항목 값 `null`은 삭제이며, XML domain도 보존하도록 전체 메타데이터는 GDAL의 문자열 목록을 유지합니다.
- `getSpatialRef` / `setSpatialRef`: 공간 참조 객체와 연결합니다. 조회는 독립 복제본을 반환하므로 `use`로 닫습니다. 설정은 복사하며 `null`로 제거할 수 있습니다. `getAccess`, `addBand`, Dataset `createMaskBand`, `clearStatistics`도 제공합니다.
- Dataset `readRaster` / `writeRaster`: Byte/Short/Int/Long/Float/Double 배열, 다중 밴드·window·출력 해상도를 지원합니다. 버퍼는 **band-sequential** 순서이며 밴드는 1부터 시작합니다. `RasterIOOptions.resampling`은 읽기에 적용합니다. Int64는 JSON에서 문자열로 전송해 정밀도를 보존합니다.
- `createLayer`, `copyLayer`, `deleteLayer`, `executeSQL`, `startTransaction` / `commitTransaction` / `rollbackTransaction`: 벡터 관리. SQL 결과는 `Layer.use`로 해제하며 부모 Dataset을 닫을 때도 정리됩니다. 레이어 삭제는 활성 SQL 결과를 먼저 해제하고 해당 레이어의 기존 핸들을 무효화합니다. Dataset 마스크 재생성은 이전 밴드 마스크/overview 핸들을 무효화하므로 다시 조회합니다. `runtime.createFieldDefn`, `Layer.createField`, `runtime.createFeature`로 새 레이어의 피처를 구성합니다.
- `Driver.createMultidimensional`, `Dataset.getRootGroup`, `Group` / `Dimension` / `MDArray`: 그룹·차원·숫자 배열 생성과 탐색, 부분 읽기·쓰기. `DatasetKind.Multidimensional`로 다차원 파일을 엽니다. 현재 MDArray slice는 Float64 변환으로 읽고 쓰며, 큰 Int64의 정밀도가 필요한 용도로 사용하지 않습니다. 문자열·복합 배열과 attribute API는 아직 노출하지 않습니다.
- 필드 도메인·관계 이름 조회 및 추가·조회·갱신·삭제. `createGlobFieldDomain`과 `createRelationship`으로 객체를 만듭니다. 조회 결과는 독립 복제본입니다. coded/range 도메인의 생성과 상세 값 API는 아직 노출하지 않습니다.

API가 존재해도 드라이버가 해당 기능이나 수정을 지원해야 합니다. 지원 여부는 capability와 반환 코드로 판단하며, 도메인·관계 수정 실패는 설명을 포함한 예외를 반환합니다. 기존 HDF5/netCDF 등 선택적 드라이버를 새로 번들하지 않습니다. GDAL의 전체 API를 모두 노출한다는 의미는 아닙니다.

```kotlin
val runtime = Gdal4k.runtime()
runtime.openDataset(inputPath).use { dataset ->
    val pixels = FloatArray(3 * 256 * 256)
    check(dataset.readRaster(pixels, RasterIOOptions(
        xOffset = 1024, yOffset = 512, xSize = 1024, ySize = 1024,
        bufferWidth = 256, bufferHeight = 256,
        bands = intArrayOf(1, 2, 3), resampling = RasterResampling.Average,
    )) == 0)
    dataset.translateTo(outputPath, driver = "COG",
        arguments = listOf("-co", "COMPRESS=JXL", "-co", "JXL_LOSSLESS=YES"))
}
```

### 직접 경로 출력

`translateTo(path, ...)`, `warpTo(path, target, ...)`, `translateVectorTo(path, ...)`는 지정한 위치에 쓰고 출력을 닫습니다. 반환형은 `Unit`이며 출력 전체를 `GdalFiles`/Base64/JSON으로 읽어 오지 않습니다. 기존 바이트 반환 API도 유지합니다.

JVM·Android·Native의 출력은 접근 가능한 OS/GDAL VSI 경로입니다. Android는 앱의 cache/files 디렉터리 등 쓰기 권한이 있는 경로를 사용합니다. 읽기 전용 content URI 어댑터에 쓰기 권한을 추가하지 않습니다. Node는 OS 파일을 seek/read/write하는 전용 VSI 어댑터를 사용합니다.

브라우저는 `opfs:/result.tif` 또는 `opfs:/exports/result.tif`를 사용합니다. 전용 Worker가 **OPFS의 동기 접근 핸들로 디스크에 직접 기록**하며 MEMFS 파일로 staging하지 않습니다. 디렉터리는 필요하면 생성됩니다. 같은 경로로 `openDataset("opfs:/result.tif")`를 호출해 다시 열 수 있습니다. OPFS는 해당 origin의 비공개 저장소이며, 사용자가 선택한 임의의 디렉터리에 대한 쓰기 권한과는 다릅니다. 저장된 파일은 앱에서 `navigator.storage.getDirectory()`의 `getFileHandle(...).getFile()`로 얻어 다운로드/공유할 수 있습니다. secure context와 OPFS sync access 지원이 필요하며, 저장 공간·권한 오류가 발생하면 실패합니다. 메모리 파일로 자동 fallback하지 않습니다.

출력 경로와 sidecar 파일은 호출자가 소유합니다. 오류나 취소 후 부분 파일이 남을 수 있으며 라이브러리가 기존 사용자 파일을 삭제하지 않습니다. 정상 반환은 GDAL 출력 close까지 성공했다는 뜻입니다. GDAL 드라이버의 블록 캐시·압축·재투영 작업 메모리는 계속 필요합니다. `/vsimem/`을 명시하면 그 출력은 메모리를 사용합니다.

CRS 객체의 축 순서는 GDAL 규칙을 따릅니다. 경도/위도 순서의 GeoTransform과 함께 쓸 경우 공간 참조에 `setAxisMappingStrategy(0)`을 설정합니다. GPKG처럼 스키마 생성을 지연하는 드라이버는 레이어/필드 생성 후 `flushCache()`로 스키마를 확정한 뒤 트랜잭션을 시작하는 편이 안전합니다.

### 다차원 문자열·복합 레코드와 attribute

`MDDataType.Numeric(GdalDataType.Int64)`, `MDDataType.Text(maxLength = 0)`, `MDDataType.Compound(name, components)`로 스키마를 만듭니다. 복합 타입의 메모리 정렬과 필드 위치, 문자열 할당·해제는 엔진이 관리합니다. 중첩 타입은 최대 32단계입니다. `Text.maxLength`는 UTF-8 바이트 수이며 0은 무제한입니다.

```kotlin
val runtime = Gdal4k.runtime()
requireNotNull(runtime.getDriver("MEM")).use { driver ->
    requireNotNull(driver.createMultidimensional("")).use { dataset ->
        requireNotNull(dataset.getRootGroup()).use { group ->
            val dim = requireNotNull(group.createDimension("sample", 2))
            val type = MDDataType.Compound("observation", listOf(
                MDComponent("id", MDDataType.Numeric(GdalDataType.UInt64)),
                MDComponent("label", MDDataType.Text()),
            ))
            requireNotNull(group.createArray("observations", listOf(dim), type)).use { array ->
                check(array.writeValues(longArrayOf(0), longArrayOf(1), listOf(
                    MDValue.Record(listOf(MDValue.Numeric("18446744073709551615"), MDValue.Text("영상"))),
                )))
                val first = array.readValues(longArrayOf(0), longArrayOf(1))
                requireNotNull(array.createAttribute("units", longArrayOf(), MDDataType.Text())).use {
                    check(it.writeValues(listOf(MDValue.Text("reflectance"))))
                }
            }
        }
    }
}
```

`MDValue.Numeric`는 숫자를 텍스트로 전송하여 JS에서도 64비트 정수의 모든 비트를 유지합니다. `Long`/`Int`/`Double` 편의 생성자도 제공합니다. 실수에는 `NaN` / `Infinity` / `-Infinity`를 사용할 수 있습니다. 복소수 타입은 `Numeric(real, imaginary)`를 사용합니다. 읽은 실수 텍스트는 반올림 복원에 충분한 정밀도로 정규화되므로 입력 텍스트의 형식까지 보존하지는 않습니다. `Record.fields`는 스키마의 필드 순서이며 중첩 레코드도 같은 규칙입니다. `Text(null)`과 `Text("")`는 구분합니다. NUL 문자열, 정수 범위 초과, 스키마 불일치, 잘못된 window·요소 수는 쓰기 호출 전에 거부합니다. 이후 드라이버 자체의 쓰기 실패는 트랜잭션으로 취급하지 않습니다.

`Group`과 `MDArray` 모두 attribute 생성·단건/목록 조회·삭제를 제공합니다. attribute는 작은 메타데이터이므로 값 전체를 읽고 쓰며, 빈 차원 배열은 scalar입니다. 배열 데이터는 `start`와 `count`로 필요한 부분만 요청합니다. attribute를 삭제하면 열린 별칭을 무효화하고, 부모를 닫으면 하위 핸들도 닫습니다. 각 드라이버의 타입·차원·쓰기 지원 여부는 GDAL에 따릅니다. 번들에 없는 netCDF/HDF5 드라이버가 추가되지는 않습니다.

### Coded·range 도메인

`CodedFieldValue(code, label)`은 nullable label을 지원하며 중복 코드는 거부합니다. `FieldDomain.getCodedValues()`는 coded 도메인에서만 목록을 반환합니다. `FieldDomainBound(value, inclusive)`로 숫자 텍스트 또는 DateTime을 지정하고, `null`은 해당 방향에 한계가 없다는 뜻입니다. range 도메인은 GDAL이 지원하는 Integer(0), Real(2), DateTime(11), Integer64(12)를 사용하며 `getMinimum` / `getMaximum`으로 경계를 조회합니다. DateTime은 ISO 8601로 읽어 옵니다. 다른 종류의 도메인을 조회한 경우 상세 값은 `null`입니다.

독립 도메인과 배열의 Int64 값은 정확히 전달합니다. 다만 GDAL 3.12.4의 GeoPackage 드라이버는 range 경계를 REAL로 저장합니다. 이때 정밀도를 잃는 Integer64 경계값은 `addFieldDomain` / `updateFieldDomain`에서 저장 전에 명확히 거부합니다. 다른 드라이버·파일 형식의 저장 제약도 별도로 확인해야 합니다. 이 API 확장은 GDAL 전체 API의 완전한 노출을 의미하지 않습니다.

Warp의 COG/CreateCopy 경로에 필요한 임시 GeoTIFF는 출력과 같은 파일시스템의 디렉터리에 생성됩니다. 메모리 결과는 해당 메모리 workspace, 직접 출력은 출력 파일의 디렉터리를 사용하며 기존 CPL_TMPDIR 설정이 있으면 그대로 따릅니다. 작업 후 thread-local 설정을 복원합니다. 브라우저 OPFS에서는 하나의 쓰기 핸들에 대한 읽기 재개방도 위치 기반 읽기 참조로 유지하며 쓰기 핸들을 추가로 공유하지 않습니다. 마지막 참조가 닫힐 때 실제 sync access handle을 해제합니다.


### Band analysis and resampled windows

`Band` exposes description/domain metadata, explicit scale/offset, cached extrema,
exact Int64/UInt64 NoData (`Long`/`ULong`), `fill`, statistics and 64-bit histogram
counts through common suspend methods. `getStatistics()` and `getDefaultHistogram()`
only request cached/cheap results and return null when unavailable. Choose `force=true`
or `computeStatistics()` explicitly when a scan is acceptable. `computeStatistics()`
always recomputes; driver support determines cache persistence and invalidation after edits. Cached histogram
results can remain stale after pixel/NoData changes, following the driver's GDAL policy. Statistics and RasterIO
use raw samples: they do not apply scale/offset, and statistics/histograms exclude NoData.
Approximate requests permit GDAL sampling/overviews; no approximation is requested by default.

`BandRasterIOOptions` adds source windows, independent buffer dimensions and read
resampling to all six primitive buffer overloads. Zero sizes mean the remaining source
extent or the source window's dimensions. Oversized read buffers keep their tail.
Writes require Nearest; GDAL can resize a buffer into the destination window. These
operations allocate only the requested buffer/window, not a full-band staging copy.

```kotlin
runtime.openDataset(path).use { dataset ->
    requireNotNull(dataset.getRasterBand(1)).use { band ->
        val stats = band.getStatistics() // null rather than scanning implicitly
        val preview = FloatArray(256 * 256)
        band.readRaster(BandRasterIOOptions(bufferWidth = 256, bufferHeight = 256,
            resampling = RasterResampling.Average), preview)
    }
}
```

Histogram snapshots are detached; setters validate bounds and reject negative counts.
Exact integer NoData APIs require the corresponding Int64/UInt64 band and retain every
bit across the web bridge. Color tables, raster attribute tables and native block I/O
are documented in the root README. Virtual-memory/cache pointers and progress callbacks remain unexposed.

## 래스터 Polygonize

분류·마스크 래스터의 같은 값 영역을 벡터 폴리곤으로 변환합니다.
`source.polygonize("-8")`은 GPKG `GdalFiles`를 반환하고,
`source.polygonizeTo("classes.gpkg", "-8")`는 파일에 직접 씁니다.
`band`, `mask`, `layerName`, `fieldName`, `driver`를 named argument로 지정할 수 있습니다.
기본 NoData/유효성 마스크를 사용하며 `-nomask`로 무시합니다. 기존 출력은 덮어쓰지 않습니다.
정수 값은 Int64 속성에 기록합니다(UInt64·복소수 제외). 실수는 GDALFPolygonize의
Float32 비교 규칙을 따릅니다. 입력은 호출자 소유이고, 반환 파일은 입력을 닫은 뒤에도
사용할 수 있습니다. [옵션과 정밀도·출력 정책](../README.md#분류-래스터를-폴리곤으로-변환-polygonize)을 참고하세요.

## 벡터 Rasterize

벡터 Dataset의 `rasterize(arguments=...)`는 `GdalFiles`를 반환하고, `rasterizeTo(path, arguments=...)`는 Node 파일·브라우저 `opfs:/`·OS 경로에 직접 씁니다. 입력 핸들은 호출자가 계속 소유합니다. GDAL `gdal_rasterize` 인자(`-burn`/`-a`, `-te`, `-ts`/`-tr`, `-l`/`-where`/`-sql`, `-add`, `-i`, `-at`, NoData·자료형·생성 옵션)를 사용합니다. 기본 출력은 GTiff이며, MEM/VRT 출력과 기존 Dataset 핸들에 대한 in-place 갱신은 제공하지 않습니다. 레이어 필터·읽기 위치의 변화와 geometry/CRS 처리는 GDAL 유틸리티 정책을 따릅니다. 출력 크기를 명시하고 큰 결과에는 `rasterizeTo`를 사용하세요. 직접 출력의 실패·취소 시 부분 파일은 호출자가 관리합니다.

```kotlin
source.rasterizeTo(
    outputPath,
    arguments = listOf("-a", "value", "-te", "0", "0", "2", "2", "-ts", "256", "256"),
)
```


OGR 속성 스키마는 `Layer.deleteField/reorderField/reorderFields/alterFieldDefn`과
`FieldDefn.cloneFieldDefn`, `FieldAlterFlags`로 편집합니다. 먼저 해당 스키마의 모든 Feature·clone과
같은 Dataset의 SQL 결과를 닫고 속성 필터를 해제합니다. 드라이버 호출 후에는 성공 여부와
무관하게 borrowed 정의를 다시 조회합니다. borrowed FieldDefn setter/FeatureDefn 구조 변경은
거부되며 독립 clone을 변경해 Layer API에 전달합니다. 지원 여부·변환·오류 코드는 드라이버 정책이며
자동 트랜잭션·rollback은 없습니다. `createField`에도 같은 Feature 수명 검사가 적용됩니다.

## OGR Geometry와 공간 필터

`GdalRuntime.createGeometryFromWkt/FromWkb/FromGeoJson`으로 새 Geometry를 만들고
기존 `Feature.setGeometry` / `Layer.createFeature` / `setFeature`로 피처를 작성·편집합니다.
입력 형식 오류는 예외이며 빈 WKB는 GDAL 호출 전에 거부합니다. 생성·overlay·CRS 조회·필터
조회 결과는 독립 핸들이므로 `use`로 닫습니다. CRS 할당은 좌표 변환이 아닙니다.

`Geometry.intersection/union/difference`는 GEOS 기반 평면 연산이고 자동 재투영/복구를
수행하지 않습니다. `Layer.setSpatialFilter`와 `setSpatialFilterRect`는 기본 geometry field를
필터링하고 변경 시 읽기를 초기화합니다. 공간/속성 조건은 함께 적용되지만 `getFeature(fid)`는
필터를 무시합니다. 드라이버의 envelope 후보 판정 가능성을 포함한 정확한 계약과 예시는
[OGR 사용법](../README.md#ogr-geometry-생성과-공간-조회)을 참고하세요.

### GDAL 진행률과 협력적 취소

Sieve·Proximity·FillNoData·Contour·Grid·Viewshed와 7개 레이어 overlay도 GDAL 자체 콜백에 연결됩니다. 진행률은 호출별 표본이며 연산이나 드라이버에 따라 중간 알림이 없을 수 있습니다. 제자리 수정과 이미 생성한 출력·레이어 스키마는 취소로 되돌리지 않습니다.

```kotlin
val files = withGdalProgress({ progress ->
    // 콜백은 짧고 thread-safe해야 합니다. UI에는 Flow/Channel 등으로 전달하세요.
    println("${progress.operation}: ${progress.fraction * 100}% ${progress.message}")
    true // false를 반환하면 현재 native 호출의 취소를 요청
}) {
    source.warp("EPSG:3857", "-r", "bilinear", driver = "GTiff")
}
```

`withGdalProgress`는 시그니처를 바꾸지 않고 블록의 각 GDAL 호출을 관찰합니다. 각 호출마다 fraction0~1과 메시지를 새로 보고하며, 빠른 호출은 마지막 샘플만 전달할 수 있습니다. GDAL이 중간 콜백을 제공하지 않는 드라이버·작업은 호출 경계에서만 취소를 확인합니다. Translate/Warp/VectorTranslate/DEM/Rasterize/BuildVRT/Polygonize, CreateCopy/overviews, band statistics/histograms와 RasterIO의 실제 GDAL 콜백을 연결합니다. 여러 단계의 fraction을 전체 블록의 선형 완료율로 해석하지 않습니다.

긴 작업은 관찰자를 설치하지 않아도 호출자 Job 취소를 확인합니다. 관찰자에서 false를 반환하거나 코루틴을 취소하면 CancellationException을 전달하되, native 실행 종료와 정리를 기다립니다. 관찰자 예외는 GDAL UserInterrupt로 바꾸지 않고 원래 오류(코루틴 stacktrace 복원 시 cause 포함)를 보존합니다. 콜백은 같은 런타임을 다시 호출하거나 차단하지 않아야 합니다. 서로 다른 런타임은 동일 관찰자를 동시에 호출할 수 있습니다. 중간 콜백 사이의 취소 지연이나 드라이버의 무응답을 강제 중단으로 해결하지 않습니다.

JNI·Native는 독립 control의 atomic 취소/진행률을 읽으며 GDAL native 콜백에서 Kotlin을 재진입하지 않습니다. 웹 Worker는 진행률 콜백에서 ordinary single-thread Wasm을 yield하고 취소 메시지를 실행 큐 밖에서 처리합니다. 작업·입력 핸들이 살아 있는 동안 Worker를 종료하거나 native 스레드를 강제 종료하지 않습니다. 취소/관찰자 실패 직전에 만들어진 반환 핸들도 모두 닫은 뒤 예외를 전달합니다. VRT의 입력 lease, URI/Node/OPFS mount와 directory descriptor는 실제 Dataset 해제 시점까지 유지됩니다.

입력은 호출자 소유이며 이미 쓴 출력·cursor·metadata 변경은 rollback하지 않습니다. 직접 출력의 부분 파일도 호출자가 정리합니다. HTTP·파일 mount 단계의 전송 정책은 별개이며, native 진행률 자체가 전송 제한 시간이나 무조건적인 네트워크 중단을 제공하지는 않습니다.

### OGR 컬럼 읽기

`Layer.openColumnStream(batchSize)`는 현재 속성·공간 필터를 적용해 처음부터 읽는 GDAL Arrow C stream입니다. `forEachColumnBatch`는 콜백 실패·취소에도 stream을 닫습니다. 반환 `FeatureColumnBatch`의 타입별 배열과 `valid`는 호출자 소유이며 Dataset을 닫은 후에도 유지됩니다. 정수는 Int/Long으로, 문자열은 UTF-8로, geometry는 WKB와 알려진 CRS의 `spatialReferenceWkt`로 전달하며 기본 플랫폼에 Arrow 라이브러리를 추가하지 않습니다.

```kotlin
layer.forEachColumnBatch(1024) { batch ->
    val ids = batch.columns.single { it.name == "id" } as FeatureColumn.Longs
    for (row in 0 until batch.rowCount) {
        if (ids.valid[row]) println(ids.values[row])
    }
}
```

한 native 레이어에는 stream 하나만 열 수 있습니다. stream을 닫기 전 다른 핸들로 같은 레이어의 커서·필터·스키마를 바꾸거나 관련 Dataset 연산을 실행하면 오류가 납니다. 레이어 별칭 또는 Dataset을 닫으면 stream도 닫힙니다. 종료 시 GDAL이 커서를 재설정할 수 있으므로 Feature 순회와 혼용하지 않습니다. 수동 stream은 EOF 후에도 닫아야 합니다.

primitive 정수·실수·boolean·문자열·binary와 WKB geometry를 지원합니다. nested/list/dictionary/date 등 지원하지 않는 Arrow 스키마는 명시적으로 거부하며 기존 Feature API를 사용합니다. `valid=false`는 unset과 명시적 null을 구분하지 않습니다. FID를 포함한 Arrow 컬럼 순서·이름을 유지하며 이름이 중복될 수 있습니다. batchSize는 1–65,536이고 응답은 64 MiB까지입니다. 큰 값은 더 작은 batchSize로 읽어야 하며, 단일 값에 대한 GDAL의 native 할당 크기는 별도입니다. 이 API는 zero-copy가 아닙니다. binary protocol 3 엔진이 필요하며 이전 엔진의 기존 Feature 경로는 유지됩니다.

### OGR 레이어 연산과 제한된 피처 일괄 처리

`overlayTo`는 두 입력 레이어를 새 결과 레이어로 Intersection/Union/SymDifference/Identity/Update/Clip/Erase 합니다. 두 입력은 독립된 native 레이어 커서여야 합니다. 같은 Dataset.getLayer로 다시 얻은 핸들도 같은 레이어이면 거부합니다. 서로 다른 레이어 사이에서도 커서를 공유하는 드라이버는 별도로 연 입력 Dataset을 사용해야 합니다. 결과도 두 입력과 달라야 하며, 쓰기·필드 생성이 가능하고 속성 필드·피처·필터가 없어야 합니다. 먼저 결과 레이어의 살아 있는 Feature와 같은 데이터셋의 SQL 결과를 닫습니다. 결과 스키마의 빌린 정의 핸들은 드라이버 호출 전에 무효화되므로 실패 코드 이후에도 다시 조회합니다. 좌표계가 양쪽에 알려져 있는데 다르면 거절합니다. GEOS 평면 연산이며 자동 재투영·도형 복구·트랜잭션은 없습니다. 기본 geometry field를 사용하고 입력 커서/필터에는 GDAL 의미가 적용됩니다.

```kotlin
val code = input.overlayTo(method, output, LayerOverlayOperation.Intersection,
    "INPUT_PREFIX=left_", "METHOD_PREFIX=right_")
check(code == 0)

layer.forEachFeatureBatch(batchSize = 256) { features ->
    // 이 콜백을 벗어나면 모든 임시 Feature가 닫힙니다. 보관하려면 안에서 cloneFeature().
    for (feature in features) println(feature.getFID())
}
```

`readFeatureBatch(limit)`는 현재 필터·커서에서 최대 limit개를 읽고 소유권을 호출자에게 줍니다. 빈 목록은 EOF이며 실제 GDAL 읽기 오류는 예외로 구별합니다. 모든 반환 Feature를 닫아야 합니다. `forEachFeatureBatch`는 기본적으로 먼저 rewind하고 매 콜백 뒤 실패·취소 시에도 Feature를 정리합니다. 같은 레이어를 다른 코루틴에서 동시에 순회·변경하지 않습니다.

`writeFeatureBatch`는 Create/Replace/Upsert를 순차 호출하고 첫 오류에서 `completed`와 `errorCode`를 반환합니다. 이전 쓰기와 GDAL의 FID 변경은 유지되고 입력 핸들은 닫지 않습니다. 원자성이 필요하면 명시적으로 지원되는 데이터셋 트랜잭션을 사용하고 실패 코드를 확인해 예외를 던집니다.

```kotlin
dataset.flushCache() // GPKG처럼 새 테이블 DDL을 미루는 드라이버의 스키마를 먼저 확정
// 다른 작업과 같은 Dataset 트랜잭션을 공유하지 않습니다.
dataset.withTransaction {
    val result = output.writeFeatureBatch(features, FeatureWriteMode.Create)
    check(result.successful) { "Written ${result.completed}, GDAL error ${result.errorCode}" }
}
```

`withTransaction`은 start 성공 뒤에만 트랜잭션을 소유하며 성공 시 commit, 블록 실패·취소·commit 오류 시 rollback을 시도합니다. 정리 오류는 원래 오류에 suppressed로 보존합니다. 자동 중첩/에뮬레이션/fallback은 없으며 드라이버의 구조 변경 rollback과 핸들 재조회 정책은 호출자가 관리합니다. 반환된 nonzero 값 자체는 예외가 아니므로 블록에서 검사해야 합니다. 트랜잭션 도중 외부에서 commit/rollback하거나 Dataset을 닫지 않습니다.

`rasterCalculate`, `Band.reclassifyTo`, `zonalStatistics`는 창 단위 연산으로 전체
래스터를 한 번에 메모리에 올리지 않습니다. [예제와 NoData·정밀도·정렬·취소 정책](../README.md#창-단위-래스터-계산재분류구역-통계)을 참고하세요.

`Dataset.grid`/`gridTo`와 `viewshed`/`viewshedTo`는 포인트 보간과 DEM 가시권을
처리합니다. [예제와 좌표계·입력 NoData 제약](../README.md#포인트-보간과-가시권-분석)을 참고하세요.

`Band.sieveTo`, `proximityTo`, `fillNoData`와 `Dataset.contour`/`contourTo`로
작은 분류 영역 제거, 거리 래스터, 결측값 보간과 등고선을 처리합니다.
[사용법과 정밀도·제자리 수정 정책](../README.md#래스터-정리거리등고선)을 참고하세요.

Proximity의 Byte·UInt16·UInt32 출력은 GDAL이 signed 임시 TIFF를 사용합니다. 명시한 `CPL_TMPDIR`은 유지하고, 기본값에서는 파일 출력의 디렉터리 또는 MEM 출력의 수명 제한 `/vsimem` 작업 공간을 사용합니다. MEM 출력의 임시 TIFF도 메모리를 사용하며 Float32·Float64 출력은 이 unsigned 변환용 임시 파일을 요구하지 않습니다.

### HTTP deadlines, retries and cancellation

For direct HTTP(S) inputs on JS/wasmJs, select a policy explicitly:

```kotlin
val options = DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(
    requestTimeoutMillis = 10_000,
    maxRetries = 2,
    retryDelayMillis = 200,
    maxRetryDelayMillis = 2_000,
    requireStrongETag = true,
))
withTimeout(30_000) {
    Gdal4k.runtime().openDataset(url, options).use { dataset ->
        val pixels = ByteArray(256 * 256)
        dataset.getRasterBand(1)!!.use { band ->
            check(band.readRaster(0, 0, 256, 256, pixels) == 0)
        }
    }
}
```

Import `dev.gdal4k.Gdal4k`, `dev.gdal4k.runtime.*` and `kotlinx.coroutines.withTimeout`. The per-attempt deadline covers response headers and the complete range body; coroutine cancellation also interrupts opening, an active fetch and retry backoff. GDAL completion and handle cleanup are awaited. Cancellation leaves caller-owned inputs available for later operations; output writes and GDAL cursor/metadata changes are not rolled back.

Defaults retain 30 seconds per attempt and zero retries. Retry applies only to network failures/timeouts and HTTP 408/425/429/500/502/503/504; exponential delay and Retry-After are capped by maxRetryDelayMillis. Authentication failures, 404, full 200 responses, invalid ranges/lengths/encoding and changed validators are not retried. Ranges require 206 and exposed Content-Range plus identity Content-Encoding. There is no full-file fallback or new arbitrary file-size cap; each requested offset/length must be a safe JavaScript integer. The input range cache stays bounded at 8 MiB with 64 KiB chunks.

A strong ETag is sent with If-Range on later requests. Without requireStrongETag, a valid Last-Modified is the fallback, and servers without either validator provide size checks only. Expose ETag/Last-Modified, Content-Range, Content-Length and Retry-After through CORS as applicable, and allow Range/If-Range. Observed identity changes poison that input: close and reopen the Dataset. GDAL's own block cache may serve already-read pixels without a new HTTP request, so this is consistency checking on actual provider reads, not an always-fresh remote view.

The same options apply to explicitly mapped sidecars, including independently signed URLs:

```kotlin
Gdal4k.runtime().openDataset(
    source = shpUrl,
    relatedFiles = mapOf("roads.shx" to shxUrl, "roads.dbf" to dbfUrl, "roads.prj" to prjUrl),
    options = options,
).use { dataset ->
    // Read layers and features here. Each HTTP sidecar uses the selected policy.
}
```

This overload uses `dev.gdal4k.runtime.openDataset` on JS/wasmJs. The main URL must retain its real file name and extension; map sidecars by the file names GDAL expects. Each URL has its own identity validator.

This policy covers direct JS/wasmJs HTTP inputs and related sidecars. Explicit selection is rejected for local/memory/File inputs and default Native/JNI engines, whose optional CURL backend is not supplied. Engine bootstrap and bundled resource loading are separate; no HTTP support is silently claimed for the other targets.

### Raster array transport and cancellation

`Dataset`/`Band` `readRaster` and `writeRaster`, plus raw `Band.readBlock`/`writeBlock`, keep the same public Kotlin signatures. Bundled JNI, Native, JS and wasmJs engines negotiate a binary raster channel: small JSON metadata and little-endian primitive bytes replace one JSON object/string conversion per sample. Int64 values and floating-point bit patterns do not pass through JavaScript numbers. JNI and Native stage native buffers; the Worker transfers privately owned ArrayBuffers. The caller's arrays remain attached. On little-endian JS hosts, Short/Int/Float/Double arrays use TypedArray byte views and bounded bulk copies rather than per-sample boxed Long arithmetic. Int64 scalar encoding uses two 32-bit words. Kotlin/Wasm GC arrays still require a copy to/from JavaScript storage; this is not a zero-copy API.

Large buffer preparation and decoding run while holding the runtime operation lock; queued calls do not allocate staging arrays, and concurrent reads do not accumulate decoded buffers outside that lock. Encoding and decoding check coroutine cancellation between bounded chunks and yield to other work. Read data is decoded in private storage and committed to the caller array only after the final cancellation check. The final copy is not interrupted; cancellation racing with that commit may observe a fully updated array. A cancelled or failed read before commit leaves the destination unchanged. Once GDAL starts writing, cancellation does not roll back data, cursor/metadata changes or partial output files. Do not modify input/output arrays concurrently with an operation.

Native calls continue to await real GDAL completion before freeing buffers, controls or input leases. Algorithms with GDAL progress callbacks can stop cooperatively during work. A driver call without such callbacks may finish its current native I/O before cancellation is delivered; no thread or Worker is forcibly terminated. Older dynamic JNI/web engines without the negotiated capability retain the JSON path. `GdalFiles`, geometry coordinates and multidimensional/attribute arrays still use their existing serialization paths.

`verification/raster-transport-report.py <engine-target>` summarizes the JVM same-engine JSON/binary comparison (8 MiB Float64 MEM raster, three alternating trials of three write/read roundtrips for Float64 and Byte arrays), cumulative JVM allocation and cancellation observations. Published JS/wasm KLIB verification also measures the raw Worker protocol on Node and Chromium with a 2 MiB Float64 MEM raster. Those Worker measurements exclude Kotlin/Wasm GC interop copies. Timing is reported as evidence, never used as a test pass threshold or a cross-platform performance promise.

The ordinary engine producer reuses verified prebuilt artifacts, but refuses a dependency installation through an `installed` symlink after a cache/manifest miss. Shared read-only dependency prefixes therefore require an explicit read-only rebuild rather than an implicit vcpkg install.

`verification/js-codec-benchmark.cjs` records the compiled Kotlin/JS Float64/Int64 codec separately, including module hashes and exact bit checks. It isolates CPU conversion from Worker/GDAL and dispatcher scheduling; the pinned compiler friend exports are used only by this diagnostic, not by production code.

### Raster window traversal and memory measurements

`Band.forEachRasterWindow(buffer, windowWidth, windowHeight, window) { region, samples -> ... }`
reads raw values into the same caller-owned Byte/Short/Int/Long/Float/Double array, in row-major
order. Only `region.width * region.height` leading samples are valid for that callback; edge
windows are clipped and the unused tail is unchanged. Copy samples if retaining a window.
The callback is awaited before the next read. Cancellation is checked between windows and
before invoking the callback; caller handles stay open. Earlier callback effects are not
rolled back, and a native I/O call without a progress callback may finish before cancellation.

```kotlin
requireNotNull(dataset.getRasterBand(1)).use { band ->
    val buffer = DoubleArray(256 * 256)
    band.forEachRasterWindow(buffer, 256, 256) { region, samples ->
        for (i in 0 until region.width * region.height) {
            // Consume samples[i] here. The array is reused for the next window.
        }
    }
}
```

Buffer capacity must cover the largest clipped window. There is no automatic NoData, scale,
offset, CRS conversion or dataset snapshot. Do not mutate data/handles/buffer concurrently.
Driver caches, bridge staging copies, callback allocations and retained results are additional
memory. Whole-image reads can be faster; window traversal trades more I/O calls for bounded
working memory. This is not zero-copy I/O.

`python verification/window-memory-report.py` compares full-image reads, per-window allocation
and caller-buffer reuse on the same 2048×2048 uncompressed tiled Float64 GTiff. It rotates order
across three trials, three scans per trial, and starts a fresh JVM for every mode/trial. macOS
and Linux OS peak RSS includes the whole child JVM, GDAL, caches, initialization and cleanup;
cumulative JVM allocation, 2ms-sampled peak JVM heap used and operation elapsed time are reported separately. Heap sampling can miss brief peaks; OS peak RSS is its own whole-process high-water metric. The 16 MiB
GDAL cache setting is isolated to these benchmark processes, not a library default. Node,
Chromium, Wasm GC, Native and Android peak memory are not measured by this JVM probe. Correct
sample counts/checksums and source/engine hashes are required; timing and memory are evidence,
not a pass threshold or a cross-platform promise.

### Bulk coordinate transforms

`CoordinateTransformation.transformPoints` preserves its in-place nested-array API and supports
mixed 2D/3D/4D rows. Bundled engines use bounded GDAL multi-point calls and negotiate binary
protocol version 2 for coordinates. Version 1 engines keep binary raster support and use JSON
for coordinates. Input and decoded output are staged privately; failed transforms or cancellation
before final commit leave caller arrays unchanged. Cancellation racing the final copy may observe
completed output. Do not mutate arrays concurrently. GDAL controls axis order, operation selection,
height and time semantics. This does not add reprojection algorithms, coordinate repair or zero-copy.

`verification/coordinate-memory-report.py after --target <platform>` records fresh-JVM JSON/binary
comparisons using the same current engine, coordinate inputs and correctness checks. It separates
operation time, cumulative JVM allocation, sampled heap and actual OS peak RSS. It runs on Linux
x64 and macOS ARM64 CI; no timing threshold or Native/web/Android performance guarantee is implied.
An optional `before` phase needs a separately preserved, hash-verified old engine; historical baseline
results must not be relabelled as a current-source run.
