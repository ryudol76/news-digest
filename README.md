# 아침 뉴스 브리핑

매일 아침 9시, 카카오톡 '나에게 보내기'로 오늘의 뉴스 브리핑이 와요.
언론사 RSS에서 최근 24시간 기사를 모아 Claude가 같은 사건끼리 묶고, 섹션별로 중요한 사건만 골라 요약해요.
전체 브리핑은 GitHub Pages(`docs/`)에 올라가고, 카톡 메시지의 버튼으로 열어요.

섹션: 종합(1면) · 정치 · 경제·금융 · 세계 · IT·테크 — `feeds.toml`에서 바꿔요.

## 동작 (GitHub Actions, `.github/workflows/briefing.yml`)
1. 매일 08:30 KST에 시작해서 브리핑 생성 (`news.py`)
2. `docs/`에 커밋 → GitHub Pages 배포
3. 페이지가 열리면 09:00까지 기다렸다가 카톡 전송 (`kakao.py send`)
4. 실패하면 카톡으로 실패 알림

## 로컬 실행
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python news.py              # 브리핑 생성 → HTML 열기
.venv/bin/python news.py --dry-run    # Claude 없이 섹션별 기사 수만
```

## 처음 설정
1. **카카오 앱** ([developers.kakao.com](https://developers.kakao.com) → 내 애플리케이션 → 애플리케이션 추가)
   - 앱 키의 **REST API 키** 복사
   - 카카오 로그인 → 활성화 ON, Redirect URI에 `http://localhost:8765/callback` 등록
   - 카카오 로그인 → 동의항목 → **카카오톡 메시지 전송** 사용 설정
   - 플랫폼 → Web → 사이트 도메인에 `https://<GitHub 계정>.github.io` 등록 (카톡 버튼 링크용)
2. **카카오 토큰** (브라우저에서 로그인 한 번):
   ```bash
   KAKAO_REST_KEY=<REST API 키> .venv/bin/python kakao_auth.py \
     --site-url https://<계정>.github.io/news-digest --repo <계정>/news-digest
   git add kakao_token.enc && git commit -m "카카오 토큰" && git push
   ```
3. **Claude 토큰**: `claude setup-token` → 나온 값을 `gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo <계정>/news-digest`로 등록
   (API 키를 쓰려면 대신 `ANTHROPIC_API_KEY` Secret을 등록)
4. Actions 탭 → 아침 뉴스 브리핑 → Run workflow로 테스트

## 참고
- 카카오 refresh token은 두 달짜리지만, 매일 실행되면서 자동으로 갱신돼 `kakao_token.enc`(암호화됨)에 저장돼요. 두 달 넘게 멈춰 있었다면 2번을 다시 하면 돼요.
- 브리핑 페이지는 공개 저장소의 Pages라 누구나 볼 수 있어요 (검색엔진 색인은 막아둠).
