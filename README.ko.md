# Wine 11.17 D3DMetal (GPTK 4.0b2, experimental)

[English](README.md)

GPTK 4.0b2 D3DMetal과 MetalFX를 사용하는 Yaagl용 Wine 11.17 런타임 소스입니다.
Apple Silicon·macOS 26 이상·Rosetta 2가 필요합니다. 독립 설치기와 Apple framework는 포함하지 않습니다.

## D3DMetal 오토패치

네이티브 실행 파일과 동봉 sidecar를 사용하므로 설치 시 Node.js·Python·Xcode 도구가 필요하지 않습니다.
Yaagl은 UI에서 Apple 라이선스 동의를 받은 뒤에만 패처에 `--accept-apple-license`를 전달해야 합니다.
패처는 다운로드·패치·서명의 고정 식별자를 검증한 뒤 준비된 framework를 출력합니다.

## 오토패치 번들 빌드

제작 환경에는 Node.js와 Apple 도구 체인이 필요합니다.

```sh
node scripts/build-d3dmetal-autopatch.mjs build/d3dmetal-autopatch
```

산출물: `build/d3dmetal-autopatch/`, `build/d3dmetal-autopatch.tar.gz`.

## Wine 런타임 패키징

macOS 26 빌드 환경에서 Wine/P3 의존성과 검증된 `f163d14` Wine 트리를 준비한 뒤:

```sh
scripts/build-yaagl-overlay.sh BASE_WINE_ROOT build/yaagl-overlay
python3 scripts/package-yaagl-runtime.py BASE_WINE_ROOT build/yaagl-overlay build/d3dmetal-autopatch build/yaagl-release
```

`wine/` 아카이브에는 네이티브 패처와 GPTK 모듈을 포함하지만 Apple `D3DMetal.framework`만 제외합니다. Wine을 로드하기 전에 `wine/lib/external/`에 framework를 준비해야 합니다. `wine/yaagl-d3dmetal-runtime.json`이 패키지 파일과 심볼릭 링크를 기록합니다.

## 라이선스

[COPYING.LIB](COPYING.LIB), [NOTICES.md](NOTICES.md)를 참고하세요. Apple GPTK에는 별도 라이선스가 적용됩니다.
