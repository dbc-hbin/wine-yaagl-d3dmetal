# Yaagl Wine DX12 런타임

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

`MTL_HUD_ENABLED=1`이면 Metal HUD는 첫 보간 이후 MetalFX “Frame Interpolator” 항목을 계속 표시합니다. 보간 중인 MetalFX FG context가 모두 꺼지면 사이드카가 약 0.5초 뒤 이 항목을 지우고, 다음 보간 때 다시 표시됩니다.

## 라이선스

[COPYING.LIB](COPYING.LIB), [NOTICES.md](NOTICES.md)를 참고하세요. Apple GPTK에는 별도 라이선스가 적용됩니다.
