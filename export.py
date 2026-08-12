"""Export conjugations.db to static JSON files, to measure whether this app
can ship without a backend (static hosting only).

Produces:
  export/verbs/<infinitive>.json  -- full conjugation table per verb
  export/verbs/index.json         -- sorted list of all infinitives (autocomplete)
  export/idx/slots.json           -- [mood, tense, person, number, gender] lookup table
  export/idx/verbs.json           -- infinitive lookup table, index = verb_id
  export/idx/translations.json    -- English gloss lookup table, index = verb_id
  export/idx/<shard>.json         -- reverse index: form -> [[verb_id, slot_id], ...]

Run directly: python export.py
"""

import gzip
import itertools
import json
import logging
import sqlite3
import unicodedata
from pathlib import Path

DB_PATH = Path(__file__).parent / "conjugations.db"
EXPORT_DIR = Path(__file__).parent / "export"
VERBS_DIR = EXPORT_DIR / "verbs"
IDX_DIR = EXPORT_DIR / "idx"
SHARD_SIZE_LIMIT = 300_000  # bytes, raw JSON; oversized shards get split by 2 chars


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("export")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(handler)
    return logger


def shard_key(form: str, length: int) -> str:
    """First `length` chars of `form`, lowercased, accents folded to base ASCII
    letters, for stable file bucketing. A non-letter first character (e.g. the
    imperativo '-' placeholder) goes to '_other'. Non-letter characters *after*
    the first (e.g. the space in self-generated compound forms like "ho parlato")
    are replaced with '_' rather than collapsing the whole key -- otherwise every
    compound form sharing an auxiliary would be indistinguishable at any depth,
    since the space appears at the same fixed position for all of them."""
    chars = []
    for i, ch in enumerate(form[:length].lower()):
        decomposed = unicodedata.normalize("NFKD", ch)
        base = "".join(c for c in decomposed if not unicodedata.combining(c))
        if base and base.isalpha() and base.isascii():
            chars.append(base)
        elif i == 0:
            return "_other"
        else:
            chars.append("_")
    return "".join(chars) if chars else "_other"


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def export_verbs(conn: sqlite3.Connection, logger: logging.Logger) -> dict:
    """Writes per-verb display JSON + autocomplete index. Returns {verb_id: infinitive}."""
    verb_rows = conn.execute("SELECT id, infinitive FROM verbs ORDER BY id").fetchall()
    verb_infinitive = {vid: inf for vid, inf in verb_rows}

    cursor = conn.execute(
        "SELECT verb_id, mood, tense, person, number, gender, pronoun, form "
        "FROM conjugations ORDER BY verb_id, mood, tense, person, number, gender"
    )
    count = 0
    for verb_id, rows in itertools.groupby(cursor, key=lambda r: r[0]):
        entries = [
            {
                "mood": mood,
                "tense": tense,
                "person": person,
                "number": number,
                "gender": gender,
                "pronoun": pronoun,
                "form": form,
            }
            for (_, mood, tense, person, number, gender, pronoun, form) in rows
        ]
        infinitive = verb_infinitive[verb_id]
        write_json(VERBS_DIR / f"{infinitive}.json", entries)
        count += 1

    write_json(VERBS_DIR / "index.json", sorted(verb_infinitive.values()))
    logger.info("Wrote %d per-verb files + index.json", count)
    return verb_infinitive


