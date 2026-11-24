#!/usr/bin/env python3
"""気象庁の注意報・警報(大雨・洪水)を見て、奈良盆地(大和川流域)に出たら ntfy で
「水位を確認してください」と通知する。水位そのものは見ない「備えの合図」。

環境変数
  NTFY_TOPIC   (必須) ntfy のトピック名
  AREAS        対象の市町村(カンマ区切り)。省略時は DEFAULT_AREAS
  STATE_FILE   既定 data/state.json
  FAIL_ALERT_COUNT  連続失敗の警告を出す回数(既定 3)
"""
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

JST = timezone(timedelta(hours=9))
FEED_SHORT = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"
FEED_LONG = "https://www.data.jma.go.jp/developer/xml/feed/extra_l.xml"
OFFICE = "奈良地方気象台"
HEADERS = {"User-Agent": "river-level-notifier/2.0 (automated; low frequency)"}

# 大和川の水位に影響する奈良盆地の市町村(奈良県内)
DEFAULT_AREAS = (
    "奈良市,大和郡山市,天理市,橿原市,桜井市,生駒市,香芝市,葛城市,御所市,大和高田市,"
    "平群町,三郷町,斑鳩町,安堵町,川西町,三宅町,田原本町,王寺町,上牧町,広陵町,河合町,"
    "高取町,明日香村"
)
KEYWORDS = ("大雨", "洪水")  # この語を含む種類だけ見る(暴風・雷などは対象外)


def severity(kind):
    if "特別警報" in kind:
        return 3
    if "警報" in kind:
        return 2
    if "注意報" in kind:
        return 1
    return 0


def strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parse_feed(text):
    """[(id, updated, author, title)] を返す。"""
    root = strip_ns(ET.fromstring(text))
    out = []
    for e in root.findall("entry"):
        eid = (e.findtext("id") or "").strip()
        link = e.find("link")
        url = link.get("href") if link is not None and link.get("href") else eid
        out.append({
            "url": url or eid,
            "updated": (e.findtext("updated") or "").strip(),
            "author": (e.findtext("author/name") or "").strip(),
            "title": (e.findtext("title") or "").strip(),
        })
    return out


def parse_warning(text, areas):
    """VPWW54 を読み、{市町村: [種類...]} (その電文で内容が示された市町村のみ)を返す。
    発表・継続の種類だけが有効。「解除」「なし」は含めない(=空リストで上書き)。"""
    root = strip_ns(ET.fromstring(text))
    status = root.findtext("Control/Status") or ""
    if status and status != "通常":
        return {}
    result = {}
    for w in root.iter("Warning"):
        if "市町村" not in (w.get("type") or ""):
            continue
        for item in w.findall("Item"):
            name = (item.findtext("Area/Name") or "").strip()
            if name not in areas:
                continue
            active = result.setdefault(name, [])
            for k in item.findall("Kind"):
                kname = (k.findtext("Name") or "").strip()
                st = (k.findtext("Status") or "").strip()
                if not kname or st in ("解除", "発表警報・注意報はなし"):
                    continue
                if any(w_ in kname for w_ in KEYWORDS) and kname not in active:
                    active.append(kname)
    return result


def get(url, retries=3, wait=10):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception as e:  # noqa
            last = e
            if i < retries - 1:
                time.sleep(wait)
    raise RuntimeError(f"取得失敗: {url}: {last}")


def send_ntfy(topic, title, message, priority=3, tags=None):
    body = {"topic": topic, "title": title, "message": message, "priority": priority}
    if tags:
        body["tags"] = tags
    r = requests.post("https://ntfy.sh", json=body, headers=HEADERS, timeout=30)
    r.raise_for_status()


def build_message(new_items, active):
    lines = []
    for area, kinds in new_items:
        lines.append(f"{area}: {'、'.join(kinds)}")
    top = max((severity(k) for _, ks in new_items for k in ks), default=1)
    head = {3: "大雨特別警報", 2: "大雨・洪水の警報", 1: "大雨・洪水の注意報"}[top]
    msg = "\n".join(lines) + "\n\n水位を確認してください。\n(注意報・警報による備えの合図です)"
    prio = {3: 5, 2: 4, 1: 3}[top]
    tags = {3: ["rotating_light"], 2: ["warning"], 1: ["cloud_with_rain"]}[top]
    return f"【{head}】奈良県 大和川流域", msg, prio, tags


