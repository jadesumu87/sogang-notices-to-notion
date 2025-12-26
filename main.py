# main.py
import os

def main():
    notion_token = os.environ["NOTION_TOKEN"]
    notion_db_id = os.environ["NOTION_DB_ID"]
    print("Loaded env OK")
    # TODO: 크롤링 -> 노션 DB 업서트(중복 방지) 로직

if __name__ == "__main__":
    main()
