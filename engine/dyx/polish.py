# -*- coding: utf-8 -*-
"""A2：AI 校对。

思路（不逐条喂全文，不重写正文）：
  1) 从某个作者的作品里抽 15–30 条代表作 → 让 chat 模型只挑「同音/近音错字」，
     回 `[{"wrong": "...", "right": "..."}]`，落进术语表（按作者 sec_uid 存）。
  2) 用术语表对全库做**确定性替换**（零 token），结果写进 `transcripts.polished`。
  3) 导出时 `Store.get_transcript()` 会优先返回校对后的文本，原始 `text` 不动。

同一作者的错法会重复出现，术语表越跑越准；换行业/换作者就是另一套表。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .util import DyxError, log

SYSTEM_PROMPT = (
    "你是中文语音转写稿的校对助手。用户会给你一段由语音识别生成的文字，"
    "你要挑出其中**真正听错的那几个字**，给出正确写法。\n"
    "规则：\n"
    "1. 错处通常只有 1–4 个字，是听错/同音造成的；right 与 wrong 长度应相同或相差 1 个字。\n"
    "2. 不要把本来正确的词当成错词。判断标准是：原句按字面读不通、或者行业上说不通，"
    "才说明那几个字听错了；原句读得通就不要报。\n"
    "3. 只改听错的字，不要改写句子、不要调整语序、不要增删内容、不要润色。\n"
    "4. wrong 必须是原文里出现的连续字串；right 必须是等义的规范写法。\n"
    "5. 专有名词、行业术语、人名地名优先还原成通行写法；不确定的不要输出。"
)


USER_TEMPLATE = """从下面这段语音识别文本里挑出**真正听错的那几个字**，并附上它所在的那句原文。

只输出词对，每行一个，格式：错词=>正词 || 含该词的原句（照抄原文，不超过30字）
不要解释，不要润色，不要续写，不要给建议，不要输出任何其它文字。
没有错词就输出：无

注意：错处通常只有 1–4 个字，正词与错词等长或只差 1 个字。
不要把本来正确的词报成错词 —— 下面第三个例子就是反面示范。

示例：
文本：朋友们大家好，欢迎收看大叔。这期咱们足句解读，历势上也有争议。
输出：
大叔=>达说 || 朋友们大家好，欢迎收看大叔
足句=>逐句 || 这期咱们足句解读
历势=>历史 || 历势上也有争议

文本：管理实施部门通常是和博物馆统筹在一个处室。
输出：
城市=>处室 || 通常是和博物馆统筹在一个城市
（说明：「统筹」本身是对的，错的只是「城市」两个字，所以只报城市=>处室）

文本：这个案子我们按文物法来办。
输出：无

