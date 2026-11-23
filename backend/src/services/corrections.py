"""Correction overlays for committed glaze revisions.

A correction order fixes the wording of one or more glaze recipes for a range
of read watermarks.  Corrections are interval based: a line covers
``[effective_from, effective_to)``.  When a new line overlaps an existing
effective correction the submission must name the replaced correction; the
new text only takes over the overlapping part, so a partially covered
correction keeps the parts outside the overlap.

The module is deliberately pure: persistence and batch numbers live in
``src.services.workflow``.  This keeps replay deterministic and testable.
"""

from dataclasses import dataclass

from .glaze_history import MAX_SEQUENCE, Ledger


MAX_CORRECTION_LINES = 100
MAX_RECIPE_LENGTH = 8000


@dataclass(frozen=True, slots=True)
class CorrectionLine:
    """A persisted correction line."""

    id: int
    order_id: int
    batch_seq: int
    formula_id: str
    effective_from: int
    effective_to: int
    recipe: str
    supersedes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DraftCorrection:
    """A correction line that has been parsed but not persisted yet."""

    formula_id: str
    effective_from: int
    effective_to: int
    recipe: str
    supersedes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Segment:
    """An effective correction segment after overlaps have been resolved."""

    start: int
    end: int
    line: CorrectionLine

    @property
    def formula_id(self) -> str:
        return self.line.formula_id


def parse_submission(payload: dict, ledger: Ledger) -> tuple[str, str, list[DraftCorrection]]:
    """Validate and normalize a correction order submission."""

    if not isinstance(payload, dict):
        raise ValueError("更正单必须是对象")
    request_id = _text(payload.get("request_id"), "提交单号", 8, 64)
    note = payload.get("note") or ""
    if not isinstance(note, str) or len(note.strip()) > 500:
        raise ValueError("更正说明不能超过 500 个字符")
    rows = payload.get("lines")
    if not isinstance(rows, list) or not rows:
        raise ValueError("更正单至少包含一行更正")
    if len(rows) > MAX_CORRECTION_LINES:
        raise ValueError(f"一张更正单最多包含 {MAX_CORRECTION_LINES} 行更正")

    drafts: list[DraftCorrection] = []
    for row_number, row in enumerate(rows, 1):
        label = f"更正第 {row_number} 行"
        if not isinstance(row, dict):
            raise ValueError(f"{label}必须是对象")
        formula_id = _text(row.get("formula_id"), f"{label}的配方编号", 1, 80)
        if formula_id not in ledger.chains:
            raise ValueError(f"配方版本中不存在配方 {formula_id}")
        effective_from = _sequence(row.get("effective_from"), f"{label}的生效水位", 0, ledger.head_seq)
        if row.get("effective_to") is None:
            effective_to = ledger.head_seq + 1
        else:
            effective_to = _sequence(row.get("effective_to"), f"{label}的失效水位",
                                     effective_from + 1, ledger.head_seq + 1)
        recipe = _text(row.get("recipe"), f"{label}的釉料配方", 1, MAX_RECIPE_LENGTH)
        raw_supersedes = row.get("supersedes") or []
        if not isinstance(raw_supersedes, list):
            raise ValueError(f"{label}的替代更正须为编号数组")
        supersedes: list[int] = []
        for value in raw_supersedes:
            if type(value) is not int or value <= 0:
                raise ValueError(f"{label}的替代更正编号须为正整数")
            if value in supersedes:
                raise ValueError(f"{label}的替代更正编号 {value} 重复")
            supersedes.append(value)
        drafts.append(DraftCorrection(formula_id, effective_from, effective_to, recipe, tuple(supersedes)))

    _validate_within_order(drafts)
    return request_id, note.strip(), drafts


def effective_segments(lines) -> dict[str, list[Segment]]:
    """Replay persisted correction lines into disjoint effective segments."""

    segments: dict[str, list[Segment]] = {}
    for line in sorted(lines, key=lambda item: (item.batch_seq, item.order_id, item.id)):
        _apply(segments, Segment(line.effective_from, line.effective_to, line))
    return segments


