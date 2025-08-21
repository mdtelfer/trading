from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import os
import time
from typing import Any

import psycopg2
from psycopg2.extras import Json
import yaml

"""
Macro Evaluator (fast/slow → fused)
-----------------------------------
- Lee reglas desde YAML (configs/fundamental_rules.yaml por defecto)
- Toma snapshot de features desde core.v_macro_latest
- Evalúa: hard_blocks → risk_bands → scenarios → overrides
- Inserta resultados en core.macro_state (tier: fast, slow) y uno fusionado (fused)
"""

# ---------------------------------------------------------------------------
# Logger local
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG,  # cambia a INFO si prefieres menos ruido
    format="%(asctime)s | %(levelname)s | macro_evaluator | %(message)s",
)
logger = logging.getLogger("macro_evaluator")


# ---------------------------------------------------------------------------
# Utilidades básicas
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(UTC)


def env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v is not None else default


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Carga YAML y helpers
# ---------------------------------------------------------------------------


@dataclass
class Rules:
    raw: dict[str, Any]
    portfolio_groups: dict[str, Any]
    eval_cfg: dict[str, Any]
    fuse_cfg: dict[str, Any]
    hard_blocks: list[dict[str, Any]]
    risk_bands: list[dict[str, Any]]
    scenarios: list[dict[str, Any]]
    overrides: dict[str, Any]


def load_rules(path: str) -> Rules:
    logger.info(f"Cargando reglas: {path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    rules = Rules(
        raw=data,
        portfolio_groups=data.get("portfolio", {}).get("groups", {}),
        eval_cfg=data.get("evaluation", {}),
        fuse_cfg=data.get("fuse", {}),
        hard_blocks=data.get("hard_blocks", []),
        risk_bands=data.get("risk_bands", []),
        scenarios=data.get("scenarios", []),
        overrides=data.get("symbol_overrides", {}),
    )
    logger.info(
        f"Reglas cargadas | groups={len(rules.portfolio_groups)} "
        f"hard_blocks={len(rules.hard_blocks)} "
        f"risk_bands={len(rules.risk_bands)} "
        f"scenarios={len(rules.scenarios)}"
    )
    return rules


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


@dataclass
class DBCreds:
    host: str
    port: int
    dbname: str
    user: str
    password: str


def load_db_creds() -> DBCreds:
    return DBCreds(
        host=env_str("DB_HOST", "localhost"),
        port=env_int("DB_PORT", 5432),
        dbname=env_str("DB_NAME", "trading"),
        user=env_str("DB_USER", "postgres"),
        password=env_str("DB_PASSWORD", "postgres"),
    )


