# Created by 青云 on 2026/10/21.
"""杭州周末指南数据抓取：天气 / 空气 / 生活指数 / 限行、展览、免费活动、演出玩乐。只用标准库。

每个来源单独 try，失败时沿用上一次 data.json 里该来源的数据并标 stale。
"""
import html, json, os, re, socket, ssl, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data.json"
CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
LAT, LON = 30.2741, 120.1551          # 杭州市中心（武林广场附近）
WCN_CITY = "101210101"                # 中国天气网城市编码：杭州
WCN_REF = {"Referer": "http://www.weather.com.cn/"}
LOOKAHEAD_DAYS = 45                   # 活动只保留这么多天内开始的


# 国美美术馆的证书链不完整（缺中间证书），只对它跳过校验；只读公开页面
INSECURE_HOSTS = {"caam.caa.edu.cn"}

# 部分站点（浙江图书馆）只对国内解析，Actions 的海外 DNS 查不到。系统 DNS 失败时改问阿里 DoH，
# 并带上杭州电信网段作 EDNS client subnet（否则 DoH 也按海外来源返回空）。
# 只替换解析结果，TLS 的 SNI 和 Host 仍是原域名
_sys_getaddrinfo = socket.getaddrinfo
_doh_cache = {}


def _doh_resolve(host):
    if host not in _doh_cache:
        url = "https://dns.alidns.com/resolve?" + urllib.parse.urlencode({"name": host, "type": "A",
                                                                         "edns_client_subnet": "115.238.0.0/24"})
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=10) as r:
            ans = json.loads(r.read().decode()).get("Answer", [])
        _doh_cache[host] = [a["data"] for a in ans if a.get("type") == 1]
    return _doh_cache[host]


def _getaddrinfo(host, port, *args, **kw):
    try:
        return _sys_getaddrinfo(host, port, *args, **kw)
    except socket.gaierror:
        if not isinstance(host, str) or host == "dns.alidns.com":
            raise
        ips = _doh_resolve(host)
        if not ips:
            raise
        print(f"  DoH: {host} -> {ips[0]}")
        return _sys_getaddrinfo(ips[0], port, *args, **kw)


socket.getaddrinfo = _getaddrinfo


def fetch(url, timeout=20, headers=None, data=None, enc="utf-8", retries=2):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    ctx = ssl._create_unverified_context() if urllib.parse.urlsplit(url).hostname in INSECURE_HOSTS else None
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read().decode(enc, "ignore")
        except Exception:
            if i == retries:
                raise
            time.sleep(2)


def fetch_json(url, **kw):
    return json.loads(fetch(url, **kw))


def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def now_cst():
    return datetime.now(CST)


def weekend_dates(today):
    """本周末（周六、周日）。周日当天仍算本周末；周一起看下一个周末。"""
    wd = today.weekday()               # 周一=0
    if wd == 6:
        sat = today - timedelta(days=1)
    else:
        sat = today + timedelta(days=5 - wd)
    return sat, sat + timedelta(days=1)


def js_var(text, name):
    """从 `var name = {...};` 里取出 JSON 对象（支持嵌套括号）。"""
    m = re.search(r"var\s+" + name + r"\s*=\s*", text)
    if not m:
        return None
    obj, _ = json.JSONDecoder().raw_decode(text[m.end():])
    return obj


