# 🤖 Gerrit AI 자동 코드 리뷰어

Gerrit에 커밋이 Push되면 AI가 자동으로 코드를 리뷰하고,  
결과를 Gerrit Change에 파일별 인라인 코멘트로 등록하는 시스템입니다.

---

## 📁 폴더 구조

```
gerrit-ai-reviewer/
│
├── ai_chat/                        ← AI 채팅 라이브러리 (재사용 모듈)
│   ├── __init__.py
│   ├── ai_factory.py               ← AI 인스턴스 팩토리
│   ├── base_ai.py                  ← 추상 베이스 클래스
│   ├── claude_ai.py                ← Anthropic Claude 구현
│   ├── gemini_ai.py                ← Google Gemini 구현
│   └── openai_ai.py                ← OpenAI GPT 구현
│
├── config/
│   ├── api_keys.json               ★ AI API 키 (반드시 설정)
│   └── reviewer_config.json        ★ Gerrit/AI/리뷰 설정 (반드시 설정)
│
├── hooks/
│   └── patchset-created            ← Gerrit Hook (Shell)
│                                     push 이벤트 발생 시 자동 실행
│
├── scripts/
│   ├── __init__.py
│   ├── gerrit_client.py            ← Gerrit REST API 클라이언트
│   ├── gerrit_reviewer.py          ← 메인 단일 리뷰 스크립트 (CLI)
│   ├── batch_reviewer.py           ← 배치(일괄) 리뷰 스크립트 (CLI)
│   ├── review_formatter.py         ← 결과 포맷 저장 (txt/md/json/html)
│   └── test_connection.py          ← 연결 진단 도구
│
├── output/                         ← 리뷰 결과 파일 저장
├── logs/                           ← 실행 로그 파일 저장
│
├── run_review.sh                   ← Shell 래퍼 (경로 독립 실행)
├── install.sh                      ← 자동 설치 스크립트
├── requirements.txt                ← Python 의존성
├── .env.example                    ← 환경 변수 예시
├── review_list.txt.example         ← 배치 리뷰 목록 파일 예시
├── .gitignore
└── README.md
```

---

## 🔄 동작 흐름

```
[개발자] git push origin HEAD:refs/for/main
         │
         ▼
[Gerrit] patchset 등록
         │
         ▼
[Hook]  hooks/patchset-created 자동 실행 (Shell)
         │  ├── Patchset 종류 필터 (TRIVIAL_REBASE 등 건너뜀)
         │  └── 브랜치 필터 (REVIEWER_BRANCHES 설정 시)
         │
         ▼
[gerrit_reviewer.py] (Python)
  Step 1 ── Gerrit REST API → 변경사항 메타데이터 조회
  Step 2 ── Gerrit REST API → 파일별 Diff 조회
  Step 3 ── ai_chat → AI 인스턴스 초기화
  Step 4 ── AI API → 파일별 코드 리뷰 생성 (JSON 구조화 응답)
           └── AI API → 전체 요약 + Code-Review 점수 생성
  Step 5 ── output/ → txt / md / json / html 저장
  Step 6 ── Gerrit REST API → 전체 요약 코멘트 + 파일별 인라인 코멘트 등록
```

---

## ⚙️ 요구사항

| 항목 | 버전 / 조건 |
|------|------------|
| Python | 3.10 이상 |
| Gerrit | 2.15 이상 (REST API + Hook 지원) |
| AI API | Claude / Gemini / OpenAI 중 하나 이상 |

---

## 🏗️ 배포 구조 — 어디에 설치해야 하나?

**AI 리뷰어 본체**(`scripts/`, `config/` 등)와 **Gerrit Hook**(`hooks/patchset-created`)은  
반드시 같은 서버에 있을 필요가 없습니다. 환경에 따라 두 가지 방식 중 선택하세요.

---

### 방식 A: Gerrit 서버에 직접 설치 (소규모 / 단순 구성)