文本：
{text}
"""


def _chat(cfg, messages: list[dict]) -> str:
    base = (cfg.get("polish.baseUrl") or "").rstrip("/")
    key = cfg.get("polish.apiKey") or ""
    model = cfg.get("polish.model") or ""
    timeout = float(cfg.get("polish.timeoutSec") or 120)
    if not base or not key:
        raise DyxError("还没配置 AI 校对模型（polish.baseUrl / polish.apiKey），"
                       "到「设置 & 登录」页填一个 chat 模型即可",
                       code="polish_not_configured")
    url = base + "/chat/completions"
    body = json.dumps({"model": model, "messages": messages, "temperature": 0,
                       "stream": False}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            choices = payload.get("choices") or []
            if not choices:
                raise DyxError("校对模型没有返回内容", code="polish_empty")
            return ((choices[0].get("message") or {}).get("content") or "").strip()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            last = f"HTTP {e.code} {detail}"
            if e.code in (429, 500, 502, 503, 504):
                import time as _t
                _t.sleep(1.5 * (2 ** attempt))
                continue
            break
        except Exception as e:  # noqa: BLE001
            last = str(e)
            import time as _t
            _t.sleep(1.5 * (2 ** attempt))
    raise DyxError(f"校对模型调用失败：{last}", code="polish_failed")


_PAIR_QUOTED = re.compile(
    r'[“"「\u2018](?P<wrong>[^”"」\u2019\n]{2,12})[”"」\u2019]\s*'
    r'(?:应该?|需要)\s*(?:改为|改成|写作|为|是)\s*[“"「]?(?P<right>[^”"」\n，。；：,;]{2,12})')
_PAIR_ARROW = re.compile(
    r'(?P<wrong>[^\s“”"「」，。；：,;]{2,12})\s*(?:=>|→|->)\s*'
    r'(?P<right>[^\s“”"「」，。；：,;]{2,12})')
_PAIR_CTX = re.compile(
    r'(?P<wrong>[^\s|｜，。；：,;]{2,12})\s*(?:=>|→|->)\s*'
    r'(?P<right>[^\s|｜，。；：,;]{2,12})\s*(?:\|\||\||｜|@)\s*(?P<ctx>.+)$')
_UNCERTAIN = ("可能", "或许", "不确定", "需确认", "需要确认", "疑似", "疑为",
              "或者", "或为", "待定", "拿不准")


def _clean(word: str) -> str:
    word = (word or "").strip().strip("“”\"「」‘’*`《》")
    word = word.strip()
    if not word or len(word) < 2 or len(word) > 12:
        return ""
    if any(ch in word for ch in "，。；：,;!?！？\n"):
        return ""
    return word


def extract_pairs(reply: str) -> tuple[dict, dict]:
    """从模型回复里抠出 {wrong: right}。

    这个模型经常不听话、回一大段解释外加若干条「“足句”应为“逐句”」，
    所以先试 JSON，失败就按行抠；含「可能/不确定」的行整行丢掉。
    """
    text = (reply or "").strip()
    if not text:
        return {}, {}
    stripped = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    m = re.search(r"\{.*\}|\[.*\]", stripped, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            data = None
        items = []
        if isinstance(data, dict):
            items = data.get("pairs") or data.get("items") or []
        elif isinstance(data, list):
            items = data
        pairs, contexts = {}, {}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            wrong = _clean(str(item.get("wrong") or ""))
            right = _clean(str(item.get("right") or ""))
            if wrong and right and wrong != right:
                pairs[wrong] = right
                ctx = str(item.get("context") or item.get("sentence") or "").strip()
                if ctx:
                    contexts[wrong] = ctx[:160]
        if pairs:
            return pairs, contexts

    pairs, contexts = {}, {}
    for line in re.split(r"[\r\n]+", text):
        raw = line.strip()
        if not raw or any(word in raw for word in _UNCERTAIN):
            continue
        hit = _PAIR_CTX.search(raw)
        if hit:
            wrong = _clean(hit.group("wrong"))
            right = _clean(hit.group("right"))
            if wrong and right and wrong != right:
                pairs[wrong] = right
                contexts[wrong] = hit.group("ctx").strip()[:160]
            continue
        for pattern in (_PAIR_QUOTED, _PAIR_ARROW):
            for hit in pattern.finditer(raw):
                wrong = _clean(hit.group("wrong"))
                right = _clean(hit.group("right"))
                if wrong and right and wrong != right:
                    pairs[wrong] = right
    return pairs, contexts


def _pick_samples(rows: list[dict], n: int) -> list[dict]:
    """在时间轴上均匀抽样，避免全是最近几条。"""
    if n <= 0 or not rows:
        return []
    if len(rows) <= n:
        return list(rows)
    step = len(rows) / float(n)
    return [rows[int(i * step)] for i in range(n)]


def apply_pairs(text: str, pairs: dict) -> tuple[str, int]:
    """确定性替换：长词优先，避免短词把长词切坏。"""
    if not text or not pairs:
        return text, 0
    hits = 0
    for wrong in sorted(pairs, key=len, reverse=True):
        right = pairs[wrong]
        if wrong and wrong in text:
            hits += text.count(wrong)
            text = text.replace(wrong, right)
    return text, hits


def filter_candidates(store, pairs, videos=None):
    """候选过滤：丢掉「已采纳的同错词」和「把已知错词当正词」的条目。"""
    manual = store.manual_fixes(None)
    bad_rights = set(manual.keys())
    kept, dropped = {}, {}
    for wrong, right in (pairs or {}).items():
        bad = wrong in manual or right in bad_rights
        if not bad and bad_rights:
            bad = any(token and token in right for token in bad_rights)
        if not bad and abs(len(right) - len(wrong)) > 2:
            bad = True
        if bad:
            dropped[wrong] = right
        else:
            kept[wrong] = right
    return kept, dropped


def mine(cfg, store, sec_uid: str | None = None, sample: int | None = None,
         ids: list[str] | None = None, cb=None) -> dict:
    """抽样挖术语表。"""
    n = int(sample or cfg.get("polish.sampleSize") or 20)
    rows = store.list_transcripts(sec_uid=sec_uid, limit=100000)
    if ids:
        wanted = {str(i) for i in ids}
        rows = [r for r in rows if str(r["aweme_id"]) in wanted]
    if not rows:
        return {"sampled": 0, "calls": 0, "terms": [], "failed": []}

    picks = _pick_samples(rows, n)
    max_chars = int(cfg.get("polish.maxCharsPerCall") or 6000)
    workers = max(1, min(int(cfg.get("polish.concurrency") or 2), 8))
    if cb:
        cb(f"抽样 {len(picks)} 条代表作，{workers} 路并发问模型")

    merged: dict[str, str] = {}
    contexts: dict[str, str] = {}
    counts: dict[str, int] = {}
    vid_seen: dict[str, set] = {}
    failed: list[str] = []

    def _one(row):
        text = (row.get("raw_text") or row.get("text") or "")[:max_chars]
        if not text.strip():
            return row, {}, {}
        reply = _chat(cfg, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(text=text)},
        ])
        pairs, ctxs = extract_pairs(reply)
        # 只保留原文里真的出现过的错词，防止模型自己编
        keep = {w: r for w, r in pairs.items() if w in text}
        return row, keep, {w: c for w, c in ctxs.items() if w in keep}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row, pairs, ctxs in pool.map(_one, picks):
            if not pairs:
                continue
            for wrong, right in pairs.items():
                merged[wrong] = right
                counts[wrong] = counts.get(wrong, 0) + 1
                vid_seen.setdefault(wrong, set()).add(row["aweme_id"])
                if ctxs.get(wrong) and wrong not in contexts:
                    contexts[wrong] = ctxs[wrong]

    videos = {w: len(s) for w, s in vid_seen.items()}
    merged, dropped = filter_candidates(store, merged, videos)
    if merged:
        store.save_terms(sec_uid or (picks[0].get("sec_uid") or ""), merged,
                         source="ai", videos=videos, contexts=contexts)

    terms = [{"wrong": w, "right": r, "count": counts.get(w, 1),
              "videos": videos.get(w, 1), "context": contexts.get(w, "")}
             for w, r in sorted(merged.items(),
                                key=lambda kv: (-videos.get(kv[0], 1), -counts.get(kv[0], 1)))]
    if cb:
        cb(f"术语表 +{len(terms)} 条")
    return {"sampled": len(picks), "filtered": len(dropped), "calls": len(picks), "terms": terms, "failed": failed}


def drop_pairs(store, pairs, sec_uid=None) -> int:
    """把指定错词从术语表删掉（用于否掉 AI 的坏候选）。"""
    wrongs = []
    for item in pairs or []:
        text = str(item or "").strip()
        if not text:
            continue
        head = re.split(r"=>|→|=", text)[0].strip()
        if head:
            wrongs.append(head)
    if not wrongs:
        return 0
    return store.drop_terms(sec_uid, wrongs)


def apply_local(text: str, patches: list[dict]) -> tuple[str, int]:
    """按上下文定位的逐处替换：只在原句里改那一个字，不影响别处。"""
    if not text or not patches:
        return text, 0
    hits = 0
    for item in patches:
        wrong = (item.get("wrong") or "").strip()
        right = (item.get("right") or "").strip()
        ctx = (item.get("context") or "").strip()
        if not wrong:
            continue
        if not ctx:
            continue
        start = text.find(ctx)
        if start < 0:
            continue
        end = start + len(ctx)
        window = text[start:end]
        if wrong not in window:
            continue
        text = text[:start] + window.replace(wrong, right, 1) + text[end:]
        hits += 1
    return text, hits


def parse_local_arg(values: list[str] | None) -> list[dict]:
    """解析 ["错=>正 || 原句"] → [{"wrong","right","context"}]。"""
    out: list[dict] = []
    for raw in values or []:
        item = str(raw or "").strip()
        if not item:
            continue
        context = ""
        for sep in ("||", "｜", "|"):
            if sep in item:
                head, _, tail = item.partition(sep)
                item, context = head.strip(), tail.strip()
                break
        pair = parse_pairs_arg([item])
        for wrong, right in pair.items():
            out.append({"wrong": wrong, "right": right, "context": context})
    return out


def accept_local(cfg, store, items: list[dict], cb=None) -> dict:
    """把「只改这处」的修正落库，并立刻重刷受影响的那几条文本。"""
    saved, touched = 0, set()
    for it in items or []:
        wrong, right, ctx = it.get("wrong"), it.get("right"), it.get("context") or ""
        targets = store.ids_by_context(ctx) if ctx else []
        if not targets:
            continue
        for aweme_id in targets:
            saved += store.save_patch(aweme_id, wrong, right, ctx)
            touched.add(aweme_id)
    if touched:
        res = apply_terms(cfg, store, ids=sorted(touched), cb=cb)
    else:
        res = {"applied": 0, "changed": 0}
    return {"patches": saved, "videos": len(touched), **res}


def accept_terms(store, pairs: dict, sec_uid: str | None = None) -> int:
    """人工确认的术语：直接以 manual 身份入库（一定参与替换）。"""
    return store.save_terms(sec_uid or "", pairs, source="manual")


def parse_pairs_arg(values: list[str] | None) -> dict:
    """把 ["错=>正", "错=正"] 解析成 dict。"""
    out: dict[str, str] = {}
    for raw in values or []:
        for sep in ("=>", "→", "="):
            if sep in raw:
                wrong, _, right = raw.partition(sep)
                wrong, right = wrong.strip(), right.strip()
                if wrong and right and wrong != right:
                    out[wrong] = right
                break
    return out


def apply_terms(cfg, store, sec_uid: str | None = None, cb=None,
                only_unpolished: bool = False, auto: bool = False,
                ids: list[str] | None = None) -> dict:
    """用术语表把库里的转写文本刷一遍（零 token）。"""
    min_hits = int(cfg.get("polish.minHits") or 2)
    all_terms = store.list_terms(sec_uid=sec_uid)
    # 默认只应用「人工采纳」的术语。这个 chat 模型偶尔会把正确用词判成错字
    # （研学→眼学、现象→假象），全库替换代价太大，所以 AI 挑出来的只当候选，
    # 要 auto=True（或 --auto）才会按 videos>=min_hits 自动应用。
    if auto:
        terms = [t for t in all_terms
                 if (t.get("source") or "") == "manual" or (t.get("videos") or 0) >= min_hits]
    else:
        terms = [t for t in all_terms if (t.get("source") or "") == "manual"]
    if not all_terms:
        return {"applied": 0, "changed": 0, "terms": 0, "skipped": True}
    if not terms:
        return {"applied": 0, "changed": 0, "terms": 0,
                "skipped": True, "reason": f"没有达到命中门槛（{min_hits}）的术语"}
    pairs = {t["wrong"]: t["right"] for t in terms}
    rows = store.list_transcripts(sec_uid=sec_uid, only_unpolished=only_unpolished,
                                  limit=100000, ids=ids)
    changed = 0
    for i, row in enumerate(rows, 1):
        text = row.get("raw_text") or row.get("text") or ""
        new, hits = apply_pairs(text, pairs)
        local, local_hits = apply_local(new, store.patches_for(row["aweme_id"]))
        if local_hits:
            new, hits = local, hits + local_hits
        if hits and new != text:
            store.save_polished(row["aweme_id"], new)
            changed += 1
        if cb and i % 100 == 0:
            cb(f"已刷 {i}/{len(rows)} 条，改动 {changed} 条")
    log(f"校对替换：{changed}/{len(rows)} 条有改动，术语 {len(pairs)} 条"
        f"（另有 {len(all_terms) - len(terms)} 条未达门槛，只做展示）")
    return {"applied": len(rows), "changed": changed, "terms": len(pairs),
            "weak": len(all_terms) - len(terms), "minHits": min_hits, "skipped": False}
