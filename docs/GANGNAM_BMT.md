# 강남구(11680) 공간정보 BMT — ai-dev-director 통합 설계

> 검증 상태: `scripts/gangnam_bmt.py`는 문법 검사만 통과했고 **QGIS 환경에서 실행해 보지 못했습니다** (이 문서를 작성한 환경에 QGIS 없음).
> 아래 외부 서비스 파라미터(Overpass, VWorld, 타일 URL)는 작성 시점에 호출해 확인하지 않았습니다. 첫 실행 때 확인하세요.

## 0. 먼저 알아둘 불일치 3가지

| 요청 | 실제 저장소 동작 | 대응 |
|---|---|---|
| REVIEWER=Antigravity가 QGIS에서 코드를 **직접 실행** | 코드 실행·테스트는 **Director의 Verifier**가 격리 worktree에서 수행. Antigravity 리뷰어는 diff와 검증 증거만 읽고 "실행했다고 주장하지 않음" | QGIS 실행은 Verifier의 `test_command`로 걸고, Antigravity는 그 JSON 증거를 리뷰 |
| COLLECTOR에 **Gemini API** | 저장소 규칙: 종량제 API 금지, `*_API_KEY`는 환경에서 제거됨 | Gemini API 대신 구독 로그인 CLI(`agy` 또는 Gemini CLI) 또는 웹 수집 사용. API 키 방식은 불가 |
| 5단계(Collector→…→Reviewer) | 저장소 파이프라인은 HEAD→BUILDER→Verifier→REVIEWER→HEAD 결정 | Collector/Scheduler는 저장소 밖 보조 단계로 둠(아래 §1) |

또한 이 PC에는 현재 `claude`, `codex`만 확인됐고 `agy`/`gemini`는 없습니다. REVIEWER는 `agy`가 설치되기 전까지 `claude` 또는 `local`로 대체해야 합니다.

## 1. 단계 매핑

```
Collector ─▶ Picker(HEAD) ─▶ Creator(BUILDER) ─▶ [Verifier: QGIS 실행] ─▶ Reviewer ─▶ HEAD 결정
 (보조)        ORDER 발행       파일 JSON 제안        benchmark_report.json     리뷰 JSON     PASS/FAIL
                                      ▲                                                   │
                                      └────────── Self-Healing (FAIL이면 피드백으로 재시도) ◀┘
Scheduler(OS 작업) ── 야간: 데이터 동기화 → `director.py run` → 리포트 보관
```

| 단계 | 담당 | 입력 → 출력 |
|---|---|---|
| Collector | 보조 스크립트(수동/웹, Gemini는 CLI만) | 동향·데이터 URL → `data/collector/gangnam_findings.json` |
| Picker | HEAD(claude) | findings → ORDER(`schemas/order.py`) |
| Creator | BUILDER(codex) | ORDER → 파일 전체 JSON 제안(Scope Guard 통과 후 worktree 반영) |
| Verifier | Director(코드) | worktree에서 `test_command` 실행 → 종료코드+`benchmark_report.json` |
| Reviewer | REVIEWER | diff+증거 → `ReviewResultSchema` |
| Scheduler | OS 작업 스케줄러/cron | 주기 실행 |

### JSON 인터페이스

Collector 출력 (`gangnam_findings.json`):
```json
{"region":"11680","collected_at":"2026-11-11T00:00:00Z",
 "sources":[{"name":"VWorld WFS","kind":"WFS","url":"https://...","license":"공공누리","verified":false}],
 "candidates":[{"id":"C1","title":"R-Tree 선필터 후 정밀 교차","metric":"join_ms","why":"건물 3~4만 건 규모","evidence_url":"https://..."}]}
```

ORDER(HEAD→BUILDER, 저장소 스키마 그대로):
```json
{"goal":"강남구 합성 데이터 BMT 스크립트를 개선하고 benchmark_report.json 통과","allowed_paths":["scripts/gangnam_bmt.py","tests/test_gangnam_bmt.py"],
 "protected_paths":["config.yaml","core/"],"acceptance_criteria":["benchmark_report.json passed=true","crs_roundtrip max_mm<=1.0","index speedup>=10x"],
 "required_outputs":["scripts/gangnam_bmt.py"],"max_files_changed":4,"max_diff_lines":600,"risk_level":"LOW","requires_human_approval":false}
```

벤치 리포트(Verifier 증거): `scripts/gangnam_bmt.py`의 `benchmark_report.json` (`checks`, `passed` 포함).

리뷰 결과(저장소 스키마): `{"verdict":"pass|changes_required","p0":[],"p1":[],"p2":[],"summary":""}`

### Self-Healing 규칙
1. Verifier 종료코드≠0 또는 `passed=false` → HEAD가 `checks`의 실패 항목을 ORDER 피드백으로 변환 → BUILDER 재시도(`autonomy.builder_attempts: 3`, 마지막 시도는 공급자 교체).
2. 리뷰 p0/p1 존재 → HEAD가 수정 지시(`max_repairs: 3`).
3. 같은 실패 3회 연속(`max_consecutive_failures: 3`) → 정지하고 사람에게 알림.
4. QGIS 미설치(종료코드 2)는 코드 결함이 아니므로 재시도하지 않고 환경 오류로 정지.