def ymd(s):
    """把各种日期写法统一成 YYYY-MM-DD。"""
    if not s:
        return None
    s = str(s)
    m = re.search(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", s)
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


# ── 天气：中国天气网（实况 / 预警 / 生活指数 / 限行 / 7 天文字预报 / 5 天 AQI）──
def wcn_index():
    t = fetch(f"http://d1.weather.com.cn/weather_index/{WCN_CITY}.html?_={int(time.time()*1000)}", headers=WCN_REF)
    sk = js_var(t, "dataSK") or {}
    alarm = js_var(t, "alarmDZ") or {}
    zs = (js_var(t, "dataZS") or {}).get("zs", {})
    now = {
        "temp": sk.get("temp"), "weather": sk.get("weather"), "wind": f'{sk.get("WD", "")} {sk.get("WS", "")}'.strip(),
        "humidity": sk.get("SD"), "aqi": sk.get("aqi"), "rain24h": sk.get("rain24h"), "time": sk.get("time"),
        "limit": sk.get("limitnumber") or "",
    }
    alarms = [{"type": w.get("w5"), "level": w.get("w7"), "desc": w.get("w9"), "time": w.get("w8")}
              for w in alarm.get("w", [])]
    keys = ["ct", "yd", "tr", "uv", "gm", "ls", "xc", "pl", "co", "gj", "hc", "pk"]
    indices = [{"key": k, "name": zs.get(f"{k}_name", "").replace("指数", ""), "hint": zs.get(f"{k}_hint", ""),
                "desc": zs.get(f"{k}_des_s", "")} for k in keys if zs.get(f"{k}_name")]
    return now, alarms, indices


def wcn_7d():
    """7 天文字预报（多云转小雨、风向风力），按日期返回。"""
    t = fetch(f"http://www.weather.com.cn/weather/{WCN_CITY}.shtml", headers=WCN_REF)
    today = now_cst().date()
    out = {}
    for i, li in enumerate(re.findall(r'<li class="sky[^"]*">(.*?)</li>', t, re.S)[:7]):
        wea = re.search(r'<p title="([^"]+)" class="wea"', li)
        tem = re.search(r'<p class="tem">\s*(?:<span>(-?\d+)</span>/)?<i>(-?\d+)℃</i>', li)
        win = re.findall(r'<span title="([^"]+)"', li)
        lvl = re.search(r"<i>([^<]*级[^<]*)</i>", li)
        out[(today + timedelta(days=i)).isoformat()] = {
            "text": wea.group(1) if wea else "",
            "wind": ("转".join(dict.fromkeys(win)) + " " + (lvl.group(1) if lvl else "")).strip(),
        }
    return out


def wcn_aqi():
    t = fetch(f"http://d1.weather.com.cn/aqi_7d/XiangJiAqiFc5d/{WCN_CITY}.html", headers=WCN_REF)
    d = js_var(t, "json") or {}
    return {ymd(x["000"]): {"aqi": int(x["001"]), "level": x["002"]} for x in d.get("2001007", []) if x.get("001")}


# ── 天气兜底与逐小时：Open-Meteo（全球可达）────────────────────
def open_meteo():
    q = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON, "timezone": "Asia/Shanghai", "forecast_days": 10,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "precipitation_sum,uv_index_max,sunrise,sunset,wind_speed_10m_max",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
    })
    d = fetch_json(f"https://api.open-meteo.com/v1/forecast?{q}")
    dl = d["daily"]
    daily = [{
        "date": dl["time"][i], "code": dl["weather_code"][i],
        "tmax": round(dl["temperature_2m_max"][i]), "tmin": round(dl["temperature_2m_min"][i]),
        "pop": dl["precipitation_probability_max"][i], "rain": dl["precipitation_sum"][i],
        "uv": dl["uv_index_max"][i], "wind_max": dl["wind_speed_10m_max"][i],
        "sunrise": (dl["sunrise"][i] or "")[11:], "sunset": (dl["sunset"][i] or "")[11:],
    } for i in range(len(dl["time"]))]
    hl = d["hourly"]
    hourly = {}
    for i, ts in enumerate(hl["time"]):
        hourly.setdefault(ts[:10], []).append(
            {"h": int(ts[11:13]), "t": round(hl["temperature_2m"][i]), "pop": hl["precipitation_probability"][i],
             "code": hl["weather_code"][i]})
    cur = d.get("current", {})
    return daily, hourly, cur


