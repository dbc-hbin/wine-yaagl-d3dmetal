# Wine 11.20 D3DMetal (GPTK 4.0b3)

[English](README.md)

Apple Silicon과 Rosetta 2를 위한 GPTK 4.0 beta 3 전용 Yaagl 런타임 소스입니다. Wine host, msync, 입력, 미디어, 오디오, 창 및 리소스 관리 패치를 유지합니다. 네이티브 sidecar는 디스플레이 라우팅, 비디오 메모리 정책과 FSR-to-MetalFX 번역을 담당합니다. 셰이더, 라이브러리, PSO와 RT 함수 재사용은 GPTK 자체 네이티브 캐시가 계속 담당합니다.

DLSS는 Apple 원본 `nvngx-on-metalfx.dll`과 Unix 모듈을 사용합니다. 실험용 DLSS 번역기, Streamline 가로채기, NPC 이름표 합성은 포함하지 않으며 이름표 수정 완료를 주장하지 않습니다.

## 현재 소스와 패치 정책

공식 `wine-11.20` 태그를 기반으로 검증된 소스 `1846c16674b`의 런타임 변경을 이식한 트리입니다. 빌드 입력은 `dlls/`, `server/`, `include/`의 Wine 소스입니다. 유지한 계약과 업스트림 중복 처리 기준은 [이식 기록](documentation/WINE-REBASE.md)에 정리합니다. 과거 P3/tuned 패치 묶음은 원본 커밋에 참고 자료로 보존하며, 이 트리에 다시 적용하거나 복사하지 않습니다.

beta3 훅 계약은 `d3dmetal-sidecar/layout.json`에 정의되어 있습니다. format 32, marker `YAAGLB3SIDEcar\x20`, dispatch 슬롯 27개이며 추가 `binaryPatches`는 없습니다. 처음 네 훅은 `GetContainingOutput`, `SetFullscreenState`, `QueryVideoMemoryInfo`, `ReplayTemporalScale`로 유지합니다. Async 훅 12개는 graphics PSO 생성·조회·해제, MPL 일반·indexed draw, root signature 수명, command list reset·query·수명과 Present를 담당합니다. DX11 훅 5개는 일반·indexed draw, query Begin·End와 context 해제를 담당합니다. `CompleteExecute`는 완료된 FSR 실행의 리소스를 회수하고, 그 뒤에 `PresentResidencyAdd` 호출 지점 훅이 옵니다. 이어지는 저지연 훅 4개는 native NVAPI 조회, Present 완료, swapchain 해제와 device 해제를 담당합니다. `ngx-hooks.mm`은 기록된 FSR 명령을 처리하고, 나머지는 Apple 원본 replay로 전달합니다. FSR SR/FG는 Metal4/MPL transport를 사용합니다.

`PresentResidencyAdd`는 `DoPresent`의 layer set 등록 호출 `0x16d242`만 교체합니다. 조사한 macOS 27 IOGPU 구현은 등록을 `NSCountedSet`으로 강하게 보유하고 고유 set 32개 제한을 검사합니다. 큐마다 현재 layer set을 한 번만 등록하며, 다른 set으로 바뀌면 이전 등록을 먼저 제거하고 새 set을 추가합니다. 큐의 다른 등록은 유지합니다. 이 호출은 native worker가 직렬화하며, 큐에 연결된 상태는 교체 또는 큐 파괴까지 layer set을 최대 하나만 보유합니다. layer 교체에 따른 등록 누적을 수정한 것이며 실제 게임의 메모리 급증 해결을 입증한 것은 아닙니다. `node --test scripts/test-present-residency.mjs`는 실제 생성된 x86 호출·gate·trampoline과 production 훅을 Foundation의 실제 counting·소유권 동작으로 128회 layer 교체에 걸쳐 검사하며, D3DMetal을 로드하거나 GPU 작업을 실행하지 않습니다.

현재 Wine FSR 진입점과 native 회귀 검사의 호출처는 Metal4를 선택하며, 저장소 안에서 backend의 Legacy command mode를 선택하는 호출처는 없습니다. SR C++ 인터페이스는 여전히 일반 `MTLCommandBuffer`를 사용하는 Legacy를 받으며 sidecar가 C++ factory/encode 심볼도 export하므로, 저장소 조사만으로 외부 호출자가 없다고 확정할 수는 없습니다. Legacy는 명시적인 인터페이스 전환 전까지 유지합니다. AMD native provider로의 원본 전달과는 별개이며, `YAAGL_FSR_UPSCALER=native`나 FG native fallback은 MetalFX Legacy를 선택하지 않습니다.

과거의 커스텀 PSO/function 캐시, RT 출처 정보 추적, 캐시 문맥 훅과 라이브러리 singleflight는 제거한 상태를 유지합니다. `GetRenderPipelineState`에는 비동기 작업 예약 wrapper를 추가했지만, Metal PSO의 실제 컴파일과 소유권은 Apple 원본 getter와 캐시가 담당합니다. 과거 device destructor 지점은 저지연 상태를 정리한 뒤 원본 destructor를 호출하는 용도로만 사용합니다. 나머지 과거 캐시 진입점은 원본을 유지하며, GPTK 자체 셰이더/stage·디스크 캐시, 라이브러리 게시·재사용, owner별 graphics/compute PSO와 RT 함수 재사용도 유지합니다.

DXGI 비디오 메모리 조회, 출력 모니터 조회, 전체 화면 진입점과 출력 열거는 원본 메서드를 호출할 때도 Windows x64 호출 규약을 사용합니다. 네이티브 내부 헬퍼는 호스트 ABI를 유지합니다. 이 규약이 섞이면 렌더링 전 Unity 기동 단계에서 크래시가 발생합니다.

converter는 원본을 유지합니다. 준비 도구는 `native-sidecar-patch`, `native-sidecar-module`, `final-framework-reseal` 순서로 적용합니다. sidecar 소스는 `d3dmetal-sidecar/`에 있으며, `scripts/build-d3dmetal-sidecar.mjs`가 `libD3DMetalSidecar.dylib`를 빌드하고 `scripts/d3dmetal-sidecar-patch.mjs`가 패치를 담당합니다. 패키징 옵션은 `--sidecar-module`과 `--sidecar-manifest`, manifest 필드는 `nativeSidecarBuild`와 `nativeSidecar`, recipe 필드는 `patches.nativeSidecar`입니다. 내부 C++ namespace `yaagl::pso`와 공개 FSR C API는 유지합니다. 제외한 패치의 이유는 다음과 같이 구분합니다.

