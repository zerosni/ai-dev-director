# 로컬 LLM (RTX 5080) 구성

`provider: local` 로 HEAD 또는 REVIEWER 역할을 내 PC의 GPU에서 도는 모델에 맡깁니다. 종량제 API가 아니며 `127.0.0.1` 로만 통신합니다.

1. `./setup-local-llm.sh` (Ollama 설치 필요, 기본 모델 `qwen2.5-coder:14b`, 16GB VRAM에 적합)
2. `config.yaml` 에서 예: `reviewer.provider: local` (클라우드 CLI 한도 절약용 REVIEWER 추천)
3. BUILDER는 로컬 모델 품질 한계 때문에 클라우드 CLI(agy/codex/claude)를 권장.

주의: 로컬 모델 리뷰는 fail-closed(응답 깨지면 FAIL). `role_settings`(콘솔 역할 교환)는 3개 클라우드 공급자 전용이라 `local`은 config.yaml로만 지정합니다. 이 변경은 Linux 샌드박스에서 스텁 서버로만 검증했고 실제 GPU 모델로는 검증하지 않았습니다.