def open_meteo_aqi():
    q = urllib.parse.urlencode({"latitude": LAT, "longitude": LON, "timezone": "Asia/Shanghai",
                                "forecast_days": 5, "hourly": "pm2_5"})
    d = fetch_json(f"https://air-quality-api.open-meteo.com/v1/air-quality?{q}")
    by_day = {}
    for ts, v in zip(d["hourly"]["time"], d["hourly"]["pm2_5"]):
        if v is not None:
            by_day.setdefault(ts[:10], []).append(v)
    # 按国标 PM2.5 分段粗算 AQI 等级（仅兜底用）
    def level(pm):
        for lim, name in ((35, "优"), (75, "良"), (115, "轻度"), (150, "中度"), (250, "重度")):
            if pm <= lim:
                return name
        return "严重"
    return {k: {"pm25": round(sum(v) / len(v)), "level": level(sum(v) / len(v))} for k, v in by_day.items()}


# ── 展览 ───────────────────────────────────────────────
def zjmuseum():
    """浙江省博物馆：官网 SPA 背后的 JSON 接口。"""
    body = {"siteId": 22, "type": 2, "pageNum": 1, "pageSize": 20, "startDate": "", "endDate": "",
            "hasLink": "", "hasOpen": "1"}
    d = fetch_json("https://www.zjmuseum.com.cn/api/exhibition/page", data=json.dumps(body).encode(),
                   headers={"Content-Type": "application/json"})
    rows = d["data"][1] if isinstance(d["data"], list) else d["data"].get("list", [])
    out = []
    for r in rows:
        area = r.get("museum_area") or ""
        out.append({
            "title": clean(r["title"]), "venue": "浙江省博物馆" + (f"·{area}" if area else ""),
            "hall": r.get("address") or "", "start": ymd(r.get("start_time")), "end": ymd(r.get("end_time")),
            "img": "https://www.zjmuseum.com.cn" + r["cover_url"] if r.get("cover_url") else "",
            "url": f'https://www.zjmuseum.com.cn/cn/#/Exhibition/TemporaryExhibition/{r["id"]}',
            "free": True, "fee": "免费（需预约）",
        })
    return out


def zjam():
    """浙江美术馆：正在展出 type=1 + 即将展出 type=2，ASP.NET 静态页（GBK）。"""
    out = []
    for typ in (1, 2):
        t = fetch(f"https://www.zjam.org.cn/Site/Exbition/ExbitionList.aspx?type={typ}", enc="gb18030")
        for blk in re.findall(r'<div class="top">(.*?)</p>', t, re.S):
            href = re.search(r'href="(ExbitionDetail\.aspx\?etid=\d+[^"]*)"', blk)
            title = re.search(r'<h3 class="title">(.*?)</h3>', blk, re.S)
            img = re.search(r'<img src="([^"]+)"', blk)
            rng = re.search(r"展览时间：\s*([\d/]+)\s*至\s*([\d/]+)", blk)
            hall = re.search(r"展厅：\s*([^<]+)", blk)
            if not (href and title):
                continue
            out.append({
                "title": clean(title.group(1)), "venue": "浙江美术馆",
                "hall": clean(hall.group(1)).replace("浙江美术馆", "") if hall else "",
                "start": ymd(rng.group(1)) if rng else None, "end": ymd(rng.group(2)) if rng else None,
                "img": "https://www.zjam.org.cn" + img.group(1) if img else "",
                "url": "https://www.zjam.org.cn/Site/Exbition/" + html.unescape(href.group(1)),
                "free": True, "fee": "免费",
            })
    return out


def caam():
    """中国美术学院美术馆：当前展览 + 展览预告。"""
    out = []
    for page in ("dqzl", "zlyg"):
        t = fetch(f"https://caam.caa.edu.cn/zl/{page}.htm")
        for blk in re.findall(r'<a href="([^"]+)" class="item">(.*?)</a>', t, re.S):
            href, body = blk
            title = re.search(r'<p class="tt">(.*?)</p>', body, re.S)
            rng = re.search(r'<p class="dt-range">(.*?)</p>', body, re.S)
            img = re.search(r"url\(([^)]+)\)", body)
            if not title:
                continue
            dates = re.findall(r"\d{4}年\d{1,2}月\d{1,2}日", rng.group(1)) if rng else []
            out.append({
                "title": clean(title.group(1)), "venue": "中国美术学院美术馆", "hall": "",
                "start": ymd(dates[0]) if dates else None, "end": ymd(dates[1]) if len(dates) > 1 else None,
                "img": urllib.parse.urljoin("https://caam.caa.edu.cn/", html.unescape(img.group(1))) if img else "",
                "url": urllib.parse.urljoin(f"https://caam.caa.edu.cn/zl/{page}.htm", href),
                "free": None, "fee": "",
            })
    return out