| 제외한 패치 | 현재 정책의 근거 |
|---|---|
| FP64 codec recipe | 알려진 실패 codec fixture가 beta3 원본 converter에서 컴파일되며 cast 구현도 기존 recipe와 다릅니다. 모든 FP64/constrained-cast 입력의 정상 동작을 증명한 것은 아닙니다. |
| Stage-lock·compute loser 해제 | beta3 `CompileCompute`가 이미 stage-cache lock을 잡고 중복 할당을 해제합니다. |
| CacheFile close/munmap | beta3가 파일을 닫고 payload와 header를 unmap한 뒤 상태를 초기화합니다. |
| CacheFile publication-order | reader가 불완전한 record를 거부하고 cache를 닫거나 다시 엽니다. writer 순서를 기본 구성에서 바꿔야 할 correctness·캐시 재사용 문제는 재현되지 않았습니다. |
| Legacy fullscreen null/forward call-site | beta3가 native output을 직접 전달합니다. Yaagl 모니터 라우팅은 별도의 sidecar 기능으로 유지합니다. |
| 매 프레임 present-residency 제거 | 대신 호출 지점 훅이 큐마다 현재 layer set 하나를 유지하고 set이 바뀔 때만 교체합니다. 중복 counted 등록을 피하고 큐의 기본 set 등록을 보존합니다. |
| Persistent-cache read-ahead | 파일 warmup은 셰이더 컴파일이 아닙니다. 셰이더·PSO 프리컴파일과 디스크 캐시는 게임과 D3DMetal이 담당합니다. |
| 커스텀 DLSS·Streamline 처리 | 이 런타임은 원본 NGX 쌍을 사용하며 실험용 이름표 처리는 범위 밖입니다. |

커서 처리는 현재 `cocoa_app.m`과 `cocoa_warpconsumption.h` 구현을 사용합니다. 이 정책들을 별도 런타임 브랜치의 모든 수정과 동일하다고 설명하지 않습니다.

## 선택적 비동기 graphics pipeline

실제로 발행한 비어 있지 않은 draw만 attachment 반복 사용 이력을 갱신합니다. DX11·DX12에서 생략한 draw는 5개 epoch 이력을 연장하지 않습니다. 실제 draw가 target 이력을 갱신하지 않으면 적격성이 만료되고 다음 draw는 네이티브 동기 경로로 처리합니다. 정점·인덱스 수 또는 instance 수가 0인 DX12 draw는 작업 요청과 target 사용 기록 없이 async 판정을 건너뜁니다.

DX12 동기 요청과 owner 전체 대기는 아직 시작하지 않은 작업을 직접 가져와 호출 스레드에서 컴파일하므로 GCD 실행 차례를 기다리지 않습니다. 이미 실행 중인 작업에는 합류하며, 네이티브 해제 전 owner의 모든 variant가 완료돼야 한다는 조건은 유지합니다. 큐 callback은 state·네이티브 PSO에 접근하기 전에 독립적으로 보유한 claim 토큰을 확인합니다. 따라서 동기 처리와 owner 해제가 끝난 뒤 callback이 도착해도 중복 컴파일이나 해제된 owner 접근이 발생하지 않습니다. Worker 병렬도와 QoS는 기존 GCD 정책을 유지합니다. Ready 확인값의 캐시 확대는 다중 PSO miss 측정에서 느려져 채택하지 않았습니다.

고정된 native FFX presenter에서는 PE 합성 callback의 context 수명 식별자, frame ID와 생성·실제 프레임 구분을 같은 스레드의 다음 일치하는 실제 swapchain Present로 전달합니다. 생성 프레임 제출이 성공한 뒤 같은 프레임의 실제 출력이 이어져도 공통 epoch는 한 번만 증가합니다. 생성 프레임만 또는 실제 프레임만 출력할 때도 진행하며, 실패·TEST·native counter가 변하지 않은 호출은 세지 않습니다. 생성 전용 설정은 불변 binding에 보존하고 뒤따를 실제 프레임을 기다리는 쌍을 남기지 않습니다. No-notify 설정은 swapchain에 여전히 설치된 binding을 유지합니다. DLL의 RSDS 빌드 식별자와 기본 합성 함수의 진입 바이트로 분석한 callback 순서의 적용 대상을 제한합니다. 앱 callback과 원본 native 기본 UI 합성은 descriptor, premultiplied-alpha 확장과 user context 의미를 유지합니다. 알 수 없는 native 빌드는 기존 동작을 유지합니다. 5개 연속 epoch 조건과 만료 규칙은 바꾸지 않으며, 서로 독립적인 실제 swapchain Present는 기존 공통 시계를 계속 사용합니다.

런타임 실행 환경에 `YAAGL_D3DMETAL_ASYNC=1`을 설정하면 DX12 graphics PSO 백그라운드 컴파일과 DX11의 네이티브 컴파일 대기 중 draw 생략을 활성화합니다. 기본값은 비활성입니다. `YAAGL_D3DMETAL_ASYNC_LOG=1`은 `dx11-skipped` 등을 기록합니다. 네이티브 읽기 전용 `yaagl_d3dmetal_async_stats` export의 구조는 `d3dmetal-sidecar/async-pipeline.hpp`에 있습니다. queued·ready·failed·synchronous는 sidecar가 관리하는 DX12 요청을, skipped는 두 API의 생략을 셉니다. 드라이버 cache miss나 프레임 시간 개선을 뜻하지 않습니다.

