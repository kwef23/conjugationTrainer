"""Build conjugations.db: a SQLite database of conjugated Italian verbs, sourced
entirely from verbecc's bundled XML templates (no ML prediction).

Run directly: python data.py
"""

import importlib.resources
import logging
import sqlite3
import time
from pathlib import Path

from lxml import etree

DB_PATH = Path(__file__).parent / "conjugations.db"

# The 7 compound mood/tense buckets are never read from verbecc's own output —
# verbecc's per-verb auxiliary choice is unreliable beyond a handful of verbs
# (e.g. it conjugates "tornare" with avere: "io ho tornato", which is wrong).
# We regenerate these ourselves from CURATED_ESSERE_VERBS + the verb's own
# past participle. See COMPOUND_TENSE_MAP below.
COMPOUND_TENSE_SKIP = frozenset(
    {
        ("indicativo", "passato-prossimo"),
        ("indicativo", "trapassato-prossimo"),
        ("indicativo", "trapassato-remoto"),
        ("indicativo", "futuro-anteriore"),
        ("congiuntivo", "passato"),
        ("congiuntivo", "trapassato"),
        ("condizionale", "passato"),
    }
)

# (target_mood, target_tense, auxiliary_mood, auxiliary_tense) — Italian compound
# tenses are formed as [auxiliary conjugated in the paired simple tense] + [past participle].
COMPOUND_TENSE_MAP = [
    ("indicativo", "passato-prossimo", "indicativo", "presente"),
    ("indicativo", "trapassato-prossimo", "indicativo", "imperfetto"),
    ("indicativo", "trapassato-remoto", "indicativo", "passato-remoto"),
    ("indicativo", "futuro-anteriore", "indicativo", "futuro"),
    ("congiuntivo", "passato", "congiuntivo", "presente"),
    ("congiuntivo", "trapassato", "congiuntivo", "imperfetto"),
    ("condizionale", "passato", "condizionale", "presente"),
]

# Curated list of essere-auxiliary Italian verbs, compiled from standard reference
# grammar (motion verbs, state-change/existence verbs, and their compounds). Not
# exhaustive over all ~7800 XML verbs -- covers what standard Italian courses
# classify as essere-auxiliary, which is what matters for a trainer app.
#
# Genuinely dual-auxiliary verbs (auxiliary depends on transitivity/meaning, not
# just the verb) are forced into one bucket here rather than modeled as "both":
# cambiare, guarire, passare, mancare, risalire, rinvenire, convenire, fuggire,
# affondare. Each is commented below with the reading that was chosen.
CURATED_ESSERE_VERBS = frozenset(
    {
        # essere itself
        "essere",
        # motion verbs
        "andare",
        "arrivare",
        "entrare",
        "partire",
        "uscire",
        "salire",
        "scendere",
        "tornare",
        "cadere",
        "venire",
        # motion compounds
        "ritornare",
        "rientrare",
        "riuscire",
        "fuoriuscire",
        "ripartire",
        "accadere",  # (impersonal "to happen", not literally "to fall")
        "decadere",
        "ricadere",
        "scadere",
        "sottostare",
        "risalire",  # dual-auxiliary; "è risalito" chosen as the default reading
        "discendere",
        "ridiscendere",
        "rinascere",
        # venire-compounds
        "avvenire",
        "convenire",  # dual-auxiliary ("conviene" is often avere); "è convenuto" chosen
        "divenire",
        "intervenire",
        "pervenire",
        "provenire",
        "rinvenire",  # dual-auxiliary; essere reading chosen ("è rinvenuto")
        "sopravvenire",
        "svenire",
        # state-change / existence / "becoming" verbs
        "diventare",
        "nascere",
        "morire",
        "crescere",
        "restare",
        "rimanere",
        "stare",
        "bastare",
        "sembrare",
        "parere",
        "apparire",
        "scomparire",
        "comparire",
        "succedere",
        "piacere",
        "dispiacere",
        "spiacere",
        "mancare",  # dual-auxiliary ("mi è mancato" chosen over "ho mancato")
        "dipendere",
        "bisognare",
        "occorrere",
        "passare",  # dual-auxiliary; "è passato" (motion) chosen over "ha passato" (spend time)
        "cambiare",  # dual-auxiliary; "è cambiato" (intransitive) chosen over "ha cambiato" (transitive)
        "guarire",  # dual-auxiliary; "è guarito" chosen
        "fuggire",  # dual-auxiliary; essere reading chosen
        "affondare",  # dual-auxiliary; "è affondato" (intransitive) chosen
        "dimagrire",
        "ingrassare",
        "invecchiare",
        "arrossire",
        "impallidire",
    }
)


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("build_conjugations_db")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(handler)
    return logger


