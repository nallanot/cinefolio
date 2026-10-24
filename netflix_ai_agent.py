"""Gemini-assisted Netflix title matching; proposals require manual approval."""
import json
import os
import urllib.request
import urllib.error


def suggest_match(title, candidates, episodes):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY absente du conteneur")
    instructions = (
        "Tu aides à identifier un visionnement Netflix parmi les résultats TMDB. "
        "Retourne un objet JSON avec kind, tmdb_id, season, episode, confidence, reason. "
        "Utilise uniquement des identifiants fournis dans candidates et des épisodes "
        "fournis dans episodes. Si aucune correspondance fiable, kind et tmdb_id sont null. "
        "Ne devine pas. Explique brièvement en français."
    )
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite").strip()
    body = {
        "systemInstruction": {"parts": [{"text": instructions}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(
            {"title": title, "candidates": candidates, "episodes": episodes}, ensure_ascii=False
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
        with urllib.request.urlopen(req, timeout=90) as response:
            answer = json.load(response)
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
