# tools/studio

`yunalee.shop` 스튜디오(`/home/ben/zit-22-2716`)에 붙인 기능들의 소스 사본과 테스트입니다.
서버에 올릴 때는 `server/*.py`는 앱 폴더로, `static/*`는 `studio_static/`로 복사합니다.

## 서버 반영 상태 (2026-10-09 기준)

| 파일 | 상태 |
|---|---|
| `server/scail_repair.py`, `static/scail-repair.*` | 반영됨 — 구간 수정(옵션·참고 이미지·구간 연장·저장된 수정본 내보내기) |
| `server/scail_live.py`, `static/scail-live.*` | 반영됨 — 생성 중인 구간 미리보기, 중단/이어서 생성 |
| `static/scail-entry.js` | 반영됨(v4) — 메뉴·카드 링크. 도우미 메뉴 항목이 들어간 v5는 **미반영** |
| `server/youtube_api.py` | 반영됨 — `invalid_grant`를 재연결 필요로 처리, 텔레그램 1회 알림, 재연결 후 자동 재개 |
| `server/ops_assistant.py`, `server/ops_restart_guard.py`, `static/ops-assistant.*` | **미반영** — 자동 안전장치가 배포를 막음 |
| `patches/studio_app.ops.patch` | **미반영** — 도우미를 앱에 연결하는 변경 |

## Gemma 정비 도우미를 올리려면

1. `server/ops_assistant.py`, `server/ops_restart_guard.py`를 앱 폴더에, `static/ops-assistant.html`, `static/ops-assistant.js`, `static/scail-entry.js`를 `studio_static/`에 복사
2. `cd <앱 폴더> && patch -p0 studio_app.py < patches/studio_app.ops.patch` (경로가 안 맞으면 파일명을 `studio_app.py`로 맞춰서)
3. `studio_static/index.html`의 `scail-entry.js?v=4`를 `?v=5`로 바꿔 캐시 갱신
4. `python -m py_compile studio_app.py ops_assistant.py ops_restart_guard.py`
5. `systemctl --user restart zstudio.service`

### 설계 (안전장치)
- 외부 AI 없음. 로컬 Gemma(`gemma4:26b-a4b-it-qat`)만 사용. GPU가 하나라 큐에 우선순위로 올려 순서대로 실행(컨텍스트 8192 제한 유지).
- 고칠 수 있는 파일은 `ops_assistant.TARGETS`의 8개뿐. 인증·토큰·큐 코어·`studio_app.py`·도우미 자신은 불가.
- **자동 적용 없음.** 수정안(diff)을 보고 사람이 눌러야 적용. 적용 전 백업, 언제든 되돌리기.
- 적용 전 검사: Python 문법·새 미정의 이름, JS `node --check`, HTML은 인라인 스크립트/`on…=`/외부 주소 금지.
- 서버 코드 변경은 `ops_restart_guard.py`가 재시작 뒤 앱이 안 뜨면 직전 파일로 자동 롤백.
- 변경 요청 API는 `X-Ops-Assistant: 1` 헤더와 Origin 검사를 요구(다른 사이트의 폼 전송 차단).
- 한계: 실제 Gemma의 패치 품질은 아직 검증되지 않았음(테스트는 스크립트된 가짜 모델).

## 테스트
서버 폴더에서 실행합니다(`/home/ben/zit-22-2716`, anaconda python). 브라우저 테스트는 Playwright + Chromium 필요.

```bash
python -W ignore tests/test_ops.py server/scail_repair.py server/ops_assistant.py   # 112개
python tests/test_guard.py server/ops_restart_guard.py                              # 16개 (가짜 systemctl)
python tests/test_youtube.py server/youtube_api.py                                  # 23개
python tests/test_live.py server/scail_repair.py server/scail_live.py               # 41개 (실제 Queue 클래스)
python tests/test_repair_options.py server/scail_repair.py                          # 49개
```
`browser_*.py`는 `mock_*.py`를 먼저 띄운 뒤 실행합니다(사이트와 같은 CSP 적용).
