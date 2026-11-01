"""Topics: clusters of related memories, found from their embeddings and then named.

Clustering is spherical k-means (cosine) over the Voyage vectors. Each cluster gets keywords
from a TF-IDF-style score across clusters, and a short human label from the LLM, which only
ever sees memory titles. If the LLM is unavailable the keywords become the label.
"""

import html
import json
import math
import re
from collections import Counter

import numpy as np
from groq import AsyncGroq

from app.core.config import get_settings

settings = get_settings()

MIN_ITEMS_FOR_TOPICS = 6
MAX_TOPICS = 8
_KMEANS_ITERATIONS = 40
_LABEL_SAMPLE_TITLES = 8
_LABEL_MAX_CHARS = 40

_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9'&-]{2,}")
_STOPWORDS = set(
    """
    the and for with from this that have will are was our has can not all new get now just more
    out about into what when how here there their they them then than been also its only over
    some any each via per your you yours ours his her she him who whom which while were would
    could should shall may might must did does doing done had having let lets use using used
    one two three first last next other such very much many most make made like want need know
    http https www com org net html email emails gmail mail inbox unsubscribe view click link
    please thanks thank regards hi hello dear team today tomorrow yesterday week weeks day days
    month months year years time am pm utc update updates re fwd fw no-reply noreply reply
    sent message messages account free off see don't can't won't it's i'm you're we're up
    subject date via cc bcc
    """.split()
)


def cluster_count(n_items: int) -> int:
    if n_items < MIN_ITEMS_FOR_TOPICS:
        return 0
    return max(2, min(MAX_TOPICS, round(math.sqrt(n_items / 2))))


_KMEANS_RESTARTS = 6


def kmeans(vectors: np.ndarray, k: int) -> np.ndarray:
    """Spherical k-means, best of several k-means++ restarts (a single unlucky seeding can
    merge two themes and split another). Seeds are fixed, so the same memories always give
    the same topics. Returns one cluster index per row."""
    x = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    best_labels, best_score = None, -np.inf
    for seed in range(_KMEANS_RESTARTS):
        labels, score = _kmeans_once(x, k, seed)
        if score > best_score:
            best_labels, best_score = labels, score
    assert best_labels is not None
    return best_labels


def _kmeans_once(x: np.ndarray, k: int, seed: int) -> tuple[np.ndarray, float]:
    """One run on unit vectors; score = total cosine similarity to assigned centers."""
    rng = np.random.default_rng(seed)
    centers = [x[rng.integers(len(x))]]
    for _ in range(1, k):
        distance = 1 - np.max(x @ np.stack(centers).T, axis=1)
        distance = np.clip(distance, 0, None) ** 2
        total = distance.sum()
        idx = rng.choice(len(x), p=distance / total) if total > 0 else rng.integers(len(x))
        centers.append(x[idx])
    c = np.stack(centers)

    labels = np.zeros(len(x), dtype=int)
    for _ in range(_KMEANS_ITERATIONS):
        new_labels = np.argmax(x @ c.T, axis=1)
        if np.array_equal(new_labels, labels) and _ > 0:
            break
        labels = new_labels
        for j in range(k):
            members = x[labels == j]
            if len(members):
                mean = members.mean(axis=0)
                c[j] = mean / max(float(np.linalg.norm(mean)), 1e-12)
    score = float(np.sum(np.max(x @ c.T, axis=1)))
    return labels, score


def _tokens(text: str, ignore: set[str]) -> list[str]:
    # Gmail snippets arrive HTML-escaped (&#39; …); unescape so entities don't become "words".
    words = (m.lower().strip("'&-") for m in _WORD.findall(html.unescape(text)))
    words = (w[:-2] if w.endswith("'s") else w for w in words)  # "india's" → "india"
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS and w not in ignore]


def cluster_keywords(
    texts_by_cluster: list[list[str]], top: int = 3, ignore: set[str] | None = None
) -> list[list[str]]:
    """Words frequent inside a cluster but rare in the others. `ignore` drops words like the
    user's own name/username, which would otherwise top every cluster of their sent mail."""
    ignore = {w.lower() for w in ignore or set()}
    counts = [
        Counter(t for text in texts for t in set(_tokens(text, ignore)))
        for texts in texts_by_cluster
    ]
    k = len(counts)
    doc_freq = Counter(word for c in counts for word in c)
    keywords = []
    for c in counts:
        scored = sorted(
            ((n * math.log((1 + k) / doc_freq[w]), w) for w, n in c.items() if n >= 2 or k == 1),
            reverse=True,
        )
        keywords.append([w for _, w in scored[:top]])
    return keywords


def keyword_label(keywords: list[str]) -> str:
    return " · ".join(w.capitalize() for w in keywords[:2]) or "Misc"


_LABEL_PROMPT = (
    "You name topic clusters in someone's personal knowledge graph. For each numbered cluster, "
    "reply with a short, specific label of 1 to 3 words in Title Case describing what its items "
    "have in common (e.g. 'Mutual Funds', 'AI Tools', 'Video Editing'). Avoid generic labels "
    "like 'Emails' or 'Updates'. Reply with JSON only, exactly: "
    '{"labels": ["...", "..."]} with one label per cluster, in order.'
)


async def llm_labels(titles_by_cluster: list[list[str]], keywords: list[list[str]]) -> list[str]:
    """One LLM call names every cluster. Falls back to keyword labels per cluster on any
    failure (no API key, network error, malformed reply)."""
    fallback = [keyword_label(k) for k in keywords]
    if not settings.groq_api_key:
        return fallback
    blocks = []
    for i, (titles, words) in enumerate(zip(titles_by_cluster, keywords, strict=True), start=1):
        sample = "\n".join(f"- {t[:120]}" for t in titles[:_LABEL_SAMPLE_TITLES])
        blocks.append(f"Cluster {i} (keywords: {', '.join(words) or 'none'}):\n{sample}")
    try:
        response = await AsyncGroq(api_key=settings.groq_api_key).chat.completions.create(
            model=settings.groq_chat_model,
            messages=[
                {"role": "system", "content": _LABEL_PROMPT},
                {"role": "user", "content": "\n\n".join(blocks)},
            ],
            temperature=0.2,
        )
        raw = response.choices[0].message.content or ""
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        labels = json.loads(match.group(0))["labels"] if match else []
    except Exception:
        return fallback
    if not isinstance(labels, list) or len(labels) != len(fallback):
        return fallback
    return [
        str(label).strip().strip("\"'")[:_LABEL_MAX_CHARS] or fallback[i]
        if isinstance(label, str)
        else fallback[i]
        for i, label in enumerate(labels)
    ]
