"""Standalone brand matcher. No database, network, or third-party imports.

Run from the repository root:
    python brand_detector.py --self-test
    python brand_detector.py --brands brands.txt --name "Grand_Seiko watch"
    python brand_detector.py --brands brands.txt --titles product_titles.txt

An optional aliases JSON file maps approved spellings to canonical names:
    {"Dolce Gabbana": "Dolce & Gabbana"}
Approximate matches are suggestions only; they never assign a brand.
"""

import argparse
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path


def normalize(value: str) -> str:
    """Apply the same normalization to titles, brands, and aliases."""
    if not isinstance(value, str):
        raise TypeError("Text must be a string")
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"(?<=\w)['’](?=\w)", "", value)
    return " ".join(re.sub(r"[\W_]+", " ", value).split())


class BrandDetector:
    """Match whole token phrases; abstain when separate brands occur."""

    def __init__(self, brands: list[str], aliases: dict[str, str] | None = None):
        self.names = {}
        for name in brands:
            self._add(name, name)
        canonical_names = set(self.names.values())
        for alias, canonical in (aliases or {}).items():
            if canonical not in canonical_names:
                raise ValueError(
                    f"Alias target is absent from catalogue: {canonical!r}"
                )
            self._add(alias, canonical)
        self.patterns = [
            (re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"), name)
            for phrase, name in self.names.items()
        ]

    def _add(self, spelling: str, canonical: str) -> None:
        phrase = normalize(spelling)
        if not phrase:
            raise ValueError("Brand spelling must not be empty")
        existing = self.names.get(phrase)
        if existing is not None and existing != canonical:
            raise ValueError(f"Conflicting normalized brand spelling: {spelling!r}")
        self.names[phrase] = canonical

    def detect(self, title: str) -> dict:
        cleaned = normalize(title)
        matches = [
            (match.start(), match.end(), name)
            for pattern, name in self.patterns
            for match in pattern.finditer(cleaned)
        ]
        # Grand Seiko contains Seiko, Hugo Boss contains Boss, etc.
        # Suppress only contained spans, never an unrelated brand elsewhere.
        remaining = [
            (start, end, name)
            for start, end, name in matches
            if not any(
                other_start <= start
                and end <= other_end
                and other_end - other_start > end - start
                for other_start, other_end, _ in matches
            )
        ]
        candidates = sorted({name for _, _, name in remaining})
        return {
            "title": title,
            "brand_name": candidates[0] if len(candidates) == 1 else None,
            "status": (
                "matched"
                if len(candidates) == 1
                else ("ambiguous" if candidates else "unmatched")
            ),
            "candidates": candidates,
        }

    def suggest(self, title: str) -> list[dict]:
        """Suggest a misspelled brand at the title's start for human review."""
        words = normalize(title).split()
        scores = {}
        for phrase, canonical in self.names.items():
            if len(phrase.replace(" ", "")) < 5:
                continue  # A short word's typo is often another ordinary word.
            width = len(phrase.split())
            if len(words) < width:
                continue
            candidate = " ".join(words[:width])
            score = SequenceMatcher(None, phrase, candidate).ratio()
            if score >= 0.88:
                scores[canonical] = max(scores.get(canonical, 0), score)
        return [
            {"brand_name": name, "similarity": round(score * 100, 1)}
            for name, score in sorted(
                scores.items(), key=lambda row: (-row[1], row[0])
            )[:3]
        ]


def load_brands(path: Path) -> list[str]:
    """Read the repository's one-quoted-brand-per-line brands.txt format."""
    return [
        name
        for line in path.read_text(encoding="utf-8").splitlines()
        if (name := line.strip().strip('"').strip())
    ]


def self_test() -> None:
    detector = BrandDetector(
        [
            "Seiko",
            "Grand Seiko",
            "King Seiko",
            "Boss",
            "Hugo Boss",
            "Dior",
            "Christian Dior",
            "Nike",
            "Adidas",
            "Levi's",
            "G-Shock",
            "U.S. Polo Assn.",
            "Dolce & Gabbana",
            "Rolex",
        ],
        {"Dolce Gabbana": "Dolce & Gabbana"},
    )
    cases = [
        ("Grand Seiko watch", "Grand Seiko", "matched"),
        ("King_Seiko watch", "King Seiko", "matched"),
        ("Hugo Boss shirt", "Hugo Boss", "matched"),
        ("Christian Dior bag", "Christian Dior", "matched"),
        ("Embossed leather bag", None, "unmatched"),
        ("Nikel plated watch", None, "unmatched"),
        ("nike shoes", "Nike", "matched"),
        ("ＮＩＫＥ shoes", "Nike", "matched"),
        ("Shoes by Adidas", "Adidas", "matched"),
        ("Levi’s jeans", "Levi's", "matched"),
        ("Levis jeans", "Levi's", "matched"),
        ("G_Shock watch", "G-Shock", "matched"),
        ("U.S. Polo Assn. shirt", "U.S. Polo Assn.", "matched"),
        ("Dolce and Gabbana bag", "Dolce & Gabbana", "matched"),
        ("Dolce Gabbana bag", "Dolce & Gabbana", "matched"),
        ("Nike / Adidas bundle", None, "ambiguous"),
        ("Grand Seiko and Seiko bundle", None, "ambiguous"),
        ("Rolexx watch", None, "unmatched"),
        ("", None, "unmatched"),
        ("Unknown designer shoes", None, "unmatched"),
    ]
    for title, expected_name, expected_status in cases:
        result = detector.detect(title)
        assert (result["brand_name"], result["status"]) == (
            expected_name,
            expected_status,
        ), result
    assert detector.suggest("Rolexx watch")[0]["brand_name"] == "Rolex"
    assert (
        BrandDetector(list(reversed(list(detector.names.values())))).detect(
            "Grand Seiko watch"
        )["brand_name"]
        == "Grand Seiko"
    )
    print(f"Passed {len(cases)} detection cases plus typo-suggestion and order checks.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brands", type=Path, default=Path("brands.txt"))
    parser.add_argument("--aliases", type=Path)
    parser.add_argument("--name", action="append", default=[])
    parser.add_argument("--titles", type=Path, help="One product title per line")
    parser.add_argument("--suggest", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    aliases = (
        json.loads(args.aliases.read_text(encoding="utf-8")) if args.aliases else {}
    )
    if not isinstance(aliases, dict):
        raise TypeError("Aliases must be a JSON dictionary")
    detector = BrandDetector(load_brands(args.brands), aliases)
    titles = args.name + (
        args.titles.read_text(encoding="utf-8").splitlines() if args.titles else []
    )
    if not titles:
        parser.error("Provide --name, --titles, or --self-test")
    for title in titles:
        result = detector.detect(title)
        if args.suggest and result["status"] == "unmatched":
            result["suggestions"] = detector.suggest(title)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