def get_latest_features(conn) -> list[dict[str, Any]]:
    """Lee snapshot simple de core.v_macro_latest."""
    sql = """
        SELECT feature, value, ts_utc, aux_values, status
        FROM core.v_macro_latest
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for feature, value, ts, aux, status in rows:
        out.append(
            {
                "feature": feature,
                "value": float(value) if value is not None else None,
                "ts": ts,
                "aux": aux or {},
                "status": status,
            }
        )
    return out


def insert_macro_state(
    conn,
    *,
    tier: str,
    ts: datetime,
    long_permission: bool,
    risk_multiplier: float,
    allowed_groups: list[str],
    prioritize: list[str],
    avoid: list[str],
    reason: str | None,
    meta: dict[str, Any],
) -> None:
    sql = """
        INSERT INTO core.macro_state (
            ts, tier, long_permission, risk_multiplier,
            allowed_groups, prioritize, avoid, reason, meta
        ) VALUES (
            %(ts)s, %(tier)s, %(lp)s, %(rm)s,
            %(ag)s, %(prio)s, %(avoid)s, %(reason)s, %(meta)s
        )
    """
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(
            sql,
            dict(
                ts=ts,
                tier=tier,
                lp=long_permission,
                rm=risk_multiplier,
                ag=allowed_groups,
                prio=prioritize,
                avoid=avoid,
                reason=reason,
                meta=Json(meta),
            ),
        )
    conn.commit()
    ms = int((time.time() - t0) * 1000)
    logger.info(
        f"Insert macro_state | tier={tier} rm={risk_multiplier:.2f} "
        f"groups={','.join(allowed_groups)} prio={len(prioritize)} avoid={len(avoid)} "
        f"hb={'+'.join(meta.get('hard_blocks', [])) if meta else ''} | ms={ms}"
    )


# ---------------------------------------------------------------------------
# Normalización de valores (unidades)
# ---------------------------------------------------------------------------


def normalize_value(feature: str, value: float | None, aux: dict[str, Any]) -> float | None:
    if value is None:
        return None
    # 1) si viene value_pct en aux, úsalo
    if isinstance(aux, dict) and "value_pct" in aux:
        try:
            return float(aux["value_pct"])
        except Exception:
            pass
    # 2) si aux.unit dice stored=decimal y original=percent => *100
    unit = aux.get("unit") if isinstance(aux, dict) else None
    if isinstance(unit, dict):
        if unit.get("stored") == "decimal" and unit.get("original") == "percent":
            return float(value) * 100.0
    # 3) valor tal cual
    return float(value)


# ---------------------------------------------------------------------------
# Evaluación de condiciones y escenarios
# ---------------------------------------------------------------------------

_OPS: dict[str, Callable[[float, float], bool]] = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def check_condition(cond: dict[str, Any], snapshot: dict[str, float]) -> tuple[bool, str | None]:
    """Devuelve (ok, missing_feature). snapshot tiene valores normalizados."""
    feat = cond.get("feature")
    if feat not in snapshot:
        return False, feat
    left = snapshot.get(feat)
    if left is None:
        return False, feat
    op = cond.get("op", "==")
    right = cond.get("value")
    try:
        fn = _OPS[op]
    except KeyError:
        return False, None
    try:
        return fn(float(left), float(right)), None
    except Exception:
        return False, None


def eval_clause_ex(
    clause: dict[str, Any], snapshot: dict[str, float]
) -> tuple[bool, list[str], int, int, str]:
    """
    Evalúa 'all'/'any' y devuelve:
      (resultado_bool, faltantes, true_count, total_count, mode)
    """
    missing: list[str] = []
    if "all" in clause:
        conds = clause["all"] or []
        oks = []
        for c in conds:
            ok, miss = check_condition(c, snapshot)
            if miss:
                missing.append(miss)
            oks.append(ok)
        true_count = sum(1 for x in oks if x)
        return all(oks), missing, true_count, len(conds), "all"

    if "any" in clause:
        conds = clause["any"] or []
        oks = []
        for c in conds:
            ok, miss = check_condition(c, snapshot)
            if miss:
                missing.append(miss)
            oks.append(ok)
        true_count = sum(1 for x in oks if x)
        return any(oks), missing, true_count, len(conds), "any"

    return False, missing, 0, 0, "none"


def eval_confirmations(
    conf_obj: Any, snapshot: dict[str, float]
) -> tuple[bool, list[str], list[dict]]:
    """
    Soporta:
      - dict con 'all'/'any' (un solo bloque)
      - lista de bloques [{'all': [...]}, {'any': [...]}]
    Devuelve: (ok_global, faltantes_acumulados, detalles_por_bloque)
    """
    if conf_obj is None:
        return True, [], []

    blocks = conf_obj if isinstance(conf_obj, list) else [conf_obj]
    all_missing: list[str] = []
    details: list[dict] = []
    ok_all = True

    for blk in blocks:
        ok, miss, true_cnt, total_cnt, mode = eval_clause_ex(blk, snapshot)
        all_missing.extend(miss)
        details.append({"mode": mode, "true": true_cnt, "total": total_cnt, "ok": ok})
        ok_all = ok_all and ok

    return ok_all, list(sorted(set(all_missing))), details


@dataclass
class EvalResult:
    tier: str
    ts: datetime
    long_permission: bool
    risk_multiplier: float
    allowed_groups: list[str]
    prioritize: list[str]
    avoid: list[str]
    reason: str | None
    meta: dict[str, Any]


def unique(seq: list[str]) -> list[str]:
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_buy_suggestions(
    active_scenarios: list[str],
    rules: Rules,
    avoids: list[str],
) -> list[dict[str, Any]]:
    votes: dict[str, int] = defaultdict(int)
    overrides: dict[str, dict[str, Any]] = {}

    for sc in rules.scenarios:
        name = sc.get("name")
        if name in active_scenarios:
            eff = sc.get("effect", {}) or {}
            syms = eff.get("prioritize_symbols", []) or []
            for s in syms:
                votes[s] += 1
            # overrides por símbolo (último gana)
            pov = eff.get("priority_overrides") or {}
            if isinstance(pov, dict):
                overrides.update(pov)

    def group_of(sym: str) -> str:
        for g, cfg in rules.portfolio_groups.items():
            if sym in (cfg or {}).get("symbols", []):
                return g
        return "CORE"

    suggestions: list[dict[str, Any]] = []
    for sym, cnt in votes.items():
        base = min(cnt, 2) * 0.35  # máx 0.70
        grp = group_of(sym)
        group_boost = 0.15 if grp == "CORE" else (0.05 if grp == "EXTENSION" else 0.0)
        penalty = -0.30 if sym in avoids else 0.0
        score = base + group_boost + penalty

        # aplicar overrides si existen
        ov = overrides.get(sym) or {}
        bump = float(ov.get("bump", 0.0))
        score = max(0.0, min(1.0, score + bump))
        tags = list(ov.get("tags", []) or [])

        suggestions.append(
            {
                "symbol": sym,
                "group": grp,
                "score": round(score, 2),
                "reasons": active_scenarios,
                "tags": tags,
            }
        )

    suggestions.sort(key=lambda x: x["score"], reverse=True)
    return suggestions


def evaluate_tier(
    tier: str,
    snapshot_rows: list[dict[str, Any]],
    rules: Rules,
) -> EvalResult:
    # Normalizar snapshot → feature -> value(float)
    snap: dict[str, float] = {}
    for row in snapshot_rows:
        val = normalize_value(row["feature"], row["value"], row.get("aux", {}))
        if val is not None:
            snap[row["feature"]] = float(val)

    ts = now_utc()
    block_is_terminal = bool(rules.eval_cfg.get("block_is_terminal", True))
    min_true = int(rules.eval_cfg.get("min_true_per_scenario", 2))

    # defaults
    long_permission = True
    risk_multiplier = 1.0
    allowed_groups = list(rules.portfolio_groups.keys()) or ["CORE"]
    prioritize: list[str] = []
    avoid: list[str] = []
    reason: str | None = None

    hard_hits: list[str] = []
    missing_overall: list[str] = []
    can_open_new = True
    blocked_by: str | None = None
    block_exclusions: list[str] = []

    # 1) hard blocks
    for hb in rules.hard_blocks:
        name = hb.get("name", "hard_block")
        ok, miss, _, _, _ = eval_clause_ex(hb, snap)
        if miss:
            missing_overall.extend(miss)
        if ok:
            hard_hits.append(name)
            eff = hb.get("effect", {}) or {}
            if eff.get("block_new_entries"):
                can_open_new = False
                blocked_by = name
            if "block_new_entries_excluding" in eff:
                can_open_new = False
                blocked_by = name
                block_exclusions = list(eff.get("block_new_entries_excluding", []) or [])
            if "cap_groups_to" in eff:
                caps = eff.get("cap_groups_to") or []
                allowed_groups = [g for g in allowed_groups if g in caps]
            if "risk_multiplier_macro" in eff:
                risk_multiplier = min(risk_multiplier, float(eff["risk_multiplier_macro"]))
            if block_is_terminal and (not can_open_new):
                break

    # 2) risk bands (conservador: tomar el menor multiplicador entre coincidencias)
    band_name = None
    for rb in rules.risk_bands:
        ok, miss, _, _, _ = eval_clause_ex(rb, snap)
        if miss:
            missing_overall.extend(miss)
        if ok:
            eff = rb.get("effect", {}) or {}
            rm = float(eff.get("risk_multiplier_macro", risk_multiplier))
            if rm < risk_multiplier:
                risk_multiplier = rm
                band_name = rb.get("name")

    # ---- NUEVO: acumulador de caps por grupo (no filtra elegibilidad) ----
    group_caps_acc: dict[str, dict[str, int]] = {}

    def _merge_group_caps(dst: dict, src: dict):
        """Funde caps conservadoramente: mismo grupo => toma el MIN de max_open_positions."""
        if not isinstance(src, dict):
            return
        for g, cfg in src.items():
            if not isinstance(cfg, dict):
                continue
            try:
                m = int(cfg.get("max_open_positions", 0))
            except Exception:
                continue
            if g in dst:
                dst[g]["max_open_positions"] = min(dst[g]["max_open_positions"], m)
            else:
                dst[g] = {"max_open_positions": m}

    # 3) scenarios (cuenta verdaderas y soporta confirmations)
    active_scenarios: list[str] = []
    for sc in rules.scenarios:
        name = sc.get("name")
        ok, miss, true_cnt, total_cnt, mode = eval_clause_ex(sc, snap)
        if miss:
            missing_overall.extend(miss)

        # Activación base del escenario
        if mode == "all":
            activate = ok
        elif mode == "any":
            needed = min(min_true, total_cnt if total_cnt > 0 else 1)
            activate = true_cnt >= needed
        else:
            activate = False

        # LOG de auditoría previo a confirmations
        logger.debug(
            f"{tier.upper()} scen={name} mode={mode} true={true_cnt}/{total_cnt} "
            f"min_true={min_true} pre_conf={activate}"
        )

        # Confirmations (si existen) → deben pasar también
        if activate:
            conf_ok, conf_missing, _conf_details = eval_confirmations(sc.get("confirmations"), snap)
            if conf_missing:
                missing_overall.extend(conf_missing)
            activate = activate and conf_ok
            logger.debug(
                f"{tier.upper()} scen={name} confirmations -> ok={conf_ok} "
                f"missing={conf_missing if conf_missing else '[]'} final={activate}"
            )

        if activate:
            active_scenarios.append(name)
            eff = sc.get("effect", {}) or {}

            # allow_new_entries (si aplica)
            if eff.get("allow_new_entries"):
                can_open_new = can_open_new and True

            # allowed_groups (intersección) — se mantiene
            if "allowed_groups" in eff:
                g = eff.get("allowed_groups") or []
                allowed_groups = [x for x in allowed_groups if x in g]

            # group_caps — YA NO filtra allowed_groups; solo acumula en meta
            if "group_caps" in eff:
                _merge_group_caps(group_caps_acc, eff.get("group_caps") or {})

            # colecciones para suggestions
            prioritize.extend(eff.get("prioritize_symbols", []) or [])
            avoid.extend(eff.get("avoid_symbols", []) or [])

    prioritize = unique(prioritize)
    avoid = unique(avoid)

    # 4) buy suggestions para UI
    suggestions = build_buy_suggestions(active_scenarios, rules, avoid)

    meta = {
        "hard_blocks": hard_hits,
        "risk_band": band_name or "normal",
        "triggered_scenarios": active_scenarios,
        "can_open_new": bool(can_open_new),
        "blocked_by": blocked_by,
        "block_exclusions": block_exclusions,
        "buy_suggestions": suggestions,
        "missing_features": sorted(unique(missing_overall)),
        "feature_sample_size": len(snap),
        "group_caps": group_caps_acc,  # <-- NUEVO: caps para que los aplique el router
    }

    reason = reason or (
        f"band={meta['risk_band']} hb={'+'.join(hard_hits) if hard_hits else 'none'}"
    )

    return EvalResult(
        tier=tier,
        ts=ts,
        long_permission=True,  # long-only global
        risk_multiplier=float(risk_multiplier),
        allowed_groups=allowed_groups,
        prioritize=prioritize,
        avoid=avoid,
        reason=reason,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# Fusión fast/slow
# ---------------------------------------------------------------------------


def fuse_states(fast: EvalResult, slow: EvalResult, rules: Rules) -> EvalResult:
    cfg = rules.fuse_cfg or {}
    allow_logic = cfg.get("allow_logic", "fast_and_slow")
    risk_logic = cfg.get("risk_logic", "min")
    prioritize_logic = cfg.get("prioritize_logic", "intersection")
    avoid_logic = cfg.get("avoid_logic", "union")
    groups_logic = cfg.get("groups_logic", "intersection")

    def inter(a: list[str], b: list[str]) -> list[str]:
        return [x for x in a if x in set(b)]

    def uni(a: list[str], b: list[str]) -> list[str]:
        return unique(a + b)

    # can_open_new
    if allow_logic == "fast_and_slow":
        can_open_new = fast.meta.get("can_open_new", False) and slow.meta.get("can_open_new", False)
    elif allow_logic == "fast_or_slow":
        can_open_new = fast.meta.get("can_open_new", False) or slow.meta.get("can_open_new", False)
    else:
        can_open_new = False

    # risk multiplier
    if risk_logic == "min":
        rm = min(fast.risk_multiplier, slow.risk_multiplier)
    elif risk_logic == "max":
        rm = max(fast.risk_multiplier, slow.risk_multiplier)
    else:
        rm = min(fast.risk_multiplier, slow.risk_multiplier)

    # groups
    if groups_logic == "intersection":
        allowed_groups = inter(fast.allowed_groups, slow.allowed_groups)
    else:
        allowed_groups = uni(fast.allowed_groups, slow.allowed_groups)

    # prioritize & avoid
    if prioritize_logic == "intersection":
        prioritize = inter(fast.prioritize, slow.prioritize)
    else:
        prioritize = uni(fast.prioritize, slow.prioritize)

    if avoid_logic == "union":
        avoid = uni(fast.avoid, slow.avoid)
    else:
        avoid = inter(fast.avoid, slow.avoid)

    # ---- NEW: merge group_caps conservador (min por grupo) ----
    def _merge_caps(a: dict | None, b: dict | None) -> dict:
        out: dict[str, dict[str, int]] = {}

        def _add(src: dict | None):
            if not isinstance(src, dict):
                return
            for g, cfg in src.items():
                if not isinstance(cfg, dict):
                    continue
                try:
                    m = int(cfg.get("max_open_positions", 0))
                except Exception:
                    continue
                if g in out:
                    out[g]["max_open_positions"] = min(out[g]["max_open_positions"], m)
                else:
                    out[g] = {"max_open_positions": m}

        _add(a)
        _add(b)
        return out

    fused_caps = _merge_caps(fast.meta.get("group_caps"), slow.meta.get("group_caps"))

    # buy suggestions (intersección + promedio score)
    f_sug = {s["symbol"]: s for s in fast.meta.get("buy_suggestions", [])}
    s_sug = {s["symbol"]: s for s in slow.meta.get("buy_suggestions", [])}
    fused_syms = set(f_sug.keys()) & set(s_sug.keys())
    buy_suggestions: list[dict[str, Any]] = []
    for sym in fused_syms:
        fs = f_sug[sym]
        ss = s_sug[sym]
        buy_suggestions.append(
            {
                "symbol": sym,
                "group": fs.get("group") or ss.get("group"),
                "score": round((float(fs.get("score", 0)) + float(ss.get("score", 0))) / 2.0, 2),
                "reasons": unique((fs.get("reasons") or []) + (ss.get("reasons") or [])),
                "tags": unique((fs.get("tags") or []) + (ss.get("tags") or [])),
            }
        )
    buy_suggestions.sort(key=lambda x: x["score"], reverse=True)

    meta = {
        "hard_blocks": unique(
            (fast.meta.get("hard_blocks") or []) + (slow.meta.get("hard_blocks") or [])
        ),
        "risk_band": f"fast={fast.meta.get('risk_band')} | slow={slow.meta.get('risk_band')}",
        "triggered_scenarios": unique(
            (fast.meta.get("triggered_scenarios") or [])
            + (slow.meta.get("triggered_scenarios") or [])
        ),
        "can_open_new": bool(can_open_new),
        "blocked_by": slow.meta.get("blocked_by") or fast.meta.get("blocked_by"),
        "buy_suggestions": buy_suggestions,
        "fuse": rules.fuse_cfg,
        "group_caps": fused_caps,  # <-- NEW
    }

    return EvalResult(
        tier="fused",
        ts=now_utc(),
        long_permission=True,
        risk_multiplier=float(rm),
        allowed_groups=allowed_groups,
        prioritize=prioritize,
        avoid=avoid,
        reason=f"fused allow={allow_logic} risk={risk_logic}",
        meta=meta,
    )


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------


def run_once(conn, rules: Rules, tier: str):
    rows = get_latest_features(conn)
    return evaluate_tier(tier=tier, snapshot_rows=rows, rules=rules)


def main() -> None:
    creds = load_db_creds()
    rules_path = env_str("RULES_PATH", "configs/fundamental_rules.yaml")
    rules = load_rules(rules_path)

    fast_every = env_int("FAST_EVERY_SEC", 300)
    slow_every = env_int("SLOW_EVERY_SEC", 3600)

    logger.info(
        f"Arrancando evaluator | DB={creds.host}:{creds.port}/{creds.dbname} "
        f"fast={fast_every}s slow={slow_every}s rules={rules_path}"
    )

    next_fast = now_utc()
    next_slow = now_utc()
    fast_res: EvalResult | None = None

    while True:
        try:
            with closing(psycopg2.connect(**creds.__dict__)) as conn:
                did_work = False
                now = now_utc()

                # FAST
                if now >= next_fast:
                    t0 = time.time()
                    fast_res = run_once(conn, rules, tier="fast")
                    ms = int((time.time() - t0) * 1000)
                    logger.info(
                        f"Eval fast | can_open_new={fast_res.meta['can_open_new']} "
                        f"rm={fast_res.risk_multiplier:.2f} band={fast_res.meta['risk_band']} "
                        f"hb={'+'.join(fast_res.meta['hard_blocks']) if fast_res.meta['hard_blocks'] else 'none'} "
                        f"scen={len(fast_res.meta['triggered_scenarios'])} "
                        f"groups={','.join(fast_res.allowed_groups)} "
                        f"missing={len(fast_res.meta['missing_features'])} "
                        f"snap={fast_res.meta['feature_sample_size']} | ms={ms}"
                    )
                    insert_macro_state(conn, **fast_res.__dict__)
                    did_work = True
                    next_fast = now + timedelta(seconds=fast_every)

                # SLOW (+ FUSED)
                if now >= next_slow:
                    t0 = time.time()
                    slow_res = run_once(conn, rules, tier="slow")
                    ms = int((time.time() - t0) * 1000)
                    logger.info(
                        f"Eval slow | can_open_new={slow_res.meta['can_open_new']} "
                        f"rm={slow_res.risk_multiplier:.2f} band={slow_res.meta['risk_band']} "
                        f"hb={'+'.join(slow_res.meta['hard_blocks']) if slow_res.meta['hard_blocks'] else 'none'} "
                        f"scen={len(slow_res.meta['triggered_scenarios'])} "
                        f"groups={','.join(slow_res.allowed_groups)} "
                        f"missing={len(slow_res.meta['missing_features'])} "
                        f"snap={slow_res.meta['feature_sample_size']} | ms={ms}"
                    )
                    insert_macro_state(conn, **slow_res.__dict__)

                    if fast_res is None:
                        fast_res = slow_res  # fallback defensivo
                    fused = fuse_states(fast_res, slow_res, rules)
                    logger.info(
                        f"FUSE | allow={rules.fuse_cfg.get('allow_logic', 'fast_and_slow')} "
                        f"risk={rules.fuse_cfg.get('risk_logic', 'min')} -> "
                        f"can_open_new={fused.meta['can_open_new']} "
                        f"rm={fused.risk_multiplier:.2f} groups={','.join(fused.allowed_groups)} "
                        f"prio={len(fused.prioritize)} avoid={len(fused.avoid)}"
                    )
                    insert_macro_state(conn, **fused.__dict__)
                    did_work = True
                    next_slow = now + timedelta(seconds=slow_every)

        except Exception as e:
            logger.error(f"[evaluator] error: {e}")

        if not did_work:
            # dormir 1s para no consumir CPU entre checks
            time.sleep(1)


if __name__ == "__main__":
    main()