def run(state, topic, areas, now, fetch=get, notify=send_ntfy):
    """1回分の処理。state を更新して返す。通知に失敗したら例外。"""
    state.setdefault("active", {})      # {市町村: [種類]}
    state.setdefault("seen", [])        # 処理済みの電文URL
    state.setdefault("notified", {})    # {市町村: [通知済みの種類]}

    entries = parse_feed(fetch(FEED_SHORT))
    last_long = state.get("last_long")
    need_long = (not last_long) or (
        now - datetime.fromisoformat(last_long) > timedelta(minutes=55))
    if need_long:
        try:
            entries += parse_feed(fetch(FEED_LONG))
            state["last_long"] = now.isoformat()
        except Exception as e:  # 長期版が取れなくても短期版で続行
            print(f"WARN long feed: {e}", file=sys.stderr)

    seen = set(state["seen"])
    todo, uniq = [], set()
    for e in entries:
        if e["url"] in seen or e["url"] in uniq:
            continue
        if OFFICE not in e["author"] or "気象警報・注意報" not in e["title"]:
            continue
        uniq.add(e["url"])
        todo.append(e)
    todo.sort(key=lambda e: e["updated"])

    for e in todo:
        updates = parse_warning(fetch(e["url"]), areas)
        for area, kinds in updates.items():
            if kinds:
                state["active"][area] = kinds
            else:
                state["active"].pop(area, None)
                state["notified"].pop(area, None)
        state["seen"].append(e["url"])

    state["seen"] = state["seen"][-400:]

    # 新しく出た種類だけ通知(同じ注意報・警報は1回だけ)
    new_items = []
    for area, kinds in state["active"].items():
        done = set(state["notified"].get(area, []))
        fresh = [k for k in kinds if k not in done]
        if fresh:
            new_items.append((area, kinds))
    if new_items:
        title, msg, prio, tags = build_message(new_items, state["active"])
        notify(topic, title, msg, prio, tags)  # 失敗したら例外 → 通知済みにしない
        for area, kinds in new_items:
            state["notified"][area] = list(kinds)
        state["was_active"] = True
    elif state.get("was_active") and not state["active"]:
        notify(topic, "【解除】奈良県 大和川流域の大雨・洪水の注意報・警報",
               "対象地域の注意報・警報はすべて解除されました。\n水位の確認は引き続き行ってください。", 2, ["white_check_mark"])
        state["was_active"] = False
    state["last_checked"] = now.isoformat()
    return state


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC が未設定です", file=sys.stderr)
        sys.exit(2)
    areas = {a.strip() for a in os.environ.get("AREAS", DEFAULT_AREAS).split(",") if a.strip()}
    path = os.environ.get("STATE_FILE", "data/state.json")
    fail_n = int(os.environ.get("FAIL_ALERT_COUNT", "3"))
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    now = datetime.now(JST)
    code = 0
    try:
        run(state, topic, areas, now)
        if state.get("failure_notified"):
            send_ntfy(topic, "注意報・警報の取得が復旧しました", "取得が再開しました。", 2)
            state["failure_notified"] = False
        state["consecutive_failures"] = 0
        state.pop("last_error", None)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["last_error"] = str(e)[:300]
        state["last_checked"] = now.isoformat()
        if state["consecutive_failures"] >= fail_n and not state.get("failure_notified"):
            try:
                send_ntfy(topic, "注意報・警報を取得できていません",
                          "気象庁のデータを取得できません。気象台の情報を直接確認してください。", 4, ["warning"])
                state["failure_notified"] = True
            except Exception as e2:
                print(f"ntfy失敗: {e2}", file=sys.stderr)
        code = 1
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    sys.exit(code)


if __name__ == "__main__":
    main()
