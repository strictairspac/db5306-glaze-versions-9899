"""回放釉料配方读快照，输出按配方归组的历史回收预检。"""

from .glaze_history import (
    Version,
    collectible_prefix_size,
    parse_ledger,
    version_at,
    visible_version,
)


SPEC = {
    "name": "瓷汀工场·窑红试样册回收预检",
    "summary": "核对试烧和质检快照所见配方，并为版次管理员生成安全回收批单。",
    "customer": "瓷汀工场·釉料配方室与烧成质检组",
    "entity_labels": ["配方版本台账", "读快照回收预检", "留存批单"],
    "field_labels": {
        "head_seq": "已提交水位",
        "snapshots": "活跃快照",
        "versions": "配方版本",
        "snapshot_id": "快照编号",
        "read_seq": "读取水位",
        "formula_id": "配方编号",
        "commit_seq": "提交序号",
        "operation": "版本操作",
        "recipe": "釉料配方",
    },
    "guidance": (
        "从同一批台账录入已提交水位、配方版本和仍需复读的活跃快照。"
        "操作填「写入」或「删除」，删除行的配方留空。按配方整批复核回收前缀。"
    ),
    "sample": {
        "head_seq": 90,
        "snapshots": [
            {"snapshot_id": "质检-复核", "read_seq": 45},
            {"snapshot_id": "试烧-称料", "read_seq": 30},
            {"snapshot_id": "窑务-入窑", "read_seq": 65},
        ],
        "versions": [
            {"formula_id": "GL-Q01", "commit_seq": 10, "operation": "写入", "recipe": "青岚釉｜长石 62 g、石英 28 g、高岭土 10 g"},
            {"formula_id": "GL-Q01", "commit_seq": 30, "operation": "写入", "recipe": "青岚釉｜长石 60 g、石英 30 g、高岭土 10 g"},
            {"formula_id": "GL-Q01", "commit_seq": 40, "operation": "删除", "recipe": ""},
            {"formula_id": "GL-Q01", "commit_seq": 60, "operation": "写入", "recipe": "青岚釉｜长石 58 g、石英 32 g、高岭土 10 g"},
            {"formula_id": "GL-Q01", "commit_seq": 80, "operation": "写入", "recipe": "青岚釉｜长石 56 g、石英 34 g、高岭土 10 g"},
            {"formula_id": "GL-B02", "commit_seq": 12, "operation": "写入", "recipe": "白砂釉｜长石 50 g、石英 35 g、高岭土 15 g"},
            {"formula_id": "GL-B02", "commit_seq": 24, "operation": "删除", "recipe": ""},
            {"formula_id": "GL-H03", "commit_seq": 5, "operation": "写入", "recipe": "褐砂釉｜长石 66 g、石英 22 g、高岭土 12 g"},
            {"formula_id": "GL-H03", "commit_seq": 18, "operation": "写入", "recipe": "褐砂釉｜长石 64 g、石英 24 g、高岭土 12 g"},
            {"formula_id": "GL-H03", "commit_seq": 36, "operation": "写入", "recipe": "褐砂釉｜长石 62 g、石英 26 g、高岭土 12 g"},
            {"formula_id": "GL-H03", "commit_seq": 72, "operation": "删除", "recipe": ""},
            {"formula_id": "GL-M04", "commit_seq": 58, "operation": "写入", "recipe": "蜜灰釉｜长石 55 g、石英 25 g、高岭土 20 g"},
        ],
    },
    "expect": {"回收低水位": 30, "可回收版本数": 4, "保留版本数": 8, "当前可见配方数": 2},
}


def validate(payload):
    parse_ledger(payload)


