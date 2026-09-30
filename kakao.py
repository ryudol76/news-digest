"""카카오톡 '나에게 보내기'

토큰은 kakao_token.enc에 암호화해서 저장소에 둔다. 복호화 키(KAKAO_TOKEN_KEY)는 GitHub Secret에만 있다.
access token은 몇 시간, refresh token은 두 달짜리라서 매 실행마다 access token을 새로 받는다.
refresh token은 만료가 한 달 안쪽으로 남으면 카카오가 새로 내려주므로, 그때 파일을 갱신해 두면
매일 실행되는 동안에는 다시 로그인할 일이 없다.

사용법 (GitHub Actions에서):
    python kakao.py send --message-file message.json --site-url URL --send-at 09:00
    python kakao.py fail --url 실행_로그_URL

필요한 환경 변수:
    KAKAO_REST_KEY        카카오 앱의 REST API 키
    KAKAO_CLIENT_SECRET   (선택) 앱에서 Client Secret을 켰다면 그 값
    KAKAO_TOKEN_KEY       kakao_auth.py가 만들어 준 암호화 키
"""

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet

TOKEN_FILE = Path(__file__).resolve().parent / "kakao_token.enc"
TEXT_LIMIT = 200  # 텍스트 템플릿의 최대 글자 수


class KakaoError(Exception):
    pass


def post(url: str, data: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, urlencode(data).encode(), headers), timeout=15) as res:
            return json.loads(res.read())
    except HTTPError as e:
        raise KakaoError(f"{url} → {e.code} {e.read().decode('utf-8', 'ignore')}") from None


def client_params() -> dict:
    params = {"client_id": os.environ["KAKAO_REST_KEY"]}
    if secret := os.environ.get("KAKAO_CLIENT_SECRET"):
        params["client_secret"] = secret
    return params


def save_token(token: dict, key: str) -> None:
    TOKEN_FILE.write_bytes(Fernet(key).encrypt(json.dumps(token).encode()))


def load_token(key: str) -> dict:
    return json.loads(Fernet(key).decrypt(TOKEN_FILE.read_bytes()))


def exchange_code(code: str, redirect_uri: str) -> dict:
    """로그인 후 받은 인가 코드를 토큰으로 바꾼다. (kakao_auth.py에서 한 번만 쓴다)"""
    token = post("https://kauth.kakao.com/oauth/token", {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, **client_params(),
    })
    token["refresh_token_expires_at"] = int(time.time()) + token["refresh_token_expires_in"]
    return token


def access_token(key: str) -> str:
    """refresh token으로 access token을 새로 받는다. refresh token도 새로 오면 파일에 저장한다."""
    stored = load_token(key)
    res = post("https://kauth.kakao.com/oauth/token", {
        "grant_type": "refresh_token", "refresh_token": stored["refresh_token"], **client_params(),
    })
    if "refresh_token" in res:
        stored["refresh_token"] = res["refresh_token"]
        stored["refresh_token_expires_at"] = int(time.time()) + res["refresh_token_expires_in"]
        save_token(stored, key)
        print("카카오 refresh token을 갱신했어요.")
    return res["access_token"]


def send_text(token: str, text: str, url: str, button: str) -> None:
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 1] + "…"
    template = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": url, "mobile_web_url": url},
        "button_title": button,
    }
    res = post("https://kapi.kakao.com/v2/api/talk/memo/default/send",
               {"template_object": json.dumps(template, ensure_ascii=False)}, token)
    if res.get("result_code") != 0:
        raise KakaoError(f"전송 실패: {res}")


def wait_until(hhmm: str) -> None:
    """오늘 이 시각(현지 시간)까지 기다린다. 이미 지났으면 바로 돌아간다."""
    now = datetime.now().astimezone()
    h, m = map(int, hhmm.split(":"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target > now:
        print(f"{hhmm}까지 {int((target - now).total_seconds())}초 기다려요.")
        time.sleep((target - now).total_seconds())


def wait_for_page(url: str, timeout: int = 600) -> None:
    """GitHub Pages 배포가 끝나 링크가 열릴 때까지 기다린다. 끝내 안 열려도 전송은 한다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=10):
                return
        except (HTTPError, URLError):
            time.sleep(15)
    print(f"페이지가 아직 안 열려요: {url}")


def main() -> None:
    parser = argparse.ArgumentParser(description="카카오톡 나에게 보내기")
    sub = parser.add_subparsers(dest="cmd", required=True)
    send = sub.add_parser("send", help="브리핑 전송")
    send.add_argument("--message-file", required=True)
    send.add_argument("--site-url", required=True)
    send.add_argument("--send-at", help="이 시각(HH:MM)까지 기다렸다가 전송")
    fail = sub.add_parser("fail", help="실패 알림 전송")
    fail.add_argument("--url", required=True)
    args = parser.parse_args()

    key = os.environ["KAKAO_TOKEN_KEY"]
    if args.cmd == "fail":
        send_text(access_token(key), "⚠️ 오늘 뉴스 브리핑을 만들지 못했어요. 실행 로그를 확인해 주세요.", args.url, "로그 보기")
        return

    message = json.loads(Path(args.message_file).read_text())
    url = f"{args.site_url.rstrip('/')}/{message['page']}"
    wait_for_page(url)
    if args.send_at:
        wait_until(args.send_at)
    send_text(access_token(key), message["text"], url, "전체 브리핑 보기")
    print("카톡 전송 완료")


if __name__ == "__main__":
    main()
