"""来源采集归属清单：把「一次采集到底采的是哪一家提供方的哪个端点」固定下来。

设计要点：
- 归属判定只认稳定编号：端点编号（ENDP-*）和提供方编号（API_*-*）。
  展示名称只用于人看，改名后旧名称进入历史名称，仍能回溯到原归属。
- 同一个请求路径允许被多家提供方登记，因此仅凭路径不足以定位来源；
  只有唯一一条「已确认」归属时才允许启动采集。
- 来源不明的历史登记保留为「待核」，必须写明待核理由，不能被静默补全。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

CONFIRMED = "已确认"
PENDING = "待核"
INVALID = "已失效"

# 历史名称允许用中英文逗号、顿号、竖线或分号分隔，方便人工登记。
_NAME_SEPARATORS = (",", "，", "、", "|", ";", "；")


def history_names(row: dict[str, Any]) -> list[str]:
    """展开归属记录里登记的历史名称（含已改名前的提供方/端点展示名）。"""
    raw = str(row.get("历史名称") or "").replace("|", ",")
    for separator in _NAME_SEPARATORS:
        raw = raw.replace(separator, ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


def append_history(row: dict[str, Any], *names: str) -> None:
    """把旧名称补进历史名称，去重且保留登记顺序。"""
    existing = history_names(row)
    for name in names:
        name = str(name or "").strip()
        if name and name not in existing and name != str(row.get("提供方名称") or ""):
            existing.append(name)
    row["历史名称"] = ",".join(existing)


def _display_names(row: dict[str, Any]) -> list[str]:
    names = [str(row.get("提供方名称") or ""), str(row.get("端点展示名称") or "")]
    names.extend(history_names(row))
    return [name for name in names if name]


def find_endpoint_by_id(endpoint_id: str) -> dict[str, Any] | None:
    for endpoint in store.rows("endpoint_def"):
        if str(endpoint.get("端点编号")) == str(endpoint_id or "").strip():
            return endpoint
    return None


def find_provider_by_ref(reference: str) -> dict[str, Any] | None:
    """按提供方编号、当前名称或归属历史名称定位提供方（改名后仍可回溯）。"""
    ref = str(reference or "").strip()
    if not ref:
        return None
    providers = store.rows("api_provider")
    for provider in providers:
        if str(provider.get("提供方编号")) == ref:
            return provider
    for provider in providers:
        if str(provider.get("提供方名称")) == ref:
            return provider
    for attribution in store.rows("source_attribution"):
        if ref in history_names(attribution):
            for provider in providers:
                if str(provider.get("提供方编号")) == str(attribution.get("提供方编号")):
                    return provider
    return None


def resolve_source(reference: str = "", path: str = "") -> dict[str, Any]:
    """按归属编号/端点编号/提供方名称/历史名称解析唯一来源。

    返回 state = confirmed | ambiguous | pending | unknown：
    - confirmed：可以启动采集，attribution 为唯一已确认归属；
    - ambiguous：同一引用命中多条已确认归属（例如只给了 /orders）；
    - pending：命中待核/已失效归属，需要人工补充依据；
    - unknown：没有任何归属证据。
    """
    ref = str(reference or "").strip()
    wanted_path = str(path or "").strip()
    rows = store.rows("source_attribution")

    def path_matches(row: dict[str, Any]) -> bool:
        return not wanted_path or str(row.get("请求路径") or "") == wanted_path

    matched: dict[int, dict[str, Any]] = {}

    def add(row: dict[str, Any]) -> None:
        if path_matches(row):
            matched[int(row.get("id", 0))] = row

    if ref:
        # 1) 直接引用归属编号：即使已失效也要如实反馈，不能当成没登记。
        for row in rows:
            if str(row.get("归属编号")) == ref:
                add(row)
        # 2) 稳定端点编号。
        if not matched:
            for row in rows:
                if str(row.get("端点编号")) == ref:
                    add(row)
        # 3) 展示名称与历史名称（旧名称改名后仍可解析）。
        if not matched:
            for row in rows:
                if ref in _display_names(row):
                    add(row)
        # 端点展示名称也可能只挂在端点定义上（归属清单尚未同步展示名）。
        if not matched:
            for endpoint in store.rows("endpoint_def"):
                if ref in (str(endpoint.get("端点展示名称") or ""),
                           *history_names(endpoint)):
                    for row in rows:
                        if str(row.get("端点编号")) == str(endpoint.get("端点编号")):
                            add(row)
        # 4) 提供方编号：该提供方下只有一个已确认端点时才可定位，多个端点仍是歧义。
        if not matched:
            provider_rows = [row for row in rows if str(row.get("提供方编号")) == ref]
            if provider_rows:
                for row in provider_rows:
                    add(row)
        # 5) 引用本身就是请求路径（历史批次常见）：按路径找，多个来源自然歧义。
        if not matched and ref.startswith("/"):
            for row in rows:
                if str(row.get("请求路径")) == ref and path_matches(row):
                    matched[int(row.get("id", 0))] = row
    else:
        # 没有引用时只按路径找，两条 /orders 会自然变成歧义。
        for row in rows:
            if wanted_path and str(row.get("请求路径")) == wanted_path:
                matched[int(row.get("id", 0))] = row

    candidates = list(matched.values())
    confirmed = [row for row in candidates if row.get("status") == CONFIRMED]
    pending = [row for row in candidates if row.get("status") == PENDING]
    invalid = [row for row in candidates if row.get("status") == INVALID]

    # 同一端点可能留下多条已确认归属（历史登记），它们指向同一个原始端点，
    # 因此按「端点编号 + 提供方编号」去重后再判断是否唯一。
    sources: dict[tuple[str, str], dict[str, Any]] = {}
    for row in confirmed:
        sources.setdefault(
            (str(row.get("端点编号") or ""), str(row.get("提供方编号") or "")), row
        )

    summary = [
        {
            "归属编号": row.get("归属编号"),
            "端点编号": row.get("端点编号"),
            "提供方编号": row.get("提供方编号"),
            "提供方名称": row.get("提供方名称"),
            "请求路径": row.get("请求路径"),
            "status": row.get("status"),
        }
        for row in candidates
    ]

    if len(sources) == 1:
        row = next(iter(sources.values()))
        return {
            "ok": True,
            "state": "confirmed",
            "reason": "",
            "attribution": row,
            "candidates": summary,
        }
    if len(sources) > 1:
        labels = "、".join(
            f"{endpoint_id}/{provider_id}" for endpoint_id, provider_id in sources
        )
        reason = (
            f"引用「{ref or wanted_path}」命中{len(sources)}个已确认来源（{labels}），"
            "同一路径存在多个来源，无法确定唯一原始端点"
        )
        return {"ok": False, "state": "ambiguous", "reason": reason,
                "attribution": None, "candidates": summary}
    if pending or invalid:
        row = (pending or invalid)[0]
        detail = str(row.get("待核理由") or "").strip()
        state = "pending" if row.get("status") == PENDING else "invalid"
        reason = f"来源归属{row.get('归属编号')}状态为{row.get('status')}"
        if detail:
            reason += f"：{detail}"
        return {"ok": False, "state": state, "reason": reason,
                "attribution": None, "candidates": summary}

    reason = f"引用「{ref or wanted_path}」没有已确认的来源归属证据"
    return {"ok": False, "state": "unknown", "reason": reason,
            "attribution": None, "candidates": summary}


class SourceAttributionService(BaseModuleService):
    MODULE = "source_attribution"
    ENTITY = "来源归属"
    OPERATION_TERM = "确认"
    LIST_FIELDS = [
        "归属编号", "端点编号", "端点展示名称", "请求路径", "请求参数",
        "提供方编号", "提供方名称", "历史名称", "待核理由", "确认依据", "登记时间",
    ]
    REQUIRED_FIELDS = ["归属编号", "请求路径"]
    STATUS_ORDER = [PENDING, CONFIRMED, INVALID]
    ACTION_RULES = {"确认归属": CONFIRMED, "标记待核": PENDING, "作废归属": INVALID}
    SIDE_EFFECT_ACTIONS = ["变更归属"]
    NEGATIVE_ACTIONS = ["标记待核", "作废归属"]

    def create_entry(self, values: dict[str, Any]):
        self.last_error = ""
        self.action_blocked = False
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing

        path = str(values.get("请求路径") or "").strip()
        if not path.startswith("/"):
            self.last_error = "请求路径必须以 / 开头，否则无法作为归属判定依据"
            return None, []
        code = str(values.get("归属编号") or "").strip()
        for row in store.rows(self.MODULE):
            if str(row.get("归属编号")) == code:
                self.last_error = f"归属编号{code}已存在"
                return None, []
        endpoint = None
        provider = None
        endpoint_id = str(values.get("端点编号") or "").strip()
        provider_id = str(values.get("提供方编号") or "").strip()
        if endpoint_id:
            endpoint = find_endpoint_by_id(endpoint_id)
            if endpoint is None:
                self.last_error = f"端点编号{endpoint_id}不存在，无法建立归属"
                return None, []
            if str(endpoint.get("请求路径")) != path:
                self.last_error = (
                    f"端点{endpoint_id}登记的请求路径为{endpoint.get('请求路径')}，"
                    f"与归属清单的{path}不一致"
                )
                return None, []
            if not provider_id:
                provider_id = str(endpoint.get("提供方编号") or "")
        if provider_id:
            provider = find_provider_by_ref(provider_id)
            if provider is None:
                self.last_error = f"提供方编号{provider_id}不存在，无法建立归属"
                return None, []
            provider_id = str(provider.get("提供方编号"))

        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in store.rows(self.MODULE)), default=0) + 1,
        }
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})
        entry["请求路径"] = path
        entry["端点编号"] = endpoint_id
        entry["提供方编号"] = provider_id
        if endpoint is not None:
            entry["端点展示名称"] = (
                str(values.get("端点展示名称") or "").strip()
                or str(endpoint.get("端点展示名称") or "")
                or str(endpoint.get("描述文档") or "")
            )
            entry["请求路径"] = str(endpoint.get("请求路径"))
        if provider is not None:
            entry["提供方名称"] = str(provider.get("提供方名称") or "")
        entry["历史名称"] = str(values.get("历史名称") or "")
        entry["登记时间"] = str(values.get("登记时间") or "2026-11-22")

        # 只有端点与提供方都落到稳定编号上，才允许直接确认；否则留待核理由。
        evidence_ok = bool(endpoint_id and provider_id and endpoint is not None)
        if evidence_ok:
            # 同一端点若已有确认归属，新记录转为待核，由人决定是否作废旧归属再确认。
            conflict = self._find_conflict(endpoint_id, provider_id)
            if conflict is not None:
                entry["待核理由"] = (
                    f"端点{endpoint_id}已有确认归属{conflict.get('归属编号')}，"
                    "同一原始端点只允许一个已确认归属，需人工核对后处理旧归属"
                )
                evidence_ok = False
        if evidence_ok:
            entry["status"] = CONFIRMED
            entry["pending"] = False
            entry["abnormal"] = False
            entry["待核理由"] = ""
            entry["确认依据"] = (
                str(values.get("确认依据") or "").strip()
                or "端点定义与提供方注册信息一致，编号可唯一确定来源"
            )
        else:
            entry["status"] = PENDING
            entry["pending"] = True
            entry["abnormal"] = True
            entry["待核理由"] = (
                str(entry.get("待核理由") or "").strip()
                or str(values.get("待核理由") or "").strip()
                or "登记时缺少端点编号或提供方编号，需人工核对原始请求归属"
            )
        store.rows(self.MODULE).append(entry)
        if not evidence_ok:
            self.last_error = f"归属已登记为待核：{entry['待核理由']}"
        return entry, []

    def run_action(self, entry_id: int, action: str):
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return super().run_action(entry_id, action)
        remark = str(getattr(self, "action_remark", "") or "").strip()

        if action == "确认归属":
            endpoint_id = str(entry.get("端点编号") or "").strip()
            provider_id = str(entry.get("提供方编号") or "").strip()
            endpoint = find_endpoint_by_id(endpoint_id)
            provider = find_provider_by_ref(provider_id) if provider_id else None
            if endpoint is None or provider is None:
                self.last_error = (
                    "缺少可核验的端点编号/提供方编号，不能确认归属；"
                    "请先补全稳定编号"
                )
                self.action_blocked = True
                entry["待核理由"] = self.last_error
                entry["abnormal"] = True
                return None, self.last_error
            if str(endpoint.get("请求路径")) != str(entry.get("请求路径")):
                self.last_error = (
                    f"端点{endpoint_id}路径{endpoint.get('请求路径')}"
                    f"与归属清单{entry.get('请求路径')}不一致，不能确认"
                )
                self.action_blocked = True
                entry["待核理由"] = self.last_error
                entry["abnormal"] = True
                return None, self.last_error
            entry["提供方名称"] = str(provider.get("提供方名称") or "")
            entry["待核理由"] = ""
            entry["确认依据"] = remark or "人工核对端点编号与提供方编号后确认"
            entry["abnormal"] = False

        if action == "标记待核":
            reason = remark or "人工标记来源待核，缺少唯一归属证据"
            entry["待核理由"] = reason
            entry["确认依据"] = ""
            entry["abnormal"] = True

        if action == "作废归属":
            entry["待核理由"] = remark or "归属已作废，原采集需重新核对来源"

        if action == "变更归属":
            return self._transfer_attribution(entry)

        return super().run_action(entry_id, action)

    def _transfer_attribution(self, entry: dict):
        """采集归属变化：换到新的稳定端点，旧归属与旧名称仍留在记录里。"""
        new_endpoint_id = str(self.action_values.get("端点编号") or "").strip()
        endpoint = find_endpoint_by_id(new_endpoint_id)
        if endpoint is None:
            self.last_error = (
                f"变更归属需要提供已存在的 values.端点编号，{new_endpoint_id or '空'}无法定位"
            )
            self.action_blocked = True
            return None, self.last_error
        new_provider_id = str(endpoint.get("提供方编号") or "").strip()
        provider = find_provider_by_ref(new_provider_id)
        if provider is None:
            self.last_error = f"新端点{new_endpoint_id}没有可核验的提供方编号，不能变更归属"
            self.action_blocked = True
            return None, self.last_error
        conflict = self._find_conflict(
            new_endpoint_id, new_provider_id, exclude_id=int(entry.get("id", 0))
        )
        if conflict is not None:
            self.last_error = (
                f"新端点{new_endpoint_id}已有确认归属{conflict.get('归属编号')}，"
                "变更后会出现两个已确认来源指向同一原始端点"
            )
            self.action_blocked = True
            return None, self.last_error

        old_snapshot = (
            f"{entry.get('端点编号') or '未登记'}/"
            f"{entry.get('提供方编号') or '未登记'}"
        )
        old_display = str(entry.get("端点展示名称") or "")
        append_history(entry, old_display, str(entry.get("提供方名称") or ""))
        entry["端点编号"] = new_endpoint_id
        entry["提供方编号"] = new_provider_id
        entry["提供方名称"] = str(provider.get("提供方名称") or "")
        entry["端点展示名称"] = (
            str(endpoint.get("展示名称") or "")
            or str(endpoint.get("描述文档") or "")
            or new_endpoint_id
        )
        entry["请求路径"] = str(endpoint.get("请求路径") or entry.get("请求路径") or "")
        entry["变更前归属"] = old_snapshot
        entry["变更原因"] = str(self.action_remark or "采集归属变更，按新端点稳定编号重新登记")
        entry["变更时间"] = "2026-11-22"
        entry["status"] = CONFIRMED
        entry["pending"] = False
        entry["abnormal"] = False
        entry["待核理由"] = ""
        entry["确认依据"] = entry["变更原因"]
        return entry, (
            f"归属{entry.get('归属编号')}已由{old_snapshot}变更为"
            f"{new_endpoint_id}/{new_provider_id}，旧归属与历史名称仍可查"
        )

    def resolve(self, reference: str = "", path: str = "") -> dict[str, Any]:
        return resolve_source(reference, path)

    def _find_conflict(self, endpoint_id: str, provider_id: str,
                       *, exclude_id: int | None = None) -> dict[str, Any] | None:
        """找出同一原始端点（端点编号+提供方编号）上已确认的其他归属。"""
        if not endpoint_id:
            return None
        for row in store.rows(self.MODULE):
            if int(row.get("id", 0)) == exclude_id:
                continue
            if (
                row.get("status") == CONFIRMED
                and str(row.get("端点编号") or "") == endpoint_id
                and str(row.get("提供方编号") or "") == str(provider_id or "")
            ):
                return row
        return None

    def resolve_history(self, attribution: dict[str, Any]) -> dict[str, Any]:
        """回看一条归属的改名轨迹，用于确认「原归属仍可查」。"""
        return {
            "归属编号": attribution.get("归属编号"),
            "端点编号": attribution.get("端点编号"),
            "端点展示名称": attribution.get("端点展示名称"),
            "请求路径": attribution.get("请求路径"),
            "提供方编号": attribution.get("提供方编号"),
            "提供方名称": attribution.get("提供方名称"),
            "历史名称": history_names(attribution),
            "status": attribution.get("status"),
            "待核理由": attribution.get("待核理由"),
            "变更前归属": attribution.get("变更前归属", ""),
        }

    def attribution_coverage(self) -> dict[str, Any]:
        """核对归属去向：每条归属领到哪些批次/日志/重放，以及来源未确定的登记。"""
        logs = store.rows("call_log")
        batches = store.rows("request_batch")
        retries = store.rows("retry_queue")

        def rows_for(module_rows: list[dict[str, Any]], attribution_id: str) -> list[str]:
            return [
                str(row.get("记录编号") or row.get("批次编号") or row.get("任务编号"))
                for row in module_rows
                if str(row.get("归属编号") or "") == attribution_id
            ]

        attributions = []
        for row in store.rows(self.MODULE):
            code = str(row.get("归属编号") or "")
            attributions.append({
                **self.resolve_history(row),
                "logs": rows_for(logs, code),
                "batches": rows_for(batches, code),
                "retries": rows_for(retries, code),
            })

        def collection_attempt(row: dict[str, Any], endpoint_field: str) -> bool:
            """只统计真正指向某个接口的登记；泛化样例行不当作采集归属缺口。"""
            path = str(row.get("请求路径") or "").strip()
            target = str(row.get(endpoint_field) or "").strip()
            return bool(path) or target.startswith("/")

        def unbound(row: dict[str, Any], endpoint_field: str) -> bool:
            """没绑定归属、且不能按旧名称/路径回溯到唯一来源，才算来源缺口。"""
            if str(row.get("归属编号") or "").strip():
                return False
            if not collection_attempt(row, endpoint_field):
                return False
            reference = str(row.get(endpoint_field) or "").strip()
            resolution = resolve_source(reference, str(row.get("请求路径") or ""))
            return not resolution["ok"]

        unresolved = {
            "batches": [
                {"批次编号": b.get("批次编号"), "请求路径": b.get("请求路径"),
                 "待核理由": b.get("待核理由") or "未绑定归属", "status": b.get("status")}
                for b in batches
                if unbound(b, "关联端点")
            ],
            "logs": [
                {"记录编号": l.get("记录编号"), "接口端点": l.get("接口端点"),
                 "请求路径": l.get("请求路径"),
                 "待核理由": l.get("待核理由") or "未绑定归属", "status": l.get("status")}
                for l in logs
                if unbound(l, "接口端点")
            ],
            "retries": [
                {"任务编号": r.get("任务编号"), "失败端点": r.get("失败端点"),
                 "请求路径": r.get("请求路径"),
                 "待核理由": r.get("待核理由") or "未绑定归属", "status": r.get("status")}
                for r in retries
                if unbound(r, "失败端点")
            ],
        }
        confirmed = sum(1 for row in store.rows(self.MODULE) if row.get("status") == CONFIRMED)
        return {
            "total_attributions": len(attributions),
            "confirmed": confirmed,
            "pending": sum(1 for a in attributions if a.get("status") == PENDING),
            "attributions": attributions,
            "unresolved": unresolved,
        }