def zmnh():
    """浙江自然博物院：临展接口，只有发布时间、没有展期。"""
    d = fetch_json("https://www.zmnh.com/web?category=18&limit=6")
    out = []
    for r in d.get("data", []):
        pub = datetime.fromtimestamp(r["publishTime"] / 1000, CST).date().isoformat() if r.get("publishTime") else None
        img = r.get("img") or r.get("icon") or ""
        out.append({
            "title": clean(r["title"]), "venue": "浙江自然博物院", "hall": "", "start": None, "end": None,
            "published": pub, "desc": clean(r.get("summary"))[:80],
            "img": "https://www.zmnh.com/" + img.lstrip("/") if img else "",
            "url": f'https://www.zmnh.com/lintezhan/lintezhan-info.html?category=18&id={r["id"]}',
            "free": True, "fee": "免费（需预约）",
        })
    return out


# ── 豆瓣同城：周末各分类 ────────────────────────────────
DOUBAN_CATS = [("exhibition", "展览"), ("drama", "戏剧"), ("music", "音乐"), ("film", "电影"),
               ("party", "聚会"), ("salon", "讲座"), ("sports", "运动"), ("others", "其他")]
EXPERIENCE_RE = re.compile(r"DIY|体验|手作|疗愈|剧场|沉浸|密室|剧本|课程|工坊")


def douban_page(cat, start=0):
    url = f"https://www.douban.com/location/hangzhou/events/weekend-{cat}"
    if start:
        url += f"?start={start}"
    t = fetch(url)
    out = []
    for li in re.findall(r'<li class="list-entry"[^>]*>(.*?)</li>\s*(?=<li class="list-entry"|</ul>)', t, re.S):
        a = re.search(r'<a href="(https://www\.douban\.com/event/\d+/)" title="([^"]+)"', li)
        if not a:
            continue
        st = re.search(r'itemprop="startDate" datetime="([^"]+)"', li)
        et = re.search(r'itemprop="endDate" datetime="([^"]+)"', li)
        loc = re.search(r'itemprop="location" content="([^"]+)"', li)
        fee = re.search(r'<li class="fee">.*?<strong>(.*?)</strong>', li, re.S)
        fee_s = clean(fee.group(1)) if fee else ""
        out.append({
            "title": clean(a.group(2)), "url": a.group(1),
            "start": (st.group(1) if st else "")[:16], "end": (et.group(1) if et else "")[:16],
            "place": clean(loc.group(1)).replace("杭州 ", "", 1) if loc else "",
            "fee": fee_s, "free": fee_s.startswith("免费"), "img": "",   # 豆瓣海报校验 Referer，Pages 上外链会 418
        })
    more = bool(re.search(rf'start={start + 10}\b', t))
    return out, more


def douban():
    """返回 (展览, 活动)。展览分类里混着不少商业体验，挑出真正的「展」。"""
    exh, events, seen = [], [], set()
    for cat, label in DOUBAN_CATS:
        start = 0
        while start <= 20:                          # 每类最多 3 页，控制请求量
            rows, more = douban_page(cat, start)
            for r in rows:
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                if cat == "exhibition" and "展" in r["title"] and not EXPERIENCE_RE.search(r["title"]):
                    exh.append({"title": r["title"], "venue": r["place"].split(" ")[1] if " " in r["place"] else r["place"],
                                "hall": "", "start": r["start"][:10], "end": r["end"][:10], "img": r["img"],
                                "url": r["url"], "free": r["free"], "fee": r["fee"], "place": r["place"]})
                else:
                    r["cat"] = "体验" if cat == "exhibition" else label
                    if "市集" in r["title"]:
                        r["cat"] = "市集"
                    r["source"] = "豆瓣同城"
                    events.append(r)
            time.sleep(1.2)
            if not more:
                break
            start += 10
    return exh, events


