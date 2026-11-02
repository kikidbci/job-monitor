"""
Monitor de ofertas en webs oficiales.
  python monitor.py                 -> ejecución normal (la que usa GitHub Actions)
  python monitor.py --check         -> prueba cada fuente y dice cuáles funcionan
  python monitor.py --discover URL  -> detecta qué sistema usa una página de empleo
  python monitor.py --ranking       -> top 100 de empresas que más puestos han publicado desde que el monitor funciona
  python monitor.py --page          -> regenera docs/index.html
"""
import datetime, html, json, os, re, sys, urllib.request
from urllib.parse import urljoin, urlencode

CFG = json.load(open("config.json", encoding="utf-8"))
SEEN_FILE = "seen.json"
DATA_FILE = "data.json"
TODAY = datetime.date.today().isoformat()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}


def http(url, data=None, raw=False):
    headers, body = dict(UA), None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        content = r.read()
    return content.decode("utf-8", "replace") if raw else json.loads(content)


# ---------- Conectores ----------
def greenhouse(c):
    d = http(f"https://boards-api.greenhouse.io/v1/boards/{c['id']}/jobs?content=true")
    for j in d.get("jobs", []):
        yield {"id": f"gh:{c['id']}:{j['id']}", "company": c.get("name", c["id"]), "title": j["title"],
               "location": (j.get("location") or {}).get("name", ""), "url": j["absolute_url"],
               "text": j.get("content", "")}


def lever(c):
    host = "api.eu.lever.co" if c.get("eu") else "api.lever.co"
    for j in http(f"https://{host}/v0/postings/{c['id']}?mode=json"):
        yield {"id": f"lv:{c['id']}:{j['id']}", "company": c.get("name", c["id"]), "title": j["text"],
               "location": (j.get("categories") or {}).get("location") or "", "url": j["hostedUrl"],
               "text": j.get("descriptionPlain", "")}


def workday(c):
    base = f"https://{c['host']}/wday/cxs/{c['tenant']}/{c['site']}/jobs"
    offset = 0
    while True:
        d = http(base, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": c.get("search", "")})
        posts = d.get("jobPostings", [])
        if not posts:
            break
        for j in posts:
            yield {"id": f"wd:{c['tenant']}:{j['externalPath']}", "company": c.get("name", c["tenant"]),
                   "title": j["title"], "location": j.get("locationsText", ""),
                   "url": f"https://{c['host']}/en-US/{c['site']}{j['externalPath']}", "text": ""}
        offset += 20
        if offset >= d.get("total", 0) or offset > 400:
            break


A_RE = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def html_links(c):
    """Para webs propias sin API: lee la página de listado y recoge los enlaces que contienen link_contains."""
    page = http(c["url"], raw=True)
    found = {}
    for href, inner in A_RE.findall(page):
        if c["link_contains"] not in href:
            continue
        url = urljoin(c["url"], href.split("#")[0])
        if url.rstrip("/") == c["url"].rstrip("/"):
            continue
        title = clean(inner) or url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        found.setdefault(url, title)
    for url, title in found.items():
        yield {"id": f"html:{url}", "company": c.get("name", c["url"]), "title": title,
               "location": "", "url": url, "text": ""}


def adzuna(c):
    """Agregador (cubre cualquier empresa, pero publica más tarde que la web oficial). Necesita ADZUNA_APP_ID / ADZUNA_APP_KEY."""
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        raise RuntimeError("faltan ADZUNA_APP_ID / ADZUNA_APP_KEY")
    country = c.get("country", "es")
    for page in range(1, c.get("pages", 3) + 1):
        q = {"app_id": app_id, "app_key": app_key, "results_per_page": 50, "sort_by": "date",
             "max_days_old": c.get("max_days_old", 30), "what_or": c["what_or"], "content-type": "application/json"}
        if c.get("where"):
            q["where"] = c["where"]
        d = http(f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?{urlencode(q)}")
        results = d.get("results", [])
        if not results:
            break
        for j in results:
            text = j.get("description", "")
            if str(j.get("salary_is_predicted")) != "1" and (j.get("salary_min") or j.get("salary_max")):
                vals = [f"{int(v):,}".replace(",", ".") for v in (j.get("salary_min"), j.get("salary_max")) if v]
                text += " Salario publicado: " + " - ".join(vals) + " €"
            yield {"id": f"az:{j['id']}", "company": (j.get("company") or {}).get("display_name", "?"),
                   "title": j.get("title", ""), "location": (j.get("location") or {}).get("display_name", ""),
                   "url": j.get("redirect_url", ""), "text": text}


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "workday": workday, "html": html_links, "adzuna": adzuna}


def label(c):
    return c.get("name") or c.get("id") or c.get("url") or c.get("tenant") or "?"


# ---------- Filtros (laxos a propósito) ----------
USD_RE = re.compile(r"\$\s?(\d{2,3})[,.](\d{3})")
DOT_RE = re.compile(r"\b(\d{2,3})\.(\d{3})\b")


def money(text):
    text = text or ""
    usd = [int(a + b) for a, b in USD_RE.findall(text)]
    eur = []
    if re.search(r"€|\beuros?\b|\beur\b", text, re.I):
        eur = [int(a + b) for a, b in DOT_RE.findall(text)]
    return usd, eur


def has(text, kws):
    """Coincidencia por inicio de palabra ('contab' casa con 'contable'; 'us tax' no casa con 'bonus tax').
    Un filtro que empieza por 're:' se trata como expresión regular."""
    text = text.lower()
    for k in kws:
        pat = k[3:] if k.startswith("re:") else r"(?<!\w)" + re.escape(k.lower())
        if re.search(pat, text):
            return True
    return False


def tier(j, strict=True):
    """strict=False ignora ubicación y salario (se usa para el ranking de empresas).
    Devuelve 'priority' (US tax), 'general' (contabilidad/fiscal) o None (descartar)."""
    title = j["title"]
    blob = title + " " + (j["text"] or "")
    if has(title, CFG.get("keywords_exclude", [])):
        return None
    general = has(title, CFG["keywords_any"])
    prio_kws = CFG.get("keywords_priority", [])
    prio = has(title, prio_kws) or (general and has(blob, prio_kws))
    if not (general or prio):
        return None
    if not strict:
        return "priority" if prio else "general"
    loc = j["location"].lower()
    # si la fuente no informa de ubicación, no descartamos (mejor un aviso de más que perder uno bueno)
    if loc and CFG.get("locations_any") and not has(loc, CFG["locations_any"]):
        return None
    usd, eur = money(j["text"])
    if usd and max(usd) < CFG.get("min_salary_usd", 0):
        return None
    if eur and max(eur) < CFG.get("min_salary_eur", 0):
        return None
    return "priority" if prio else "general"


def matches(j):
    return tier(j) is not None


# ---------- ntfy ----------
def notify(title, message, url=None, priority=3):
    if not NTFY_TOPIC:
        print("[sin NTFY_TOPIC]", title, "|", message)
        return
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message, "priority": priority}
    if url:
        payload["click"] = url
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()