def disable_ml_prediction(logger: logging.Logger) -> None:
    """Every verb we conjugate is an exact XML-template match, so ML prediction
    is never needed -- disabling it avoids reading the cached ~100MB model zip."""
    try:
        import verbecc.src.defs.types.data.verbs as _verbs_mod

        _verbs_mod.config.ENABLE_ML_PREDICTION = False
    except Exception:
        logger.warning(
            "Could not disable verbecc ML prediction; proceeding anyway "
            "(costs an unnecessary cached-model read, not a correctness issue)."
        )


def load_xml_infinitives() -> list:
    xml_resource = importlib.resources.files("verbecc") / "data" / "xml" / "verbs" / "verbs-it.xml"
    with xml_resource.open("rb") as f:
        tree = etree.parse(f)
    return [v.find("i").text for v in tree.getroot() if v.tag == "v"]


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE verbs (
            id              INTEGER PRIMARY KEY,
            infinitive      TEXT NOT NULL UNIQUE,
            template        TEXT NOT NULL,
            stem            TEXT,
            auxiliary       TEXT NOT NULL CHECK (auxiliary IN ('avere', 'essere')),
            is_regular      INTEGER,
            translation_en  TEXT,
            frequency_rank  INTEGER CHECK (frequency_rank IS NULL OR frequency_rank BETWEEN 1 AND 10)
        );

        CREATE TABLE conjugations (
            id       INTEGER PRIMARY KEY,
            verb_id  INTEGER NOT NULL REFERENCES verbs(id) ON DELETE CASCADE,
            mood     TEXT NOT NULL,
            tense    TEXT NOT NULL,
            person   TEXT,
            number   TEXT,
            gender   TEXT,
            pronoun  TEXT,
            form     TEXT NOT NULL,
            UNIQUE (verb_id, mood, tense, person, number, gender, pronoun)
        );

        CREATE INDEX idx_conjugations_verb_id ON conjugations(verb_id);
        CREATE INDEX idx_conjugations_verb_mood_tense ON conjugations(verb_id, mood, tense);
        """
    )


def _str_or_none(value) -> "str | None":
    return None if value is None else str(value)


def build_aux_paradigm(cc, infinitive: str) -> dict:
    """Conjugate essere/avere once and extract the 7 simple tenses needed to
    build compound tenses, each as {(person, number, pronoun): bare_form}."""
    needed = {(aux_mood, aux_tense) for (_, _, aux_mood, aux_tense) in COMPOUND_TENSE_MAP}
    result = cc.conjugate(infinitive, conjugate_pronouns=False)
    paradigm = {}
    for mood in result:
        mstr = str(mood)
        mood_conj = result[mood]
        for tense in mood_conj:
            tstr = str(tense)
            if (mstr, tstr) not in needed:
                continue
            entries = {}
            for c in mood_conj[tense]:
                key = (
                    _str_or_none(c.get_person()),
                    _str_or_none(c.get_number()),
                    _str_or_none(c.get_pronoun()),
                )
                entries[key] = c.get_conjugations()[0]
            paradigm[(mstr, tstr)] = entries
    return paradigm


def generate_compound_rows(aux: str, aux_paradigm: dict, participle_forms: dict) -> list:
    rows = []
    if len(participle_forms) != 4:
        return rows
    for target_mood, target_tense, aux_mood, aux_tense in COMPOUND_TENSE_MAP:
        paradigm = aux_paradigm[aux].get((aux_mood, aux_tense))
        if not paradigm:
            continue
        for (person, number, pronoun), aux_form in paradigm.items():
            if aux == "avere":
                p_form = participle_forms.get(("m", "s"))
                if p_form is None:
                    continue
                rows.append((target_mood, target_tense, person, number, None, pronoun, f"{aux_form} {p_form}"))
            else:
                # "lui" is inherently masculine and "lei" inherently feminine, so
                # each only pairs with the matching participle. io/tu/noi/voi/loro
                # don't encode gender in the pronoun itself, so both are given.
                if pronoun == "lui":
                    genders = ("m",)
                elif pronoun == "lei":
                    genders = ("f",)
                else:
                    genders = ("m", "f")
                for gender in genders:
                    p_form = participle_forms.get((gender, number))
                    if p_form is None:
                        continue
                    rows.append((target_mood, target_tense, person, number, gender, pronoun, f"{aux_form} {p_form}"))
    return rows


def process_verb(cc, infinitive: str, aux_paradigm: dict, logger: logging.Logger, stats: dict):
    try:
        result = cc.conjugate(infinitive, conjugate_pronouns=False)
    except Exception as exc:
        stats["skipped"] += 1
        stats["skip_by_type"][type(exc).__name__] = stats["skip_by_type"].get(type(exc).__name__, 0) + 1
        logger.warning("Skipping verb %r: %s: %s", infinitive, type(exc).__name__, exc)
        return None

    verb_info = result.get_verb_info()
    conj_rows = []
    participle_forms = {}  # (gender, number) -> form
    raw_gerund_forms = []

    for mood in result:
        mstr = str(mood)
        mood_conj = result[mood]
        for tense in mood_conj:
            tstr = str(tense)
            if (mstr, tstr) in COMPOUND_TENSE_SKIP:
                continue
            tense_conj = mood_conj[tense]
            if mstr == "infinito" and tstr == "gerundio":
                for c in tense_conj:
                    raw_gerund_forms.append(c.get_conjugations()[0])
                continue
            for c in tense_conj:
                form = c.get_conjugations()[0]
                number = _str_or_none(c.get_number())
                gender = _str_or_none(c.get_gender())
                row = (
                    mstr,
                    tstr,
                    _str_or_none(c.get_person()),
                    number,
                    gender,
                    _str_or_none(c.get_pronoun()),
                    form,
                )
                conj_rows.append(row)
                if mstr == "participio" and tstr == "participio-passato":
                    participle_forms[(gender, number)] = form

    # Derive the true (invariant) gerund from verbecc's buggy 5-entry gerundio
    # bucket, which mixes the infinitive, past participle (twice), and the real
    # gerund (twice) under fake person/pronoun tags.
    exclude = {verb_info.infinitive} | set(participle_forms.values())
    remaining_distinct = {f for f in raw_gerund_forms if f not in exclude}
    if len(remaining_distinct) == 1:
        conj_rows.append(("infinito", "gerundio", None, None, None, None, remaining_distinct.pop()))
    else:
        stats["gerund_failed"] += 1
        logger.warning(
            "Gerund derivation failed for %r: raw=%r distinct_after_filter=%r",
            infinitive,
            raw_gerund_forms,
            remaining_distinct,
        )

    aux = "essere" if infinitive in CURATED_ESSERE_VERBS else "avere"
    if len(participle_forms) != 4:
        logger.warning(
            "Incomplete participio-passato data for %r (%d forms), skipping compound tenses",
            infinitive,
            len(participle_forms),
        )
    conj_rows.extend(generate_compound_rows(aux, aux_paradigm, participle_forms))

    verb_row = (verb_info.infinitive, verb_info.template, verb_info.stem, aux)
    return verb_row, conj_rows


def flush(conn: sqlite3.Connection, buffer: list) -> None:
    if not buffer:
        return
    conn.executemany(
        "INSERT INTO conjugations (verb_id, mood, tense, person, number, gender, pronoun, form) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        buffer,
    )


def verify(conn: sqlite3.Connection, logger: logging.Logger) -> None:
    logger.info("--- Verification ---")

    (verb_count,) = conn.execute("SELECT COUNT(*) FROM verbs").fetchone()
    (conj_count,) = conn.execute("SELECT COUNT(*) FROM conjugations").fetchone()
    logger.info("verbs: %d rows, conjugations: %d rows", verb_count, conj_count)

    for infinitive in ("parlare", "essere", "avere", "andare"):
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM conjugations c JOIN verbs v ON v.id = c.verb_id WHERE v.infinitive = ?",
            (infinitive,),
        ).fetchone()
        logger.info("%s: %d conjugation rows", infinitive, count)

    for infinitive, expected_aux in (("andare", "essere"), ("essere", "essere"), ("parlare", "avere"), ("avere", "avere")):
        row = conn.execute("SELECT auxiliary FROM verbs WHERE infinitive = ?", (infinitive,)).fetchone()
        ok = row is not None and row[0] == expected_aux
        logger.info("auxiliary check %s -> expected %s: %s", infinitive, expected_aux, "OK" if ok else "FAIL (%r)" % (row,))

    # tornare: curated as essere even though verbecc itself conjugates it with avere.
    row = conn.execute(
        "SELECT c.form FROM conjugations c JOIN verbs v ON v.id = c.verb_id "
        "WHERE v.infinitive = 'tornare' AND c.mood = 'indicativo' AND c.tense = 'passato-prossimo' AND c.pronoun = 'io'",
    ).fetchall()
    logger.info("tornare indicativo.passato-prossimo (io) forms: %s (expect essere-based, e.g. 'è tornato')", row)

    bad_essere = conn.execute(
        "SELECT v.infinitive, c.form FROM conjugations c JOIN verbs v ON v.id = c.verb_id "
        "WHERE v.auxiliary = 'essere' AND c.mood IN ('indicativo','congiuntivo','condizionale') "
        "AND c.tense IN ('passato-prossimo','trapassato-prossimo','trapassato-remoto','futuro-anteriore','passato','trapassato') "
        "AND (c.form LIKE 'ho %' OR c.form LIKE 'hai %' OR c.form LIKE 'ha %' "
        "OR c.form LIKE 'abbiamo %' OR c.form LIKE 'avete %' OR c.form LIKE 'hanno %') LIMIT 5"
    ).fetchall()
    logger.info("essere-verbs with avere-shaped compound forms (should be empty): %s", bad_essere)

    bad_avere = conn.execute(
        "SELECT v.infinitive, c.form FROM conjugations c JOIN verbs v ON v.id = c.verb_id "
        "WHERE v.auxiliary = 'avere' AND c.mood IN ('indicativo','congiuntivo','condizionale') "
        "AND c.tense IN ('passato-prossimo','trapassato-prossimo','trapassato-remoto','futuro-anteriore','passato','trapassato') "
        "AND (c.form LIKE 'sono %' OR c.form LIKE 'sei %' OR c.form LIKE 'è %' "
        "OR c.form LIKE 'siamo %' OR c.form LIKE 'siete %') LIMIT 5"
    ).fetchall()
    logger.info("avere-verbs with essere-shaped compound forms (should be empty): %s", bad_avere)

    row = conn.execute(
        "SELECT v.infinitive, c.mood, c.tense, c.pronoun FROM conjugations c JOIN verbs v ON v.id = c.verb_id "
        "WHERE c.form = 'capirò'"
    ).fetchall()
    logger.info("reverse lookup 'capirò' (expect exactly 1 row, capire/indicativo/futuro/io): %s", row)

    for infinitive, expected_gerund in (("fare", "facendo"), ("dire", "dicendo"), ("bere", "bevendo"), ("parlare", "parlando")):
        row = conn.execute(
            "SELECT c.form FROM conjugations c JOIN verbs v ON v.id = c.verb_id "
            "WHERE v.infinitive = ? AND c.mood = 'infinito' AND c.tense = 'gerundio'",
            (infinitive,),
        ).fetchone()
        ok = row is not None and row[0] == expected_gerund
        logger.info("gerund check %s -> expected %s: %s", infinitive, expected_gerund, "OK" if ok else "FAIL (%r)" % (row,))

    (placeholder_count,) = conn.execute(
        "SELECT COUNT(*) FROM verbs WHERE is_regular IS NOT NULL OR translation_en IS NOT NULL OR frequency_rank IS NOT NULL"
    ).fetchone()
    logger.info("placeholder columns populated (should be 0): %d", placeholder_count)

    for infinitive in ("accanirsi", "assentarsi", "cancerizzarsi", "torre", "spengere"):
        row = conn.execute("SELECT 1 FROM verbs WHERE infinitive = ?", (infinitive,)).fetchone()
        logger.info("known-bad verb %s absent: %s", infinitive, row is None)

    (orphan_count,) = conn.execute(
        "SELECT COUNT(*) FROM conjugations WHERE verb_id NOT IN (SELECT id FROM verbs)"
    ).fetchone()
    logger.info("orphaned conjugation rows (should be 0): %d", orphan_count)


def main() -> None:
    logger = setup_logger()
    DB_PATH.unlink(missing_ok=True)

    disable_ml_prediction(logger)
    from verbecc import CompleteConjugator, LangCodeISO639_1 as Lang

    cc = CompleteConjugator(Lang.it)
    aux_paradigm = {
        "avere": build_aux_paradigm(cc, "avere"),
        "essere": build_aux_paradigm(cc, "essere"),
    }

    infinitives = load_xml_infinitives()
    logger.info("Loaded %d infinitives from verbs-it.xml", len(infinitives))

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
    create_schema(conn)

    stats = {"skipped": 0, "skip_by_type": {}, "gerund_failed": 0, "essere": 0, "avere": 0}
    conj_buffer = []
    t0 = time.time()

    for i, infinitive in enumerate(infinitives, 1):
        processed = process_verb(cc, infinitive, aux_paradigm, logger, stats)
        if processed is not None:
            verb_row, conj_rows = processed
            cur = conn.execute(
                "INSERT INTO verbs (infinitive, template, stem, auxiliary) VALUES (?, ?, ?, ?)",
                verb_row,
            )
            verb_id = cur.lastrowid
            conj_buffer.extend((verb_id, *row) for row in conj_rows)
            stats[verb_row[3]] += 1

            if len(conj_buffer) >= 20000:
                flush(conn, conj_buffer)
                conj_buffer.clear()

        if i % 1000 == 0:
            logger.info("Progress: %d/%d verbs processed (%.1fs elapsed)", i, len(infinitives), time.time() - t0)

    flush(conn, conj_buffer)
    conn.commit()

    elapsed = time.time() - t0
    stored = stats["essere"] + stats["avere"]
    logger.info(
        "Build complete in %.1fs: %d verbs stored, %d skipped (%s), %d essere / %d avere, "
        "%d gerund-derivation warnings",
        elapsed,
        stored,
        stats["skipped"],
        stats["skip_by_type"],
        stats["essere"],
        stats["avere"],
        stats["gerund_failed"],
    )

    verify(conn, logger)
    conn.close()


if __name__ == "__main__":
    main()