def showstart():
    """秀动杭州：只要未来 LOOKAHEAD_DAYS 天内首场的新演出（常驻演出首场日期在过去，跳过）。"""
    t = fetch("https://www.showstart.com/event/list?cityCode=571&pageNo=1")
    today = now_cst().date()
    out = []
    for href, body in re.findall(r'<a href="(/event/\d+)" class="show-item item"[^>]*>(.*?)</a>', t, re.S):
        title = re.search(r'<div class="title">(.*?)</div>', body, re.S)
        tm = re.search(r'<div class="time">时间：([^<]+)</div>', body)
        price = re.search(r'<div class="price">价格：<span>([^<]*)</span>', body)
        addr = re.search(r'<div class="addr">.*?</i>(.*?)</div>', body, re.S)
        d = ymd(tm.group(1)) if tm else None
        if not (title and d):
            continue
        if not (today <= date.fromisoformat(d) <= today + timedelta(days=LOOKAHEAD_DAYS)):
            continue
        out.append({
            "title": clean(title.group(1)), "url": "https://www.showstart.com" + href,
            "start": tm.group(1).strip().replace("/", "-"), "end": "",
            "place": clean(addr.group(1)).replace("[杭州]", "") if addr else "",
            "fee": clean(price.group(1)) if price else "", "free": False, "img": "",
            "cat": "演出", "source": "秀动",
        })
    return out


# ── 免费活动：图书馆 ─────────────────────────────────────
ZJLIB_ORG = "1916318653650423810"


def zjlib():
    """浙江图书馆：预告中(0) / 报名中(3) / 进行中(1) 的活动，全部免费。"""
    out, seen = [], set()
    base = "https://www.zjlib.cn/bff-api/portal-admin-service/portal-pc-api/activity/portal/page"
    for cycle in (0, 3, 1):
        q = urllib.parse.urlencode({"funTag": 3, "current": 1, "size": 30, "orgId": ZJLIB_ORG, "activityCycle": cycle})
        d = fetch_json(f"{base}?{q}")
        for r in d.get("data", {}).get("records", []):
            if r["id"] in seen or "通告" in r["activityName"] or "公告" in r["activityName"]:
                continue
            seen.add(r["id"])
            out.append({
                "title": clean(r["activityName"]), "start": (r.get("activityStartTime") or "")[:16],
                "end": (r.get("activityEndTime") or "")[:16], "place": "浙江图书馆" + (
                    "·" + r["siteName"].replace("浙江图书馆", "") if r.get("siteName") else ""),
                "cat": r.get("classifyName") or "活动", "url": f'https://www.zjlib.cn/activity/details/{r["id"]}',
                "free": True, "fee": "免费", "source": "浙江图书馆",
            })
        time.sleep(0.5)
    return out


def hzlib():
    """杭州图书馆：讲座 / 沙龙 / 展览列表，只有标题和发布日期（时间地点在海报里）。"""
    out = []
    for col, cat in (("hdygjz", "讲座"), ("hdygsl", "沙龙"), ("hdygzl", "展览")):
        t = fetch(f"https://www.zjhzlib.cn/{col}/index.htm")
        for href, title, pub in re.findall(
                r'<li><a href="([^"]+)" title="([^"]+)"[^>]*>.*?</a><span>(\d{4}-\d{2}-\d{2})</span>', t, re.S)[:12]:
            out.append({"title": clean(title), "published": pub, "start": "", "end": "", "place": "杭州图书馆",
                        "cat": cat, "url": urllib.parse.urljoin("https://www.zjhzlib.cn/", href),
                        "free": True, "fee": "免费", "source": "杭州图书馆"})
        time.sleep(0.5)
    return out


