# Wine 11.20 D3DMetal (GPTK 4.0b3)

[English](README.md)

Apple Silicon과 Rosetta 2를 위한 GPTK 4.0 beta 3 전용 Yaagl 런타임 소스입니다. Wine host, msync, 입력, 미디어, 오디오, 창 및 리소스 관리 패치를 유지합니다. 네이티브 sidecar는 디스플레이 라우팅, 비디오 메모리 정책과 FSR-to-MetalFX 번역을 담당합니다. 셰이더, 라이브러리, PSO와 RT 함수 재사용은 GPTK 자체 네이티브 캐시가 계속 담당합니다.

DLSS는 Apple 원본 `nvngx-on-metalfx.dll`과 Unix 모듈을 사용합니다. 실험용 DLSS 번역기, Streamline 가로채기, NPC 이름표 합성은 포함하지 않으며 이름표 수정 완료를 주장하지 않습니다.

## 현재 소스와 패치 정책

공식 `wine-11.20` 태그를 기반으로 검증된 소스 `1846c16674b`의 런타임 변경을 이식한 트리입니다. 빌드 입력은 `dlls/`, `server/`, `include/`의 Wine 소스입니다. 유지한 계약과 업스트림 중복 처리 기준은 [이식 기록](documentation/WINE-REBASE.md)에 정리합니다. 과거 P3/tuned 패치 묶음은 원본 커밋에 참고 자료로 보존하며, 이 트리에 다시 적용하거나 복사하지 않습니다.

beta3 훅 계약은 `d3dmetal-sidecar/layout.json`에 정의되어 있습니다. format 27, marker `YAAGLB3SIDEcar\x1b`, 빈 슬롯 없이 배치한 dispatch 슬롯 4개이며 추가 `binaryPatches`는 없습니다. 진입점 훅은 dispatch 순서대로 `GetContainingOutput`, `SetFullscreenState`, `QueryVideoMemoryInfo`, `ReplayTemporalScale`입니다. 디스플레이 라우팅, 비디오 메모리 조회와 FSR replay만 담당합니다. `ngx-hooks.mm`은 `ReplayTemporalScale`에서 기록된 FSR 명령을 처리하고, 나머지는 Apple 원본 replay로 전달합니다. FSR SR/FG는 Metal4/MPL transport를 사용합니다.

현재 Wine FSR 진입점과 native 회귀 검사의 호출처는 Metal4를 선택하며, 저장소 안에서 backend의 Legacy command mode를 선택하는 호출처는 없습니다. SR C++ 인터페이스는 여전히 일반 `MTLCommandBuffer`를 사용하는 Legacy를 받으며 sidecar가 C++ factory/encode 심볼도 export하므로, 저장소 조사만으로 외부 호출자가 없다고 확정할 수는 없습니다. Legacy는 명시적인 인터페이스 전환 전까지 유지합니다. AMD native provider로의 원본 전달과는 별개이며, `YAAGL_FSR_UPSCALER=native`나 FG native fallback은 MetalFX Legacy를 선택하지 않습니다.

PSO/function 캐시, RT 출처 정보 추적, 캐시 문맥·수명 관리 훅과 라이브러리 singleflight를 포함한 커스텀 캐시 관련 훅 13개를 모두 제거했습니다. 해당 네이티브 진입점 바이트는 원본 그대로이며 전달용 wrapper로 대체하지 않습니다. GPTK 자체 셰이더/stage·디스크 캐시, 라이브러리 게시·재사용, owner별 graphics/compute PSO와 RT 함수 재사용은 유지합니다. 제거한 커스텀 캐시와 같은 owner 간 재사용이나 측정된 성능 동등성을 주장하지는 않습니다.

DXGI 비디오 메모리 조회, 출력 모니터 조회, 전체 화면 진입점과 출력 열거는 원본 메서드를 호출할 때도 Windows x64 호출 규약을 사용합니다. 네이티브 내부 헬퍼는 호스트 ABI를 유지합니다. 이 규약이 섞이면 렌더링 전 Unity 기동 단계에서 크래시가 발생합니다.

converter는 원본을 유지합니다. 준비 도구는 `native-sidecar-patch`, `native-sidecar-module`, `final-framework-reseal` 순서로 적용합니다. 4개 훅을 가진 sidecar 소스는 `d3dmetal-sidecar/`에 있으며, `scripts/build-d3dmetal-sidecar.mjs`가 `libD3DMetalSidecar.dylib`를 빌드하고 `scripts/d3dmetal-sidecar-patch.mjs`가 패치를 담당합니다. 패키징 옵션은 `--sidecar-module`과 `--sidecar-manifest`, manifest 필드는 `nativeSidecarBuild`와 `nativeSidecar`, recipe 필드는 `patches.nativeSidecar`입니다. 캐시 시절 인터페이스 이름은 호환 별칭 없이 교체하며 내부 C++ namespace `yaagl::pso`와 공개 FSR C API는 유지합니다. 제외한 패치의 이유는 다음과 같이 구분합니다.

