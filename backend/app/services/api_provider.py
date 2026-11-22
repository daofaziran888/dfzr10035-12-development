"""API提供方领域服务：管理API源注册，编排下游鉴权、限流、端点、监控的联动创建。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.store import store


class ApiProviderService(BaseModuleService):
    MODULE = "api_provider"
    ENTITY = "API提供方"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["提供方编号", "提供方名称", "认证方式", "BaseURL", "SLA承诺", "QPS上限", "接入状态", "注册时间"]
    REQUIRED_FIELDS = ["提供方编号", "提供方名称", "认证方式"]
    STATUS_ORDER = ["待审核", "已注册", "SLA降级", "已停用"]
    ACTION_RULES = {"审核注册": "已注册", "标记降级": "SLA降级", "停用来源": "已停用"}
    # 改名不改变接入状态，单独处理后把旧名留进关联归属的历史名称。
    SIDE_EFFECT_ACTIONS = ["提供方改名"]
    NEGATIVE_ACTIONS = ["停用来源"]

    def run_action(self, entry_id: int, action: str):
        """审核注册时联动创建鉴权方案、端点定义和限流策略。"""
        if action == "提供方改名":
            return self._rename_provider(entry_id)
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "审核注册":
            self._orchestrate_downstream(entry)
            message += "，已联动创建鉴权/端点/限流/监控/用量管线和来源归属"
        return entry, message

    def _rename_provider(self, entry_id: int):
        """右侧改名：提供方编号不变，展示名称更新，旧名写入端点与归属清单历史。"""
        provider = store.find(self.MODULE, entry_id)
        if provider is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        new_name = str(self.action_values.get("新名称") or "").strip()
        if not new_name:
            self.last_error = "提供方改名需要提供 values.新名称；提供方编号保持不变"
            self.action_blocked = True
            return None, self.last_error
        old_name = str(provider.get("提供方名称") or "")
        if new_name == old_name:
            self.last_error = f"新名称「{new_name}」与当前名称相同，未做改名"
            self.action_blocked = True
            return None, self.last_error
        provider["提供方名称"] = new_name
        provider["历史名称"] = ",".join(
            filter(None, [str(provider.get("历史名称") or ""), old_name])
        )

        # 下游按名称展示的模块同步改名，判定仍走稳定编号。
        for endpoint in store.rows("endpoint_def"):
            if str(endpoint.get("提供方")) == old_name:
                endpoint["提供方"] = new_name
                names = [n for n in str(endpoint.get("历史名称") or "").split(",") if n]
                if old_name not in names:
                    names.append(old_name)
                endpoint["历史名称"] = ",".join(names)
        for auth in store.rows("auth_profile"):
            if str(auth.get("提供方")) == old_name:
                auth["提供方"] = new_name
        for cost in store.rows("cost_account"):
            if str(cost.get("提供方")) == old_name:
                cost["提供方"] = new_name
        # 归属清单：旧名称进入历史名称，提供方编号与端点编号不变。
        for attribution in store.rows("source_attribution"):
            if str(attribution.get("提供方编号")) == str(provider.get("提供方编号")):
                names = [n for n in str(attribution.get("历史名称") or "").split(",") if n]
                if old_name not in names:
                    names.append(old_name)
                attribution["历史名称"] = ",".join(names)
                attribution["提供方名称"] = new_name
        return provider, (
            f"提供方{provider.get('提供方编号')}名称已由「{old_name}」改为「{new_name}」，"
            "编号不变，原归属仍按编号可查"
        )

    def _orchestrate_downstream(self, provider: dict):
        """上游注册通过后，一次性联动6个下游模块的记录创建。"""
        pid = str(provider["id"])
        pname = str(provider["提供方名称"])
        pbase = str(provider["BaseURL"])
        # 1) 鉴权方案
        auth_rows = store.rows("auth_profile")
        auth_rows.append({
            "id": max((int(r.get("id", 0)) for r in auth_rows), default=0) + 1,
            "方案编号": f"AUTH-{pid.zfill(4)}",
            "提供方": pname,
            "鉴权类型": "BearerToken",
            "密钥版本": "v1",
            "Token过期(秒)": 7200,
            "刷新策略": "自动刷新",
            "轮换周期": "30天",
            "最近轮换": provider.get("注册时间", ""),
            "status": "草稿", "pending": True, "abnormal": False,
        })
        # 2) 端点定义
        ep_rows = store.rows("endpoint_def")
        ep_rows.append({
            "id": max((int(r.get("id", 0)) for r in ep_rows), default=0) + 1,
            "端点编号": f"ENDP-{pid.zfill(4)}",
            "端点展示名称": f"{pname}默认数据接口",
            "提供方": pname,
            "提供方编号": str(provider.get("提供方编号") or ""),
            "请求路径": f"{pbase}/v1/data",
            "请求方法": "GET",
            "参数Schema": '{"page": "int", "size": "int"}',
            "响应Schema": '{"code": "int", "data": "array"}',
            "分页策略": "page_size",
            "描述文档": f"{pname}数据查询接口",
            "status": "草稿", "pending": True, "abnormal": False,
        })
        # 3) 限流策略
        rl_rows = store.rows("rate_limit")
        rl_rows.append({
            "id": max((int(r.get("id", 0)) for r in rl_rows), default=0) + 1,
            "策略编号": f"RATE-{pid.zfill(4)}",
            "目标端点": f"ENDP-{pid.zfill(4)}",
            "QPS上限": str(provider.get("QPS上限", "100")),
            "滑动窗口": "60s",
            "熔断阈值(fail%)": 30.0,
            "恢复时长(s)": 60,
            "排队队列": "FIFO-1000",
            "当前状态": "待生效",
            "status": "草稿", "pending": True, "abnormal": False,
        })
        # 4) 监控指标
        mon_rows = store.rows("api_monitor")
        mon_rows.append({
            "id": max((int(r.get("id", 0)) for r in mon_rows), default=0) + 1,
            "指标编号": f"API_-{pid.zfill(4)}",
            "目标端点": f"ENDP-{pid.zfill(4)}",
            "监控类型": "可用性+延迟",
            "采集间隔": "30s",
            "告警阈值": "延迟>2000ms",
            "告警渠道": "webhook",
            "静默时段": provider.get("注册时间", ""),
            "最近采样": provider.get("注册时间", ""),
            "status": "待启用", "pending": True, "abnormal": False,
        })
        # 5) 用量账单
        cost_rows = store.rows("cost_account")
        cost_rows.append({
            "id": max((int(r.get("id", 0)) for r in cost_rows), default=0) + 1,
            "账单编号": f"COST-{pid.zfill(4)}",
            "提供方": pname,
            "提供方编号": str(provider.get("提供方编号") or ""),
            "计费周期": "monthly",
            "调用次数": 0,
            "数据流量(MB)": 0,
            "超出配额": "否",
            "费用预估": 0.0,
            "账单状态": "初始",
            "status": "统计中", "pending": True, "abnormal": False,
        })
        # 6) 来源归属：注册即把新端点登记为唯一已确认来源，后续批次可按编号领取。
        attr_rows = store.rows("source_attribution")
        attr_rows.append({
            "id": max((int(r.get("id", 0)) for r in attr_rows), default=0) + 1,
            "归属编号": f"ATTR-{pid.zfill(4)}",
            "端点编号": f"ENDP-{pid.zfill(4)}",
            "端点展示名称": f"{pname}默认数据接口",
            "请求路径": f"{pbase}/v1/data",
            "请求参数": "{}",
            "提供方编号": str(provider.get("提供方编号") or ""),
            "提供方名称": pname,
            "历史名称": "",
            "待核理由": "",
            "确认依据": f"提供方{pid}审核注册时联动创建，编号唯一",
            "登记时间": provider.get("注册时间", ""),
            "status": "已确认", "pending": False, "abnormal": False,
        })
        # 7) 调用日志（注册事件）
        log_rows = store.rows("call_log")
        log_rows.append({
            "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
            "记录编号": f"CALL-{pid.zfill(4)}",
            "接口端点": f"ENDP-{pid.zfill(4)}",
            "请求参数": provider.get("注册时间", ""),
            "响应状态": "200",
            "响应耗时(ms)": 0,
            "返回行数": 0,
            "错误信息": "",
            "调用时间": provider.get("注册时间", ""),
            "status": "成功", "pending": True, "abnormal": False,
        })

    def check_sla_health(self, provider_id: int) -> dict:
        """跨模块SLA巡检：读取关联的限流/监控/用量数据评估健康度。"""
        provider = store.find("api_provider", provider_id)
        if not provider:
            return {"ok": False, "message": "提供方不存在"}
        pid = str(provider_id).zfill(4)
        # 从限流模块读取策略
        rate_rows = [
            r for r in store.rows("rate_limit")
            if r.get("策略编号") == f"RATE-{pid}"
        ]
        rate_ok = any(r.get("status") == "生效中" for r in rate_rows)
        # 从监控模块读取告警
        mon_rows = [
            m for m in store.rows("api_monitor")
            if m.get("指标编号") == f"API_-{pid}"
        ]
        mon_alert = any(m.get("status") == "已告警" for m in mon_rows)
        # 从用量模块读取超额
        cost_rows = [
            c for c in store.rows("cost_account")
            if c.get("账单编号") == f"COST-{pid}"
        ]
        cost_over = any(c.get("status") == "超额告警" for c in cost_rows)
        return {
            "provider_id": provider_id,
            "provider_name": provider["提供方名称"],
            "sla_ok": rate_ok and not mon_alert and not cost_over,
            "rate_limit_active": rate_ok,
            "monitor_alert": mon_alert,
            "cost_overrun": cost_over,
        }