# ── 汇总 ───────────────────────────────────────────────
def load_old():
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    old = load_old()
    old_src = {s["id"]: s for s in old.get("sources", [])}
    weather_ids = ("wcn_index", "wcn_7d", "wcn_aqi", "open_meteo", "open_meteo_aqi")
    store = {k: v for k, v in old.get("store", {}).items() if k in weather_ids}   # 天气类来源上一次成功的结果，失败时兜底
    sources = []

    def run(sid, name, fn, fallback=None):
        """跑一个来源；失败时天气类用 store 兜底，列表类用上一份 data.json 里同来源的条目兜底。"""
        try:
            res = fn()
            cnt = sum(len(x) for x in res) if sid == "douban" else len(res)
            sources.append({"id": sid, "name": name, "ok": True, "stale": False, "count": cnt,
                            "updated": now_cst().isoformat(timespec="seconds")})
            print(f"✓ {name}: {cnt}")
        except Exception as e:
            res = store.get(sid) if fallback is None else fallback
            prev = old_src.get(sid, {})
            sources.append({"id": sid, "name": name, "ok": False, "stale": bool(res), "count": prev.get("count", 0),
                            "updated": prev.get("updated"), "error": f"{type(e).__name__}: {e}"[:160]})
            print(f"✗ {name}: {e}")
            return res
        if fallback is None:
            store[sid] = res
        return res

    def prev_items(key, source):
        return [x for x in old.get(key, []) if x.get("source") == source]

    wi = run("wcn_index", "中国天气网 实况/指数", lambda: list(wcn_index()))
    w7 = run("wcn_7d", "中国天气网 7 天预报", wcn_7d)
    wa = run("wcn_aqi", "中国天气网 AQI 预报", wcn_aqi)
    om = run("open_meteo", "Open-Meteo 天气", lambda: list(open_meteo()))
    oa = run("open_meteo_aqi", "Open-Meteo 空气", open_meteo_aqi)

    exhibitions = []
    for sid, name, fn in (("zjmuseum", "浙江省博物馆", zjmuseum), ("zjam", "浙江美术馆", zjam),
                          ("caam", "国美美术馆", caam), ("zmnh", "浙江自然博物院", zmnh)):
        res = run(sid, name, lambda fn=fn, name=name: [{**x, "source": name} for x in fn()],
                  fallback=prev_items("exhibitions", name))
        exhibitions += res or []
    db = run("douban", "豆瓣同城", lambda: list(douban()),
             fallback=[prev_items("exhibitions", "豆瓣同城"), prev_items("events", "豆瓣同城")])
    exhibitions += [{**x, "source": "豆瓣同城"} for x in db[0]]
    events = list(db[1]) + run("showstart", "秀动", showstart, fallback=prev_items("events", "秀动"))
    free = (run("zjlib", "浙江图书馆", zjlib, fallback=prev_items("free", "浙江图书馆"))
            + run("hzlib", "杭州图书馆", hzlib, fallback=prev_items("free", "杭州图书馆")))

    # 去掉已经结束的展览 / 活动，同名展留官方来源（官方来源排在豆瓣前面）
    today = now_cst().date().isoformat()
    exhibitions = [x for x in exhibitions if not x.get("end") or x["end"] >= today]
    seen, uniq = set(), []
    for x in exhibitions:
        k = re.sub(r"\W", "", x["title"])[:14]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    events = [x for x in events if not x.get("end") or x["end"][:10] >= today]
    free = [x for x in free if not x.get("end") or x["end"][:10] >= today]

    # 天气合并：Open-Meteo 给数值，中国天气网给中文描述，AQI 优先站点预报
    daily, hourly, cur = om or [[], {}, {}]
    w7, wa, oa = w7 or {}, wa or {}, oa or {}
    for d in daily:
        d.update(w7.get(d["date"], {}))
        if d["date"] in wa:
            d["aqi"], d["aqi_level"] = wa[d["date"]]["aqi"], wa[d["date"]]["level"]
        elif d["date"] in oa:
            d["pm25"], d["aqi_level"] = oa[d["date"]]["pm25"], oa[d["date"]]["level"]
    now, alarms, indices = wi or [{}, [], []]
    if not now.get("temp") and cur:
        now = {"temp": cur.get("temperature_2m"), "humidity": f'{cur.get("relative_humidity_2m")}%',
               "code": cur.get("weather_code"), "wind": f'{cur.get("wind_speed_10m")} km/h', "limit": ""}

    sat, sun = weekend_dates(now_cst().date())
    data = {
        "updated": now_cst().isoformat(timespec="seconds"),
        "city": "杭州",
        "weekend": [sat.isoformat(), sun.isoformat()],
        "now": now, "alarms": alarms, "indices": indices,
        "daily": daily, "hourly": {k: v for k, v in hourly.items() if k in (sat.isoformat(), sun.isoformat())},
        "exhibitions": uniq, "events": events, "free": free,
        "sources": sources, "store": store,
    }
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"done: {len(uniq)} 展览 / {len(events)} 活动 / {len(free)} 免费")
    try:
        places()
    except Exception as e:                     # 地点推荐出错不影响天气 / 展览 / 活动
        print(f"✗ 地点推荐: {type(e).__name__}: {e}")


