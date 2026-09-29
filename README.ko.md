# CX 26.3 D3DMetal 런타임 (GPTK 4.0b2)

[English](README.md)

Yaagl용 CrossOver 26.3 기반 Wine 11.0 클린 빌드 경로입니다.
Apple Silicon·macOS 26 이상·Rosetta 2를 대상으로 하며, macOS 26 실기기 검증은 별도로 필요합니다.
Wine 로더와 Unix 모듈은 x86_64, Windows 모듈은 i386/x86_64, wineserver는 ARM64로 빌드합니다.

## 포함 범위

- Yaagl의 MF 영상 재생·Timeout 호환 처리. MF 작업 큐 해제와 Timeout 안전 검사·명시적 OFF를 유지합니다.
- ARM64 wineserver. CX의 MSync와 서버 프로토콜은 그대로 유지합니다.
- FSR API를 MetalFX SR·FG로 연결하는 모듈과 native FG fallback.
  MetalFX·자동 제공자의 유효한 FFX 메모리 조회(V1/V2)는 D3DMetal의 DLSS 호환 정책처럼 두 바이트 값을 0으로 채우고 성공을 반환합니다. 0은 실측 사용량이 아니라 통계 미제공을 뜻하며, native FG·swapchain 조회는 기존 native 결과를 유지합니다. 자원 추적이나 조회를 위한 GPU 할당은 추가하지 않습니다.
- GPTK 4.0b2의 FP64 codec·stage-lock·native PSO/함수/RT 캐시 패치와 필요한 화면·자원 수명 처리.
- ZZZ에는 RX 9070, 다른 실행에는 RTX 5060을 노출하는 GPU 정책.
- FG가 멈췄을 때 Metal HUD의 Frame Interpolator 행을 정리하는 처리.

자체 MSync 튜닝, 커서 소유권·RawInput·warp 변경, Wine 11.17 범용 백포트, 사전 cache warmup, 유휴 MetalFX SR 백엔드 풀, 과거 RT 진단·변환 도구는 포함하지 않습니다.
커서 변경은 향후 A/B 비교 대상이며 이 빌드에는 적용하지 않습니다.

## 소스와 입력 경계

`scripts/build-wine-crossover.sh`는 고정된 공식 `crossover-sources-26.3.0.tar.gz`를 검증하고 `build/cx26.3/source-root/sources/wine`에 준비합니다.
`patches/wine-cx/`의 세 패치와 명시적으로 열거한 신규 FSR 소스만 적용합니다.
저장소 루트에는 선택 패치와 신규 FSR 모듈까지 적용한 CX 26.3 / Wine 11.0 소스가 들어 있습니다.
빌드는 고정 아카이브와 같은 패치로 별도의 검증된 소스 트리를 재구성하며, 이전 Wine 본체나 빌드 트리를 상속하지 않습니다.

`scripts/wine-crossover-inputs.json`은 기존 검증 입력에서 가져올 외부 GPTK 연결 모듈과 비-Wine 의존 라이브러리만 허용합니다.
이전 Wine 로더·서버·일반 PE/Unix 모듈·NLS·폰트는 상속하지 않습니다.
입력 런타임의 전체 파일 매니페스트와 개별 입력을 검증하며, 변경된 입력은 거부합니다.
Yaagl 기본 CrossOver 배포판은 호환 처리의 참고 기준이지 이 빌드의 소스 아카이브를 대신하지 않습니다.

## 빌드

필요 도구: Xcode, Node.js, Python 3, llvm-mingw, x86_64 MacPorts/GStreamer 의존성 SDK.
전체 빌드 시작 시 여유 공간 20 GiB 이상, 패키징 시작 시 8 GiB 이상을 요구합니다.

```sh
scripts/build-wine-crossover.sh fetch
scripts/build-wine-crossover.sh preflight
node scripts/build-d3dmetal-autopatch.mjs build/cx26.3/autopatch
scripts/build-wine-crossover.sh all
```

단계별 실행은 `prepare`, `configure`, `build`, `install`, `package`를 사용합니다.
기존 구성 트리나 패키지 출력은 덮어쓰지 않습니다. 중단된 작업은 원인을 해결한 뒤 해당 단계부터 실행합니다.

기본 입력 경로는 제작 환경에 맞춰져 있습니다. 다른 환경에서는 `WINE_CX_ROOT`, `WINE_CX_MINGW`, `WINE_CX_DEPS_PREFIX`, `WINE_CX_GSTREAMER_ROOT`, `WINE_CX_DONOR`를 지정합니다.
`WINE_CX_DONOR`는 경로만 변경하며 고정된 입력 매니페스트 검증을 우회하지 않습니다.

출력:

- `build/cx26.3/host/`: 새 소스에서 설치한 Wine 본체.
- `build/cx26.3/package/wine/`: 외부 연결 모듈·의존성·네이티브 패처를 합친 런타임.
- `build/cx26.3/package/wine-cx26.3-d3dmetal-gptk4.0b2-macos26.tar.xz`: `wine/` 루트의 배포 아카이브.
- `build/cx26.3/package/SHA256SUMS`: 아카이브 체크섬.

## D3DMetal 준비와 검증

배포 아카이브와 Git에는 Apple `D3DMetal.framework`를 넣지 않습니다.
Yaagl은 Apple 라이선스 동의를 받은 뒤 `wine/libexec/yaagl-d3dmetal/prepare-d3dmetal-runtime`에 `--accept-apple-license`를 전달하고, Wine 실행 전에 framework를 `wine/lib/external/`에 준비해야 합니다.
다운로드·패치 전후의 정확한 해시와 서명을 검증하며 다른 GPTK 버전은 허용하지 않습니다.

관련 경계 검사:

```sh
node --test scripts/prepare-d3dmetal-runtime.test.mjs scripts/metalir-fp64-codec-patch.test.mjs
python3 scripts/test-resource-map.py
python3 scripts/test-fg-normalization.py
python3 scripts/test-fsr-memory-query.py
python3 scripts/test-wine-launch-wrapper.py
```

빌드 성공만으로 게임 동작을 보장하지 않습니다. 배포 전 격리 prefix에서 32/64-bit 프로그램과 ARM64 서버, 실제 D3D12/FSR GPU 실행, MF 영상 재생을 확인해야 합니다.
게임 화면·장시간 플레이·프레임 생성 화질은 별도의 실게임 검증이 필요합니다.
기존 앱·게임 prefix는 빌드 과정에서 변경하지 않습니다.

## 라이선스

[LICENSE](LICENSE), [COPYING.LIB](COPYING.LIB)와 각 외부 소스에 포함된 저작권 고지를 참고하세요. Apple GPTK에는 별도 라이선스가 적용됩니다.