DX12의 미완료 PSO draw 생략은 reset으로 추적된 MPL direct command list의 `DrawInstanced`와 `DrawIndexedInstanced`에만 적용합니다. 활성 query가 없고, geometry·tessellation·mesh·stream-output 경로가 아니며, 일치하는 root signature가 UAV와 descriptor heap 직접 인덱싱을 허용하지 않아야 합니다. 실제 texture·mip·slice attachment의 5개 연속 Present epoch 사용 이력도 필요합니다. 생략할 때 dirty state를 유지하므로 이후 draw가 완료된 pipeline을 정상 바인딩합니다. 첫 사용 target과 알 수 없는 root 표현 등은 동기로 처리하며, DX12의 `ExecuteIndirect`·mesh·legacy·compute·RT 실행은 생략하지 않습니다.

DX11은 beta3의 기존 백그라운드 컴파일러와 원자적 미완료 작업 플래그를 재사용합니다. `Draw`·`DrawIndexed`와 각 instanced 형태는 deferred command list 재생을 포함해 컴파일 대기 중 네이티브 draw 명령만 생략할 수 있습니다. context 잠금, 바인딩 명령과 리소스 사용 추적은 원래 순서를 유지합니다. 활성 query·predication·graphics UAV·geometry·tessellation·stream-output 및 첫 사용 attachment는 원래 draw 경로로 처리합니다. 네이티브 texture view와 subresource 식별값이 5개 연속 Present epoch에서 사용돼야 생략할 수 있습니다. 미완료 플래그가 해제됐다고 모든 동적 variant가 존재하는 것은 아니므로, 예약되지 않은 variant와 indirect draw·`DrawAuto`는 무기한 생략하지 않고 원래 컴파일 동작을 따릅니다.

오래된 subresource 이력 정리는 texture별 Present epoch당 한 번 수행합니다. DX11·DX12 이력 관측에는 runtime registry 잠금 대신 texture별 잠금을 사용합니다. 같은 epoch에서 이미 관측을 마친 attachment 묶음은 스레드별 메모로 반복 기록을 생략합니다. 매번 새로 조회한 history의 고유 식별값, 정확한 subresource key와 원자적 변경 번호를 확인하며 texture를 추가 보유하지 않습니다. 다른 스레드의 이력 초기화, texture 주소 재사용 또는 epoch 변경 시 다시 관측합니다. Cold draw의 생략 여부는 모든 attachment의 이력 잠금을 일정한 순서로 잡아 판정하며, 64개 이력 초과 시 초기화와 texture 소유 수명 규칙은 유지합니다.

DX12는 정확히 일치하는 동적 key가 ready이고 owner의 미완료 작업이 없으면 생략 조건 검사를 건너뜁니다. 네이티브 draw와 target 이력 관측은 계속 수행해 이후 cold PSO의 반복 사용 판정을 유지합니다. 스레드별 ready 확인값으로 registry와 owner 상태의 반복 잠금을 생략하며, 새 variant 등록이나 owner 정지는 작업 시작 또는 네이티브 PSO 해제 전에 이 확인값을 무효화합니다. 미완료 owner는 기존 안전 조건과 unsafe draw의 owner 전체 대기를 유지합니다. DX11 전용 프로세스에서는 첫 managed DX12 PSO가 등록되기 전까지 공통 getter의 DX12 owner-map 잠금을 생략합니다. 등록 플래그는 PSO 해제 시 초기화하지 않으므로 두 API를 함께 쓰는 프로세스는 기존 owner 조회를 유지합니다.

DX12의 UAV 판정은 원본 converter의 root-signature 파서를 재사용합니다. 비어 있는 shader binding mask만으로 쓰기가 없다고 추정하지 않습니다. 셰이더 변환·함수 로딩·reflection 생성은 여전히 동기입니다. Sidecar가 관리하는 DX12 작업은 graphics 생성자가 끝난 뒤 시작하고 네이티브 PSO 해제 전에 완료를 기다리며, DX11은 기존 네이티브 작업과 destructor 대기 절차를 유지합니다. DX12 cold draw의 eligibility 조회와 pipeline owner 보유를 같은 잠금 안에서 처리해 중복 잠금·pipeline 조회를 제거하며, draw 사이에 eligibility를 캐시하지 않습니다. 이 모드에서는 물체가 늦게 나타날 수 있습니다. target 반복 사용은 휴리스틱이지 모든 렌더링 의존성이 draw 누락을 허용한다는 증명은 아닙니다.

`node --test scripts/test-async-pipeline.mjs`는 동일 key 작업 중복 방지, 완료·실패·해제와 eligibility를 결정적으로 검사하며, 실제 texture-history 구현의 만료, mip·slice 분리, 객체 수명과 용량 경계도 검사합니다. `scripts/d3dmetal-async-smoke.cpp`는 별도 staged runtime에서 실제 D3D12·GPU readback으로 draw 재개, query·UAV 동기 처리, indirect root 상태 초기화와 PSO 해제를 검사합니다. 빌드 명령은 영문 README의 같은 절에 있으며, 격리한 prefix에서 async를 끈 경우와 켠 경우를 실행합니다. 첫 draw 이전에 컴파일이 끝난 경우도 정상으로 취급하고, pending 동작은 sleep이나 컴파일 속도에 의존하지 않는 네이티브 테스트로 검사합니다.

`scripts/d3dmetal-dx11-async-smoke.cpp`는 실제 DX11 GPU readback, multithread 보호를 켠 context 잠금, 네 가지 direct draw 형태, query·UAV 동기 처리, 첫 사용 target과 리소스 수명·상태 복원을 포함한 deferred 재생을 검사합니다. 빌드 명령은 영문 README의 같은 절에 있으며 async OFF/ON으로 실행합니다. 네이티브 테스트는 pending 플래그를 명시적으로 유지해 컴파일 속도에 의존하지 않고 생략·완료·예약되지 않은 variant의 진행과 안전 조건을 검사합니다.

## 선택적 2프레임 저지연 모드

`YAAGL_D3DMETAL_LOW_LATENCY=1`을 설정하고 게임의 Reflex 저지연 옵션을 켭니다. 기본값은 비활성이며 async 컴파일 옵션과 독립적입니다. 원본 NVAPI·Unix bridge를 거쳐, 게임이 입력을 받기 전에 호출하는 `NvAPI_D3D_Sleep`에서 대기합니다. 이 호출이 없는 게임에 입력 훅을 강제로 추가하지 않습니다.