리뷰어 전체를 Gerrit 서버 안에 설치하는 방식입니다.  
Hook이 실행될 때 같은 서버에서 바로 리뷰어를 호출하므로 별도 네트워크 설정이 없습니다.

```
┌─────────────────────────────────────────┐
│             Gerrit 서버                  │
│                                         │
│  /var/gerrit/hooks/                     │
│    └── patchset-created   ← Hook        │
│                                         │
│  /opt/gerrit-ai-reviewer/               │
│    ├── scripts/                         │
│    ├── config/            ← 리뷰어 본체 │
│    └── ai_chat/                         │
└─────────────────────────────────────────┘
```

```bash
# Gerrit 서버에 접속해서 실행
scp gerrit-ai-reviewer.zip gerrit-server:/opt/
ssh gerrit-server

cd /opt && unzip gerrit-ai-reviewer.zip && cd gerrit-ai-reviewer
chmod +x install.sh run_review.sh
./install.sh --gerrit-site /var/gerrit   # Hook 자동 복사 포함
```

---

### 방식 B: 별도 서버에 설치 (권장 — 운영 환경)

리뷰어 본체는 별도 서버(또는 개발 PC)에 설치하고,  
Gerrit Hook은 SSH 등으로 리뷰어를 원격 호출합니다.  
Gerrit 서버에 직접 접근할 수 없거나, 서버 부하를 분리하고 싶을 때 적합합니다.

```
┌──────────────────┐   REST API    ┌────────────────────────────┐
│   Gerrit 서버    │ ←──────────── │    AI 리뷰어 서버           │
│                  │               │    (또는 개발 PC)           │
│  hooks/          │               │                            │
│  patchset-created│ ──SSH 호출──→ │  /opt/gerrit-ai-reviewer/  │
│                  │               │    scripts/                │
└──────────────────┘               │    config/                 │
                                   └────────────────────────────┘
```

```bash
# 1. 리뷰어 본체를 별도 서버(또는 개발 PC)에 설치
#    --gerrit-site 없이 실행 → Hook 수동 설치 안내만 출력
unzip gerrit-ai-reviewer.zip -d /opt/
cd /opt/gerrit-ai-reviewer
chmod +x install.sh run_review.sh
./install.sh          # --gerrit-site 옵션 생략

# 2. 설정 완료 후, Hook 파일 1개만 Gerrit 서버에 복사
scp hooks/patchset-created  gerrit-admin@gerrit-server:/var/gerrit/hooks/
ssh gerrit-admin@gerrit-server "chmod +x /var/gerrit/hooks/patchset-created"
```

방식 B에서 Hook이 리뷰어 서버를 호출하려면 `hooks/patchset-created` 하단의 실행 부분을 아래처럼 수정합니다.

```bash
# hooks/patchset-created 수정 예시 (원격 SSH 호출)
ssh reviewer-server \
  "cd /opt/gerrit-ai-reviewer && \
   python scripts/gerrit_reviewer.py \
     --change $CHANGE --patchset $PATCHSET" >> "$HOOK_LOG" 2>&1 &
```

---

### 방식 선택 기준

| 상황 | 권장 방식 | `--gerrit-site` |
|------|-----------|-----------------|
| 소규모 팀, 단순 구성 | A — Gerrit 서버 직접 설치 | `/var/gerrit` 경로 지정 |
| 운영 환경, 부하 분리 | B — 별도 서버 | 생략 |
| Gerrit 서버 SSH 접근 불가 | B — 별도 서버 | 생략 |
| 개발 PC에서 테스트 | B — 로컬 PC | 생략 |

> **`--gerrit-site`는 Hook 파일을 자동으로 복사해주는 편의 옵션일 뿐입니다.**  
> 핵심은 `hooks/patchset-created` 파일이 Gerrit 서버의  
> `<gerrit_site>/hooks/` 디렉토리 안에 있으면 됩니다.

---

## 🚀 설치 방법

### 1단계: 파일 배포 및 설치 스크립트 실행

