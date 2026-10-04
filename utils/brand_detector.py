"""Resolve scraped product titles against a supplied canonical brand catalogue."""

import re
import unicodedata

from rapidfuzz import fuzz


def _normalize(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Brand names and product titles must be strings")
    value = unicodedata.normalize("NFKC", value).casefold().replace("&", " and ")
    value = re.sub(r"(?<=\w)['’](?=\w)", "", value)
    return " ".join(re.sub(r"[\W_]+", " ", value).split())


def _one_edit(left: str, right: str) -> bool:
    """Allow one insertion, deletion, substitution, or adjacent transposition."""
    if left == right:
        return False
    if len(left) > len(right):
        left, right = right, left
    if len(right) - len(left) > 1:
        return False
    if len(left) != len(right):
        return any(right[:i] + right[i + 1 :] == left for i in range(len(right)))
    changed = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
    if len(changed) == 1:
        return True
    return (
        len(changed) == 2
        and changed[1] == changed[0] + 1
        and left[changed[0]] == right[changed[1]]
        and left[changed[1]] == right[changed[0]]
    )


class BrandDetector:
    """Match full phrases anywhere; correct one typo in a brand at title start."""

    def __init__(
        self,
        brands: list[str],
        *,
        min_similarity: float = 88,
        min_margin: float = 8,
    ):
        if not 0 < min_similarity <= 100:
            raise ValueError("Minimum brand similarity must be between 0 and 100")
        if not 0 < min_margin <= 100:
            raise ValueError("Minimum brand margin must be between 0 and 100")
        self.min_similarity = min_similarity
        self.min_margin = min_margin
        self.entries = []
        for name in dict.fromkeys(brands):
            phrase = _normalize(name)
            if not phrase:
                continue
            pattern = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)")
            self.entries.append((name, phrase, phrase.split(), pattern))

    @staticmethod
    def _result(title: str, candidates: list[str], selected=None) -> dict:
        return {
            "title": title,
            "brand_name": selected[2] if selected else None,
            "status": (
                "matched" if selected else ("ambiguous" if candidates else "unmatched")
            ),
            "candidates": candidates,
            "method": selected[3] if selected else None,
            "similarity": round(selected[4], 1) if selected else None,
        }

    def detect(self, title: str) -> dict:
        cleaned = _normalize(title)
        words = cleaned.split()
        matches = []
        fuzzy = []
        for name, phrase, brand_words, pattern in self.entries:
            matches.extend(
                (match.start(), match.end(), name, "exact", 100.0)
                for match in pattern.finditer(cleaned)
            )
            prefix = words[: len(brand_words)]
            if len(prefix) != len(brand_words):
                continue
            differences = [(a, b) for a, b in zip(prefix, brand_words) if a != b]
            if len(differences) != 1:
                continue
            supplied, canonical = differences[0]
            if (
                supplied[0] != canonical[0]
                or min(len(supplied), len(canonical)) < 4
                or not _one_edit(supplied, canonical)
            ):
                continue
            candidate = " ".join(prefix)
            score = fuzz.ratio(phrase, candidate)
            fuzzy.append((0, len(candidate), name, "fuzzy", score))

        ranked = sorted(fuzzy, key=lambda match: (-match[4], match[2]))
        exact_prefix = any(match[0] == 0 for match in matches)
        if ranked and ranked[0][4] >= self.min_similarity and not exact_prefix:
            if len(ranked) > 1 and ranked[0][4] - ranked[1][4] < self.min_margin:
                candidates = sorted({match[2] for match in matches + ranked})
                return self._result(title, candidates)
            matches.append(ranked[0])

        # Discard a shorter phrase only when a longer match contains its span.
        remaining = [
            match
            for match in matches
            if not any(
                other[0] <= match[0]
                and match[1] <= other[1]
                and other[1] - other[0] > match[1] - match[0]
                for other in matches
            )
        ]
        candidates = sorted({match[2] for match in remaining})
        selected = (
            max(remaining, key=lambda match: match[4]) if len(candidates) == 1 else None
        )
        return self._result(title, candidates, selected)
