"""Capitalisation-rule named-entity recognition for the reference backend (ML-2, text).

Not a real NER model: it exists so pre-labelling text items can be exercised
end to end without shipping a language model. Runs of capitalised words are
candidate entities; cues around them pick the label:

- ``ORG`` when the run ends in (or contains) an organisation word — ``Inc``,
  ``Ltd``, ``Oy``, ``GmbH``, ``University``, ``Bank`` … — or follows
  ``at`` / ``for``;
- ``LOC`` when it follows ``in`` / ``from`` / ``near`` / ``to``;
- ``PER`` otherwise, for runs of two or more words, or a single word that is
  not the first word of a sentence or line. A common sentence opener ("The",
  "Yesterday", "Dear" …) is dropped from the front of a run.

A single word is never an entity when it is a field label ("Date:"), a month
or weekday, or a short all-caps code ("EUR"); one after a comma or a number
is taken as a place ("Ltd, Tampere", "00100 Helsinki"); in "X and Y" the
second gets the first's label; and a single word that starts an ``ORG``
elsewhere in the text is that ``ORG`` ("Contoso" after "Contoso Ltd").

Offsets are Python ``str`` indices, i.e. Unicode code points — the unit of
span offsets in CONTRACTS.md (text items).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

LABELS: Final = ("PER", "ORG", "LOC")

_WORD: Final = re.compile(r"[^\W\d_][\w'\u2019\-]*", re.UNICODE)
_ORG_WORDS: Final = frozenset(
    {
        "inc",
        "inc.",
        "ltd",
        "ltd.",
        "llc",
        "plc",
        "corp",
        "corp.",
        "oy",
        "oyj",
        "ab",
        "ag",
        "sa",
        "gmbh",
        "company",
        "corporation",
        "university",
        "analytics",
        "bank",
        "group",
        "institute",
    }
)
_ORG_CUES: Final = frozenset({"at", "for"})
#: Capitalised only because they open a sentence; never the start of a name.
_OPENERS: Final = frozenset(
    {
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "yesterday",
        "today",
        "tomorrow",
        "in",
        "on",
        "at",
        "after",
        "before",
        "when",
        "while",
        "but",
        "and",
        "or",
        "so",
        "then",
        "he",
        "she",
        "it",
        "we",
        "they",
        "i",
        "you",
        "there",
        "here",
        "if",
        "as",
        "by",
        "for",
        "dear",
        "hi",
        "hello",
    }
)
#: Capitalised by convention, never names on their own.
_CALENDAR: Final = frozenset(
    {
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    }
)  # fmt: skip
_LIST_JOINERS: Final = frozenset({"and", "or"})
_LOC_CUES: Final = frozenset({"in", "from", "near", "to"})


@dataclass(frozen=True, slots=True)
class Entity:
    start: int
    end: int
    label: str
    score: float


def _capitalised(word: str) -> bool:
    return word[:1].isupper()


def _sentence_start(text: str, index: int) -> bool:
    """`index` opens a sentence or a line: documents put one field per line."""
    before = text[:index]
    stripped = before.rstrip()
    return not stripped or stripped[-1] in ".!?" or "\n" in before[len(stripped) :]


def _not_a_name(text: str, word: str, end: int) -> bool:
    """A single capitalised word that is a field label, a date word or a code."""
    return (
        text[end : end + 1] == ":"
        or word.lower() in _CALENDAR
        or (word.isupper() and len(word) <= 4)
    )


def find_entities(text: str) -> list[Entity]:
    """Entities in ``text``, in order of appearance, never overlapping."""
    words = list(_WORD.finditer(text))
    entities: list[Entity] = []
    i = 0
    while i < len(words):
        if not _capitalised(words[i].group()):
            i += 1
            continue
        j = i
        # Extend the run over capitalised words separated by a single space.
        while (
            j + 1 < len(words)
            and _capitalised(words[j + 1].group())
            and text[words[j].end() : words[j + 1].start()] == " "
        ):
            j += 1
        run = words[i : j + 1]
        if _sentence_start(text, run[0].start()) and run[0].group().lower() in _OPENERS:
            run = run[1:]
            if not run:
                i = j + 1
                continue
        start, end = run[0].start(), run[-1].end()
        # A trailing "Inc." keeps its dot.
        if end < len(text) and text[end] == "." and run[-1].group().lower() + "." in _ORG_WORDS:
            end += 1
        first = words.index(run[0])
        previous = words[first - 1].group().lower() if first > 0 else ""
        gap = text[words[first - 1].end() : start] if first > 0 else ""
        lowered = {word.group().lower() for word in run}
        single = len(run) == 1
        if single and _not_a_name(text, run[0].group(), end):
            pass
        elif lowered & _ORG_WORDS or f"{run[-1].group().lower()}." in _ORG_WORDS:
            entities.append(Entity(start, end, "ORG", 0.8))
        elif previous in _ORG_CUES:
            entities.append(Entity(start, end, "ORG", 0.5))
        elif previous in _LOC_CUES:
            entities.append(Entity(start, end, "LOC", 0.6))
        elif (
            previous in _LIST_JOINERS
            and entities
            and text[entities[-1].end : start].strip().lower() in _LIST_JOINERS
        ):
            entities.append(Entity(start, end, entities[-1].label, entities[-1].score))
        elif not single:
            entities.append(Entity(start, end, "PER", 0.6))
        elif _sentence_start(text, start):
            pass
        elif "," in gap or (first > 0 and text[start - 1 : start] == " " and gap.strip().isdigit()):
            entities.append(Entity(start, end, "LOC", 0.4))
        elif previous == "of":
            pass  # "Head of Data": a title, not a name
        else:
            entities.append(Entity(start, end, "PER", 0.4))
        i = j + 1
    return _consistent_orgs(text, entities)


def _consistent_orgs(text: str, entities: list[Entity]) -> list[Entity]:
    """A one-word entity that starts an ``ORG`` elsewhere ("Contoso" beside
    "Contoso Ltd") is that organisation."""
    org_heads = {
        text[e.start : e.end].split()[0]
        for e in entities
        if e.label == "ORG" and " " in text[e.start : e.end]
    }
    return [
        Entity(e.start, e.end, "ORG", e.score)
        if e.label != "ORG"
        and " " not in text[e.start : e.end]
        and text[e.start : e.end] in org_heads
        else e
        for e in entities
    ]