Reflex 호출 없이 적용하려면 `YAAGL_D3DMETAL_LOW_LATENCY=force`를 설정합니다. 실제로 진행한 Present가 반환되기 전에 미완료 Present가 2개 미만이 될 때까지 기다려 다음 프레임의 공간을 확보합니다. 이때 native Present의 잠금은 이미 해제된 상태입니다. TEST, 비진행 호출과 `DXGI_PRESENT_DO_NOT_WAIT`에는 추가 대기를 넣지 않으며 DXGI waitable object 설정과 swapchain 생성 플래그는 유지합니다.

강제 모드에서도 게임이 저지연을 요청하고 실제 Sleep을 호출하면 입력 전 경로로 전환합니다. 설정만 켜고 호출하지 않는 경우에는 전환하지 않습니다. Sleep 호출이 중단되거나 게임에서 Reflex를 끄면 Present 대기로 돌아가며, 두 경로에서 동시에 기다리지 않습니다. 따라서 게임의 Reflex OFF만으로 명시적으로 선택한 강제 모드가 꺼지지는 않습니다. 전체 기능을 끄려면 환경변수를 해제합니다. 강제 모드는 임의 게임의 입력 위치를 알 수 없으며 별도 시뮬레이션 스레드가 앞서가는 것까지 막지는 못합니다.

협력 경로에서는 CPU가 예약한 프레임과 native Present 완료를 기다리는 프레임을 합쳐 최대 2개만 허용합니다. Sleep에서 먼저 예약하므로 렌더 스레드가 늦어져도 CPU가 계속 앞서가지 않습니다. Native Present 완료가 여유 공간을 반환하며 TEST·비진행 Present는 소비하지 않습니다. 협력 경로의 Present에는 추가 대기를 넣지 않습니다.

TEST 뒤 실제 Present가 이어지면 예약을 유지합니다. TEST만 수행한 입력 반복이나 CPU 예약이 여러 개여서 TEST의 소속을 판별할 수 없는 경우에는, 발생하지 않을 GPU 완료를 기다리는 대신 대기를 비활성화합니다.

두 모드 모두 활성 swapchain이 여러 개면 대기를 중단하며, 알려진 FFX 생성 프레임·Reflex out-of-band marker·겹치는 Present는 대기를 비활성화합니다. 협력 전용 모드는 동기화 이후 예약 없이 들어온 Present도 비활성화 조건입니다. 강제 모드보다 이 안전 조건이 우선합니다. 대기 경로 전환, 제외 판정과 native 객체 정리는 해당 대기자를 깨웁니다. Boost 요청은 힌트로 받지만 Apple GPU 클럭을 바꾸지 않습니다. NVAPI 최소 프레임 간격이 0이 아닌 요청은 `NVAPI_NOT_SUPPORTED`를 반환합니다. 고정 깊이 제한이며 FPS 제한기는 아닙니다. Sleep status는 실제 적격성과 관측한 대기 시간을 보고하며, `GetLatency`는 원본 구현으로 유지하므로 새로운 입력-화면 지연 측정값이 아닙니다.

`node --test scripts/test-low-latency.mjs`는 이벤트 기반 동기화로 2개 제한, 지연 제출, 반환 전 완료 callback, rollback, 강제·Reflex 전환, 비차단 Present, 취소, 해제와 device 분리를 검사합니다. Native ABI·수명 근거와 측정 범위는 [저지연 조사 및 구현 기록](documentation/LOW-LATENCY-DESIGN.md)에 정리합니다.

## Wine과 sidecar 빌드

Xcode, Node.js, Python, LLVM MinGW, Bison과 x86_64 TLS/font/media 의존성이 필요합니다. `WINE_DEPS_ENV`에 의존성 환경 파일을, `WINE_MINGW_ROOT`에 설치된 LLVM MinGW 경로를 지정합니다. 다른 소스를 내려받는 대신 미커밋 변경을 포함한 현재 워크트리를 빌드합니다.

```sh
WINE_NATIVE_SERVER=yes scripts/build-wine-p3.sh all
node scripts/build-d3dmetal-sidecar.mjs build/native-sidecar
```

`WINE_P3_ROOT`로 새 빌드 디렉터리를 지정할 수 있으며 기본값은 `build/wine-p3-current`입니다. `host/`와 provenance에는 실제 Wine 소스가 기록됩니다. sidecar 산출물에는 dylib과 소스 해시 manifest가 포함됩니다. 기존 빌드 디렉터리가 있다는 사실만으로 현재 소스의 빌드가 검증되지는 않습니다.

staging 요약은 `native_build_manifest.d3dmetal.hookCount`에서 실제 훅 수를 읽습니다.

### ZZZ 게임 모드 세션

전체 소스 빌드는 `scripts/game-mode.py`로 `scripts/game-policy-supervisor.c`에서 독립적인 universal `game-policy-supervisor`를 빌드하고 Apple 원본 서명의 `gamepolicyctl`을 복사합니다. 두 도구는 `ZZZ.app` 밖의 `libexec/yaagl-game-mode/`에 유지합니다. 내부 x86_64 host는 선택된 Wine 빌드의 `loader/main.o`와 생성된 링크 규칙을 재사용하며, `scripts/yaagl-game-mode.plist`를 실행 파일에 내장하고 번들의 `Info.plist`에도 같은 metadata를 설치합니다. supervisor와 host 번들을 각각 서명하며 deep signing이나 Apple 도구 변경은 하지 않습니다. staging은 서명, 아키텍처, metadata와 인접 `ntdll.so` 부재를 검증하고 최종 파일을 inventory에 기록합니다. 로컬 및 framework 제외 패키지 manifest는 `gameMode: {"version":3,"target":"zzz","supervisor":"libexec/yaagl-game-mode/game-policy-supervisor","host":"libexec/yaagl-game-mode/ZZZ.app/Contents/MacOS/wine"}`를 요구하며 버전 1과 2는 호환되지 않습니다.

런처의 선택적 기능은 Apple Silicon/macOS 26 이상에서 ZZZ만 대상으로 합니다. `game-policy-supervisor <absolute wineserver> <absolute Wine loader> [Wine args...]`를 실행하고 게임 환경 변수와 표준 입출력을 그대로 전달합니다. 기존 renderer wrapper는 실행 경로에 유지합니다. 끄면 일반 Wine을 실행하며 macOS 정책을 강제로 끄지 않습니다.

