"""Populate English glosses for verbs in conjugations.db, from kaikki.org's
wiktextract-based Italian dictionary extract (senses drawn from English
Wiktionary). Writes up to 3 numbered senses per verb into a `translations`
table -- kept separate from the `verbs` table because unlike its other columns,
this table survives independently of data.py's rebuilds (data.py deletes and
recreates conjugations.db from scratch on every run, so translation data can't
live in a table data.py owns without being wiped alongside it).

Run after data.py, before export.py: python translate.py
"""

import json
import logging
import sqlite3
import urllib.request
from pathlib import Path

DB_PATH = Path(__file__).parent / "conjugations.db"
DICT_CACHE_PATH = Path(__file__).parent / "kaikki-italian.jsonl"
DICT_URL = "https://kaikki.org/dictionary/Italian/kaikki.org-dictionary-Italian.jsonl"
MAX_SENSES_PER_VERB = 3


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("translate")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(handler)
    return logger


def ensure_dictionary_downloaded(logger: logging.Logger) -> None:
    if DICT_CACHE_PATH.exists():
        return
    logger.info("Downloading %s to %s (one-time, ~750MB)...", DICT_URL, DICT_CACHE_PATH)
    urllib.request.urlretrieve(DICT_URL, DICT_CACHE_PATH)
    logger.info("Download complete.")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS translations (
            verb_id  INTEGER NOT NULL REFERENCES verbs(id) ON DELETE CASCADE,
            rank     INTEGER NOT NULL,
            gloss    TEXT NOT NULL,
            UNIQUE (verb_id, rank)
        )
        """
    )


def is_lemma_sense(sense: dict) -> bool:
    """Excludes senses that are just an inflected-form cross-reference (e.g. the
    "fa" entry, tagged 'form-of', glossing "third-person singular present
    indicative of fare") -- wiktextract emits one of those for every conjugated
    form, not just for infinitives, so pos == "verb" alone isn't a strong enough
    filter to isolate the dictionary meaning of the infinitive itself."""
    if sense.get("form_of"):
        return False
    tags = sense.get("tags") or []
    return "form-of" not in tags


def collect_glosses(entries: list) -> list:
    """entries: all pos=="verb" dictionary entries for one infinitive (usually
    one, but homographs from different etymologies produce more than one).
    Returns up to MAX_SENSES_PER_VERB distinct glosses in first-encountered order."""
    glosses = []
    for entry in entries:
        for sense in entry.get("senses", []):
            if not is_lemma_sense(sense):
                continue
            sense_glosses = sense.get("glosses")
            if not sense_glosses:
                continue
            gloss = sense_glosses[-1]
            if "Category:" in gloss:
                # Leaked Wiktionary category-link markup, not an actual gloss
                # (seen on a handful of irregular verbs like "avere").
                continue
            if gloss not in glosses:
                glosses.append(gloss)
            if len(glosses) >= MAX_SENSES_PER_VERB:
                return glosses
    return glosses


def build_translations(infinitive_to_id: dict, logger: logging.Logger) -> dict:
    """Streams the dictionary JSONL (must not be loaded into memory at once --
    it's ~750MB) and returns {verb_id: [gloss, ...]} for every infinitive with
    at least one matched lemma sense."""
    entries_by_word: dict = {}
    with open(DICT_CACHE_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("lang") != "Italian" or entry.get("pos") != "verb":
                continue
            word = entry.get("word")
            if word not in infinitive_to_id:
                continue
            entries_by_word.setdefault(word, []).append(entry)

    translations = {}
    for word, entries in entries_by_word.items():
        glosses = collect_glosses(entries)
        if glosses:
            translations[infinitive_to_id[word]] = glosses
    logger.info("Matched dictionary entries for %d/%d target infinitives", len(entries_by_word), len(infinitive_to_id))
    return translations


def write_translations(conn: sqlite3.Connection, translations: dict) -> None:
    conn.execute("DELETE FROM translations")
    rows = [
        (verb_id, rank, gloss)
        for verb_id, glosses in translations.items()
        for rank, gloss in enumerate(glosses, 1)
    ]
    conn.executemany("INSERT INTO translations (verb_id, rank, gloss) VALUES (?, ?, ?)", rows)
    conn.commit()


def main() -> None:
    logger = setup_logger()
    ensure_dictionary_downloaded(logger)

    conn = sqlite3.connect(DB_PATH)
    create_schema(conn)

    infinitive_to_id = {inf: vid for vid, inf in conn.execute("SELECT id, infinitive FROM verbs")}
    logger.info("Loaded %d infinitives from conjugations.db", len(infinitive_to_id))

    translations = build_translations(infinitive_to_id, logger)
    write_translations(conn, translations)

    unmatched = sorted(set(infinitive_to_id) - {inf for inf, vid in infinitive_to_id.items() if vid in translations})
    unmatched_path = Path(__file__).parent / "translate_unmatched.txt"
    unmatched_path.write_text("\n".join(unmatched), encoding="utf-8")
    logger.info(
        "Coverage: %d/%d verbs got at least one gloss (%.1f%%); %d unmatched infinitives written to %s",
        len(translations),
        len(infinitive_to_id),
        100 * len(translations) / len(infinitive_to_id),
        len(unmatched),
        unmatched_path.name,
    )

    conn.close()


if __name__ == "__main__":
    main()
