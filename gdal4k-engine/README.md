# 내장 GDAL 빌드와 배포

`gdal4k`는 공통 인터페이스, `gdal4k-runtime`은 모든 타깃의 구현입니다. 앱은 `gdal4k-runtime`과 엔진을 추가하며, 공통 인터페이스는 runtime을 통해 함께 제공됩니다. JVM 엔진은 `gdal4k-jvm-binaries` 모듈에서 리소스 JAR로 만들고 `gdal4k-engine` 루트 좌표의 classifier로 게시합니다. 소비 앱에는 GDAL, vcpkg, Emscripten, CMake가 필요하지 않습니다. 이 도구들은 라이브러리를 소스에서 만드는 producer에만 필요합니다.

## 고정된 소스

- GDAL 3.12.4 및 JPEG XL을 포함하는 `engine/vcpkg.json`과 `ports` overlay
- vcpkg `3cd21e48a75f67acfef289da52893ce79144aaa1`
- 웹: Emscripten 4.0.23 (`emsdk` commit `c0bb220cb6e6f4e0fabb6f6db9efd53390ef5e56`)
- Linux/Windows: Kotlin/Native 2.4.21에 포함된 sysroot / C++ ABI를 사용
- Android: NDK r28c, API 28 이상, 16 KiB 정렬
- Apple: macOS 11 / iOS 15 이상. Kotlin/Native 자체 지원 범위도 적용됩니다.

`tools/build-engine.py`는 고정 버전의 vcpkg를 준비하고 소스에서 GDAL과 정적 의존성을 빌드합니다. 시스템에 설치된 GDAL로 fallback하지 않습니다. `manifest.json`에 소스 fingerprint와 산출물 SHA-256을 기록하고 재사용 시 모두 검증합니다.

## Producer 로컬 실행

JDK 21, Python 3, Git, 플랫폼 SDK가 필요합니다.

```sh
python3 -m venv verification/tools/python
verification/tools/python/bin/pip install -r verification/requirements.txt
export PATH="$PWD/verification/tools/python/bin:$PATH"
./gradlew :gdal4k:compileKotlinMacosArm64
python3 gdal4k-engine/tools/build-engine.py macos-arm64
./gradlew :gdal4k-runtime:jvmTest :gdal4k-runtime:macosArm64Test
python3 verification/consumer.py --target macosArm64
```

호스트에 맞춰 `macosArm64` / `macos-arm64`를 바꿉니다. Linux ARM64는 x64 Linux 또는 macOS에서 교차 빌드하고 ARM64 호스트에서 실행합니다. Windows 바이너리는 Windows에서 Kotlin/Native의 MinGW 도구로 빌드합니다. Android producer는 `ANDROID_NDK_HOME`, Android Gradle은 `ANDROID_HOME`을 사용합니다. Android는 `arm64-v8a`만 지원하며 AAR도 이 ABI만 포함합니다. 다른 ABI를 지정하면 빌드 설정 단계에서 실패합니다.

웹은 `.github/workflows/library-tests.yml`과 같은 SDK를 준비·활성화합니다.

```sh
source verification/tools/emsdk/emsdk_env.sh
./gradlew :gdal4k-runtime:jsNodeTest :gdal4k-runtime:wasmJsNodeTest
CHROME_BIN=/path/to/chrome ./gradlew :gdal4k-runtime:jsBrowserTest :gdal4k-runtime:wasmJsBrowserTest
python3 verification/consumer.py --target js
python3 verification/consumer.py --target wasmJs
```

웹 엔진은 `gdal3.js`를 사용하지 않습니다. 자체 빌드한 `gdal4k.mjs`, `gdal4k.wasm`, Worker와 입력 어댑터를 JS/wasmJs KLIB에 같은 정적 자산으로 넣습니다. 엔진을 Kotlin/Base64 문자열로 펼치거나 Blob/data URL로 모듈을 재생성하지 않습니다. Kotlin Gradle의 Maven→로컬 npm 추출과 webpack의 정적 `new URL(..., import.meta.url)` 처리로 앱에 자산을 함께 배포합니다. 별도 npm 엔진 게시, CDN 또는 앱의 GDAL 경로 설정이 필요하지 않습니다.

