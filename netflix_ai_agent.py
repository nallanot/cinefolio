"""Gemini-assisted Netflix title matching; proposals require manual approval."""
import json
import os
import urllib.request
import urllib.error
import socket
import urllib.parse
import re
import html


def wikipedia_context(series_title, episode_title):
    """Retrieve public Wikipedia search evidence; never treat snippets as episode IDs."""
    findings = []
    for lang in ("fr", "es", "en"):
        query = series_title + " " + episode_title
        params = urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": 3, "format": "json", "utf8": 1
        })
        url = "https://" + lang + ".wikipedia.org/w/api.php?" + params
        request = urllib.request.Request(url, headers={"User-Agent": "Cinefolio/1.0 (Netflix episode matching; Wikipedia public API)"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.load(response)
            for hit in data.get("query", {}).get("search", []):
                page = hit.get("title", "")
                snippet = html.unescape(re.sub(r"<[^>]*>", " ", hit.get("snippet", "")))
                findings.append({
                    "language": lang, "title": page,
                    "excerpt": snippet[:300],
                    "url": "https://" + lang + ".wikipedia.org/wiki/" + urllib.parse.quote(page.replace(" ", "_"))
                })
        except (OSError, ValueError, KeyError):
            continue
    return findings[:9]



def wikidata_context(series_title, episode_title):
    """Search Wikidata entities and expose labels/aliases as evidence, not invented episode numbers."""
    findings = []
    seen = set()
    for query in (series_title + " " + episode_title, episode_title, series_title):
        for language in ("fr", "es", "en"):
            params = urllib.parse.urlencode({
                "action": "wbsearchentities", "search": query, "language": language,
                "uselang": language, "limit": 5, "format": "json", "type": "item"
            })
            req = urllib.request.Request(
                "https://www.wikidata.org/w/api.php?" + params,
                headers={"User-Agent": "Cinefolio/1.0 (Netflix title matching; contact via project repository)"}
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.load(response)
                for hit in data.get("search", []):
                    qid = hit.get("id", "")
                    if not re.fullmatch(r"Q[1-9][0-9]*", qid) or qid in seen:
                        continue
                    seen.add(qid)
                    findings.append({
                        "qid": qid, "label": hit.get("label", ""),
                        "description": hit.get("description", ""),
                        "matched": hit.get("match", {}).get("text", ""),
                        "url": "https://www.wikidata.org/wiki/" + qid
                    })
            except (OSError, ValueError, KeyError):
                continue
            if len(findings) >= 12:
                return findings
    return findings


def suggest_match(title, candidates, episodes, wikipedia=None, wikidata=None):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY absente du conteneur")
    instructions = (
        "Tu aides à identifier un visionnement Netflix parmi les résultats TMDB. "
        "Retourne un objet JSON avec kind, tmdb_id, season, episode, confidence, reason. "
        "Utilise uniquement des identifiants fournis dans candidates et des épisodes "
        "fournis dans episodes. Si aucune correspondance fiable, kind et tmdb_id sont null. "
        "Wikipédia et Wikidata apportent des titres alternatifs et du contexte, mais leurs résultats de recherche ne prouvent pas un numéro d’épisode. "
        "Ne devine jamais le numéro d’épisode. Si le titre d’épisode ne correspond pas clairement à un nom fourni, retourne kind et tmdb_id null. N’utilise jamais S1E1 comme valeur par défaut. Explique brièvement en français."
    )
    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
    body = {
        "systemInstruction": {"parts": [{"text": instructions}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(
            {"title": title, "candidates": candidates, "episodes": episodes, "wikipedia": wikipedia or [], "wikidata": wikidata or []}, ensure_ascii=False
        )}]}],
        "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 700},
    }
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            answer = json.load(response)
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError("Gemini : délai de réponse dépassé (30 s)") from exc
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            error_body = json.load(exc)
            detail = str(error_body.get("error", {}).get("message", ""))[:250]
        except Exception:
            pass
        raise RuntimeError("Gemini HTTP " + str(exc.code) + (": " + detail if detail else "")) from None
    raw = "".join(
        part.get("text", "")
        for candidate in answer.get("candidates", [])[:1]
        for part in candidate.get("content", {}).get("parts", [])
    )
    if not raw:
        raise RuntimeError("Gemini n'a renvoyé aucun contenu")
    proposal = json.loads(raw)
    kind, tmdb_id = proposal.get("kind"), proposal.get("tmdb_id")
    if kind not in ("tv", "movie") or type(tmdb_id) is not int:
        return None
    if not any(c.get("kind") == kind and c.get("id") == tmdb_id for c in candidates):
        return None
    # Reject arbitrary episode selection even if Gemini invents a high confidence score.
    if kind == "tv":
        suffix = title.split(":", 1)[1].strip() if ":" in title else ""
        season, episode = proposal.get("season"), proposal.get("episode")
        matching = [
            e for e in episodes
            if e.get("id") == tmdb_id and e.get("season") == season and e.get("episode") == episode
        ]
        if not matching or not suffix:
            return None
        import unicodedata
        import re
        def normalize(value):
            value = unicodedata.normalize("NFKD", str(value).casefold())
            value = "".join(ch for ch in value if not unicodedata.combining(ch))
            return re.sub(r"[^a-z0-9]+", " ", value).strip()
        episode_title = normalize(suffix)
        names = [normalize(n) for n in matching[0].get("names", [matching[0].get("name", "")])]
        # Only auto-propose a specific episode when a source title agrees.
        if not any(n and (n == episode_title or (len(n) >= 8 and (n in episode_title or episode_title in n))) for n in names):
            return None
    result = {
        "kind": kind,
        "tmdb_id": tmdb_id,
        "confidence": proposal.get("confidence", "low"),
        "reason": str(proposal.get("reason", ""))[:400],
    }
    if kind == "tv":
        season, episode = proposal.get("season"), proposal.get("episode")
        if not any(
            e.get("id") == tmdb_id and e.get("season") == season and e.get("episode") == episode
            for e in episodes
        ):
            return None
        result.update(season=season, episode=episode)
    return result
