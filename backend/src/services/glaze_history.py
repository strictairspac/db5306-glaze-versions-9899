"""Committed glaze revisions ordered by the workshop's logical commit sequence."""

from bisect import bisect_right
from dataclasses import dataclass


MAX_SEQUENCE = 2**53 - 1


@dataclass(frozen=True, slots=True)
class Version:
    commit_seq: int
    recipe: str | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    snapshot_id: str
    read_seq: int


@dataclass(frozen=True, slots=True)
class Ledger:
    head_seq: int
    snapshots: list[Snapshot]
    chains: dict[str, list[Version]]

    @property
    def watermark(self) -> int:
        return self.snapshots[0].read_seq if self.snapshots else self.head_seq


def parse_ledger(payload: dict) -> Ledger:
    if not isinstance(payload, dict):
        raise ValueError("输入必须是对象")
    head = _sequence(payload.get("head_seq"), "已提交水位", 0, MAX_SEQUENCE)

    snapshots: list[Snapshot] = []
    snapshot_ids: set[str] = set()
    for row_number, row in enumerate(_rows(payload, "snapshots", "活跃快照"), 1):
        label = f"活跃快照第 {row_number} 行"
        snapshot_id = _required_text(row.get("snapshot_id"), f"{label}的快照编号")
        if snapshot_id in snapshot_ids:
            raise ValueError(f"快照编号重复：{snapshot_id}")
        read_seq = _sequence(row.get("read_seq"), f"{label}的读取水位", 0, head)
        snapshots.append(Snapshot(snapshot_id, read_seq))
        snapshot_ids.add(snapshot_id)

    versions: dict[str, dict[int, Version]] = {}
    for row_number, row in enumerate(_rows(payload, "versions", "配方版本"), 1):
        label = f"配方版本第 {row_number} 行"
        formula_id = _required_text(row.get("formula_id"), f"{label}的配方编号")
        commit_seq = _sequence(row.get("commit_seq"), f"{label}的提交序号", 1, head)
        operation = row.get("operation")
        recipe = row.get("recipe")
        if operation == "写入":
            recipe = _required_text(recipe, f"{label}的釉料配方")
        elif operation == "删除":
            if recipe is not None and (not isinstance(recipe, str) or recipe.strip()):
                raise ValueError(f"{label}是删除标记，釉料配方须留空")
            recipe = None
        else:
            raise ValueError(f"{label}的操作须为写入或删除")

        chain = versions.setdefault(formula_id, {})
        if commit_seq in chain:
            raise ValueError(f"配方 {formula_id} 的提交序号 {commit_seq} 重复")
        chain[commit_seq] = Version(commit_seq, recipe)

    snapshots.sort(key=lambda snapshot: (snapshot.read_seq, snapshot.snapshot_id))
    chains = {
        formula_id: sorted(chain.values(), key=lambda version: version.commit_seq)
        for formula_id, chain in sorted(versions.items())
    }
    return Ledger(head, snapshots, chains)


def _rows(payload: dict, key: str, label: str) -> list[dict]:
    rows = payload.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{label}必须是对象数组")
    return rows


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}须为非空文本")
    return value.strip()


def _sequence(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label}须为 {minimum} 至 {maximum} 的整数")
    return value


def _boundary(chain: list[Version], read_seq: int) -> int:
    return bisect_right(chain, read_seq, key=lambda version: version.commit_seq)


def version_at(chain: list[Version], read_seq: int) -> Version | None:
    boundary = _boundary(chain, read_seq)
    return chain[boundary - 1] if boundary else None


def collectible_prefix_size(chain: list[Version], watermark: int) -> int:
    boundary = _boundary(chain, watermark)
    if boundary and chain[boundary - 1].recipe is not None:
        return boundary - 1
    # A boundary delete can disappear with its predecessors, leaving the formula absent.
    return boundary


def visible_version(hit: Version | None) -> Version | None:
    return hit if hit is not None and hit.recipe is not None else None