브라우저는 별도 `.wasm` 응답을 스트리밍 컴파일합니다. Node는 엔진 파일의 정확한 범위를 가리키는 `ArrayBuffer`를 전달해 Emscripten이 `Buffer` 내용을 다시 복사하는 단계를 없앱니다. 엔진 파일 바이트는 초기 컴파일에 필요하지만 입력 영상 전체를 적재하는 것은 아닙니다. 앱 호스팅은 `.wasm`에 `application/wasm` MIME을 제공해야 스트리밍 컴파일을 사용할 수 있으며, 전송량을 줄이려면 일반 정적 자산처럼 gzip/Brotli 압축과 캐시를 적용합니다. KLIB 게시와 독립 Kotlin 소비 테스트에 필요한 자산이 빠지면 검증이 실패합니다.

웹 입력은 GDAL VSI 콜백을 Worker의 비동기 읽기에 연결합니다. Asyncify로 C 호출을 대기·재개하므로 SharedArrayBuffer, COOP/COEP 또는 JSPI를 요구하지 않습니다. Asyncify는 Wasm 코드 크기와 실행 비용을 추가하므로 로딩 방식 변경의 메모리 효과와 함께 측정합니다.

웹 빌드에서 gdal4k가 생성하던 GDAL 보조 데이터 157개의 정적 배열을 제거합니다. `gdal4k-resources.mjs`의 파일명·크기·위치만 등록하고, GDAL이 파일을 읽을 때 해당 구간을 `gdal4k-data.wasm`에서 읽습니다. 이 파일은 KGP가 Maven 아티팩트에서 자동 추출할 수 있는 **Wasm custom section 데이터 컨테이너**이며 실행하거나 인스턴스화하지 않습니다. Node는 디스크의 필요한 구간을, 브라우저는 정적 자산의 HTTP Range를 읽습니다. 호스팅은 이 데이터 자산에도 206/Content-Range 응답을 지원해야 합니다. 입력 파일과 같은 LRU에 읽은 구간만 보관하며 전체 데이터를 `/vsimem`에 복제하지 않습니다. GDAL 내부 fallback 리소스와 PROJ의 `proj.db`, 실행 코드는 여전히 엔진에 내장합니다.

웹 초기 작업 힙은 1 MiB에서 시작하고 정적 데이터·스택을 합친 초기 Wasm 메모리 크기는 링커가 계산합니다. 필요한 할당에 따라 증가하며, 별도의 GDAL 블록 캐시 상한은 설정하지 않습니다. 늘어난 Wasm 메모리를 실행 중에 다시 축소하는 기능은 없으므로 엔진 전체 반환에는 Worker 종료가 필요합니다. 래스터 읽기는 출력 배열의 길이만 엔진으로 보내고, 출력용 배열의 기존 내용을 JSON으로 복제하지 않습니다. 반환 래스터와 나머지 JSON 브리지 비용은 남아 있습니다.

## 산출물과 게시

- JVM 코드: `gdal4k-runtime-jvm`에는 JNI 호출 코드만 포함하며 기본 바이너리 전이 의존성이 없습니다.
- JVM 리소스: `gdal4k-engine:<version>:<classifier>`. classifier는 `macos-arm64`, `linux-x64-gnu`, `linux-arm64-gnu`, `windows-x64`이며 각 JAR에 해당 플랫폼 JNI 라이브러리 하나만 포함합니다. Linux는 glibc용이며 Alpine/musl은 지원하지 않습니다.
- Native: 구현체 `gdal4k-runtime-<target>`에는 C 바인딩만 포함. 명시적으로 선택하는 `gdal4k-engine-native-<platform>` KLIB에 GDAL과 선택된 종속성의 정적 archive 포함
- Android: 플랫폼 Kotlin 구현체에는 JNI를 포함하지 않음. 별도 `gdal4k-engine-android-arm64` AAR (`arm64-v8a`만 포함), Android 9 / API 28 이상
- 웹: 구현체 `gdal4k-runtime-js` / `gdal4k-runtime-wasm-js`에는 엔진 자산을 포함하지 않음. 별도 `gdal4k-engine-js` / `gdal4k-engine-wasm-js`가 같은 Worker·ES module·Wasm 자산 제공
- `gdal4k`, `gdal4k-runtime`: `0.1.0-SNAPSHOT`
- `gdal4k-engine`, JVM 리소스 JAR, Android JNI AAR: `0.1.0-SNAPSHOT`

