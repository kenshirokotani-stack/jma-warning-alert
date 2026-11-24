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
import re
import sys
import time
import unicodedata
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
KEYWORDS = ("大雨", "洪水", "氾濫")  # この語を含む種類だけ見る(暴風・雷などは対象外)
# 従来形式(H27)は1つの電文に全種類が入る。新形式(R06)は種類ごとに分かれ内容が部分的なので、
# 実際の形を確認するまで状態の更新には使わない。
FORMAT_MARK = "H27"


# 指定河川洪水予報(国土交通省と気象台の共同発表)。大和川上流=VXKO50、下流=VXKO51(JPOS)
FLOOD_OFFICE = "大阪管区気象台"
DEFAULT_ZONES = "大和川上流"  # 下流(柏原)も見るなら ZONES に「大和川上流,大和川下流」


def flood_level(kind):
    """指定河川洪水予報の種類名 → (段階, 優先度)。段階 0=解除。判別できなければ None。
    旧名(氾濫注意情報など)と新名(レベル２氾濫注意報など)の両方に対応。"""
    k = unicodedata.normalize("NFKC", kind)
    if "解除" in k:
        return 0, 2
    if "発生" in k or "特別警報" in k:
        return 4, 5
    if "危険" in k:
        return 3, 5
    if "警戒" in k or "氾濫警報" in k:
        return 2, 4
    if "注意" in k:
        return 1, 3
    return None


def parse_flood(text, zones):
    """指定河川洪水予報(VXKO)を読む。対象の予報区域なら
    {"zone","kind","headline","time"} を返す。対象外は None。"""
    root = strip_ns(ET.fromstring(text))
    status = root.findtext("Control/Status") or ""
    if status and status != "通常":
        return None
    title = (root.findtext("Head/Title") or "").strip()
    headline = (root.findtext("Head/Headline/Text") or "").strip()
    rtime = (root.findtext("Head/ReportDateTime") or "").strip()
    zone, kind = None, None
    for w in root.iter("Warning"):
        if "予報区域" not in (w.get("type") or ""):
            continue
        for item in w.findall("Item"):
            an = (item.findtext("Area/Name") or "").strip()
            if an in zones:
                zone = an
                for k in item.findall("Kind"):
                    kn = (k.findtext("Name") or "").strip()
                    if "氾濫" in kn:
                        kind = kn
                        break
    if zone is None:  # 構造が想定と違う場合の保険: タイトルから
        for z in zones:
            if z in title:
                zone = z
    if zone is None:
        return None
    if kind is None:
        kind = title or headline
    return {"zone": zone, "kind": kind, "headline": headline, "time": rtime}


def kind_level(kind):
    """種類名 → レベル(2注意報,3警報,4危険警報,5特別警報)。新名称の「レベルN」を優先。"""
    n = unicodedata.normalize("NFKC", kind)
    m = re.search(r"レベル(\d)", n)
    if m:
        return int(m.group(1))
    if "特別警報" in n:
        return 5
    if "危険警報" in n:
        return 4
    if "警報" in n:
        return 3
    if "注意報" in n:
        return 2
    return 0


def kind_key(kind):
    """旧名称(大雨注意報)と新名称(レベル２大雨注意報)を同じものとして扱う鍵。"""
    n = unicodedata.normalize("NFKC", kind)
    hazard = "洪水" if ("洪水" in n or "氾濫" in n) else "大雨"
    return f"{hazard}{kind_level(kind)}"


def severity(kind):
    """1=注意報, 2=警報, 3=危険警報(レベル4), 4=特別警報"""
    lv = kind_level(kind)
    return 0 if lv == 0 else 1 if lv <= 2 else lv - 1


def merged_active(state):
    """形式(H27/R06)ごとの状態を、同じ注意報・警報が重ならないよう合わせる。"""
    out = {}
    for src in ("R06", "H27"):
        for area, kinds in state["src"].get(src, {}).items():
            cur = out.setdefault(area, {})
            for k in kinds:
                cur.setdefault(kind_key(k), k)
    return {a: list(v.values()) for a, v in out.items() if v}


def strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def is_flood_entry(e):
    """大阪管区気象台の指定河川洪水予報(VXKO)か。"""
    return FLOOD_OFFICE in e["author"] and (
        "指定河川洪水予報" in e["title"] or "_VXKO" in e["url"])


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


def parse_warning(text, areas, names_seen=None):
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
            if names_seen is not None and name:
                names_seen.add(name)
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
    head = {4: "大雨特別警報", 3: "大雨・洪水の危険警報", 2: "大雨・洪水の警報", 1: "大雨・洪水の注意報"}[top]
    msg = "\n".join(lines) + "\n\n水位を確認してください。\n(注意報・警報による備えの合図です)"
    prio = {4: 5, 3: 5, 2: 4, 1: 3}[top]
    tags = {4: ["rotating_light"], 3: ["rotating_light"], 2: ["warning"], 1: ["cloud_with_rain"]}[top]
    return f"【{head}】奈良県 大和川流域", msg, prio, tags