선택 기능이 켜진 게임 실행에만 `YAAGL_GAME_MODE_TARGET`을 게임 EXE의 절대 Unix 경로로 설정하고 setup에는 전달하지 않습니다. Wine은 명령행이나 파일 이름이 아니라 실제로 해석한 이미지의 파일 식별값을 비교하여 해당 이미지에만 host를 선택합니다. host는 네이티브 게임 앱 식별값과 Game Mode metadata를 제공하며 정책 관리는 담당하지 않습니다. Finder나 `open`으로 실행하는 앱이 아니라 내부 explicit-exec 전용입니다. `argv[0]`은 일반 Wine loader의 정규 경로를 유지하므로 수정하지 않은 `loader/main.c`가 번들 내부 조회 실패 후 원래 런타임 ntdll을 찾습니다. 인접 ntdll이나 번들 밖으로 향하는 심볼릭 링크를 설치하지 않으며 현재 no-preloader 빌드를 지원합니다.

게임 모드가 창을 선택하거나 네이티브 전체 화면에 자동 진입·이탈·재진입시키는 처리를 제거했습니다. 창 모드와 테두리 없는 전체 화면은 일반 Wine의 창 위치와 크기를 유지합니다. 사용자가 직접 네이티브 전체 화면에 진입하면 기존 전환 콜백에서 지연된 최신 Wine 위치 요청을 보존하고, 노치 영역을 포함한 실제 AppKit content geometry를 통지합니다. 정확히 같은 geometry의 확인 응답은 대기 중인 위치 요청을 지우지 않습니다. `python3 scripts/test-game-mode-fullscreen.py`는 실제 Objective-C 메서드를 합성 화면에서 실행해 창 모드 포커스, 테두리 없는 창, 수동 전환, geometry 확인 응답과 실패 경로를 검사하며 데스크톱 Space를 변경하지 않습니다.

선택 기능을 켜면 supervisor가 같은 디렉터리의 Apple 도구를 실행해 **시스템 전체** 게임 모드 override를 요청합니다. 네이티브 게임 식별값이나 전역 정책 상태만으로 특정 게임의 스케줄링 이점 또는 창 모드·테두리 없는 전체 화면의 정책 효과를 입증할 수 없으며, 해당 효과는 런타임 관찰 전까지 미검증입니다. 사용자 실행에는 Xcode나 `xcrun`이 필요하지 않으며 빌드할 때만 선택된 Xcode에서 도구를 가져옵니다. 패키징은 원본 바이트와 서명을 보존하고 supervisor는 실행 전에 Apple 식별값과 privileged entitlement를 검사합니다. 도구 누락, 심볼릭 링크 또는 신뢰 검사 실패 시 외부 개발 도구를 찾지 않고 거부합니다. 정책 획득에 실패하면 stderr에 경고를 출력하고 게임은 계속 실행합니다. 실행 성공만으로 정책 활성화를 의미하지 않습니다.

정책 소유권의 수명은 특정 게임 프로세스가 아니라 현재 Wine 세션입니다. Wine 명령이 종료된 뒤 같은 환경에서 지정된 `wineserver -w`가 끝날 때까지 기다린 후 소유권을 해제합니다. 따라서 같은 Wine prefix의 백그라운드 프로세스가 남아 있으면 게임 창을 닫은 뒤에도 정책이 유지될 수 있습니다. 같은 사용자의 겹치는 세션은 전역 소유권을 공유하고 마지막 세션이 기존 auto/on/off 정책을 복원합니다. Wine 명령의 0이 아닌 종료 코드는 서버 종료 대기 후에도 보존합니다. 관찰된 외부 정책 변경은 유지하고 복원 실패 기록은 다음 실행에서 복구하도록 남깁니다. supervisor 자체를 SIGKILL로 종료하면 즉시 복원을 보장할 수 없으며 외부에서 같은 forced-on 값으로 다시 설정한 경우도 구별할 수 없습니다. `python3 scripts/test-game-mode-policy.py`는 시스템 정책을 변경하지 않는 상태 기반 로컬 backend로 세션 수명을 검사합니다.

전체 빌드와 staging 이후 `python3 scripts/game-mode.py validate build/staged/wine`으로 host와 두 도구를 검증할 수 있습니다. `python3 scripts/test-game-mode-routing.py build/staged/wine`은 검사 전용 probe로 네이티브 실행 파일 경로를 관찰하면서 실제 Wine 자식 생성, 파일 별칭, 실제 이미지와 다른 명령행, 비대상 자손 및 host 누락 시 실패를 확인하며 정책 도구는 호출하지 않습니다. 실제 검증에서는 창 모드와 테두리 없는 전체 화면의 게임 앱 식별값을 관찰하고 실행 중 `build/staged/wine/libexec/yaagl-game-mode/gamepolicyctl game-mode status` 및 Wine 세션 종료 후 이전 정책 복원을 확인해야 합니다. 물리적인 다중 모니터 동작은 보조 디스플레이를 연결한 상태에서 별도로 확인해야 합니다.

## Apple framework 로컬 준비

원격 재배포 파일을 가정하지 않습니다. 네이티브 준비 도구를 만들 때 beta3 원본 framework를 지정합니다.

```sh
node scripts/build-d3dmetal-autopatch.mjs build/d3dmetal-autopatch \
  --framework "$GPTK_FRAMEWORK" --accept-apple-license \
  --sidecar-module build/native-sidecar/libD3DMetalSidecar.dylib \
  --sidecar-manifest build/native-sidecar/build-manifest.json
```

생성된 도구는 로컬 Apple beta3 디스크 이미지와 동봉 sidecar를 사용합니다.

```sh
build/d3dmetal-autopatch/prepare-d3dmetal-runtime \
  --dmg "$GPTK_DMG" --output build/prepared-d3dmetal \
  --accept-apple-license
```