def validate_against_existing(existing, drafts: list[DraftCorrection]) -> None:
    """Reject an order that overlaps an effective correction it does not name."""

    segments = effective_segments(existing)
    known = {line.id for line in existing}
    for draft in drafts:
        overlaps = {
            segment.line.id
            for segment in segments.get(draft.formula_id, ())
            if _overlaps(segment.start, segment.end, draft.effective_from, draft.effective_to)
        }
        declared = set(draft.supersedes)
        unknown = declared - known
        if unknown:
            raise ValueError(f"找不到被替代的更正：{'、'.join(map(str, sorted(unknown)))}")
        missing = overlaps - declared
        if missing:
            raise ValueError(
                f"配方 {draft.formula_id} 的更正区间 "
                f"[{draft.effective_from}, {draft.effective_to}) 与更正 "
                f"{'、'.join(map(str, sorted(missing)))} 交叠，须在替代列表中逐条指明"
            )
        stale = declared - overlaps
        if stale:
            raise ValueError(
                f"替代的更正 {'、'.join(map(str, sorted(stale)))} 未与本单交叠"
            )


def apply_drafts(existing, drafts: list[DraftCorrection], batch_seq: int) -> dict[str, list[Segment]]:
    """Apply drafts on top of existing lines and return the resulting segments.

    Used for validation previews and tests.  Persisted applications re-read the
    stored lines after insertion instead.
    """

    segments = effective_segments(existing)
    for offset, draft in enumerate(drafts, 1):
        line = CorrectionLine(
            id=-offset,
            order_id=-1,
            batch_seq=batch_seq,
            formula_id=draft.formula_id,
            effective_from=draft.effective_from,
            effective_to=draft.effective_to,
            recipe=draft.recipe,
            supersedes=draft.supersedes,
        )
        _apply(segments, Segment(line.effective_from, line.effective_to, line))
    return segments


def segment_at(segments: dict[str, list[Segment]], formula_id: str, read_seq: int) -> Segment | None:
    for segment in segments.get(formula_id, ()):
        if segment.start <= read_seq < segment.end:
            return segment
        if segment.start > read_seq:
            break
    return None


def effective_ids(segments: dict[str, list[Segment]]) -> set[int]:
    return {segment.line.id for chain in segments.values() for segment in chain}


def coverage_rows(segments: dict[str, list[Segment]]) -> list[dict]:
    """Render effective corrections with their provenance for reports."""

    rows = []
    for formula_id in sorted(segments):
        for segment in segments[formula_id]:
            line = segment.line
            rows.append({
                "配方编号": formula_id,
                "生效水位": segment.start,
                "失效水位": segment.end,
                "釉料配方": line.recipe,
                "更正编号": line.id,
                "更正单": line.order_id,
                "更正批次": line.batch_seq,
                "替代更正": "、".join(map(str, line.supersedes)) if line.supersedes else None,
            })
    return rows


def _apply(segments: dict[str, list[Segment]], incoming: Segment) -> None:
    chain = segments.setdefault(incoming.formula_id, [])
    kept: list[Segment] = []
    for segment in chain:
        if not _overlaps(segment.start, segment.end, incoming.start, incoming.end):
            kept.append(segment)
            continue
        # Replacement only affects the overlapping part; leftovers stay with
        # the correction that owned them.
        if segment.start < incoming.start:
            kept.append(Segment(segment.start, incoming.start, segment.line))
        if segment.end > incoming.end:
            kept.append(Segment(incoming.end, segment.end, segment.line))
    kept.append(incoming)
    kept.sort(key=lambda segment: segment.start)
    segments[incoming.formula_id] = kept


def _validate_within_order(drafts: list[DraftCorrection]) -> None:
    for index, left in enumerate(drafts):
        for right in drafts[index + 1:]:
            if left.formula_id == right.formula_id and _overlaps(
                left.effective_from, left.effective_to, right.effective_from, right.effective_to
            ):
                raise ValueError(f"同一张单内配方 {left.formula_id} 的更正区间重叠")


def _overlaps(a_from: int, a_to: int, b_from: int, b_to: int) -> bool:
    return max(a_from, b_from) < min(a_to, b_to)


def _text(value: object, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}须为 {minimum} 至 {maximum} 个字符的文本")
    text = value.strip()
    if not minimum <= len(text) <= maximum:
        raise ValueError(f"{label}须为 {minimum} 至 {maximum} 个字符的文本")
    return text


def _sequence(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label}须为 {minimum} 至 {maximum} 的整数")
    return value


# MAX_SEQUENCE is re-exported for callers that validate batch bounds.
__all__ = [
    "CorrectionLine",
    "DraftCorrection",
    "Segment",
    "MAX_SEQUENCE",
    "parse_submission",
    "effective_segments",
    "validate_against_existing",
    "apply_drafts",
    "segment_at",
    "effective_ids",
    "coverage_rows",
]
