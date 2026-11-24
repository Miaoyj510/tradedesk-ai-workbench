# -*- coding: utf-8 -*-
"""发布服务 —— 灰度推进与分类别回滚（S6-3；PRD 13.4.8）。

四个动作，对应 PRD 13.4.8 的四件事：

    create()   —— 建一次发布（审批人 / 变更说明 / 回滚预案必填）
    advance()  —— 推进档位（PILOT → P20 → P50 → P100，每档观察 ≥ 1 个工作日）
    rollback() —— **按类别**回滚（app / agent / knowledge / sanctions 互不影响）
    resolve()  —— 一次询盘实际命中哪一版（按企业分桶，语种可限定）

红线
----
* 回滚**只切换版本指向**，不删数据、不改历史询盘 —— 历史草稿与审计原样保留。
* 任何写操作都写审计（release.create / release.advance / release.rollback）。
* 名单库（sanctions）变更必须附 L-6 风控回归证据，否则**建不了**。
* Agent / 提示词变更必须附回归证据，否则**建不了**。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .bucketing import in_canary
from .manifest import (CATEGORIES, COMPONENT_CATEGORY, COMPONENT_KEYS,
                       LANGS, OBSERVE_HOURS, STAGES, STATUS_CANARY,
                       STATUS_FULL, STATUS_ROLLED_BACK, EffectiveRelease,
                       ReleaseManifest, category_keys, utcnow)


class ReleaseError(RuntimeError):
    """发布域的业务错误（带错误码，便于 HTTP 层映射状态码）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__("%s: %s" % (code, message))


# 错误码 → HTTP 状态码。参数类错误 422（调用方改参数），状态冲突类 409（时序问题）。
HTTP_STATUS: Dict[str, int] = {
    "validation": 422,
    "regression_required": 422,
    "risk_evidence_required": 422,
    "bad_category": 422,
    "not_found": 404,
}


def http_status_for(code: str) -> int:
    """错误码 → HTTP 状态码（其余一律 409）。"""
    return HTTP_STATUS.get(code, 409)


