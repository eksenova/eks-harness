from __future__ import annotations

import functools
import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

KERN_FEATURE = "kern"
PAIR_POSITIONING = 2
EXTENSION_POSITIONING = 9


@dataclass
class Kerning:
    units_per_em: int
    pairs: dict[tuple[int, int], int] = field(default_factory=dict[tuple[int, int], int])
    classes: list[Callable[[int, int], int | None]] = field(default_factory=list[Callable[[int, int], int | None]])

    def adjust(self, left: int, right: int) -> int:
        found = self.pairs.get((left, right))
        if found is not None:
            return found
        for lookup in self.classes:
            value = lookup(left, right)
            if value is not None:
                return value
        return 0


def _advance(record: Any) -> int:
    value = getattr(record, "Value1", None)
    return int(getattr(value, "XAdvance", 0) or 0) if value is not None else 0


def _class_lookup(subtable: Any, order: dict[str, int]) -> Callable[[int, int], int | None]:
    covered = {order[name] for name in subtable.Coverage.glyphs if name in order}
    first = {order[name]: value for name, value in subtable.ClassDef1.classDefs.items() if name in order}
    second = {order[name]: value for name, value in subtable.ClassDef2.classDefs.items() if name in order}
    rows = subtable.Class1Record

    def lookup(left: int, right: int) -> int | None:
        if left not in covered or right not in second:
            return None
        return _advance(rows[first.get(left, 0)].Class2Record[second[right]])

    return lookup


@functools.cache
def kerning(path: str) -> Kerning:
    ttlib: Any = importlib.import_module("fontTools.ttLib")
    font = ttlib.TTFont(path, lazy=True)
    order = {name: index for index, name in enumerate(font.getGlyphOrder())}
    found = Kerning(units_per_em=int(font["head"].unitsPerEm))
    if "GPOS" not in font:
        return found
    table = font["GPOS"].table
    if table.FeatureList is None or table.LookupList is None:
        return found
    indices = sorted({index for record in table.FeatureList.FeatureRecord if record.FeatureTag == KERN_FEATURE
                      for index in record.Feature.LookupListIndex})
    for index in indices:
        lookup = table.LookupList.Lookup[index]
        for subtable in lookup.SubTable:
            kind = lookup.LookupType
            if kind == EXTENSION_POSITIONING:
                kind = subtable.ExtensionLookupType
                subtable = subtable.ExtSubTable
            if kind != PAIR_POSITIONING:
                continue
            if subtable.Format == 1:
                for name, pair_set in zip(subtable.Coverage.glyphs, subtable.PairSet, strict=True):
                    left = order.get(name)
                    if left is None:
                        continue
                    for record in pair_set.PairValueRecord:
                        right = order.get(record.SecondGlyph)
                        if right is not None:
                            found.pairs.setdefault((left, right), _advance(record))
            elif subtable.Format == 2:
                found.classes.append(_class_lookup(subtable, order))
    return found


__all__ = ["Kerning", "kerning"]