사용자는 엔진 루트 `gdal4k-engine`을 선언합니다. Native·Android·웹 variant의 실제 payload 좌표는 아래와 같으며 JVM은 루트 좌표의 classifier를 사용합니다. 모든 버전은 `0.1.0-SNAPSHOT`입니다.

| 실행 대상 | 엔진 artifactId |
| --- | --- |
| Native macOS ARM64 | `gdal4k-engine-native-macos-arm64` |
| Native Linux x64 / glibc | `gdal4k-engine-native-linux-x64-gnu` |
| Native Linux ARM64 / glibc | `gdal4k-engine-native-linux-arm64-gnu` |
| Native Windows MinGW x64 | `gdal4k-engine-native-mingw-x64` |
| Native iOS ARM64 | `gdal4k-engine-native-ios-arm64` |
| Native iOS Simulator ARM64 | `gdal4k-engine-native-ios-simulator-arm64` |
| JS | `gdal4k-engine-js` |
| wasmJs | `gdal4k-engine-wasm-js` |
| JVM macOS ARM64 | classifier `macos-arm64` |
| JVM Linux x64 / glibc | classifier `linux-x64-gnu` |
| JVM Linux ARM64 / glibc | classifier `linux-arm64-gnu` |
| JVM Windows x64 | classifier `windows-x64` |
| Android ARM64 | `gdal4k-engine-android-arm64` |

`gdal4k-engine` 루트의 Gradle metadata는 Native·Android·JS·wasmJs 타깃에 맞는 위 좌표를 선택합니다. JVM은 기본 payload가 없는 variant를 선택하고 별도 classifier를 명시합니다. Kotlin API·구현체는 `gdal4k-runtime`과 `gdal4k` 및 각 KMP 타깃 좌표로 게시합니다. 구현 코드와 로더는 `gdal4k-runtime`에 통합하며 `gdal4k`에 의존합니다. Gradle producer 모듈 이름과 네이티브 파일의 리소스 경로는 Maven artifactId와 독립적입니다.

GDAL을 실행하는 Kotlin 앱은 `gdal4k-runtime` 루트 좌표에 의존하며 Gradle이 Kotlin 타깃 아티팩트를 선택합니다. 모든 플랫폼에서 엔진을 명시적으로 선택해야 합니다. JVM에서는 필요한 플랫폼 classifier를 `runtimeOnly`로 지정합니다([사용 예시](../README.md)). KMP `jvmMain.dependencies`에도 같은 설정을 사용합니다. 기본값이나 전체 플랫폼 묶음 JAR은 없습니다.

예를 들어 macOS ARM64 classifier만 로컬 게시하려면 `./gradlew :gdal4k-engine:publishKotlinMultiplatformPublicationToMavenLocal -Pgdal4kJvmClassifiers=macos-arm64`를 실행합니다. 쉼표로 classifier를 여러 개 지정할 수 있으며 선택한 엔진만 빌드합니다. CI는 플랫폼별 검증 산출물을 모은 뒤 네 classifier를 같은 루트 publication에 함께 게시합니다. 플랫폼별 runner가 같은 Snapshot 좌표를 독립적으로 게시하지 않습니다. `verification/consumer.py --target jvm --jvm-binary macos-arm64`는 해당 classifier와 runtime 게시 후 외부 소비를 검증합니다. 테스트 도구의 호스트 선택은 제품 의존성 기본값이 아닙니다.

CI는 타깃별 빌드·공통 테스트·독립 소비 테스트 후 아티팩트를 모읍니다. 코드 JAR에 바이너리가 없는지, POM/Gradle metadata에 기본 바이너리 의존성이 없는지, JVM classifier 4종의 각 JAR에 정확히 해당 리소스만 있고 SHA-256이 맞는지 검사합니다. `verification/engine-consumer.py`는 별도 KMP 앱의 commonMain에서 엔진 루트를 선언하고 타깃별 선택 결과와 JVM classifier 격리를 검증합니다. 미선택·잘못된 플랫폼·복수 플랫폼 의존성도 별도 JVM 소비 테스트로 확인합니다. Android AAR에 ARM64 이외의 ABI가 들어가면 실패합니다. 전체 Maven 좌표는 라이브러리 22개와 엔진 10개를 합쳐 32개이며 JVM classifier JAR 4개는 엔진 루트 좌표의 추가 파일입니다. 모든 플랫폼의 라이브러리 POM/Gradle metadata에 엔진 의존성이 없고, JAR/KLIB/AAR에 엔진 파일이 없는지도 검사합니다. `verification/publish.py`는 합쳐진 **검증 완료 Maven 파일**을 재빌드 없이 게시합니다. PR에서는 게시하지 않으며 main push 또는 수동 publish 선택 시 모든 검증 성공 후 실행합니다. 실제 원격 게시 성공은 CI 검증 성공과 별도로 확인해야 합니다.

