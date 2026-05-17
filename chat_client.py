#!/usr/bin/env python3
"""
chat_client.py
--------------
ai_chat 모듈의 동작을 개별적으로 테스트하기 위한 인터랙티브 CLI 클라이언트입니다.

사용법:
    python3 chat_client.py --provider ollama --web-search
    python3 chat_client.py --provider gemini --model gemini-2.5-flash
"""

import argparse
import sys
import os

# 현재 디렉토리를 경로에 추가하여 ai_chat 모듈을 임포트 가능하게 함
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from ai_chat import create_ai, list_providers

def main():
    parser = argparse.ArgumentParser(description="AI Chat 모듈 테스트 클라이언트")
    parser.add_argument(
        "--provider", "-p",
        type=str,
        default="ollama",
        choices=list_providers(),
        help="사용할 AI 제공자 (기본값: ollama)"
    )
    parser.add_argument(
        "--model", "-m",
        type=str,
        default=None,
        help="사용할 모델명 (지정하지 않으면 제공자의 기본 모델 사용)"
    )
    parser.add_argument(
        "--web-search", "-w",
        action="store_true",
        help="실시간 웹 검색 활성화"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 API 호출 없이 응답 텍스트만 확인"
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default="config/api_keys.json",
        help="API 키 설정 파일 경로"
    )

    args = parser.parse_args()

    print("==================================================")
    print("🤖 AI Chat Client 초기화 중...")
    print(f"Provider:   {args.provider}")
    print(f"Model:      {args.model or '(기본값)'}")
    print(f"Web Search: {'활성화' if args.web_search else '비활성화'}")
    print("==================================================")

    try:
        # AI 객체 생성
        ai = create_ai(
            provider=args.provider,
            model=args.model,
            config_path=args.config,
            dry_run=args.dry_run,
            web_search=args.web_search,
            temperature=0.7 # 챗봇 환경이므로 창의성을 위해 온도를 살짝 높임
        )
    except Exception as e:
        print(f"❌ AI 객체 생성 실패: {e}")
        sys.exit(1)

    print("\n✅ AI 준비 완료! (종료하려면 'quit', 'exit' 또는 Ctrl+C 입력)")
    print("--------------------------------------------------")

    while True:
        try:
            user_input = input("\n👤 사용자: ").strip()
            if not user_input:
                continue
            
            if user_input.lower() in ["quit", "exit"]:
                print("👋 클라이언트를 종료합니다.")
                break
                
            print("🤖 AI 입력 처리 중...\n")
            
            # AI 호출
            response = ai.chat(user_input)
            
            # 응답 출력
            if response.success:
                print(response.to_text())
            else:
                print(f"❌ 오류 발생: {response.error}")
                
        except KeyboardInterrupt:
            print("\n👋 클라이언트를 종료합니다.")
            break
        except Exception as e:
            print(f"\n❌ 예기치 않은 오류: {e}")

if __name__ == "__main__":
    main()