# ---------- Datos y página web ----------
def load_data():
    if os.path.exists(DATA_FILE):
        return json.load(open(DATA_FILE, encoding="utf-8"))
    return {"jobs": [], "stats": {}}


def is_agency(name):
    return has(name, CFG.get("agencias", []))


def record_stat(data, j):
    st = data["stats"].setdefault(j["company"], {"total": 0, "months": {}})
    st["total"] += 1
    st["months"][TODAY[:7]] = st["months"].get(TODAY[:7], 0) + 1


def ranking(data, n=100):
    rows = [{"name": k, "total": v["total"], "agency": is_agency(k)} for k, v in data["stats"].items()]
    return sorted(rows, key=lambda r: -r["total"])[:n]


def build_page(data):
    if not os.path.exists("template.html"):
        return
    jobs = sorted(data["jobs"], key=lambda x: x["first_seen"], reverse=True)
    jobs.sort(key=lambda x: x["tier"] != "priority")
    payload = {"updated": TODAY, "jobs": jobs, "companies": ranking(data)}
    blob = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    os.makedirs("docs", exist_ok=True)
    open("docs/index.html", "w", encoding="utf-8").write(open("template.html", encoding="utf-8").read().replace("/*DATA*/null", blob))


# ---------- Modos ----------
def check():
    for c in CFG["companies"]:
        try:
            jobs = list(FETCHERS[c["ats"]](c))
        except Exception as e:
            print(f"❌ {label(c)}: ERROR {e}")
            continue
        tiers = [(tier(j), j) for j in jobs]
        hits = [(t, j) for t, j in tiers if t]
        n_prio = sum(1 for t, _ in hits if t == "priority")
        print(f"{'✅' if jobs else '⚠️ '} {label(c)}: {len(jobs)} ofertas leídas, {len(hits)} encajan ({n_prio} de US tax)")
        for t, j in (sorted(hits, key=lambda x: x[0] != "priority") or [(None, j) for j in jobs])[:3]:
            tag = "🇺🇸" if t == "priority" else "  "
            print(f"     {tag} {j['title']} | {j['location'] or '(sin ubicación)'} | {j['url']}")
        if not jobs:
            print("     0 ofertas: la URL/conector no sirve o la web carga el listado con JavaScript")