**방식 A — Gerrit 서버 직접 설치:**
```bash
scp gerrit-ai-reviewer.zip gerrit-server:/opt/
ssh gerrit-server "cd /opt && unzip gerrit-ai-reviewer.zip"
ssh gerrit-server "cd /opt/gerrit-ai-reviewer && \
  chmod +x install.sh run_review.sh && \
  ./install.sh --gerrit-site /var/gerrit"
```

**방식 B — 별도 서버 / 개발 PC:**
```bash
unzip gerrit-ai-reviewer.zip -d /opt/
cd /opt/gerrit-ai-reviewer
chmod +x install.sh run_review.sh
./install.sh          # --gerrit-site 생략
```

---

### 2단계: AI API 키 설정

```bash
vi config/api_keys.json
```

```json
{
  "claude": { "api_key": "sk-ant-api03-XXXXXXXX" },
  "gemini": { "api_key": "AIzaXXXXXXXX" },
  "openai": { "api_key": "sk-XXXXXXXX" }
}
```

> 사용할 AI 제공자의 키만 입력하면 됩니다.

---

### 3단계: Gerrit 연결 설정

```bash
vi config/reviewer_config.json
```

```json
{
  "gerrit": {
    "url":      "https://gerrit.your-company.com",
    "username": "ai-reviewer",
    "password": "Gerrit-HTTP-Password"
  },
  "ai": { "provider": "claude" }
}
```

> **Gerrit HTTP Password 발급**: Gerrit 로그인 → Settings → HTTP credentials → Generate New Password  
> SSH 키와는 별도로 발급하는 비밀번호입니다.

---

### 4단계: Hook 설치

**방식 A:** `install.sh --gerrit-site` 실행 시 자동으로 설치됩니다.

**방식 B:** Hook 파일을 Gerrit 서버로 직접 복사합니다.

```bash
# 리뷰어 서버 → Gerrit 서버로 Hook 파일 복사
scp hooks/patchset-created  gerrit-admin@gerrit-server:/var/gerrit/hooks/
ssh gerrit-admin@gerrit-server "chmod +x /var/gerrit/hooks/patchset-created"

# 설치 확인 (-rwxr-xr-x 이어야 함)
ssh gerrit-admin@gerrit-server "ls -la /var/gerrit/hooks/patchset-created"
```

---

## 🧪 단계별 디버깅 가이드

실제 Gerrit에 등록하기 전 **3단계 검증**을 권장합니다.

### 0단계: 연결 진단

```bash
./run_review.sh --test                 # 전체 진단 (Python 환경, Gerrit, AI)
./run_review.sh --test --change 12345  # 특정 Change 접근 포함
```

### 1단계: DRY-RUN (가장 안전 — API 호출 없음)

AI API와 Gerrit 모두 실제 호출하지 않고 전체 흐름과 설정만 검증합니다.

```bash
./run_review.sh --change 12345 --patchset 1 --dry-run --verbose
```

### 2단계: NO-POST (AI 리뷰 확인, Gerrit 미등록)

AI 리뷰는 실제로 수행하지만 Gerrit에는 등록하지 않습니다.  
`output/` 디렉토리의 HTML 파일로 리뷰 품질을 먼저 확인하세요.

```bash
./run_review.sh --change 12345 --patchset 1 --no-post

# 결과 파일 확인
open output/review_c12345_p1_*.html        # macOS
xdg-open output/review_c12345_p1_*.html   # Linux
```

### 3단계: 실제 등록

```bash
./run_review.sh --change 12345 --patchset 1
```

---

## 📦 배치 처리

여러 Change를 한 번에 리뷰합니다.

```bash
# Change 번호 직접 지정
./run_review.sh --batch --changes 100 101 102 --no-post

# 파일에서 읽기
./run_review.sh --batch --file review_list.txt

# Gerrit 쿼리 기반
./run_review.sh --batch --query "status:open project:my-project branch:main"

# 병렬 처리 (API 속도 제한 주의)
./run_review.sh --batch --changes 100 101 102 --workers 2
```

