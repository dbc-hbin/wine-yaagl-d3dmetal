# zzz-wine-d3dmetal-dx12

[English](README.md) | **한국어**

Apple Silicon의 **Yaagl ZZZ OS**에서 **젠레스 존 제로(Zenless Zone Zero, ZZZ)**를 **Direct3D 12(Apple GPTK 4.0b2)**로 실행하기 위한 Wine 11.17 런타임 소스와 원클릭 GUI 설치 프로그램입니다.

v1.1.0 공개 런타임은 그래픽 어댑터를 **AMD Radeon RX 9070**(`0x1002:0x7550`)으로 표시하고 게임의 FSR 업스케일링 API를 MetalFX로 번역합니다. DLSS 또는 NVIDIA NGX 번역 경로는 포함하지 않습니다. 설치 프로그램은 Yaagl Wine 메뉴에 **`Wine 11.17 ZZZ DX12 (GPTK4.0b2)`**를 등록합니다.

**요구 환경은 Apple Silicon의 macOS 26.0 이상과 Rosetta 2입니다.** 모든 Mac에서 temporal upscaling은 시스템 기본 MetalFX 모델을 사용하며 BBR 또는 비공개 모델 버전을 강제하지 않습니다.

## 빠른 시작

1. [ZZZWineDX12Installer.zip](https://github.com/dbc-hbin/zzz-wine-d3dmetal-dx12/releases/latest/download/ZZZWineDX12Installer.zip)을 다운로드합니다.
2. 압축을 풀고 **`ZZZ Wine DX12 Installer.app`**을 실행합니다.
3. Yaagl과 Wine 프로세스를 종료합니다. **`Yaagl Target`**에서 사용할 런처를 고른 뒤 **`Install Wine 11.17 ZZZ DX12`**를 선택합니다. 같은 런타임이 이미 선택돼 있으면 버튼이 **`Reinstall / Update Wine`**으로 표시됩니다.
4. 선택한 Yaagl 런처를 실행하고 Wine 메뉴에서 **`Wine 11.17 ZZZ DX12 (GPTK4.0b2)`**를 선택합니다.

설치 프로그램은 Yaagl 앱과 지원 디렉터리를 감지하고, 동봉 아카이브를 설치하고, 런타임을 등록하며, Yaagl 리소스·Wine 선택·이전 런타임 디렉터리를 백업합니다. 아카이브는 Yaagl의 로컬 런타임 저장소에 남아 오프라인에서도 선택할 수 있습니다. Node.js는 필요하지 않습니다.

같은 이름의 런타임도 재설치할 수 있습니다. 이름이 같다는 이유로 현재 파일이라고 간주하지 않고 동봉 아카이브로 캐시와 런타임 디렉터리를 모두 교체합니다. 마지막 Wine 선택 활성화에 실패하면 최초 복원 백업을 소비하지 않고 이번 시도 직전의 런타임과 선택 상태로 되돌립니다. 여기서 “업데이트”는 실행한 설치 프로그램에 포함된 빌드로 교체한다는 뜻이며 온라인 업데이트 확인 기능이 아닙니다.

현재 설치기 소스에는 아직 기존 배포 ZIP에 포함되지 않은 DX12 마이그레이션이 있습니다. 구버전의 강제 DX12 실행 규칙이 확인되고, 같은 대상 D3DMetal 런타임이 선택돼 DX12를 지원하며, 저장된 DX12 설정이 없을 때만 ON을 저장해 v1.0.5의 실효 기본값을 보존합니다. 저장된 OFF는 덮어쓰지 않습니다. 새 런처와 이미 설정 기반인 런처는 기존 설정을 따르며, v1.1.x의 불명확한 설정 이력을 값만 보고 추측하지 않습니다.

### 터미널 설치

대상 선택 메뉴는 **Yaagl ZZZ OS**, **Yaagl ZZZ OS DX12 Beta**(글로벌), **Yaagl ZZZ DX12 Beta**(중국)를 지원하며 [DX12 베타 릴리즈](https://github.com/dbc-hbin/yaagl-ZZZ-DX12/releases)에도 설치할 수 있습니다. 각 대상은 별도의 `~/Library/Application Support/<런처 이름>` 폴더를 사용하므로 베타에 설치해도 일반판의 Wine은 교체하지 않습니다. Yaagl을 처음 설치했다면 한 번 실행해 지원 폴더를 만든 뒤 종료하고 설치 프로그램을 사용하세요. 일반판이 설치돼 있으면 기본 선택하며, 없으면 설치된 베타를 감지합니다.

CLI에서는 글로벌 베타에 `--app-path "/Applications/Yaagl ZZZ OS DX12 Beta.app"`, 중국 베타에 `--app-path "/Applications/Yaagl ZZZ DX12 Beta.app"`를 지정합니다. 알려진 앱 이름이면 대응하는 지원 폴더를 자동 선택하며, 사용자 지정 설치에서는 명시한 `--support-path`가 우선합니다.

```bash
./installer/zzz-wine-installer --install \
  --app-path "/Applications/Yaagl ZZZ OS.app" \
  --support-path "$HOME/Library/Application Support/Yaagl ZZZ OS"

./installer/zzz-wine-installer --restore \
  --app-path "/Applications/Yaagl ZZZ OS.app" \
  --support-path "$HOME/Library/Application Support/Yaagl ZZZ OS"
```

## v1.1.0 공개 런타임

### FSR 업스케일링 → MetalFX

- 공개 v1.1.0 wrapper는 그래픽 식별자를 AMD Radeon RX 9070(`0x1002:0x7550`)으로 고정하며 NVIDIA 선택은 없습니다. **미배포 현재 소스**의 wrapper는 `ZenlessZoneZero.exe` 실행에는 RX 9070을, 나머지 실행에는 NVIDIA GeForce RTX 5060(`0x10de:0x2d05`)을 자동 지정합니다. 직접·Steam 경유 실행 인자 또는 호출된 Yaagl 표준 `config.bat`의 내용에서 실행 파일명을 대소문자 구분 없이 판별합니다. 배치는 `WINEPREFIX`의 부모 디렉터리에서 읽으며, 무관한 실행에는 이전 배치의 ZZZ 정보가 적용되지 않습니다. 상속된 `D3DM_*` GPU 값은 덮어쓰고 기존 수동 선택용 `YAAGL_GPU_IDENTITY`는 제거합니다. 이 Wine 빌드만 변경하며 Yaagl·DXMT 소스는 변경하지 않습니다.
- ZZZ 이외의 실행도 포함하여 매 `wine.real` 실행 전에 보관된 `d3d10core.dll`, `d3d11.dll`, `dxgi.dll`을 복원하고 Yaagl의 정상 원복을 위해 `.bak`은 보존합니다. 복원 실패 시 종료 코드 124로 실행을 중단합니다. 격리 회귀 검사는 `python3 scripts/test-wine-launch-wrapper.py`로 실행하며, 실제 Wine 실행이나 게임 호환성 검증은 아닙니다.
- 현재 wrapper는 별도 helper 없이 FSR을 선택하고 Yaagl의 `MTL_HUD_ENABLED` 선택(미설정·빈 값 포함)을 보존합니다. 기존 v1.1.x 배포 archive에는 HUD를 강제로 켜는 구 helper가 남아 있습니다. 현재 wrapper 동작을 적용하려면 기존 아카이브와 설치된 런타임을 교체해야 합니다.
- builtin `amd_fidelityfx_upscaler_dx12` 모듈이 공개 FSR API 경계를 구현하고 허용된 temporal-upscaling 작업을 MetalFX로 번역합니다. AMD FSR4 신경망을 실행하지 않습니다.
- 새로 staging한 런타임에서는 `YAAGL_FSR_UPSCALER=metalfx`(미설정·빈 값도 기본값)가 builtin 업스케일러를, `YAAGL_FSR_UPSCALER=native`가 게임의 원본 canonical 업스케일러 DLL을 선택합니다. native 선택 시 builtin으로 fallback하지 않습니다. 이 명시적 SR 비교 옵션은 실행 wrapper를 통과하며 프레임 생성 provider 정책은 바꾸지 않습니다. 다른 값은 Wine 실행 전에 오류로 종료합니다. 기존 배포 archive에는 다시 빌드하기 전까지 반영되지 않습니다.
- Native AA와 Quality, Balanced, Performance, Ultra Performance 모드는 게임/provider가 명시적으로 선택합니다. 번역기가 임의로 품질 모드를 선택하지 않습니다. 요청이 MetalFX 최대 temporal 배율을 넘으면 MetalFX 출력을 하나의 균일 배율로 제한해 caller의 출력 텍스처 가운데에 배치하고 주변 texel은 보존합니다.
- 명시적인 OFF 선택은 게임 설정을 그대로 따릅니다. 런타임이 업스케일링이나 프레임 생성을 자동으로 켜지 않습니다.
- 새로 staging한 런타임은 출력 크기가 반복 변경될 때 FSR context당 비활성 temporal scaler를 최대 3개 보유합니다. 이전 크기로 돌아가면 temporal history를 reset합니다. 처음 보는 크기는 여전히 scaler를 생성하므로 MetalFX/driver가 계상하는 메모리가 증가할 수 있습니다.
- 미배포 소스는 완료된 SR 작업의 descriptor가 일치하는 임시 텍스처와 상수 버퍼를 dispatch 사이에 재사용합니다. MetalFX feature당 비활성 텍스처는 최대 128 MiB를 보유하며 caller별 residency·argument table은 매 프레임 다시 만듭니다. 할당 횟수를 줄이는 대신 제한된 메모리를 보유하는 방식이며 FPS 개선을 입증한 것은 아닙니다. 기존 배포 archive에는 포함되지 않습니다.
- 미배포 SR은 MetalFX feature당 descriptor가 호환되는 reactive mask 유무별 scaler를 최대 2개 보유합니다. 이전 variant로 돌아갈 때 history를 reset하며 format·layout·입력 수용 크기가 바뀌면 호환되지 않는 variant를 제거하되 진행 중인 frame과 lease가 필요한 리소스를 계속 소유합니다. ARM64 Metal4에서 mask 교대·composition mask·입력 크기 증가·출력 readback 검사를 통과했으며 실제 게임 FPS 개선을 측정한 것은 아닙니다.
- 모든 Mac에서 시스템 기본 MetalFX temporal 모델을 사용합니다. 하드웨어 이름 추정, BBR 강제 정책 또는 비공개 모델 버전 override는 없습니다.
- FSR exposure, reactive/composition mask, transfer function, sharpening, reset, jitter, motion-vector scale 및 활성 입출력 범위를 명시적으로 번역합니다. 잘못되거나 지원하지 않는 계약은 성공 no-op으로 처리하지 않고 오류를 반환합니다.

### 프레임 생성과 native fallback

- 자동 프레임 생성 provider는 실제 command-buffer mode와 Apple의 해당 mode 지원 검사를 통과할 때만 MetalFX interpolation을 선택합니다.
- 원본 FSR provider가 swapchain 생성·wrapping, presentation timing, pacing, 등록된 UI resource, custom present callback 및 위임된 swapchain query를 계속 소유합니다.
- 명시적인 native provider 선택은 native 경로를 유지합니다. 번역할 수 없는 입력은 MetalFX 작업을 기록하기 전에 원본 provider로 fallback합니다. MetalFX가 해당 frame의 작업을 기록한 뒤 오류가 발생해도 두 번째 native interpolation을 실행하지 않습니다.
- 패키지 런타임은 private 읽기 전용 native fallback을 절대 경로로 연결해 canonical DLL 재귀 로드를 막습니다. 원본 loader와 native fallback은 override하지 않습니다.
- 명시적인 OFF 상태는 그대로 꺼진 상태입니다. HUD label이 남았다는 사실만으로 프레임 생성이 계속됐다고 판단할 수 없습니다.
- 새로 staging한 런타임은 프레임 생성 presentation을 끄면 소비되지 않은 frame metadata를 비웁니다. 이미 기록된 GPU 작업은 완료까지 자체 resource를 보유합니다. ON에서 서로 다른 미완료 frame 설정은 최대 64개를 허용하며, 완료 콜백이 정리하기 전의 추가 설정은 HUD-less resource를 무한히 보유하는 대신 runtime error를 반환합니다.

### 번역 계약 상세

- 논리 D3D12 device는 raw COM tear-off 포인터 비교가 아니라 `ID3D12Device::GetAdapterLuid`로 식별합니다. command list와 모든 resource는 같은 adapter LUID를 보고해야 합니다. 이는 단일-adapter 런타임 검증이며 실제 multi-adapter 하드웨어 검증은 아닙니다.
- 현재 활성 입력/출력보다 큰 backing texture를 허용합니다. MetalFX에는 활성 크기와 일치하는 resource를 전달하고 필요한 경우에만 GPU staging을 사용하며 caller의 활성 출력 영역 밖 texel은 보존합니다. 저해상도와 출력 해상도 motion vector 모두 같은 규칙을 따릅니다.
- Prepare V1은 camera 정보를 생략하거나 단일 optional camera extension으로 제공할 수 있고 V2는 camera 정보를 직접 포함합니다. frame ID가 연속적이지 않거나 reset이 명시되면 history를 reset합니다. generation rectangle은 signed 좌표를 유지하며 width와 height가 모두 0일 때만 full display이고 partial rectangle의 좌상단은 depth/motion 좌표 (0,0)에 대응합니다.
- 프레임 생성 Prepare는 고정 SDK의 camera 기본값 처리를 따릅니다. 유한한 `viewSpaceToMetersFactor ≤ 0`은 배율 `1.0`으로 처리하고 context 생성 시 무한 깊이를 선택했다면 `cameraFar`는 검사하지 않습니다. 유한 깊이 모드의 두 plane은 양수·유한·서로 다른 값이어야 하며 min/max로 순서를 정규화합니다. reversed depth는 별도 flag로 유지하므로 `cameraNear=5000`, `cameraFar=0.1`도 depth texture를 반전하지 않고 처리합니다. 유한하지 않은 scale은 계속 거부합니다.
- sRGB, PQ, scRGB transfer는 정의된 luminance 변환을 보존하고 유한하지 않거나 잘못된 범위는 거부합니다. sharpening이 꺼져 있으면 `[0,1]` 범위의 유한한 sharpness가 남아 있어도 RCAS 없이 처리합니다. jitter phase query는 고정 SDK처럼 소수부를 버리고(1600→2000은 12), null dispatch descriptor는 `FFX_API_RETURN_ERROR_PARAMETER`를 반환합니다.
- distortion field, AMD debug shader view와 tear/reset overlay, custom DX12 backend allocation callback, frame당 2개 이상의 generated output은 지원하지 않으며 오류를 반환합니다.
- Metal4 compute parameter buffer는 encode 전에 residency에 포함합니다. configuration cache는 최대 8개이며 luminance·rectangle 원점 변경은 factory 재생성 없이 history를 reset하고, cache에서 제거된 configuration도 진행 중 작업이 완료될 때까지 보존합니다.
- swapchain에 알리는 Configure 호출은 앱 callback과 user context가 같으면 불변 binding을 재사용해 불필요한 present drain을 피합니다. 실제 callback 변경 시에는 native swapchain을 통해 이전 binding을 회수하며 일반 Configure 호출 중 처리 대기 중인 frame별 HUD-less snapshot을 보존합니다. 이는 pacing·수명 관리 수정이지 측정된 FPS나 화질 개선을 뜻하지 않습니다.

### 프레임 생성 검증 경계

아래 항목은 아직 검증하지 않은 위험입니다. 이 항목들이 닫히기 전까지 이 경로는 FPS나 화질을 보장하지 않습니다.

- 과거 합성 검증은 균일한 depth와 하나의 global motion vector를 사용했습니다. disocclusion이나 전경/배경의 혼합 motion은 다루지 않았으며, 게임에서 관찰된 2256×1272 render-resolution motion vector가 3840×2160 출력으로 전달되는 조건도 재현하지 않았습니다.
- 현재 depth와 motion vector는 보간 전에 nearest sampling으로 확대합니다. 이 방식과 MetalFX에 native 저해상도 입력을 직접 주는 방식 중 어느 쪽이 나은지는 A/B 비교가 필요하며, 알려진 화질 버그로 확정된 것은 아닙니다.
- descriptor의 nullable `scaler`는 연결하지 않습니다. Apple WWDC25 session 211 샘플은 scaler를 연결하지만, 이는 아키텍처 차이일 뿐 현재 경로의 화질 결함을 입증하지 않습니다.
- 입력 color가 이미 temporal upscale된 경우 jitter 단위는 아직 확인되지 않았습니다. 근거 없이 jitter를 재배율하거나 0으로 바꾸지 말고, 먼저 producer가 실제로 사용하는 규약을 캡처한 뒤 시간적으로 안정된 장면에서 비교해야 합니다.
- 4K에서 RGBA16F, R32F, RG16F texture 크기로 계산한 Generate 1회당 논리적 scratch 할당량은 UI 없을 때 약 190 MiB, UI가 있을 때 약 253 MiB입니다. 이는 논리적 할당 크기이며 측정된 resident memory, bandwidth, latency 또는 frame-time 비용이 아닙니다.
- generation과 생성 프레임 present 로그는 각각 callback 120회에서 중단되며 게임 frame 120개를 뜻하지 않습니다. OFF 전환이나 그 이후 생성 중단을 기록하지 않으므로 이를 입증할 수 없습니다.

다음 검증은 2256×1272→3840×2160 조건에서 disocclusion과 혼합 motion이 있는 게임 캡처, nearest 확대 입력과 native 저해상도 depth/MV 입력의 A/B 비교, 실제 jitter 규약 기록, resident memory 및 GPU 시간 profiling, callback 로그 제한 이후에도 OFF 전환과 후속 생성 동작을 명시적으로 관찰하는 과정을 포함해야 합니다.

### 선택적 제한 로그

`YAAGL_FSR_LOG`는 선택 사항이며 절대 경로를 지정해야 합니다.

- 업스케일링은 lifecycle/query/error event와 성공한 dispatch metadata를 전역 dispatch ID 1~120에 대해서만 기록합니다. 실패 frame 상세 기록은 별도로 최대 120회입니다.
- 프레임 생성은 `YAAGL_FSR_LOG`에 `first_encode` JSON record를 한 번 기록합니다. stderr의 프레임 생성 실패 기록은 120회에서 중단합니다.
- 선택적 `WINEDEBUG=trace+yaagl_fsr_fg` 채널은 generation과 생성 프레임 present callback 결과를 각각 120회까지만 기록합니다.
- 정상 상태에서 무제한 frame별 로그를 남기지 않습니다.
- 로그는 API, encode 또는 callback 진행을 나타냅니다. GPU 완료, 화질, FPS 또는 OFF 전환의 증거가 아닙니다.

### 미배포 선택적 커서 진단

현재 소스의 `winemac.so`와 `win32u.so`를 함께 빌드해야 합니다. 기존 배포 런타임에는 이 기능이 없습니다. `YAAGL_CURSOR_TRACE`가 Wine 프로세스에 전달된 경우에만 기록하며, 기록 활성화 자체는 커서 보정·클리핑·RawInput 전달 정책을 바꾸지 않습니다.

```sh
trace_dir="$(mktemp -d /tmp/yaagl-cursor.XXXXXX)"
YAAGL_CURSOR_TRACE="$trace_dir" /path/to/diagnostic/wine /path/to/application.exe
python3 scripts/decode-cursor-trace.py "$trace_dir" --summary
python3 scripts/decode-cursor-trace.py "$trace_dir" --last-seconds 5 > cursor.jsonl
# 특정 레코드의 clock_ns를 중심으로 전후 구간 선택:
python3 scripts/decode-cursor-trace.py "$trace_dir" --around-ns 123456789000 --before 2 --after 1
```

- 출력 디렉터리는 미리 존재하는 절대 경로여야 합니다. 파일은 권한 `0600`으로 생성됩니다. 프로세스 시작 때 설정을 읽으므로, 다음 실행에서 변수를 전달하지 않으면 꺼집니다.
- 실제 Confinement/EventTap 선택, 클립·포커스·Retina 전환, 강제 이동과 no-op, EventTap 보정, Cocoa 이동량과 시간 필터, 큐 병합·폐기, Wine 전달, 앱의 RawInput·좌표 읽기를 기록합니다. 키보드 내용과 게임 카메라 각도는 기록하지 않습니다.
- DLL별·프로세스별로 **8 MiB + 128바이트**, 최근 65,536개 레코드만 보존합니다. 파일은 종료 후에도 남습니다. 입력 경로에서 명시적인 파일 쓰기·추가 스레드·동적 할당은 하지 않지만, 진단 비용과 OS의 매핑 페이지 쓰기 비용이 없다는 뜻은 아닙니다.
- 증상 직후 별도 터미널에서 읽으십시오. 오래 기다리면 해당 구간이 덮어써집니다. 실행 중 읽기는 원자적 전체 스냅샷이 아니며 분석기는 경합으로 빠진 기록, 보존 범위 밖 시퀀스, 불안정한 슬롯을 보고합니다.
- `clock_ns`는 공통 단조 시계이고 원본 이벤트 시간의 단위는 별도 표시합니다. 포인터 식별자는 재사용될 수 있어 시퀀스와 수명을 함께 봐야 하며 프로세스 간 인과관계를 자동으로 확정하지 않습니다. 같은 RawInput 핸들의 반복 읽기를 추가 물리 입력으로 합산하면 안 됩니다. Confinement에서는 EventTap 워프 보정이 실행되지 않습니다.
- 수집기·분석기의 비활성화, 순환 경계, 동시 기록, fork 격리, 잘못된 파일 검증: `python3 scripts/test-cursor-trace.py`.

### 미배포 커서 재적용 중복 축소

커서 실험 브랜치의 `0017-cursor-reconciliation-coalescing.patch`는 한 커서 요청의 모양·애니메이션·표시 상태를 먼저 준비하고, 마지막에 위치 판정과 네이티브 재적용을 한 번 수행합니다. 동일한 커서 요청도 재적용하므로 macOS가 기본 화살표로 덮어쓴 상태를 복구할 수 있습니다. 같은 애니메이션 프레임 배열은 프레임 위치와 타이머를 유지합니다.

같은 창의 연속된 동기화 요청은 마지막 메인 큐 작업이 아직 대기 중일 때만 합칩니다. 다른 창이 끼어든 `A → B → A`의 순서와, 작업 시작 이후 발생한 새 요청은 보존합니다. 시작·활성화·첫 콘텐츠·표시/숨김·AppKit 커서 전환 트리거와 기존 Wine 이벤트 큐 병합은 유지합니다. 이 변경은 진단 환경변수와 독립적이며, RawInput·좌표·워프 보정이나 서버 프로토콜은 바꾸지 않습니다.

### 미배포 직접 워프 입력 보정

`0018-confinement-warp-correction.patch`는 Confinement/클리핑 해제 상태에서 성공한 직접 커서 워프를 추적하고, 해당 이동 이벤트에서 실제 네이티브 강제 이동량만 뺀 뒤 일반 이동과 RawInput을 누적합니다. 이동량이 0인 워프 알림은 보정을 소비하지 않으며, 타임스탬프 순서로 대기 이벤트와 연속 워프를 구분합니다. no-op·실패한 워프는 이동량을 추가하지 않고, 포커스 전환에서는 오래된 보정을 지웁니다. EventTap 경로는 기존 보정을 유지하며 이중으로 차감하지 않습니다. 큐 공간은 커서를 움직이기 전에 확보하므로 할당 실패 시 추적되지 않은 워프 대신 이동 실패를 반환합니다.

`python3 scripts/test-cursor-warp-correction.py`로 입력 보존·할당 실패 회귀 8개를 검사합니다. 재빌드한 x86_64 `winemac.so`의 실제 Objective-C 이동 처리 메서드에 캡처 이벤트를 재생했을 때, 지연된 `(-675,-136)`에서 실제 잔여 움직임 `(1,-2)`가 남고 다음 `(4,-3)` 입력도 유지됐습니다. 이 재생은 네이티브 창·큐 경계를 대체하므로 물리 마우스 타이밍이나 게임 카메라 검증은 아닙니다. 적용에는 `winemac.so` 재빌드가 필요하며, 소스 변경만으로 런타임을 설치하거나 진단 기록을 끄지 않습니다.

### 미배포 현재 소스의 NGX 복구

Metal4 replay와 legacy encode의 공용 처리는 별도 외부 wrapper를 유지하지 않고 `ngx-hooks.mm` 안으로 직접 복원했습니다. `bridge.mm`의 슬롯 17·18은 NGX replay·encode 진입점에 직접 연결되며, NGX descriptor를 해석하기 전에 FSR 기록 명령을 식별하고 실행합니다. 분리했던 `d3dmetal-replay-hooks.{hpp,mm}` 계층과 해당 빌드 항목은 제거했습니다.

공개 v1.1.0/v1.1.1 런타임은 여전히 FSR 전용입니다. **미배포 schema 6 후보**는 v1.0.5로 되돌아가지 않고 현재 Wine 소스에 추가하는 방식으로 GPTK 원본 `nvngx.dll`(SHA-256 `f6bc9d77fd1e898fec8c6339d367bd8e0f338992c9c0c66d59b30c6e9e0743e4`)과 `nvngx.so` → `../../external/libd3dshared.dylib`(대상 SHA-256 `d932330841e77682d47688641e0ac17049a2aff498deafac88921983dc16eedb`)을 복구합니다. staging 기록에 출처·symlink 대상·서명 산출물 hash 목록과 게임별 자동 GPU 정책을 보존합니다. 과거 schema 5 기록은 당시의 수동 선택 정책을 유지하며, metadata 갱신으로 기존 런타임의 동작을 새 정책으로 잘못 표시하지 않습니다. 기존 설치 아카이브·게임·prefix는 변경하지 않습니다.

Native layout **v14**는 dispatch 항목 25개(일반 hook 21개와 특수 hook 4개)를 게시합니다. NGX wrapper는 공용 replay·encode 두 개만 남기며, FSR을 우선 처리하고 일반 NGX 명령은 원본 D3DMetal replay·encode에 바로 전달합니다. NGX private 출력 shadow·복사 adapter는 제거했으며 FSR backend의 출력 처리는 변경하지 않았습니다. NGX Evaluate·record 관찰 훅, 진단 옵션, 선택적 exposure·temporal 보정은 제거했고 해당 호출은 stock NGX가 직접 처리합니다. 현재 FSR, 출력 선택, GPU 완료·리소스 수명 관리, Wine MSync 수정과 시스템 기본 MetalFX 모델은 유지합니다. GPU 식별자는 위의 게임별 실행 정책을 따릅니다. 기존 frame-probe 캡처 계층도 포함하지 않습니다.

앞선 격리 실행에서는 Metal API Validation을 끈 상태로 Metal4·legacy의 stock NGX 경로를 실행했습니다. 해당 합성 테스트는 제거했으며, 출력을 재사용하던 검사는 매 NGX 평가가 새 픽셀을 생성했다는 근거가 되지 못합니다. Validation ON에서는 shared 출력 NGX fixture에서 `outputTexture must have private storage mode` assertion이 발생했으며 이 제한은 남아 있습니다. production Validation 설정은 변경하지 않았고, Validation 준수·실게임 안정성·FPS·화질 개선을 주장하지 않습니다. 짝이 맞는 format-14 D3DMetal patch와 native sidecar가 필요하며 공개 배포 아카이브는 변경하지 않았습니다.

현재 소스는 완료된 execution lease를 allocator Reset 전에 회수하되, 미제출 작업이나 callback 등록 실패에서는 owner의 안전한 보유를 유지합니다. SR은 더 작은 active input에서 호환되는 scaler capacity를 재사용하며 history reset과 동기화된 edge staging을 수행합니다. [메모리 측정 결과와 한계](docs/screenshot-sr-analysis-2026-09-23.ko.md)는 bounded reuse와 즉시 물리 메모리 반환, 아직 검증하지 않은 게임 전체 메모리 차이를 구분합니다.

### 미배포 수명 관리 수정

MSync 메시지 분기 회귀 검사: macOS에서 `python3 scripts/test-msync-message-dispatch.py -v`. 제품 C 함수 본문을 실행하는 10개 사례로 높은 ID의 대기 등록·해제, 잘못된 close 거부, 실제 전용 Mach 포트를 통한 close 처리를 검사합니다.

- 프로세스 내 동기화 캐시는 24바이트 항목이 블록 크기를 나누어떨어지게 하지 않아도 64 KiB 전체를 할당합니다. MSync export ID 추가 후 높은 핸들이 다음 캐시 블록을 요구할 때 발생하던 `anon_mmap_alloc` assertion을 항목 패딩이나 소유권 검사 제거 없이 수정했습니다. `ntdll` sync 회귀 테스트는 이벤트 4,096개를 유지하면서 독립적인 signal/reset/wait 상태를 검사합니다. 테스트 본문을 추출한 실행은 재빌드한 런타임과 격리 prefix에서 18,432개 assertion을 통과했고, 별도 이벤트 2,740개 API 스모크도 기존에 크래시하던 높은 핸들 동작을 통과했습니다. 테스트는 소스 테스트 모음에 유지하고 런타임 quilt에는 제품 코드 수정만 반영합니다. Beta 설치나 실게임 검증을 뜻하지 않습니다.
- MSync 스레드 종료 시 빌린 alert 인덱스를 Unix descriptor로 닫지 않습니다. 캐시 참조에 프로세스별 일회성 export ID를 부여하고 native 프로세스 종료 후 회수합니다. `ntdll`과 `wineserver`를 함께 빌드해야 합니다. 서버 프로토콜은 tuned **968**, safe-msync **969**, MSync Mach wire는 **3**이며 기존 요청 번호는 유지합니다. 프로토콜 969는 safe-msync 요청 구조 전용으로 예약하며 tuned에서 재사용하면 안 됩니다.
- 서버 내부 MSync export 해제는 전용 메시지 ID를 사용하고 공유 인덱스는 payload에 따로 전달하므로 높은 스레드 ID가 대기 등록·해제를 close로 바꾸지 않습니다. 정확한 메시지 크기·서버 cookie·인덱스 범위를 확인한 뒤 참조를 해제합니다. 잘못된 close는 대기로 해석하지 않고 버리며 클라이언트 대기 메시지와 공유 메모리 구조는 유지합니다. 격리한 재빌드 Wine에서 이벤트 4,096개 반복 재사용, 실제 등록 로그를 확인한 다중 대기 깨우기 256회, export를 남긴 자식 프로세스 종료를 통과했습니다.
- 공통 MSync 패치에서 커서 프로토콜 의존성을 분리했습니다. tuned와 safe-msync는 마지막 프로토콜 버전 패치를 각각 적용하고, safe-msync에는 커서·창 변경을 가져오지 않습니다. 복원한 pre-tuned baseline에서 두 프로필의 실제 prepare overlay 함수, 반복 적용 검증, 소스 상속 inventory 검사, `tools/make_requests` 재생성 결과의 바이트 일치를 확인했습니다. 준비된 P3 baseline이 없어 upstream pin을 확인하는 전체 preflight는 실행하지 않았습니다. P3 manifest의 해시는 변경된 graphics-bridge 패치와 일치합니다.
- macdrv는 창 데이터 잠금 안에서 상태를 분리한 뒤 잠금 밖에서 Cocoa 창과 대기 중인 surface 이벤트를 정리합니다. 창 파괴와 최상위 창의 자식 창 전환에 같은 순서를 적용합니다.
- Native FG 콜백은 소유 context의 callback scope에 진입해 configure/dispatch 잠금 역전을 피합니다. 콜백이 같으면 할당·present 대기 없이 binding을 재사용하고, 이전 binding은 교체 성공과 필요한 drain 이후에만 회수합니다.
- FG bridge **v4**는 콜백 실패·생성 생략에도 완료된 frame ID를 정리합니다. 이전 미완료 프레임과 기록된 명령의 snapshot은 보존합니다. FG PE/Unix 모듈과 native sidecar를 함께 다시 빌드해야 합니다.
- SR 준비 프레임마다 scaler 활성화 번호를 보관해 지연 실행이나 전환 직후 프레임 폐기로 reactive variant 복귀에 필요한 reset이 사라지지 않게 합니다.
- MF 비동기 명령에 초기 소유 참조를 부여하고 소스 오류 시 대기 중인 읽기·seek를 실패 완료합니다. WM parser 재초기화 실패는 disconnect 후 읽기 스레드를 join합니다. IOHID 시작은 런루프 잠금 대기 없이 상태를 원자적으로 게시합니다.

현재 소스의 수정이며 설치된 Beta나 기존 archive를 바꾸지 않습니다. 실제 게임 FPS 개선을 입증한 것은 아닙니다.

### 과거 검증 기록

아래는 과거 릴리스 검사 기록이며 현재 실게임 합격 판정이 아닙니다. 여기서 언급하는 합성 테스트는 이후 제거했습니다.

macOS 27 / Apple M5 Pro에서 다음을 확인했습니다.

- **v1.1.1은 등록 실패를 수정했습니다.** 이 설치기가 이미 등록한 Yaagl frontend인데 hash 기반 복원 백업이 사라진 경우 “changed without a matching backup”으로 거부되던 문제입니다. 이제 등록 시 해당 frontend에서 이 설치기 자신의 updater hook과 catalog 항목만 제거해 marker 없는 복원 기준을 복구하며, 무관한 catalog 항목·frontend 버전·local-archive 설치 경로는 유지합니다. 인식할 수 없거나 변조된 hook은 frontend를 건드리지 않고 그대로 실패합니다. Wine 런타임 바이트는 v1.1.0과 동일합니다.
- 보고된 상태의 사본으로 재현했습니다. v1.1.0 helper는 보고된 메시지와 함께 종료 코드 1로 실패하고 아무것도 바꾸지 않았고, v1.1.1 helper는 완료해 복구 기준을 기록하면서 등록된 frontend를 바이트 그대로 유지했습니다. 이미 등록된 바이트를 다시 등록해도 변화가 없으며, hook 인자를 변조하면 여전히 종료 코드 1로 실패하고 frontend는 보존됩니다.
- 최종 전체 런타임 아카이브를 다시 추출해 Metal4와 legacy command-buffer 양쪽에서 FSR 업스케일링·프레임 생성 GPU 검사를 통과했습니다. 이 검사에는 Metal API Validation을 활성화했습니다.
- 일반 실행 환경에서 DX12 그래픽·컴퓨트·레이 트레이싱 GPU readback을 통과했습니다. direct/indirect draw, blending, logic operation, MSAA 및 동일·상이 descriptor의 독립 D3D12 객체를 포함합니다.
- MetalFX backend, quality, transport, legacy-transport 네이티브 suite를 통과했습니다. 네이티브 cache/stage-cache/key 검사는 graphics·compute·RT key 경로의 single-flight와 객체 재사용을 확인했습니다. production hit counter는 노출되지 않으며 측정했다고 주장하지 않습니다. production launcher의 실제 DXGI 열거 결과는 `0x1002:0x7550`, **AMD Radeon RX 9070**이었습니다.
- core/backend 아카이브를 재조립한 결과 파일 목록·바이트·모드·symlink가 staging과 일치했습니다. 서명과 격리된 Wine 초기화도 통과했습니다. 선언된 tuned Wine core 산출물 45개는 검증된 v1.0.5 base와 바이트가 같으며 FSR/native overlay를 새로 빌드했습니다.
- v1.1.0 설치 ZIP을 다시 추출해 deep/strict 서명을 확인하고 실제 보존한 v1.0.5 아카이브로 설치·업데이트·복원·활성화 실패 시나리오 9개를 통과했습니다. DX12 실행 인자 회귀 3개와 경로를 옮긴 FSR launcher 회귀도 통과했습니다.
- v1.1.1 설치 ZIP은 deep/strict 서명을 통과했고, 변경되지 않은 v1.1.0 런타임을 포함하며, 백업 유실 복구 시나리오를 포함한 리소스 수명주기 10개 시나리오를 모두 통과했습니다.
- 커서 소유권·RawInput 소스 harness와 격리된 Win32 cold-start/layered-window 커서 metadata 검사를 통과했습니다. 네이티브 커서 픽셀이나 물리 RawInput을 측정한 결과는 아닙니다.

**한계:** macOS 26 실기기 실행, 네이티브 커서 픽셀, 첫 물리 RawInput delta는 미검증입니다. Apple 원본 `libdxccontainer.dylib`는 변경하지 않았으며 최소 버전 26.4를 기록합니다. 선택적 Metal API Validation을 켠 generic MSAA resolve 대조 검사에서는 원본 v1.0.5와 v1.1.0 모두 동일한 render-target-usage assertion이 발생합니다. 이 기존 validation 제약을 수정했다고 주장하지 않으며 일반 실행 환경의 GPU readback은 통과했습니다. 위의 프레임 생성 화질·성능 검증 한계도 그대로 적용됩니다.

## 이전 릴리스 기록

### v1.0.5: 커서·RawInput 런타임 재빌드와 동일 이름 업그레이드

- v1.0.5는 커서 소유권·RawInput 분리 수정을 포함해 새 빌드 디렉터리에서 Wine 산출물 45개를 재빌드했습니다. 네이티브 모듈 7개는 SDK 26.5로 macOS 26.0을 타깃으로 빌드했고, 나머지 Wine 파일은 고정된 P3 패키지에서 가져왔습니다. 다른 tuned 패치, 네이티브 PSO 캐시와 `D3DM_MTL4=1`은 변경하지 않았습니다.
- D3DMetal의 DXIL 컨테이너 분석과 DXBC/HLSL 변환에 필요한 Apple 원본 `libdxccontainer.dylib`는 기록된 최소 버전 26.4를 포함해 바이트 그대로 유지했습니다. 조사한 import에서 26.4 전용 API는 발견되지 않았습니다. Wine 설정과 격리된 DX12 그래픽·컴퓨트·레이 트레이싱 GPU readback은 macOS 27에서 통과했으며 macOS 26 실기기 실행은 검증하지 않았습니다.
- 동봉 Wine에는 `db45a95`를 실제 빌드해 넣었습니다. 커서 소유권 동기화가 포인터 좌표를 바꾸지 않으며 보정된 RawInput 이동량은 별도로 전달했습니다. Wine 클라이언트·서버 모듈은 프로토콜 **966**으로 함께 재빌드했습니다.
- Wine 메뉴 이름과 런타임 ID를 유지했습니다. v1.0.5 설치기는 다른 Wine catalog 항목을 보존하면서 같은 이름의 기존 런타임과 캐시 아카이브를 새 빌드로 교체했습니다.
- 최종 선택 활성화에 실패하면 최초 복원 백업을 소비하지 않고 이번 설치 시도 직전의 런타임과 선택 상태로 되돌렸습니다.
- 배포 ZIP을 풀어 설치·업데이트·복원 시나리오 9개와 DX12 실행 인자 검사 3개를 통과했습니다. 이전 프로토콜 965 아카이브에서 업그레이드해 네이티브 모듈 4개(`wineserver`, `ntdll`, `winemac`, `win32u`)의 교체도 확인했습니다.
- 격리된 Wine 창에서 cold-start cursor request, layered-window ownership 및 Esc 이후 capture transition을 실행했습니다. 이 capture는 macOS native activation이나 custom cursor pixel을 확인하지 못했습니다. 합성 포인터 입력으로는 물리 RawInput callback이 발생하지 않아 native cursor pixel과 첫 물리 마우스 이동량은 미검증으로 남았습니다.
- 기존 game cursor 수정은 유지됐습니다. 소스 수준의 arrow setter 호출만으로 의도하지 않은 native cursor overwrite를 입증할 수 없어 이전 native-overlay P2 판정은 철회했습니다.

### v1.0.4: DX12 실행 인자 전달

- v1.0.4는 선택한 배포판 식별자를 실제 Wine runner에 유지하고 **`Wine 11.17 ZZZ DX12 (GPTK4.0b2)`**에만 `-use-d3d12`를 추가했습니다. 이전 설치기는 실행 객체에 없는 `id`를 검사해 패치가 적용돼도 인자를 빠뜨릴 수 있었습니다.
- 일반 실행과 Steam patch 실행 모두에 게임 인자를 전달했습니다. 다른 D3DMetal Wine에는 DX12를 강제하지 않았습니다.
- 이전 ID 조건과 backend 전체 대상 조건을 범위가 제한된 조건으로 교체했으며 반복 설치해도 인자가 중복되지 않았습니다.
- 이 수정에는 Wine 아카이브만이 아니라 설치 프로그램과 update helper가 필요했습니다. v1.0.4는 v1.0.2/v1.0.3 아카이브를 유지했고 v1.0.5에서 커서·RawInput 재빌드 런타임으로 교체했습니다. 이 릴리스들의 Tahoe 실기기 DX12 실행은 검증하지 않았습니다.

### v1.0.3: launcher update와 복원

v1.0.3은 설치 프로그램만 변경했고 macOS 26 Wine 아카이브와 tuning은 v1.0.2와 같았습니다.

- 앱 번들이 아니라 Yaagl 데이터 디렉터리의 실행용 `resources.neu`를 patch했습니다. 앱 리소스와 기존 앱 백업은 건드리지 않았습니다.
- `.zzz-wine-registration`의 native helper가 다운로드된 앱 내부 update에 Wine을 등록한 뒤 활성 frontend를 교체했습니다. 설치 또는 앱 내부 update 때만 실행되며 background service나 Node.js는 필요하지 않았습니다.
- 복원은 오래된 전체 리소스 백업이 아니라 현재 등록 generation과 짝이 맞는 pristine resource를 사용했습니다.

구버전 설치기로 Yaagl이 downgrade됐다면 원하는 버전으로 Yaagl을 update하고 종료한 뒤 현재 설치 프로그램을 실행하세요. 앱 전체 교체나 외부에서의 resource 교체는 앱 내부 hook을 우회할 수 있으므로 그런 변경 후에는 설치 프로그램을 다시 실행하세요.

## 주요 런타임 구성

1. **Direct3D 12와 GPTK 4.0b2** — D3DMetal과 Metal IR이 Direct3D 12 rendering을 Metal로 변환합니다.
2. **Native ARM64 wineserver** — server를 Rosetta로 실행하지 않아 synchronization과 IPC overhead를 줄입니다.
3. **MSync fast path** — Windows synchronization primitive를 overhead가 낮은 macOS mechanism에 연결합니다.
4. **Native PSO cache** — shader compile 중복을 제거하고 device lifetime 동안 compile된 pipeline state object를 유지합니다.
5. **Cursor ownership과 RawInput 분리** — ownership synchronization과 pointer coordinate를 분리하고 보정한 motion delta를 별도로 전달합니다.
6. **Media·audio·window·resource tuning** — 저장소의 GStreamer, Media Foundation, CoreAudio, window 및 network patch를 유지합니다.

미배포 MSync는 abandoned mutex의 `WaitAll` 무한 재시도와 획득 실패 시 rollback을 수정합니다. 이미 소유한 재귀 mutex의 소유권과 abandoned 상태를 보존하고, 이번 시도에서 획득했다가 돌려놓은 객체의 waiter를 깨웁니다. 등록 대기의 128회 spin 한도는 변경하지 않았습니다. 결정적 경쟁 검사로 rollback과 실제 pthread waiter의 깨우기를 확인했으며 격리 Wine API 실행에서 abandoned 반환값·유한 재귀 timeout/소유권·한 번만 소비되는 동작을 확인했습니다. 두 결함은 공식 CrossOver 26.3.0 FOSS 원본(Wine 11.0)에도 존재하며, 상용 CrossOver 바이너리를 실행해 확인한 것은 아닙니다.

## 저장소 구조

```text
zzz-wine-d3dmetal-dx12/
├── dlls/                   # FSR upscaler/FG builtin을 포함한 Wine source
├── d3dmetal-pso-cache/     # Native PSO cache와 FSR → MetalFX backend
├── external/               # 로컬 GPTK framework 입력
├── include/                # Wine/FSR bridge 공용 header
├── installer/              # SwiftUI installer와 CLI source
├── patches/                # Wine tuned/P3 patch series
├── scripts/                # Build, verification, staging, packaging tool
└── server/                 # Native wineserver와 MSync 구현
```

## 소스에서 빌드

### 요구 환경

- Apple Silicon의 macOS 26.0 이상, Rosetta 2 및 macOS 26 SDK
- Xcode Command Line Tools
- LLVM MinGW toolchain
- Bison, pkg-config 및 GStreamer dependency
- 준비된 P3 source/host/dependency/provenance 입력, 로컬 GPTK overlay 및 Steam helper payload. 이 저장소는 해당 외부 입력을 다운로드하지 않습니다.

### 런타임과 설치 프로그램

```bash
export WINE_P3_ROOT="/absolute/path/to/prepared/wine-p3"
export YAAGL_STEAM_HELPER_DIR="/absolute/path/to/protonextras"
export GPTK_SOURCE="/absolute/path/to/gptk-overlay/wine"
export MACOSX_DEPLOYMENT_TARGET=26.0
export SDKROOT="/path/to/MacOSX26.sdk"
export WINE_PACKAGE_NAME=wine-11.17-zzz-dx12-gptk4b2-macos26
export WINE_RUNTIME_ID=11.17-zzz-dx12-tuned-stage-parallel-cache-warmup-cursor-rollback-gptk4b2-arm64server

./scripts/build-wine-tuned.sh all
./scripts/package-wine-p3-runtime.sh build/wine-tuned/host "$GPTK_SOURCE" \
  build/wine-tuned/provenance.json build/wine-tuned/package
./installer/build.sh
```

### 업스트림 Yaagl 연동

[Yaagl PR #759](https://github.com/yaagl/yet-another-anime-game-launcher/pull/759) 연동은 split 쌍을 사용합니다. Yaagl이 backend를 별도로 다운로드·캐시하고 Wine 초기화 전에 추출한 core의 `wine/` 디렉터리에 설치합니다. 두 패키지는 짝을 이룬 조합이며 임의의 Wine·backend 호환성을 보장하지 않습니다. all-in-one 아카이브와 GUI 설치 프로그램이 계속 지원되는 자체 완결 경로입니다. 업스트림 연동에서 ZZZ DirectX 12 옵션은 기본 꺼짐이며 `supportsD3d12`를 선언한 배포판에서만 활성화됩니다.

### v1.1.0 릴리스 asset

v1.1.1은 v1.1.0 런타임을 그대로 다시 게시하며 `ZZZWineDX12Installer.zip`만 변경됩니다. 따라서 아래 런타임 아카이브 이름이 v1.1.1 릴리스에서도 그대로 사용됩니다.

|Asset|내용|
|---|---|
|`ZZZWineDX12Installer.zip`|GUI 설치 프로그램과 아래 full runtime 아카이브|
|`wine-11.17-zzz-dx12-gptk4b2-macos26.tar.xz`|all-in-one 런타임(`wine/` 루트)|
|`wine-11.17-zzz-core-macos26.tar.xz`|split core 아카이브(`wine/` 루트)|
|`d3dmetal-gptk4b2-zzz-v1.1.0.tar.xz`|split backend 오버레이(상대 경로 `lib/`)|

각 아카이브에는 `.sha256` sidecar가 함께 제공됩니다. `installer/build.sh`는 기본적으로 `build/release-v1.1.1/wine-11.17-zzz-dx12-gptk4b2-macos26.tar.xz`의 full runtime 아카이브를 읽습니다(`RUNTIME_ARCHIVE_SOURCE`로 변경 가능).

```bash
# 검증된 현재 schema 4 소스 런타임과 고정된 외부 입력을 지정합니다.
# 설치된 런타임이나 게임 prefix 위에 staging하지 않고 새 후보를 만듭니다.
WINE_ROOT=/absolute/path/to/new-candidate/wine
WINE_SOURCE=/absolute/path/to/current-schema4-runtime/wine
D3DMETAL_INPUT=/absolute/path/to/verified-stage-locked-D3DMetal
NGX_DLL=/absolute/path/to/pinned-gptk/nvngx-on-metalfx.dll
NATIVE_BUILD=/absolute/path/to/native-build
OUTPUT_DIR=/absolute/path/to/split-output
(
  set -e
  # 입력은 현재 고정 D3DMetal layout 검사를 통과해야 합니다.
  # 이 옵션은 SHA가 일치하는 pristine 입력도 허용합니다.
  python3 scripts/stage-runtime.py --wine-source "$WINE_SOURCE" --wine-dest "$WINE_ROOT" \
    --pristine-d3dmetal "$D3DMETAL_INPUT" --ngx-dll "$NGX_DLL" \
    --build-dir "$NATIVE_BUILD" --play --fsr-translator

  # 읽기 전용 현재 소스 런타임을 기준으로 상속된 P3 metadata를 갱신합니다.
  python3 scripts/refresh-staged-runtime-metadata.py \
    --tree "$WINE_ROOT" --base "$WINE_SOURCE" \
    --native-manifest "$NATIVE_BUILD/build-manifest.json"

  # split 패키징은 schema 5를 현재 소스와 대조한 뒤 두 archive를
  # 재조립하고 전용 Wine prefix에서 smoke를 실행합니다.
  sh scripts/package-wine-runtime-split.sh "$WINE_ROOT" "$OUTPUT_DIR"
)
```

정확한 아카이브 hash는 상위 릴리스 노트에 기록하며 이 문서에서는 주장하지 않습니다.

### 출력 모니터와 현재 주사율 선택

앞선 native patch format 12는 adapter output 0 대신 swapchain HWND가 속한 출력을 선택하도록 수정했으며, 현재 format 14도 이를 유지합니다. 창 모드는 모니터 이동을 따라가며, 명시적 전체화면 대상은 창 모드로 돌아올 때까지 우선합니다. 출력을 바꿀 때 swapchain 등록과 참조 소유권도 함께 이전합니다. Present 간격 계산 전 Wine bridge에서 저장된 registry mode가 아닌 `ENUM_CURRENT_SETTINGS`를 조회합니다. `SyncInterval=0`과 명시적 `D3DM_MAX_FPS` 동작은 유지하며, 특정 주사율이나 프레임 제한을 강제하지 않습니다.

현재 P3 patch로 Wine을 다시 빌드한 뒤 staging해야 합니다. `stage-runtime.py`는 `winemac.so`의 별도 `macdrv_query_d3dmetal_display` export를 요구하고 해당 바이너리의 identity를 기록·검증합니다. 구 런타임의 native sidecar만 교체해서는 충분하지 않습니다. 기존 192바이트 Wine callback table은 변경하지 않았습니다.

전체화면 수정에는 sidecar 재빌드뿐 아니라 D3DMetal 재패치도 필요합니다. 직접 Windows ABI로 호출되는 vtable thunk와 unixcall unpacker 모두 명시적 출력 인자를 전달하고 native HRESULT를 보존하도록 고쳤습니다. 출력 선택은 PE `GetDesc` vtable을 다시 호출하는 대신 native 출력 인터페이스를 사용합니다. D3D12 회귀 검사는 모니터 한 대에서도 명시적 전체화면 진입, 상태·출력 조회, 창 모드 복귀를 실행합니다. 기본 실행과 저장값·현재 주사율을 다르게 설정한 실행 모두 실제 디스플레이 모드를 바꾸지 않고 통과했습니다.

격리 D3D12 회귀 검사에서 CURRENT=120Hz / 저장값=60Hz를 재현했습니다. 구 swapchain은 60Hz를 보고했지만 수정본은 120Hz를 보고했고, Present 최소 간격은 16.667ms에서 8.333ms로 바뀌었습니다. SyncInterval 0은 최소 간격을 요청하지 않았으며, 명시적 30FPS 제한은 33.333ms를 유지했습니다. GPU 픽셀 readback도 통과했습니다. 짝을 맞춘 format-12 런타임은 실제 Wine에서 보고된 60/120Hz 두 출력 간 이동·명시적 전체화면 전환과 CURRENT=60Hz / 저장값=50Hz 구분을 Metal API/GPU validation을 켠 상태로 통과했습니다. 게임 FPS 개선을 확인했다는 뜻은 아닙니다.

창의 surface 배열은 CFArray release callback 없이 참조를 명시적으로 소유합니다. 항목 제거는 참조 소유권을 이전하며, 개별 참조 해제와 창 파괴 시 배열의 참조 해제는 `win_data_mutex`를 푼 뒤 수행해 `surfaces_lock`의 역순 획득을 피합니다. 과거 회귀 검사에서는 반대 방향의 잠금 획득과 view 생성 실패·창 파괴 시 참조 균형을 검증했습니다. 실제 D3D12 smoke에서는 swapchain 수명 128회와 동시 창 크기 변경 128회, GPU 픽셀, 창을 먼저 파괴한 뒤 남은 view를 해제하는 경로를 확인했습니다. 앞선 잠금 수명 검사는 surface마다 별도 queue를 사용했습니다.

앞선 patch format 12는 `DoPresent`의 마지막 Metal4 commit·signal·present까지 drawable residency를 등록하고 해당 등록만 제거합니다. 기존 D3DMetal 리소스 소유권과 queue의 기본 등록은 유지합니다. 고정된 바이너리의 실제 Wine 검사에서 단일 queue로 Present 수명 132회와 Present 없는 대조 4회를 수행하고 GPU 픽셀 136회를 확인했습니다. 등록을 제거한 뒤 실행된 실제 presentation callback 132회 모두 원래 layer·residency set과 살아 있는 drawable texture를 참조했습니다. queue 등록 누적을 고친 것이며 게임 FPS·메모리 개선을 측정한 것은 아닙니다. 다시 패치한 D3DMetal과 짝이 맞는 sidecar가 필요합니다. 과거 단일-queue 회귀 검사도 Metal API/GPU validation을 켠 상태로 크기 변경을 포함한 swapchain 수명 128회와 유효한 GPU 픽셀 readback 256회를 통과했습니다.

### 필수 안전 검사와 실게임 검증

현재 소스에는 프로젝트 전용 독립 검사를 두 종류만 남깁니다.

- 바이너리 패치 안전성: 부분 패치·변조·지원하지 않는 입력·중복 패치를 거부하고 무관한 바이트를 보존합니다.
- 설치 안전성: 사용자 파일을 보존하고 설치·활성화 실패 시 이전 런타임과 선택 상태를 복구합니다.

```bash
node --test scripts/metalir-fp64-codec-patch.test.mjs
# 런타임 아카이브를 포함한 빌드된 설치기와 명시적인 원본 리소스 fixture가 필요합니다.
python3 scripts/test-resource-lifecycle.py --stock-resource /path/to/stock/resources.neu
```

설치 검사는 임시 앱·지원 디렉터리 사본을 사용하며 설치된 게임 prefix를 대상으로 하지 않습니다. 빌드·staging·패키징의 바이너리 식별·layout·서명·ABI/export·소스/출처·아카이브 재조합 검증은 유지합니다. split 패키징의 격리 prefix Wine 초기화 확인도 유지합니다. 이는 패키징 안전 검사이지 그래픽 합격 판정이 아닙니다.

프로젝트 전용 NGX·FSR·MetalFX 렌더링·화질·캐시·커서·동기화·실행 설정 테스트와 테스트 전용 빌드 옵션은 제거했습니다. Wine 원본 테스트·CI와 `docs/evidence/`의 과거 기록은 변경하지 않습니다.

그래픽 변경의 합격 여부는 실게임에서 판단합니다. 격리된 후보로 같은 장면·설정의 프레임 시간 안정성·메모리·화질을 비교하고, FSR 및 FG OFF→ON→OFF, 커서·메뉴에서 카메라로 복귀, 전체화면·모니터 전환을 확인해야 합니다. 합성 테스트 PASS 로그, API 성공, 빌드 성공만으로 실게임 정상 동작이나 성능을 주장하지 않습니다.

## 라이선스

- Wine 소스 코드는 **GNU Lesser General Public License(LGPL v2.1+)**를 따릅니다.
- D3DMetal bridge 구성 요소와 설치 프로그램 도구에는 이 저장소에 포함된 조건이 적용됩니다.
- `d3dmetal-pso-cache/third-party/fidelityfx/`에 포함된 FidelityFX SDK header는 AMD의 MIT license 본문과 copyright 고지를 그대로 유지합니다.
