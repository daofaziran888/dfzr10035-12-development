"""请求批次领域服务：管理批量API请求，与端点定义、调用日志、重放队列、结果缓存联动。

归属规则：
- 批次登记的是「来源归属编号」指向的实际目标，而不是只靠请求路径猜来源；
  同一路径被多家提供方登记时，只给路径无法唯一确定，必须留待核理由。
- 启动前再核验一次归属：只有唯一一条「已确认」归属才允许采集。
- 一批采集只写一条调用日志，日志绑定同一个已确认的原始端点；重试沿用原归属。
"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.source_attribution import resolve_source
from app.store import store


class RequestBatchService(BaseModuleService):
    MODULE = "request_batch"
    ENTITY = "调用批次"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "批次编号", "关联端点", "归属编号", "端点编号", "提供方编号", "提供方名称",
        "请求路径", "参数模板", "请求数量", "并发控制", "超时上限", "完成数量",
        "失败数量", "待核理由",
    ]
    REQUIRED_FIELDS = ["批次编号", "参数模板"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "部分失败"]
    ACTION_RULES = {
        "启动执行": "执行中",
        "重试批次": "执行中",
        "取消批次": "已完成",
        "标记完成": "部分失败",
    }
    NEGATIVE_ACTIONS = []

    # ---------- 登记：把实际目标固定到归属清单 ----------

    def create_entry(self, values):
        self.last_error = ""
        self.action_blocked = False
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if not str(values.get("关联端点") or "").strip() and not str(
            values.get("归属编号") or ""
        ).strip() and not str(values.get("端点编号") or "").strip():
            missing.append("关联端点/归属编号")
        if missing:
            return None, missing

        rows = store.rows(self.MODULE)
        batch_code = str(values.get("批次编号") or "").strip()
        for row in rows:
            if str(row.get("批次编号")) == batch_code:
                self.last_error = f"批次编号{batch_code}已存在"
                return None, []

        entry: dict = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        }
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})
        entry["status"] = self.STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["待核理由"] = ""
        entry["登记时间"] = str(values.get("登记时间") or "2026-11-22")

        reference = (
            str(values.get("归属编号") or "").strip()
            or str(values.get("端点编号") or "").strip()
            or str(values.get("关联端点") or "").strip()
        )
        path = str(values.get("请求路径") or "").strip()
        # 只给批次编号/旧名称时，用归属清单回溯；不带路径是为了避免把同名路径硬拗成唯一。
        resolution = resolve_source(reference, path)
        if resolution["ok"]:
            self._bind_attribution(entry, resolution["attribution"])
            if not str(entry.get("关联端点") or "").strip():
                entry["关联端点"] = entry["端点展示名称"]
        else:
            # 历史登记允许先落库，但必须留下待核理由，不能默认补一个来源。
            entry["待核理由"] = resolution["reason"]
            entry["abnormal"] = True
            if not str(entry.get("关联端点") or "").strip():
                entry["关联端点"] = reference
            self.last_error = f"来源待核：{resolution['reason']}"
        rows.append(entry)
        return entry, []

    def _bind_attribution(self, entry: dict, attribution: dict) -> None:
        """把归属清单里的稳定编号写到批次上；展示名称只用于列表显示。"""
        entry["归属编号"] = str(attribution.get("归属编号") or "")
        entry["端点编号"] = str(attribution.get("端点编号") or "")
        entry["端点展示名称"] = str(attribution.get("端点展示名称") or "")
        entry["提供方编号"] = str(attribution.get("提供方编号") or "")
        entry["提供方名称"] = str(attribution.get("提供方名称") or "")
        entry["请求路径"] = str(attribution.get("请求路径") or "")
        entry["待核理由"] = ""
        entry["abnormal"] = False

    def _resolve_entry(self, entry: dict) -> dict:
        """优先沿用已登记归属；否则按端点编号、再按旧名称回溯。"""
        attribution_id = str(entry.get("归属编号") or "").strip()
        if attribution_id:
            resolution = resolve_source(attribution_id, str(entry.get("请求路径") or ""))
        else:
            reference = (
                str(entry.get("端点编号") or "").strip()
                or str(entry.get("关联端点") or "").strip()
            )
            resolution = resolve_source(reference, str(entry.get("请求路径") or ""))
        return resolution

    # ---------- 动作：启动 / 重试 / 收尾 ----------

    def run_action(self, entry_id: int, action: str):
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return super().run_action(entry_id, action)

        if action in ("启动执行", "重试批次"):
            return self._start_batch(entry, retry=action == "重试批次")

        result, message = super().run_action(entry_id, action)
        if result is None:
            return result, message
        if action in ("取消批次", "标记完成"):
            self._on_batch_complete(result, action)
            if action == "标记完成" and int(result.get("失败数量") or 0) > 0:
                if self._enqueue_retry(result):
                    message += "，失败请求已按原归属加入重放队列"
                else:
                    message += f"，{self.last_error}，未创建重放任务"
        return result, message

    def _start_batch(self, entry: dict, retry: bool = False):
        resolution = self._resolve_entry(entry)
        if not resolution["ok"]:
            reason = resolution["reason"]
            entry["待核理由"] = reason
            entry["abnormal"] = True
            self.last_error = f"批次{entry.get('批次编号')}来源未确定，未启动采集：{reason}"
            self.action_blocked = True
            return None, self.last_error
        if retry and entry.get("status") != "部分失败":
            self.last_error = "只有执行失败（部分失败）的批次才允许重试"
            self.action_blocked = True
            return None, self.last_error

        attribution = resolution["attribution"]
        self._bind_attribution(entry, attribution)
        entry["status"] = "执行中"
        entry["pending"] = True
        entry["abnormal"] = False
        # 启动只写执行中状态；最终成功/失败等收尾动作回写，避免半途就记成成功。
        self._write_log(entry, status="超时", response_state="executing",
                        error="", rows=int(entry.get("请求数量") or 0))
        verb = "重试" if retry else "启动"
        return entry, (
            f"批次{entry.get('批次编号')}已{verb}，绑定归属{entry['归属编号']}"
            f"（端点{entry['端点编号']} / 提供方{entry['提供方编号']} {entry['提供方名称']}）"
        )

    def _log_code(self, batch: dict) -> str:
        return f"CALL-BATCH-{str(batch['id']).zfill(4)}"

    def _adopt_current_name(self, batch: dict) -> str:
        """把归属清单里当前的展示名同步到批次，返回可用于日志的展示名。

        统计和历史记录都按稳定编号追溯；这里是唯一按名称变化刷新的地方。
        """
        display = str(batch.get("端点展示名称") or "")
        attribution_id = str(batch.get("归属编号") or "")
        if not attribution_id:
            return display
        current = next(
            (row for row in store.rows("source_attribution")
             if str(row.get("归属编号")) == attribution_id),
            None,
        )
        if current is None:
            return display
        display = str(current.get("端点展示名称") or display)
        batch["端点展示名称"] = display
        batch["提供方名称"] = str(current.get("提供方名称") or batch.get("提供方名称") or "")
        return display

    def _write_log(self, batch: dict, *, status: str, response_state: str,
                   error: str, rows: int) -> dict:
        """一批采集只维护一条调用日志；重试复用同一条，归属保持不变。"""
        log_rows = store.rows("call_log")
        code = self._log_code(batch)
        log = next((row for row in log_rows if row.get("记录编号") == code), None)
        if log is None:
            log = {
                "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
                "记录编号": code,
            }
            log_rows.append(log)
        # 展示名称按当前归属刷新；判定与追溯仍用归属编号/端点编号。
        display = self._adopt_current_name(batch)
        log.update({
            "归属编号": batch.get("归属编号", ""),
            "端点编号": batch.get("端点编号", ""),
            "接口端点": display or batch.get("关联端点", ""),
            "提供方编号": batch.get("提供方编号", ""),
            "提供方名称": batch.get("提供方名称", ""),
            "请求路径": batch.get("请求路径", ""),
            "请求参数": batch.get("参数模板", ""),
            "响应状态": response_state,
            "响应耗时(ms)": batch.get("超时上限", 5000),
            "响应耗时": batch.get("超时上限", 5000),
            "返回行数": rows,
            "错误信息": error,
            "调用时间": "2026-11-22",
            "status": status,
            "pending": status != "成功",
            "abnormal": status != "成功",
        })
        return log

    def _on_batch_complete(self, batch: dict, action: str):
        """批次收尾时把结果回写同一条调用日志，并核对归属未变。"""
        if not str(batch.get("归属编号") or "").strip():
            # 没有归属证据的历史批次，收尾也不伪造调用日志。
            return
        if action == "取消批次":
            # 取消表示没有采到数据，不能回写成成功。
            self._write_log(
                batch, status="超时", response_state="cancelled",
                error="批次已取消，未完成采集", rows=0,
            )
            return
        failed = int(batch.get("失败数量") or 0)
        finished = int(batch.get("完成数量") or 0)
        self._write_log(
            batch,
            status="超时" if failed else "成功",
            response_state="partial_failed" if failed else "200",
            error=f"失败{failed}项" if failed else "",
            rows=finished,
        )

    def _enqueue_retry(self, batch: dict) -> bool:
        """只有归属仍是唯一已确认时才建重放任务；原归属编号一并留存。"""
        resolution = self._resolve_entry(batch)
        if not resolution["ok"]:
            self.last_error = f"来源未确定，不能继续重放：{resolution['reason']}"
            batch["待核理由"] = resolution["reason"]
            batch["abnormal"] = True
            return False
        attribution = resolution["attribution"]
        self._bind_attribution(batch, attribution)
        retry_rows = store.rows("retry_queue")
        bid = str(batch["id"]).zfill(4)
        task_code = f"RETR-BATCH-{bid}"
        task = next((row for row in retry_rows if row.get("任务编号") == task_code), None)
        if task is None:
            task = {
                "id": max((int(r.get("id", 0)) for r in retry_rows), default=0) + 1,
                "任务编号": task_code,
            }
            retry_rows.append(task)
        task.update({
            "批次编号": batch.get("批次编号", ""),
            "归属编号": batch.get("归属编号", ""),
            "端点编号": batch.get("端点编号", ""),
            "提供方编号": batch.get("提供方编号", ""),
            "提供方名称": batch.get("提供方名称", ""),
            "请求路径": batch.get("请求路径", ""),
            "失败端点": batch.get("端点展示名称") or batch.get("关联端点", ""),
            "失败原因": f"批次{bid}含{batch.get('失败数量', 0)}条失败",
            "重试次数": task.get("重试次数", 0),
            "最大重试": task.get("最大重试", 3),
            "退避策略": task.get("退避策略", "exponential:2s/baseline"),
            "最后重试": task.get("最后重试", ""),
            "队列状态": "等待",
            "待核理由": "",
            "status": "待重试", "pending": True, "abnormal": False,
        })
        return True

    def check_batch_throughput(self, endpoint_id: str) -> dict:
        """跨模块吞吐量分析：按稳定端点编号汇总调用日志+重放队列+缓存。"""
        logs = [
            l for l in store.rows("call_log")
            if l.get("端点编号") == endpoint_id or l.get("接口端点") == endpoint_id
        ]
        retries = [
            r for r in store.rows("retry_queue")
            if r.get("端点编号") == endpoint_id or r.get("失败端点") == endpoint_id
        ]
        caches = [c for c in store.rows("data_cache") if c.get("目标端点") == endpoint_id]
        return {
            "endpoint": endpoint_id,
            "total_calls": len(logs),
            "failed_calls": sum(1 for l in logs if l.get("status") != "成功"),
            "pending_retries": len(retries),
            "cache_hit": any(c.get("status") == "生效中" for c in caches),
            "sources": sorted({str(l.get("归属编号")) for l in logs if l.get("归属编号")}),
        }