# ── 地点推荐：高德 Web 服务 POI 搜索（按区 × 类别，只留有照片的）────────────
PLACES_FILE = ROOT / "places.json"
DISTRICTS = [                                    # 余杭默认，排第一
    ("330110", "余杭"), ("330106", "西湖"), ("330105", "拱墅"), ("330102", "上城"), ("330108", "滨江"),
    ("330113", "临平"), ("330109", "萧山"), ("330114", "钱塘"), ("330111", "富阳"), ("330112", "临安"),
    ("330122", "桐庐"), ("330127", "淳安"), ("330182", "建德"),
]
DEFAULT_DISTRICT = "330110"
PLACE_CATS = [                                   # (id, 名称, 搜索关键词)
    ("camp", "露营", ["露营地", "露营"]),
    ("outdoor", "户外", ["郊野公园", "森林公园", "湿地公园", "徒步登山", "绿道", "风景区"]),
    ("kids", "遛娃", ["亲子乐园", "儿童乐园", "动物园", "亲子农场", "采摘园"]),
    ("indoor", "雨天室内", ["室内儿童乐园", "科技馆", "博物馆", "室内游乐场"]),
    ("food", "吃喝", ["农家乐", "亲子餐厅", "特色餐厅", "咖啡馆"]),
]
AMAP_BUDGET = 100                                # 每次运行最多调用次数（个人开发者免费额度有限）
REFRESH_DAYS = {DEFAULT_DISTRICT: 1}             # 余杭每天刷新，其余区 5 天轮一遍
REFRESH_OTHER = 5
KEEP_PER_CAT = 24


def amap_key():
    k = os.environ.get("AMAP_KEY", "").strip()
    if not k and (ROOT / ".amap_key").exists():  # 本地调试用，文件已在 .gitignore
        k = (ROOT / ".amap_key").read_text().strip()
    return k


class AmapQuota(Exception):
    pass


def amap_search(key, keywords, adcode, page=1):
    q = urllib.parse.urlencode({"keywords": keywords, "region": adcode, "city_limit": "true", "page_size": 25,
                                "page_num": page, "show_fields": "business,photos", "key": key})
    d = fetch_json(f"https://restapi.amap.com/v5/place/text?{q}")
    if d.get("status") != "1":
        info = d.get("info", "")
        if "LIMIT" in info or "QUOTA" in info or info in ("INVALID_USER_KEY", "USERKEY_PLAT_NOMATCH"):
            raise AmapQuota(info)
        raise RuntimeError(info)
    return d.get("pois") or []


