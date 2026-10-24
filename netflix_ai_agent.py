"""OpenAI-assisted Netflix title matching; proposals require manual approval."""
import json
import os
import urllib.request


def suggest_match(title, candidates, episodes):
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY absente du conteneur")
    instructions = (
        "Tu aides à identifier un visionnement Netflix parmi les résultats TMDB. "
        "Retourne un objet JSON avec kind, tmdb_id, season, episode, confidence, reason. "
        "Utilise uniquement des identifiants fournis dans candidates et des épisodes "
        "fournis dans episodes. Si aucune correspondance fiable, kind et tmdb_id sont null. "
        "Ne devine pas. Explique brièvement en français."
    )
    body = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        "instructions": instructions,
        "input": json.dumps({"title": title, "candidates": candidates, "episodes": episodes}, ensure_ascii=False),
        "text": {"format": {"type": "json_object"}},
        "max_output_tokens": 350,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        answer = json.load(response)
    raw = "".join(
        part.get("text", "")
        for entry in answer.get("output", [])
        for part in entry.get("content", [])
        if part.get("type") == "output_text"
    )
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