입력, 패치 및 서명을 검증한 뒤 결과를 공개하며 디스크 이미지는 읽기 전용으로 마운트합니다. `--framework`, `--license`, `--acknowledgements`를 각각 지정하는 직접 입력도 지원합니다. Yaagl은 Apple 라이선스 동의를 받은 뒤에만 동의 플래그를 전달해야 합니다. 이 로컬 준비 작업은 사용 중인 Yaagl 런타임을 설치하거나 교체하지 않습니다.

## 완전한 로컬 런타임 스테이징

`GPTK_RUNTIME`은 `lib/external`과 원본 PE/Unix 그래픽 모듈을 갖춘 beta3 Wine 그래픽 배포 디렉터리이며, 새로 빌드한 Wine host와 구분합니다. 의존성 라이브러리, GStreamer framework와 원본 네이티브 FSR 프레임 생성 DLL도 지정합니다.

```sh
python3 scripts/stage-full-wine-runtime.py \
  build/wine-p3-current/host "$GPTK_RUNTIME" build/native-sidecar build/staged/wine \
  --dependency-lib "$DEPENDENCY_LIB" \
  --gstreamer-framework "$GSTREAMER_FRAMEWORK" \
  --native-fg "$FSR_NATIVE_FG_DLL" --accept-apple-license
python3 scripts/stage-runtime.py --verify-runtime build/staged/wine --current-sources
```

스테이징된 런처는 `yaagl-d3dmetal-runtime.json`을 사용하며 Metal4/MetalFX와 RTX 4060 식별값을 설정합니다. `YAAGL_FSR_UPSCALER=metalfx`는 번역 업스케일러를 선택하고, `native`는 네이티브 업스케일러와 패키지의 프레임 생성 브리지를 사용합니다. DLSS 경로는 원본 NGX PE/Unix 쌍을 유지합니다.

명시적인 `wineboot`·`wineboot.exe` 실행에서는 선택적 Mono/Gecko 설치 대화상자를 생략해 무인 프리픽스 초기화가 끝나도록 합니다. 해당 프로세스 트리에만 적용하고 FSR 로더 정책은 유지하며, 영구적인 DLL 비활성화 설정을 기록하지 않습니다. 일반 애플리케이션 실행에는 이 초기화 전용 비활성화가 적용되지 않습니다.

`package-wine-p3-runtime.sh`로 완전한 로컬 런타임을 패키징합니다. `package-yaagl-runtime.py STAGED_WINE_ROOT AUTOPATCH_DIR OUTPUT_DIR`는 검증된 런타임과 네이티브 준비 도구를 묶되 Apple framework를 제외합니다. 분리 패키징 진입점도 같은 beta3 계약을 사용합니다. Apple framework의 재배포에는 Apple 라이선스가 적용됩니다.

## 검증

변경한 native transport, 패처, 준비 도구, staging, 런처의 관련 테스트를 실행합니다. 빌드나 CPU fixture 통과는 게임 화질이나 FSR 프레임 생성 동작의 증명이 아닙니다. 실제 런타임 시나리오 결과는 정적 검사·단위 테스트와 구분해 기록합니다.

FSR 오버헤드 최적화는 번역 입력, 정밀도, 소유권과 history/reset 결정을 보존합니다. 패스 통합에는 명시적인 Metal4 visibility barrier와 형식별 GPU 비교를 사용합니다. SR은 feature 안에서 내장 셰이더 library를 공유하고, FG는 같은 context·device의 configuration 사이에서 library와 불변 compute pipeline을 공유하되 interpolator와 history identity는 분리합니다. Unix API 주소는 성공한 경우 acquire/release 방식으로 게시하여 이후 호출의 로더 mutex를 생략하며, 조회 실패는 다시 시도할 수 있습니다. FG는 closing context의 마지막 참조가 반환될 때만 파괴 대기자를 깨우고, 객체 보관 공간을 GPU 인코딩 전에 예약합니다.

임시 COM 장치 참조는 shared control block 없이 scoped 소유권으로 관리하며 장기 보관 참조는 유지합니다. SR은 검증한 출력 layout을 재사용합니다. FG는 configure 잠금 안에서 mode 조회를 재사용하되 native 선택 뒤에 갱신하고, 동일 HUD-less 참조는 유지하면서 metadata를 갱신합니다. 같은 scene/present texture는 한 실행 안에서 raw view를 공유합니다. FG 상수는 실행별 불변 buffer 하나로 묶으며 GPU가 쓰는 disable hint의 복사 offset도 함께 반영합니다. SR argument table은 execution lease와 함께 보관하고 lease 반환 뒤에만 재사용합니다. 크기가 제한된 idle pool로 다른 프레임에서도 재사용하되 선택적 binding을 지우고 dormant feature에는 반환하지 않습니다. 사용 중인 lease끼리는 가변 table이나 상수 저장 공간을 공유하지 않습니다.

FG는 execution별 argument table 하나를 모든 번역 패스에 사용합니다. Dispatch마다 모든 binding을 갱신한 뒤 snapshot하며, 동시 execution은 별도의 table을 유지합니다. SR은 현재 연산과 depth 형식에 필요한 pipeline만 만들고 이후 프레임이 다른 연산을 요청하면 추가하며, debug 출력은 쓰지 않는 일반 finish 상수를 할당하지 않습니다. Transport는 실행별 lease를 해당 Metal4 command buffer에 등록합니다. 검증한 beta3 GPU 완료 훅이 원래 완료 함수의 fence 통지와 buffer pool 반환 전에 해당 buffer의 lease만 회수합니다. 기록된 입력은 재실행을 위해 allocator가 계속 소유하고, 미제출 작업은 allocator 소유권으로 보호합니다. 부분 인코딩 실패도 게시된 lease를 완료 또는 allocator 반환까지 유지합니다. C++ owner state는 Objective-C recorded owner 안에 유지합니다.

Metal4 SR은 독립적인 exposure·motion·color·mask 전처리를 encoder 하나로 묶습니다. 생성 색상의 padding은 dispatch visibility barrier 뒤에서 이미 변환된 활성 영역의 가장자리 값을 복사하므로 비선형 transfer 연산을 반복하지 않습니다. Mask는 생성할 때 clamp된 padding까지 채웁니다. 활성 입력 크기와 정확히 일치하는 호환 캐시가 있으면 큰 현재 scaler보다 우선하며, 없으면 기존 high-water 재사용을 유지합니다. 캐시 재활성화 시 sequence reset과 대기 중인 프레임의 소유권을 유지합니다. 입력별 원점, 부동소수점 정밀도와 Legacy padding도 유지합니다.