기존 Conda/SWIG producer 스크립트는 더 이상 배포 경로가 아닙니다. 드라이버 프로필과 비동기 API의 변경 사항은 [공통 API 문서](../gdal4k-runtime/README.md)를 참고하세요.

## 웹 직접 읽기 검증

```sh
# published KLIB 안의 자산만 추출해 검증합니다.
python3 verification/web-inputs.py --repo verification/build/consumer-maven --target js
python3 verification/web-inputs.py --repo verification/build/consumer-maven --target wasmJs
# 입력 파일을 제외한 엔진 초기화 메모리 측정(macOS/Linux, Chromium)
python3 verification/web-inputs.py --memory-only
# 동일 sparse TIFF 전체 checksum·JXL 변환·window 읽기의 전후 비교
python3 verification/web-inputs.py --memory-only --workload --profile-label after
# 필요한 window만 읽는 경우를 별도로 측정
python3 verification/web-inputs.py --memory-only --window-only --profile-label window-after
```

256 MiB sparse TIFF에서 처음·마지막 window의 정확한 픽셀을 HTTP Range, 선택한 브라우저 File, Node 디스크 경로로 검사합니다. Range 미지원·잘못된 Content-Range·파일 변경·CORS 오류 뒤의 복구, 64 KiB 요청과 전체 8 MiB 캐시 상한 및 close 시 캐시 반환도 검사합니다. 입력을 `/vsimem`에 복사하지 않습니다. 브라우저의 파일 선택 권한은 File 객체를 얻기 전에 앱이 처리합니다.

메모리 보고서는 `verification/results/web-engine-memory-optimized/profile.json`에 저장합니다. 엔진만 측정하며 Kotlin 앱과 영상 작업 메모리는 포함하지 않습니다. RSS는 코드·런타임·공유 페이지·미회수 임시 할당을 포함하므로 정확한 엔진 allocator 크기로 해석하지 않습니다.

`--workload`를 사용하면 256 MiB 영상 전체 checksum, 1024×1024 무손실 JXL 변환 3회, 512×512 래스터 읽기를 포함합니다. `--profile-label`별로 보고서를 보존하고, 실행 중 다른 빌드·측정 작업과 겹치지 않게 비교합니다. 테스트는 데이터 자산의 전체 다운로드 없이 내장 NZTM2000 타일 정의를 읽어 COG를 생성하는 경우도 검사합니다.


## CI 게시 대상

main push의 전체 검증이 성공하면 검증한 Maven 파일을 기본적으로 Maven Central에 게시합니다. 현재 모든 아티팩트의 버전 `0.1.0-SNAPSHOT`은 `https://central.sonatype.com/repository/maven-snapshots/`로 전송합니다. 수동 workflow도 `mavenCentral`이 기본이며 `githubPackages`는 명시적으로 선택할 수 있습니다. PR에서는 원격 게시하지 않습니다.

GitHub Actions의 `mavenCentral` Environment에 PostGIS 저장소와 같은 Secrets를 등록합니다. 기존 이름도 계속 사용할 수 있으며 둘 다 있으면 기존 GDAL 이름을 우선합니다.

| PostGIS와 같은 이름 | 기존 GDAL 이름 | 내용 |
| --- | --- | --- |
| `MAVEN_USER` | `MAVEN_CENTRAL_USERNAME` | Central Portal 사용자 토큰 username |
| `MAVEN_PASS` | `MAVEN_CENTRAL_PASSWORD` | Central Portal 사용자 토큰 password |
| `SIGNING_CERT` | `SIGNING_KEY` | ASCII-armored PGP 개인키 또는 그 Base64 값 |
| `SIGNING_PASS` | `SIGNING_PASSWORD` | 개인키 암호 |