## 2. 설정 예시 (`config.yaml`에 병합)

```yaml
head:     {provider: claude}
builder:  {mode: proposal, provider: codex}
reviewer: {provider: claude}        # agy 설치 후 antigravity, 또는 local(Ollama)
safety:
  approved_commands: [python, pytest, "C:/Program Files/QGIS 3.34.0/bin/python-qgis.bat"]   # 실제 경로로 수정
projects:
  "C:/Users/user/gangnam-gis":      # git 저장소 절대경로
    verification:
      test_command: '"C:/Program Files/QGIS 3.34.0/bin/python-qgis.bat" scripts/gangnam_bmt.py --out benchmark_report.json'
autonomy: {max_repairs: 3, builder_attempts: 3, max_consecutive_failures: 3}
```
야간 배치(Windows 작업 스케줄러): `python director.py run -p C:/Users/user/gangnam-gis -g "BMT 회귀 점검"` 을 매일 02:00에 등록.

## 3. 강남구 데이터 취득

**BBOX**: 경도 127.01~127.14, 위도 37.46~37.54 (EPSG:4326).

**Overpass QL** (건물+도로 1회 쿼리, QuickOSM의 "Raw query"에 붙여넣기):
```
[out:json][timeout:180];
area["name:ko"="강남구"]["boundary"="administrative"]["admin_level"="6"]->.a;
(
  way["building"](area.a);
  relation["building"](area.a);
  way["highway"](area.a);
);
out body geom;
```
행정구역 area 조회가 실패하면 `(37.46,127.01,37.54,127.14)` BBOX 필터로 대체하세요.

**VWorld 연속지적도/용도지역** (인증키 필요, 무료 발급, 종량제 아님):
```
https://api.vworld.kr/req/data?service=data&request=GetFeature&data=LP_PA_CBND_BUBUN
  &key={VWORLD_KEY}&domain={등록도메인}&geomFilter=BOX(127.01,37.46,127.14,37.54)
  &attrFilter=pnu:like:11680&crs=EPSG:4326&format=json&size=1000&page=1
```
연속지적도는 PNU 앞 5자리가 시군구코드(`11680`)입니다. 용도지역지구는 `data=LT_C_UQ111`처럼 데이터셋 코드만 바꿉니다(코드는 VWorld 데이터 목록에서 확인). 키는 코드/설정에 커밋하지 말고 환경 변수로만 두세요.

**래스터 타일 (XYZ)**:
- VWorld 위성: `https://api.vworld.kr/req/wmts/1.0.0/{KEY}/Satellite/{z}/{y}/{x}.jpeg` (※ 행/열 순서 `{z}/{y}/{x}`)
- Google 위성: `https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}` — **약관상 QGIS 등 직접 타일 사용이 허용되지 않을 수 있으므로 상업·자동화 사용 전 확인 필요**
- QGIS 데이터 소스 관리자 > XYZ 연결에 위 URL을 넣고, 렌더링 범위는 BBOX로 제한합니다.

## 4. BMT 매트릭스 (강남구 건물 약 3~4만 건 기준)

| 영역 | 지표 | 측정 방법 | 합격선(초안) | 스크립트 측정 |
|---|---|---|---|---|
| 플랫폼 품질 | 5179↔4326 왕복 오차(mm) | 무작위 2,000점 왕복 | max ≤ 1.0 mm | ✅ 합성 |
| | Bessel(5174)↔5179 변환 오차 | 국가기준점 실측 필요 | 협의 | ❌ 기준점·격자 필요 |
| | 위상 결함율(무효/중복/슬리버) | `isGeosValid`, WKB 해시, 얇기지수 | 실데이터 < 0.5% | ✅ (결함 주입으로 탐지 검증) |
| 분석 성능 | 점-건물 교차 시간 (인덱스 无/有) | 무인덱스는 300점 표본 후 외삽 | 가속 ≥ 10× | ✅ |
| | R-Tree 생성 시간 | `QgsSpatialIndex` 적재 | 기록 | ✅ |
| | 도로 버퍼 15 m × 건물 조인 | 인덱스 경유 | 기록 | ✅ |
| | 메모리 상주량(RSS) | psutil | 증가량 기록 | ✅ (psutil 있을 때) |
| 고해상도 활용 | 타일 렌더링 지연 | 네트워크 필요 | 협의 | ❌ 미측정 |
| | Zonal Statistics 처리율 | 래스터(DEM/정사영상) 필요 | 협의 | ❌ 미측정 |

합성 데이터 결과는 **알고리즘 회귀용**이며 실제 강남구 데이터의 절대 수치가 아닙니다. 실데이터 단계에서는 §3의 건물 레이어를 `QgsVectorLayer`로 불러 `buildings` 목록만 바꾸면 같은 지표를 얻습니다.

## 5. 실행

```
# OSGeo4W Shell 또는 QGIS 번들 파이썬
python scripts/gangnam_bmt.py --buildings 10000 --points 5000 --seed 42 --out benchmark_report.json
```
종료코드: 0 통과 / 1 지표 미달 / 2 QGIS 없음. 출력은 `benchmark_report.json`(콘솔에도 동일 출력).
