# Wine 11.17 D3DMetal (GPTK 4.0b2, experimental)

[English](README.md)

GPTK 4.0b2 D3DMetal과 MetalFX를 사용하는 Yaagl용 Wine 11.17 런타임 소스입니다.
Apple Silicon·macOS 26 이상·Rosetta 2를 대상으로 하며 macOS 26 실기기는 미검증입니다. 독립 설치기와 Apple framework는 포함하지 않습니다.

현재 릴리스: [experimental 9](https://github.com/dbc-hbin/wine-yaagl-d3dmetal/releases/tag/wine-11.17-gptk4.0b2-9).

## D3DMetal 오토패치

네이티브 실행 파일과 동봉 sidecar를 사용하므로 설치 시 Node.js·Python·Xcode 도구가 필요하지 않습니다.
Yaagl은 UI에서 Apple 라이선스 동의를 받은 뒤에만 패처에 `--accept-apple-license`를 전달해야 합니다.
패처는 PATH·심볼릭 링크 실행에서도 실제 실행 파일 옆의 sidecar를 찾으며, 다운로드·패치·서명을 검증한 뒤 framework를 출력합니다.
원본 자산과 패치 결과의 고정 SHA-256 검증은 유지합니다. 모든 패치를 최종 서명 전에 적용하며, 현지에서 재서명한 파일은 고정 전체 해시 대신 `codesign`으로 검증하고 서명과 무관한 D3DMetal payload 해시 검증도 유지합니다. 준비 manifest에는 최종 파일의 실제 해시를 기록합니다.

## 오토패치 번들 빌드

제작 환경에는 Node.js와 Apple 도구 체인이 필요합니다.

```sh
node scripts/build-d3dmetal-autopatch.mjs build/d3dmetal-autopatch
```

산출물: `build/d3dmetal-autopatch/`, `build/d3dmetal-autopatch.tar.gz`.

## Wine 런타임 패키징

```sh
export GSTREAMER_ROOT=/path/to/development/GStreamer.framework/Versions/1.0
export WINE_DEPS_ROOT=/path/to/development/opt/local
scripts/build-yaagl-overlay.sh BASE_WINE_ROOT build/yaagl-overlay
python3 scripts/package-yaagl-runtime.py BASE_WINE_ROOT build/yaagl-overlay build/d3dmetal-autopatch build/yaagl-release
```

아카이브에는 Apple `D3DMetal.framework`가 없으므로 Wine 실행 전에 `wine/lib/external/`에 준비해야 합니다.
Metal HUD의 MetalFX “Frame Interpolator” 항목은 FG가 멈추고 약 0.5초 뒤 사라집니다.

`BASE_WINE_ROOT`는 고정된 `f163d14` 기준 런타임과 전체 파일 매니페스트가 일치해야 합니다.
오버레이는 `GSTREAMER_ROOT`의 GStreamer·FFmpeg 개발 헤더와 pkg-config 메타데이터를 사용합니다. 기준 런타임의 framework에 개발 파일이 있을 때만 이를 기본값으로 사용할 수 있고, 개발 파일을 제외한 배포 기준본에는 별도 개발 framework가 필요합니다. 선택적 `WINE_DEPS_ROOT`에는 GnuTLS·SDL 등 필수 의존성의 외부 개발 prefix(`include/`, `lib/`, `lib/pkgconfig/`)를 지정합니다. pkg-config는 각 패키지를 해당 SDK의 prefix로 재배치하며, 이 입력은 x86_64에만 적용하고 ARM64 서버 빌드에는 전달하지 않습니다. 고정된 기준본에 파일을 추가하거나 필수 configure 기능을 끄지 마세요.
`scripts/wine-artifacts.json`은 오버레이 빌드가 다시 빌드하고 패키저가 교체하는 모듈 목록입니다.

## 런타임 내부 처리

FSR 전용 JSON 진단(`YAAGL_FSR_LOG`)과 진단용 카운터·메타데이터를 제거했습니다. 내부 MetalFX 계약은 읽지 않는 입력 여부 표시 없이 값을 직접 저장하며, 기본값·입력 검증·SDK 디버그 콜백·오류 반환·렌더링 동작은 유지합니다. 나머지 자체 Wine 튜닝도 유지합니다.

전체화면 대상 전달은 직접 COM 호출, Wine unixcall, 레거시 factory 생성 경로를 모두 처리합니다. 레거시의 암시적 대상 호출은 남아 있는 레지스터 값을 출력 인터페이스로 해석하지 않도록 명시적으로 null을 전달하며, 명시적 대상과 HRESULT 전달은 유지합니다.
Metal4 화면 표시의 layer residency 등록은 제출·표시 예약 후 해제하는 훅을 유지합니다. 이미 제출한 GPU 작업의 residency는 유지됩니다. 격리된 실제 GPU layer 교체 비교에서 원래 방식은 빈 set을 큐 소멸까지 보유했고, 범위를 제한한 등록은 남기지 않았습니다. Drawable 크기 변경은 같은 set을 재사용했습니다. 이는 오래된 등록 정리의 근거이며, GPU 자원 누수나 FPS 개선을 입증한 것은 아닙니다.

SR·FG 자원 매핑은 네이티브 자원 소유권을 한 번만 획득하고, 출력의 UAV 권한을 Metal 텍스처 추출 전에 검사합니다. 실패 시 참조를 남기지 않으며, 성공 시 보유한 텍스처는 호출자가 해제합니다. Descriptor 레이아웃의 정확한 바이트 검증을 유지합니다.
Legacy SR·FG replay는 명령 태그가 다르면 payload를 복사하기 전에 원래 경로로 넘깁니다. MetalFX 직전의 blit encoder 종료는 바이트가 검증된 원래 `GetExternalCommandBuffer` 호출에 맡겨 중복 flush만 없앴습니다. 앞선 전체 encoder flush, fence 갱신·대기, 동기화와 GPU 완료까지의 자원 수명은 유지합니다.
FG Prepare·Generate 입력은 한 번 정규화·검증한 스택 패킷으로 전달합니다. 공급자 선택 조건은 컨텍스트 잠금 안에서 다시 확인하며, 네이티브 확장·fallback, V1·V2 reset 의미, 콜백이 만든 입력의 처리를 유지합니다.
MetalFX·자동 제공자의 유효한 FFX 메모리 조회(V1/V2)는 D3DMetal의 DLSS 호환 정책처럼 두 바이트 값을 0으로 채우고 성공을 반환합니다. 0은 실측 사용량이 아니라 통계 미제공을 뜻하며, native FG·swapchain 조회는 기존 native 결과를 유지합니다. FFX SR/FG export는 SDK의 cdecl ABI를 씁니다.
사이드카는 서로 다른 네이티브 pipeline 소유자의 동등한 PSO 생성을 합치고, 살아 있는 결과만 재사용합니다. 완료된 항목은 PSO와 키 자원을 약한 참조로 보관하며, reflection은 PSO에 연결되어 해당 PSO가 해제될 때까지 유지됩니다. 함수 캐시에는 `ExtractFunctions`·`LoadGraphicsFunctions` 훅만 복원합니다. 검증된 호출 위치와 실제 특수화 상수를 키에 반영해 같은 device·살아 있는 library 안에서만 재사용합니다. 같은 키의 동시 요청은 한 번만 생성하고, 완료된 함수는 약한 참조로 보관하며, device 소멸 시 캐시를 정리합니다. 캐시 miss와 적용 대상이 아닌 요청은 원래 추출 경로를 사용합니다. 수명·동시성·키 구분과 실제 Metal GPU 검증은 `python3 scripts/test-function-cache.py`로 실행합니다.
바깥쪽 Compute·Graphics stage 컴파일과 stage 키 생성은 훅하지 않습니다. D3DMetal 원래 stage 캐시와 생성 중 결과를 기다리는 동기화를 그대로 사용합니다. 중복 삽입 경쟁에서 진 88바이트 Compute stage table만 해제하도록 원본 경로를 직접 수정하며, 정식 table과 빌려 쓰는 stage 결과는 유지합니다. 해제 경로 회귀 검증은 `node --test scripts/d3dmetal-compute-loser.test.mjs`로 실행합니다. 별도의 stage-ID 조회 패치는 대기 전에 바깥 잠금을 풀도록 유지합니다. Library 동시 생성 방지와 서로 다른 pipeline 간 살아 있는 PSO 재사용도 유지하며, dispatch 레이아웃 21은 일반 항목 18개와 특수 항목 5개로 구성됩니다. 원래 컴파일러·디스크 캐시와 exp 7 정확성 수정을 유지합니다. 게임 FPS 향상을 입증한 변경은 아닙니다.
캐시 기록은 payload 체크섬과 음수 길이 footer를 쓴 뒤 완료 위치를 공개합니다. 정확한 바이트 패치는 독립된 `D3DMCacheFile::End`와 확인된 인라인 복사본 16곳(셰이더 stage 저장 12종, Compute·Graphics pipeline-stage 기록, bytecode, root signature)에 적용됩니다. 기록 형식은 바꾸지 않으며, 전원 차단 시 보존이나 디스크 flush가 아닌 공개 순서 수정입니다. 명령어 단위의 읽기 일관성과 체크섬 처리 중단 회귀 검증은 `node --test scripts/d3dmetal-cache-publication.test.mjs`로 실행합니다.
`QueryVideoMemoryInfo`는 고정 예산을 보고합니다. 로컬 세그먼트 그룹의 예산은 `recommendedMaxWorkingSetSize`와 같은 RAM에서 Windows가 그래픽에 주는 양(`min(RAM × 80%, max(RAM − 16 GB, RAM × 50%))`, 예: 16 GB에서 8 GB, 24 GB에서 12 GB) 중 작은 값이고, 사용량은 64비트 `currentAllocatedSize` 전체입니다. 비로컬 그룹은 D3D12가 UMA 어댑터에 정한 대로 모두 0입니다. 원래 D3DMetal은 권장치의 2배를 예산으로 보고하고 사용량을 32비트에서 자릅니다. D3DMetal의 `Evict`·`MakeResident`는 아무것도 하지 않으므로, 예산을 시스템 메모리에 따라 줄이면 메모리는 그대로인 채 게임이 텍스처 화질만 낮춥니다. 그래서 예산을 고정합니다. 예약은 구현하지 않아 0으로 보고합니다.

기존과 같은 C++ 함수 입구 훅으로 원래 어댑터 생성자를 실행한 뒤 `DedicatedVideoMemory`는 위 예산, `DedicatedSystemMemory`는 0, `SharedSystemMemory`는 추가 상한 없이 물리 RAM에서 전용 VRAM을 뺀 값으로 설정합니다. 예를 들어 RAM 32 GiB·전용 16 GiB이면 공유 16 GiB, RAM 64 GiB·전용 48 GiB이면 공유 16 GiB입니다. 물리 RAM 조회 실패 시 공유 용량은 0이며, 뺄셈 언더플로를 방지합니다. 저장된 정보를 한 번 수정하므로, 복사를 인라인한 COM·Wine unixcall 경로를 포함해 `GetDesc` 네 버전에 모두 적용됩니다. 세 메모리 필드는 `gpuinfo` 값보다 우선하며, GPU 식별 정보·LUID·나머지 필드는 유지합니다. 이는 UMA 호환용 보고 정책이며, Windows 드라이버의 정확한 재현이나 메모리 추가 할당·실제 사용량 감소를 뜻하지 않습니다.

경계 검증: `python3 scripts/test-msync-message-dispatch.py`, `python3 scripts/test-resource-map.py`, `python3 scripts/test-fg-normalization.py`, `python3 scripts/test-fsr-memory-query.py`, `python3 scripts/test-wine-artifact-catalog.py`.

## 커서 처리

커서·RawInput 서버 프로토콜은 966입니다.
프로그램의 커서 변경과 네이티브 소유 창 조회는 활성 창 판정을 공유하고, AppKit이 지정한 창의 커서 갱신은 별도 창 검사를 유지합니다.
직접 `CGWarpMouseCursorPosition` 호출은 실제 이동 변위를 기록합니다. 이후 이동량이 0이 아닌 마우스 이동 이벤트에서 일치하는 기록을 한 번 순회하며 소비하고, 일반 입력·RawInput 처리 전에 해당 변위만 뺍니다. 이동량 0 알림은 기록을 유지하고 앱 활성 상태 전환은 기록을 비웁니다. EventTap 클리핑은 별도 보정 경로를 유지합니다.
입력 보존 회귀 검증은 `python3 scripts/test-cursor-warp-correction.py`로 실행합니다.

## 라이선스

[COPYING.LIB](COPYING.LIB), [NOTICES.md](NOTICES.md)를 참고하세요. Apple GPTK에는 별도 라이선스가 적용됩니다.
