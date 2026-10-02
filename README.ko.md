# Wine 11.17 D3DMetal (GPTK 4.0b2, experimental)

[English](README.md)

GPTK 4.0b2 D3DMetal과 MetalFX를 사용하는 Yaagl용 Wine 11.17 런타임 소스입니다.
Apple Silicon·macOS 26 이상·Rosetta 2를 대상으로 하며 macOS 26 실기기는 미검증입니다. 독립 설치기와 Apple framework는 포함하지 않습니다.

현재 릴리스: [experimental 6](https://github.com/dbc-hbin/wine-yaagl-d3dmetal/releases/tag/wine-11.17-gptk4.0b2-6).

## D3DMetal 오토패치

네이티브 실행 파일과 동봉 sidecar를 사용하므로 설치 시 Node.js·Python·Xcode 도구가 필요하지 않습니다.
Yaagl은 UI에서 Apple 라이선스 동의를 받은 뒤에만 패처에 `--accept-apple-license`를 전달해야 합니다.
패처는 PATH·심볼릭 링크 실행에서도 실제 실행 파일 옆의 sidecar를 찾으며, 다운로드·패치·서명을 검증한 뒤 framework를 출력합니다.

## 오토패치 번들 빌드

제작 환경에는 Node.js와 Apple 도구 체인이 필요합니다.

```sh
node scripts/build-d3dmetal-autopatch.mjs build/d3dmetal-autopatch
```

산출물: `build/d3dmetal-autopatch/`, `build/d3dmetal-autopatch.tar.gz`.

## Wine 런타임 패키징

```sh
scripts/build-yaagl-overlay.sh BASE_WINE_ROOT build/yaagl-overlay
python3 scripts/package-yaagl-runtime.py BASE_WINE_ROOT build/yaagl-overlay build/d3dmetal-autopatch build/yaagl-release
```

아카이브에는 Apple `D3DMetal.framework`가 없으므로 Wine 실행 전에 `wine/lib/external/`에 준비해야 합니다.
Metal HUD의 MetalFX “Frame Interpolator” 항목은 FG가 멈추고 약 0.5초 뒤 사라집니다.

`BASE_WINE_ROOT`는 고정된 `f163d14` 기준 런타임과 전체 파일 매니페스트가 일치해야 합니다.
`scripts/wine-artifacts.json`은 오버레이 빌드가 다시 빌드하고 패키저가 교체하는 모듈 목록입니다.

## 런타임 내부 처리

SR·FG 자원 매핑은 네이티브 자원 소유권을 한 번만 획득하고, 출력의 UAV 권한을 Metal 텍스처 추출 전에 검사합니다. 실패 시 참조를 남기지 않으며, 성공 시 보유한 텍스처는 호출자가 해제합니다. Descriptor 레이아웃의 정확한 바이트 검증을 유지합니다.
FG Prepare·Generate 입력은 한 번 정규화·검증한 스택 패킷으로 전달합니다. 공급자 선택 조건은 컨텍스트 잠금 안에서 다시 확인하며, 네이티브 확장·fallback, V1·V2 reset 의미, 콜백이 만든 입력의 처리를 유지합니다.
MetalFX·자동 제공자의 유효한 FFX 메모리 조회(V1/V2)는 D3DMetal의 DLSS 호환 정책처럼 두 바이트 값을 0으로 채우고 성공을 반환합니다. 0은 실측 사용량이 아니라 통계 미제공을 뜻하며, native FG·swapchain 조회는 기존 native 결과를 유지합니다. FFX SR/FG export는 SDK의 cdecl ABI를 씁니다.
네이티브 PSO·함수 캐시는 동시 생성을 한 번으로 합치고, 살아 있는 결과만 재사용합니다. 완료된 항목은 약한 참조만 가지므로 D3DMetal이 파이프라인·reflection·추출 함수를 해제하면 함께 해제되며, 이후 요청은 D3DMetal과 Metal의 디스크 캐시가 처리합니다.
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