| 제외한 패치 | 현재 정책의 근거 |
|---|---|
| FP64 codec recipe | 알려진 실패 codec fixture가 beta3 원본 converter에서 컴파일되며 cast 구현도 기존 recipe와 다릅니다. 모든 FP64/constrained-cast 입력의 정상 동작을 증명한 것은 아닙니다. |
| Stage-lock·compute loser 해제 | beta3 `CompileCompute`가 이미 stage-cache lock을 잡고 중복 할당을 해제합니다. |
| CacheFile close/munmap | beta3가 파일을 닫고 payload와 header를 unmap한 뒤 상태를 초기화합니다. |
| CacheFile publication-order | reader가 불완전한 record를 거부하고 cache를 닫거나 다시 엽니다. writer 순서를 기본 구성에서 바꿔야 할 correctness·캐시 재사용 문제는 재현되지 않았습니다. |
| Legacy fullscreen null/forward call-site | beta3가 native output을 직접 전달합니다. Yaagl 모니터 라우팅은 별도의 sidecar 기능으로 유지합니다. |
| Scoped present-residency cleanup | 제출 오류 수정이 아니라 제출 후 association 수명을 줄이는 최적화입니다. residency 증가나 driver memory failure가 측정되지 않아 제외합니다. |
| Persistent-cache read-ahead | 파일 warmup은 셰이더 컴파일이 아닙니다. 셰이더·PSO 프리컴파일과 디스크 캐시는 게임과 D3DMetal이 담당합니다. |
| 커스텀 DLSS·Streamline 처리 | 이 런타임은 원본 NGX 쌍을 사용하며 실험용 이름표 처리는 범위 밖입니다. |

커서 처리는 현재 `cocoa_app.m`과 `cocoa_warpconsumption.h` 구현을 사용합니다. 이 정책들을 별도 런타임 브랜치의 모든 수정과 동일하다고 설명하지 않습니다.

## Wine과 sidecar 빌드

Xcode, Node.js, Python, LLVM MinGW, Bison과 x86_64 TLS/font/media 의존성이 필요합니다. `WINE_DEPS_ENV`에 의존성 환경 파일을, `WINE_MINGW_ROOT`에 설치된 LLVM MinGW 경로를 지정합니다. 다른 소스를 내려받는 대신 미커밋 변경을 포함한 현재 워크트리를 빌드합니다.

```sh
WINE_NATIVE_SERVER=yes scripts/build-wine-p3.sh all
node scripts/build-d3dmetal-sidecar.mjs build/native-sidecar
```

`WINE_P3_ROOT`로 새 빌드 디렉터리를 지정할 수 있으며 기본값은 `build/wine-p3-current`입니다. `host/`와 provenance에는 실제 Wine 소스가 기록됩니다. sidecar 산출물에는 dylib과 소스 해시 manifest가 포함됩니다. 기존 빌드 디렉터리가 있다는 사실만으로 현재 소스의 빌드가 검증되지는 않습니다.

### ZZZ 게임 모드 호스트

전체 소스 빌드는 해당 빌드의 기존 `loader/main.o`로 `libexec/yaagl-game-mode/ZZZ.app/Contents/MacOS/wine`도 링크합니다. `scripts/game-mode-host.py`는 생성된 loader 링크 규칙에서 출력 경로와 내장 plist만 바꿉니다. loader 구현을 복제하거나 구버전 Wine의 주소 공간 예약을 가정하지 않으며, Wine 11.20의 low-8GB 예약과 실제 빌드 플래그를 그대로 사용합니다. 로더가 고정 번들 위치에서 같은 런타임의 `lib/wine/x86_64-unix/ntdll.so`를 찾습니다. 환경 변수로 라이브러리를 선택하거나 서명된 앱 내부에서 외부 심볼릭 링크를 사용하지 않습니다.

내장 plist와 번들 plist는 동일하며 `io.github.yaagl.zzz-game-host`, Games 분류, `LSSupportsGameMode=true`, `WineApplication`을 사용하고 `LSUIElement`는 없습니다. 설치와 staging에서 번들을 서명하고, staging은 실행 파일·양쪽 plist·상대 ntdll 경로·서명을 검사한 뒤 최종 바이트를 inventory에 기록합니다. 로컬 및 framework 제외 패키지 manifest 모두 `gameMode: {"version":1,"host":"libexec/yaagl-game-mode/ZZZ.app/Contents/MacOS/wine","target":"zzz"}`를 제공하며 기존 해시와 심볼릭 링크 inventory가 무결성 기준입니다.