def run(state, topic, areas, now, fetch=get, notify=send_ntfy, zones=None):
    """1回分の処理。state を更新して返す。通知に失敗したら例外。"""
    state.setdefault("src", {"H27": state.pop("active", {}), "R06": {}})  # 形式ごとの {市町村: [種類]}
    state["src"].setdefault("H27", {})
    state["src"].setdefault("R06", {})
    state.setdefault("seen", [])        # 処理済みの電文URL
    state.setdefault("notified", {})    # {市町村: [通知済みの種類]}
    for a_, ks_ in list(state["notified"].items()):  # 旧形式(種類名)の通知済みを鍵に変換
        state["notified"][a_] = sorted({k if re.fullmatch(r"(大雨|洪水)\d", k) else kind_key(k) for k in ks_})
    state.setdefault("flood", {})       # {予報区域: 現在の洪水予報}
    state.setdefault("flood_notified", {})  # {予報区域: 通知済みの段階}
    zones = zones if zones is not None else {DEFAULT_ZONES}

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
        if is_flood_entry(e):
            e["kind"] = "flood"
        elif OFFICE in e["author"] and "警報・注意報" in e["title"] \
                and "特別警報・警報・注意報" not in e["title"]:
            e["kind"] = "warning"  # 府県予報区の電文(VPWW53)は除き、市町村の電文を使う
        else:
            continue
        uniq.add(e["url"])
        todo.append(e)
    todo.sort(key=lambda e: e["updated"])

    names_seen = set()
    for e in todo:
        if e["kind"] == "flood":
            f = parse_flood(fetch(e["url"]), zones)
            if f:
                lv = flood_level(f["kind"])
                if lv is not None and lv[0] == 0:
                    state["flood"].pop(f["zone"], None)
                else:
                    state["flood"][f["zone"]] = {
                        "kind": f["kind"], "level": lv[0] if lv else -1,
                        "prio": lv[1] if lv else 4,
                        "headline": f["headline"], "time": f["time"]}
            else:
                print(f"INFO 大和川以外の洪水予報のため無視: {e['title']} {e['url']}")
            state["seen"].append(e["url"])
            continue
        norm = unicodedata.normalize("NFKC", e["title"]).upper()
        if FORMAT_MARK in norm:
            src = "H27"
        elif "R06" in norm and "大雨" in norm:
            src = "R06"  # 新形式の大雨(VPWW55)。その他(雷・暴風など)の電文は使わない
        else:
            print(f"INFO 対象外の形式のため状態更新に使わない: {e['title']} {e['url']}")
            state["seen"].append(e["url"])
            continue
        updates = parse_warning(fetch(e["url"]), areas, names_seen)
        for area, kinds in updates.items():
            if kinds:
                state["src"][src][area] = kinds
            else:
                state["src"][src].pop(area, None)
        state["seen"].append(e["url"])

    state["seen"] = state["seen"][-400:]
    unknown = sorted(n for n in names_seen if n not in areas)
    if unknown:
        print("INFO 電文に出た市町村名(対象外): " + "、".join(unknown))

    # 指定河川洪水予報: 段階が変わったときだけ通知(同じ段階の続報は通知しない)
    for zone in sorted(set(state["flood"]) | set(state["flood_notified"])):
        cur = state["flood"].get(zone)
        done = state["flood_notified"].get(zone)
        if cur is None:
            if done is not None:  # 解除された
                notify(topic, f"【{zone}】洪水予報の解除",
                       "指定河川洪水予報が解除されました。\n水位の確認は引き続き行ってください。\n"
                       "出典: 気象庁防災情報XML(指定河川洪水予報)", 2, ["white_check_mark"])
                state["flood_notified"].pop(zone, None)
        elif done != cur["level"]:
            body = (cur["headline"] or cur["kind"]) + "\n\n水位を確認してください。\n" \
                "国土交通省・気象台の共同発表(指定河川洪水予報)です。\n" \
                "出典: 気象庁防災情報XML(抜粋・編集:本システム)"
            notify(topic, f"【{zone}】{cur['kind']}", body, cur["prio"],
                   ["rotating_light"] if cur["prio"] >= 5 else ["warning"])
            state["flood_notified"][zone] = cur["level"]

    # 新しく出た種類だけ通知(同じ注意報・警報は1回だけ。旧名称と新名称は同じ扱い)
    active = merged_active(state)
    for area in list(state["notified"]):
        if area not in active:
            state["notified"].pop(area)
    new_items = []
    for area, kinds in active.items():
        done = set(state["notified"].get(area, []))
        if any(kind_key(k) not in done for k in kinds):
            new_items.append((area, kinds))
    if new_items:
        title, msg, prio, tags = build_message(new_items, active)
        notify(topic, title, msg, prio, tags)  # 失敗したら例外 → 通知済みにしない
        for area, kinds in new_items:
            state["notified"][area] = sorted({kind_key(k) for k in kinds})
        state["was_active"] = True
    elif state.get("was_active") and not active:
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
    if os.environ.get("TEST_NOTIFY") == "true":
        # テスト通知: 状態ファイルには触れず、通知の経路だけ確認する
        send_ntfy(topic, "【テスト】注意報・警報の通知", "これはテスト通知です。この通知が届けば、経路は正常です。", 3, ["white_check_mark"])
        print("テスト通知を送りました")
        sys.exit(0)
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
        zones = {z.strip() for z in os.environ.get("ZONES", DEFAULT_ZONES).split(",") if z.strip()}
        run(state, topic, areas, now, zones=zones)
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
