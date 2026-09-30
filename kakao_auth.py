"""카카오 로그인을 한 번 해서 토큰을 받고, 암호화해 kakao_token.enc로 저장한다.

사용법:
    KAKAO_REST_KEY=... [KAKAO_CLIENT_SECRET=...] .venv/bin/python kakao_auth.py --site-url URL [--repo 계정/저장소]

브라우저에서 카카오 로그인 + '카카오톡 메시지 전송' 동의를 하면 끝난다.
--repo를 주면 GitHub Secret(KAKAO_REST_KEY, KAKAO_CLIENT_SECRET, KAKAO_TOKEN_KEY)도 gh로 등록한다.
"""

import argparse
import os
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.fernet import Fernet

import kakao

PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/callback"


def wait_for_code() -> str:
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>완료했어요. 이 창은 닫아도 돼요.</h2>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", PORT), Handler)
    while "code" not in result and "error" not in result:
        server.handle_request()
    if "error" in result:
        sys.exit(f"로그인 실패: {result.get('error_description', result['error'])}")
    return result["code"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", help="GitHub Secret을 등록할 저장소 (예: kwakseobang/news-digest)")
    parser.add_argument("--site-url", required=True, help="브리핑이 공개되는 주소 (카카오 앱에 등록한 도메인)")
    args = parser.parse_args()
    if not os.environ.get("KAKAO_REST_KEY"):
        sys.exit("KAKAO_REST_KEY 환경 변수에 REST API 키를 넣어 주세요.")

    url = "https://kauth.kakao.com/oauth/authorize?" + urlencode({
        "client_id": os.environ["KAKAO_REST_KEY"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "talk_message",
    })
    print("브라우저에서 카카오 로그인 후 '카카오톡 메시지 전송'에 동의해 주세요.")
    webbrowser.open(url)
    token = kakao.exchange_code(wait_for_code(), REDIRECT_URI)

    key = os.environ.get("KAKAO_TOKEN_KEY") or Fernet.generate_key().decode()
    kakao.save_token(token, key)
    print(f"저장: {kakao.TOKEN_FILE}")

    kakao.send_text(kakao.access_token(key), "뉴스 브리핑 연결 완료! 내일 아침 9시에 만나요.",
                    args.site_url, "브리핑 페이지")
    print("카톡으로 테스트 메시지를 보냈어요.")

    if args.repo:
        secrets = {"KAKAO_REST_KEY": os.environ["KAKAO_REST_KEY"], "KAKAO_TOKEN_KEY": key}
        if s := os.environ.get("KAKAO_CLIENT_SECRET"):
            secrets["KAKAO_CLIENT_SECRET"] = s
        for name, value in secrets.items():
            subprocess.run(["gh", "secret", "set", name, "--repo", args.repo], input=value, text=True, check=True)
        print(f"GitHub Secret 등록 완료: {', '.join(secrets)}")
    else:
        print(f"GitHub Secret KAKAO_TOKEN_KEY에 넣을 값: {key}")


if __name__ == "__main__":
    main()
