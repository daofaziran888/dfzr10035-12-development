"""重放队列领域服务：管理失败请求重试，与 request_batch、call_log、api_monitor 联动。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.source_attribution import resolve_source
from app.store import store


class RetryQueueService(BaseModuleService):
    MODULE = "retry_queue"
    ENTITY = "重放任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "任务编号", "归属编号", "端点编号", "失败端点", "提供方编号", "提供方名称",
        "请求路径", "失败原因", "重试次数", "最大重试", "退避策略", "最后重试",
        "队列状态", "待核理由",
    ]
    REQUIRED_FIELDS = ["任务编号", "失败端点", "失败原因"]
    STATUS_ORDER = ["待重试", "重试中", "已成功", "永久失败"]
    ACTION_RULES = {"加入队列": "重试中", "手动重试": "已成功", "标记失败": "永久失败"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str):
        """重试中/已成功时联动写调用日志和更新监控；永久失败时触发告警。"""
        task = store.find(self.MODULE, entry_id)
        # 只有带唯一来源证据的任务才继续采集；只登记了 /orders 这类多来源路径的
        # 历史任务先核来源，原归属与待核理由都留在任务上。
        if task is not None and action in ("加入队列", "手动重试"):
            reference = (
                str(task.get("归属编号") or "").strip()
                or str(task.get("端点编号") or "").strip()
                or str(task.get("失败端点") or "").strip()
            )
            resolution = resolve_source(reference, str(task.get("请求路径") or ""))
            if not resolution["ok"]:
                task["待核理由"] = resolution["reason"]
                task["abnormal"] = True
                self.last_error = f"重放任务{task.get('任务编号')}来源未确定：{resolution['reason']}"
                self.action_blocked = True
                return None, self.last_error
            attribution = resolution["attribution"]
            task["归属编号"] = str(attribution.get("归属编号") or "")
            task["端点编号"] = str(attribution.get("端点编号") or "")
            task["提供方编号"] = str(attribution.get("提供方编号") or "")
            task["提供方名称"] = str(attribution.get("提供方名称") or "")
            task["请求路径"] = str(attribution.get("请求路径") or "")
            task["失败端点"] = str(attribution.get("端点展示名称") or task.get("失败端点") or "")
            task["待核理由"] = ""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "加入队列":
            self._log_retry_start(entry)
        elif action == "手动重试":
            self._log_retry_success(entry)
            self._update_monitor_health(entry)
            message += "，已更新调用日志和监控健康上报"
        elif action == "标记失败":
            self._trigger_alert(entry)
            message += "，已触发监控告警"
        return entry, message

    def _log_retry_start(self, task: dict):
        log_rows = store.rows("call_log")
        tid = str(task["id"]).zfill(4)
        log_rows.append({
            "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
            "记录编号": f"CALL-RETRY-{tid}",
            "归属编号": task.get("归属编号", ""),
            "端点编号": task.get("端点编号", ""),
            "接口端点": task.get("失败端点", ""),
            "提供方编号": task.get("提供方编号", ""),
            "提供方名称": task.get("提供方名称", ""),
            "请求路径": task.get("请求路径", ""),
            "请求参数": f"retry:task={tid}",
            "响应状态": "retrying",
            "响应耗时(ms)": 0,
            "返回行数": 0,
            "错误信息": task.get("失败原因", ""),
            "调用时间": "2026-11-17",
            "status": "超时", "pending": True, "abnormal": True,
        })

    def _log_retry_success(self, task: dict):
        """手动重试也要有调用日志可对账；没有起始日志就先补一条再回写成功。"""
        tid = str(task["id"]).zfill(4)
        code = f"CALL-RETRY-{tid}"
        log = next((row for row in store.rows("call_log") if row.get("记录编号") == code), None)
        if log is None:
            self._log_retry_start(task)
            log = next(row for row in store.rows("call_log") if row.get("记录编号") == code)
        log["status"] = "成功"
        log["响应状态"] = "200"
        log["响应耗时(ms)"] = 150
        log["响应耗时"] = 150
        log["返回行数"] = 1
        log["错误信息"] = ""
        log["pending"] = False
        log["abnormal"] = False

    def _update_monitor_health(self, task: dict):
        """重试成功后联动刷新监控健康度。"""
        endpoint = task.get("失败端点", "")
        for mon in store.rows("api_monitor"):
            if mon.get("目标端点") == endpoint:
                mon["最近采样"] = "2026-11-17"
                if mon.get("status") == "已告警":
                    mon["status"] = "监控中"
                    mon["pending"] = True

    def _trigger_alert(self, task: dict):
        """永久失败触发监控告警和治理报告。"""
        endpoint = task.get("失败端点", "")
        tid = str(task["id"]).zfill(4)
        # 更新监控状态
        for mon in store.rows("api_monitor"):
            if mon.get("目标端点") == endpoint:
                mon["status"] = "已告警"
                mon["pending"] = False
                mon["最近采样"] = "2026-11-17"
        # 创建治理报告条目
        gov_rows = store.rows("govern_report")
        gov_rows.append({
            "id": max((int(r.get("id", 0)) for r in gov_rows), default=0) + 1,
            "报告编号": f"GOVE-RETRY-{tid}",
            "评估范围": f"重放队列永久失败: {endpoint}",
            "合规指标": "数据采集完整性",
            "当前得分": 60.0,
            "改进事项": f"任务{tid}重试耗尽，需人工介入",
            "评估周期": "2026-Q4",
            "负责人": "运维值班",
            "生成时间": "2026-11-17",
            "status": "待生成", "pending": True, "abnormal": True,
        })

    def calc_recovery_rate(self) -> dict:
        """跨模块恢复率统计：分析重放队列成功/失败分布及关联调用日志。"""
        retries = store.rows("retry_queue")
        total = len(retries)
        success = sum(1 for r in retries if r.get("status") == "已成功")
        perm_fail = sum(1 for r in retries if r.get("status") == "永久失败")
        # 关联调用日志
        logs_related = [
            l for l in store.rows("call_log")
            if "retry" in str(l.get("请求参数", ""))
        ]
        return {
            "total_retries": total,
            "with_source": sum(1 for r in retries if r.get("归属编号")),
            "unresolved_source": sum(1 for r in retries if r.get("待核理由")),
            "success": success,
            "permanent_fail": perm_fail,
            "recovery_rate": success / total if total else 0.0,
            "logged_events": len(logs_related),
        }