class ReleaseService:
    """发布 / 灰度 / 回滚的领域服务。

    `store` 只需实现 base.StateStore 里的 6 个发布方法；
    `defaults` 是基线组件版本（见 manifest.baseline_components）。
    """

    def __init__(self, store, defaults: Dict[str, str],
                 observe_hours: int = OBSERVE_HOURS) -> None:
        self.store = store
        self.defaults = dict(defaults or {})
        self.observe_hours = int(observe_hours)

    # ------------------------------------------------------------ 读取
    def stable_components(self) -> Dict[str, str]:
        """当前**稳定版**组件版本 = 基线 + 各类别已发布的别名。

        别名按类别解析：knowledge 类别指向新库，app 类别还指着旧镜像 —— 这就是
        "四类独立回滚"的落地形态。
        """
        comps = dict(self.defaults)
        aliases = self.store.list_release_aliases() or {}
        for cat, alias in aliases.items():
            src = (alias or {}).get("components") or {}
            for key in category_keys(cat):
                val = src.get(key)
                if val:
                    comps[key] = str(val)
        return comps

    def get(self, release_id: str) -> Optional[ReleaseManifest]:
        return self.store.get_release(release_id)

    def list(self) -> List[ReleaseManifest]:
        return list(self.store.list_releases())

    def canary(self) -> Optional[ReleaseManifest]:
        """当前在灰度中的发布（同时只允许一个）。"""
        for r in self.store.list_releases():
            if r.status == STATUS_CANARY:
                return r
        return None

    def status(self) -> Dict[str, Any]:
        return {
            "defaults": dict(self.defaults),
            "stable": self.stable_components(),
            "releases": [r.model_dump(mode="json") for r in self.store.list_releases()],
            "aliases": self.store.list_release_aliases() or {},
            "observe_hours": self.observe_hours,
            "categories": list(CATEGORIES),
            "stages": list(STAGES),
            "langs": list(LANGS),
        }

    # ------------------------------------------------------------ 创建发布
    def create(self, *, components: Optional[Dict[str, str]] = None,
               approver: str = "", change_notes: str = "", rollback_plan: str = "",
               regression_evidence: str = "", risk_evidence: str = "",
               pilot_companies: Optional[List[str]] = None,
               langs: Optional[List[str]] = None,
               release_id: str = "",
               now: Optional[datetime] = None,
               actor: str = "") -> ReleaseManifest:
        """建一次灰度发布。

        合并基线 = **当前稳定版**（逐类别用别名解析），再叠加本次指定的组件。
        为什么不是"用默认值合并"：上一个发布转全量后，如果这次只改知识库，
        用默认值合并会把没提到的组件**隐性回退**到基线 —— 那是一次没打算做的回滚。
        """
        now = now or utcnow()
        approver = str(approver or "").strip()
        change_notes = str(change_notes or "").strip()
        rollback_plan = str(rollback_plan or "").strip()
        regression_evidence = str(regression_evidence or "").strip()
        risk_evidence = str(risk_evidence or "").strip()

        # ---- 1. 审批三件套必填（PRD 13.4.8：审计记审批人 / 变更 / 回滚预案） ----
        missing = [n for n, v in (("审批人", approver), ("变更说明", change_notes),
                                  ("回滚预案", rollback_plan)) if not v]
        if missing:
            raise ReleaseError("validation", "缺少必填项：" + "、".join(missing))

        # ---- 2. 灰度范围：白名单至少 1 家；语种必须受支持 ----
        pilot = [str(c).strip() for c in (pilot_companies or []) if str(c).strip()]
        if not pilot:
            raise ReleaseError("validation", "灰度白名单至少要有 1 家企业（PILOT 档靠它选人）")
        lang_list = [str(x).strip() for x in (langs or []) if str(x).strip()]
        bad_langs = [x for x in lang_list if x not in LANGS]
        if bad_langs:
            raise ReleaseError("validation", "不支持的语种：" + "、".join(bad_langs))

        # ---- 3. 组件校验：只认已知组件，版本号不能为空 ----
        given = {k: str(v).strip() for k, v in dict(components or {}).items()}
        unknown = [k for k in given if k not in COMPONENT_CATEGORY]
        if unknown:
            raise ReleaseError("validation", "未知组件：" + "、".join(sorted(unknown)))
        empty = [k for k, v in given.items() if not v]
        if empty:
            raise ReleaseError("validation", "组件版本号为空：" + "、".join(sorted(empty)))

        base = self.stable_components()
        merged = dict(base)
        merged.update(given)
        changed = sorted({COMPONENT_CATEGORY[k] for k in given
                          if merged.get(k) != base.get(k)})
        if not changed:
            raise ReleaseError("no_change", "本次发布的组件与当前稳定版完全一致，无需发布")

        # ---- 4. 证据要求：Agent 变更要回归证据，名单变更要 L-6 风控证据 ----
        if "agent" in changed and not regression_evidence:
            raise ReleaseError("regression_required",
                               "Agent / 提示词变更必须附回归证据（回归报告编号或链接）")
        if "sanctions" in changed and not risk_evidence:
            raise ReleaseError("risk_evidence_required",
                               "名单库变更必须附 L-6 风控回归证据（PRD 13.4.8）")

        # ---- 5. 发布号不能重复（输入问题先报，再谈状态冲突） ----
        rid = str(release_id or "").strip() or self._next_release_id(now)
        if self.store.get_release(rid) is not None:
            raise ReleaseError("duplicate_release", "发布号 %s 已存在" % rid)

        # ---- 6. 同时只允许一个灰度 ----
        running = self.canary()
        if running is not None:
            raise ReleaseError("canary_in_flight",
                               "已有灰度发布 %s 在进行中（档位 %s），先推进或回滚它"
                               % (running.release_id, running.stage))

        manifest = ReleaseManifest(
            release_id=rid,
            components=merged,
            approver=approver,
            change_notes=change_notes,
            rollback_plan=rollback_plan,
            regression_evidence=regression_evidence,
            risk_evidence=risk_evidence,
            pilot_companies=pilot,
            langs=lang_list,
            stage="PILOT",
            stage_entered_at=now,
            status=STATUS_CANARY,
            changed_categories=changed,
            created_at=now,
            updated_at=now,
        )
        self.store.save_release(manifest)
        self.store.append_audit(actor or approver, "release.create", rid, {
            "components": merged,
            "changed_categories": changed,
            "approver": approver,
            "change_notes": change_notes,
            "rollback_plan": rollback_plan,
            "pilot_companies": pilot,
            "langs": lang_list,
            "regression_evidence": regression_evidence,
            "risk_evidence": risk_evidence,
        })
        return manifest

    # ------------------------------------------------------------ 推进档位
    def advance(self, release_id: str, now: Optional[datetime] = None,
                actor: str = "") -> ReleaseManifest:
        """推进一档（PILOT → P20 → P50 → P100）。

        三条规矩：
        * **观察窗没到不许进档**（PRD：每档观察 ≥ 1 个工作日）；
        * **一次只进一档**（不许跳档）：跳档 = 跳过了中间的观察期；
        * 到 P100 = 全量：四个类别的别名统一切到本发布（已回滚的类别不重新接管）。
        """
        now = now or utcnow()
        manifest = self.store.get_release(release_id)
        if manifest is None:
            raise ReleaseError("not_found", "发布 %s 不存在" % release_id)
        if manifest.status != STATUS_CANARY:
            raise ReleaseError("not_canary",
                               "发布 %s 当前状态是 %s，不能继续放量" % (release_id, manifest.status))
        elapsed = (now - manifest.stage_entered_at).total_seconds()
        need = self.observe_hours * 3600
        if elapsed < need:
            remain = (need - elapsed) / 3600.0
            raise ReleaseError("observe_window",
                               "当前档位 %s 的观察期未满 %d 小时（还剩约 %.1f 小时）"
                               % (manifest.stage, self.observe_hours, remain))
        idx = STAGES.index(manifest.stage)
        if idx >= len(STAGES) - 1:
            raise ReleaseError("observe_window", "已经在 %s 档，无需继续推进" % manifest.stage)

        prev_stage = manifest.stage
        manifest.stage = STAGES[idx + 1]
        manifest.stage_entered_at = now
        manifest.updated_at = now
        if manifest.stage == "P100":
            manifest.status = STATUS_FULL
            for cat in CATEGORIES:
                if cat in manifest.rolled_back_categories:
                    # 已回滚的类别不重新接管：回滚是人的决定，不该被推进覆盖
                    continue
                self.store.set_release_alias(cat, manifest.release_id, manifest.components)
        self.store.save_release(manifest)
        self.store.append_audit(actor or "system", "release.advance", release_id, {
            "from_stage": prev_stage,
            "to_stage": manifest.stage,
            "status": manifest.status,
            "components": manifest.components,
        })
        return manifest

    # ------------------------------------------------------------ 回滚
    def rollback(self, release_id: str, category: str, now: Optional[datetime] = None,
                 actor: str = "") -> ReleaseManifest:
        """**按类别**回滚：把该类别的版本指向切回上一个版本。

        回滚只改"版本指向"：历史草稿、已发邮件、审计链一律不动。
        找不到更早的版本时回到基线（defaults）。
        """
        now = now or utcnow()
        category = str(category or "").strip()
        if category not in CATEGORIES:
            raise ReleaseError("bad_category",
                               "回滚类别非法：%s（合法值：%s）" % (category, "、".join(CATEGORIES)))
        manifest = self.store.get_release(release_id)
        if manifest is None:
            raise ReleaseError("not_found", "发布 %s 不存在" % release_id)

        prev = self._previous_release(manifest, category)
        prev_components = dict(prev.components) if prev is not None else dict(self.defaults)
        prev_rid = prev.release_id if prev is not None else ""
        self.store.set_release_alias(category, prev_rid, prev_components)

        if category not in manifest.rolled_back_categories:
            manifest.rolled_back_categories = sorted(
                set(manifest.rolled_back_categories) | {category})
        if manifest.changed_categories and \
                set(manifest.changed_categories) <= set(manifest.rolled_back_categories):
            manifest.status = STATUS_ROLLED_BACK
        manifest.updated_at = now
        self.store.save_release(manifest)

        keys = category_keys(category)
        self.store.append_audit(actor or "system", "release.rollback", release_id, {
            "category": category,
            "from_release": release_id,
            "to_release": prev_rid or "baseline",
            "from_components": {k: manifest.components.get(k, "") for k in keys},
            "to_components": {k: prev_components.get(k, "") for k in keys},
            "rollback_plan": manifest.rollback_plan,
            "rolled_back_categories": list(manifest.rolled_back_categories),
            "status": manifest.status,
        })
        return manifest

    # ------------------------------------------------------------ 分流
    def resolve(self, company: str = "", lang: str = "") -> EffectiveRelease:
        """一次询盘实际命中哪一版。

        优先级：命中灰度 → 灰度版本（已回滚的类别仍走稳定版）；
        否则 → 稳定版（别名）；再否则 → 基线（等于没发布过）。
        """
        canary = self.canary()
        if canary is not None and in_canary(canary, company, lang):
            comps = self.stable_components()
            for cat in CATEGORIES:
                if cat in canary.rolled_back_categories:
                    continue
                for key in category_keys(cat):
                    val = canary.components.get(key)
                    if val:
                        comps[key] = val
            return EffectiveRelease(components=comps, source="canary",
                                    release_id=canary.release_id)
        aliases = self.store.list_release_aliases() or {}
        rids = sorted({str((a or {}).get("release_id") or "") for a in aliases.values()})
        return EffectiveRelease(
            components=self.stable_components(),
            source="stable" if aliases else "baseline",
            release_id=rids[0] if len(rids) == 1 else "")

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _next_release_id(now: datetime) -> str:
        return "REL-%s-%s" % (now.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:4])

    def _previous_release(self, manifest: ReleaseManifest,
                          category: str) -> Optional[ReleaseManifest]:
        """该类别上一个可用版本（最近一次该类别内容不同的更早发布）。"""
        keys = category_keys(category)
        candidates: List[ReleaseManifest] = []
        for r in self.store.list_releases():
            if r.release_id == manifest.release_id:
                continue
            if r.created_at >= manifest.created_at:
                continue
            if any(r.components.get(k) != manifest.components.get(k) for k in keys):
                candidates.append(r)
        candidates.sort(key=lambda r: (r.created_at, r.release_id))
        return candidates[-1] if candidates else None