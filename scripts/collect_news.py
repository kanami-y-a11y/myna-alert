"""
Incident-aware news collector for マイナ保険証/健康保険証 negative events.

Flow:
  1. Search Google News RSS with comprehensive keyword queries
  2. For each relevant article, determine:
     a. Is it relevant at all?
     b. Does it match an existing incident (continuation)?
     c. What is the update type (NEW / UPDATE / RECOVERY / REFUND / CAUSE / etc.)?
  3. Update incidents.json accordingly
  4. Exit with code 1 if any changes were made (triggers email/commit in CI)
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
DATA_FILE = Path(__file__).parent.parent / "data" / "incidents.json"
FEED_BASE = "https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"

# ── Search queries ──────────────────────────────────────────────────────────
QUERIES = [
    "マイナ保険証 ミス OR 不具合 OR エラー OR 誤り OR 誤表示 OR 誤交付",
    "マイナ保険証 漏洩 OR 漏えい OR 情報流出 OR 不正",
    "オンライン資格確認 障害 OR 停止 OR エラー OR 不具合",
    "オンライン資格確認 誤表示 OR 誤り OR 別人",
    "健康保険証 自治体 OR 市 ミス OR 誤送付 OR 誤交付 OR トラブル",
    "後期高齢者医療 ミス OR 不具合 OR 誤り OR 停止 OR トラブル",
    "国民健康保険 ミス OR 誤り OR エラー OR 不具合 OR 誤送付",
    "資格確認書 誤交付 OR 誤送付 OR ミス OR 不具合",
    "負担割合 誤表示 OR 誤り OR ミス OR 誤登録",
    "限度額区分 誤表示 OR 誤り OR ミス OR 誤登録",
    "マイナ保険証 謝罪 OR お詫び OR 注意喚起 OR 再発防止",
    "マイナ保険証 復旧 OR 原因判明 OR 返金 OR 差額",
    "広域連合 マイナ保険証 OR 後期高齢者 問題 OR トラブル",
    "協会けんぽ マイナ保険証 OR オンライン資格確認 障害 OR 不具合",
    "資格無効 誤表示 OR 無効表示 マイナ保険証",
]

# ── Relevance detection ─────────────────────────────────────────────────────
RELEVANCE_REQUIRED = [
    "マイナ保険証", "マイナンバーカード", "健康保険証", "オンライン資格確認",
    "資格確認書", "後期高齢者医療", "国民健康保険", "国保", "負担割合",
    "限度額区分", "高額療養費", "協会けんぽ", "健康保険組合",
]
NEGATIVE_REQUIRED = [
    "ミス", "エラー", "不具合", "障害", "誤り", "問題", "トラブル", "停止",
    "漏洩", "漏えい", "間違い", "混乱", "誤送付", "誤交付", "誤表示",
    "別人", "不正", "混同", "誤通知", "誤登録", "紛失", "お詫び", "謝罪",
    "臨時停止", "利用停止", "窓口対応できない", "発行できない", "返金",
    "差額", "還付", "再発防止", "原因判明", "復旧", "注意喚起",
]
EXCLUDE = ["促進", "普及", "メリット", "使い方", "登録方法", "申請方法", "説明会", "特集"]

def is_relevant(title: str, body: str) -> bool:
    text = title + body
    if any(kw in text for kw in EXCLUDE):
        return False
    has_subject = any(kw in text for kw in RELEVANCE_REQUIRED)
    has_negative = any(kw in text for kw in NEGATIVE_REQUIRED)
    return has_subject and has_negative

# ── Update type detection ───────────────────────────────────────────────────
def detect_update_type(title: str, body: str) -> str:
    text = title + body
    if any(kw in text for kw in ["返金", "差額", "還付", "精算", "返還"]):
        return "REFUND"
    if any(kw in text for kw in ["全面復旧", "復旧しました", "復旧を確認", "修正完了", "正常に戻り"]):
        return "RECOVERY"
    if any(kw in text for kw in ["原因判明", "原因は", "原因が判明", "調査結果"]):
        return "CAUSE"
    if any(kw in text for kw in ["対象件数", "対象者数", "確定", "件が判明", "人が判明"]):
        return "COUNT"
    if any(kw in text for kw in ["訂正", "修正しました", "誤りがありました"]):
        return "CORRECTION"
    if any(kw in text for kw in ["注意喚起", "お知らせ", "ご注意", "再発防止"]):
        return "NOTICE"
    return "UPDATE"

# ── Status detection ────────────────────────────────────────────────────────
def detect_status(title: str, body: str) -> str:
    text = title + body
    if any(kw in text for kw in ["全面復旧", "復旧しました", "修正が完了"]):
        return "全面復旧"
    if any(kw in text for kw in ["復旧見込み", "復旧予定", "復旧を予定"]):
        return "復旧見込み"
    if any(kw in text for kw in ["一部復旧", "一部の機能が"]):
        return "一部復旧"
    if any(kw in text for kw in ["返金完了", "返金しました", "還付しました"]):
        return "返金完了"
    if any(kw in text for kw in ["返金", "差額返金", "還付"]):
        return "返金対応中"
    if any(kw in text for kw in ["再発防止策", "再発防止を"]):
        return "再発防止策公表"
    if any(kw in text for kw in ["原因判明", "原因は"]):
        return "原因判明"
    if any(kw in text for kw in ["対象件数確定", "対象者数が確定"]):
        return "対象件数確定"
    if any(kw in text for kw in ["停止中", "臨時停止中", "利用停止中"]):
        return "調査中"
    return "調査中"

# ── Severity detection ──────────────────────────────────────────────────────
def detect_severity(title: str, body: str) -> str:
    text = title + body
    if any(kw in text for kw in ["別人", "漏洩", "漏えい", "不正", "誤交付", "混同", "個人情報"]):
        return "urgent"
    return "caution"

# ── Organization extraction ─────────────────────────────────────────────────
ORG_PATTERNS = {
    "municipality": re.compile(r"([^\s　「」【】（）()、。]{2,8}(?:市|区|町|村))"),
    "prefecture_office": re.compile(r"([^\s　「」【】（）()、。]{2,5}(?:都|道|府|県))(?:庁|知事|担当|は)"),
    "insurer": re.compile(r"([^\s　「」【】（）()、。]{3,15}健康保険組合|協会けんぽ|共済組合|国民健康保険組合)"),
    "koken": re.compile(r"([^\s　「」【】（）()、。]{3,20}広域連合|国民健康保険団体連合会|国保連)"),
}
PREF_NAMES = [
    "北海道","青森県","岩手県","宮城県","秋田県","山形県","福島県",
    "茨城県","栃木県","群馬県","埼玉県","千葉県","東京都","神奈川県",
    "新潟県","富山県","石川県","福井県","山梨県","長野県","岐阜県",
    "静岡県","愛知県","三重県","滋賀県","京都府","大阪府","兵庫県",
    "奈良県","和歌山県","鳥取県","島根県","岡山県","広島県","山口県",
    "徳島県","香川県","愛媛県","高知県","福岡県","佐賀県","長崎県",
    "熊本県","大分県","宮崎県","鹿児島県","沖縄県",
]

def extract_org(title: str, body: str) -> tuple[str, str, str]:
    text = title + " " + body
    for org_type, pattern in ORG_PATTERNS.items():
        m = pattern.search(text)
        if m:
            org_name = m.group(1)
            pref = next((p for p in PREF_NAMES if p[:2] in text), "")
            return org_name, org_type, pref
    return "", "other", ""

# ── Category detection ──────────────────────────────────────────────────────
CAT_KEYWORDS = {
    "マイナ保険証": ["マイナ保険証", "マイナンバーカード 保険"],
    "国民健康保険": ["国民健康保険", "国保"],
    "後期高齢者医療": ["後期高齢者医療", "後期高齢"],
    "オンライン資格確認": ["オンライン資格確認", "資格確認端末"],
    "資格確認書": ["資格確認書"],
    "負担割合": ["負担割合", "一部負担"],
    "限度額区分": ["限度額区分", "限度額適用"],
    "誤表示": ["誤表示", "誤った表示", "別人"],
    "誤登録": ["誤登録", "誤入力", "誤記載"],
    "誤交付": ["誤交付", "誤って交付"],
    "誤送付": ["誤送付", "誤って送付", "誤送"],
    "システム障害": ["システム障害", "システム不具合"],
    "システム停止": ["システム停止", "システムを停止", "サービス停止"],
    "臨時停止": ["臨時停止", "一時停止"],
    "窓口業務影響": ["窓口", "発行できない", "窓口業務"],
    "返金": ["返金", "差額", "還付", "精算"],
    "データ連携不備": ["データ連携", "連携不備", "データの反映"],
}

def detect_categories(title: str, body: str) -> list[str]:
    text = title + body
    return [cat for cat, kws in CAT_KEYWORDS.items() if any(kw in text for kw in kws)]

# ── Incident matching ───────────────────────────────────────────────────────
KNOWN_ORGS: dict[str, str] = {}  # populated from incidents.json

def normalize_org(name: str) -> str:
    return re.sub(r"[市区町村都道府県]$", "", name)

def match_existing_incident(
    org_name: str,
    prefecture: str,
    article_date: str,
    incidents: list[dict],
) -> dict | None:
    if not org_name:
        return None
    norm_new = normalize_org(org_name)
    article_dt = datetime.fromisoformat(article_date) if article_date else datetime.now(JST)
    for inc in incidents:
        if not inc.get("orgName"):
            continue
        norm_existing = normalize_org(inc["orgName"])
        if norm_new in norm_existing or norm_existing in norm_new:
            # Check date proximity: within 90 days of first published
            fp = inc.get("firstPublishedAt") or inc.get("occurredAt")
            if fp:
                try:
                    fp_dt = datetime.fromisoformat(fp).replace(tzinfo=JST)
                    if abs((article_dt.replace(tzinfo=JST) - fp_dt).days) <= 90:
                        return inc
                except Exception:
                    return inc
            return inc
    return None

# ── Main ────────────────────────────────────────────────────────────────────
def now_jst() -> str:
    return datetime.now(JST).isoformat()

def article_hash(title: str) -> str:
    return hashlib.md5(title.encode()).hexdigest()[:10]

def fetch_all() -> list[dict]:
    seen_hashes: set[str] = set()
    results = []
    for query in QUERIES:
        url = FEED_BASE.format(q=quote(query))
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"  fetch error: {e}", file=sys.stderr)
            continue
        for entry in feed.entries:
            title = entry.get("title", "")
            body = entry.get("summary", "")
            link = entry.get("link", "")
            h = article_hash(title)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            if not is_relevant(title, body):
                continue
            pub = entry.get("published_parsed")
            if pub:
                dt = datetime(*pub[:6], tzinfo=timezone.utc).astimezone(JST)
            else:
                dt = datetime.now(JST)
            date_str = dt.strftime("%Y-%m-%d")
            if (datetime.now(JST) - dt).days > 90:
                continue
            org_name, org_type, pref = extract_org(title, body)
            results.append({
                "_hash": h,
                "title": title,
                "body": body,
                "url": link,
                "date": date_str,
                "source_pub": feed.feed.get("title", "Google News"),
                "org_name": org_name,
                "org_type": org_type,
                "prefecture": pref,
                "update_type": detect_update_type(title, body),
                "status": detect_status(title, body),
                "categories": detect_categories(title, body),
            })
    return results


def main() -> int:
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    else:
        data = {"meta": {}, "incidents": []}

    incidents: list[dict] = data.get("incidents", [])
    existing_hashes: set[str] = {
        s.get("_hash", "")
        for inc in incidents
        for u in inc.get("updates", [])
        for s in u.get("sources", [])
    }

    articles = fetch_all()
    new_count = 0
    update_count = 0
    notify_lines: list[str] = []

    for art in articles:
        if art["_hash"] in existing_hashes:
            continue

        matched = match_existing_incident(art["org_name"], art["prefecture"], art["date"], incidents)

        new_source = {
            "_hash": art["_hash"],
            "sourceType": art["org_type"],
            "publisher": art["source_pub"],
            "title": art["title"],
            "url": art["url"],
            "publishedAt": art["date"],
            "isPrimary": False,
            "reliabilityLevel": 3,
        }

        if matched:
            # Add as update to existing incident
            new_update = {
                "id": f"{matched['id']}-u{len(matched.get('updates', [])) + 1:03d}",
                "type": art["update_type"],
                "publishedAt": art["date"],
                "title": art["title"][:120],
                "summary": art["body"][:250].strip(),
                "diffFromPrevious": None,
                "statusAfterUpdate": art["status"],
                "newFacts": [],
                "sources": [new_source],
            }
            if "updates" not in matched:
                matched["updates"] = []
            matched["updates"].append(new_update)
            matched["lastUpdatedAt"] = art["date"]
            # Update status only if it looks like a progression
            PROGRESS_ORDER = [
                "調査中", "対応中", "修正済み", "復旧見込み", "一部復旧", "原因判明",
                "対象調査中", "対象件数確定", "返金対応中", "返金完了",
                "再発防止策公表", "全面復旧", "復旧", "終了"
            ]
            curr_idx = PROGRESS_ORDER.index(matched.get("status","調査中")) if matched.get("status") in PROGRESS_ORDER else 0
            new_idx = PROGRESS_ORDER.index(art["status"]) if art["status"] in PROGRESS_ORDER else 0
            if new_idx > curr_idx:
                matched["status"] = art["status"]
            update_count += 1
            label = {"RECOVERY":"🟢 復旧","REFUND":"💰 返金","CAUSE":"🔍 原因判明"}.get(art["update_type"], "🔵 続報")
            notify_lines.append(f"{label} [{art['update_type']}] {matched['orgName']} — {art['title'][:60]}")
        else:
            if not art["org_name"]:
                continue
            inc_id = f"{art['date'][:4]}-{art['prefecture'][:2]}-{art['org_name']}-{article_hash(art['title'])}"
            new_inc = {
                "id": inc_id,
                "title": art["title"][:150],
                "orgName": art["org_name"],
                "orgType": art["org_type"],
                "prefecture": art["prefecture"],
                "municipality": art["org_name"] if art["org_type"] == "municipality" else None,
                "insuranceTypes": [],
                "categories": art["categories"],
                "status": art["status"],
                "occurredAt": None,
                "firstPublishedAt": art["date"],
                "recoveredAt": None,
                "lastUpdatedAt": art["date"],
                "lastCheckedAt": art["date"],
                "summary": art["body"][:250].strip(),
                "currentSituation": art["body"][:250].strip(),
                "cause": None,
                "confirmedFacts": [],
                "uncertaintyNote": "自動収集記事のため、事実確認が必要です。",
                "potentialCount": None,
                "confirmedCount": None,
                "refund": {"exists": "返金" in art["body"] or "差額" in art["body"], "count": None, "amount": None, "status": None},
                "unresolvedItems": ["詳細確認", "原因特定", "影響件数確認"],
                "updates": [{
                    "id": f"{inc_id}-u001",
                    "type": "NEW",
                    "publishedAt": art["date"],
                    "title": art["title"][:120],
                    "summary": art["body"][:250].strip(),
                    "diffFromPrevious": None,
                    "statusAfterUpdate": art["status"],
                    "newFacts": [],
                    "sources": [new_source],
                }],
                "createdAt": now_jst(),
                "updatedAt": now_jst(),
            }
            incidents.insert(0, new_inc)
            new_count += 1
            notify_lines.append(f"🔴 NEW [{art['org_name']}] {art['title'][:60]}")

    if new_count == 0 and update_count == 0:
        print("新着・更新なし")
        return 0

    print(f"新規: {new_count}件 / 続報: {update_count}件")
    for line in notify_lines:
        print(f"  {line}")

    incidents.sort(key=lambda i: i.get("lastUpdatedAt",""), reverse=True)
    unresolved_statuses = {"発生","公表","調査中","対応中","修正済み","復旧見込み","一部復旧",
                           "原因判明","対象調査中","対象件数確定","返金対応中","続報待ち","再発防止策公表"}
    data["incidents"] = incidents
    data["meta"] = {
        "lastUpdated": now_jst(),
        "version": "2.0",
        "totalCount": len(incidents),
        "unresolvedCount": sum(1 for i in incidents if i.get("status") in unresolved_statuses),
    }
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = Path(__file__).parent.parent / "new_articles_summary.txt"
    lines = [f"【マイナ保険証インシデントウォッチ】新規{new_count}件 / 続報{update_count}件\n"]
    lines += notify_lines
    summary.write_text("\n".join(lines), encoding="utf-8")
    return 1


if __name__ == "__main__":
    sys.exit(main())