런처의 선택적 기능은 Apple Silicon/macOS 26 이상에서 ZZZ만 대상으로 합니다. `YAAGL_GAME_MODE_TARGET`은 선택한 게임 실행 파일의 절대 Unix 경로이며, 실제 열린 Windows 이미지가 일치할 때만 호스트로 라우팅합니다. `YAAGL_GAME_MODE_ACTIVE=1`은 해당 프로세스 전용 내부 표시이지 런처 전체 스위치가 아닙니다. 기존 renderer wrapper를 먼저 실행합니다. 끄면 일반 Wine 경로를 사용하고 macOS 정책을 강제로 끄지 않으며, 명시적인 창 모드 설정을 유지합니다. 호스트 진입은 macOS의 게임 모드 활성화 증명이 아니고 최종 판단은 macOS가 합니다. 전역 `gamepolicyctl`, ForgePlay 코드, Apple framework 추가 배포를 사용하지 않습니다.

네이티브 전체 화면 전환 중에는 Wine의 최신 위치 요청을 보존합니다. 대상 모니터가 바뀌면 전체 화면을 해제하고 기존 종료 콜백에서 요청한 위치를 적용한 뒤 재진입합니다. 사용자가 직접 전체 화면을 종료하면 자동 재진입은 계속 억제됩니다. 기존 디스플레이 변경 보정 훅은 위치 적용이 지연되는 동안에도 오래된 프레임 이벤트를 폐기합니다. `python3 scripts/test-game-mode-fullscreen.py`는 실제 Objective-C 메서드를 합성 화면에서 실행해 전환 순서와 실패 경로를 검사하며 데스크톱 Space를 변경하지 않습니다. 물리적인 다중 모니터 검증을 대신하지는 않습니다.

전체 빌드와 staging 이후 `python3 scripts/game-mode-host.py validate build/staged/wine` 및 `codesign --verify --strict build/staged/wine/libexec/yaagl-game-mode/ZZZ.app`으로 호스트를 검사할 수 있습니다. 아래 런타임 검증에도 같은 메타데이터와 서명 검사가 포함됩니다. 실제 활성화는 Steam → ZZZ 전체 화면 시나리오에서 macOS 게임 모드 UI를 관찰해야 합니다.

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

FSR 1차 오버헤드 최적화는 셰이더 소스, GPU 패스, 리소스 바인딩, fence와 history/reset 결정을 보존합니다. SR은 feature 안에서 내장 셰이더 library를 공유하고, FG는 configuration의 기존 pipeline 묶음마다 library를 한 번 생성합니다. Unix API 주소는 성공한 경우 acquire/release 방식으로 게시하여 이후 호출의 로더 mutex를 생략하며, 조회 실패는 다시 시도할 수 있습니다. FG는 closing context의 마지막 참조가 반환될 때만 파괴 대기자를 깨우고, 실제 실행 리소스·패스 수로 객체 보관 공간을 GPU 인코딩 전에 예약합니다. `python3 scripts/test-fsr-lifecycle.py`는 동시 초기화, 실패 후 재시도, 초기화 완료 후 빠른 경로와 파괴 대기자 알림을 검사합니다. 이 변경만으로 게임 성능 향상이 실측됐다는 뜻은 아닙니다.

FG는 Prepare/Dispatch 호출마다 configuration 조회·생성 전에 Metal4 지원 여부를 한 번 검사하고, configuration은 같은 검증된 command를 사용하여 중복 조회를 생략합니다. 이 검사는 configuration 캐시 밖에 있어 캐시 적중 시에도 유지됩니다. Producer→native 회귀 검사는 캐시 생성 전후에 compiler가 없으면 대기 프레임을 소비하거나 history를 바꾸지 않고 거부하는지, 이후 정상 재시도가 성공하는지 확인합니다.

FG의 callback 대기 방지 조건은 descriptor 종류를 먼저 확인하여 일반 Prepare/Generate의 불필요한 TLS 조회를 생략합니다. Callback 내부의 WAIT_FOR_PRESENTS 거부와 별도의 context별 callback 검사는 유지합니다. Dispatch 회귀 검사는 callback 밖의 대기 허용, 내부의 대기 거부, 일반 callback Prepare/Generate 처리를 확인합니다.