SIGS = [
    ("workday", r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Za-z]{2}/)?([A-Za-z0-9_-]+)"),
    ("greenhouse", r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)"),
    ("greenhouse", r"greenhouse\.io/(?!embed)([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.(eu\.)?lever\.co/([A-Za-z0-9_-]+)"),
]
OTHERS = ["ashbyhq.com", "smartrecruiters.com", "personio.", "teamtailor.com", "recruitee.com", "workable.com",
          "successfactors", "taleo.net", "icims.com", "avature.net", "phenom", "eightfold.ai", "jobvite.com",
          "factorialhr", "hiringroom", "oraclecloud.com"]


def discover(url):
    page = http(url, raw=True)
    done = False
    for kind, pat in SIGS:
        m = re.search(pat, page)
        if not m:
            continue
        g = m.groups()
        if kind == "workday":
            print(json.dumps({"ats": "workday", "host": f"{g[0]}.{g[1]}.myworkdayjobs.com", "tenant": g[0],
                              "site": g[2], "name": g[0]}))
        elif kind == "greenhouse":
            print(json.dumps({"ats": "greenhouse", "id": g[0], "name": g[0]}))
        else:
            print(json.dumps({"ats": "lever", "id": g[1], **({"eu": True} if g[0] else {}), "name": g[1]}))
        done = True
        break
    seen_others = [o for o in OTHERS if o in page.lower()]
    if seen_others:
        print("Sistemas detectados sin conector todavía:", ", ".join(seen_others))
    if not done and not seen_others:
        print("No se detectó ningún sistema conocido en el HTML (puede cargarse con JavaScript).")
        print("Abre la web, pulsa F12 > Network, recarga y busca peticiones con 'jobs', 'search' o 'wday'.")


def main():
    first_run = not os.path.exists(SEEN_FILE)
    seen = set(json.load(open(SEEN_FILE))) if not first_run else set()
    data = load_data()
    new_matches, broken, total = [], [], 0
    for c in CFG["companies"]:
        key = "broken:" + label(c)
        try:
            jobs = list(FETCHERS[c["ats"]](c))
            if not jobs:
                raise RuntimeError("0 ofertas leídas (¿URL o conector incorrectos?)")
        except Exception as e:
            print(f"ERROR {label(c)}: {e}", file=sys.stderr)
            if key not in seen:
                seen.add(key)
                broken.append(f"{label(c)}: {e}")
            continue
        seen.discard(key)
        for j in jobs:
            total += 1
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            if tier(j, strict=False):
                record_stat(data, j)
            t = tier(j)
            if t:
                new_matches.append((t, j))
                usd, eur = money(j["text"])
                sal = f"{min(eur):,}-{max(eur):,} €".replace(",", ".") if eur else (f"${min(usd):,}-${max(usd):,}" if usd else "")
                data["jobs"].append({"id": j["id"], "company": j["company"], "title": j["title"], "location": j["location"],
                                     "url": j["url"], "tier": t, "salary": sal, "first_seen": TODAY})
    if first_run:
        msg = f"{total} ofertas indexadas. Desde ahora solo aviso de las nuevas."
        if broken:
            msg += "\nFuentes con problemas: " + "; ".join(broken)
        notify("Monitor de empleo activo", msg)
    else:
        for t, j in sorted(new_matches, key=lambda x: x[0] != "priority"):
            usd, eur = money(j["text"])
            extra = ""
            if eur:
                extra = f"\nSalario en la oferta: {min(eur):,}-{max(eur):,} €".replace(",", ".")
            elif usd:
                extra = f"\nSalario en la oferta: ${min(usd):,}-${max(usd):,}"
            head = f"{j['company']}: {j['title']}"
            if t == "priority":
                notify("🇺🇸 US TAX · " + head, f"{j['location']}{extra}".strip() or j["url"], j["url"], 5)
            else:
                notify(head, f"{j['location']}{extra}".strip() or j["url"], j["url"], 3)
        if broken:
            notify("Fuente de empleo con problemas", "\n".join(broken), priority=2)
    json.dump(sorted(seen), open(SEEN_FILE, "w"))
    data["jobs"] = data["jobs"][-600:]
    json.dump(data, open(DATA_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    build_page(data)
    print(f"{total} ofertas revisadas, {len(new_matches)} nuevas que encajan")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        check()
    elif len(sys.argv) > 2 and sys.argv[1] == "--discover":
        discover(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "--ranking":
        for i, r in enumerate(ranking(load_data()), 1):
            print(f"{i:3}. {r['total']:5}  {r['name']}{'  [intermediaria]' if r['agency'] else ''}")
    elif len(sys.argv) > 1 and sys.argv[1] == "--page":
        build_page(load_data())
    else:
        main()