def build_reverse_index(conn: sqlite3.Connection, logger: logging.Logger) -> None:
    # Seed the slot table from the real distinct combinations first, so it's
    # guaranteed to cover everything actually in the data; collapsed
    # (gender-doublet) slots get appended afterward as they're encountered.
    slots = []
    slot_index = {}
    for mood, tense, person, number, gender in conn.execute(
        "SELECT DISTINCT mood, tense, person, number, gender FROM conjugations "
        "ORDER BY mood, tense, person, number, gender"
    ):
        key = (mood, tense, person, number, gender)
        slot_index[key] = len(slots)
        slots.append([mood, tense, person, number, gender])

    def get_slot_id(mood, tense, person, number, gender):
        key = (mood, tense, person, number, gender)
        sid = slot_index.get(key)
        if sid is None:
            sid = len(slots)
            slot_index[key] = sid
            slots.append([mood, tense, person, number, gender])
        return sid

    # form -> list of [verb_id, slot_id]
    reverse_index: dict = {}
    doublets_collapsed = 0

    cursor = conn.execute(
        "SELECT verb_id, mood, tense, person, number, gender, form "
        "FROM conjugations ORDER BY verb_id, mood, tense, person, number, gender"
    )
    for verb_id, verb_rows in itertools.groupby(cursor, key=lambda r: r[0]):
        for (mood, tense, person, number), group in itertools.groupby(
            verb_rows, key=lambda r: (r[1], r[2], r[3], r[4])
        ):
            entries = list(group)  # [(verb_id, mood, tense, person, number, gender, form), ...]
            if (
                len(entries) == 2
                and {e[5] for e in entries} == {"m", "f"}
                and entries[0][6] == entries[1][6]
            ):
                # Same form regardless of gender (e.g. "deve" for lui and lei) -- one entry.
                form = entries[0][6]
                slot_id = get_slot_id(mood, tense, person, number, None)
                reverse_index.setdefault(form, []).append([verb_id, slot_id])
                doublets_collapsed += 1
            else:
                for _, _, _, _, _, gender, form in entries:
                    slot_id = get_slot_id(mood, tense, person, number, gender)
                    reverse_index.setdefault(form, []).append([verb_id, slot_id])

    logger.info("Reverse index: %d distinct forms, %d gender-doublets collapsed", len(reverse_index), doublets_collapsed)

    write_json(IDX_DIR / "slots.json", slots)
    verb_count = conn.execute("SELECT COUNT(*) FROM verbs").fetchone()[0]
    max_id = conn.execute("SELECT MAX(id) FROM verbs").fetchone()[0]
    if verb_count != max_id:
        logger.warning("verbs.id is not a contiguous 1..N range (count=%d, max id=%d); idx/verbs.json will have gaps", verb_count, max_id)
    verbs_array = [None] * (max_id + 1)
    for vid, infinitive in conn.execute("SELECT id, infinitive FROM verbs"):
        verbs_array[vid] = infinitive
    write_json(IDX_DIR / "verbs.json", verbs_array)

    translations_array = [None] * (max_id + 1)
    has_translations_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='translations'"
    ).fetchone()
    translated_count = 0
    if has_translations_table:
        cursor = conn.execute("SELECT verb_id, gloss FROM translations ORDER BY verb_id, rank")
        for vid, rows in itertools.groupby(cursor, key=lambda r: r[0]):
            translations_array[vid] = [gloss for (_, gloss) in rows]
            translated_count += 1
    write_json(IDX_DIR / "translations.json", translations_array)
    logger.info("Wrote translations.json: %d/%d verbs have a gloss", translated_count, verb_count)

    # Recursively split by growing prefix length until every shard fits (or we
    # hit max_depth / can't split further because only one form remains).
    # depth is tracked explicitly rather than via len(prefix): shard_key can
    # return the same string ("_other") for genuinely different inputs, so
    # string length alone isn't a safe recursion-termination signal.
    max_depth = 12
    shards: dict = {}

    def store_shard(key: str, bucket: dict, raw_size: int) -> None:
        final_key = key
        n = 1
        while final_key in shards:
            n += 1
            final_key = f"{key}-{n}"
        shards[final_key] = bucket
        if raw_size > SHARD_SIZE_LIMIT:
            logger.warning("Shard '%s' still %d bytes (over limit) after reaching max depth", final_key, raw_size)

    def recursive_shard(items: list, prefix: str, depth: int) -> None:
        bucket = dict(items)
        raw_size = len(json.dumps(bucket, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if raw_size <= SHARD_SIZE_LIMIT or depth >= max_depth or len(items) <= 1:
            store_shard(prefix if prefix else "_root", bucket, raw_size)
            return
        sub: dict = {}
        for form, refs in items:
            sub.setdefault(shard_key(form, depth + 1), []).append((form, refs))
        for sub_key, sub_items in sub.items():
            recursive_shard(sub_items, sub_key, depth + 1)

    recursive_shard(list(reverse_index.items()), "", 0)

    for key, bucket in shards.items():
        write_json(IDX_DIR / f"{key}.json", bucket)

    write_json(IDX_DIR / "_manifest.json", sorted(shards.keys()))

    logger.info("Wrote %d reverse-index shards (max shard size target: %d bytes)", len(shards), SHARD_SIZE_LIMIT)


def report_sizes(logger: logging.Logger) -> None:
    def sizes(path: Path):
        raw = path.read_bytes()
        gz = gzip.compress(raw, compresslevel=9)
        return len(raw), len(gz)

    logger.info("--- Size report ---")

    idx_shards = [p for p in IDX_DIR.glob("*.json") if p.stem not in ("slots", "verbs", "translations", "_manifest")]
    largest = max(idx_shards, key=lambda p: p.stat().st_size)
    raw, gz = sizes(largest)
    logger.info("Largest idx shard (%s): raw=%d bytes, gzip=%d bytes", largest.name, raw, gz)

    typical = VERBS_DIR / "parlare.json"
    raw, gz = sizes(typical)
    logger.info("Typical verb file (parlare.json): raw=%d bytes, gzip=%d bytes", raw, gz)

    index_file = VERBS_DIR / "index.json"
    raw, gz = sizes(index_file)
    logger.info("verbs/index.json: raw=%d bytes, gzip=%d bytes", raw, gz)

    all_files = list(EXPORT_DIR.rglob("*.json"))
    total_raw = sum(p.stat().st_size for p in all_files)
    logger.info("Total export/: %d files, %d bytes raw (%.1f MB)", len(all_files), total_raw, total_raw / 1_000_000)


def main() -> None:
    logger = setup_logger()
    conn = sqlite3.connect(DB_PATH)

    export_verbs(conn, logger)
    build_reverse_index(conn, logger)
    conn.close()

    report_sizes(logger)


if __name__ == "__main__":
    main()