`python3 scripts/test-fg-normalization.py`는 FG dispatch와 소유권뿐 아니라 실제 Metal4 motion texture를 읽어 jitter 보정을 검증합니다. 번역기는 프레임별 정규화된 jitter history를 보관하여 motion 크기 생략, 렌더 해상도 변경, reset 경계와 미리 준비한 snapshot에서도 올바른 MV 좌표계를 사용합니다.

생성 시에는 해당 프레임의 Prepare snapshot과 실제 생성 이력의 연속성을 사용하며, 더 최신인 Prepare가 대기 중이라는 이유만으로 이전의 유효한 프레임을 reset하지 않습니다. Producer→native 회귀 검사는 direct·callback 경로의 패킷을 native 프레임 레지스트리와 실제 Metal4 인코딩·GPU readback에 연결하여 명시적 reset, 미래 프레임과의 격리, 누락·소비된 snapshot의 거부를 확인합니다. COM/D3DMetal transport와 context 초기화는 대체하므로 실제 Wine·게임 실행 검사는 아닙니다.

합성하는 FG 카메라 투영행렬은 일반·역방향 원본 깊이 모두에서 MetalFX의 역방향 작업 깊이 좌표계를 사용하며, 유한·무한 far plane을 처리합니다. 원본 깊이 플래그와 직접 전달된 투영행렬은 보존합니다. 카메라 회귀 검증은 두 공개 카메라 packet 생산자를 통해 native 56프레임을 실행하고, 전달된 행렬·GPU 정규화 입력·최종 픽셀을 검사합니다. `FG_CAMERA_NATIVE_MASK=1`은 검토한 MetalFX 바이너리 UUID에 한해 실제 mover mask도 읽습니다. 비교 장면의 텍스처 물체는 화면 안에 유지합니다. 별도의 전체 화면 평면 실험에서 확인한 원본 MetalFX의 화면 바깥 경계 normal/reversed-depth 차이는 이번 카메라 수정에 포함하지 않습니다.

`python3 scripts/test-fsr-native.py`는 production SR의 중간·최종 GPU 텍스처를 읽습니다. 비선형 색상 변환은 기존 고정밀 입력의 정밀도를 낮추지 않는 부동소수점 작업 텍스처를 사용하며, 하드웨어 sRGB 디코딩은 텍스처 포맷이 담당합니다. 요청 출력이 temporal scaler의 한도를 넘으면 표시 해상도 motion의 활성 영역 전체를 재표본화하고, 축별 scale을 실제 temporal 출력 크기로 변환합니다. 렌더 해상도 motion, 한도 이내 motion, 카메라 jitter와 중앙 출력 배치는 기존 규약을 유지합니다. 검증에는 어두운 sRGB 그라데이션, 하드웨어 sRGB, BGRA, PQ, 비균일 motion, 부분 영역, history와 배율 제한 전환이 포함됩니다.

Depth와 렌더 해상도 motion은 `outputUV + currentJitter/renderSize`에서 읽어 jitter가 있는 샘플 격자를 안정화된 색상과 정렬합니다. 표시 해상도 motion은 이미 안정화된 격자를 유지합니다. 이 위치 보정은 motion 벡터 값에 jitter가 포함됐는지를 나타내는 플래그와 별개입니다. 비균일 GPU readback으로 양쪽 부호와 경계 clamp를 검사하고, 실제 MetalFX 최종 출력을 읽어 별도 UI 합성, HUDless/present 기반 UI 추론 합성, UI 없는 색상도 검사합니다. 별도 premultiplied UI는 색상 인코딩 뒤에 한 번만 합성합니다. 이 통제된 입력의 검증이 게임 전체 화질을 입증하는 것은 아닙니다.

History 연속성에는 동일한 MetalFX interpolator 인스턴스도 필요합니다. 캐시에서 제거된 인스턴스를 새로 만들면 history와 GPU가 쓰는 interpolation-disable hint를 리셋합니다. GPU 회귀 검증은 이 캐시 경계, 서로 다른 원점의 투명·반투명 alpha 마스크 복원, 주변 픽셀을 보존하는 생성 출력 배치도 포함합니다. 같은 리소스를 가리키는 입력은 전환·복원 barrier를 한 쌍만 기록하며, 초기 상태나 읽기·쓰기 역할이 모순되면 barrier 기록 전에 거부합니다.

같은 테스트에서 SDK enum 기반 FFX→D3D12 상태 변환, native provider로의 원본 전달, production recorder의 전환·복원 barrier도 검증합니다. Barrier 캡처는 transport가 거부되는 경로를 사용하며, 성공적인 Wine/GPU 제출을 검증한 것은 아닙니다.

## 라이선스

[COPYING.LIB](COPYING.LIB), [NOTICES.md](NOTICES.md)를 참고하세요. Apple GPTK에는 별도 라이선스가 적용됩니다.