---

## 📂 출력 파일

리뷰 실행마다 `output/` 에 4가지 포맷이 생성됩니다.

| 파일 | 설명 |
|------|------|
| `review_c12345_p1_TIMESTAMP.txt`  | 텍스트 리포트 (인라인 코멘트 포함) |
| `review_c12345_p1_TIMESTAMP.md`   | 마크다운 리포트 (인라인 코멘트 테이블 포함) |
| `review_c12345_p1_TIMESTAMP.json` | 구조화 JSON (파이프라인 연동용) |
| `review_c12345_p1_TIMESTAMP.html` | 브라우저 HTML 리포트 (인라인 코멘트 색상 표시) |

배치 실행 시 추가 생성:
- `batch_summary_TIMESTAMP.json`
- `batch_summary_TIMESTAMP.txt`

---

## 🌍 환경 변수

Hook 또는 CI/CD에서 설정 파일을 오버라이드할 수 있습니다.

```bash
cp .env.example .env
vi .env
source .env
```

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `GERRIT_URL` / `GERRIT_USER` / `GERRIT_PASSWORD` | Gerrit 연결 정보 | config 파일 |
| `AI_PROVIDER` / `AI_MODEL` | AI 제공자·모델 | config 파일 |
| `REVIEWER_DRY_RUN` | DRY-RUN 모드 | `false` |
| `REVIEWER_NO_POST` | NO-POST 모드 | `false` |
| `REVIEWER_VERBOSE` | 상세 로그 출력 | `false` |
| `REVIEWER_BRANCHES` | 대상 브랜치 필터 (공백 구분) | 전체 |
| `REVIEWER_SKIP_KINDS` | 건너뛸 Patchset 종류 | `TRIVIAL_REBASE NO_CODE_CHANGE` |
| `REVIEWER_PYTHON` | Python 경로 오버라이드 | 자동 탐색 |

---

## 📝 프롬프트 커스터마이징

`config/reviewer_config.json`의 `prompt` 섹션에서 조정합니다.

```json
{
  "prompt": {
    "language": "English",
    "focus_areas": ["Security", "Performance", "Test Coverage"],
    "system": "You are a senior software engineer with 10+ years of experience."
  }
}
```

**치환 변수**: `{subject}` `{project}` `{branch}` `{diff}` `{focus_areas}` `{language}`

---

## 🔧 AI 제공자 변경

`config/reviewer_config.json`의 `ai.provider`를 변경합니다.

```json
{
  "ai": {
    "provider": "gemini",
    "model": "gemini-2.0-flash"
  }
}
```

| 제공자 | 기본 모델 |
|--------|----------|
| `claude` | claude-opus-4-6 |
| `gemini` | gemini-1.5-pro |
| `openai` | gpt-4o |

---

## ❓ 문제 해결

| 문제 | 원인 | 해결 방법 |
|------|------|----------|
| 401 인증 실패 | HTTP Password 오류 | Settings → HTTP credentials → Generate New Password (SSH 키와 다름) |
| Hook 미실행 | 실행 권한 없음 | `chmod +x /var/gerrit/hooks/patchset-created` |
| Hook 실행 후 리뷰 없음 | 방식 B 원격 호출 실패 | `logs/hook_날짜.log` 확인, SSH 접속 키 설정 확인 |
| SSL 인증서 오류 | 자체 서명 인증서 | `"verify_ssl": false` 설정 (운영에서는 올바른 인증서 권장) |
| Python 버전 오류 | Python 3.10 미만 | `export REVIEWER_PYTHON=/usr/bin/python3.11` |
| AI API 오류 | 키 오류 또는 할당량 초과 | `./run_review.sh --test --ai-only` 로 진단 |
| 인라인 코멘트 미등록 | 라인 번호 범위 초과 | `logs/reviewer_*.log` 에서 422 오류 확인 |
