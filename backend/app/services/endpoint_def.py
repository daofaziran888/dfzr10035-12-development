"""接口端点定义领域服务：管理API端点，与 api_provider、response_parse、rate_limit、schema_diff 联动。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.source_attribution import append_history
from app.store import store


class EndpointDefService(BaseModuleService):
    MODULE = "endpoint_def"
    ENTITY = "接口端点"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["端点编号", "端点展示名称", "提供方", "请求路径", "请求方法", "参数Schema", "响应Schema", "分页策略", "描述文档"]
    REQUIRED_FIELDS = ["端点编号", "提供方", "请求路径"]
    STATUS_ORDER = ["草稿", "联调中", "已投产", "Schema变更"]
    ACTION_RULES = {"提交联调": "联调中", "投产发布": "已投产", "标记变更": "Schema变更"}
    # 端点改名不改变状态，只改展示名称并留历史名，因此单独在处理分支里实现。
    SIDE_EFFECT_ACTIONS = ["端点改名"]
    NEGATIVE_ACTIONS = []

    def create_entry(self, values):
        """端点编号是归属判定与鉴权编排用的稳定编号，展示名称只负责展示。"""
        self.last_error = ""
        endpoint_id = str(values.get("端点编号") or "").strip()
        path = str(values.get("请求路径") or "").strip()
        provider_ref = str(values.get("提供方") or "").strip()
        if endpoint_id and path:
            for endpoint in store.rows(self.MODULE):
                if (
                    str(endpoint.get("端点编号")) == endpoint_id
                    or (
                        str(endpoint.get("提供方")) == provider_ref
                        and str(endpoint.get("请求路径")) == path
                    )
                ):
                    self.last_error = (
                        f"同一提供方{provider_ref}下请求路径{path}已登记为"
                        f"端点{endpoint.get('端点编号')}，不能重复建号"
                    )
                    return None, []
        entry, missing = super().create_entry(values)
        if entry is not None:
            entry["端点展示名称"] = (
                str(values.get("端点展示名称") or "").strip()
                or str(values.get("描述文档") or "")
                or endpoint_id
            )
            entry["历史名称"] = str(values.get("历史名称") or "")
            # 归属判定依赖提供方编号；登记时就从提供方注册信息里落下来，
            # 不让后续归属清单只能靠名称猜。
            entry["提供方编号"] = str(values.get("提供方编号") or "").strip()
            if not entry["提供方编号"]:
                entry["提供方编号"] = self._provider_id(provider_ref)
        return entry, missing

    @staticmethod
    def _provider_id(provider_ref: str) -> str:
        ref = str(provider_ref or "").strip()
        for provider in store.rows("api_provider"):
            if str(provider.get("提供方编号")) == ref:
                return ref
            if str(provider.get("提供方名称")) == ref:
                return str(provider.get("提供方编号") or "")
        return ""

    def run_action(self, entry_id: int, action: str):
        """投产发布时联动创建响应解析规则和Schema差异记录；Schema变更时通知缓存刷新。"""
        if action == "端点改名":
            return self._rename_endpoint(entry_id)
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "投产发布":
            self._create_parse_rule(entry)
            self._create_schema_diff(entry, "initial")
            self._sync_rate_limit_creation(entry)
            message += "，已联动创建解析规则/Schema记录/限流策略"
        elif action == "标记变更":
            self._create_schema_diff(entry, "upgrade")
            self._notify_cache_refresh(entry)
            message += "，已记录Schema差异并通知缓存刷新"
        return entry, message

    def _rename_endpoint(self, entry_id: int):
        """改名只动展示名称，端点编号不变；旧名进入归属清单历史名称。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        new_name = str(self.action_values.get("新名称") or "").strip()
        if not new_name:
            self.last_error = "端点改名需要提供 values.新名称；端点编号保持不变"
            self.action_blocked = True
            return None, self.last_error
        old_name = str(entry.get("端点展示名称") or "")
        old_desc = str(entry.get("描述文档") or "")
        entry["端点展示名称"] = new_name
        append_history(entry, old_name, old_desc)
        # 同步到归属清单：判定依据仍是端点编号，展示与历史名称跟着改。
        for attribution in store.rows("source_attribution"):
            if str(attribution.get("端点编号")) == str(entry.get("端点编号")):
                append_history(attribution, old_name, old_desc)
                if str(attribution.get("端点展示名称") or "") == old_name:
                    attribution["端点展示名称"] = new_name
        return entry, (
            f"端点{entry.get('端点编号')}展示名称已由「{old_name}」改为「{new_name}」，"
            "端点编号不变，历史名称已留痕"
        )

    def _create_parse_rule(self, endpoint: dict):
        """投产时自动创建响应解析规则。"""
        parse_rows = store.rows("response_parse")
        eid = str(endpoint["id"]).zfill(4)
        parse_rows.append({
            "id": max((int(r.get("id", 0)) for r in parse_rows), default=0) + 1,
            "规则编号": f"RESP-{endpoint['端点编号']}",
            "目标端点": endpoint["端点编号"],
            "JSON路径": "$.data[*]",
            "字段别名": "source_field",
            "数据类型": "string",
            "默认值": "",
            "解析顺序": "1",
            "映射描述": f"{endpoint.get('请求路径', '')}响应解析",
            "status": "草稿", "pending": True, "abnormal": False,
        })

    def _create_schema_diff(self, endpoint: dict, reason: str):
        diff_rows = store.rows("schema_diff")
        eid = str(endpoint["id"]).zfill(4)
        diff_rows.append({
            "id": max((int(r.get("id", 0)) for r in diff_rows), default=0) + 1,
            "记录编号": f"SCHE-{endpoint['端点编号']}",
            "关联端点": endpoint["端点编号"],
            "变更字段": "responseSchema" if reason == "upgrade" else "initialSchema",
            "旧定义": endpoint.get("响应Schema", "") if reason == "upgrade" else "",
            "新定义": endpoint.get("响应Schema", "") if reason == "initial" else "upgraded",
            "差异等级": "MAJOR" if reason == "upgrade" else "INFO",
            "影响评估": f"端点{eid}投产{'变更' if reason == 'upgrade' else '初始化'}",
            "检测时间": "2026-11-17",
            "status": "已检测", "pending": True, "abnormal": reason == "upgrade",
        })

    def _sync_rate_limit_creation(self, endpoint: dict):
        """确保投产端点有关联限流策略。"""
        endpoint_id = endpoint["端点编号"]
        exist = any(
            rl.get("目标端点") == endpoint_id
            for rl in store.rows("rate_limit")
        )
        if not exist:
            rl_rows = store.rows("rate_limit")
            eid = str(endpoint["id"]).zfill(4)
            rl_rows.append({
                "id": max((int(r.get("id", 0)) for r in rl_rows), default=0) + 1,
                "策略编号": f"RATE-{endpoint_id}",
                "目标端点": endpoint_id,
                "QPS上限": "100",
                "滑动窗口": "60s",
                "熔断阈值(fail%)": 30.0,
                "恢复时长(s)": 60,
                "排队队列": "FIFO-1000",
                "当前状态": "草稿",
                "status": "草稿", "pending": True, "abnormal": False,
            })

    def _notify_cache_refresh(self, endpoint: dict):
        """Schema变更通知关联缓存刷新。"""
        endpoint_id = endpoint["端点编号"]
        for cache in store.rows("data_cache"):
            if cache.get("目标端点") == endpoint_id:
                cache["最后刷新"] = "2026-11-17"
                cache["status"] = "容量告警"
                cache["pending"] = True

    def check_endpoint_chain(self, endpoint_id: str) -> dict:
        """跨模块端点链路检查：端点→提供方→限流→解析规则→Schema差异。"""
        endpoints = [e for e in store.rows("endpoint_def") if e.get("端点编号") == endpoint_id]
        if not endpoints:
            return {"ok": False, "message": "端点不存在"}
        ep = endpoints[0]
        provider = ep.get("提供方", "")
        rate_limits = [rl for rl in store.rows("rate_limit") if rl.get("目标端点") == endpoint_id]
        parse_rules = [p for p in store.rows("response_parse") if p.get("目标端点") == endpoint_id]
        schema_diffs = [sd for sd in store.rows("schema_diff") if sd.get("关联端点") == endpoint_id]
        return {
            "endpoint": endpoint_id,
            "provider": provider,
            "rate_limits": len(rate_limits),
            "parse_rules": len(parse_rules),
            "schema_diffs": len(schema_diffs),
            "production_ready": len(rate_limits) > 0 and len(parse_rules) > 0,
        }