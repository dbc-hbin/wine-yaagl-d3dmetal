# Wine 11.17 D3DMetal (GPTK 4.0b2, experimental)

[English](README.md)

GPTK 4.0b2 D3DMetal과 MetalFX를 사용하는 Yaagl용 Wine 11.17 런타임 소스입니다.
Apple Silicon·macOS 26 이상·Rosetta 2를 대상으로 하며 macOS 26 실기기는 미검증입니다. 독립 설치기와 Apple framework는 포함하지 않습니다.

현재 릴리스: [experimental 4](https://github.com/dbc-hbin/wine-yaagl-d3dmetal/releases/tag/wine-11.17-gptk4.0b2-4).

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
`scripts/wine-artifacts.json`은 tuned/safe-msync 소스 빌드와 Yaagl 오버레이 빌드·패키징이 공유하는 산출물 목록입니다.
레거시 P3·split 패키저와 전용 스테이징 도구는 제거했습니다. `build-wine-tuned.sh`는 소스 빌드 도구로 유지하며, 제거된 패키저로의 인계는 더 이상 안내하지 않습니다.
`install`은 프로필 host와 provenance를 생성한 뒤 종료합니다. 릴리스 패키징은 위 명령으로 별도 수행합니다.

## 런타임 내부 처리

MSync 패치 스택은 후속 덮어쓰기 없이 `patches/wine-tuned/0001-msync-tuned.patch`로 통합했습니다. 소스 빌드 도구의 `patch` 적용 경로를 사용합니다.
SR·FG 자원 매핑은 네이티브 자원 소유권을 한 번만 획득하고, 출력의 UAV 권한을 Metal 텍스처 추출 전에 검사합니다. 실패 시 참조를 남기지 않으며, 성공 시 보유한 텍스처는 호출자가 해제합니다. Descriptor 레이아웃의 정확한 바이트 검증을 유지합니다.
FG Prepare·Generate 입력은 한 번 정규화·검증한 스택 패킷으로 전달합니다. 공급자 선택 조건은 컨텍스트 잠금 안에서 다시 확인하며, 네이티브 확장·fallback, V1·V2 reset 의미, 콜백이 만든 입력의 처리를 유지합니다.
MetalFX·자동 제공자의 유효한 FFX 메모리 조회(V1/V2)는 D3DMetal의 DLSS 호환 정책처럼 두 바이트 값을 0으로 채우고 성공을 반환합니다. 0은 실측 사용량이 아니라 통계 미제공을 뜻하며, native FG·swapchain 조회는 기존 native 결과를 유지합니다. FFX SR/FG export는 SDK의 cdecl ABI를 씁니다.

경계 검증: `python3 scripts/test-msync-message-dispatch.py`, `python3 scripts/test-resource-map.py`, `python3 scripts/test-fg-normalization.py`, `python3 scripts/test-fsr-memory-query.py`, `python3 scripts/test-wine-artifact-catalog.py`.

## 커서 처리

Tuned의 커서·RawInput 변경은 `patches/wine-tuned/0004-macdrv-reset-rawinput-baseline.patch`로 통합되어 있으며 커서 프로토콜은 966을 유지합니다.
프로그램의 커서 변경과 네이티브 소유 창 조회는 활성 창 판정을 공유하고, AppKit이 지정한 창의 커서 갱신은 별도 창 검사를 유지합니다.
직접 `CGWarpMouseCursorPosition` 호출은 실제 이동 변위를 기록합니다. 이후 이동량이 0이 아닌 마우스 이동 이벤트에서 일치하는 기록을 한 번 순회하며 소비하고, 일반 입력·RawInput 처리 전에 해당 변위만 뺍니다. 이동량 0 알림은 기록을 유지하고 앱 활성 상태 전환은 기록을 비웁니다. EventTap 클리핑은 별도 보정 경로를 유지합니다.
입력 보존 회귀 검증은 `python3 scripts/test-cursor-warp-correction.py`로 실행합니다.

## 라이선스

[COPYING.LIB](COPYING.LIB), [NOTICES.md](NOTICES.md)를 참고하세요. Apple GPTK에는 별도 라이선스가 적용됩니다.
