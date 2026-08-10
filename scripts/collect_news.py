"""
Collect negative news about マイナ保険証 from Google News RSS.
Updates data/articles.json and exits with code 1 if new articles were added
(so GitHub Actions can detect whether to send an email).
"""

import feedparser
import json
import hashlib
import sys
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

JST = timezone(timedelta(hours=9))

SEARCH_QUERIES = [
    "マイナ保険証 ミス OR 不具合 OR エラー OR 誤り",
    "マイナ保険証 漏洩 OR 漏えい OR 情報流出",
    "オンライン資格確認 障害 OR 停止 OR トラブル",
    "健康保険証 自治体 ミス OR 誤送付 OR 誤交付",
    "後期高齢者医療 保険証 ミス OR 不具合",
    "マイナ保険証 保険者 エラー OR ミス",
    "広域連合 マイナ保険証 問題 OR トラブル",
]

NEGATIVE_KEYWORDS = [
    "ミス", "エラー", "不具合", "障害", "誤り", "問題", "トラブル",
    "停止", "漏洩", "漏えい", "間違い", "混乱", "誤送付", "誤交付",
    "別人", "不正", "混同", "誤通知", "失念", "紛失",
]

URGENT_KEYWORDS = ["別人", "漏洩", "漏えい", "情報流出", "不正", "誤交付", "混同", "個人情報"]
CAUTION_KEYWORDS = ["停止", "障害", "ミス", "不具合", "エラー", "誤通知", "誤送付"]

ORG_PATTERNS = {
    "自治体": re.compile(r"[都道府県]|[市区町村]|自治体"),
    "保険者": re.compile(r"健康保険組合|健保|協会けんぽ|国民健康保険"),
    "広域連合": re.compile(r"広域連合|後期高齢者"),
    "厚労省": re.compile(r"厚生労働省|厚労省|デジタル庁"),
}

EXCLUDE_KEYWORDS = ["促進", "普及", "推進", "メリット", "利便性", "使い方", "登録方法"]

DATA_FILE = Path(__file__).parent.parent / "data" / "articles.json"
FEED_URL = "https://news.google.com/rss/search?q={query}&hl=ja&gl=JP&ceid=JP:ja"


def article_id(title: str, date: str) -> str:
    return hashlib.md5(f"{title}{date}".encode()).hexdigest()[:12]


def classify_severity(title: str, summary: str) -> str:
    text = title + summary
    if any(kw in text for kw in URGENT_KEYWORDS):
        return "urgent"
    if any(kw in text for kw in CAUTION_KEYWORDS):
        return "caution"
    return "info"


def classify_org(title: str, summary: str) -> str:
    text = title + summary
    for org, pattern in ORG_PATTERNS.items():
        if pattern.search(text):
            return org
    return "報道"


def extract_location(title: str) -> str:
    m = re.search(r"([^\s　「」【】（）()]+[都道府県](?:[^\s　「」【】（）()]+[市区町村])?)", title)
    return m.group(1) if m else ""


def is_negative(title: str, summary: str) -> bool:
    text = title + summary
    if any(kw in text for kw in EXCLUDE_KEYWORDS):
        return False
    return any(kw in text for kw in NEGATIVE_KEYWORDS)


def fetch_articles() -> list[dict]:
    results = []
    seen_titles: set[str] = set()

    for query in SEARCH_QUERIES:
        url = FEED_URL.format(query=quote(query))
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            link = entry.get("link", "")

            # deduplicate within this run
            if title in seen_titles:
                continue
            seen_titles.add(title)

            if not is_negative(title, summary):
                continue

            pub = entry.get("published_parsed")
            if pub:
                dt = datetime(*pub[:6], tzinfo=timezone.utc).astimezone(JST)
                date_str = dt.strftime("%Y-%m-%d")
                published_at = dt.isoformat()
            else:
                dt = datetime.now(JST)
                date_str = dt.strftime("%Y-%m-%d")
                published_at = dt.isoformat()

            # skip articles older than 90 days
            if (datetime.now(JST) - dt).days > 90:
                continue

            results.append({
                "id": article_id(title, date_str),
                "title": title,
                "date": date_str,
                "source": feed.feed.get("title", "Google News"),
                "url": link,
                "severity": classify_severity(title, summary),
                "org_type": classify_org(title, summary),
                "location": extract_location(title),
                "summary": summary[:200].strip() if summary else "",
                "published_at": published_at,
            })

    return results


def main() -> int:
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    else:
        data = {"articles": [], "last_updated": ""}

    existing_ids = {a["id"] for a in data["articles"]}
    existing_titles = {a["title"] for a in data["articles"]}

    fetched = fetch_articles()
    new_articles = [
        a for a in fetched
        if a["id"] not in existing_ids and a["title"] not in existing_titles
    ]

    if not new_articles:
        print("新着なし")
        return 0

    print(f"新着 {len(new_articles)} 件:")
    for a in new_articles:
        print(f"  [{a['severity']}] {a['title']} ({a['date']})")

    data["articles"] = new_articles + data["articles"]
    data["last_updated"] = datetime.now(JST).isoformat()

    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # write summary for email body
    summary_lines = [f"【マイナ保険証 インシデントウォッチ】新着アラート {len(new_articles)} 件\n"]
    for a in new_articles:
        label = {"urgent": "🔴 緊急", "caution": "🟡 注意", "info": "🔵 情報"}.get(a["severity"], "")
        summary_lines.append(f"{label} [{a['org_type']}] {a['title']}")
        summary_lines.append(f"   {a['date']} | {a['source']}")
        if a["url"]:
            summary_lines.append(f"   {a['url']}")
        summary_lines.append("")

    summary_path = Path(__file__).parent.parent / "new_articles_summary.txt"
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")

    return 1  # signal: new articles found


if __name__ == "__main__":
    sys.exit(main())