def num_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def slim_poi(p, cat):
    b = p.get("business") or {}
    photos = [ph["url"].replace("http://", "https://", 1) for ph in (p.get("photos") or []) if ph.get("url")][:3]
    lng, lat = (p.get("location") or ",").split(",")[:2]
    tag = (b.get("tag") or "").split(",")[:3]
    return {
        "id": p["id"], "name": p["name"], "cat": cat, "type": (p.get("type") or "").split("|")[0].split(";")[-1],
        "addr": p.get("address") if isinstance(p.get("address"), str) else "", "area": p.get("adname") or "",
        "lng": lng, "lat": lat, "rating": num_or_none(b.get("rating")),
        "cost": (lambda c: c if c and c < 1000 else None)(num_or_none(b.get("cost"))),   # 人均上千多是会员卡价，不显示
        "open": b.get("opentime_today") or b.get("opentime_week") or "", "tags": [t for t in tag if t],
        "photos": photos,
    }


# 名字像服务设施 / 门店配套的不算推荐地点
NOT_PLACE = re.compile(r"租赁|集散中心|停车场|售票|游客中心|服务中心|管理处|公厕|卫生间|入口|出口|充电站|地铁站|公交站|驾校|培训")


def keep(p, cat):
    if NOT_PLACE.search(p["name"]):
        return False
    if cat == "camp" and not re.search(r"露营|营地|帐篷|房车", p["name"] + p["type"]):
        return False
    return True


def rank(places):
    # 有评分的按评分，评分相同看有无人均；没评分的（公园、绿道常见）排后面但保留
    return sorted(places, key=lambda x: (-(x["rating"] or 3.9), -len(x["photos"])))


def places():
    key = amap_key()
    old = {}
    try:
        old = json.loads(PLACES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    dists = old.get("districts", {})
    if not key:
        print("✗ 地点推荐: 没有 AMAP_KEY，沿用旧数据")
        return
    today = now_cst().date()

    def due(code):
        last = dists.get(code, {}).get("updated")
        days = REFRESH_DAYS.get(code, REFRESH_OTHER)
        return not last or (today - date.fromisoformat(last[:10])).days >= days

    # 余杭优先，其余按上次刷新时间从旧到新
    order = [c for c, _ in DISTRICTS if due(c)]
    order.sort(key=lambda c: (c != DEFAULT_DISTRICT, dists.get(c, {}).get("updated") or ""))
    calls, done, err = 0, [], None
    names = dict(DISTRICTS)
    for code in order:
        need = sum(len(k) for _, _, k in PLACE_CATS)
        if calls + need > AMAP_BUDGET:
            break
        cats, seen = {}, set()
        try:
            for cid, _, kws in PLACE_CATS:
                found = []
                for kw in kws:
                    for p in amap_search(key, kw, code):
                        if p["id"] in seen or not p.get("photos"):
                            continue
                        x = slim_poi(p, cid)
                        if not keep(x, cid):
                            continue
                        seen.add(p["id"])                 # 同一地点只归入第一个命中的类别
                        found.append(x)
                    calls += 1
                    time.sleep(0.35)                      # 个人开发者 QPS 很低
                cats[cid] = rank(found)[:KEEP_PER_CAT]
        except AmapQuota as e:
            err = f"高德额度 / key 问题：{e}"
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:120]
            continue
        dists[code] = {"name": names[code], "updated": now_cst().isoformat(timespec="seconds"), "cats": cats}
        done.append(names[code])
    for d in dists.values():                      # 过滤规则调整后，旧数据也按新规则筛一遍
        d["cats"] = {cid: [x for x in lst if keep(x, cid)] for cid, lst in d.get("cats", {}).items()}
    out = {
        "updated": now_cst().isoformat(timespec="seconds") if done else old.get("updated"),
        "default": DEFAULT_DISTRICT,
        "districts_order": [c for c, _ in DISTRICTS],
        "cats": [{"id": c, "name": n} for c, n, _ in PLACE_CATS],
        "districts": dists,
        "status": {"ok": err is None, "error": err, "calls": calls, "refreshed": done},
    }
    PLACES_FILE.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{'✓' if err is None else '✗'} 地点推荐: 刷新 {'、'.join(done) or '无'}，调用 {calls} 次" + (f"，{err}" if err else ""))


if __name__ == "__main__":
    main()