`SIGNING_KEY_ID`는 선택입니다. 공개키는 Central에서 조회할 수 있도록 공개키 서버에 등록되어 있어야 합니다. 다른 저장소의 Secrets는 자동 공유되지 않으므로 이 저장소에도 등록합니다. Environment variable `MAVEN_CENTRAL_NAMESPACE`는 Portal에 표시되는 실제 namespace로 설정합니다(기본 `dev.tronto.gdal4k`). 토큰은 해당 namespace의 게시 권한이 있어야 하며, 상위 namespace인 `dev.tronto`가 등록되어 있다면 그 값을 지정합니다.

Snapshot은 PGP 서명 없이 Snapshot 저장소에 직접 게시하며 해당 namespace의 Snapshot 게시가 활성화되어 있어야 합니다. 정식 버전(`-SNAPSHOT`이 없는 버전)은 전체 검증 후 CI에서 PGP 서명 → 검증한 파일 업로드 → 같은 runner에서 Portal에 `publishing_type=automatic` 전달 → 해당 deployment가 `PUBLISHED`가 될 때까지 확인합니다. 실패·시간 초과는 CI 실패로 처리하며 Portal 전달 요청을 자동으로 재시도하지 않습니다. Portal에서 수동 Publish를 누를 필요는 없습니다. 이 흐름은 [Sonatype OSSRH 호환 API](https://central.sonatype.org/publish/publish-portal-ossrh-staging-api/)와 [deployment 상태 API](https://central.sonatype.org/publish/publish-portal-api/)를 사용합니다.

현재 버전은 Snapshot이므로 이 변경만으로 정식 버전이 공개되지는 않습니다. 정식 릴리스를 만들 때는 소스의 `gdal4kVersion`을 정식 버전으로 변경하여 전체 CI에서 같은 라이브러리 버전의 구현체와 엔진을 함께 검증·게시합니다. PR에서는 원격 게시하지 않으며 로컬 `--destination` 게시 검증 및 GitHub Packages 게시에서는 Portal API를 호출하지 않습니다. 루트 Gradle의 개별 publication 게시 명령은 업로드만 수행합니다. 전체 검증 파일을 모은 `verification/publish.py`가 CI의 최종 공개 경로입니다.

`python3 -m unittest discover -s verification -p test_publish.py`는 공개 성공·실패·시간 초과, 현재 deployment 식별, Snapshot/로컬/GitHub Packages의 분기와 업로드 실패 시 미공개를 원격 요청 없이 검사합니다. 실제 원격 게시 성공은 CI 검증 및 설정 검증과 별도로 확인해야 합니다.


## Gradle 도구 준비

`prepareNativeBuildTools`는 CMake·Ninja·vcpkg를, `prepareWebBuildTools`는 Emscripten까지 준비합니다.
`buildEngine*` Gradle task에도 연결되어 있습니다. 기본 경로는 Gradle User Home의
`build-tools/{cmake,ninja,vcpkg,emsdk}`이며 프로젝트·워크트리 간 버전별 설치를 공유합니다.
`GRADLE_USER_HOME`과 `--gradle-user-home`을 따르며 도구별 경로도 변경할 수 있습니다.
vcpkg 설치 결과·buildtrees·packages와 CMake 빌드 출력은 각 프로젝트에 분리합니다.
기존 설치는 `localDirectory`로 명시하면 버전 검증 후 수정 없이 사용합니다.
설정·오프라인 동작·호스트 지원 범위는 [도구 준비 문서](../docs/build-tools.md)를 참고하세요.

Linux x64/ARM64 및 Windows MinGW 엔진 task는 엔진에 의존하지 않는 `gdal4k` API 모듈의
대응 Native 컴파일을 먼저 실행합니다. Kotlin/Native 컴파일러와 타깃 의존성은 KGP가 준비하므로
첫 빌드 전에 별도의 초기화 명령을 실행할 필요가 없습니다. Kotlin/Native 저장소를 바꾸는
`konan.data.dir` 설정도 producer에 전달합니다.

vcpkg는 소스 revision과 실행 파일 release를 각각 고정하고, Gradle이 호스트별 실행 파일을
SHA-256으로 검증해 직접 설치합니다. bootstrap 스크립트나 Git Bash 설치는 필요하지 않습니다.
Gradle이 전달하는 빌드 환경에서는 POSIX 압축 명령도 관리 CMake/Python으로 처리합니다.
Linux의 libcurl 런타임, OS SDK 및 vcpkg 호스트 도구 컴파일러 요구사항은
[개발자 준비물](../docs/build-tools.md#developer-prerequisites)을 참고하세요.