Metal4 FG는 baseline 복사와 전처리를 묶고 blit→dispatch visibility 및 MetalFX 앞의 fence를 유지합니다. R32Float depth/motion 정규화는 서로 다른 샘플 격자·jitter 보정·상수 블록을 유지하면서 dispatch 하나로 처리하며, 다른 depth 형식은 저장된 depth를 읽는 경로를 사용합니다. 추론용 scene/UI decode도 각 원점을 유지하면서 통합합니다. 명시적 UI 합성과 최종 출력 scatter는 별도로 유지합니다.

FG context는 반환된 scratch 묶음을 하나만 보관합니다. 정규화 입력, 생성 출력, 선택적인 추론 UI·distortion, 상수, argument table과 비운 residency set이 대상이며 입력 snapshot과 현재·이전 scene history는 제외합니다. 사용 중인 lease가 묶음을 독점하고, OFF/DESTROY는 idle slot을 비우며 epoch 검사로 늦은 반환이나 stale replay가 다시 채우지 못하게 합니다. Native 대기 프레임과 PE configuration snapshot은 64칸의 고정 저장소를 사용해 프레임별 컨테이너 할당을 제거합니다. 전체 frame ID, backpressure, 가장 가까운 이전 configuration 선택과 순서가 바뀐 반환을 보존하며, PE 리소스는 dispatch lock 밖에서 해제합니다. Texture ancestry는 한 dispatch 안에서만 재사용하고 역할별 mapping과 alias 거부는 유지합니다.

GPU 회귀 검사는 분리·결합 전처리의 33개 비트 단위 비교, 생성 padding readback, 겹치는 lease, configuration 변경과 OFF/DESTROY 후 반환을 포함합니다. 레지스트리 검사는 64/65개 경계, 드문 frame ID, 기록 실패와 리소스 alias를 다룹니다. 생성 출력 baseline 복사와 direct-output 정책은 실패 결과와 픽셀을 보존하기 위해 유지합니다. FG Configure는 state mutex 안에서 요청을 검증한 뒤 중복 ON이면 즉시 반환하며, 실제 상태 전환과 모든 OFF 요청은 실행 동기화와 정리를 유지합니다. 회귀 검사에는 실행 잠금을 잡은 상태의 중복 ON과 이전 프레임을 보유한 저해상도→고해상도→저해상도 SR 캐시 재활성화를 포함합니다.

Native FG 회귀 검사는 실제 Metal4 encoder의 첫 prepass pipeline binding과 종료 fence 갱신에 예외를 주입합니다. 부분 기록된 command buffer를 제출한 뒤 baseline 픽셀, history 불변, lease 유지, scratch 반환과 동일 prepared frame의 정상 재시도를 확인합니다.

FG Prepare와 Generate는 입력 snapshot과 생산 상태를 공유하고 각 recorded execution이 configuration을 소유하여 snapshot 복제와 별도 생산 상태 할당을 없앱니다. R32Float는 결합 depth/motion pipeline만 요구하고 다른 depth 형식은 분리 pipeline을 요구합니다. 필수 shader가 없으면 configuration을 거부하며 이후 재시도할 수 있습니다. 이름 있는 compute texture binding은 기존 Metal4 슬롯 초기화와 Legacy binding을 유지합니다.

Metal4 FG는 측정한 렌더 1920x1080 → 출력 2560x1440 조합에서만 16x8 threadgroup을 사용하고, 다른 조합과 Legacy는 8x8을 유지합니다. Context별 별도 idle slot에 depth snapshot, motion snapshot, scene texture를 각각 최대 하나 보관하며 device·format·크기·usage가 정확히 일치해야 재사용합니다. 공유 소유자가 기록된 producer·consumer와 execution lease 동안 snapshot을 유지하고, scene history는 현재 writer와 이전 history를 읽는 모든 reader가 소유합니다. 마지막 소유자가 해제된 뒤에만 반환하며 OFF/DESTROY 이전 세대의 늦은 반환은 epoch로 차단합니다.

SR RCAS는 finish pass가 이미 읽은 중심 색상을 다시 사용하여 중복 읽기를 제거합니다. 이웃 픽셀의 가장자리 clamp, exposure 곱셈, sharpening 계산, transfer 변환과 출력 alpha는 유지합니다. Native 검사는 원점이 다른 활성 crop과 중앙 배치 padding을 float32 출력으로 비교하며 exposure, 모든 transfer 함수와 sharpness 양 끝값을 포함합니다.

SR은 residency를 비운 뒤 하나의 pool 잠금에서 완료된 묶음의 scratch를 반환하며 기존 메모리 상한과 퇴출 순서를 유지합니다. Dispatch 안에서 resource의 device pointer가 검증된 command device와 같으면 adapter 검증을 재사용하고, 다른 pointer이면 기존 LUID 검사를 수행합니다. 회귀 검사는 지연된 Prepare 실행, shader 실패와 depth 형식 전환, pool 상한과 dormant 전환, device 소유권과 거부 경계를 포함합니다.

`python3 scripts/test-fsr-lifecycle.py`는 동시 초기화, 실패 후 재시도, 초기화 완료 후 빠른 경로, 파괴 대기자 알림, 임시·장기 COM 소유권과 HUD-less 교체·비활성화를 검사합니다. Native SR 회귀 검사는 유지 중인 lease의 격리, 반환된 table의 재사용, 노출 texture 재바인딩, dormant 정리와 연산 변경 후 새 feature와의 출력 비교를 확인합니다. FG 회귀 검사는 동일 texture의 sRGB 출력, context 파괴 이후 공유 pipeline의 수명, UI·distortion을 사용하는 반복 실행의 table 격리를 확인합니다. `node --test scripts/test-native-contract.mjs`는 backend 경계를 제어하면서 production recorded-owner 정의의 retain 실패, 부분·빈 encode 실패와 allocator 반환 시 소멸을 검사합니다. 이 검사만으로 게임 성능 향상이나 게임 전체 화면의 동일성이 입증되는 것은 아닙니다.