def solve(payload, engine):
    """Simulate collection in the supplied ledger by removing planned prefixes."""
    ledger = parse_ledger(payload)
    watermark = ledger.watermark
    reads = [
        ("活跃快照", snapshot.snapshot_id, snapshot.read_seq)
        for snapshot in ledger.snapshots
    ]
    reads.append(("当前读", "当前", ledger.head_seq))
    read_rows = []
    version_rows = []
    batch_rows = []
    collect_count = 0
    current_count = 0

    for formula_id, chain in ledger.chains.items():
        prefix_size = collectible_prefix_size(chain, watermark)
        retained = chain[prefix_size:]
        collect_count += prefix_size
        if prefix_size:
            last_candidate = chain[prefix_size - 1]
            batch_rows.append({
                "配方编号": formula_id,
                "批内最大提交序号": last_candidate.commit_seq,
                "可回收版本数": prefix_size,
                "含删除标记数": sum(version.recipe is None for version in chain[:prefix_size]),
                "执行口径": "按此配方整批移除列出的历史前缀",
            })
        for index, version in enumerate(chain):
            eligible = index < prefix_size
            version_rows.append({
                "配方编号": formula_id,
                "提交序号": version.commit_seq,
                "版本操作": "删除" if version.recipe is None else "写入",
                "釉料配方": version.recipe,
                "判定": "可回收" if eligible else "保留",
                "依据": _collection_reason(chain, index, watermark, eligible),
            })
        for read_type, snapshot_id, read_seq in reads:
            hit = version_at(chain, read_seq)
            after = version_at(retained, read_seq)
            visible = visible_version(hit)
            if visible != visible_version(after):
                raise RuntimeError(f"配方 {formula_id} 在读取水位 {read_seq} 的可见性发生变化")
            if read_type == "当前读" and visible is not None:
                current_count += 1
            read_rows.append({
                "配方编号": formula_id,
                "读取类型": read_type,
                "快照编号": snapshot_id,
                "读取水位": read_seq,
                "判定": _read_status(hit),
                "命中提交序号": hit.commit_seq if hit is not None else None,
                "釉料配方": visible.recipe if visible is not None else None,
                "回收后命中序号": after.commit_seq if after is not None else None,
                "前后可见配方一致": "是",
            })

    total_versions = len(version_rows)
    sources = [
        snapshot.snapshot_id for snapshot in ledger.snapshots if snapshot.read_seq == watermark
    ]
    return {
        "summary": (
            f"回收低水位为 {watermark}，{len(batch_rows)} 个配方批次共 {collect_count} 条版本可回收。"
            f"已核对 {len(read_rows)} 条配方读取，模拟回收前后的可见配方一致。"
        ),
        "metrics": {
            "回收低水位": watermark,
            "活跃快照数": len(ledger.snapshots),
            "配方版本数": total_versions,
            "历史回收批次数": len(batch_rows),
            "可回收版本数": collect_count,
            "保留版本数": total_versions - collect_count,
            "当前可见配方数": current_count,
            "读取核对数": len(read_rows),
        },
        "sections": [
            {"title": "回收低水位依据", "rows": [{
                "已提交水位": ledger.head_seq,
                "回收低水位": watermark,
                "活跃快照数": len(ledger.snapshots),
                "低水位来源": "最早活跃快照" if sources else "无活跃快照，取已提交水位",
                "限制回收的快照": "、".join(sources) if sources else None,
            }]},
            {"title": "配方读取与回收后复核", "rows": read_rows},
            {"title": "逐版本留存依据", "rows": version_rows},
            {"title": "待核准回收批单", "rows": batch_rows},
        ],
    }


def _read_status(hit: Version | None) -> str:
    if hit is None:
        return "无可见版本"
    if hit.recipe is None:
        return "已删除"
    return "可见"


def _collection_reason(chain: list[Version], index: int, watermark: int, eligible: bool) -> str:
    version = chain[index]
    if version.commit_seq > watermark:
        return f"提交序号高于低水位 {watermark}，保留该读取区间的变化"
    if not eligible:
        return f"低水位 {watermark} 命中的配方版次"
    if index + 1 < len(chain) and chain[index + 1].commit_seq <= watermark:
        return f"提交 {chain[index + 1].commit_seq} 已在低水位前或当时覆盖此版本"
    return f"低水位 {watermark} 命中删除标记，与此前的同配方历史一起回收"