FG는 Prepare/Dispatch 호출마다 configuration 조회·생성 전에 Metal4 지원 여부를 한 번 검사하고, configuration은 같은 검증된 command를 사용하여 중복 조회를 생략합니다. 이 검사는 configuration 캐시 밖에 있어 캐시 적중 시에도 유지됩니다. Producer→native 회귀 검사는 캐시 생성 전후에 compiler가 없으면 대기 프레임을 소비하거나 history를 바꾸지 않고 거부하는지, 이후 정상 재시도가 성공하는지 확인합니다.

FG의 callback 대기 방지 조건은 descriptor 종류를 먼저 확인하여 일반 Prepare/Generate의 불필요한 TLS 조회를 생략합니다. Callback 내부의 WAIT_FOR_PRESENTS 거부와 별도의 context별 callback 검사는 유지합니다. Dispatch 회귀 검사는 callback 밖의 대기 허용, 내부의 대기 거부, 일반 callback Prepare/Generate 처리를 확인합니다.

`python3 scripts/test-fg-normalization.py`는 FG dispatch와 소유권뿐 아니라 실제 Metal4 motion texture를 읽어 jitter 보정을 검증합니다. 번역기는 프레임별 정규화된 jitter history를 보관하여 motion 크기 생략, 렌더 해상도 변경, reset 경계와 미리 준비한 snapshot에서도 올바른 MV 좌표계를 사용합니다.

생성 시에는 해당 프레임의 Prepare snapshot과 실제 생성 이력의 연속성을 사용하며, 더 최신인 Prepare가 대기 중이라는 이유만으로 이전의 유효한 프레임을 reset하지 않습니다. Producer→native 회귀 검사는 direct·callback 경로의 패킷을 native 프레임 레지스트리와 실제 Metal4 인코딩·GPU readback에 연결하여 명시적 reset, 미래 프레임과의 격리, 누락·소비된 snapshot의 거부를 확인합니다. COM/D3DMetal transport와 context 초기화는 대체하므로 실제 Wine·게임 실행 검사는 아닙니다.

합성하는 FG 카메라 투영행렬은 일반·역방향 원본 깊이 모두에서 MetalFX의 역방향 작업 깊이 좌표계를 사용하며, 유한·무한 far plane을 처리합니다. 원본 깊이 플래그와 직접 전달된 투영행렬은 보존합니다. 카메라 회귀 검증은 두 공개 카메라 packet 생산자를 통해 native 56프레임을 실행하고, 전달된 행렬·GPU 정규화 입력·최종 픽셀을 검사합니다. `FG_CAMERA_NATIVE_MASK=1`은 검토한 MetalFX 바이너리 UUID에 한해 실제 mover mask도 읽습니다. 비교 장면의 텍스처 물체는 화면 안에 유지합니다. 별도의 전체 화면 평면 실험에서 확인한 원본 MetalFX의 화면 바깥 경계 normal/reversed-depth 차이는 이번 카메라 수정에 포함하지 않습니다.

`python3 scripts/test-fsr-native.py`는 production SR의 중간·최종 GPU 텍스처를 읽습니다. 비선형 색상 변환은 기존 고정밀 입력의 정밀도를 낮추지 않는 부동소수점 작업 텍스처를 사용하며, 하드웨어 sRGB 디코딩은 텍스처 포맷이 담당합니다. 요청 출력이 temporal scaler의 한도를 넘으면 표시 해상도 motion의 활성 영역 전체를 재표본화하고, 축별 scale을 실제 temporal 출력 크기로 변환합니다. 렌더 해상도 motion, 한도 이내 motion, 카메라 jitter와 중앙 출력 배치는 기존 규약을 유지합니다. 검증에는 어두운 sRGB 그라데이션, 하드웨어 sRGB, BGRA, PQ, 비균일 motion, 부분 영역, history와 배율 제한 전환이 포함됩니다.

Depth와 렌더 해상도 motion은 `outputUV + currentJitter/renderSize`에서 읽어 jitter가 있는 샘플 격자를 안정화된 색상과 정렬합니다. 표시 해상도 motion은 이미 안정화된 격자를 유지합니다. 이 위치 보정은 motion 벡터 값에 jitter가 포함됐는지를 나타내는 플래그와 별개입니다. 안정화된 입력을 받는 native interpolator의 sampling jitter는 0으로 전달하고, 원본 렌더 jitter는 전처리와 프레임별 history에 유지합니다. 비균일 GPU readback으로 양쪽 부호와 경계 clamp 및 native sampling offset의 좌표계 일치를 검사하고, 실제 MetalFX 최종 출력을 읽어 별도 UI 합성, HUDless/present 기반 UI 추론 합성, UI 없는 색상도 검사합니다. 별도 premultiplied UI는 색상 인코딩 뒤에 한 번만 합성합니다. 이 통제된 입력의 검증이 게임 전체 화질을 입증하는 것은 아닙니다.

History 연속성에는 동일한 MetalFX interpolator 인스턴스도 필요합니다. 캐시에서 제거된 인스턴스를 새로 만들면 history와 GPU가 쓰는 interpolation-disable hint를 리셋합니다. GPU 회귀 검증은 이 캐시 경계, 서로 다른 원점의 투명·반투명 alpha 마스크 복원, 주변 픽셀을 보존하는 생성 출력 배치도 포함합니다. 같은 리소스를 가리키는 입력은 전환·복원 barrier를 한 쌍만 기록하며, 초기 상태나 읽기·쓰기 역할이 모순되면 barrier 기록 전에 거부합니다.

같은 테스트에서 SDK enum 기반 FFX→D3D12 상태 변환, native provider로의 원본 전달, production recorder의 전환·복원 barrier도 검증합니다. Barrier 캡처는 transport가 거부되는 경로를 사용하며, 성공적인 Wine/GPU 제출을 검증한 것은 아닙니다.

## 라이선스

[COPYING.LIB](COPYING.LIB), [NOTICES.md](NOTICES.md)를 참고하세요. Apple GPTK에는 별도 라이선스가 적용됩니다.
